"""成交流水（交易台账）服务：把"静态持仓快照"升级为可归因的真实事实来源。

背景
----
产品此前的持仓数据来自 `imported_portfolio_positions`——一张每次导入都会被
"整表删后重插"的**静态快照**。快照只能回答"现在有多少股"，回答不了：

* 哪一笔买入真正赚了钱（需要逐笔成交与摊薄成本）；
* 哪一次卖出是止损、哪一次是止盈（需要卖出原因分类学）；
* 加仓之后的摊薄成本是多少（快照只有一个 `average_cost`，会被覆盖）；
* 今天还有多少股是 T+1 可卖的（快照的 `available_position` 是导入瞬间的值）。

`trade_ledger` 表提供了逐笔事实，本模块负责：

1. **记账**：`record_trade` 把一笔买卖写成流水行，并回填该笔之后的持仓/可卖/
   摊薄成本/已实现盈亏（幂等：同 user+symbol+date+action+price+shares 视为重复导入）；
2. **重放**：`rebuild_position_state` 按 `(trade_date, created_at)` 顺序重放流水，
   算出摊薄成本、已实现盈亏与 T+1 可卖数量（不用脆弱的一次性公式）；
3. **归因**：`attribution_by_sell_reason` / `attribution_summary` 按卖出原因、
   按标的聚合已实现盈亏、胜率与平均盈亏，并暴露"未归因笔数"以便度量归因覆盖率；
4. **迁移**：`import_trade_points` / `sync_from_imported_positions` 把导入快照里的
   `trade_points_json` 搬进台账，并与快照做对账（`mismatches`）——产品由此可以
   逐步不再盲信静态快照。

费用口径（A 股现实，产品此前完全漏掉印花税）
--------------------------------------------
* 佣金 = max(成交额 × 万三, 5 元最低)；
* 印花税 = 成交额 × 0.05%，**仅卖出**；
* 过户费 = 成交额 × 0.001%，买卖双边；
* 买入：`net_amount = amount + 费用`（真实现金流出）；卖出：`net_amount = amount - 费用`。

买入费用计入成本，因此 `average_cost` 是**含费的诚实摊薄成本**；卖出费用冲减
已实现盈亏。卖出不改变 `average_cost`（A 股通行的"摊薄成本法"）。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from api.database import ImportedPortfolioPositionDB, TradeLedgerDB

# 卖出原因分类学与中文标签：与交易计划共用同一套取值，禁止在本模块重新定义
from api.services.trade_plan_service import (
    SELL_REASON_INVALIDATION,
    SELL_REASON_MANUAL,
    SELL_REASON_REBALANCE,
    SELL_REASON_STOP_LOSS,
    SELL_REASON_TAKE_PROFIT,
    SELL_REASON_TIME_STOP,
    SELL_REASON_TRAILING_STOP,
    SELL_REASON_UNKNOWN,
)
from api.services.trade_plan_service import SELL_REASON_LABELS as SELL_REASON_LABELS_ZH
from tradingagents.dataflows.trade_calendar import cn_today_str, is_cn_trading_day

logger = logging.getLogger(__name__)

# 兼容别名：部分调用方直接引用 SELL_REASON_LABELS
SELL_REASON_LABELS = SELL_REASON_LABELS_ZH

ACTION_BUY = "BUY"
ACTION_SELL = "SELL"
VALID_ACTIONS = (ACTION_BUY, ACTION_SELL)

# 现行 A 股费率默认值（可按券商调整，均以关键字参数覆盖）
DEFAULT_COMMISSION_RATE = 0.0003      # 佣金万三
DEFAULT_MIN_COMMISSION = 5.0          # 单笔最低 5 元
DEFAULT_STAMP_TAX_RATE = 0.0005       # 印花税千分之 0.5，仅卖出
DEFAULT_TRANSFER_FEE_RATE = 0.00001   # 过户费十万分之一，双边

# 合法卖出原因取值（唯一来源：trade_plan_service.SELL_REASON_LABELS）
VALID_SELL_REASONS = frozenset(SELL_REASON_LABELS_ZH.keys())
_ZH_LABEL_TO_REASON = {label: key for key, label in SELL_REASON_LABELS_ZH.items()}

# 数值容差
_SHARE_EPS = 1e-6        # 股数比较容差
_RECONCILE_EPS = 1e-4    # 快照对账容差

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")
_REASON_CAP = 100  # 导入时最多保留多少条跳过原因，避免返回体膨胀


# --------------------------------------------------------------------------- #
# 成交费用
# --------------------------------------------------------------------------- #

@dataclass
class TradeCostBreakdown:
    """单笔成交的费用拆解（金额均保留 2 位小数）。"""

    amount: float          # 成交额 = price * shares
    commission: float      # 佣金（含最低 5 元约束）
    stamp_tax: float       # 印花税，仅卖出
    transfer_fee: float    # 过户费，双边
    total_fee: float       # commission + stamp_tax + transfer_fee
    net_amount: float      # 买入：amount + total_fee；卖出：amount - total_fee

    def to_dict(self) -> dict[str, float]:
        return {
            "amount": self.amount,
            "commission": self.commission,
            "stamp_tax": self.stamp_tax,
            "transfer_fee": self.transfer_fee,
            "total_fee": self.total_fee,
            "net_amount": self.net_amount,
        }


def compute_trade_cost(
    action: str,
    price: float,
    shares: float,
    *,
    commission_rate: float = DEFAULT_COMMISSION_RATE,
    min_commission: float = DEFAULT_MIN_COMMISSION,
    stamp_tax_rate: float = DEFAULT_STAMP_TAX_RATE,
    transfer_fee_rate: float = DEFAULT_TRANSFER_FEE_RATE,
) -> TradeCostBreakdown:
    """计算一笔买卖的实际费用（A 股：印花税仅卖出，过户费双边）。

    产品此前的成交额计算**完全没有印花税**，卖出盈利被系统性高估。这里把
    佣金/印花税/过户费拆开返回，交由调用方落库与展示。
    """

    action_norm = normalize_action(action)
    if action_norm is None:
        raise ValueError(f"action 必须为 {ACTION_BUY} 或 {ACTION_SELL}，收到：{action!r}")
    price_val = _to_float(price)
    shares_val = _to_float(shares)
    if price_val is None or shares_val is None:
        raise ValueError("price 与 shares 必须为数字")
    if price_val <= 0 or shares_val <= 0:
        raise ValueError("price 与 shares 必须为正数")

    amount = round(price_val * shares_val, 2)
    commission = round(max(amount * float(commission_rate), float(min_commission)), 2)
    stamp_tax = round(amount * float(stamp_tax_rate), 2) if action_norm == ACTION_SELL else 0.0
    transfer_fee = round(amount * float(transfer_fee_rate), 2)
    total_fee = round(commission + stamp_tax + transfer_fee, 2)
    if action_norm == ACTION_BUY:
        net_amount = round(amount + total_fee, 2)
    else:
        net_amount = round(amount - total_fee, 2)

    return TradeCostBreakdown(
        amount=amount,
        commission=commission,
        stamp_tax=stamp_tax,
        transfer_fee=transfer_fee,
        total_fee=total_fee,
        net_amount=net_amount,
    )


# --------------------------------------------------------------------------- #
# 基础解析工具（同时服务导入解析与读路径容错）
# --------------------------------------------------------------------------- #

def _to_float(value: Any) -> float | None:
    """宽松转 float：支持 "1,000"、"1.5万股"、"10.5元"、"--"、None 等。"""

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        f = float(value)
        return None if f != f else f  # 过滤 NaN
    if isinstance(value, str):
        text = (
            value.strip()
            .replace(",", "")
            .replace("，", "")
            .replace(" ", "")
            .replace("\u3000", "")
        )
        if not text or text in {"-", "--", "—", "N/A", "n/a", "None", "null", "无"}:
            return None
        multiplier = 1.0
        if "亿" in text:
            multiplier = 100000000.0
        elif "万" in text:
            multiplier = 10000.0
        match = _NUM_RE.search(text)
        if not match:
            return None
        try:
            return float(match.group()) * multiplier
        except ValueError:
            return None
    return None


def normalize_action(value: Any) -> str | None:
    """把各种买卖写法归一为 BUY / SELL；无法识别返回 None。

    支持英文（BUY/SELL/B/S）与中文（买入/买/证券买入/卖出/卖/证券卖出/减持…）。
    """

    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text:
        return None
    low = text.lower()
    if low in {"buy", "b", "buy_in", "buyin", "long"}:
        return ACTION_BUY
    if low in {"sell", "s", "sell_out", "sellout", "short"}:
        return ACTION_SELL
    # 中文优先：先判卖出，避免"证券买入/卖出"混排时误判
    if "卖" in text or "減持" in text or "减持" in text or "赎回" in text:
        return ACTION_SELL
    if "买" in text or "買" in text or "增持" in text:
        return ACTION_BUY
    if "sell" in low:
        return ACTION_SELL
    if "buy" in low:
        return ACTION_BUY
    return None


def normalize_trade_date(value: Any) -> str | None:
    """宽松归一成交日期为 "YYYY-MM-DD"；无法识别返回 None。"""

    if isinstance(value, datetime):
        return value.date().strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = (
        text.replace("年", "-")
        .replace("月", "-")
        .replace("日", "")
        .replace("/", "-")
        .replace(".", "-")
    )
    # "2024-05-06 09:35:00" / "2024-05-06T09:35:00"
    text = text.replace("T", " ").split(" ")[0].strip()
    if _DATE_RE.match(text) is None:
        digits = re.sub(r"\D", "", text)
        if len(digits) == 8:
            text = f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    if _DATE_RE.match(text) is None:
        return None
    try:
        date.fromisoformat(text)
    except ValueError:
        return None
    return text


def _is_valid_date_strict(value: Any) -> str | None:
    """严格校验 "YYYY-MM-DD"（record_trade 用；导入解析走宽松版本）。"""

    if not isinstance(value, str):
        return None
    text = value.strip()
    if _DATE_RE.match(text) is None:
        return None
    try:
        date.fromisoformat(text)
    except ValueError:
        return None
    return text


def normalize_symbol(value: Any) -> str:
    """标的代码归一：去空白 + 大写（与台账写入口径一致）。"""

    return str(value or "").strip().upper().replace(" ", "")


def _symbol_core(symbol: str) -> str:
    """取代码主键（6 位数字），用于快照/台账之间的别名匹配。"""

    match = re.search(r"\d{6}", symbol or "")
    if match:
        return match.group(0)
    return (symbol or "").upper()


def _norm_map(raw: Mapping[Any, Any]) -> dict[str, Any]:
    """键名小写化后的字典，便于大小写不敏感地取值。"""

    return {str(key).strip().lower(): value for key, value in raw.items()}


def _pick(nmap: Mapping[str, Any], keys: Sequence[str]) -> Any:
    """按候选键顺序取第一个"有值"的字段。"""

    for key in keys:
        if key.lower() not in nmap:
            continue
        value = nmap[key.lower()]
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _sanitize_sell_reason(value: Any) -> tuple[str | None, str | None]:
    """校验卖出原因取值：非法值回退为 SELL_REASON_UNKNOWN 并给出提示。"""

    if value is None:
        return None, None
    text = str(value).strip()
    if not text:
        return None, None
    key = text.lower()
    if key in VALID_SELL_REASONS:
        return key, None
    if text in _ZH_LABEL_TO_REASON:
        return _ZH_LABEL_TO_REASON[text], None
    return (
        SELL_REASON_UNKNOWN,
        f"卖出原因 {text!r} 不在标准分类中，已归为 {SELL_REASON_UNKNOWN}",
    )


# 卖出原因文本推断规则（顺序即优先级：先专指、后泛指）
_REASON_TEXT_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (SELL_REASON_TRAILING_STOP, ("移动止盈", "移动止损", "回撤止盈", "跟踪止盈", "保护止盈")),
    (SELL_REASON_TIME_STOP, ("时间止损", "持有到期", "到期", "超时", "时间成本")),
    (SELL_REASON_STOP_LOSS, ("止损", "割肉", "跌破", "破位")),
    (SELL_REASON_TAKE_PROFIT, ("止盈", "获利了结", "落袋", "目标价达成", "到达目标")),
    (SELL_REASON_INVALIDATION, ("逻辑失效", "证伪", "基本面恶化", "假设不成立", "失效")),
    (SELL_REASON_REBALANCE, ("调仓", "换仓", "再平衡", "仓位调整", "置换")),
    (SELL_REASON_MANUAL, ("清仓", "平仓", "了结", "手动", "人工", "主动")),
)


def infer_sell_reason_from_text(value: Any) -> str | None:
    """从自由文本推断卖出原因（导入历史成交时的兜底）。"""

    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    for reason, keywords in _REASON_TEXT_RULES:
        for keyword in keywords:
            if keyword in text:
                return reason
    return None


# --------------------------------------------------------------------------- #
# 台账重放
# --------------------------------------------------------------------------- #

def _row_costs(row: TradeLedgerDB) -> tuple[float, float, float]:
    """返回某行的 (佣金类费用, 印花税, 合计费用)。

    优先使用落库值；缺失时按现行费率重算，避免手工/他方写入的行让盈亏失真。
    约定：`fee` = 佣金 + 过户费，`stamp_tax` 单独存放（与 record_trade 写入口径一致）。
    """

    price = _to_float(getattr(row, "price", None)) or 0.0
    shares = _to_float(getattr(row, "shares", None)) or 0.0
    computed: TradeCostBreakdown | None = None
    if price > 0 and shares > 0:
        try:
            computed = compute_trade_cost(str(getattr(row, "action", "") or ""), price, shares)
        except ValueError:
            computed = None

    fee = _to_float(getattr(row, "fee", None))
    if fee is None:
        fee = (computed.commission + computed.transfer_fee) if computed else 0.0
    stamp_tax = _to_float(getattr(row, "stamp_tax", None))
    if stamp_tax is None:
        stamp_tax = computed.stamp_tax if computed else 0.0
    fee = round(fee, 2)
    stamp_tax = round(stamp_tax, 2)
    return fee, stamp_tax, round(fee + stamp_tax, 2)


def _row_sort_key(row: TradeLedgerDB) -> tuple[str, str, str]:
    """重放顺序：交易日 → 落库时间 → id（created_at 可能为 None，故转字符串）。"""

    trade_date = str(getattr(row, "trade_date", "") or "")
    created = getattr(row, "created_at", None)
    created_key = created.isoformat() if hasattr(created, "isoformat") else str(created or "")
    return (trade_date, created_key, str(getattr(row, "id", "") or ""))


def _add_warning(warnings: list[str], message: str) -> None:
    if message not in warnings:
        warnings.append(message)


def _replay(rows: Sequence[TradeLedgerDB], *, today: str) -> dict[str, Any]:
    """按时间顺序重放流水，返回逐笔快照与汇总状态。

    **T+1 可卖**不是一次性公式，而是随重放逐步累计（避免"用总买减总卖"这种
    脆弱写法在越卖/同日买卖时给出荒谬结果）：

    * 买入：若 ``trade_date < today``，该笔在"今天"之前已结算，立即计入可卖；
      当日或更晚的买入不计入（要等下一个交易日）；
    * 卖出：从既有可卖中扣减，并且只扣"实际持仓范围内"的股数，结果不为负；
    * 收尾：可卖再被总持仓兜底（``available <= position``）。

    由于流水按 ``(trade_date, created_at)`` 排序，"早于今天的买入"总是排在
    今天的卖出之前，因此该结果与"sum(买入且日期<今天) - sum(卖出且日期<=今天)"
    等价，但不会因为某一笔越卖而把后续买入的可卖额度算错。
    """

    ordered = sorted(rows, key=_row_sort_key)
    warnings: list[str] = []

    position = 0.0
    available = 0.0
    average_cost = 0.0
    realized_total = 0.0
    today_buy_shares = 0.0

    total_bought = 0.0
    total_sold = 0.0
    total_bought_amount = 0.0
    total_sold_amount = 0.0
    total_buy_fee = 0.0
    total_sell_fee = 0.0
    snapshots: list[dict[str, Any]] = []
    row_ids: list[str] = []

    for row in ordered:
        trade_date = normalize_trade_date(getattr(row, "trade_date", None)) or str(
            getattr(row, "trade_date", "") or ""
        )
        action = normalize_action(getattr(row, "action", None))
        price = _to_float(getattr(row, "price", None)) or 0.0
        shares = _to_float(getattr(row, "shares", None)) or 0.0
        fee, stamp_tax, total_fee = _row_costs(row)

        if action is None or shares <= 0:
            _add_warning(
                warnings,
                f"{trade_date} 的流水 action/shares 非法（action={getattr(row, 'action', None)!r}），已跳过重放",
            )
            snapshots.append(
                {
                    "position": round(position, 4),
                    "available": round(available, 4),
                    "average_cost": round(average_cost, 4),
                    "realized_pnl": None,
                    "realized_pnl_total": round(realized_total, 2),
                    "skipped": True,
                }
            )
            row_ids.append(str(getattr(row, "id", "") or ""))
            continue

        realized_delta: float | None = None

        if action == ACTION_BUY:
            position_before = position
            position = position_before + shares
            cost_amount = price * shares + total_fee
            average_cost = (average_cost * position_before + cost_amount) / position if position > 0 else 0.0
            # T+1：只有 trade_date < today 的买入，才在"今天"之前已经结算为可卖；
            # 当日（或更晚）买入的股数不进入可卖，直到下一个交易日。
            if trade_date < today:
                available += shares
            if trade_date == today:
                today_buy_shares += shares
            total_bought += shares
            total_bought_amount += price * shares
            total_buy_fee += total_fee
        else:
            position_before = position
            effective = min(shares, position_before)
            if shares > position_before + _SHARE_EPS:
                _add_warning(
                    warnings,
                    f"{trade_date} 卖出 {_fmt_shares(shares)} 股超过当时持仓 "
                    f"{_fmt_shares(position_before)} 股，已按可卖持仓计算（历史导入可能不完整）",
                )
            if effective <= _SHARE_EPS:
                _add_warning(
                    warnings,
                    f"{trade_date} 卖出 {_fmt_shares(shares)} 股时无持仓，该笔已实现盈亏记为 0（请检查历史导入完整性）",
                )
                realized_delta = 0.0
            else:
                realized_delta = round((price - average_cost) * effective - total_fee, 2)
                realized_total = round(realized_total + realized_delta, 2)
            if effective > available + _SHARE_EPS:
                _add_warning(
                    warnings,
                    f"{trade_date} 卖出 {_fmt_shares(effective)} 股超过 T+1 可卖 "
                    f"{_fmt_shares(available)} 股，可卖数量已按 0 兜底",
                )
            available = max(available - effective, 0.0)
            position = max(position - effective, 0.0)
            total_sold += effective
            total_sold_amount += price * effective
            total_sell_fee += total_fee
            # 卖出不改变摊薄成本（A 股通行口径）

        snapshots.append(
            {
                "position": round(position, 4),
                "available": round(available, 4),
                "average_cost": round(average_cost, 4),
                "realized_pnl": realized_delta,
                "realized_pnl_total": round(realized_total, 2),
                "skipped": False,
            }
        )
        row_ids.append(str(getattr(row, "id", "") or ""))

    available = max(available, 0.0)
    position = max(position, 0.0)
    if available > position:
        available = position

    if today_buy_shares > 0 and not _is_trading_day(today):
        _add_warning(
            warnings,
            f"{today} 非交易日：当日买入的 {_fmt_shares(today_buy_shares)} 股需待下一交易日方可卖出",
        )

    if not ordered:
        _add_warning(warnings, "台账中没有该标的的成交记录，无法还原持仓")

    return {
        "rows": list(ordered),
        "row_ids": row_ids,
        "snapshots": snapshots,
        "position": round(position, 4),
        "available": round(available, 4),
        "average_cost": round(average_cost, 4),
        "realized_pnl": round(realized_total, 2),
        "total_bought": round(total_bought, 4),
        "total_sold": round(total_sold, 4),
        "total_bought_amount": round(total_bought_amount, 2),
        "total_sold_amount": round(total_sold_amount, 2),
        "total_buy_fee": round(total_buy_fee, 2),
        "total_sell_fee": round(total_sell_fee, 2),
        "trade_count": len(ordered),
        "warnings": warnings,
    }


def _fmt_shares(value: float) -> str:
    """股数展示：整数不带小数点。"""

    if abs(value - round(value)) < 1e-6:
        return str(int(round(value)))
    return f"{value:.2f}"


def _is_trading_day(date_str: str) -> bool:
    """是否为交易日；日历不可用时按"未知即视为交易日"处理，绝不因它报错。"""

    try:
        return bool(is_cn_trading_day(date_str))
    except Exception:  # pragma: no cover - 日历/网络异常不应影响台账
        logger.debug("交易日历不可用，跳过非交易日提示", exc_info=True)
        return True


def _load_rows(db: Session, user_id: str, symbol: str) -> list[TradeLedgerDB]:
    return (
        db.query(TradeLedgerDB)
        .filter(
            TradeLedgerDB.user_id == user_id,
            TradeLedgerDB.symbol == symbol,
        )
        .order_by(TradeLedgerDB.trade_date, TradeLedgerDB.created_at)
        .all()
    )


def rebuild_position_state(db: Session, *, user_id: str, symbol: str) -> dict[str, Any]:
    """重放某标的的全部流水，还原持仓/可卖/摊薄成本/已实现盈亏。

    返回 `{symbol, position, available, average_cost, realized_pnl, total_bought,
    total_sold, trade_count, ...}`。历史导入不完整（卖出多于买入）时不会出现
    负数，问题会写进 `warnings` 供产品提示用户。
    """

    symbol_norm = normalize_symbol(symbol)
    rows = _load_rows(db, user_id, symbol_norm)
    today = cn_today_str()
    state = _replay(rows, today=today)
    return {
        "symbol": symbol_norm,
        "position": state["position"],
        "available": state["available"],
        "average_cost": state["average_cost"],
        "realized_pnl": state["realized_pnl"],
        "total_bought": state["total_bought"],
        "total_sold": state["total_sold"],
        "total_bought_amount": state["total_bought_amount"],
        "total_sold_amount": state["total_sold_amount"],
        "total_buy_fee": state["total_buy_fee"],
        "total_sell_fee": state["total_sell_fee"],
        "trade_count": state["trade_count"],
        "today": today,
        "warnings": state["warnings"],
    }


# --------------------------------------------------------------------------- #
# 记账
# --------------------------------------------------------------------------- #

def record_trade(
    db: Session,
    *,
    user_id: str,
    symbol: str,
    action: str,
    price: float,
    shares: float,
    trade_date: str,
    name: str | None = None,
    sell_reason: str | None = None,
    sell_reason_note: str | None = None,
    report_id: str | None = None,
    plan_id: str | None = None,
    source: str = "manual",
    note: str | None = None,
) -> tuple[TradeLedgerDB | None, dict[str, Any]]:
    """写入一笔成交并回填该笔之后的持仓状态。

    返回 `(row, payload)`；`payload["status"]` 取值：

    * ``ok``       写入成功（幂等表未命中重复键）；
    * ``duplicate`` 命中 `(user_id, symbol, trade_date, action, price, shares)` 唯一约束，
      视为同一条成交被重复导入，返回 `(None, ...)` 而不是抛异常；
    * ``invalid``  参数非法（action/price/shares/trade_date/symbol/user_id），未落库。

    幂等是刚需：持仓导入会被反复执行，重复导入同一笔成交必须静默跳过。
    """

    warnings: list[str] = []

    user_id_norm = str(user_id or "").strip()
    if not user_id_norm:
        return None, {"status": "invalid", "reason": "user_id 不能为空", "state": None, "warnings": warnings}

    symbol_norm = normalize_symbol(symbol)
    if not symbol_norm:
        return None, {"status": "invalid", "reason": "symbol 不能为空", "state": None, "warnings": warnings}

    action_norm = normalize_action(action)
    if action_norm not in VALID_ACTIONS:
        return None, {
            "status": "invalid",
            "reason": f"action 必须为 {ACTION_BUY} 或 {ACTION_SELL}，收到：{action!r}",
            "state": None,
            "warnings": warnings,
        }

    price_val = _to_float(price)
    if price_val is None or price_val <= 0:
        return None, {"status": "invalid", "reason": "price 必须为正数", "state": None, "warnings": warnings}
    shares_val = _to_float(shares)
    if shares_val is None or shares_val <= 0:
        return None, {"status": "invalid", "reason": "shares 必须为正数", "state": None, "warnings": warnings}

    trade_date_norm = _is_valid_date_strict(trade_date)
    if trade_date_norm is None:
        return None, {
            "status": "invalid",
            "reason": "trade_date 必须为 YYYY-MM-DD 格式的合法日期",
            "state": None,
            "warnings": warnings,
        }

    # 卖出原因：卖出缺省为"人工决定"，买入不接受卖出原因
    reason_value: str | None = None
    if action_norm == ACTION_SELL:
        if sell_reason is None or (isinstance(sell_reason, str) and not sell_reason.strip()):
            reason_value = SELL_REASON_MANUAL
        else:
            reason_value, reason_warning = _sanitize_sell_reason(sell_reason)
            if reason_warning:
                warnings.append(reason_warning)
            if reason_value is None:
                reason_value = SELL_REASON_MANUAL
    elif sell_reason is not None and str(sell_reason).strip():
        warnings.append("买入不接受 sell_reason，已忽略")

    cost = compute_trade_cost(action_norm, price_val, shares_val)
    price_val = round(price_val, 4)
    shares_val = round(shares_val, 4)

    def _existing() -> TradeLedgerDB | None:
        return (
            db.query(TradeLedgerDB)
            .filter(
                TradeLedgerDB.user_id == user_id_norm,
                TradeLedgerDB.symbol == symbol_norm,
                TradeLedgerDB.trade_date == trade_date_norm,
                TradeLedgerDB.action == action_norm,
                TradeLedgerDB.price == price_val,
                TradeLedgerDB.shares == shares_val,
            )
            .first()
        )

    if _existing() is not None:
        return None, {
            "status": "duplicate",
            "reason": "同一笔成交已存在于台账，已跳过",
            "state": rebuild_position_state(db, user_id=user_id_norm, symbol=symbol_norm),
            "warnings": warnings,
        }

    row = TradeLedgerDB(
        id=uuid4().hex,
        user_id=user_id_norm,
        symbol=symbol_norm,
        name=(str(name).strip() if name else None),
        trade_date=trade_date_norm,
        action=action_norm,
        price=price_val,
        shares=shares_val,
        amount=cost.amount,
        fee=round(cost.commission + cost.transfer_fee, 2),
        stamp_tax=cost.stamp_tax,
        net_amount=cost.net_amount,
        slippage_pct=None,
        sell_reason=reason_value,
        sell_reason_note=(str(sell_reason_note).strip() if sell_reason_note else None),
        report_id=(str(report_id).strip() if report_id else None),
        plan_id=(str(plan_id).strip() if plan_id else None),
        source=(str(source).strip() if source else "manual") or "manual",
        note=(str(note).strip() if note else None),
        created_at=datetime.now(timezone.utc),
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        # 唯一约束兜底：并发写入或预检查遗漏时同样按"重复"处理
        db.rollback()
        return None, {
            "status": "duplicate",
            "reason": "同一笔成交已存在于台账（唯一约束冲突），已跳过",
            "state": rebuild_position_state(db, user_id=user_id_norm, symbol=symbol_norm),
            "warnings": warnings,
        }

    state = rebuild_position_state(db, user_id=user_id_norm, symbol=symbol_norm)
    rows = _load_rows(db, user_id_norm, symbol_norm)
    replay = _replay(rows, today=state["today"])
    snapshot = None
    if row.id in replay["row_ids"]:
        snapshot = replay["snapshots"][replay["row_ids"].index(row.id)]

    if snapshot is not None and not snapshot.get("skipped"):
        row.position_after = snapshot["position"]
        row.available_after = snapshot["available"]
        row.average_cost_after = snapshot["average_cost"]
        row.realized_pnl = snapshot["realized_pnl"] if action_norm == ACTION_SELL else None
    else:
        row.position_after = state["position"]
        row.available_after = state["available"]
        row.average_cost_after = state["average_cost"]
        row.realized_pnl = None

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return None, {
            "status": "duplicate",
            "reason": "同一笔成交已存在于台账（提交时唯一约束冲突），已跳过",
            "state": rebuild_position_state(db, user_id=user_id_norm, symbol=symbol_norm),
            "warnings": warnings,
        }

    return row, {
        "status": "ok",
        "state": state,
        "cost": cost.to_dict(),
        "warnings": warnings,
    }


# --------------------------------------------------------------------------- #
# 查询与归因
# --------------------------------------------------------------------------- #

def list_trades(
    db: Session,
    *,
    user_id: str,
    symbol: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[TradeLedgerDB]:
    """按用户（可选标的、可选日期区间，闭区间）列出流水，按时间升序。"""

    query = db.query(TradeLedgerDB).filter(TradeLedgerDB.user_id == user_id)
    symbol_norm = normalize_symbol(symbol) if symbol else None
    if symbol_norm:
        query = query.filter(TradeLedgerDB.symbol == symbol_norm)
    start = normalize_trade_date(start_date) if start_date else None
    if start:
        query = query.filter(TradeLedgerDB.trade_date >= start)
    end = normalize_trade_date(end_date) if end_date else None
    if end:
        query = query.filter(TradeLedgerDB.trade_date <= end)
    return query.order_by(TradeLedgerDB.trade_date, TradeLedgerDB.created_at, TradeLedgerDB.id).all()


def _sell_rows(db: Session, *, user_id: str, start_date: str | None, end_date: str | None) -> list[TradeLedgerDB]:
    """取出区间内的卖出流水（action 大小写不敏感，兼容他方写入）。"""

    query = db.query(TradeLedgerDB).filter(
        TradeLedgerDB.user_id == user_id,
        func.upper(TradeLedgerDB.action) == ACTION_SELL,
    )
    start = normalize_trade_date(start_date) if start_date else None
    if start:
        query = query.filter(TradeLedgerDB.trade_date >= start)
    end = normalize_trade_date(end_date) if end_date else None
    if end:
        query = query.filter(TradeLedgerDB.trade_date <= end)
    return query.order_by(TradeLedgerDB.trade_date, TradeLedgerDB.created_at, TradeLedgerDB.id).all()


def _row_amount(row: TradeLedgerDB) -> float:
    """成交额：优先用落库值，缺失时按 price * shares 补算。"""

    amount = _to_float(getattr(row, "amount", None))
    if amount is not None:
        return round(amount, 2)
    price = _to_float(getattr(row, "price", None)) or 0.0
    shares = _to_float(getattr(row, "shares", None)) or 0.0
    return round(price * shares, 2)


def _row_realized(row: TradeLedgerDB) -> float:
    value = _to_float(getattr(row, "realized_pnl", None))
    return round(value, 2) if value is not None else 0.0


def _group_by_sell_reason(rows: Sequence[TradeLedgerDB]) -> list[dict[str, Any]]:
    """按卖出原因聚合（缺失原因的流水归入"未归因"桶，便于度量覆盖率）。"""

    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        raw_reason = getattr(row, "sell_reason", None)
        reason = str(raw_reason).strip().lower() if raw_reason else ""
        if reason not in VALID_SELL_REASONS:
            reason = SELL_REASON_UNKNOWN
        bucket = buckets.setdefault(
            reason,
            {
                "sell_reason": reason,
                "label_zh": SELL_REASON_LABELS_ZH.get(reason, "未归因"),
                "count": 0,
                "shares": 0.0,
                "amount": 0.0,
                "realized_pnl": 0.0,
                "win_count": 0,
                "loss_count": 0,
                "flat_count": 0,
                "missing_reason_count": 0,
            },
        )
        realized = _row_realized(row)
        bucket["count"] += 1
        bucket["shares"] += _to_float(getattr(row, "shares", None)) or 0.0
        bucket["amount"] += _row_amount(row)
        bucket["realized_pnl"] += realized
        if realized > 0:
            bucket["win_count"] += 1
        elif realized < 0:
            bucket["loss_count"] += 1
        else:
            bucket["flat_count"] += 1
        if not raw_reason or not str(raw_reason).strip():
            bucket["missing_reason_count"] += 1

    items = []
    for bucket in buckets.values():
        bucket["shares"] = round(bucket["shares"], 4)
        bucket["amount"] = round(bucket["amount"], 2)
        bucket["realized_pnl"] = round(bucket["realized_pnl"], 2)
        items.append(bucket)
    items.sort(key=lambda item: (-abs(item["realized_pnl"]), -item["count"], item["sell_reason"]))
    return items


def attribution_by_sell_reason(
    db: Session,
    *,
    user_id: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    """按卖出原因归因已实现盈亏：回答"止损亏了多少、止盈赚了多少"。"""

    rows = _sell_rows(db, user_id=user_id, start_date=start_date, end_date=end_date)
    items = _group_by_sell_reason(rows)
    total_realized = round(sum(item["realized_pnl"] for item in items), 2)
    return {
        "items": items,
        "total_realized_pnl": total_realized,
        "total_count": len(rows),
        "total_shares": round(sum(item["shares"] for item in items), 4),
        "total_amount": round(sum(item["amount"] for item in items), 2),
        "win_count": sum(item["win_count"] for item in items),
        "loss_count": sum(item["loss_count"] for item in items),
        "missing_reason_count": sum(item["missing_reason_count"] for item in items),
        "start_date": normalize_trade_date(start_date) if start_date else None,
        "end_date": normalize_trade_date(end_date) if end_date else None,
        "label": "按卖出原因归因",
    }


def attribution_summary(db: Session, *, user_id: str) -> dict[str, Any]:
    """组合层面的盈亏归因：按原因、按标的，以及胜率/平均盈亏与归因覆盖率。

    `missing_sell_reason_count` 专门暴露"卖出原因缺失"的笔数：产品据此度量
    自己的归因覆盖率，否则"归因闭环"永远只是口号。
    """

    rows = list_trades(db, user_id=user_id)
    sell_rows = [row for row in rows if normalize_action(getattr(row, "action", None)) == ACTION_SELL]

    # 按标的聚合（买卖双侧统计，便于识别"高频交易但没赚钱"的标的）
    by_symbol: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = normalize_symbol(getattr(row, "symbol", None))
        bucket = by_symbol.setdefault(
            symbol,
            {
                "symbol": symbol,
                "name": getattr(row, "name", None),
                "realized_pnl": 0.0,
                "trade_count": 0,
                "buy_count": 0,
                "sell_count": 0,
                "win_count": 0,
                "loss_count": 0,
                "bought_amount": 0.0,
                "sold_amount": 0.0,
            },
        )
        if not bucket.get("name") and getattr(row, "name", None):
            bucket["name"] = getattr(row, "name")
        action = normalize_action(getattr(row, "action", None))
        bucket["trade_count"] += 1
        if action == ACTION_SELL:
            realized = _row_realized(row)
            bucket["sell_count"] += 1
            bucket["realized_pnl"] += realized
            bucket["sold_amount"] += _row_amount(row)
            if realized > 0:
                bucket["win_count"] += 1
            elif realized < 0:
                bucket["loss_count"] += 1
        elif action == ACTION_BUY:
            bucket["buy_count"] += 1
            bucket["bought_amount"] += _row_amount(row)

    symbol_items = []
    for bucket in by_symbol.values():
        bucket["realized_pnl"] = round(bucket["realized_pnl"], 2)
        bucket["bought_amount"] = round(bucket["bought_amount"], 2)
        bucket["sold_amount"] = round(bucket["sold_amount"], 2)
        symbol_items.append(bucket)
    symbol_items.sort(key=lambda item: (-item["realized_pnl"], item["symbol"]))

    wins = [_row_realized(row) for row in sell_rows if _row_realized(row) > 0]
    losses = [_row_realized(row) for row in sell_rows if _row_realized(row) < 0]
    flats = [_row_realized(row) for row in sell_rows if _row_realized(row) == 0]
    decided = len(wins) + len(losses)
    win_rate = round(len(wins) / decided, 4) if decided else 0.0

    missing_reason = sum(1 for row in sell_rows if not getattr(row, "sell_reason", None))
    coverage = round((len(sell_rows) - missing_reason) / len(sell_rows), 4) if sell_rows else 0.0

    items = _group_by_sell_reason(sell_rows)
    average_win = round(sum(wins) / len(wins), 2) if wins else 0.0
    average_loss = round(sum(losses) / len(losses), 2) if losses else 0.0

    return {
        "user_id": user_id,
        "by_sell_reason": items,
        "by_symbol": symbol_items,
        "total_realized_pnl": round(sum(item["realized_pnl"] for item in items), 2),
        "trade_count": len(rows),
        "buy_count": sum(1 for row in rows if normalize_action(getattr(row, "action", None)) == ACTION_BUY),
        "sell_count": len(sell_rows),
        "win_count": len(wins),
        "loss_count": len(losses),
        "flat_count": len(flats),
        "win_rate": win_rate,
        "average_win": average_win,
        "average_loss": average_loss,
        "average_loss_abs": abs(average_loss),
        "missing_sell_reason_count": missing_reason,
        "attribution_coverage": coverage,
        "total_bought_amount": round(sum(_row_amount(row) for row in rows if normalize_action(getattr(row, "action", None)) == ACTION_BUY), 2),
        "total_sold_amount": round(sum(_row_amount(row) for row in sell_rows), 2),
        "symbol_count": len(symbol_items),
        "label": "组合盈亏归因",
    }


# --------------------------------------------------------------------------- #
# 历史成交点导入
# --------------------------------------------------------------------------- #

# `trade_points_json` 的**真实来源**：本仓库从未生成过（`portfolio_import_service.py:203`
# 恒写入空列表），前端类型也只是 `Array<Record<string, unknown>>`
# （`frontend/src/types/index.ts:625`）。因此解析器按"券商导出/截图识别"的常见键名
# 做最大兼容，而不是绑定某个不存在的固定 schema。
_KEY_TRADE_DATE = (
    "trade_date", "date", "trade_time", "time", "datetime", "dt", "occurred_at",
    "成交日期", "成交时间", "交易日期", "日期", "时间",
)
_KEY_ACTION = (
    "action", "direction", "type", "side", "trade_type", "bs", "business", "business_name",
    "买卖方向", "买卖标志", "交易类型", "方向", "操作", "业务名称", "摘要",
)
_KEY_PRICE = (
    "price", "trade_price", "deal_price", "avg_price", "average_price", "filled_price",
    "成交价格", "成交均价", "成交价", "价格", "均价",
)
_KEY_SHARES = (
    "shares", "share", "quantity", "qty", "volume", "vol", "count",
    "成交数量", "成交量", "数量", "股数", "amount", "成交金额", "金额",
)
_KEY_SYMBOL = ("symbol", "code", "security_code", "stock_code", "证券代码", "股票代码", "代码")
_KEY_NAME = ("name", "security_name", "stock_name", "证券名称", "股票名称", "名称")
_KEY_REASON = ("sell_reason", "reason", "卖出原因", "原因")
_KEY_TEXT = (
    "note", "remark", "comment", "memo", "note_text", "摘要", "备注", "说明", "操作说明",
    "业务名称", "sell_reason_note", "卖出原因", "原因",
)


def _point_reason(nmap: Mapping[str, Any]) -> str | None:
    """从成交点里解析卖出原因：显式标准值优先，其次从文本推断。"""

    explicit = _pick(nmap, _KEY_REASON)
    if explicit is not None:
        key = str(explicit).strip().lower()
        if key in VALID_SELL_REASONS:
            return key
        if str(explicit).strip() in _ZH_LABEL_TO_REASON:
            return _ZH_LABEL_TO_REASON[str(explicit).strip()]
        inferred = infer_sell_reason_from_text(explicit)
        if inferred:
            return inferred
    text_parts = []
    for key in _KEY_TEXT:
        value = _pick(nmap, (key,))
        if value is not None and not isinstance(value, (Mapping, list, tuple)):
            text_parts.append(str(value))
    return infer_sell_reason_from_text(" ".join(text_parts))


def import_trade_points(
    db: Session,
    *,
    user_id: str,
    symbol: str,
    trade_points: list[dict],
    name: str | None = None,
    source: str = "import",
) -> dict[str, Any]:
    """把 `ImportedPortfolioPositionDB.trade_points_json` 里的成交点导入台账。

    解析对键名、数值格式、中文买卖方向都尽量宽容，**任何单条异常都只跳过不抛错**
    （历史数据脏是常态，导入不该因此整体失败）。重复导入同一批成交会全部落到
    `duplicates`，不产生重复行。
    """

    result: dict[str, Any] = {
        "user_id": user_id,
        "symbol": normalize_symbol(symbol),
        "inserted": 0,
        "duplicates": 0,
        "skipped": 0,
        "reasons": [],
        "warnings": [],
    }
    reasons: list[str] = result["reasons"]

    def _skip(index: int, message: str) -> None:
        result["skipped"] += 1
        if len(reasons) < _REASON_CAP:
            reasons.append(f"第 {index} 条跳过：{message}")
        elif len(reasons) == _REASON_CAP:
            reasons.append("（更多跳过原因已省略）")

    if not trade_points:
        result["state"] = rebuild_position_state(db, user_id=user_id, symbol=symbol)
        return result

    symbol_norm = normalize_symbol(symbol)
    for index, raw in enumerate(trade_points, start=1):
        if isinstance(raw, Mapping) and isinstance(raw.get("trade"), Mapping):
            # 兼容 {"trade": {...}} 的嵌套结构，外层字段优先
            merged = dict(raw["trade"])
            merged.update({key: value for key, value in raw.items() if key != "trade"})
            raw = merged
        if not isinstance(raw, Mapping):
            _skip(index, f"不是字典而是 {type(raw).__name__}")
            continue

        nmap = _norm_map(raw)
        point_symbol = normalize_symbol(_pick(nmap, _KEY_SYMBOL) or symbol_norm) or symbol_norm
        point_name = _pick(nmap, _KEY_NAME) or name

        trade_date = normalize_trade_date(_pick(nmap, _KEY_TRADE_DATE))
        if trade_date is None:
            _skip(index, "缺少或无法识别成交日期")
            continue
        action = normalize_action(_pick(nmap, _KEY_ACTION))
        if action is None:
            _skip(index, f"无法识别买卖方向（{_pick(nmap, _KEY_ACTION)!r}）")
            continue
        price = _to_float(_pick(nmap, _KEY_PRICE))
        if price is None or price <= 0:
            _skip(index, "成交价格缺失或非正数")
            continue
        shares = _to_float(_pick(nmap, _KEY_SHARES))
        if shares is None or shares <= 0:
            _skip(index, "成交数量缺失或非正数")
            continue

        sell_reason = _point_reason(nmap) if action == ACTION_SELL else None
        note_text = _pick(nmap, ("note", "remark", "comment", "备注", "说明"))

        try:
            row, payload = record_trade(
                db,
                user_id=user_id,
                symbol=point_symbol,
                action=action,
                price=price,
                shares=shares,
                trade_date=trade_date,
                name=str(point_name) if point_name else None,
                sell_reason=sell_reason,
                sell_reason_note=str(note_text) if note_text else None,
                source=source,
                note=str(note_text) if note_text else None,
            )
        except Exception as exc:  # pragma: no cover - 解析层兜底，绝不让导入整体失败
            logger.warning("导入成交点时发生未预期异常：%s", exc, exc_info=True)
            _skip(index, f"写入台账失败（{exc}）")
            continue

        status = payload.get("status")
        if status == "ok" and row is not None:
            result["inserted"] += 1
        elif status == "duplicate":
            result["duplicates"] += 1
        else:
            _skip(index, payload.get("reason") or "参数非法")

    db.commit()
    result["state"] = rebuild_position_state(db, user_id=user_id, symbol=symbol_norm)
    return result


# --------------------------------------------------------------------------- #
# 与导入快照对账
# --------------------------------------------------------------------------- #

def sync_from_imported_positions(db: Session, *, user_id: str) -> dict[str, Any]:
    """把导入的持仓快照（含其成交点）同步进台账，并与快照逐项对账。

    这是产品摆脱"静态快照"的桥：快照仍可继续导入，但持仓/可卖的**事实来源**
    切换为台账；两边不一致时用 `mismatches` 明确暴露，而不是默默相信快照。
    """

    today = cn_today_str()
    imported_rows = (
        db.query(ImportedPortfolioPositionDB)
        .filter(ImportedPortfolioPositionDB.user_id == user_id)
        .order_by(ImportedPortfolioPositionDB.symbol)
        .all()
    )

    # 台账已有代码（用于处理 "600519" 与 "600519.SH" 的别名差异）
    ledger_symbols = [
        row[0]
        for row in db.query(TradeLedgerDB.symbol)
        .filter(TradeLedgerDB.user_id == user_id)
        .distinct()
        .all()
    ]
    core_index: dict[str, str] = {}
    for symbol in ledger_symbols:
        core_index.setdefault(_symbol_core(symbol), symbol)

    symbol_states: list[dict[str, Any]] = []
    mismatches: list[dict[str, Any]] = []
    imported_totals = {"inserted": 0, "duplicates": 0, "skipped": 0, "reasons": []}

    for imported in imported_rows:
        symbol = normalize_symbol(imported.symbol)
        if not symbol:
            continue
        points = imported.trade_points_json if isinstance(imported.trade_points_json, list) else []
        imported_result = import_trade_points(
            db,
            user_id=user_id,
            symbol=symbol,
            trade_points=points,
            name=imported.security_name,
            source="import",
        )
        imported_totals["inserted"] += imported_result["inserted"]
        imported_totals["duplicates"] += imported_result["duplicates"]
        imported_totals["skipped"] += imported_result["skipped"]
        if len(imported_totals["reasons"]) < _REASON_CAP:
            for reason in imported_result["reasons"]:
                if len(imported_totals["reasons"]) >= _REASON_CAP:
                    imported_totals["reasons"].append("（更多跳过原因已省略）")
                    break
                imported_totals["reasons"].append(f"{symbol} {reason}")

        # 若快照代码带后缀而台账只有 6 位代码（或反之），按代码主体做别名匹配
        ledger_symbol = symbol
        if symbol not in ledger_symbols:
            alias = core_index.get(_symbol_core(symbol))
            if alias and alias != symbol:
                ledger_symbol = alias

        state = rebuild_position_state(db, user_id=user_id, symbol=ledger_symbol)
        symbol_mismatches: list[dict[str, Any]] = []

        def _compare(field: str, snapshot_value: Any, ledger_value: float, unit: str = "") -> None:
            snapshot_num = _to_float(snapshot_value)
            if snapshot_num is None:
                return  # 快照未提供该字段，无法对账
            diff = round(ledger_value - snapshot_num, 4)
            if abs(diff) <= _RECONCILE_EPS:
                return
            item = {
                "symbol": symbol,
                "ledger_symbol": ledger_symbol,
                "field": field,
                "snapshot": snapshot_num,
                "ledger": ledger_value,
                "diff": diff,
                "message": (
                    f"{symbol} {field} 不一致：快照 {_fmt_shares(snapshot_num)}{unit}，"
                    f"台账 {_fmt_shares(ledger_value)}{unit}（差额 {_fmt_shares(diff)}{unit}）"
                ),
            }
            symbol_mismatches.append(item)
            mismatches.append(item)

        _compare("position", imported.current_position, state["position"], "股")
        _compare("available", imported.available_position, state["available"], "股")

        symbol_states.append(
            {
                "symbol": symbol,
                "ledger_symbol": ledger_symbol,
                "name": imported.security_name,
                "source": imported.source,
                "imported": {
                    "current_position": _to_float(imported.current_position),
                    "available_position": _to_float(imported.available_position),
                    "average_cost": _to_float(imported.average_cost),
                    "trade_points_count": imported.trade_points_count or len(points),
                },
                "ledger": state,
                "average_cost_diff": (
                    round(state["average_cost"] - (_to_float(imported.average_cost) or 0.0), 4)
                    if _to_float(imported.average_cost) is not None
                    else None
                ),
                "mismatches": symbol_mismatches,
                "imported_result": {
                    "inserted": imported_result["inserted"],
                    "duplicates": imported_result["duplicates"],
                    "skipped": imported_result["skipped"],
                },
                "trade_points_count": len(points),
            }
        )

    ledger_trade_count = (
        db.query(func.count(TradeLedgerDB.id)).filter(TradeLedgerDB.user_id == user_id).scalar() or 0
    )

    return {
        "user_id": user_id,
        "today": today,
        "symbols": symbol_states,
        "positions": symbol_states,  # 别名：便于直接渲染表格
        "mismatches": mismatches,
        "imported": imported_totals,
        "summary": {
            "symbols": len(symbol_states),
            "mismatch_count": len(mismatches),
            "symbols_with_mismatch": len({item["symbol"] for item in mismatches}),
            "matched_symbols": len({item["symbol"] for item in symbol_states}) - len({item["symbol"] for item in mismatches}),
            "symbols_with_trade_points": sum(1 for item in symbol_states if item["trade_points_count"] > 0),
            "ledger_trade_count": int(ledger_trade_count),
            "imported": {
                "inserted": imported_totals["inserted"],
                "duplicates": imported_totals["duplicates"],
                "skipped": imported_totals["skipped"],
            },
        },
        "label": "导入快照 vs 交易台账 对账",
    }
