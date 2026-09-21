"""跨轮状态端到端（Phase 3.2–3.5）：指代、账本、未覆盖延续、跨轮确认。

## 这个测试锁住什么

单元测试已覆盖状态对象的语义（`test_conversation_state.py`）与落库
（`test_conversation_state_store.py`）。这里锁的是**接线**：

1. **未覆盖项真的延续到下一轮的回答里**：上一轮声明「capital_composition 未覆盖」，
   下一轮没覆盖到它时，回答里必须仍然声明——这是最容易被静默丢掉的一条。
2. **证据账本跨轮编号不撞车**：第二轮的新证据接着第一轮的编号往下排。
3. **指代靠槽位、不靠模型猜**：第二轮只说「它」，标的必须来自上一轮的槽位；
   槽位为空时**不猜**（进入澄清）。
4. **跨轮「确认」能真的执行**：上一轮提议、下一轮回复「确认」时动作必须落到
   既有服务；确认之后提议必须作废，否则再一句「好的」会重复执行。
"""

from __future__ import annotations

import pytest


def _loop_harness():
    """复用既有 agent loop 测试的替身（假端口 / 假模型 / 冻结时钟）。"""
    from tests.test_agent_loop import FrozenClock, QueueModel, _ports_factory, _registry

    return FrozenClock, QueueModel, _ports_factory, _registry


def _grounded(evidence, verdict):
    from tradingagents.grounding.types import Claim, StructuredAnswer

    ref = evidence.items[0].id if evidence.items else None
    claims = (Claim(text="反抽量能恢复至 1.228", refs=(ref,)),) if ref else ()
    return StructuredAnswer(
        summary="缩量下杀后出现承接。",
        claims=claims,
        verdict_ref=verdict.id if verdict is not None else None,
        declared_unavailable=tuple(getattr(verdict, "unavailable", ()) or ()),
    )


def _run(text, *, state=None, model=None, symbol="600519.SH", **kw):
    from tradingagents.agent.loop import TurnRequest, run_turn
    from tradingagents.agent.events import EventSink

    FrozenClock, QueueModel, _ports_factory, _registry = _loop_harness()

    class _Sink(EventSink):
        def __init__(self):
            self.events = []

        def emit(self, event):
            self.events.append(event)

    sink = _Sink()
    request = TurnRequest(
        text=text, user_id="u1", tier="medium", symbol=symbol, as_of="2026-01-16",
        state=state, **kw,
    )
    result = run_turn(
        request=request,
        registry=_registry(),
        model=model or QueueModel([_grounded]),
        sink=sink,
        clock=FrozenClock(),
    )
    return result, sink


def test_uncovered_carries_into_the_next_turn_declaration():
    """上一轮的未覆盖项必须继续出现在下一轮的声明里（不得静默消失）。"""
    from tradingagents.agent.state import ConversationState
    from tradingagents.grounding.types import Coverage

    first, _ = _run("600519 是不是在洗盘")
    assert first.conversation_state is not None, "回合结束必须返回推进后的会话状态"

    state = first.conversation_state.advance(
        coverage=Coverage(available=("stock_data",), unavailable=("capital_composition（资金流未覆盖）",))
    )

    second, _ = _run("那它还能拿吗", state=state)

    declared = "、".join(second.unavailable_declared)
    assert "capital_composition" in declared, (
        "上一轮声明未覆盖的维度，在下一轮未覆盖到时必须继续声明"
    )


def test_evidence_ids_do_not_collide_across_turns():
    first, _ = _run("600519 是不是在洗盘")
    state = first.conversation_state
    assert state is not None and state.evidence_ids(), "第一轮必须留下证据"

    second, _ = _run("再确认一下均线", state=state)

    ids = second.conversation_state.evidence_ids()
    assert len(ids) == len(set(ids)), "跨轮的证据编号必须全局唯一"
    assert second.conversation_state.ledger[0].turn == 1
    assert second.conversation_state.ledger[-1].turn == 2


