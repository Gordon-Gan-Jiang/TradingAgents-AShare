"""A report whose direction lives in ANALYST_JSON must not be reported as unparsable.

## What went wrong

Every analyst prompt mandated **two** machine-readable blocks in the same position
at the end of the report: ``VERDICT`` (from the system prompt) and ``ANALYST_JSON``
(appended to the human message, so read last). The later, more specific instruction
won, and the model emitted only ``ANALYST_JSON``.

``consensus_service`` read only ``VERDICT``. So on the real corpus:

* 262 analyst traces → **114 (43.5%)** had ``verdict_parsed=False``;
* of those, **103 (90.4%)** carried a perfectly good ``structured.direction``;
* since 09-18: of 220 reports, 96 had **only** ANALYST_JSON and **zero** had neither block.

The user-visible result was a red badge reading "5 份报告解析失败，未计入共识" on a
card whose own report was fine — and, worse, a **consensus strength computed from a
denominator nobody could see**. Report ``12096d10f2c64693bab52d9145b317b9``
(000993.SZ) displayed "看多 强度 100/100" while only 2 of 7 analysts were counted.

## What these tests pin

1. ``resolve_analyst_direction`` is the single precedence rule
   (VERDICT → ANALYST_JSON → none), shared by the consensus layer, the analyst
   trace builders and the research manager — which previously disagreed.
2. A trace is excluded from the vote only when **no** block stated a direction,
   and that is still fail-closed: a genuinely neutral opinion must not be
   manufactured out of a missing one.
3. The consensus and the research manager must never report different directions
   for the same stored traces.
"""

import pytest

from api.services import consensus_service as cs
from api.services.consensus_service import build_consensus_summary
from tradingagents.agents.utils.analyst_structured import (
    build_structured_brief_for_research_manager,
    resolve_analyst_direction,
    resolve_trace_direction,
)


def _trace(agent, verdict, confidence, *, parsed=None, structured=None, horizon="short"):
    """One stored analyst trace, with only the keys the analyst nodes actually write."""
    t = {
        "agent": agent,
        "horizon": horizon,
        "verdict": verdict,
        "confidence": confidence,
        "key_finding": f"{agent} 结论：{verdict}",
    }
    if parsed is not None:
        t["verdict_parsed"] = parsed
    if structured is not None:
        t["structured"] = structured
    return t


def _summary(traces, direction="看多"):
    return build_consensus_summary(
        result_data={"analyst_traces": traces, "risk_feedback_state": {}},
        final_direction=direction,
        final_confidence=68,
    )


# ---------------------------------------------------------------------------
# the precedence rule itself
# ---------------------------------------------------------------------------

class TestResolveAnalystDirection:
    def test_verdict_block_wins(self):
        got = resolve_analyst_direction(
            verdict="看多", confidence=70, verdict_parsed=True,
            structured={"direction": "看空", "confidence": 90},
        )
        assert (got.direction, got.source) == ("看多", "verdict")

    def test_analyst_json_is_used_when_verdict_did_not_parse(self):
        """The exact shape of the 114 broken reports: good direction, no VERDICT."""
        got = resolve_analyst_direction(
            verdict="中性", confidence=48, verdict_parsed=False,
            structured={"direction": "偏多", "confidence": 70},
        )
        assert (got.direction, got.source, got.parsed) == ("偏多", "analyst_json", True)
        assert got.confidence == 70, "confidence must come from the block that held the direction"

    def test_legacy_trace_without_the_flag_is_still_counted(self):
        """Traces written before verdict_parsed existed must not be dropped."""
        got = resolve_trace_direction({"agent": "market_analyst", "verdict": "看多", "confidence": 70})
        assert (got.direction, got.source) == ("看多", "verdict")

    def test_no_direction_anywhere_is_reported_as_none(self):
        """Fail-closed: if neither block stated a direction there is no opinion."""
        got = resolve_analyst_direction(
            verdict="中性", confidence=48, verdict_parsed=False, structured=None,
        )
        assert got.direction is None and got.source == "none" and not got.parsed

    def test_unrecognizable_verdict_falls_through_to_analyst_json(self):
        got = resolve_analyst_direction(
            verdict="??", verdict_parsed=True, structured={"direction": "BEARISH", "confidence": 55},
        )
        assert (got.direction, got.source) == ("看空", "analyst_json")

    @pytest.mark.parametrize(
        "raw,expected",
        [("偏多", "偏多"), ("BULLISH", "看多"), ("LEAN_BEARISH", "偏空"), ("NEUTRAL", "中性")],
    )
    def test_both_direction_vocabularies_normalize(self, raw, expected):
        assert resolve_analyst_direction(verdict=raw, verdict_parsed=True).direction == expected

    def test_garbage_confidence_is_not_invented(self):
        got = resolve_analyst_direction(verdict="看多", confidence=None, verdict_parsed=True)
        assert got.confidence is None

    def test_non_mapping_input_never_raises(self):
        for bad in (None, "看多", 7, ["看多"]):
            assert resolve_trace_direction(bad).direction is None


