"""token / 模型记账真实化（审计 B4 修复）。

## 修复前的实际状态

`tasks.md` 声称「轮次记账：tier / model / 工具名 / token / 延迟落库，可按 tier
归因成本」，实际：

* `ctx.tokens` **从未累加** → `TurnResult.tokens` 恒为 0
* `_persist_message` 不传 `model` → DB 的 `model` 列恒为空字符串
* 没有任何按 tier 归因的查询

于是「成本可归因」是假的——而 14.2 的验收正是引用它作为口径。
"""

from __future__ import annotations

import pytest


def _completion(text: str, in_tokens: int = 0, out_tokens: int = 0):
    from tradingagents.agent.model import Completion

    return Completion(text=text, input_tokens=in_tokens, output_tokens=out_tokens)


# ══════════════════════════════════════════════════════════════════════════
# 模型层：从响应里取 usage
# ══════════════════════════════════════════════════════════════════════════


def test_completion_total_tokens():
    c = _completion("x", 100, 50)
    assert c.total_tokens == 150


def test_model_records_usage_from_completion():
    from tradingagents.agent.model import LlmAnswerModel

    m = LlmAnswerModel(lambda prompt: _completion('{"summary":"x","claims":[]}', 120, 30))
    m.propose(prompt="p", evidence=__import__(
        "tradingagents.grounding.types", fromlist=["EvidenceSet"]
    ).EvidenceSet(), verdict=None)

    assert m.last_usage is not None
    assert m.last_usage.total_tokens == 150


def test_model_accepts_plain_string_completer():
    """兼容旧的 `str` 取数器：拿不到 usage 时**不得编一个**数字。"""
    from tradingagents.agent.model import LlmAnswerModel

    m = LlmAnswerModel(lambda prompt: '{"summary":"x","claims":[]}')
    m.propose(prompt="p", evidence=__import__(
        "tradingagents.grounding.types", fromlist=["EvidenceSet"]
    ).EvidenceSet(), verdict=None)

    assert m.last_usage is not None
    assert m.last_usage.total_tokens == 0  # 未知就是 0，不是估出来的值
    assert m.last_usage.reported is False


def test_usage_accumulates_across_propose_and_repair():
    from tradingagents.agent.model import LlmAnswerModel
    from tradingagents.grounding.types import CheckResult, EvidenceSet, StructuredAnswer

    answers = [
        _completion('{"summary":"x","claims":[]}', 100, 20),
        _completion('{"summary":"y","claims":[]}', 80, 10),
    ]
    m = LlmAnswerModel(lambda prompt: answers.pop(0))
    m.propose(prompt="p", evidence=EvidenceSet(), verdict=None)
    m.repair(
        previous=StructuredAnswer(summary="x"),
        failure=CheckResult.rejected("unsupported"),
        evidence=EvidenceSet(),
        verdict=None,
    )
    assert m.total_tokens == 210


# ══════════════════════════════════════════════════════════════════════════
# 循环层：token 真正累加
# ══════════════════════════════════════════════════════════════════════════


def test_loop_accumulates_tokens_into_the_result():
    """修复前 `ctx.tokens` 从未累加，`result.tokens` 恒为 0。"""
    from tests.test_agent_loop import (
        CollectingSink, FrozenClock, _grounded_answer, _registry, _request,
    )
    from tradingagents.agent.model import Completion
    from tradingagents.agent.loop import run_turn
    from tradingagents.grounding.types import Claim, StructuredAnswer

    class _MeteredModel:
        total_tokens = 0
        last_usage = None

        def propose(self, *, prompt, evidence, verdict, has_tool_text=False):
            self.total_tokens += 137
            self.last_usage = Completion(text="", input_tokens=100, output_tokens=37)
            ref = evidence.items[0].id if evidence.items else None
            claims = (Claim(text="量能出现萎缩", refs=(ref,)),) if ref else ()
            return StructuredAnswer(
                summary="缩量下杀后出现承接。", claims=claims,
                verdict_ref=verdict.id if verdict else None,
                declared_unavailable=tuple(verdict.unavailable) if verdict else (),
            )

        def repair(self, **kw):  # pragma: no cover
            return kw["previous"]

    result = run_turn(
        request=_request(),
        registry=_registry(),
        model=_MeteredModel(),
        sink=CollectingSink(),
        clock=FrozenClock(),
    )
    assert result.tokens == 137
    assert result.model_calls == 1


