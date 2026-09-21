"""三档意图路由。

## 为什么这个模块值得单独测试

现状是所有问句都走**同一条**重档链路：「今天主力净流入多少」（1 次数据调用）
与「全面分析一下」（7 分析师 + 2 轮辩论 + 三方风控 + 裁判）成本差约 100 倍。
路由是「体验与成本」的全部杠杆，因此它的每条规则都必须被钉住。

三条最容易被实现错的规则：
1. **仅有可解析标的不足以判为重档**。现状正是「抽到代码就启动完整管道」，
   所以「600519 是不是在洗盘」会被当成重档——这是本变更要修的核心问题。
2. **分类失败必须向下降到中档，绝不上升到重档**。重档成本是 100×，误升级
   不可逆；而等待时间过长比「中等深度但立刻可读」更糟。
3. **无法定档时必须追问，而不是猜**。「帮我看看还能不能拿」缺标的时，
   猜一个标的等于制造一个错误答案。
"""

from __future__ import annotations

import pytest


class _FakeResolver:
    def __init__(self, symbols=(), candidates=()):
        self._symbols = tuple(symbols)
        self._candidates = tuple(candidates)

    def resolve(self, text: str):
        from tradingagents.agent.routing import Resolution

        return Resolution(symbols=self._symbols, candidates=self._candidates)


def _rule(resolver=None):
    from tradingagents.agent.routing import RuleTierClassifier

    return RuleTierClassifier(resolver if resolver is not None else _FakeResolver(symbols=("600519.SH",)))


# ══════════════════════════════════════════════════════════════════════════
# 三档分类
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "text",
    [
        "600519 现在多少钱",
        "今天主力净流入多少",
        "贵州茅台换手率是多少",
        "现在涨幅多少",
    ],
)
def test_data_questions_are_light(text):
    """事实型问句只需一次数据调用，不得启动管道。"""
    d = _rule().classify(text)
    assert d.tier == "light"


@pytest.mark.parametrize(
    "text",
    [
        "600519 是不是在洗盘",
        "帮我看看贵州茅台还能不能拿",
        "这只票该不该止损",
        "300750 是派发还是震仓",
    ],
)
def test_judgement_questions_are_medium(text):
    """研判型问句应由确定性探针 + 一次归纳回答（中档）。"""
    d = _rule().classify(text)
    assert d.tier == "medium"


@pytest.mark.parametrize(
    "text",
    [
        "帮我全面分析一下 600519",
        "出一份贵州茅台的研报",
        "对宁德时代做一次完整的多智能体深度分析",
        "启动深度分析：000001.SZ",
    ],
)
def test_explicit_research_requests_are_heavy(text):
    d = _rule().classify(text)
    assert d.tier == "heavy"


def test_reading_an_existing_report_is_not_heavy():
    """「这份研报说了什么」是**阅读**既有报告，不是生成新报告。

    只看「研报」这个词会把阅读型问句判为重档，白白启动分钟级管道。
    因此重档采用「产物 + 生成动作」的组合判据。
    """
    r = _rule()
    for text in ("这份研报说了什么", "昨天的报告里结论是什么"):
        assert r.classify(text).tier != "heavy", text


def test_resolvable_symbol_alone_does_not_imply_heavy():
    """**核心回归**：现状是「抽到代码就启动完整管道」。

    若这条不成立，「600519 是不是在洗盘」会付出 100 倍成本与分钟级等待，
    本变更的全部价值归零。
    """
    d = _rule().classify("600519 是不是在洗盘")
    assert d.symbol == "600519.SH"
    assert d.tier != "heavy"


def test_light_tier_has_single_call_budget():
    from tradingagents.agent.routing import TIER_BUDGETS

    assert TIER_BUDGETS["light"].max_model_calls == 1
    assert TIER_BUDGETS["light"].target_latency_ms <= 5000
    assert TIER_BUDGETS["medium"].target_latency_ms <= 30000
    assert TIER_BUDGETS["medium"].max_tool_calls <= 5


# ══════════════════════════════════════════════════════════════════════════
# 向下降级：绝不升级
# ══════════════════════════════════════════════════════════════════════════


def _decider(inner, **kw):
    from tradingagents.agent.routing import DegradingClassifier

    return DegradingClassifier(inner, **kw)


def test_classifier_error_degrades_to_medium_not_heavy():
    from tradingagents.agent.routing import TIER_MEDIUM

    class _Boom:
        def classify(self, text):
            raise RuntimeError("classifier unavailable")

    d = _decider(_Boom()).classify("随便问点什么")
    assert d.tier == TIER_MEDIUM
    assert d.degraded_from is None or d.degraded_from != "heavy"
    assert "error" in d.reason or "unavailable" in d.reason


