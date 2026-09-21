"""会话状态持久化：跨轮恢复、用户隔离、坏数据降级。

## 这个测试锁住什么

1. **能跨轮恢复**：写进去的槽位/账本/未覆盖项，读出来必须一模一样（含数值类型）。
2. **用户隔离**：同一个 `conversation_id` 换个 `user_id` 读不到——只按会话 id 查
   会让猜中一个 id 就拿到别人的证据与槽位。
3. **坏 payload 降级不抛错**：状态是增强，读不到大不了少一点上下文，但**不能让
   整轮对话失败**。
4. **写失败不抛错**（落库故障不得让回答失败），未跑过的空状态不落库。
"""

from __future__ import annotations

import pytest

from tradingagents.agent.state import ConversationState
from tradingagents.grounding.types import Coverage, EvidenceItem


def _state_with_content(conversation_id: str = "conv-1") -> ConversationState:
    return ConversationState(conversation_id=conversation_id).advance(
        evidence=[
            EvidenceItem(
                id="E1", field="现价", value=1700.0, unit="yuan",
                source_key="stock_data", as_of="2026-09-21",
            )
        ],
        coverage=Coverage(available=("stock_data",), unavailable=("northbound（北向资金未覆盖）",)),
        slots={"symbol": "600519.SH", "as_of": "2026-09-21"},
        pending={"kind": "watchlist_add", "params": {"symbol": "600519.SH"}},
        job_ids=["job-1"],
    )


def test_state_round_trips_through_the_database():
    from api.database import get_db_ctx
    from api.services import conversation_state_service as svc

    with get_db_ctx() as db:
        assert svc.save_state(db, user_id="state-user", state=_state_with_content()) is True

    with get_db_ctx() as db:
        loaded = svc.load_state(db, user_id="state-user", conversation_id="conv-1")

    assert loaded.turn == 1
    assert loaded.slots == {"symbol": "600519.SH", "as_of": "2026-09-21"}
    assert loaded.uncovered == ("northbound（北向资金未覆盖）",)
    assert loaded.pending == {"kind": "watchlist_add", "params": {"symbol": "600519.SH"}}
    assert loaded.job_ids == ("job-1",)
    assert loaded.evidence_ids() == ("E1",)
    assert loaded.ledger[0].item.value == 1700.0, "数值类型必须原样还原"


def test_state_is_saved_as_an_upsert_not_duplicated_rows():
    from api.database import ChatConversationStateDB, get_db_ctx
    from api.services import conversation_state_service as svc

    with get_db_ctx() as db:
        svc.save_state(db, user_id="upsert-user", state=_state_with_content("conv-up"))
        second = _state_with_content("conv-up").advance(slots={"as_of": "2026-09-22"})
        svc.save_state(db, user_id="upsert-user", state=second)

    with get_db_ctx() as db:
        rows = (
            db.query(ChatConversationStateDB)
            .filter(ChatConversationStateDB.conversation_id == "conv-up")
            .all()
        )
    assert len(rows) == 1, "同一会话只能有一行状态"
    assert rows[0].payload["turn"] == 2


def test_state_is_user_scoped():
    from api.database import get_db_ctx
    from api.services import conversation_state_service as svc

    with get_db_ctx() as db:
        svc.save_state(db, user_id="owner", state=_state_with_content("conv-shared"))

    with get_db_ctx() as db:
        other = svc.load_state(db, user_id="intruder", conversation_id="conv-shared")

    assert other.turn == 0, "换一个用户读同一个会话必须拿到空状态（防串号）"
    assert other.slots == {}
    assert other.ledger == ()


def test_corrupt_payload_degrades_to_an_empty_state():
    from api.database import ChatConversationStateDB, get_db_ctx
    from api.services import conversation_state_service as svc

    with get_db_ctx() as db:
        db.add(
            ChatConversationStateDB(
                conversation_id="conv-corrupt", user_id="corrupt-user", payload="not-a-mapping"
            )
        )
        db.commit()

    with get_db_ctx() as db:
        loaded = svc.load_state(db, user_id="corrupt-user", conversation_id="conv-corrupt")

    assert loaded.turn == 0, "坏 payload 必须降级成空状态，而不是抛错"


def test_empty_or_foreign_keys_are_no_ops():
    from api.database import get_db_ctx
    from api.services import conversation_state_service as svc

    with get_db_ctx() as db:
        assert svc.save_state(db, user_id="", state=_state_with_content()) is False
        # 还没跑过任何一轮 → 不落库（避免"有状态但全是空"的假象）
        assert svc.save_state(
            db, user_id="u", state=ConversationState(conversation_id="conv-empty")
        ) is False
        assert svc.load_state(db, user_id="u", conversation_id=None).turn == 0


def test_save_failure_does_not_raise(monkeypatch):
    """落库故障不得让回答失败：状态是增强，不是回答的前置条件。"""
    from api.database import get_db_ctx
    from api.services import conversation_state_service as svc

    class _BoomSession:
        def query(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise RuntimeError("db is gone")

        def rollback(self) -> None:
            raise RuntimeError("rollback also fails")

    assert svc.save_state(_BoomSession(), user_id="u", state=_state_with_content()) is False
    loaded = svc.load_state(_BoomSession(), user_id="u", conversation_id="conv-1")
    assert loaded.turn == 0

    with get_db_ctx() as db:  # 真库仍然可正常读写（上面那次失败没有污染会话）
        assert svc.save_state(db, user_id="u", state=_state_with_content("conv-ok")) is True


def test_state_summary_is_trace_sized():
    from api.services import conversation_state_service as svc

    summary = svc.state_summary(_state_with_content())
    assert summary["turn"] == 1
    assert summary["evidence_ids"] == ["E1"]
    assert summary["pending_kind"] == "watchlist_add"
    assert "1700" not in str(summary), "摘要不得带证据正文，否则 trace 会被账本撑爆"
