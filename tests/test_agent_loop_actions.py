"""行动提议与澄清在回合循环里的接线。

守的是「不暗示已执行」与「外部面不得执行」这两条：
* Agent 无法执行时必须明说，而不是含糊带过——用户以为设置好了却没设置，
  比不回答更糟
* MCP 等外部面 V1 只读：同一份提议在产品内可确认执行，在外部面必须被拒绝
"""

from __future__ import annotations

from tests.test_agent_loop import (
    CollectingSink,
    FrozenClock,
    QueueModel,
    _grounded_answer,
    _registry,
    _request,
)


def _run(model, *, request=None, sink=None, **kw):
    from tradingagents.agent.loop import run_turn

    sink = sink or CollectingSink()
    result = run_turn(
        request=request or _request(),
        registry=kw.pop("registry", None) or _registry(),
        model=model,
        sink=sink,
        clock=kw.pop("clock", None) or FrozenClock(),
        middleware=kw.pop("middleware", None),
        **kw,
    )
    return result, sink


# ══════════════════════════════════════════════════════════════════════════
# 提议
# ══════════════════════════════════════════════════════════════════════════


def test_price_alert_request_emits_a_proposal_event():
    from tradingagents.agent.events import ProposalEvent

    result, sink = _run(
        QueueModel([_grounded_answer]),
        request=_request(text="帮我盯着 600519，跌破 180 告诉我"),
    )

    proposals = sink.of(ProposalEvent)
    assert len(proposals) == 1
    assert proposals[0].proposal["kind"] == "price_alert"
    assert result.proposal is not None
    assert result.proposal["params"]["threshold"] == 180.0


def test_proposal_asks_for_confirmation_and_does_not_claim_execution():
    """不能暗示已执行：必须明说在等确认。"""
    result, _ = _run(
        QueueModel([_grounded_answer]),
        request=_request(text="跌破 180 告诉我"),
    )
    assert result.proposal is not None
    assert result.action_executed is False
    assert "确认" in result.answer or "回复" in result.answer


def test_deep_analysis_request_emits_an_escalation_proposal():
    """重档是「提议」：路由层绝不静默升级，但用户明示后可以要到它。"""
    from tradingagents.agent.events import ProposalEvent

    result, sink = _run(
        QueueModel([_grounded_answer]),
        request=_request(text="启动深度分析", tier="medium"),
    )
    proposals = sink.of(ProposalEvent)
    assert proposals and proposals[0].proposal["kind"] == "deep_analysis"
    assert result.proposal["requires_confirmation"] is True


def test_plain_question_emits_no_proposal():
    from tradingagents.agent.events import ProposalEvent

    _, sink = _run(QueueModel([_grounded_answer]), request=_request(text="600519 是不是在洗盘"))
    assert sink.of(ProposalEvent) == []


# ══════════════════════════════════════════════════════════════════════════
# 两步确认与执行
# ══════════════════════════════════════════════════════════════════════════


def _pending(surface="product_chat"):
    return {
        "kind": "price_alert",
        "params": {"symbol": "600519.SH", "threshold": 180.0, "direction": "below"},
        "summary": "为 600519.SH 创建价格预警",
        "requires_confirmation": True,
    }


def test_confirmation_executes_via_injected_executor():
    from tradingagents.agent.events import ActionExecutedEvent

    calls: list[tuple] = []

    def executor(proposal, user_id):
        calls.append((proposal["kind"], user_id, proposal["params"]["threshold"]))
        return "预警已创建：600519.SH 跌破 180 时通知你。"

    result, sink = _run(
        QueueModel([]),
        request=_request(text="确认", pending_proposal=_pending()),
        action_executor=executor,
    )

    assert calls == [("price_alert", "u1", 180.0)]
    assert result.action_executed is True
    assert "已创建" in result.answer
    assert sink.of(ActionExecutedEvent)


def test_confirmation_without_pending_proposal_does_not_execute():
    calls: list = []
    result, _ = _run(
        QueueModel([_grounded_answer]),
        request=_request(text="确认"),
        action_executor=lambda proposal, user_id: calls.append(proposal),
    )
    assert calls == []
    assert result.action_executed is False


def test_external_surface_must_not_execute_actions():
    """同一份提议在产品内可执行，在 MCP 面必须被拒绝。"""
    from tradingagents.agent.events import ActionExecutedEvent

    calls: list = []
    result, sink = _run(
        QueueModel([_grounded_answer]),
        request=_request(
            text="确认", pending_proposal=_pending(), surface="mcp"
        ),
        action_executor=lambda proposal, user_id: calls.append(proposal),
    )

    assert calls == []
    assert result.action_executed is False
    assert sink.of(ActionExecutedEvent) == []
    # 必须明确告知无法执行，而不是含糊带过
    assert "无法" in result.answer or "不支持" in result.answer


