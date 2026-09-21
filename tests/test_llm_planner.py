"""LLM 规划层：规则定不了标的时才用，且必须能确定性降级。

## 定位

规则层能判定的高频问句（带代码/公司名）走**零 LLM** 路径——那里工具是机械决定的，
付规划成本是纯浪费。规则层**判不出标的**的场景（"有什么能买的""最近主线是什么"）
才是 LLM 的用武之地，也正是旧实现直接 400 追问的地方。

## 本文件锁住

1. **规则能定 → 一次 LLM 都不调**（用计数替身断言，不靠"我觉得"）。
2. LLM 输出非法/超时/抛错 → **降级到规则结果**，绝不中断回合。
3. 计划被接受时，档位/标的/是否追问都来自计划（而不是被规则覆盖）。
4. 计划可以经 `DegradingClassifier` 包裹——降级语义只有一处实现。
"""

from __future__ import annotations

import pytest

from tradingagents.agent.routing import (
    DegradingClassifier,
    LlmPlanner,
    PlanError,
    RouteDecision,
    RuleTierClassifier,
    Resolution,
    TIER_LIGHT,
    TIER_MEDIUM,
)

KNOWN = ("signal_washout_probe", "market_screen", "get_realtime_quotes", "data_symbol_news")


class _Resolver:
    """按预设结果解析标的（替身）。"""

    def __init__(self, symbols=(), candidates=()):
        self._resolution = Resolution(symbols=tuple(symbols), candidates=tuple(candidates))

    def resolve(self, text: str):
        return self._resolution


class _Counter:
    """记录被调用次数与最后一次提示词的完成器替身。"""

    def __init__(self, reply: str = "", *, error: Exception | None = None):
        self.calls: list[str] = []
        self.reply = reply
        self.error = error

    def __call__(self, prompt: str) -> str:
        self.calls.append(prompt)
        if self.error is not None:
            raise self.error
        return self.reply


def _planner(*, resolver, complete):
    return LlmPlanner(
        inner=RuleTierClassifier(resolver),
        complete=complete,
        known_tools=KNOWN,
        tool_digest="signal_washout_probe：个股洗盘判定\nmarket_screen：全市场筛选",
    )


PLAN_JSON = (
    '{"intent": "market_view", "tier": "medium", "symbol": null, "symbols": [],'
    ' "screen": false, "market_level": true, "steps": [], "need_clarify": false,'
    ' "clarify_question": "", "confidence": 0.8}'
)


def test_rules_decisive_means_zero_llm_calls():
    """高频问句（已解析出标的）不得付规划成本。"""
    complete = _Counter(reply=PLAN_JSON)
    planner = _planner(resolver=_Resolver(symbols=("600519.SH",)), complete=complete)

    decision = planner.classify("600519 是不是在洗盘")

    assert complete.calls == [], f"规则已能判定却调用了 LLM：{complete.calls}"
    assert decision.symbol == "600519.SH"
    assert decision.classifier == "rule"


def test_unresolved_text_triggers_llm_planning():
    """规则定不了标的时才交给 LLM——这正是"任意自然语言"的入口。"""
    complete = _Counter(reply=PLAN_JSON)
    planner = _planner(resolver=_Resolver(), complete=complete)

    decision = planner.classify("最近哪个板块最强")

    assert len(complete.calls) == 1, "该交给 LLM 的问句没有触发规划"
    assert decision.tier == TIER_MEDIUM
    assert decision.needs_clarification is False, "计划已表态：市场级问题不需要追问"
    assert decision.classifier == "llm"


def test_plan_carries_tier_and_symbols():
    plan_json = (
        '{"intent": "multi_symbol_compare", "tier": "medium", "symbol": null,'
        ' "symbols": ["600519.SH", "300750.SZ"], "steps":'
        ' [{"tool": "signal_washout_probe", "arguments": {"symbol": "600519.SH"}}],'
        ' "need_clarify": false, "clarify_question": "", "confidence": 0.7}'
    )
    planner = _planner(resolver=_Resolver(), complete=_Counter(reply=plan_json))
    decision = planner.classify("这两只哪个好")

    assert decision.symbols == ("600519.SH", "300750.SZ")
    assert decision.tier == TIER_MEDIUM


def test_invalid_json_degrades_to_rules():
    """非法输出不得中断回合：降级到规则结果。"""
    planner = _planner(resolver=_Resolver(symbols=("600519.SH",)), complete=_Counter(reply="不是 JSON"))
    # 规则已能判定 → 根本不会问 LLM；换成定不了的输入
    planner2 = _planner(resolver=_Resolver(), complete=_Counter(reply="不是 JSON"))

    decision = planner2.classify("随便说说")
    assert decision is not None
    assert decision.needs_clarification is True, "拿不到计划时应退回追问"


def test_unknown_tool_in_plan_degrades_not_crashes():
    bad = PLAN_JSON.replace('"steps": []', '"steps": [{"tool": "ghost_tool", "arguments": {}}]')
    planner = _planner(resolver=_Resolver(), complete=_Counter(reply=bad))
    decision = planner.classify("最近哪个板块最强")
    assert decision.classifier == "rule", "计划含未知工具 → 整体拒绝并降级到规则"


def test_llm_exception_raises_plan_error_for_the_caller_to_degrade():
    planner = _planner(
        resolver=_Resolver(), complete=_Counter(error=TimeoutError("slow"))
    )
    with pytest.raises(PlanError):
        planner.plan("最近哪个板块最强")


def test_planner_works_inside_degrading_classifier():
    """计划层可被 `DegradingClassifier` 包裹，且**不会把异常抛出去**。

    降级有**两层**，都要成立：
    * 规划器内部：LLM 抛错 → 回落规则结果（多数情况在这一层就被吃掉）；
    * 外层 `DegradingClassifier`：兜住任何仍逃出来的异常。
    这条用"LLM 必然抛错"的替身同时压这两层——不能崩、要给出可服务的决策。
    """
    planner = _planner(resolver=_Resolver(), complete=_Counter(error=RuntimeError("boom")))
    wrapped = DegradingClassifier(planner)

    decision = wrapped.classify("最近哪个板块最强")

    assert isinstance(decision, RouteDecision)
    assert decision.tier in (TIER_LIGHT, TIER_MEDIUM)
    assert decision.needs_clarification is True, "拿不到计划时要退回追问，而不是编一个落点"


def test_prompt_lists_tools_and_demands_json():
    complete = _Counter(reply=PLAN_JSON)
    planner = _planner(resolver=_Resolver(), complete=complete)
    prompt = planner.build_prompt("最近哪个板块最强")

    assert "signal_washout_probe" in prompt, "工具摘要必须进提示词，否则模型无从选工具"
    assert "JSON" in prompt
    assert "最近哪个板块最强" in prompt


def test_code_fences_are_tolerated():
    """模型很爱加 ```json 围栏——容忍它，不因此降级。"""
    planner = _planner(resolver=_Resolver(), complete=_Counter(reply=f"```json\n{PLAN_JSON}\n```"))
    decision = planner.classify("最近哪个板块最强")
    assert decision.classifier == "llm"