# ---------------------------------------------------------------------------
# the consensus layer: recovered votes count, silent reports still do not
# ---------------------------------------------------------------------------

class TestConsensusRecoversAnalystJson:
    def test_analyst_json_only_report_is_counted_not_reported_as_unparsed(self):
        """The user-visible bug: a good report must not show up as 解析失败."""
        summary = _summary([
            _trace("market_analyst", "中性", 48, parsed=False,
                   structured={"direction": "偏多", "confidence": 72}),
        ])
        assert summary["unparsed_analyst_n"] == 0
        assert summary["recovered_analyst_n"] == 1
        assert summary["agent_breakdown"][0]["verdict"] == "偏多"
        assert summary["agent_breakdown"][0]["verdict_source"] == "analyst_json"

    def test_recovered_neutral_vote_dilutes_strength_honestly(self):
        """Recovered votes are real votes: a recovered 中性 must lower strength.

        This is the flip side of the bug. Before the fix the analyst vanished and
        the remaining 看多 votes reported full strength; the honest number is lower.
        000993.SZ showed "看多 强度 100/100" with 5 of 7 analysts silently dropped.
        """
        two_bulls = _summary([
            _trace("market_analyst", "看多", 80, parsed=True),
            _trace("social_media_analyst", "看多", 80, parsed=True),
        ])
        plus_recovered_neutral = _summary([
            _trace("market_analyst", "看多", 80, parsed=True),
            _trace("social_media_analyst", "看多", 80, parsed=True),
            _trace("news_analyst", "中性", 48, parsed=False,
                   structured={"direction": "中性", "confidence": 60}),
        ])
        assert two_bulls["consensus_strength"] > plus_recovered_neutral["consensus_strength"]
        assert plus_recovered_neutral["neutral_ratio"] > 0

    def test_a_report_with_no_direction_in_either_block_is_still_excluded(self):
        """Fail-closed must survive the fix: recovery may only surface stated opinions."""
        alone = _summary([_trace("market_analyst", "看多", 80, parsed=True)])
        with_silent = _summary([
            _trace("market_analyst", "看多", 80, parsed=True),
            _trace("news_analyst", "中性", 48, parsed=False),
        ])
        assert with_silent["unparsed_analyst_n"] == 1
        assert with_silent["recovered_analyst_n"] == 0
        assert with_silent["consensus_direction"] == alone["consensus_direction"]
        assert with_silent["consensus_strength"] == alone["consensus_strength"], \
            "a report with no stated direction must not dilute the vote"

    def test_a_genuine_neutral_verdict_still_counts_as_a_neutral_vote(self):
        with_neutral = _summary([
            _trace("market_analyst", "看多", 80, parsed=True),
            _trace("news_analyst", "中性", 60, parsed=True),
        ])
        assert with_neutral["unparsed_analyst_n"] == 0
        assert with_neutral["recovered_analyst_n"] == 0
        assert with_neutral["neutral_ratio"] > 0

    def test_recovered_and_unparsed_are_disjoint_and_cover_every_trace(self):
        traces = [
            _trace("market_analyst", "看多", 80, parsed=True),
            _trace("news_analyst", "中性", 48, parsed=False,
                   structured={"direction": "偏空", "confidence": 65}),
            _trace("macro_analyst", "中性", 48, parsed=False),
        ]
        summary = _summary(traces)
        assert summary["recovered_analyst_n"] == 1
        assert summary["unparsed_analyst_n"] == 1
        assert len(summary["agent_breakdown"]) == 2

    def test_confidence_shown_comes_from_the_block_that_held_the_direction(self):
        """Never display the 48 the parser synthesised for a missing VERDICT block."""
        summary = _summary([
            _trace("market_analyst", "中性", 48, parsed=False,
                   structured={"direction": "偏多", "confidence": 77}),
        ])
        assert summary["agent_breakdown"][0]["confidence"] == 77

    def test_missing_analyst_json_confidence_is_not_invented(self):
        summary = _summary([
            _trace("market_analyst", "中性", 48, parsed=False,
                   structured={"direction": "偏多"}),
        ])
        assert summary["agent_breakdown"][0]["confidence"] is None


