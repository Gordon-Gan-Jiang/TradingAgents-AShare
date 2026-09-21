from unittest.mock import MagicMock

import pytest

from tradingagents.graph.intent_parser import (
    _FAST_AGENT_TYPES,
    _SLOW_AGENT_TYPES,
    build_horizon_context,
    parse_intent,
    variable_speed,
)


def test_parse_intent_returns_defaults():
    """`horizons` is fixed to a single short run by design.

    Commit b40824b ("fix(intent): 默认只跑短线视角") deliberately replaced the old
    ``["short", "medium"]`` default with ``["short"]``: the graph now runs once and
    each analyst keeps its own natural window, so a second horizon no longer means
    a second pass over the graph. The old expectation outlived the change.
    """
    mock_llm = MagicMock()
    mock_llm.invoke.return_value = MagicMock(
        content='{"ticker": "600519", "horizons": ["short", "medium"], "focus_areas": [], "specific_questions": []}'
    )
    result = parse_intent("分析600519", mock_llm)
    assert result["ticker"] == "600519"
    assert result["horizons"] == ["short"], "LLM 请求的双周期不应再被采纳为运行计划"
    assert result["focus_areas"] == []


def test_parse_intent_fallback_on_invalid_json():
    mock_llm = MagicMock()
    mock_llm.invoke.return_value = MagicMock(content="这不是JSON")
    result = parse_intent("600519", mock_llm, fallback_ticker="600519")
    assert result["ticker"] == "600519"
    assert result["horizons"] == ["short"]
    assert result["focus_areas"] == []


def test_build_horizon_context_short_contains_label():
    ctx = build_horizon_context("short", ["量价关系"], ["能否突破"])
    assert "短线" in ctx
    assert "量价关系" in ctx
    assert "能否突破" in ctx


def test_build_horizon_context_medium_has_label():
    ctx = build_horizon_context("medium", [], [], agent_type="fundamentals")
    assert "中线" in ctx


def test_build_horizon_context_short_fundamentals_has_downweight_hint():
    ctx = build_horizon_context("short", [], [], agent_type="fundamentals")
    assert "次要" in ctx


# ---------------------------------------------------------------------------
# P6 / F2: the information set must match the horizon
# ---------------------------------------------------------------------------

# The exact strings produced by `grep -rn "agent_type=" tradingagents/agents/`.
# If a caller renames its dimension, these tests must be updated *with* it —
# a classification list that no longer matches production fails silently.
REAL_ANALYST_TYPES = [
    "market",
    "volume_price",
    "smart_money",
    "social",
    "news",
    "macro",
    "fundamentals",
]
REAL_RESEARCHER_TYPES = ["bull", "bear"]


class TestVariableSpeedClassification:
    def test_every_real_analyst_dimension_is_classified(self):
        """A dimension the code never classifies is one that silently escapes layering."""
        unclassified = [a for a in REAL_ANALYST_TYPES if variable_speed(a) is None]
        assert unclassified == [], (
            f"这些真实存在的分析维度没有被分类，分层对它们不生效: {unclassified}"
        )

    def test_fast_dimensions(self):
        for agent in ["market", "volume_price", "smart_money", "social", "news"]:
            assert variable_speed(agent) == "fast", agent

    def test_slow_dimensions(self):
        for agent in ["fundamentals", "macro"]:
            assert variable_speed(agent) == "slow", agent

    def test_debate_roles_are_deliberately_unclassified(self):
        """bull/bear read every analyst, so they carry no fast/slow label themselves."""
        for agent in REAL_RESEARCHER_TYPES:
            assert variable_speed(agent) is None, agent

    def test_unknown_dimension_is_not_guessed(self):
        assert variable_speed("something_new") is None
        assert variable_speed("") is None
        assert variable_speed(None) is None

    def test_classification_is_case_and_space_insensitive(self):
        assert variable_speed("  MARKET ") == "fast"
        assert variable_speed("Fundamentals") == "slow"

    def test_the_two_sets_do_not_overlap(self):
        assert _FAST_AGENT_TYPES & _SLOW_AGENT_TYPES == set()


