"""持仓解析（tasks 13.2/13.5）。

## 为什么必须有这一层，以及它的边界

路线图记录的核心缺陷之一是「99.5% 的深度分析是盲评」——模型在**假装你没有
持仓**的前提下回答「我这只该不该卖」。本轮引入持仓解析，但**不假装已经解决**：

| 数据源 | 现状 | 本层行为 |
|---|---|---|
| `trade_ledger`（成交流水） | 实测 **0 行** | 有则优先使用（可摊薄成本、可算 T+1 可卖） |
| `imported_portfolio_positions`（导入快照） | 390 行 | 退而用之（静态快照，无流水） |
| 两者都没有 | — | **显式 unknown**，不得假设空仓、不得编造成本 |

## 三条硬约束

1. **绝不默认假设**。没有数据时 `known=False` 且 `average_cost=None`，
   回答层据此说「未提供持仓」，而不是按空仓给建议。
2. **来源必须可辨**。台账与导入快照的可信度不同（前者可归因、后者是快照），
   回答与界面都要能区分。
3. **数值必须可引用**。成本/数量要作为证据项产出，否则回答层说「你的成本
   190」时无法被校验器核对——那就又回到「模型自己编一个成本」。
"""

from __future__ import annotations

import pytest


def _ledger_row(user_id: str, symbol: str, *, position_after: float, available_after: float,
                average_cost_after: float, trade_date: str = "2026-03-02") -> None:
    from api.database import TradeLedgerDB, get_db_ctx
    from tradingagents.agent.repository import new_id

    with get_db_ctx() as db:
        db.add(
            TradeLedgerDB(
                id=new_id(), user_id=user_id, symbol=symbol, trade_date=trade_date,
                action="BUY", price=average_cost_after, shares=100.0,
                position_after=position_after, available_after=available_after,
                average_cost_after=average_cost_after,
            )
        )
        db.commit()


def _imported_row(user_id: str, symbol: str, *, current_position: float,
                  available_position: float, average_cost: float) -> None:
    from api.database import ImportedPortfolioPositionDB, get_db_ctx
    from tradingagents.agent.repository import new_id

    with get_db_ctx() as db:
        db.add(
            ImportedPortfolioPositionDB(
                id=new_id(), user_id=user_id, symbol=symbol, source="manual",
                current_position=current_position, available_position=available_position,
                average_cost=average_cost,
            )
        )
        db.commit()


# ══════════════════════════════════════════════════════════════════════════
# 来源优先级
# ══════════════════════════════════════════════════════════════════════════


def test_ledger_is_preferred_over_imported_snapshot():
    """台账优先：它能还原成本与 T+1 可卖数量，导入快照只是会被整体覆盖的快照。"""
    from api.services.position_context_service import resolve_position

    _ledger_row("pos-a", "600519.SH", position_after=300, available_after=200, average_cost_after=1850.0)
    _imported_row("pos-a", "600519.SH", current_position=300, available_position=300, average_cost=1800.0)

    ctx = resolve_position(user_id="pos-a", symbol="600519.SH")
    assert ctx.known is True
    assert ctx.source == "trade_ledger"
    assert ctx.quantity == pytest.approx(300)
    assert ctx.sellable == pytest.approx(200)
    assert ctx.average_cost == pytest.approx(1850.0)


def test_imported_snapshot_used_when_ledger_is_empty():
    from api.services.position_context_service import resolve_position

    _imported_row("pos-b", "000001.SZ", current_position=1000, available_position=1000, average_cost=11.2)

    ctx = resolve_position(user_id="pos-b", symbol="000001.SZ")
    assert ctx.known is True
    assert ctx.source == "imported_snapshot"
    assert ctx.quantity == pytest.approx(1000)
    assert ctx.average_cost == pytest.approx(11.2)
    # 快照无法还原成交流水，这一点必须可辨
    assert "快照" in ctx.note or "snapshot" in ctx.note


def test_latest_ledger_row_wins():
    from api.services.position_context_service import resolve_position

    _ledger_row("pos-c", "600519.SH", position_after=100, available_after=0,
                average_cost_after=1900.0, trade_date="2026-02-01")
    _ledger_row("pos-c", "600519.SH", position_after=300, available_after=200,
                average_cost_after=1850.0, trade_date="2026-03-02")

    ctx = resolve_position(user_id="pos-c", symbol="600519.SH")
    assert ctx.quantity == pytest.approx(300)
    assert ctx.average_cost == pytest.approx(1850.0)
    assert ctx.as_of == "2026-03-02"


# ══════════════════════════════════════════════════════════════════════════
# 显式 unknown：绝不假设
# ══════════════════════════════════════════════════════════════════════════