# ---------------------------------------------------------------------------
# the two layers must agree
# ---------------------------------------------------------------------------

class TestConsensusAndResearchManagerAgree:
    """They used to disagree: consensus read `verdict`, the manager preferred
    `structured.direction`. One stored report could be 偏多 to the manager and
    中性-or-dropped to the card rendered beside it."""

    def test_direction_matches_for_a_verdict_only_trace(self):
        trace = _trace("market_analyst", "看空", 60, parsed=True)
        assert _summary([trace])["agent_breakdown"][0]["verdict"] == "看空"
        assert "看空" in build_structured_brief_for_research_manager([trace])

    def test_direction_matches_for_an_analyst_json_only_trace(self):
        trace = _trace("market_analyst", "中性", 48, parsed=False,
                       structured={"direction": "偏多", "confidence": 70})
        assert _summary([trace])["agent_breakdown"][0]["verdict"] == "偏多"
        assert "偏多" in build_structured_brief_for_research_manager([trace])

    def test_manager_no_longer_drops_a_trace_that_lacks_analyst_json(self):
        """Mirror-image bug: the manager used to skip any trace without `structured`,
        so a report whose VERDICT parsed vanished from its input entirely."""
        brief = build_structured_brief_for_research_manager([
            _trace("market_analyst", "看多", 80, parsed=True),
        ])
        assert "market_analyst" in brief and "看多" in brief

    def test_manager_skips_only_traces_with_no_direction_at_all(self):
        brief = build_structured_brief_for_research_manager([
            _trace("market_analyst", "看多", 80, parsed=True),
            _trace("macro_analyst", "中性", 48, parsed=False),
        ])
        assert "market_analyst" in brief
        assert "macro_analyst" not in brief

    @pytest.mark.parametrize(
        "trace",
        [
            _trace("market_analyst", "看多", 80, parsed=True),
            _trace("market_analyst", "中性", 48, parsed=False,
                   structured={"direction": "看空", "confidence": 66}),
            _trace("market_analyst", "看多", 80),
        ],
    )
    def test_both_layers_report_the_same_direction(self, trace):
        breakdown = _summary([trace])["agent_breakdown"]
        brief = build_structured_brief_for_research_manager([trace])
        assert breakdown[0]["verdict"] in brief


# ---------------------------------------------------------------------------
# the prompt contract must not demand two blocks again
# ---------------------------------------------------------------------------

