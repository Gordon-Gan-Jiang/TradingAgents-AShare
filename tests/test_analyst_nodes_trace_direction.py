"""Each analyst node must build its trace from the block the model actually wrote.

## Why this file exists (regression: runtime NameError in 6 of 7 analysts)

The consensus fix added ``resolve_analyst_direction`` to the 7 analyst nodes. The
import was only added to ``market_analyst``; the other six called the function
without importing it. Every existing test still passed:

* ``tests/test_market_analyst.py`` only executes ``market_analyst`` -- the one file
  that happened to have the import;
* the consensus/resolver tests feed hand-written trace dicts straight into
  ``build_consensus_summary``, so they never enter a node body;
* syntax checks (``ast.parse``, ``import api.main``) cannot see a ``NameError``,
  which only fires when the function body actually runs.

The bug therefore reached production and surfaced as::

    RuntimeError: Horizon analysis failed: short: name 'resolve_analyst_direction' is not defined

**The lesson is the point of this file: a change to a node body must be covered by
a test that RUNS that node body.** Import-level and syntax-level checks are not
coverage. These tests execute all 7 nodes for real (stub LLM + stub collector) and
assert the trace each one produces.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tradingagents.agents.analysts.fundamentals_analyst import create_fundamentals_analyst
from tradingagents.agents.analysts.macro_analyst import create_macro_analyst
from tradingagents.agents.analysts.market_analyst import create_market_analyst
from tradingagents.agents.analysts.news_analyst import create_news_analyst
from tradingagents.agents.analysts.smart_money_analyst import create_smart_money_analyst
from tradingagents.agents.analysts.social_media_analyst import create_social_media_analyst
from tradingagents.agents.analysts.volume_price_analyst import create_volume_price_analyst

# (factory, trace agent name, report key returned)
ANALYSTS = [
    (create_market_analyst, "market_analyst", "market_report"),
    (create_news_analyst, "news_analyst", "news_report"),
    (create_social_media_analyst, "social_media_analyst", "sentiment_report"),
    (create_fundamentals_analyst, "fundamentals_analyst", "fundamentals_report"),
    (create_macro_analyst, "macro_analyst", "macro_report"),
    (create_smart_money_analyst, "smart_money_analyst", "smart_money_report"),
    (create_volume_price_analyst, "volume_price_analyst", "volume_price_report"),
]
ANALYST_IDS = [a[1] for a in ANALYSTS]


class _StubCollector:
    """Minimal data_collector: whatever a node asks for, it gets the same pool."""

    def __init__(self, pool):
        self._pool = pool

    def get(self, ticker, date):
        return dict(self._pool)

    def get_window(self, pool, horizon, date):
        return dict(pool) if isinstance(pool, dict) else dict(self._pool)


def _make_state(horizon="short"):
    return {
        "trade_date": "2026-03-12",
        "company_of_interest": "600519",
        "horizon": horizon,
        "user_intent": {
            "raw_query": "test", "ticker": "600519",
            "horizons": ["short", "medium"], "focus_areas": [], "specific_questions": [],
        },
    }


def _stub_pool(horizon="short"):
    days = "14天" if horizon == "short" else "90天"
    return {
        "stock_data": "close,volume\n100,1000",
        "indicators": {k: "50" for k in [
            "close_50_sma", "close_200_sma", "close_10_ema",
            "rsi", "macd", "boll", "boll_ub", "boll_lb", "atr", "vwma",
        ]},
        "_data_window": days, "_horizon": horizon,
    }


def _llm_returning(content: str) -> MagicMock:
    mock_llm = MagicMock()

    async def _astream(_messages, *args, **kwargs):
        yield SimpleNamespace(content=content)

    mock_llm.astream = _astream
    return mock_llm


# A report that writes ONLY ANALYST_JSON -- the real-world shape that made the
# consensus card report "5 份报告解析失败". There is deliberately no VERDICT block.
_ONLY_ANALYST_JSON = (
    "分析正文：技术面偏多，量能配合。\n\n"
    '<!-- ANALYST_JSON: {"agent_role": "analyst", "direction": "偏多", '
    '"confidence": 71, "evidence_anchors": [], "data_gaps": []} -->'
)

# Both blocks present and disagreeing: the VERDICT block must win, because the
# resolver's precedence is what keeps single-block and dual-block reports consistent.
_BOTH_BLOCKS = (
    "分析正文。\n\n"
    '<!-- VERDICT: {"direction": "看空", "reason": "证据转弱", "confidence": 60} -->\n'
    '<!-- ANALYST_JSON: {"agent_role": "analyst", "direction": "偏多", '
    '"confidence": 71, "evidence_anchors": [], "data_gaps": []} -->'
)

# Neither block: a genuine abstention. Must stay fail-closed (excluded from the vote)
# rather than being turned into a neutral opinion.
_NEITHER_BLOCK = "分析正文，没有任何机读块。"


def _run(factory, content, horizon="short"):
    node = factory(_llm_returning(content), _StubCollector(_stub_pool(horizon)))
    return asyncio.run(node(_make_state(horizon)))


# ---------------------------------------------------------------------------
# the regression: every node body must actually execute
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("factory,agent,report_key", ANALYSTS, ids=ANALYST_IDS)
def test_node_builds_a_trace_without_raising(factory, agent, report_key):
    """Guards the NameError class of bug: the node body has to run end to end."""
    result = _run(factory, _ONLY_ANALYST_JSON)
    assert report_key in result, f"{agent} did not return {report_key}"
    assert len(result["analyst_traces"]) == 1
    assert result["analyst_traces"][0]["agent"] == agent


@pytest.mark.parametrize("factory,agent,report_key", ANALYSTS, ids=ANALYST_IDS)
def test_analyst_json_only_report_resolves_direction(factory, agent, report_key):
    """The exact production shape: direction comes from ANALYST_JSON, and the trace
    must say so instead of falling back to the neutral display value."""
    trace = _run(factory, _ONLY_ANALYST_JSON)["analyst_traces"][0]
    assert trace["verdict"] == "偏多", f"{agent} lost the ANALYST_JSON direction"
    assert trace["verdict_source"] == "analyst_json"
    assert trace["verdict_parsed"] is False, "narrow meaning: only the VERDICT block's status"
    assert "偏多" in trace["key_finding"], "key_finding must not show the neutral fallback"
    assert trace["confidence"] == 71, "confidence must come from the block that held the direction"


@pytest.mark.parametrize("factory,agent,report_key", ANALYSTS, ids=ANALYST_IDS)
def test_verdict_block_wins_when_both_are_present(factory, agent, report_key):
    trace = _run(factory, _BOTH_BLOCKS)["analyst_traces"][0]
    assert trace["verdict"] == "看空"
    assert trace["verdict_source"] == "verdict"
    assert trace["verdict_parsed"] is True


@pytest.mark.parametrize("factory,agent,report_key", ANALYSTS, ids=ANALYST_IDS)
def test_a_report_with_no_block_stays_an_abstention(factory, agent, report_key):
    """Fail-closed must survive: no block means no stated opinion. The trace keeps the
    neutral display fallback (so the trace UI always renders something) but is
    reported as source="none" so the consensus layer excludes it."""
    trace = _run(factory, _NEITHER_BLOCK)["analyst_traces"][0]
    assert trace["verdict_source"] == "none"
    assert trace["verdict_parsed"] is False


@pytest.mark.parametrize("factory,agent,report_key", ANALYSTS, ids=ANALYST_IDS)
def test_node_recovers_from_analyst_json_direction_that_needs_normalising(
    factory, agent, report_key
):
    """en-pack vocabulary must resolve too, not just the zh canonical words."""
    content = (
        "report.\n\n"
        '<!-- ANALYST_JSON: {"agent_role": "analyst", "direction": "LEAN_BEARISH", '
        '"confidence": 55} -->'
    )
    trace = _run(factory, content)["analyst_traces"][0]
    assert trace["verdict"] == "偏空"
    assert trace["verdict_source"] == "analyst_json"


# ---------------------------------------------------------------------------
# the traces these nodes produce must flow through consensus correctly
# ---------------------------------------------------------------------------

def test_all_seven_nodes_feed_a_full_consensus_vote():
    """End to end: 7 nodes, ANALYST_JSON only, all 7 must be counted."""
    from api.services.consensus_service import build_consensus_summary

    traces = [
        _run(factory, _ONLY_ANALYST_JSON)["analyst_traces"][0]
        for factory, _agent, _key in ANALYSTS
    ]
    summary = build_consensus_summary(
        result_data={"analyst_traces": traces, "risk_feedback_state": {}},
        final_direction="偏多", final_confidence=68,
    )
    assert summary["unparsed_analyst_n"] == 0
    assert summary["recovered_analyst_n"] == 7
    assert len(summary["agent_breakdown"]) == 7
    assert summary["consensus_direction"] == "偏多"


def test_abstaining_nodes_produce_no_consensus_rather_than_a_fake_neutral_one():
    """When *every* analyst abstains the service returns None: there is no consensus.

    ``build_consensus_summary`` ends with ``if total_weight <= 0: return None``, so a
    report whose analysts all stayed silent yields no consensus object -- the card
    shows nothing instead of a fabricated "中性". That is the same fail-closed rule
    the exclusion logic follows; asserting the counts would mean asserting a summary
    that must not exist.
    """
    from api.services.consensus_service import build_consensus_summary

    traces = [
        _run(factory, _NEITHER_BLOCK)["analyst_traces"][0]
        for factory, _agent, _key in ANALYSTS
    ]
    assert all(t["verdict_source"] == "none" for t in traces), "all 7 must be abstentions"
    summary = build_consensus_summary(
        result_data={"analyst_traces": traces, "risk_feedback_state": {}},
        final_direction="中性", final_confidence=50,
    )
    assert summary is None


def test_abstentions_are_excluded_while_the_real_votes_still_count():
    """The mixed case: abstentions are dropped from the denominator but the report
    still produces a consensus from the analysts who did state a direction."""
    from api.services.consensus_service import build_consensus_summary

    traces = [
        _run(factory, _ONLY_ANALYST_JSON)["analyst_traces"][0]
        for factory, _agent, _key in ANALYSTS[:2]
    ] + [
        _run(factory, _NEITHER_BLOCK)["analyst_traces"][0]
        for factory, _agent, _key in ANALYSTS[2:]
    ]
    summary = build_consensus_summary(
        result_data={"analyst_traces": traces, "risk_feedback_state": {}},
        final_direction="偏多", final_confidence=68,
    )
    assert summary["unparsed_analyst_n"] == 5
    assert summary["recovered_analyst_n"] == 2
    assert len(summary["agent_breakdown"]) == 2
