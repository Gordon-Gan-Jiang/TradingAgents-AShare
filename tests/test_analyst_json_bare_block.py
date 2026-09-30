"""The ANALYST_JSON block must be read whether or not the model wraps it in <!-- -->.

## The gap this closes

``extract_analyst_structured_json`` accepted exactly one shape::

    <!-- ANALYST_JSON: {...} -->

A live report (600699.SH, 2026-09-29) showed 6 of 7 analysts writing that wrapped
form and one (``smart_money``) writing the JSON **bare**::

    主力累计净流出规模超过5亿元，即判定为主力启动大规模派发，当前偏多判断失效。

    ANALYST_JSON: {"agent_role":"smart_money","direction":"偏多","confidence":70,...}

The JSON was complete and valid and stated ``"direction":"偏多"``, but the
``<!--`` / ``-->`` delimiters were missing, so the regex missed it, ``structured``
came back ``None``, and that analyst was excluded from the consensus vote. Measured
across the first four post-fix reports: 3 of 28 analyst slots (10.7%) abstained this
way -- the number the UI surfaces as "N 份报告未计入共识（未给出方向）".

## Why accepting the bare form is still fail-closed

The bare form has no ``-->`` terminator, so it is genuinely weaker evidence than the
wrapped form. Two rules keep it honest:

* the payload must parse as a JSON **object** (brace-depth scanned, so nested objects
  and braces inside strings are handled) -- prose is not accepted;
* it must carry a non-empty ``direction``, i.e. a direction the model actually stated.

A bare block without a direction is rejected, so nothing is ever invented and no
analyst is silently turned into a neutral vote.
"""

import json

import pytest

from tradingagents.agents.utils.analyst_structured import (
    extract_analyst_structured_json,
    resolve_analyst_direction,
    resolve_trace_direction,
)

# Verbatim shape of the live smart_money report: bare block, space after the colon,
# nested evidence_anchors, Chinese values.
_LIVE_BARE = (
    "主力累计净流出规模超过5亿元，即判定为主力启动大规模派发，当前偏多判断失效。\n"
    "\n"
    'ANALYST_JSON: {"agent_role":"smart_money","direction":"偏多","confidence":70,'
    '"evidence_anchors":[{"field":"近5日主力资金净流向",'
    '"value":"09-23主力净流向1.75亿元、09-24主力净流向6.45亿元","source":"get_stock_data"},'
    '{"field":"2026-09-29主力净流向","value":"-1.17亿元","source":"get_stock_data"}],'
    '"data_gaps":["缺少2026-09-29当日换手率数据","缺少当日量价对应关系明细数据"]}'
)

_WRAPPED = (
    "量价配合支撑反弹，资金整体偏流入\n\n"
    '<!-- ANALYST_JSON: {"agent_role":"market","direction":"偏多","confidence":70,'
    '"evidence_anchors":[],"data_gaps":[]} -->'
)


# ---------------------------------------------------------------------------
# the defect
# ---------------------------------------------------------------------------

def test_the_live_bare_block_is_parsed():
    """The exact production shape that was being dropped."""
    parsed = extract_analyst_structured_json(_LIVE_BARE)
    assert parsed is not None, "bare ANALYST_JSON block was not recognised"
    assert parsed["direction"] == "偏多"
    assert parsed["agent_role"] == "smart_money"
    assert parsed["confidence"] == 70


def test_the_live_bare_block_resolves_to_a_counted_direction():
    """End to end: the extracted block must actually produce a usable direction."""
    parsed = extract_analyst_structured_json(_LIVE_BARE)
    decision = resolve_analyst_direction(
        verdict=None, confidence=None, verdict_parsed=False, structured=parsed
    )
    assert decision.direction == "偏多"
    assert decision.confidence == 70
    assert decision.source == "analyst_json"


def test_bare_block_with_nested_objects_keeps_its_anchors():
    parsed = extract_analyst_structured_json(_LIVE_BARE)
    assert len(parsed["evidence_anchors"]) == 2
    assert parsed["evidence_anchors"][0]["source"] == "get_stock_data"
    assert len(parsed["data_gaps"]) == 2


@pytest.mark.parametrize(
    "text",
    [
        'ANALYST_JSON: {"direction":"偏空"}',
        'ANALYST_JSON:{"direction":"偏空"}',
        'analyst_json: {"direction":"偏空"}',          # case-insensitive
        'ANALYST_JSON : {"direction":"偏空"}',          # space before the colon
        '正文\n\nANALYST_JSON:\n{"direction":"偏空"}',   # newline after the colon
        '正文\n\nANALYST_JSON: {"direction": "偏空", "confidence": 60}',
    ],
)
def test_bare_block_syntax_variants(text):
    parsed = extract_analyst_structured_json(text)
    assert parsed is not None, f"not recognised: {text!r}"
    assert parsed["direction"] == "偏空"


def test_braces_inside_strings_do_not_confuse_the_scan():
    """A naive greedy regex would stop at the wrong brace or run past the object."""
    text = 'ANALYST_JSON: {"direction":"偏多","note":"用了 {大括号} 和 \\"引号\\" 说明"}'
    parsed = extract_analyst_structured_json(text)
    assert parsed is not None
    assert parsed["direction"] == "偏多"
    assert "大括号" in parsed["note"]