class TestAnalystPromptsDemandOneMachineReadableBlock:
    ANALYSTS = (
        "market", "news", "social", "fundamentals", "macro", "smart_money", "volume_price",
    )

    @pytest.mark.parametrize("lang", ["zh", "en"])
    def test_no_analyst_prompt_emits_a_verdict_template(self, lang):
        """A literal VERDICT template in the system prompt is what made the model
        choose between two competing blocks in the first place."""
        from tradingagents.prompts import en, zh

        pack = {"zh": zh, "en": en}[lang]
        for role in self.ANALYSTS:
            text = str(pack.PROMPTS.get(f"{role}_system_message") or "")
            if not text:
                continue
            assert "VERDICT: {" not in text, f"{lang}/{role} still ships a VERDICT template"
            assert "VERDICT: {{" not in text, f"{lang}/{role} still ships a VERDICT template"

    def test_zh_analyst_prompts_point_at_analyst_json(self):
        from tradingagents.prompts import zh

        for role in self.ANALYSTS:
            text = str(zh.PROMPTS.get(f"{role}_system_message") or "")
            assert text, f"missing {role}_system_message"
            assert "ANALYST_JSON" in text, f"zh/{role} must name the surviving block"

    def test_non_analyst_roles_keep_their_verdict_contract(self):
        """Trader / research manager / risk manager are read by other code paths
        (e.g. report_service reads VERDICT out of the trader's decision), so their
        contract must not be collateral damage."""
        from tradingagents.prompts import zh

        for key in ("research_manager_prompt", "risk_manager_prompt", "trader_system_prompt"):
            assert "VERDICT: {" in str(zh.PROMPTS.get(key) or ""), f"{key} lost its VERDICT block"


# ---------------------------------------------------------------------------
# end-to-end on the real shape of the report from the screenshot
# ---------------------------------------------------------------------------

class TestScreenshotReportIsNowCounted:
    """600699.SH 2026-09-29: the card that showed "5 份报告解析失败，未计入共识".

    Two blocks were present in the corpus but the reports only carried
    ANALYST_JSON; consensus counted 2 of 7 analysts, and the dropped five
    included a 中性 that would have lowered the displayed strength.
    """

    TRACES = [
        _trace("market_analyst", "偏多", 70, parsed=True, structured={"direction": "偏多", "confidence": 70}),
        _trace("news_analyst", "中性", 48, parsed=False, structured={"direction": "偏多", "confidence": 65}),
        _trace("social_media_analyst", "中性", 48, parsed=False, structured={"direction": "偏多", "confidence": 60}),
        _trace("fundamentals_analyst", "中性", 48, parsed=False, structured={"direction": "偏多", "confidence": 68}),
        _trace("macro_analyst", "中性", 48, parsed=False, structured={"direction": "偏多", "confidence": 62}),
        _trace("smart_money_analyst", "中性", 48, parsed=False, structured={"direction": "中性", "confidence": 55}),
        _trace("volume_price_analyst", "中性", 48, parsed=False, structured={"direction": "中性", "confidence": 58}),
    ]

    def test_all_seven_analysts_now_vote(self):
        summary = _summary(self.TRACES)
        assert summary["unparsed_analyst_n"] == 0
        assert summary["recovered_analyst_n"] == 6
        assert len(summary["agent_breakdown"]) == 7
        assert summary["consensus_direction"] == "偏多"

    def test_the_old_behaviour_would_have_dropped_six_of_seven(self):
        """Guard against a regression that silently restores the old denominator."""
        legacy = cs.build_consensus_summary(
            result_data={
                "analyst_traces": [{k: v for k, v in t.items() if k != "structured"} for t in self.TRACES],
                "risk_feedback_state": {},
            },
            final_direction="偏多",
            final_confidence=68,
        )
        assert legacy["unparsed_analyst_n"] == 6
        assert legacy["recovered_analyst_n"] == 0
        assert len(legacy["agent_breakdown"]) == 1

    def test_verdict_parsed_keeps_its_narrow_meaning(self):
        """`verdict_parsed` must keep meaning "the VERDICT block parsed" so the
        provenance stays auditable; only `verdict_source` says where the value came from."""
        summary = _summary(self.TRACES)
        by_agent = {row["agent"]: row for row in summary["agent_breakdown"]}
        assert by_agent["market_analyst"]["verdict_source"] == "verdict"
        assert by_agent["macro_analyst"]["verdict_source"] == "analyst_json"
