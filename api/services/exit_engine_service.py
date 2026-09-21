"""持仓退出评估引擎（确定性、无 LLM）。

背景与问题
----------
产品此前没有任何"卖出引擎"：全仓库唯一的卖出逻辑硬编码在
``api/services/paper_trading_service.py`` 里（浮亏 6% 清仓、浮盈 10% 减 30%），
它只服务虚拟盘，且完全无视每份研报自己算出的止损/止盈锚点。结果是
"买入研究"做得越来越重，而"持有期管理"完全空缺：计划里的硬止损、分批止盈、
移动止盈、时间止损、逻辑失效条件从来没有被真正监控过一次。

本模块把 ``trade_plans`` 里那份计划变成一条**逐持仓的纪律信号**：

  0. **生效前置校验（方向自洽）** —— 计划方向不是 BUY，或多头价格锚点不自洽
     （止损价不低于入场、止盈档位低于入场）时，规则 3–5 的多头语义不再适用，
     只记录不拦截，交回人工；
  1. 停牌 / 无有效行情 —— 观察，请人工确认；
  2. 没有可监控的交易计划 —— 观察，并提示补建计划（产品指标：计划覆盖率）；
  3. 硬止损（跌破计划止损价）—— 清仓，最高优先级；
  4. 移动止盈（自高点回撤超过计划阈值）—— 减仓；
  5. 分批止盈（触及计划止盈阶梯）—— 按已触及的最高档位减仓；
  6. 时间止损（持有交易日数达计划上限）—— 观察，建议释放资金复核；
  7. 逻辑失效条件 —— 永远交回人工复核（基本面/技术面条件无法用价格判定）；
  8. 以上都不触发 —— 按计划持有。

方向语义（规则 0 的原因）
------------------------
本模块的价格规则全部按**多头持仓**语义书写：止损在下方（``px <= stop``）、
止盈在上方（``px >= tier``）。但 ``trade_plans.direction`` 允许 SELL / HOLD，
而 SELL 计划里的价格锚点并不是多头止损位——它更像是"看空逻辑失效价"。
旧实现完全无视 ``direction``，把 SELL 计划的失效价当成多头止损位，
于是只要该价高于现价（这在看空计划里恰恰是常态），**首日就会推出"清仓 100%"**，
并通过企微/WPS 推送给真实用户，还会据此生成模拟卖单。

因此在应用任何多头价格规则之前，先做方向自洽校验；不通过就不产出方向性动作。

设计原则
--------
* **纯确定性**：只做数值比较，不调用模型、不访问网络，同样的输入永远同样的输出。
* **A 股可执行性**：所有建议都经过 T+1 可卖数量、100 股整手、涨跌停可成交性校正；
  信号本身不会因为不可执行而消失（"该止损"这件事仍然要说出来），
  只是把可下单股数归零并附上约束说明。
* **不编造数据**：没有可靠峰值就不做移动止盈判断；没有信号日就不猜持有天数。
* **永不抛异常**：单只标的评估失败只跳过该标的，绝不拖垮整个组合视图。
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from api.database import ImportedPortfolioPositionDB
from api.services import trade_plan_service
from api.services.tracking_board_service import _fetch_live_quotes, _to_float
from tradingagents.agents.utils.direction import normalize_direction, to_trading_decision
from tradingagents.dataflows.trade_calendar import cn_today_str, is_cn_trading_day

logger = logging.getLogger(__name__)

# 涨跌停幅度直接复用价格预警服务里那份已对齐前端的实现（ST 5% / .BJ 30% /
# 300·301·688·689 20% / 其余 10%）。仅在该模块不可导入时退回本地等价实现，
# 保证退出引擎自身永远可导入（不因一个无关模块的语法错误而失去卖出纪律）。
try:
    from api.services.tracking_price_alert_service import infer_daily_limit_pct
except Exception:  # pragma: no cover - 仅在依赖模块不可导入时触发
    logger.warning("[ExitEngine] 涨跌停幅度函数导入失败，使用本地等价实现")

    def infer_daily_limit_pct(symbol: str, name: str = "") -> float:
        sym = (symbol or "").upper()
        nm = (name or "").upper()
        if "ST" in nm:
            return 5.0
        if sym.endswith(".BJ"):
            return 30.0
        if sym.startswith(("300", "301", "688", "689")):
            return 20.0
        return 10.0

# 与产品其它出口保持一致的免责声明：这里是纪律提示，不是投资建议。
EXIT_DISCLAIMER = "以上仅为纪律提示，不构成投资建议。"

# --------------------------------------------------------------------------- #
# 动作与优先级
# --------------------------------------------------------------------------- #

ACTION_HOLD = "持有"
ACTION_REDUCE = "减仓"
ACTION_EXIT = "清仓"
ACTION_WATCH = "观察"

PRIORITY_HIGH = "high"
PRIORITY_MEDIUM = "medium"
PRIORITY_LOW = "low"

ALL_ACTIONS = (ACTION_HOLD, ACTION_REDUCE, ACTION_EXIT, ACTION_WATCH)
ALL_PRIORITIES = (PRIORITY_HIGH, PRIORITY_MEDIUM, PRIORITY_LOW)

# --------------------------------------------------------------------------- #
# 触发器机器码（前后端与预警共用，取值不可随意改动）
# --------------------------------------------------------------------------- #

TRIGGER_HARD_STOP = "hard_stop"
TRIGGER_TAKE_PROFIT = "take_profit"
TRIGGER_TRAILING_STOP = "trailing_stop"
TRIGGER_TIME_STOP = "time_stop"
TRIGGER_INVALIDATION_REVIEW = "invalidation_review"
TRIGGER_NO_PLAN = "no_plan"
TRIGGER_SUSPENDED = "suspended"
TRIGGER_T1_BLOCKED = "t1_blocked"
TRIGGER_LIMIT_UP_BLOCKED = "limit_up_blocked"
TRIGGER_LIMIT_DOWN_BLOCKED = "limit_down_blocked"
# 计划方向不是 BUY / 价格锚点方向不自洽：多头语义的价格规则不再适用，只记录不拦截。
TRIGGER_PLAN_NOT_LONG = "plan_not_long"

# 规则 0 的三值结果。见 ``anchor_gate``。
ANCHOR_OK = "ok"
ANCHOR_UNKNOWN = "unknown"
ANCHOR_INCOHERENT = "incoherent"

ALL_TRIGGERS = (
    TRIGGER_HARD_STOP,
    TRIGGER_TAKE_PROFIT,
    TRIGGER_TRAILING_STOP,
    TRIGGER_TIME_STOP,
    TRIGGER_INVALIDATION_REVIEW,
    TRIGGER_NO_PLAN,
    TRIGGER_SUSPENDED,
    TRIGGER_T1_BLOCKED,
    TRIGGER_LIMIT_UP_BLOCKED,
    TRIGGER_LIMIT_DOWN_BLOCKED,
    TRIGGER_PLAN_NOT_LONG,
)

# 涨跌停判定容差：盘中价格四舍五入后极少正好等于上限，留 0.15% 的贴近量。
_LIMIT_TOUCH_TOLERANCE_PCT = 0.15

# 移动止盈触发默认减仓比例（计划未给出减仓比例时的行业惯例）
_TRAILING_REDUCE_PCT = 50.0

# 卖出信号被 T+1 阻断时的说明
_T1_CONSTRAINT = "T+1：今日买入的股份不可卖出，需等下一交易日"
_SUSPENDED_CONSTRAINT = "停牌期间无法成交"
_LIMIT_UP_CONSTRAINT = "已接近/封住涨停，卖单可能无法成交"
_LIMIT_DOWN_CONSTRAINT = "已接近/封住跌停，卖单可能无法成交"
# 规则 0 未通过时的说明：卖出建议已暂停生效。
_DIRECTION_GATE_CONSTRAINT = "计划方向/价格锚点不支持自动止损止盈，卖出建议仅供人工复核"

# 交易日统计的最大回溯跨度：超过则视为信号日不可信，返回 None（不猜）
_MAX_TRADING_DAY_SPAN = 400

_PRIORITY_RANK = {PRIORITY_HIGH: 0, PRIORITY_MEDIUM: 1, PRIORITY_LOW: 2}


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #


@dataclass
class HoldingDecision:
    """单只持仓的退出纪律信号。

    ``action`` 与 ``suggested_pct`` 是"该做什么"，``suggested_shares`` 是
    "在 A 股规则下今天实际能下多少量"——两者可能不一致（T+1 / 整手 / 涨跌停），
    差异全部记录在 ``constraints`` 里，避免把"不可执行"悄悄变成"不用执行"。
    """

    symbol: str
    name: str
    action: str
    priority: str
    reasons: List[str]
    triggers: List[str]
    suggested_shares: int
    suggested_pct: float
    price: Optional[float]
    previous_close: Optional[float]
    pnl_pct: Optional[float]
    position: float
    available_position: float
    average_cost: Optional[float]
    constraints: List[str]
    plan_id: Optional[str]
    plan_source: Optional[str]
    plan_available: bool
    # 方向自洽校验结果（规则 0）。下游推送（企微/WPS）与模拟盘必须据此判断
    # "这条卖出建议是否可信"，而不是只看 action。
    plan_direction: Optional[str] = None
    anchor_gate: Optional[str] = None
    long_side_ok: bool = False
    label: str = EXIT_DISCLAIMER

    @property
    def direction_gate_passed(self) -> bool:
        """方向校验是否**明确**通过（``ANCHOR_OK``）。

        ``ANCHOR_UNKNOWN``（计划没写方向）不算明确通过：它允许判定继续，
        但下游若要对外推送/下单，应把它当作"未经确认"处理。
        """
        return self.anchor_gate == ANCHOR_OK

    @property
    def is_actionable_sell(self) -> bool:
        """该卖出建议是否通过了方向自洽校验、可以对外生效。

        只有方向**明确**为多头计划（``ANCHOR_OK``）的减仓/清仓才允许推送与
        生成模拟卖单；方向缺失或证明不自洽的一律只记录。这是"先只记录不拦截"
        的落地：网格上不拦截判定，但对外动作必须等方向被确认。
        """
        return (
            self.direction_gate_passed
            and self.action in (ACTION_REDUCE, ACTION_EXIT)
            and self.suggested_pct > 0
        )

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["is_actionable_sell"] = self.is_actionable_sell
        return data


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #


def _clean_symbol(symbol: Any) -> str:
    return str(symbol or "").strip().upper()


def _pos_float(value: Any) -> Optional[float]:
    """只接受正数，其余（含 0、负数、脏数据）视为"未提供"。"""
    num = _to_float(value)
    if num is None or num <= 0:
        return None
    return float(num)


def _quantity(value: Any) -> float:
    """持仓数量：允许 0，负数收敛为 0。"""
    num = _to_float(value)
    if num is None or num <= 0:
        return 0.0
    return float(num)


def _fmt(value: Any) -> str:
    """把数字渲染成中文提示里好读的形态（11.50 → 11.5，50.0 → 50）。"""
    num = _to_float(value)
    if num is None:
        return "—"
    text = f"{num:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def _dedupe(items: Sequence[str]) -> List[str]:
    out: List[str] = []
    for item in items:
        if item and item not in out:
            out.append(item)
    return out


def count_trading_days_between(start_date: Optional[str], end_date: Optional[str]) -> Optional[int]:
    """统计 (start_date, end_date] 区间内的 A 股交易日数。

    只数交易日，不数自然日——"时间止损 10 日"指的是 10 个交易日。
    日期不可解析、或跨度大得离谱（说明信号日不可信）时返回 None，绝不猜。
    """
    try:
        start = datetime.strptime(str(start_date)[:10], "%Y-%m-%d").date()
        end = datetime.strptime(str(end_date)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    if end < start:
        return 0
    if (end - start).days > _MAX_TRADING_DAY_SPAN:
        return None

    count = 0
    cursor = start
    while cursor < end:
        cursor = cursor + timedelta(days=1)
        try:
            if is_cn_trading_day(cursor.strftime("%Y-%m-%d")):
                count += 1
        except Exception:  # 日历不可用时退回"周末规则"，仍保持确定性
            if cursor.weekday() < 5:
                count += 1
    return count


def infer_trading_days_held(plan: Optional[Dict[str, Any]], today: Optional[str]) -> Optional[int]:
    """由计划的信号交易日推导已持有交易日数；信息不足时返回 None。"""
    if not isinstance(plan, dict) or not today:
        return None
    signal_date = str(plan.get("signal_trade_date") or "")[:10]
    if len(signal_date) != 10:
        return None
    return count_trading_days_between(signal_date, str(today)[:10])


def _normalize_quote(quote: Any) -> Dict[str, Any]:
    """兼容三种行情入参：完整报价字典、纯价格数字、None。"""
    if isinstance(quote, dict):
        return quote
    if quote is None or isinstance(quote, bool):
        return {}
    num = _to_float(quote)
    if num is None:
        return {}
    return {"price": num}


def _ladder_reached(plan: Dict[str, Any], price: float) -> List[tuple]:
    """已触及的止盈档位（按价格升序）。

    用 ``target_levels`` 而非 ``ladder_targets``：后者只认显式的分批阶梯，
    而结构化抽取回退来的计划只有一个 ``target_price``——那也是合法的止盈位，
    只是没有分档。统一入口避免"同一份计划在不同链路上档位数不一样"。
    """
    reached = []
    for tier_price, reduce_pct in trade_plan_service.target_levels(plan):
        if price >= tier_price:
            reached.append((float(tier_price), float(reduce_pct)))
    return reached


def _round_lot(shares: float) -> int:
    """向下取整到 100 股整手。"""
    return int(shares // 100) * 100


# --------------------------------------------------------------------------- #
# 方向自洽校验（规则 0）
# --------------------------------------------------------------------------- #


def _plan_direction(plan: Optional[Dict[str, Any]]) -> Optional[str]:
    """计划的标准化方向：``BUY`` / ``SELL`` / ``HOLD``，无法判定时为 ``None``。

    复用 :mod:`tradingagents.agents.utils.direction` 的规范化，接受
    ``BUY``/``看多``/``偏多`` 等写法；无法识别的一律返回 ``None``（fail-closed，
    即"不知道方向"），绝不猜测。
    """
    if not isinstance(plan, dict):
        return None
    raw = plan.get("direction")
    if raw in (None, ""):
        return None
    canonical = normalize_direction(raw)
    if canonical is None:
        return None
    return to_trading_decision(canonical)


def anchor_gate(plan: Optional[Dict[str, Any]]) -> Tuple[str, str]:
    """判断计划的价格锚点能否按**多头语义**解读。

    返回 ``(gate, 原因)``，``gate`` 取三值之一：

    ``ANCHOR_OK``
        方向明确为 BUY，且锚点自洽（止损低于入场、止盈高于入场）。规则 3–5 生效。
    ``ANCHOR_UNKNOWN``
        方向缺失，且锚点**未被证明**与多头语义矛盾。无法证伪就不拦截，
        但会记录一条 flag 供观察——这是"先只记录不拦截"的落地方式。
    ``ANCHOR_INCOHERENT``
        方向明确为 SELL/HOLD，或锚点可被证明方向错误（止损不低于入场、
        止盈档位不高于入场）。此时多头价格规则**必须**停止生效，否则
        SELL 计划的"看空逻辑失效价"会被当成多头止损位，
        只要它高于现价（看空计划里恰是常态）就会首日推出"清仓 100%"。

    入场价取 ``entry_high``（最保守的上沿），缺失时退回 ``entry_low``；
    两者都没有时无法校验，除非方向已明确非多头，否则按 ``ANCHOR_UNKNOWN`` 处理。
    """
    if not isinstance(plan, dict) or not plan:
        return ANCHOR_INCOHERENT, "计划缺失"

    direction = _plan_direction(plan)
    if direction is not None and direction != "BUY":
        return (
            ANCHOR_INCOHERENT,
            f"计划方向为 {direction}，其价格锚点不是多头止损/止盈位",
        )

    entry = _pos_float(plan.get("entry_high"))
    if entry is None:
        entry = _pos_float(plan.get("entry_low"))

    if entry is not None:
        stop_price = _pos_float(plan.get("hard_stop_price"))
        if stop_price is not None and stop_price >= entry:
            return (
                ANCHOR_INCOHERENT,
                f"硬止损 {_fmt(stop_price)} 不低于入场 {_fmt(entry)}，"
                f"按多头语义会在首日直接判定清仓",
            )

        for tier_price, _reduce_pct in trade_plan_service.target_levels(plan):
            tier = _pos_float(tier_price)
            if tier is not None and tier <= entry:
                return (
                    ANCHOR_INCOHERENT,
                    f"止盈档位 {_fmt(tier)} 不高于入场 {_fmt(entry)}，不是多头止盈位",
                )

    if direction is None:
        return ANCHOR_UNKNOWN, "计划未标明方向（BUY/SELL/HOLD），无法确认多头语义"

    return ANCHOR_OK, ""


def long_side_anchors_coherent(plan: Optional[Dict[str, Any]]) -> Tuple[bool, str]:
    """锚点是否可按多头语义解读（``anchor_gate`` 的布尔视图）。

    ``ANCHOR_UNKNOWN`` 视为通过：无法证伪的方向缺失不拦截，只记录。
    """
    gate, reason = anchor_gate(plan)
    return gate != ANCHOR_INCOHERENT, reason


# --------------------------------------------------------------------------- #
# 单持仓评估
# --------------------------------------------------------------------------- #


def evaluate_holding(
    *,
    symbol: str,
    name: str = "",
    position: Any,
    available_position: Any,
    average_cost: Any = None,
    price: Any = None,
    previous_close: Any = None,
    change_pct: Any = None,
    plan: Optional[Dict[str, Any]] = None,
    plan_id: Optional[str] = None,
    plan_source: Optional[str] = None,
    today: Optional[str] = None,
    trading_days_held: Optional[int] = None,
    peak_price: Any = None,
    suspended: bool = False,
) -> HoldingDecision:
    """对单只持仓做一次确定性退出评估。

    规则按优先级依次判定，一只持仓只得到一个 ``action``，但可以同时命中
    多个 ``triggers`` / ``reasons``（例如既跌破止损又触及止盈档位的脏数据场景，
    此时止损优先，止盈只作为附加提示）。
    """
    raw_symbol = str(symbol or "").strip()
    sym = _clean_symbol(symbol)
    nm = str(name or "")

    held = _quantity(position)
    available = _quantity(available_position)
    cost = _pos_float(average_cost)
    px = _pos_float(price)
    prev_close = _pos_float(previous_close)
    peak = _pos_float(peak_price)

    # 行情里常常只有价格与昨收：按定义补算涨跌幅，涨跌停判定才有依据。
    chg = _to_float(change_pct)
    if chg is None and px is not None and prev_close is not None:
        chg = round((px - prev_close) / prev_close * 100.0, 4)

    pnl_pct = round((px - cost) / cost * 100.0, 2) if (px is not None and cost) else None

    plan_dict = plan if isinstance(plan, dict) else None
    plan_available = bool(plan_dict) and trade_plan_service.is_monitorable(plan_dict)
    resolved_plan_id = plan_id or (plan_dict.get("id") if plan_dict else None)
    resolved_plan_source = plan_source or (plan_dict.get("source") if plan_dict else None)
    conditions = _plan_conditions(plan_dict)

    reasons: List[str] = []
    triggers: List[str] = []
    constraints: List[str] = []
    action = ACTION_HOLD
    priority = PRIORITY_LOW
    suggested_pct = 0.0
    decided = False  # 是否已由某个规则给出方向性动作
    # 规则 0 的校验结果。没有可用计划时无从校验，保持 ANCHOR_INCOHERENT——
    # 下游因此不会把无计划标的的建议当成生效建议。
    plan_dir: Optional[str] = None
    anchor_status = ANCHOR_INCOHERENT
    long_ok = False

    if suspended or px is None:
        # 规则 1：停牌 / 无有效行情——没有价格就没有任何可判定的价格锚点。
        action, priority = ACTION_WATCH, PRIORITY_LOW
        triggers.append(TRIGGER_SUSPENDED)
        reasons.append("停牌或无有效行情，无法评估，请人工确认")
        constraints.append(_SUSPENDED_CONSTRAINT)
        # 逻辑失效条件与行情无关（基本面/技术面），没有行情时同样要交回人工复核
        if plan_available:
            _append_invalidation(conditions, triggers, reasons)
        decided = True

    elif not plan_available:
        # 规则 2：没有可监控计划——无法把"持有"说成纪律，只能提示补建计划。
        action, priority = ACTION_WATCH, PRIORITY_MEDIUM
        triggers.append(TRIGGER_NO_PLAN)
        reasons.append(
            f"该持仓没有可用的交易计划，无法给出纪律化提示；"
            f"建议先对 {raw_symbol or '该标的'} 生成一份深度分析以得到止损/止盈锚点"
        )
        decided = True

    else:
        stop_price = _pos_float(plan_dict.get("hard_stop_price"))
        trailing_pct = _pos_float(plan_dict.get("trailing_stop_pct"))
        time_stop_days = _plan_int(plan_dict.get("time_stop_days"))
        signal_date = str(plan_dict.get("signal_trade_date") or "").strip() or "—"

        # 规则 0：生效前置校验——价格规则全按多头语义书写，方向不自洽时不得套用。
        # 旧实现无视 direction，把 SELL 计划的失效价当成多头止损位：该价高于现价时
        # 首日就命中 `px <= stop_price` → 清仓 100%，并推送给真实用户。
        anchor_status, anchor_note = anchor_gate(plan_dict)
        plan_dir = _plan_direction(plan_dict)
        long_ok = anchor_status != ANCHOR_INCOHERENT
        if not long_ok:
            action, priority = ACTION_WATCH, PRIORITY_MEDIUM
            triggers.append(TRIGGER_PLAN_NOT_LONG)
            reasons.append(
                f"计划价格锚点无法按多头语义解读（{anchor_note}），"
                f"已暂停自动止损/止盈判定，请人工复核该标的的持有逻辑"
            )
            decided = True

        # 规则 3：硬止损（最高优先级）——计划失效的最后一道防线。
        if not decided and stop_price is not None and px <= stop_price:
            action, priority = ACTION_EXIT, PRIORITY_HIGH
            triggers.append(TRIGGER_HARD_STOP)
            reasons.append(
                f"现价 {_fmt(px)} 已跌破硬止损 {_fmt(stop_price)}（计划 {signal_date}）"
            )
            suggested_pct = 100.0
            decided = True

        # 规则 4：移动止盈保护——只在有可靠峰值时才判定，绝不编造高点。
        if not decided and trailing_pct is not None and peak is not None:
            trigger_price = peak * (1.0 - trailing_pct / 100.0)
            if px <= trigger_price + 1e-9:
                action, priority = ACTION_REDUCE, PRIORITY_HIGH
                triggers.append(TRIGGER_TRAILING_STOP)
                reasons.append(
                    f"自高点 {_fmt(peak)} 回撤已超 {_fmt(trailing_pct)}%，触及移动止盈保护"
                )
                suggested_pct = _TRAILING_REDUCE_PCT
                decided = True

        # 规则 5：分批止盈——命中多档时按最高档位的减仓比例执行。
        # 整段以 `long_ok` 为前提：止盈档位只有在多头语义下才成立，否则
        # "已触及止盈位"本身就是错的（SELL 计划的档位是下方目标位）。
        reached = _ladder_reached(plan_dict, px) if long_ok else []
        if reached:
            top_price, top_pct = reached[-1]
            triggers.append(TRIGGER_TAKE_PROFIT)
            reasons.append(
                f"现价 {_fmt(px)} 已触及止盈位 {_fmt(top_price)}，建议按计划减仓 {_fmt(top_pct)}%"
            )
            if len(reached) > 1:
                detail = "、".join(f"{_fmt(p)}({_fmt(r)}%)" for p, r in reached)
                reasons.append(f"同时已触及 {len(reached)} 个止盈档位：{detail}")
            if not decided:
                action, priority = ACTION_REDUCE, PRIORITY_MEDIUM
                suggested_pct = float(top_pct)
                decided = True

        # 规则 6：时间止损——价格没给信号，但资金效率已经不划算。
        if not decided and time_stop_days and trading_days_held is not None:
            if int(trading_days_held) >= int(time_stop_days):
                action, priority = ACTION_WATCH, PRIORITY_LOW
                triggers.append(TRIGGER_TIME_STOP)
                reasons.append(
                    f"距信号已 {int(trading_days_held)} 个交易日，达计划时间止损 "
                    f"{int(time_stop_days)} 日，建议释放资金复核"
                )
                decided = True

        # 规则 7：逻辑失效条件——基本面/技术面判断，价格推不出来，永远交回人工。
        if conditions:
            _append_invalidation(conditions, triggers, reasons)
            if not decided:
                action, priority = ACTION_WATCH, PRIORITY_MEDIUM
                decided = True

        # 规则 8：以上都不触发，按计划持有。
        if not decided:
            action, priority = ACTION_HOLD, PRIORITY_LOW
            reasons.append("现价未触及计划中的任何止损/止盈/时间条件，按计划持有")

    # ------------------------------------------------------------------ #
    # A 股可执行性校正（对每一条决策生效）
    # ------------------------------------------------------------------ #
    wants_sell = action in (ACTION_REDUCE, ACTION_EXIT) and suggested_pct > 0 and held > 0
    suggested_shares = 0
    if wants_sell:
        if available <= 0:
            # T+1：信号是真的，只是今天下不了单。
            constraints.append(_T1_CONSTRAINT)
            triggers.append(TRIGGER_T1_BLOCKED)
        else:
            lot_shares = _round_lot(held * suggested_pct / 100.0)
            available_lot = _round_lot(available)
            if available_lot <= 0:
                # 可卖余额不足一手：允许按精确余额卖出（常见于零股/部分解锁）
                available_lot = int(available)
            suggested_shares = min(lot_shares, available_lot)
            if suggested_shares <= 0 and available < 100:
                suggested_shares = int(available)
            suggested_shares = min(suggested_shares, int(held))

    limit_pct = infer_daily_limit_pct(sym, nm)
    if chg is not None:
        if chg >= limit_pct - _LIMIT_TOUCH_TOLERANCE_PCT:
            if _LIMIT_UP_CONSTRAINT not in constraints:
                constraints.append(_LIMIT_UP_CONSTRAINT)
            triggers.append(TRIGGER_LIMIT_UP_BLOCKED)
        if chg <= -(limit_pct - _LIMIT_TOUCH_TOLERANCE_PCT):
            if _LIMIT_DOWN_CONSTRAINT not in constraints:
                constraints.append(_LIMIT_DOWN_CONSTRAINT)
            triggers.append(TRIGGER_LIMIT_DOWN_BLOCKED)

    if plan_available and anchor_status == ANCHOR_INCOHERENT:
        constraints.append(_DIRECTION_GATE_CONSTRAINT)

    return HoldingDecision(
        symbol=sym,
        name=nm,
        action=action,
        priority=priority,
        reasons=reasons,
        triggers=_dedupe(triggers),
        suggested_shares=int(suggested_shares),
        suggested_pct=round(float(suggested_pct), 2),
        price=round(px, 4) if px is not None else None,
        previous_close=round(prev_close, 4) if prev_close is not None else None,
        pnl_pct=pnl_pct,
        position=held,
        available_position=available,
        average_cost=round(cost, 4) if cost is not None else None,
        constraints=_dedupe(constraints),
        plan_id=resolved_plan_id,
        plan_source=resolved_plan_source,
        plan_available=plan_available,
        plan_direction=plan_dir,
        anchor_gate=anchor_status if plan_available else None,
        long_side_ok=long_ok,
        label=EXIT_DISCLAIMER,
    )


def _plan_conditions(plan: Optional[Dict[str, Any]]) -> List[str]:
    """计划里的逻辑失效条件（去掉空项与重复项）。"""
    if not isinstance(plan, dict):
        return []
    raw = plan.get("invalidation_conditions") or []
    if isinstance(raw, str):
        raw = [raw]
    out: List[str] = []
    for item in raw:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _append_invalidation(conditions: Sequence[str], triggers: List[str], reasons: List[str]) -> None:
    """把逻辑失效条件追加成"人工复核"提示；空列表则什么都不做。

    引擎不会假装判断基本面/技术面条件是否成立——只负责把模型当初写下的
    失效条件原样交回给人看。
    """
    if not conditions:
        return
    triggers.append(TRIGGER_INVALIDATION_REVIEW)
    reasons.append("需人工复核计划失效条件：" + "；".join(conditions))


def _plan_int(value: Any) -> Optional[int]:
    """计划里的整数字段（交易日、天数）；<=0 或不可解析视为未设置。"""
    num = _to_float(value)
    if num is None or num <= 0:
        return None
    return int(round(num))


# --------------------------------------------------------------------------- #
# 组合评估
# --------------------------------------------------------------------------- #


def evaluate_portfolio(
    db: Session,
    *,
    user_id: str,
    quotes: Optional[Dict[str, Any]] = None,
    today: Optional[str] = None,
    peak_price_provider: Optional[Callable[[str], Any]] = None,
) -> List[HoldingDecision]:
    """对该用户全部导入持仓给出退出纪律信号。

    ``quotes`` 为空时自动取实时行情；无行情的标的会落到"观察/停牌"分支而不是被丢掉。
    ``peak_price_provider`` 用于外部注入可靠峰值（如自高点统计表），默认不传——
    没有可靠数据就不做移动止盈判断。
    """
    if not user_id:
        return []

    try:
        rows = (
            db.query(ImportedPortfolioPositionDB)
            .filter(ImportedPortfolioPositionDB.user_id == user_id)
            .all()
        )
    except Exception as exc:  # 数据库异常也不能让持仓页整页挂掉
        logger.warning("[ExitEngine] 读取持仓失败 user=%s: %s", user_id, exc)
        return []

    if not rows:
        return []

    ref_day = str(today or cn_today_str())[:10]

    if quotes is None:
        symbols = sorted({_clean_symbol(row.symbol) for row in rows if row.symbol})
        if symbols:
            try:
                quotes = _fetch_live_quotes(symbols) or {}
            except Exception as exc:
                logger.warning("[ExitEngine] 行情获取失败：%s", exc)
                quotes = {}
        else:
            quotes = {}
    if not isinstance(quotes, dict):
        quotes = {}

    decisions: List[HoldingDecision] = []
    for row in rows:
        try:
            decision = _evaluate_row(
                db,
                row=row,
                user_id=user_id,
                quotes=quotes,
                today=ref_day,
                peak_price_provider=peak_price_provider,
            )
        except Exception as exc:
            # 单只标的失败只跳过自己，组合视图必须能出结果
            logger.warning(
                "[ExitEngine] 评估失败 user=%s symbol=%s: %s",
                user_id,
                getattr(row, "symbol", "?"),
                exc,
            )
            continue
        if decision is not None:
            decisions.append(decision)

    decisions.sort(key=_decision_sort_key)
    return decisions


def _evaluate_row(
    db: Session,
    *,
    row: Any,
    user_id: str,
    quotes: Dict[str, Any],
    today: str,
    peak_price_provider: Optional[Callable[[str], Any]] = None,
) -> HoldingDecision:
    sym = _clean_symbol(getattr(row, "symbol", ""))
    quote = _normalize_quote(quotes.get(sym) or quotes.get(getattr(row, "symbol", "")))

    plan_row = trade_plan_service.get_monitor_plan(db, user_id=user_id, symbol=sym)
    plan = trade_plan_service.plan_dict_from_row(plan_row)

    peak: Optional[float] = None
    if peak_price_provider is not None:
        try:
            peak = _pos_float(peak_price_provider(sym))
        except Exception as exc:
            logger.debug("[ExitEngine] 峰值获取失败 %s: %s", sym, exc)
            peak = None

    days_held = infer_trading_days_held(plan, today)

    return evaluate_holding(
        symbol=sym,
        name=(getattr(row, "security_name", None) or ""),
        position=getattr(row, "current_position", None),
        available_position=getattr(row, "available_position", None),
        average_cost=getattr(row, "average_cost", None),
        price=quote.get("price"),
        previous_close=quote.get("previous_close"),
        change_pct=quote.get("change_pct"),
        plan=plan,
        plan_id=(plan_row.id if plan_row is not None else None),
        plan_source=(plan_row.source if plan_row is not None else None),
        today=today,
        trading_days_held=days_held,
        peak_price=peak,
        suspended=bool(quote.get("suspended")),
    )


def _decision_sort_key(decision: HoldingDecision):
    """优先级高者在前；同优先级按浮亏从大到小（最需要处理的先看到）。"""
    pnl = decision.pnl_pct
    return (
        _PRIORITY_RANK.get(decision.priority, len(_PRIORITY_RANK)),
        0 if pnl is not None else 1,
        pnl if pnl is not None else 0.0,
        decision.symbol,
    )


def summarize_decisions(decisions: Optional[List[HoldingDecision]]) -> Dict[str, Any]:
    """组合级汇总——给前端顶部卡片与产品指标（计划覆盖率）直接用。"""
    items = [d for d in (decisions or []) if d is not None]
    summary: Dict[str, Any] = {
        "total": len(items),
        ACTION_EXIT: 0,
        ACTION_REDUCE: 0,
        ACTION_HOLD: 0,
        ACTION_WATCH: 0,
        "high_priority": 0,
        "with_plan": 0,
        "without_plan": 0,
        "label": EXIT_DISCLAIMER,
    }
    for decision in items:
        if decision.action in summary:
            summary[decision.action] += 1
        if decision.priority == PRIORITY_HIGH:
            summary["high_priority"] += 1
        if decision.plan_available:
            summary["with_plan"] += 1
        else:
            summary["without_plan"] += 1
    return summary