def test_slots_track_the_symbol_and_survive_a_question_without_one():
    first, _ = _run("600519 是不是在洗盘")
    state = first.conversation_state
    assert state.current_symbol() == "600519.SH"

    # 一个**没有标的**的问句不该把槽位清空（否则下一句「它」就没得指了）
    second, _ = _run("今天大盘怎么样", state=state, symbol=None)
    assert second.conversation_state.current_symbol() == "600519.SH"


def test_reference_resolution_is_used_by_the_service_layer():
    """服务层必须用槽位消解指代（而不是把「它」直接丢给模型猜）。"""
    import inspect

    from api.services import chat_service

    source = inspect.getsource(chat_service)
    assert "state.resolve_symbol(" in source, "chat_service 必须用槽位消解指代"
    assert "reference resolved from conversation state" in source, "消解依据要留痕（可排查）"


def test_reference_without_a_slot_does_not_invent_a_symbol():
    """槽位为空时「它」不得被解析成任何标的——交给澄清分支去问。"""
    from tradingagents.agent.state import ConversationState

    empty = ConversationState(conversation_id="c-empty")
    assert empty.resolve_symbol("它现在怎么样") == (None, "reference_without_slot")


def test_confirmation_next_turn_executes_and_then_clears_the_proposal():
    """跨轮确认：上一轮提议、下一轮「确认」，动作必须落到既有服务且随后作废。"""
    from api.database import WatchlistItemDB, get_db_ctx
    from api.services import chat_service

    executor = chat_service.build_action_executor("confirm-user")
    summary = executor(
        {"kind": "watchlist_add", "params": {"symbol": "300394.SZ"}}, "confirm-user"
    )
    assert "300394.SZ" in summary

    with get_db_ctx() as db:
        symbols = [
            row.symbol
            for row in db.query(WatchlistItemDB)
            .filter(WatchlistItemDB.user_id == "confirm-user")
            .all()
        ]
    assert symbols == ["300394.SZ"], "确认后必须真的写入自选"


def test_loop_reads_pending_from_state_and_clears_it_after_confirm():
    """待确认提议来自会话状态时，确认分支必须执行；执行后状态里的提议必须清空。"""
    from tradingagents.agent.state import ConversationState
    from tradingagents.agent.loop import TurnRequest, run_turn

    FrozenClock, QueueModel, _ports_factory, _registry = _loop_harness()
    from tradingagents.agent.events import EventSink

    class _Sink(EventSink):
        def __init__(self):
            self.events = []

        def emit(self, event):
            self.events.append(event)

    sink = _Sink()
    calls: list[dict] = []
    state = ConversationState(conversation_id="c-confirm").advance(
        pending={"kind": "watchlist_add", "params": {"symbol": "600519.SH"}}
    )
    result = run_turn(
        request=TurnRequest(
            text="确认", user_id="u1", tier="medium", symbol="600519.SH",
            as_of="2026-01-16", state=state,
        ),
        registry=_registry(),
        model=QueueModel([]),
        sink=sink,
        clock=FrozenClock(),
        action_executor=lambda proposal, uid: calls.append(dict(proposal)) or "已加入自选。",
    )

    assert calls and calls[0]["kind"] == "watchlist_add", "跨轮确认必须调用执行器"
    assert result.action_executed is True
    assert result.conversation_state.pending is None, "确认之后提议必须作废（防重复执行）"


def test_rejection_next_turn_does_not_execute():
    from tradingagents.agent.state import ConversationState
    from tradingagents.agent.loop import TurnRequest, run_turn

    FrozenClock, QueueModel, _ports_factory, _registry = _loop_harness()
    from tradingagents.agent.events import EventSink

    class _Sink(EventSink):
        def __init__(self):
            self.events = []

        def emit(self, event):
            self.events.append(event)

    calls: list[dict] = []
    state = ConversationState(conversation_id="c-reject").advance(
        pending={"kind": "watchlist_add", "params": {"symbol": "600519.SH"}}
    )
    result = run_turn(
        request=TurnRequest(
            text="算了", user_id="u1", tier="medium", symbol="600519.SH",
            as_of="2026-01-16", state=state,
        ),
        registry=_registry(),
        model=QueueModel([]),
        sink=_Sink(),
        clock=FrozenClock(),
        action_executor=lambda proposal, uid: calls.append(dict(proposal)) or "不该被调用",
    )

    assert calls == [], "拒绝绝不能执行动作"
    assert result.conversation_state.pending is None