def test_low_confidence_heavy_degrades_to_medium():
    """低置信的重档必须降级——重档成本是 100×，误升级不可逆。"""
    from tradingagents.agent.routing import RouteDecision

    class _Shaky:
        def classify(self, text):
            return RouteDecision(tier="heavy", confidence=0.2, reason="guess")

    d = _decider(_Shaky(), min_confidence=0.6).classify("x")
    assert d.tier == "medium"
    assert d.degraded_from == "heavy"


def test_low_confidence_medium_degrades_to_light():
    from tradingagents.agent.routing import RouteDecision

    class _Shaky:
        def classify(self, text):
            return RouteDecision(tier="medium", confidence=0.1, reason="guess")

    d = _decider(_Shaky(), min_confidence=0.6).classify("x")
    assert d.tier == "light"
    assert d.degraded_from == "medium"


def test_light_tier_cannot_degrade_further():
    from tradingagents.agent.routing import RouteDecision

    class _Shaky:
        def classify(self, text):
            return RouteDecision(tier="light", confidence=0.0, reason="guess")

    d = _decider(_Shaky(), min_confidence=0.6).classify("x")
    assert d.tier == "light"


def test_degradation_never_escalates():
    """无论内部返回什么，降级器都不得把档位**抬高**。"""
    from tradingagents.agent.routing import RouteDecision

    class _Light:
        def classify(self, text):
            return RouteDecision(tier="light", confidence=0.99, reason="sure")

    d = _decider(_Light()).classify("x")
    assert d.tier == "light"


def test_confident_classification_is_preserved():
    from tradingagents.agent.routing import RouteDecision

    class _Sure:
        def classify(self, text):
            return RouteDecision(tier="heavy", confidence=0.9, reason="explicit request")

    d = _decider(_Sure(), min_confidence=0.6).classify("x")
    assert d.tier == "heavy"
    assert d.degraded_from is None


# ══════════════════════════════════════════════════════════════════════════
# 澄清先于假设
# ══════════════════════════════════════════════════════════════════════════


def test_unresolvable_judgement_request_asks_for_clarification():
    """无法解析标的时必须追问，而不是猜一个。

    猜标的是「制造一个错误答案」，比拒绝回答糟得多。
    """
    from tradingagents.agent.routing import RuleTierClassifier

    d = RuleTierClassifier(_FakeResolver()).classify("帮我看看还能不能拿")
    assert d.needs_clarification is True
    assert d.symbol is None


def test_multiple_candidates_ask_for_selection():
    from tradingagents.agent.routing import RuleTierClassifier

    d = RuleTierClassifier(_FakeResolver(candidates=("600519.SH", "600809.SH"))).classify("分析一下白酒")
    assert d.needs_clarification is True
    assert d.candidates == ("600519.SH", "600809.SH")


def test_light_tier_also_requires_a_subject():
    from tradingagents.agent.routing import RuleTierClassifier

    d = RuleTierClassifier(_FakeResolver()).classify("现在多少钱")
    assert d.needs_clarification is True


def test_resolved_symbol_is_carried_on_the_decision():
    d = _rule().classify("600519 是不是在洗盘")
    assert d.symbol == "600519.SH"
    assert d.needs_clarification is False


# ══════════════════════════════════════════════════════════════════════════
# 决策可观测
# ══════════════════════════════════════════════════════════════════════════


def test_decision_records_tier_confidence_symbol_and_reason():
    """路由记录必须能回答「为什么这一问走了这一档」。"""
    d = _rule().classify("600519 是不是在洗盘")

    assert d.tier in {"light", "medium", "heavy"}
    assert 0.0 <= d.confidence <= 1.0
    assert d.symbol == "600519.SH"
    assert d.reason
    assert d.classifier


def test_decision_is_immutable():
    import dataclasses

    d = _rule().classify("600519 是不是在洗盘")
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.tier = "heavy"  # type: ignore[misc]


def test_tier_distribution_is_computable():
    """档位分布可统计 —— 这是发现「重档失控」的唯一手段。"""
    from collections import Counter

    from tradingagents.agent.routing import RuleTierClassifier

    r = RuleTierClassifier(_FakeResolver(symbols=("600519.SH",)))
    texts = [
        "600519 现在多少钱",
        "600519 是不是在洗盘",
        "帮我全面分析 600519",
        "600519 换手率多少",
    ]
    dist = Counter(r.classify(t).tier for t in texts)
    assert dist["light"] == 2
    assert dist["heavy"] == 1
    assert sum(dist.values()) == len(texts)


def test_heavy_tier_requires_explicit_language_not_inference():
    """重档只能由**用户明示**触发，不得由模型推断「他大概想要一份研报」。"""
    from tradingagents.agent.routing import RuleTierClassifier

    r = RuleTierClassifier(_FakeResolver(symbols=("600519.SH",)))
    for text in ("600519 怎么看", "600519 能买吗", "600519 最近怎么样"):
        assert r.classify(text).tier != "heavy", text
