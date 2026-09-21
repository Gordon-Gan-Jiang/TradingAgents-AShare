"""计划（Plan）的结构与 schema 校验。

## 为什么计划必须可校验

规划层由 LLM 产出，而 LLM 会给出形状不对的东西（未知工具、超步数、追问却没有
问题文本、置信度不在区间内）。**任何违规都必须整体拒绝并降级**，绝不能"修一修
继续用"：计划的每一步都改变答案的含义，悄悄丢掉一个未知工具步骤会让回答看起来
"查过了"，实际少了一大块证据。

同时，计划必须能**落库与重放**——否则同一句话两次运行走不同路径，校准与回测
就失去意义（这是本平台与"随手写个 agent"的分界）。
"""

from __future__ import annotations

import pytest

from tradingagents.agent.routing import (
    MAX_PLAN_STEPS,
    PLAN_INTENTS,
    Plan,
    PlanStep,
    parse_plan,
)

KNOWN = ("signal_washout_probe", "signal_washout_probe_batch", "market_screen",
         "get_realtime_quotes", "data_symbol_news")


def _payload(**kw):
    base = {
        "intent": "single_symbol_judgement",
        "tier": "medium",
        "symbol": "600519.SH",
        "steps": [{"tool": "signal_washout_probe", "arguments": {"symbol": "600519.SH"},
                   "reason": "judgement"}],
        "confidence": 0.9,
        "source": "llm",
    }
    base.update(kw)
    return base


def test_valid_plan_round_trips():
    plan = parse_plan(_payload(), known_tools=KNOWN)
    assert isinstance(plan, Plan)
    assert plan.intent == "single_symbol_judgement"
    assert plan.tier == "medium"
    assert plan.symbol == "600519.SH"
    assert [step.tool for step in plan.steps] == ["signal_washout_probe"]
    assert plan.steps[0].arguments["symbol"] == "600519.SH"


def test_plan_records_are_flat_and_json_friendly():
    """落库记录必须扁平且可序列化——计划要进 trace、要能重放。"""
    import json

    plan = parse_plan(_payload(), known_tools=KNOWN)
    record = plan.as_record()
    json.dumps(record, ensure_ascii=False)  # 不抛错即可序列化
    assert record["steps"][0]["tool"] == "signal_washout_probe"
    assert record["intent"] == "single_symbol_judgement"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"intent": "bogus_intent", "tier": "medium"},
        _payload(tier="ultra"),
        _payload(steps=[{"tool": "nonexistent_tool", "arguments": {}}]),
        _payload(steps=[{"tool": "signal_washout_probe", "arguments": "not-a-mapping"}]),
        _payload(steps=[{"arguments": {}}]),
        _payload(steps=[] ),  # 合法：概念问答不需要工具
    ],
)
def test_invalid_payloads_are_rejected_or_accepted_as_declared(payload):
    plan = parse_plan(payload, known_tools=KNOWN)
    if payload is None or payload == {} or payload.get("intent") not in PLAN_INTENTS:
        assert plan is None
    elif payload.get("steps") == []:
        assert plan is not None and plan.steps == (), "概念问答可以没有步骤"
    else:
        assert plan is None, f"违规计划必须被整体拒绝：{payload}"


def test_unknown_tool_rejects_the_whole_plan():
    """未知工具必须整体拒绝——静默跳过会让回答看起来"查过了"。"""
    plan = parse_plan(
        _payload(steps=[
            {"tool": "signal_washout_probe", "arguments": {}},
            {"tool": "tool_that_does_not_exist", "arguments": {}},
        ]),
        known_tools=KNOWN,
    )
    assert plan is None


def test_too_many_steps_rejected():
    steps = [{"tool": "get_realtime_quotes", "arguments": {}}] * (MAX_PLAN_STEPS + 1)
    assert parse_plan(_payload(steps=steps), known_tools=KNOWN) is None


def test_clarify_requires_a_question():
    """追问却没有问题文本 = 用户看到空白。"""
    assert parse_plan(_payload(need_clarify=True, clarify_question=""), known_tools=KNOWN) is None
    ok = parse_plan(
        _payload(need_clarify=True, clarify_question="你想问哪一只？"), known_tools=KNOWN
    )
    assert ok is not None and ok.need_clarify is True


def test_confidence_is_clamped_and_bad_values_rejected():
    plan = parse_plan(_payload(confidence=7), known_tools=KNOWN)
    assert plan is not None and plan.confidence == 1.0
    assert parse_plan(_payload(confidence="high"), known_tools=KNOWN) is None


def test_symbols_are_deduplicated_and_trimmed():
    plan = parse_plan(
        _payload(symbol=None, symbols=["600519.SH", " 600519.SH ", "300750.SZ"]),
        known_tools=KNOWN,
    )
    assert plan is not None
    assert plan.symbols == ("600519.SH", "300750.SZ")


def test_source_must_be_known():
    assert parse_plan(_payload(source="magic"), known_tools=KNOWN) is None
    assert parse_plan(_payload(source="rules"), known_tools=KNOWN).source == "rules"


def test_intent_vocabulary_matches_the_golden_set():
    """意图枚举必须与黄金集一致，否则规划准确率无从度量。"""
    from tests.golden.harness import INTENTS

    assert set(PLAN_INTENTS) == set(INTENTS)
