"""交易计划（TradePlan）结构化服务。

背景与问题
----------
模型在 trader 节点其实已经算出了一份完整的专业计划：方向、入场区间、总仓上限、
首仓、硬止损、分批止盈、移动止盈、时间止损、逻辑失效条件。但这些内容过去只以
散文形式存在于 ``trader_investment_plan`` / ``final_trade_decision`` 里。

生产库实测：提到"止盈"的研报 3922 份，而结构化 ``target_price`` 只有 532 份，
约 86% 的止盈锚点在抽取环节蒸发；止损锚点同样丢失约 39%。后果是计划既无法被
监控、也无法触发预警、更无法复盘——产品只有"买入研究"，没有"持有期管理"。

本模块把计划变成一等对象：
  1. 优先解析模型输出的机读块 ``<!-- TRADE_PLAN ... -->``（无损、确定性）；
  2. 缺失时回退到已有结构化抽取（``target_price`` / ``stop_loss_price``）；
  3. 再回退到已有的 ``risk_feedback_state``（硬约束 / 减险触发器 / 执行前置条件）。
结果落库到 ``trade_plans``，供退出引擎、双向预警与归因闭环使用。

机读块刻意采用**无花括号的 key: value 行格式**：既避开提示词模板 ``{{ }}``
转义歧义，也比 JSON 更容易被模型稳定复现、被规则稳定解析。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from uuid import uuid4

from sqlalchemy import or_
from sqlalchemy.orm import Session

from api.database import TradePlanDB

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 常量：状态、来源、卖出原因分类学
# --------------------------------------------------------------------------- #

STATUS_ACTIVE = "active"
STATUS_EXPIRED = "expired"
STATUS_FILLED = "filled"
STATUS_INVALIDATED = "invalidated"
STATUS_SUPERSEDED = "superseded"

SOURCE_BLOCK = "trade_plan_block"
SOURCE_EXTRACTED = "extracted"
SOURCE_DERIVED = "derived"

# 卖出原因分类学（交易台账与复盘共用同一套取值）
SELL_REASON_STOP_LOSS = "stop_loss"
SELL_REASON_TAKE_PROFIT = "take_profit"
SELL_REASON_INVALIDATION = "invalidation"
SELL_REASON_TIME_STOP = "time_stop"
SELL_REASON_TRAILING_STOP = "trailing_stop"
SELL_REASON_REBALANCE = "rebalance"
SELL_REASON_MANUAL = "manual"
SELL_REASON_UNKNOWN = "unknown"

SELL_REASON_LABELS = {
    SELL_REASON_STOP_LOSS: "止损",
    SELL_REASON_TAKE_PROFIT: "止盈",
    SELL_REASON_INVALIDATION: "逻辑失效",
    SELL_REASON_TIME_STOP: "时间止损",
    SELL_REASON_TRAILING_STOP: "移动止盈保护",
    SELL_REASON_REBALANCE: "换仓/调仓",
    SELL_REASON_MANUAL: "人工决定",
    SELL_REASON_UNKNOWN: "未归因",
}

_DIRECTION_MAP = {
    "buy": "BUY", "long": "BUY", "bullish": "BUY", "建仓": "BUY", "买入": "BUY",
    "增持": "BUY", "加仓": "BUY", "看多": "BUY", "偏多": "BUY", "做多": "BUY",
    "sell": "SELL", "short": "SELL", "bearish": "SELL", "卖出": "SELL", "清仓": "SELL",
    "减仓": "SELL", "减持": "SELL", "看空": "SELL", "偏空": "SELL", "做空": "SELL",
    "hold": "HOLD", "neutral": "HOLD", "持有": "HOLD", "观望": "HOLD", "中性": "HOLD",
}

_NULL_TOKENS = {
    "", "-", "—", "－", "none", "null", "n/a", "na", "nan", "nil",
    "无", "暂无", "不适用", "未知", "空", "[]", "()", "（）", "不设", "不适用。",
}

_NUM_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")
_BLOCK_RE = re.compile(r"<!--\s*TRADE_PLAN\b(.*?)(?:-->|$)", re.S | re.I)
_KV_RE = re.compile(r"^\s*[-*•]?\s*([A-Za-z_][A-Za-z0-9_]*)\s*[:：]\s*(.*?)\s*$")

# 键名别名：模型有时换个说法，统一收敛到规范键
_KEY_ALIASES = {
    "direction": "direction",
    "action": "direction",
    "horizon_days": "horizon_days",
    "hold_days": "horizon_days",
    "planned_hold_days": "horizon_days",
    "entry_low": "entry_low",
    "entry_high": "entry_high",
    "entry_range": "entry_range",
    "position_cap_pct": "position_cap_pct",
    "total_position_pct": "position_cap_pct",
    "max_position_pct": "position_cap_pct",
    "first_tranche_pct": "first_tranche_pct",
    "first_position_pct": "first_tranche_pct",
    "initial_position_pct": "first_tranche_pct",
    "hard_stop_price": "hard_stop_price",
    "stop_loss_price": "hard_stop_price",
    "stop_loss": "hard_stop_price",
    "take_profit_ladder": "take_profit_ladder",
    "take_profit": "take_profit_ladder",
    "target_ladder": "take_profit_ladder",
    "trailing_stop_pct": "trailing_stop_pct",
    "time_stop_days": "time_stop_days",
    "invalidation_conditions": "invalidation_conditions",
    "invalidation": "invalidation_conditions",
    "invalidation_condition": "invalidation_conditions",
}

# 解析器接受的机读块规范（与 tradingagents/prompts/zh.py 中 trader 提示词嵌入的
# 规范保持一致；由 tests/test_trade_plan_service.py 断言防漂移）。
# 之所以放在这里：便于未来"计划不合格时重新询问模型"等场景直接复用同一段文本。
TRADE_PLAN_BLOCK_SPEC = (
    "必须同时在决策末尾输出机读交易计划块（格式固定，不可省略，不可改动键名；"
    "该块缺失即视为计划无效）：\\n"
    "<!-- TRADE_PLAN\\n"
    "direction: BUY\\n"
    "horizon_days: 20\\n"
    "entry_low: 12.30\\n"
    "entry_high: 12.90\\n"
    "position_cap_pct: 9\\n"
    "first_tranche_pct: 2\\n"
    "hard_stop_price: 11.50\\n"
    "take_profit_ladder: 13.50:30, 14.80:40, 16.00:30\\n"
    "trailing_stop_pct: 8\\n"
    "time_stop_days: 10\\n"
    "invalidation_conditions: 跌破20日线 | 商誉减值超5亿 | 毛利率低于15%\\n"
    "-->\\n"
    "填写要求：\\n"
    "- direction 只可填 BUY / SELL / HOLD，且必须与 VERDICT 方向一致。\\n"
    "- entry_low / entry_high 为可执行的具体入场价格区间；不建议买入时都填 0。\\n"
    "- hard_stop_price 必须给出**具体价格**，不得写「跌破均线」这类无法监控的描述；不设止损填 0。\\n"
    "- take_profit_ladder 为分批止盈，格式 `价格:减仓百分比`，按价格从低到高、用英文逗号分隔；无则填 []。\\n"
    "- trailing_stop_pct 为移动止盈回撤触发百分比；time_stop_days 为时间止损交易日数；不适用填 0。\\n"
    "- invalidation_conditions 为可判定的逻辑失效条件，最多 3 条，每条不超过 20 字，用 | 分隔。\\n"
    "- 所有价格与百分比只写数字，不要带货币符号、千分位或百分号。"
)


# --------------------------------------------------------------------------- #
# 基础解析工具
# --------------------------------------------------------------------------- #


def _norm_symbol(symbol: str) -> str:
    return str(symbol or "").strip().upper()


def _is_null(raw: Any) -> bool:
    if raw is None:
        return True
    if isinstance(raw, str) and raw.strip().lower() in _NULL_TOKENS:
        return True
    return False


def _to_float(raw: Any) -> Optional[float]:
    """把模型可能写出的各种数字形态收敛成 float。"""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if _is_null(raw):
        return None
    s = str(raw).replace(",", "").replace("，", "")
    m = _NUM_RE.search(s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _to_price(raw: Any) -> Optional[float]:
    """价格：<=0 视为"未提供"（规范中 0 表示不适用）。"""
    value = _to_float(raw)
    if value is None or value <= 0:
        return None
    return round(value, 4)


def _to_pct(raw: Any) -> Optional[float]:
    """百分比：允许模型写成 9 或 9% 或 0.09（小数比例自动换算）。"""
    value = _to_float(raw)
    if value is None:
        return None
    text = str(raw)
    if value <= 1.0 and ("%" not in text and "％" not in text) and value != 0:
        # 0.09 这类小数比例按百分比理解；但 1 本身歧义，故仅在 <1 时换算
        if value < 1.0:
            value *= 100.0
    value = max(0.0, min(100.0, value))
    return round(value, 4)


def _to_int(raw: Any) -> Optional[int]:
    value = _to_float(raw)
    if value is None:
        return None
    if value <= 0:
        return None
    return int(round(value))


def _parse_range(raw: Any) -> Tuple[Optional[float], Optional[float]]:
    """解析入场区间，支持 "12.30-12.90" / "12.30~12.90" / "12.30 至 12.90"。"""
    if _is_null(raw):
        return None, None
    s = str(raw).replace(",", "").replace("，", "")
    parts = [p for p in re.split(r"[-~～—至到]", s) if _NUM_RE.search(p)]
    if len(parts) >= 2:
        low, high = _to_price(parts[0]), _to_price(parts[1])
    elif len(parts) == 1:
        low = high = _to_price(parts[0])
    else:
        return None, None
    if low is not None and high is not None and low > high:
        low, high = high, low
    return low, high


def _split_conditions(raw: Any) -> List[str]:
    """逻辑失效条件：按 | ; ； 、 或换行切分，去掉列表符号与空项。"""
    if _is_null(raw):
        return []
    if isinstance(raw, (list, tuple)):
        items: Iterable[Any] = raw
    else:
        items = re.split(r"[|;；、\n]|,\s*(?=[^\d])", str(raw))
    out: List[str] = []
    for item in items:
        text = re.sub(r"^\s*[-*•\d]+[.、)]?\s*", "", str(item or "")).strip()
        if text and text.lower() not in _NULL_TOKENS and text not in out:
            out.append(text)
    return out[:5]


def _parse_ladder(raw: Any) -> List[List[float]]:
    """解析分批止盈为 [[price, reduce_pct], ...]，按价格升序。

    支持 "13.50:30, 14.80:40"、"13.50(30%)"、"13.50 30%"、[[13.5, 30]] 等写法。
    只给价格不给比例时，按档数均分。
    """
    if _is_null(raw):
        return []
    if isinstance(raw, (list, tuple)):
        entries = list(raw)
    else:
        entries = [p for p in re.split(r"[,;；、|]|\s{2,}", str(raw)) if p and p.strip()]

    collected: Dict[float, Optional[float]] = {}
    for entry in entries:
        if isinstance(entry, (list, tuple)) and entry:
            price = _to_price(entry[0])
            pct = _to_pct(entry[1]) if len(entry) > 1 else None
        else:
            nums = _NUM_RE.findall(str(entry).replace(",", "").replace("，", ""))
            if not nums:
                continue
            price = _to_price(nums[0])
            pct = _to_pct(nums[1]) if len(nums) > 1 else None
        if price is None:
            continue
        if price not in collected:
            collected[price] = pct

    if not collected:
        return []

    ladder = [[price, collected[price]] for price in sorted(collected)]
    if all(pct is None for _, pct in ladder):
        share = round(100.0 / len(ladder), 2)
        ladder = [[price, share] for price, _ in ladder]

    # 比例合计超过 100% 时按比例缩放到 100%，避免"减仓 120%"这类脏数据
    total = sum(pct or 0.0 for _, pct in ladder)
    if total > 100.0:
        ladder = [[price, round((pct or 0.0) * 100.0 / total, 2)] for price, pct in ladder]
    return ladder


def _normalize_direction(raw: Any) -> Optional[str]:
    if _is_null(raw):
        return None
    key = str(raw).strip().lower()
    if key in _DIRECTION_MAP:
        return _DIRECTION_MAP[key]
    for token, mapped in _DIRECTION_MAP.items():
        if token in key:
            return mapped
    return None


# --------------------------------------------------------------------------- #
# 机读块解析
# --------------------------------------------------------------------------- #


def parse_trade_plan_block(text: str) -> Tuple[Dict[str, Any], List[str]]:
    """解析 ``<!-- TRADE_PLAN ... -->`` 机读块。

    返回 ``(计划字典, 警告列表)``。解析失败返回空字典——调用方据此降级，
    绝不因为格式问题丢弃整份报告。
    """
    warnings: List[str] = []
    if not text:
        return {}, warnings

    match = _BLOCK_RE.search(str(text))
    if not match:
        return {}, warnings

    raw_kv: Dict[str, Any] = {}
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        kv = _KV_RE.match(line)
        if not kv:
            continue
        key = kv.group(1).strip().lower()
        canonical = _KEY_ALIASES.get(key)
        if not canonical:
            continue
        raw_kv[canonical] = kv.group(2).strip()

    if not raw_kv:
        warnings.append("TRADE_PLAN 块存在但未解析出任何字段")
        return {}, warnings

    plan: Dict[str, Any] = {}

    direction = _normalize_direction(raw_kv.get("direction"))
    if direction:
        plan["direction"] = direction
    elif "direction" in raw_kv:
        warnings.append(f"direction 无法识别：{raw_kv.get('direction')!r}")

    plan["horizon_days"] = _to_int(raw_kv.get("horizon_days"))

    entry_low = _to_price(raw_kv.get("entry_low"))
    entry_high = _to_price(raw_kv.get("entry_high"))
    if entry_low is None and entry_high is None and raw_kv.get("entry_range"):
        entry_low, entry_high = _parse_range(raw_kv.get("entry_range"))
    elif entry_low is not None and entry_high is None:
        entry_high = entry_low
    elif entry_high is not None and entry_low is None:
        entry_low = entry_high
    if entry_low is not None and entry_high is not None and entry_low > entry_high:
        entry_low, entry_high = entry_high, entry_low
    plan["entry_low"] = entry_low
    plan["entry_high"] = entry_high

    plan["position_cap_pct"] = _to_pct(raw_kv.get("position_cap_pct"))
    plan["first_tranche_pct"] = _to_pct(raw_kv.get("first_tranche_pct"))
    plan["hard_stop_price"] = _to_price(raw_kv.get("hard_stop_price"))
    plan["take_profit_ladder"] = _parse_ladder(raw_kv.get("take_profit_ladder"))
    plan["trailing_stop_pct"] = _to_pct(raw_kv.get("trailing_stop_pct"))
    plan["time_stop_days"] = _to_int(raw_kv.get("time_stop_days"))
    plan["invalidation_conditions"] = _split_conditions(raw_kv.get("invalidation_conditions"))

    if plan["take_profit_ladder"] and not plan["hard_stop_price"]:
        warnings.append("计划含止盈阶梯但缺少硬止损价格")
    if not is_monitorable(plan):
        warnings.append("TRADE_PLAN 块中没有任何可用于监控的价格锚点")

    return {k: v for k, v in plan.items() if v not in (None, [], {})}, warnings


# --------------------------------------------------------------------------- #
# 兜底来源：结构化抽取 / risk_feedback_state
# --------------------------------------------------------------------------- #


def build_from_extracted(
    *,
    target_price: Optional[float],
    stop_loss_price: Optional[float],
) -> Dict[str, Any]:
    """回退来源二：报告已有的结构化抽取字段（锚点较粗，但覆盖历史报告）。

    只写报告真正给出的字段。历史实现把单一目标价包成
    ``take_profit_ladder = [[target, 100.0]]``，让"抽取出的一个数字"在库里
    长得像模型提出的分批止盈方案——下游（退出引擎、前端、复盘）据此展示
    "止盈阶梯"，但根本没有阶梯。这里如实存成 ``target_price``，
    由 :func:`target_levels` 统一成"单一档位"参与监控。
    """
    plan: Dict[str, Any] = {}
    stop = _to_price(stop_loss_price)
    if stop is not None:
        plan["hard_stop_price"] = stop
    target = _to_price(target_price)
    if target is not None:
        plan["target_price"] = target
    return plan


def build_from_risk_feedback(risk_feedback_state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """回退来源三：已存在的风控机读块（硬约束 / 减险触发器 / 执行前置条件）。"""
    if not isinstance(risk_feedback_state, dict) or not risk_feedback_state:
        return {}
    plan: Dict[str, Any] = {}
    hard = _split_conditions(risk_feedback_state.get("hard_constraints"))
    de_risk = _split_conditions(risk_feedback_state.get("de_risk_triggers"))
    pre = _split_conditions(risk_feedback_state.get("execution_preconditions"))
    if hard:
        plan["hard_constraints"] = hard
    if de_risk:
        plan["de_risk_triggers"] = de_risk
    if pre:
        plan["execution_preconditions"] = pre
    return plan


# --------------------------------------------------------------------------- #
# 合并与校验
# --------------------------------------------------------------------------- #

_ANCHOR_KEYS = ("hard_stop_price", "take_profit_ladder", "target_price", "trailing_stop_pct", "entry_low")


def is_monitorable(plan: Optional[Dict[str, Any]]) -> bool:
    """计划是否包含至少一个可被行情监控的价格锚点。"""
    if not isinstance(plan, dict):
        return False
    for key in _ANCHOR_KEYS:
        value = plan.get(key)
        if value not in (None, [], {}, 0, 0.0):
            return True
    return False


def compose_trade_plan(
    *,
    decision_text: str = "",
    trader_plan_text: str = "",
    risk_feedback_state: Optional[Dict[str, Any]] = None,
    target_price: Optional[float] = None,
    stop_loss_price: Optional[float] = None,
    risk_gate: Optional[str] = None,
    confidence: Optional[float] = None,
    horizon: Optional[str] = None,
    horizon_days: Optional[int] = None,
) -> Tuple[Dict[str, Any], str, List[str]]:
    """按可信度合并三个来源，返回 ``(计划, 来源标签, 警告)``。

    优先级：机读块（无损） > 结构化抽取（较粗） > 逻辑推导（无锚点）。
    低优先级来源只补齐高优先级缺失的字段，绝不覆盖。
    """
    warnings: List[str] = []

    plan: Dict[str, Any] = {}
    source = SOURCE_DERIVED

    derived = build_from_risk_feedback(risk_feedback_state)
    if derived:
        plan.update(derived)

    extracted = build_from_extracted(target_price=target_price, stop_loss_price=stop_loss_price)
    if extracted:
        plan.update(extracted)
        source = SOURCE_EXTRACTED

    block, block_warnings = parse_trade_plan_block(decision_text or trader_plan_text)
    warnings.extend(block_warnings)
    if block:
        plan.update(block)
        source = SOURCE_BLOCK

    if not plan:
        warnings.append("三个来源均未产出计划字段")
        return {}, source, warnings

    if horizon_days and not plan.get("horizon_days"):
        plan["horizon_days"] = int(horizon_days)
    if risk_gate:
        plan["risk_gate"] = risk_gate
    if confidence is not None:
        plan["confidence"] = float(confidence)
    if horizon:
        plan["horizon"] = horizon

    warnings.extend(validate_plan(plan))
    return plan, source, warnings


def validate_plan(plan: Dict[str, Any]) -> List[str]:
    """计划自洽性检查——把"看起来有止损其实不可用"的计划显式暴露出来。

    方向语义决定锚点该在上面还是下面：

    * ``BUY``  —— 止损在入场价**下方**，止盈在入场价**上方**；
    * ``SELL`` —— 看空计划的"失效价"在入场价**上方**（涨上去则看空逻辑作废），
      目标位在入场价**下方**；
    * ``HOLD`` —— 不持仓动作，不应携带止损/止盈锚点。

    历史实现只写 ``direction == "BUY"`` 的分支，于是 SELL/HOLD 计划完全绕过校验、
    带着自相矛盾的锚点入库；方向缺失时更是连检查都不做（fail-open）。

    :func:`blocking_plan_problems` 从中筛出**可证明矛盾**的那部分，用于落库拦截。
    """
    problems: List[str] = []
    direction = _normalize_direction(plan.get("direction"))
    stop = _to_price(plan.get("hard_stop_price"))
    entry_low = _to_price(plan.get("entry_low"))
    entry_high = _to_price(plan.get("entry_high"))
    cap = plan.get("position_cap_pct")
    first = plan.get("first_tranche_pct")
    levels = target_levels(plan)

    if plan.get("direction") not in (None, "") and direction is None:
        problems.append(f"方向无法识别：{plan.get('direction')!r}（只接受 BUY/SELL/HOLD）")
    elif direction is None:
        problems.append("计划未标明方向（BUY/SELL/HOLD），无法校验价格锚点方向")

    if entry_low is not None and entry_high is not None and entry_low > entry_high:
        problems.append(f"入场区间下沿 {entry_low} 高于上沿 {entry_high}")

    # 多头语义下的锚点方向。entry 取上沿（最保守），缺失时退回下沿。
    entry = entry_high if entry_high is not None else entry_low
    if direction == "BUY":
        if stop is not None and entry is not None and stop >= entry:
            problems.append(f"止损价 {stop} 不低于入场价 {entry}，止损无效")
        for price, _pct in levels:
            if entry is not None and price <= entry:
                problems.append(f"止盈价 {price} 不高于入场价 {entry}，不是多头止盈位")
        if stop is None:
            # 只告警不拦截：计划不完整，但不会让引擎"说反话"。
            problems.append("买入计划缺少硬止损")

    # 空头语义：失效价在上、目标位在下。entry 取下沿（最保守）。
    if direction == "SELL":
        short_entry = entry_low if entry_low is not None else entry_high
        if stop is not None and short_entry is not None and stop <= short_entry:
            problems.append(f"看空计划的失效价 {stop} 不高于入场价 {short_entry}，失效条件无效")
        for price, _pct in levels:
            if short_entry is not None and price >= short_entry:
                problems.append(f"看空计划的目标位 {price} 不低于入场价 {short_entry}，不是看空目标")

    if direction == "HOLD" and (stop is not None or levels):
        problems.append("持有计划不应携带止损/止盈锚点")

    if cap is not None and first is not None and first > cap:
        problems.append(f"首仓 {first}% 超过总仓上限 {cap}%")
    if cap is not None and cap > 100:
        problems.append(f"总仓上限 {cap}% 超出 100%")
    if cap is not None and cap <= 0:
        problems.append(f"总仓上限 {cap}% 不是正数")

    return problems


# 可证明矛盾、必须拦截落库的问题前缀。方向缺失/缺止损这类只告警不拦截：
# 它们让计划"不完整"，但不让计划"说反话"——后者才会驱动错误卖出。
_BLOCKING_PROBLEM_MARKERS = (
    "止损价", "止盈价", "失效价", "目标位", "入场区间下沿", "方向无法识别",
    "持有计划不应携带",
)


def blocking_plan_problems(plan: Dict[str, Any]) -> List[str]:
    """校验问题中**可证明矛盾**的子集——落库前必须拦截的那部分。

    区分标准：完整性缺陷（没写止损、没写方向）只告警，因为计划仍然可用；
    方向性矛盾（止损在入场之上、止盈在入场之下）会让退出引擎按多头语义
    读出完全相反的动作，属于必须拦截的错误。

    `validate_plan` 是唯一的事实来源，这里只负责筛选，避免两处各写一套判断。
    """
    return [
        p for p in validate_plan(plan) if any(m in p for m in _BLOCKING_PROBLEM_MARKERS)
    ]


# --------------------------------------------------------------------------- #
# 落库与查询
# --------------------------------------------------------------------------- #


def upsert_trade_plan(
    db: Session,
    *,
    user_id: Optional[str],
    report_id: str,
    symbol: str,
    name: Optional[str] = None,
    signal_trade_date: Optional[str] = None,
    plan: Optional[Dict[str, Any]] = None,
    source: str = SOURCE_DERIVED,
    raw_text: Optional[str] = None,
    parse_warnings: Optional[List[str]] = None,
    status: str = STATUS_ACTIVE,
) -> Optional[TradePlanDB]:
    """幂等落库：同一 report_id 重复写入时更新既有行。"""
    if not report_id or not symbol:
        return None
    plan = plan or {}
    sym = _norm_symbol(symbol)

    # 落库前拦截可证明矛盾的锚点。历史实现把校验结果只当告警，于是方向与价格
    # 互相打架的计划照样入库，再由退出引擎按多头语义读成相反动作并推送给用户。
    # 完整性缺陷（缺方向、缺止损）仍然放行——计划不完整不等于计划说反话。
    blocking = blocking_plan_problems(plan)
    if blocking:
        logger.warning(
            "[trade-plan] 拒绝落库：锚点方向自相矛盾 report_id=%s symbol=%s: %s",
            report_id, sym, "；".join(blocking),
        )
        return None

    row = db.query(TradePlanDB).filter(TradePlanDB.report_id == report_id).one_or_none()
    created = row is None
    if row is None:
        row = TradePlanDB(id=uuid4().hex, report_id=report_id, user_id=user_id, symbol=sym)
        db.add(row)

    row.user_id = user_id
    row.symbol = sym
    row.name = name or row.name
    row.signal_trade_date = signal_trade_date or row.signal_trade_date
    row.horizon = plan.get("horizon") or row.horizon
    row.horizon_days = plan.get("horizon_days") or row.horizon_days
    row.direction = plan.get("direction") or row.direction
    row.entry_low = plan.get("entry_low") if plan.get("entry_low") is not None else row.entry_low
    row.entry_high = plan.get("entry_high") if plan.get("entry_high") is not None else row.entry_high
    row.position_cap_pct = (
        plan.get("position_cap_pct") if plan.get("position_cap_pct") is not None else row.position_cap_pct
    )
    row.first_tranche_pct = (
        plan.get("first_tranche_pct") if plan.get("first_tranche_pct") is not None else row.first_tranche_pct
    )
    row.hard_stop_price = (
        plan.get("hard_stop_price") if plan.get("hard_stop_price") is not None else row.hard_stop_price
    )
    if plan.get("take_profit_ladder"):
        row.take_profit_ladder_json = plan["take_profit_ladder"]
    row.target_price = (
        plan.get("target_price") if plan.get("target_price") is not None else row.target_price
    )
    row.trailing_stop_pct = (
        plan.get("trailing_stop_pct") if plan.get("trailing_stop_pct") is not None else row.trailing_stop_pct
    )
    row.time_stop_days = plan.get("time_stop_days") or row.time_stop_days
    if plan.get("invalidation_conditions"):
        row.invalidation_conditions_json = plan["invalidation_conditions"]
    if plan.get("de_risk_triggers"):
        row.de_risk_triggers_json = plan["de_risk_triggers"]
    if plan.get("execution_preconditions"):
        row.execution_preconditions_json = plan["execution_preconditions"]
    if plan.get("hard_constraints"):
        row.hard_constraints_json = plan["hard_constraints"]
    row.risk_gate = plan.get("risk_gate") or row.risk_gate
    row.confidence = plan.get("confidence") if plan.get("confidence") is not None else row.confidence
    row.status = status
    row.source = source
    if raw_text:
        row.raw_text = raw_text
    if parse_warnings:
        row.parse_warnings_json = parse_warnings

    # 同一 user+symbol 只保留一个"当前有效计划"；旧计划保留供审计。
    # 仅当新计划确实可监控时才顶替，避免"新报告没给止损"把有效止损弄丢。
    if created and is_monitorable(plan) and user_id:
        db.query(TradePlanDB).filter(
            TradePlanDB.user_id == user_id,
            TradePlanDB.symbol == sym,
            TradePlanDB.status == STATUS_ACTIVE,
            TradePlanDB.id != row.id,
        ).update(
            {"status": STATUS_SUPERSEDED, "status_reason": f"被报告 {report_id} 的新计划取代"},
            synchronize_session=False,
        )

    return row


def persist_plan_from_analysis(
    db: Session,
    *,
    user_id: Optional[str],
    report_id: str,
    symbol: str,
    name: Optional[str] = None,
    signal_trade_date: Optional[str] = None,
    decision_text: str = "",
    trader_plan_text: str = "",
    risk_feedback_state: Optional[Dict[str, Any]] = None,
    target_price: Optional[float] = None,
    stop_loss_price: Optional[float] = None,
    risk_gate: Optional[str] = None,
    confidence: Optional[float] = None,
    horizon: Optional[str] = None,
    horizon_days: Optional[int] = None,
) -> Tuple[Optional[TradePlanDB], Dict[str, Any], str, List[str]]:
    """分析完成后的一站式入口：合并 → 落库。

    返回 ``(落库行, 计划字典, 来源, 警告)``。任何异常都被兜住——
    计划落库失败绝不能让一份已经生成好的研报丢失。
    """
    plan, source, warnings = compose_trade_plan(
        decision_text=decision_text,
        trader_plan_text=trader_plan_text,
        risk_feedback_state=risk_feedback_state,
        target_price=target_price,
        stop_loss_price=stop_loss_price,
        risk_gate=risk_gate,
        confidence=confidence,
        horizon=horizon,
        horizon_days=horizon_days,
    )
    if not plan:
        return None, plan, source, warnings

    raw_text = ""
    match = _BLOCK_RE.search(str(decision_text or trader_plan_text or ""))
    if match:
        raw_text = match.group(0)[:4000]

    try:
        row = upsert_trade_plan(
            db,
            user_id=user_id,
            report_id=report_id,
            symbol=symbol,
            name=name,
            signal_trade_date=signal_trade_date,
            plan=plan,
            source=source,
            raw_text=raw_text or None,
            parse_warnings=warnings or None,
        )
    except Exception as exc:  # 计划落库是增强，不是主链路，绝不因此丢报告
        logger.warning("[TradePlan] 落库失败 report=%s symbol=%s: %s", report_id, symbol, exc)
        try:
            db.rollback()
        except Exception:
            pass
        return None, plan, source, warnings + [f"落库失败：{exc}"]

    if warnings:
        logger.info("[TradePlan] %s %s 警告：%s", _norm_symbol(symbol), source, "；".join(warnings))
    return row, plan, source, warnings


def get_monitor_plan(
    db: Session,
    *,
    user_id: Optional[str],
    symbol: str,
) -> Optional[TradePlanDB]:
    """取该标的最新的、可用于监控的计划（退出引擎的输入）。"""
    sym = _norm_symbol(symbol)
    if not sym:
        return None
    query = db.query(TradePlanDB).filter(TradePlanDB.symbol == sym)
    if user_id:
        query = query.filter(or_(TradePlanDB.user_id == user_id, TradePlanDB.user_id.is_(None)))
    rows = query.order_by(TradePlanDB.created_at.desc()).limit(20).all()
    for row in rows:
        if row.status in (STATUS_EXPIRED, STATUS_INVALIDATED):
            continue
        if _row_has_anchors(row):
            return row
    return None


def list_active_plans(db: Session, *, user_id: str) -> List[TradePlanDB]:
    """该用户所有"当前有效且可监控"的计划。"""
    if not user_id:
        return []
    rows = (
        db.query(TradePlanDB)
        .filter(TradePlanDB.user_id == user_id, TradePlanDB.status == STATUS_ACTIVE)
        .order_by(TradePlanDB.created_at.desc())
        .all()
    )
    return [row for row in rows if _row_has_anchors(row)]


def set_plan_status(
    db: Session,
    *,
    plan_id: str,
    status: str,
    reason: Optional[str] = None,
) -> Optional[TradePlanDB]:
    row = db.query(TradePlanDB).filter(TradePlanDB.id == plan_id).one_or_none()
    if row is None:
        return None
    row.status = status
    row.status_reason = reason
    return row


def _row_has_anchors(row: TradePlanDB) -> bool:
    return any(
        [
            row.hard_stop_price,
            row.take_profit_ladder_json,
            row.trailing_stop_pct,
            row.entry_low,
        ]
    )


def plan_dict_from_row(row: Optional[TradePlanDB]) -> Optional[Dict[str, Any]]:
    """把落库行还原成计划字典（退出引擎与前端共用）。"""
    if row is None:
        return None
    return {
        "id": row.id,
        "report_id": row.report_id,
        "symbol": row.symbol,
        "name": row.name,
        "signal_trade_date": row.signal_trade_date,
        "horizon": row.horizon,
        "horizon_days": row.horizon_days,
        "direction": row.direction,
        "entry_low": row.entry_low,
        "entry_high": row.entry_high,
        "position_cap_pct": row.position_cap_pct,
        "first_tranche_pct": row.first_tranche_pct,
        "hard_stop_price": row.hard_stop_price,
        "take_profit_ladder": row.take_profit_ladder_json or [],
        "target_price": row.target_price,
        "trailing_stop_pct": row.trailing_stop_pct,
        "time_stop_days": row.time_stop_days,
        "invalidation_conditions": row.invalidation_conditions_json or [],
        "de_risk_triggers": row.de_risk_triggers_json or [],
        "execution_preconditions": row.execution_preconditions_json or [],
        "hard_constraints": row.hard_constraints_json or [],
        "risk_gate": row.risk_gate,
        "confidence": row.confidence,
        "status": row.status,
        "status_reason": row.status_reason,
        "source": row.source,
        "parse_warnings": row.parse_warnings_json or [],
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def plan_days_elapsed(plan: Dict[str, Any], today: str) -> Optional[int]:
    """计划自信号日起经过的自然日数（时间止损判断的一部分）。"""
    signal_date = str(plan.get("signal_trade_date") or "")
    if len(signal_date) != 10:
        return None
    try:
        start = datetime.strptime(signal_date, "%Y-%m-%d")
        end = datetime.strptime(str(today)[:10], "%Y-%m-%d")
    except ValueError:
        return None
    return max(0, (end - start).days)


def ladder_targets(plan: Optional[Dict[str, Any]]) -> List[Tuple[float, float]]:
    """规范化止盈阶梯为 ``[(price, reduce_pct)]``（按价格升序）。"""
    if not plan:
        return []
    out: List[Tuple[float, float]] = []
    for item in plan.get("take_profit_ladder") or []:
        if isinstance(item, (list, tuple)) and item:
            price = _to_price(item[0])
            pct = _to_pct(item[1]) if len(item) > 1 else None
            if price is not None:
                out.append((price, pct if pct is not None else 100.0))
    return sorted(out, key=lambda x: x[0])


def target_levels(plan: Optional[Dict[str, Any]]) -> List[Tuple[float, float]]:
    """计划的止盈档位，统一"真阶梯"与"单一目标价"两种表示。

    有 ``take_profit_ladder`` 时用它；只有 ``target_price``（结构化抽取的回退来源）
    时视为**唯一一档、一次性止盈 100%**。两者都不存在则返回空列表。

    调用方（退出引擎的分批止盈判定、监控可用性判断）只认这一个入口，
    不必再关心计划是"模型给了阶梯"还是"只抽到一个目标数字"。
    """
    ladder = ladder_targets(plan)
    if ladder:
        return ladder
    if not plan:
        return []
    single = _to_price(plan.get("target_price"))
    if single is None:
        return []
    return [(single, 100.0)]


def plan_target_price(plan: Optional[Dict[str, Any]]) -> Optional[float]:
    """计划的目标价：止盈阶梯的**最高档**，且在多头计划下校验其高于入场价。

    旧实现把 ``ladder[0][0]`` 当作目标价，但 :func:`ladder_targets` 是按价格
    **升序**排列的——那是最低的第一档减仓位，不是目标价。结果是"分批止盈"的首个
    减仓点被写成研报的目标价，产出大量"看多 + 目标价低于现价"的自相矛盾记录，
    并流入下游（退出引擎、复盘、前端展示）。

    目标价语义上是"这份研报想看到的价格"，因此取最高档。同时做方向校验：
    多头计划的止盈位必须高于入场价，否则该阶梯不是多头止盈阶梯，
    宁可返回 ``None``（诚实缺失）也不回填一个自相矛盾的数字。
    """
    levels = target_levels(plan)
    if not levels:
        return None

    target = float(levels[-1][0])

    direction = _normalize_direction((plan or {}).get("direction"))
    if direction not in (None, "BUY"):
        return None

    entry = _to_price((plan or {}).get("entry_high"))
    if entry is None:
        entry = _to_price((plan or {}).get("entry_low"))
    if entry is not None and target <= entry:
        return None

    return target