def test_loop_reports_model_label():
    """模型标识必须可落库——否则「哪个模型更准」无法归因。"""
    from tests.test_agent_loop import CollectingSink, FrozenClock, QueueModel, _grounded_answer, _registry, _request
    from tradingagents.agent.loop import run_turn

    result = run_turn(
        request=_request(),
        registry=_registry(),
        model=QueueModel([_grounded_answer]),
        sink=CollectingSink(),
        clock=FrozenClock(),
        model_label="deepseek-chat",
    )
    assert result.model == "deepseek-chat"


def test_model_label_defaults_to_empty_not_a_guess():
    from tests.test_agent_loop import CollectingSink, FrozenClock, QueueModel, _grounded_answer, _registry, _request
    from tradingagents.agent.loop import run_turn

    result = run_turn(
        request=_request(),
        registry=_registry(),
        model=QueueModel([_grounded_answer]),
        sink=CollectingSink(),
        clock=FrozenClock(),
    )
    assert result.model == ""


# ══════════════════════════════════════════════════════════════════════════
# 落库与归因
# ══════════════════════════════════════════════════════════════════════════


def test_message_persists_model_and_tokens():
    from api.database import ChatConversationDB, ChatMessageDB, get_db_ctx
    from api.services.chat_service import _persist_message
    from tradingagents.agent.repository import new_id

    cid = new_id()
    with get_db_ctx() as db:
        db.add(ChatConversationDB(id=cid, user_id="acct-u"))
        db.commit()

    _persist_message(
        conversation_id=cid, user_id="acct-u", role="assistant", content="答案",
        tier="medium", tokens=321, latency_ms=1200, model="deepseek-chat",
    )

    with get_db_ctx() as db:
        row = db.query(ChatMessageDB).filter(ChatMessageDB.conversation_id == cid).one()
    assert row.tokens == 321
    assert row.model == "deepseek-chat"


def test_cost_by_tier_aggregates_real_accounting():
    """按 tier 归因必须基于真实落库数据，而不是测试替身的计数。"""
    from api.database import ChatConversationDB, ChatMessageDB, get_db_ctx
    from api.services.chat_service import _persist_message, cost_by_tier
    from tradingagents.agent.repository import new_id

    cid = new_id()
    with get_db_ctx() as db:
        db.add(ChatConversationDB(id=cid, user_id="tier-u"))
        db.commit()

    _persist_message(conversation_id=cid, user_id="tier-u", role="assistant",
                     content="a", tier="light", tokens=100, latency_ms=500)
    _persist_message(conversation_id=cid, user_id="tier-u", role="assistant",
                     content="b", tier="medium", tokens=900, latency_ms=6000)
    _persist_message(conversation_id=cid, user_id="tier-u", role="assistant",
                     content="c", tier="medium", tokens=1100, latency_ms=8000)

    report = cost_by_tier(user_id="tier-u")
    assert report["light"]["turns"] == 1
    assert report["light"]["tokens"] == 100
    assert report["medium"]["turns"] == 2
    assert report["medium"]["tokens"] == 2000
    assert report["medium"]["avg_latency_ms"] == 7000


def test_cost_by_tier_is_user_scoped():
    from api.database import ChatConversationDB, ChatMessageDB, get_db_ctx
    from api.services.chat_service import _persist_message, cost_by_tier
    from tradingagents.agent.repository import new_id

    cid_a, cid_b = new_id(), new_id()
    with get_db_ctx() as db:
        db.add(ChatConversationDB(id=cid_a, user_id="scope-a2"))
        db.add(ChatConversationDB(id=cid_b, user_id="scope-b2"))
        db.commit()

    _persist_message(conversation_id=cid_a, user_id="scope-a2", role="assistant",
                     content="a", tier="medium", tokens=500, latency_ms=1000)
    _persist_message(conversation_id=cid_b, user_id="scope-b2", role="assistant",
                     content="b", tier="medium", tokens=9999, latency_ms=1000)

    assert cost_by_tier(user_id="scope-a2")["medium"]["tokens"] == 500
