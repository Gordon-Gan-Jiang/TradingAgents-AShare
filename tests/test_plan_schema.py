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


def test_confidence_is_clamped_and_uninterpretable_values_rejected():
    plan = parse_plan(_payload(confidence=7), known_tools=KNOWN)
    assert plan is not None and plan.confidence == 1.0
    # 读不懂的值仍然整体拒绝（这是"违规就降级"的原文）。
    assert parse_plan(_payload(confidence=[1, 2]), known_tools=KNOWN) is None
    assert parse_plan(_payload(confidence="大概吧"), known_tools=KNOWN) is None


def test_word_confidence_is_understood_not_fatal():
    """`"high"` 这类**表述型**差异不该毁掉整个计划。

    实测证据：黄金集里「这只票要不要止损」的规划**完全正确**（意图、追问文本都对），
    却因为模型把 confidence 写成 `"high"` 而被整体拒绝 → 回落规则层 → 标的不存在
    → 整轮不可服务。同一轮里 s07 也栽在同一个字上。

    confidence 不改变答案的含义（它只进 `RouteDecision.confidence` 与 trace），
    把它和"未知工具""步数超限"这类**语义**违规同等对待，是拿用户能得到的答案
    去换一个字段的整洁。**不能信息丢失的归一化**与"修一修继续用"是两回事：
    字符串步骤也被保留，未知工具仍然整体拒绝。
    """
    plan = parse_plan(_payload(confidence="high"), known_tools=KNOWN)
    assert plan is not None, "表述型差异不该导致整计划被拒"
    assert plan.confidence == 0.9

    assert parse_plan(_payload(confidence="0.42"), known_tools=KNOWN).confidence == 0.42
    assert parse_plan(_payload(confidence=""), known_tools=KNOWN).confidence == 0.0


def test_bare_string_step_is_understood_not_fatal():
    """`steps: ["watchlist_read", ...]` 也必须能用。

    实测证据（同一轮跑分日志）：模型把步骤写成工具名数组而不是对象数组，
    `parse_plan` 因 `isinstance(raw, Mapping)` 判否而**丢掉整个计划**——
    用户得到的不是"少一个工具"，而是"这轮什么都没查"。归一化成
    `{tool, arguments:{}}` 不丢任何信息：工具名留在计划里，缺参数由
    `_plan_tools` 按 schema 回填（填不满就跳过并留痕）。
    """
    plan = parse_plan(
        _payload(steps=["signal_washout_probe_batch", "get_realtime_quotes"]),
        known_tools=KNOWN,
    )
    assert plan is not None
    assert [s.tool for s in plan.steps] == ["signal_washout_probe_batch", "get_realtime_quotes"]
    assert all(s.arguments == {} for s in plan.steps)

    # 未知工具仍然整体拒绝——归一化不放松语义校验。
    assert parse_plan(_payload(steps=["nope"]), known_tools=KNOWN) is None


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


def test_duplicate_steps_are_deduplicated():
    """同工具同参数的重复步骤必须去掉——重复是纯浪费。

    实测：规划器把 `report_consensus_read` 连排两次（`c01 600519 和 300750 哪个好`）。
    不去重的话一次对话会打两遍同一网络请求，证据也会重复占用引用编号。
    """
    plan = parse_plan(
        _payload(steps=[
            {"tool": "get_realtime_quotes", "arguments": {"symbols": ["600519.SH"]}},
            {"tool": "get_realtime_quotes", "arguments": {"symbols": ["600519.SH"]}},
            {"tool": "signal_washout_probe", "arguments": {"symbol": "600519.SH"}},
        ]),
        known_tools=KNOWN,
    )
    assert plan is not None
    assert [s.tool for s in plan.steps] == ["get_realtime_quotes", "signal_washout_probe"]


def test_same_tool_with_different_arguments_is_kept():
    """同工具不同参数是**合法**的（例如逐只判定），不能被误去重。"""
    plan = parse_plan(
        _payload(steps=[
            {"tool": "signal_washout_probe", "arguments": {"symbol": "600519.SH"}},
            {"tool": "signal_washout_probe", "arguments": {"symbol": "300750.SZ"}},
        ]),
        known_tools=KNOWN,
    )
    assert plan is not None and len(plan.steps) == 2