def test_a_fresh_turn_does_not_resurrect_a_stale_proposal():
    """用户没确认就问了别的：提议必须作废，避免下一句「好的」误执行旧动作。

    这是**端到端**断言（走 loop），不是只断言状态对象：真正会误执行的是
    「状态里还挂着旧提议 + 用户下一句说了「好的」」这个组合。
    """
    from tradingagents.agent.state import ConversationState

    state = ConversationState(conversation_id="c-stale").advance(
        pending={"kind": "watchlist_add", "params": {"symbol": "600519.SH"}}
    )
    assert state.pending is not None

    # 用户没确认，问了别的（不是确认词）→ 这一轮走正常流程
    result, _ = _run("这只票的均线怎么样", state=state)

    assert result.conversation_state.pending is None, (
        "未确认的旧提议必须作废，否则下一句「好的」会执行用户早已放弃的动作"
    )

    # 作废之后，即使下一句是确认词，也不该有任何执行
    from tradingagents.agent.loop import TurnRequest, run_turn

    FrozenClock, QueueModel, _ports_factory, _registry = _loop_harness()
    from tradingagents.agent.events import EventSink

    class _Sink(EventSink):
        def __init__(self):
            self.events = []

        def emit(self, event):
            self.events.append(event)

    calls: list[dict] = []
    after = run_turn(
        request=TurnRequest(
            text="好的", user_id="u1", tier="medium", symbol="600519.SH",
            as_of="2026-01-16", state=result.conversation_state,
        ),
        registry=_registry(),
        model=QueueModel([_grounded]),
        sink=_Sink(),
        clock=FrozenClock(),
        action_executor=lambda proposal, uid: calls.append(dict(proposal)) or "不该执行",
    )
    assert calls == [], "旧提议作废后，一句「好的」不得触发任何动作"


def test_job_ids_land_in_the_state_when_a_submit_tool_runs():
    """提交异步任务后，任务号必须进状态（否则下一轮无从续接）。"""
    from tradingagents.agent.loop import TurnRequest, run_turn

    FrozenClock, QueueModel, _ports_factory, _registry = _loop_harness()
    from tradingagents.agent.events import EventSink
    from tradingagents.grounding.types import Coverage, EvidenceItem
    from tradingagents.tools.spec import ToolResult

    class _Sink(EventSink):
        def __init__(self):
            self.events = []

        def emit(self, event):
            self.events.append(event)

    def _task_ports(symbol, as_of):
        return object()

    from tradingagents.tools import build_registry

    registry = build_registry(
        ports_factory=_ports_factory(),
        news_fetcher=lambda *a: [],
        task_port=type(
            "_TaskPort",
            (),
            {
                "submit_backtest": lambda self, **kw: "job-abc",
                "job_status": lambda self, **kw: None,
            },
        )(),
    )
    result = run_turn(
        request=TurnRequest(
            text="回测一下 600519", user_id="u1", tier="medium", symbol="600519.SH",
            as_of="2026-01-16",
            planned_steps=(
                {"tool": "submit_backtest_job",
                 "arguments": {"symbol": "600519.SH", "start_date": "2026-01-01",
                               "end_date": "2026-02-01"},
                 "reason": "planned"},
            ),
        ),
        registry=registry,
        model=QueueModel([_grounded]),
        sink=_Sink(),
        clock=FrozenClock(),
    )

    assert "job-abc" in result.conversation_state.job_ids, (
        "异步任务号必须进会话状态，否则「刚才那个任务好了吗」答不出来"
    )