class TestHorizonWeightHint:
    """Slow variables must not drive the next-day call, and vice versa for medium."""

    def test_slow_dimension_is_secondary_for_the_next_day(self):
        ctx = build_horizon_context("short", [], [], agent_type="fundamentals")
        assert "【本维度在次日研判中的定位：次要】" in ctx
        assert "不能" in ctx and "方向" in ctx

    def test_fast_dimension_is_primary_for_the_next_day(self):
        for agent in ["market", "volume_price", "smart_money", "news"]:
            ctx = build_horizon_context("short", [], [], agent_type=agent)
            assert "【本维度在次日研判中的定位：主要】" in ctx, agent

    def test_slow_dimension_is_primary_for_the_medium_term(self):
        ctx = build_horizon_context("medium", [], [], agent_type="fundamentals")
        assert "【本维度在中线研判中的定位：主要】" in ctx

    def test_fast_dimension_is_only_supporting_for_the_medium_term(self):
        ctx = build_horizon_context("medium", [], [], agent_type="market")
        assert "【本维度在中线研判中的定位：辅助】" in ctx

    def test_every_real_dimension_gets_a_hint_on_the_short_horizon(self):
        for agent in REAL_ANALYST_TYPES:
            ctx = build_horizon_context("short", [], [], agent_type=agent)
            assert "【本维度" in ctx, f"{agent} 在短线口径下没有拿到权重提示"

    def test_unclassified_dimension_gets_no_hint_rather_than_a_wrong_one(self):
        ctx = build_horizon_context("short", [], [], agent_type="bull")
        assert "【本维度" not in ctx

    def test_missing_agent_type_keeps_the_old_behaviour(self):
        """Back-compat: no agent_type means no weight claim at all."""
        ctx = build_horizon_context("short", ["量价关系"], ["能否突破"])
        assert "【本维度" not in ctx
        assert "短线" in ctx and "量价关系" in ctx

    def test_focus_and_questions_survive_the_hint(self):
        ctx = build_horizon_context("short", ["资金流向"], ["是否放量"], agent_type="smart_money")
        assert "资金流向" in ctx
        assert "是否放量" in ctx
        assert "【本维度" in ctx

    @pytest.mark.parametrize("agent", ["fundamentals", "macro"])
    def test_slow_hint_forbids_using_it_for_direction(self, agent):
        """The hint must be a prohibition, not a soft suggestion."""
        ctx = build_horizon_context("short", [], [], agent_type=agent)
        assert "不能" in ctx, "慢变量提示必须是禁止性的"
        assert "风险" in ctx, "慢变量应被允许进入风险/背景"

    def test_default_horizon_treated_as_short(self):
        """An unexpected horizon string must not silently grant slow variables direction."""
        ctx = build_horizon_context("weird", [], [], agent_type="macro")
        assert "【本维度在次日研判中的定位：次要】" in ctx

    def test_english_catalogue_has_the_same_four_hints(self):
        """The layering must not exist only in Chinese."""
        from tradingagents.prompts.en import PROMPTS as EN
        from tradingagents.prompts.zh import PROMPTS as ZH

        for key in (
            "weight_hint_slow_short",
            "weight_hint_fast_short",
            "weight_hint_slow_medium",
            "weight_hint_fast_medium",
        ):
            assert key in ZH, f"中文缺 {key}"
            assert key in EN, f"英文缺 {key}"
            assert "{weight_hint}" not in ZH[key]
            assert "{weight_hint}" not in EN[key]


class TestResearchManagerInformationSetLayering:
    """P6/F2: the manager is where direction is decided, so the rule must live there.

    The analyst hint alone is not enforcement: the manager blends every analyst and
    can still promote a fundamentals view into a next-day direction. The prompt used
    to say only "不同维度的分析师权重不同…综合判断权重", i.e. it delegated the
    layering to the model's judgement with no prohibition.
    """

    def _prompt(self, lang):
        if lang == "zh":
            from tradingagents.prompts.zh import PROMPTS
        else:
            from tradingagents.prompts.en import PROMPTS
        return PROMPTS["research_manager_prompt"]

    @pytest.mark.parametrize("lang", ["zh", "en"])
    def test_layering_rule_is_present(self, lang):
        p = self._prompt(lang)
        assert ("信息集分层" in p) or ("Information-set layering" in p)

    @pytest.mark.parametrize("lang", ["zh", "en"])
    def test_slow_variables_are_named(self, lang):
        p = self._prompt(lang)
        for slow in ("基本面", "宏观") if lang == "zh" else ("fundamentals", "macro"):
            assert slow in p, f"{lang} 未点名慢变量 {slow}"

    @pytest.mark.parametrize("lang", ["zh", "en"])
    def test_fast_variables_are_named(self, lang):
        p = self._prompt(lang)
        for fast in (("主力资金", "价")) if lang == "zh" else ("smart money", "sentiment"):
            assert fast in p, f"{lang} 未点名快变量 {fast}"

    @pytest.mark.parametrize("lang", ["zh", "en"])
    def test_insufficient_fast_evidence_must_abstain(self, lang):
        """The rule must route the model to NEUTRAL, not to a fundamentals-driven call."""
        p = self._prompt(lang)
        if lang == "zh":
            assert "必须" in p and "中性" in p
        else:
            assert "NEUTRAL" in p

    @pytest.mark.parametrize("lang", ["zh", "en"])
    def test_verdict_machine_block_still_formats(self, lang):
        """A stray brace in the new text would break `.format()` at call time."""
        p = self._prompt(lang)
        assert '{{"direction"' in p
        if lang == "zh":
            out = p.format(
                analyst_structured_brief="a", smart_money_report="b",
                volume_price_report="c", sentiment_report="d", history="e",
                claims_text="f", unresolved_claims_text="g", round_summary="h",
            )
            assert "信息集分层" in out
            assert '"direction"' in out

    def test_zh_prompt_forbids_going_bullish_on_good_fundamentals(self):
        p = self._prompt("zh")
        assert "不得因为基本面好看就给出看多" in p
