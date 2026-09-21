"""持仓解析：把「用户到底持不持有这只票」变成一个**有来源、可引用**的事实。

## 解决的是什么

路线图记录的核心缺陷：**99.5% 的深度分析是盲评**——模型在「用户未提供持仓，
按无持仓处理」的前提下回答「我这只该不该卖」。本模块提供持仓的只读解析，
使回答层能真正基于持仓作答。

## 但绝不假装问题已经解决

`trade_ledger` 实测 **0 行**：没有成交流水，就无法摊薄成本、无法算 T+1 可卖
数量。`imported_portfolio_positions` 有 390 行，但那是**会被整体覆盖的静态
快照**。因此本模块：

* 优先用台账（可归因），退而用导入快照（快照），都没有则**显式 unknown**
* 每个结果都带 `source`，让回答与界面能区分可信度
* 没有数据时**不假设空仓、不编造成本**——`known=False` 就是「不知道」

## 为什么成本与数量要作为证据项产出

回答层说「你的成本 190」时，机械校验器必须能核对这个数字。若持仓数值不进
证据集，模型就只能凭上下文复述——那又回到编造。因此 `to_evidence()` 只在
数值**真实存在**时产出条目；NULL 成本不产出任何条目（0 成本会让浮盈彻底失真）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from tradingagents.grounding.types import EvidenceItem

SOURCE_LEDGER = "trade_ledger"
SOURCE_IMPORTED = "imported_snapshot"
SOURCE_NONE = "none"

NOTE_LEDGER = "来源：交易台账（可还原成本与 T+1 可卖数量）"
NOTE_IMPORTED = "来源：导入持仓快照（静态快照，无法还原成交流水与 T+1 可卖数量）"
NOTE_NONE = (
    "未提供持仓：系统不知道你是否持有该标的，"
    "因此不会按「空仓」或「持仓」给出建议——如需持仓相关判断请先录入台账或导入持仓"
)


@dataclass(frozen=True)
class PositionContext:
    symbol: str
    known: bool
    source: str = SOURCE_NONE
    quantity: float | None = None
    sellable: float | None = None
    average_cost: float | None = None
    position_pct: float | None = None
    market_value: float | None = None
    security_name: str | None = None
    as_of: str | None = None
    note: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_flat(self) -> bool:
        """已知的空仓（数量为 0）。与「不知道」截然不同。"""
        return self.known and self.quantity == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "known": self.known,
            "source": self.source,
            "quantity": self.quantity,
            "sellable": self.sellable,
            "average_cost": self.average_cost,
            "position_pct": self.position_pct,
            "market_value": self.market_value,
            "security_name": self.security_name,
            "as_of": self.as_of,
            "is_flat": self.is_flat,
            "note": self.note,
        }

    def to_evidence(self) -> tuple[EvidenceItem, ...]:
        """产出可引用的证据项。**只产出真实存在的数值**。"""
        if not self.known:
            return ()
        as_of = self.as_of or ""
        items: list[EvidenceItem] = []
        if self.quantity is not None:
            items.append(
                EvidenceItem(
                    id=f"P{len(items) + 1}", field="quantity", value=self.quantity,
                    unit="count", source_key="position_context", as_of=as_of,
                )
            )
        if self.average_cost is not None:
            items.append(
                EvidenceItem(
                    id=f"P{len(items) + 1}", field="average_cost", value=self.average_cost,
                    unit="yuan", source_key="position_context", as_of=as_of,
                )
            )
        if self.sellable is not None:
            items.append(
                EvidenceItem(
                    id=f"P{len(items) + 1}", field="sellable", value=self.sellable,
                    unit="count", source_key="position_context", as_of=as_of,
                )
            )
        return tuple(items)


def _unknown(symbol: str) -> PositionContext:
    return PositionContext(symbol=symbol, known=False, source=SOURCE_NONE, note=NOTE_NONE)


def _from_ledger(db: Any, user_id: str, symbol: str) -> PositionContext | None:
    from api.database import TradeLedgerDB

    row = (
        db.query(TradeLedgerDB)
        .filter(TradeLedgerDB.user_id == user_id, TradeLedgerDB.symbol == symbol)
        .order_by(TradeLedgerDB.trade_date.desc(), TradeLedgerDB.created_at.desc())
        .first()
    )
    if row is None:
        return None

    quantity = row.position_after
    cost = row.average_cost_after
    note = NOTE_LEDGER
    if cost is None and quantity not in (None, 0):
        # 台账在但成本为空：数量可信、成本不可知。如实说明，不给 0。
        note = f"{NOTE_LEDGER}；成本未记录，浮盈相关判断不可用"
    return PositionContext(
        symbol=symbol,
        known=True,
        source=SOURCE_LEDGER,
        quantity=quantity,
        sellable=row.available_after,
        average_cost=cost,
        security_name=row.name,
        as_of=str(row.trade_date)[:10] if row.trade_date else None,
        note=note,
    )


def _from_imported(db: Any, user_id: str, symbol: str) -> PositionContext | None:
    from api.database import ImportedPortfolioPositionDB

    row = (
        db.query(ImportedPortfolioPositionDB)
        .filter(
            ImportedPortfolioPositionDB.user_id == user_id,
            ImportedPortfolioPositionDB.symbol == symbol,
        )
        .order_by(ImportedPortfolioPositionDB.updated_at.desc())
        .first()
    )
    if row is None:
        return None

    note = NOTE_IMPORTED
    if row.average_cost is None and row.current_position not in (None, 0):
        note = f"{NOTE_IMPORTED}；成本未导入，浮盈相关判断不可用"

    return PositionContext(
        symbol=symbol,
        known=True,
        source=SOURCE_IMPORTED,
        quantity=row.current_position,
        sellable=row.available_position,
        average_cost=row.average_cost,
        position_pct=row.current_position_pct,
        market_value=row.market_value,
        security_name=row.security_name,
        as_of=str(row.latest_trade_at)[:10] if row.latest_trade_at else None,
        note=note,
    )


def resolve_position(
    *,
    user_id: str,
    symbol: str,
    db: Any | None = None,
) -> PositionContext:
    """解析用户在某标的上的持仓。台账优先，其次导入快照，都没有则显式未知。"""
    if not user_id or not symbol:
        return _unknown(symbol or "")

    if db is not None:
        return _resolve_with(db, user_id, symbol)

    from api.database import get_db_ctx

    with get_db_ctx() as session:
        return _resolve_with(session, user_id, symbol)


def _resolve_with(db: Any, user_id: str, symbol: str) -> PositionContext:
    try:
        ledger = _from_ledger(db, user_id, symbol)
    except Exception:  # noqa: BLE001 — 表缺失/查询失败都退化为未知，不中断对话
        ledger = None
    if ledger is not None:
        return ledger

    try:
        imported = _from_imported(db, user_id, symbol)
    except Exception:  # noqa: BLE001
        imported = None
    if imported is not None:
        return imported

    return _unknown(symbol)