def test_unknown_when_no_position_data_exists():
    """没有数据时必须显式 unknown——**不得假设空仓，更不得编造成本**。

    路线图记录的 99.5% 盲评正是这个病：模型在「假装你没有持仓」的前提下
    回答「该不该卖」。
    """
    from api.services.position_context_service import resolve_position

    ctx = resolve_position(user_id="pos-nobody", symbol="600519.SH")
    assert ctx.known is False
    assert ctx.source == "none"
    assert ctx.quantity is None
    assert ctx.average_cost is None
    assert "未提供" in ctx.note or "未知" in ctx.note


def test_flat_position_is_known_not_unknown():
    """台账显示已清仓（持仓 0）是**已知的空仓**，与「不知道」截然不同。

    把两者混为一谈会让回答层无法区分「你已卖出」与「你没告诉我」。
    """
    from api.services.position_context_service import resolve_position

    _ledger_row("pos-flat", "600519.SH", position_after=0, available_after=0,
                average_cost_after=0.0)

    ctx = resolve_position(user_id="pos-flat", symbol="600519.SH")
    assert ctx.known is True
    assert ctx.quantity == pytest.approx(0)
    assert ctx.is_flat is True


def test_null_cost_is_reported_as_unknown_not_zero():
    """成本列为 NULL 时不得当成 0——0 成本会让浮盈计算彻底失真。"""
    from api.database import ImportedPortfolioPositionDB, get_db_ctx
    from api.services.position_context_service import resolve_position
    from tradingagents.agent.repository import new_id

    with get_db_ctx() as db:
        db.add(
            ImportedPortfolioPositionDB(
                id=new_id(), user_id="pos-null-cost", symbol="600519.SH",
                source="manual", current_position=100, available_position=100,
                average_cost=None,
            )
        )
        db.commit()

    ctx = resolve_position(user_id="pos-null-cost", symbol="600519.SH")
    assert ctx.known is True
    assert ctx.quantity == pytest.approx(100)
    assert ctx.average_cost is None
    assert "成本" in ctx.note


# ══════════════════════════════════════════════════════════════════════════
# 用户隔离
# ══════════════════════════════════════════════════════════════════════════


def test_positions_are_isolated_per_user():
    from api.services.position_context_service import resolve_position

    _imported_row("owner", "600519.SH", current_position=500, available_position=500, average_cost=1900.0)

    other = resolve_position(user_id="intruder", symbol="600519.SH")
    assert other.known is False
    assert other.quantity is None


# ══════════════════════════════════════════════════════════════════════════
# 可引用证据
# ══════════════════════════════════════════════════════════════════════════


def test_known_position_emits_citable_evidence():
    """成本与数量必须作为证据项产出，回答层才能引用它们。

    否则模型说「你的成本 190」时无法被机械校验器核对——又回到编造。
    """
    from api.services.position_context_service import resolve_position

    _ledger_row("pos-ev", "600519.SH", position_after=300, available_after=200,
                average_cost_after=1850.0, trade_date="2026-03-02")

    ctx = resolve_position(user_id="pos-ev", symbol="600519.SH")
    evidence = ctx.to_evidence()
    fields = {e.field for e in evidence}
    assert "average_cost" in fields
    assert "quantity" in fields
    assert all(e.source_key.startswith("position") for e in evidence)
    assert all(e.as_of for e in evidence)


def test_unknown_position_emits_no_evidence():
    """未知持仓不得产出任何证据项——产出就等于断言了一个不存在的事实。"""
    from api.services.position_context_service import resolve_position

    ctx = resolve_position(user_id="pos-none-ev", symbol="600519.SH")
    assert ctx.to_evidence() == ()


def test_null_cost_emits_no_cost_evidence():
    from api.database import ImportedPortfolioPositionDB, get_db_ctx
    from api.services.position_context_service import resolve_position
    from tradingagents.agent.repository import new_id

    with get_db_ctx() as db:
        db.add(
            ImportedPortfolioPositionDB(
                id=new_id(), user_id="pos-null-ev", symbol="600519.SH", source="manual",
                current_position=100, available_position=100, average_cost=None,
            )
        )
        db.commit()

    ctx = resolve_position(user_id="pos-null-ev", symbol="600519.SH")
    assert "average_cost" not in {e.field for e in ctx.to_evidence()}


def test_context_serialises_for_the_api_layer():
    from api.services.position_context_service import resolve_position

    _imported_row("pos-ser", "600519.SH", current_position=100, available_position=80, average_cost=1900.0)
    payload = resolve_position(user_id="pos-ser", symbol="600519.SH").as_dict()
    assert payload["known"] is True
    assert payload["source"] == "imported_snapshot"
    assert payload["quantity"] == pytest.approx(100)
    assert "note" in payload