def test_rejection_cancels_the_pending_proposal_without_executing():
    calls: list = []
    result, _ = _run(
        QueueModel([_grounded_answer]),
        request=_request(text="取消", pending_proposal=_pending()),
        action_executor=lambda proposal, user_id: calls.append(proposal),
    )
    assert calls == []
    assert result.action_executed is False
    assert result.proposal is None
    assert "取消" in result.answer


def test_executor_failure_is_reported_not_swallowed():
    def _boom(proposal, user_id):
        raise RuntimeError("alert service down")

    result, _ = _run(
        QueueModel([]),
        request=_request(text="确认", pending_proposal=_pending()),
        action_executor=_boom,
    )
    assert result.action_executed is False
    assert "失败" in result.answer or "未完成" in result.answer


def test_a_new_question_is_not_treated_as_confirmation():
    """「好的，那 600519 呢」不是确认——误执行动作的代价高于多问一次。"""
    calls: list = []
    result, _ = _run(
        QueueModel([_grounded_answer]),
        request=_request(text="好的，那 600519 现在多少钱", pending_proposal=_pending(), tier="light"),
        action_executor=lambda proposal, user_id: calls.append(proposal),
    )
    assert calls == []
    assert result.action_executed is False


# ══════════════════════════════════════════════════════════════════════════
# 澄清
# ══════════════════════════════════════════════════════════════════════════


def test_missing_symbol_asks_for_clarification_instead_of_guessing():
    """缺标的必须追问：猜一个标的等于制造一个错误答案。"""
    from tradingagents.agent.events import ClarificationNeeded

    result, sink = _run(
        QueueModel([]),
        request=_request(text="帮我看看还能不能拿", symbol=None, tier="medium"),
    )

    assert sink.of(ClarificationNeeded)
    assert result.proposal is None
    assert result.tool_calls == ()
    assert "哪只" in result.answer or "标的" in result.answer or "代码" in result.answer


def test_multiple_candidates_are_presented_for_selection():
    from tradingagents.agent.events import ClarificationNeeded

    result, sink = _run(
        QueueModel([]),
        request=_request(text="分析一下白酒", symbol=None, candidates=("600519.SH", "600809.SH")),
    )

    events = sink.of(ClarificationNeeded)
    assert events
    assert set(events[0].candidates) == {"600519.SH", "600809.SH"}
    assert "600519.SH" in result.answer and "600809.SH" in result.answer


def test_clarification_does_not_start_tools_or_the_model():
    """追问回合不该付任何模型/工具成本——它只是一句澄清问题。"""
    model = QueueModel([])
    result, _ = _run(
        model,
        request=_request(text="帮我看看还能不能拿", symbol=None),
    )
    assert model.propose_calls == 0
    assert result.tool_calls == ()
    assert result.status == "completed"


# ══════════════════════════════════════════════════════════════════════════
# 持仓上下文接入回答路径（tasks 13.2/13.5）
# ══════════════════════════════════════════════════════════════════════════


def test_known_position_evidence_is_available_to_the_answer():
    """持仓数值必须进入本轮证据集，回答层才能引用「你的成本 1850」。

    否则模型只能凭上下文复述这个数字——又回到编造。
    """
    from api.services.position_context_service import PositionContext

    pos = PositionContext(
        symbol="600519.SH", known=True, source="trade_ledger",
        quantity=300, sellable=200, average_cost=1850.0, as_of="2026-03-02",
        note="来源：交易台账",
    )
    result, _ = _run(
        QueueModel([_grounded_answer]),
        request=_request(text="600519 还能不能拿"),
        position_context=pos,
    )
    assert result.position_known is True
    assert result.status in {"completed", "degraded"}


def test_unknown_position_forces_the_answer_to_disclose_it():
    """持仓未知时必须进入 coverage —— 强制回答声明「未提供持仓」。

    这正是「不假装持仓感知已通」的可执行形式：不是靠文案自觉，
    而是让校验器要求披露。
    """
    from api.services.position_context_service import PositionContext

    pos = PositionContext(symbol="600519.SH", known=False, source="none", note="未提供持仓")
    result, _ = _run(
        QueueModel([_grounded_answer]),
        request=_request(text="600519 还能不能拿"),
        position_context=pos,
    )
    assert result.position_known is False
    assert "position" in result.unavailable_declared


def test_no_position_context_leaves_behaviour_unchanged():
    """未传持仓上下文时行为与之前一致（不引入回归）。"""
    result, _ = _run(QueueModel([_grounded_answer]), request=_request())
    assert result.position_known is None