def test_text_after_the_bare_block_is_not_swallowed():
    """A quick summary sentence follows the block; the JSON must still parse."""
    text = 'ANALYST_JSON: {"direction":"偏多","confidence":66}\n\n以上为机读结论。'
    parsed = extract_analyst_structured_json(text)
    assert parsed is not None
    assert parsed["direction"] == "偏多"


# ---------------------------------------------------------------------------
# fail-closed must survive
# ---------------------------------------------------------------------------

def test_bare_block_without_a_direction_is_rejected():
    """The bare form has no delimiter, so it must prove itself with a direction."""
    assert extract_analyst_structured_json('ANALYST_JSON: {"agent_role":"market"}') is None
    assert extract_analyst_structured_json('ANALYST_JSON: {"direction":""}') is None
    assert extract_analyst_structured_json('ANALYST_JSON: {"direction":null}') is None


def test_bare_malformed_json_is_rejected():
    """A truncated block must not be guessed at."""
    assert extract_analyst_structured_json('ANALYST_JSON: {"direction":"偏多",') is None
    assert extract_analyst_structured_json("ANALYST_JSON: 无") is None


def test_prose_mentioning_the_marker_is_not_a_block():
    text = "我按 ANALYST_JSON 格式输出，但这次没有写任何机读块。"
    assert extract_analyst_structured_json(text) is None


def test_no_block_at_all_still_yields_none():
    assert extract_analyst_structured_json("纯正文，没有任何机读块。") is None
    assert extract_analyst_structured_json("") is None
    assert extract_analyst_structured_json(None) is None


# ---------------------------------------------------------------------------
# the wrapped form must keep working exactly as before
# ---------------------------------------------------------------------------

def test_the_wrapped_form_still_parses():
    parsed = extract_analyst_structured_json(_WRAPPED)
    assert parsed["direction"] == "偏多"
    assert parsed["agent_role"] == "market"


def test_wrapped_form_wins_when_both_are_present():
    """The wrapped block is anchored by '-->', so it stays authoritative."""
    text = (
        'ANALYST_JSON: {"direction":"偏空","confidence":10}\n'
        '<!-- ANALYST_JSON: {"direction":"偏多","confidence":90,"agent_role":"market"} -->'
    )
    parsed = extract_analyst_structured_json(text)
    assert parsed["direction"] == "偏多"
    assert parsed["confidence"] == 90


def test_wrapped_block_with_nested_objects_still_parses():
    text = (
        '<!-- ANALYST_JSON: {"agent_role":"news","direction":"偏空","confidence":61,'
        '"evidence_anchors":[{"field":"头条","value":"某事件","source":"get_news"}]} -->'
    )
    parsed = extract_analyst_structured_json(text)
    assert parsed["direction"] == "偏空"
    assert parsed["evidence_anchors"][0]["source"] == "get_news"


def test_wrapped_form_does_not_require_a_direction():
    """Unchanged behaviour: the resolver, not the extractor, decides 'no direction'."""
    parsed = extract_analyst_structured_json('<!-- ANALYST_JSON: {"agent_role":"macro"} -->')
    assert parsed is not None
    decision = resolve_analyst_direction(
        verdict=None, confidence=None, verdict_parsed=False, structured=parsed
    )
    assert decision.direction is None
    assert decision.source == "none"


# ---------------------------------------------------------------------------
# the trace path a real node uses
# ---------------------------------------------------------------------------

def test_a_bare_block_reaches_the_trace_resolver():
    """resolve_trace_direction is what the consensus layer reads."""
    parsed = extract_analyst_structured_json(_LIVE_BARE)
    trace = {
        "agent": "smart_money_analyst",
        "verdict": "中性",           # display fallback the node wrote
        "verdict_parsed": False,
        "confidence": 50,
        "structured": parsed,
    }
    decision = resolve_trace_direction(trace)
    assert decision.direction == "偏多", "the analyst's stated direction must win"
    assert decision.source == "analyst_json"
    assert decision.confidence == 70


def test_consensus_counts_the_recovered_analyst():
    """The whole point: a bare-block report must stop showing as 未计入共识."""
    from api.services.consensus_service import build_consensus_summary

    parsed = extract_analyst_structured_json(_LIVE_BARE)
    traces = [
        {
            "agent": "smart_money_analyst",
            "verdict": "中性",
            "verdict_parsed": False,
            "confidence": 50,
            "structured": parsed,
        },
        {
            "agent": "market_analyst",
            "verdict": "偏多",
            "verdict_parsed": True,
            "confidence": 70,
        },
    ]
    summary = build_consensus_summary(
        result_data={"analyst_traces": traces, "risk_feedback_state": {}},
        final_direction="偏多", final_confidence=68,
    )
    assert summary["unparsed_analyst_n"] == 0, "no analyst should abstain here"
    assert summary["recovered_analyst_n"] == 1
    assert len(summary["agent_breakdown"]) == 2


def test_json_dump_round_trip_is_stable():
    """Whatever we now accept must survive a re-serialisation (manager handoff)."""
    parsed = extract_analyst_structured_json(_LIVE_BARE)
    again = extract_analyst_structured_json(
        "ANALYST_JSON: " + json.dumps(parsed, ensure_ascii=False)
    )
    assert again == parsed
