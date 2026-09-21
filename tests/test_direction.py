"""Tests for the canonical direction parser (``agents/utils/direction.py``).

These tests encode the fail-closed contract. The two regressions that matter most
and are covered explicitly:

* ``test_no_full_text_scan`` — prose mentioning 买入/卖出 without a VERDICT block
  or a labelled line must yield abstention. The old parser scanned the whole
  document and returned BUY.
* ``test_conflicting_phrase_is_conflict`` — a snippet containing both a buy and a
  sell keyword must yield ``(None, True)``. The old parser tested buy keywords
  first and therefore returned BUY.
"""

import pytest

from tradingagents.agents.utils.direction import (
    ABSTAIN,
    BEARISH,
    BULLISH,
    CANONICAL_DIRECTIONS,
    LEAN_BEARISH,
    LEAN_BULLISH,
    NEUTRAL,
    classify_direction_phrase,
    coerce_confidence,
    coerce_persistable_decision,
    direction_from_score,
    extract_direction_result,
    extract_tagged_json,
    normalize_direction,
    to_trading_decision,
)


# --- normalize_direction -----------------------------------------------------


@pytest.mark.parametrize("canonical", CANONICAL_DIRECTIONS)
def test_normalize_passthrough(canonical):
    assert normalize_direction(canonical) == canonical


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("BULLISH", BULLISH),
        ("bullish", BULLISH),
        ("BUY", BULLISH),
        ("buy", BULLISH),
        ("BEARISH", BEARISH),
        ("SELL", BEARISH),
        ("sell", BEARISH),
        ("NEUTRAL", NEUTRAL),
        ("neutral", NEUTRAL),
        ("HOLD", NEUTRAL),
        ("hold", NEUTRAL),
        ("LEAN_BULLISH", LEAN_BULLISH),
        ("lean_bearish", LEAN_BEARISH),
    ],
)
def test_normalize_english_aliases(raw, expected):
    assert normalize_direction(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("买入", BULLISH),
        ("增持", BULLISH),
        ("做多", BULLISH),
        ("卖出", BEARISH),
        ("减持", BEARISH),
        ("清仓", BEARISH),
        ("回避", BEARISH),
        ("持有", NEUTRAL),
        ("观望", NEUTRAL),
        ("看涨", BULLISH),
        ("看跌", BEARISH),
    ],
)
def test_normalize_chinese_synonyms(raw, expected):
    assert normalize_direction(raw) == expected


@pytest.mark.parametrize(
    "raw", [None, "", "   ", "UNKNOWN", "unknown", "DRY_RUN", "N/A", "NA", "TBD"]
)
def test_normalize_non_direction_is_none(raw):
    """Non-answers must never become a direction."""
    assert normalize_direction(raw) is None


def test_normalize_strips_decoration():
    assert normalize_direction("  **看多**  ") == BULLISH
    assert normalize_direction("「偏空」") == LEAN_BEARISH


def test_normalize_handles_trailing_detail():
    """A direction followed by detail still resolves to that direction."""
    assert normalize_direction("看多（置信度72）") == BULLISH


def test_normalize_nested_preference_is_not_conflict():
    """'谨慎看多' is one opinion (偏多), not a 谨慎/看多 conflict."""
    assert normalize_direction("谨慎看多") == LEAN_BULLISH


def test_normalize_compound_neutral_bias():
    assert normalize_direction("中性偏多") == LEAN_BULLISH
    assert normalize_direction("中性偏空") == LEAN_BEARISH


def test_normalize_bool_is_none():
    assert normalize_direction(True) is None


# --- classify_direction_phrase ----------------------------------------------


def test_classify_single_opinion():
    direction, conflict = classify_direction_phrase("建议减持")
    assert direction == BEARISH
    assert conflict is False


def test_conflicting_phrase_is_conflict():
    """Regression: buy keywords were tested before sell keywords."""
    direction, conflict = classify_direction_phrase("买入并卖出")
    assert direction is None
    assert conflict is True


def test_conflicting_mixed_synonyms_is_conflict():
    direction, conflict = classify_direction_phrase("可以建仓，但也可考虑清仓")
    assert direction is None
    assert conflict is True


def test_classify_empty_is_not_conflict():
    assert classify_direction_phrase("") == (None, False)
    assert classify_direction_phrase(None) == (None, False)


def test_hedge_marker_is_not_conflict():
    """'减持，等待企稳' is one bearish opinion plus a timing caveat."""
    direction, conflict = classify_direction_phrase("建议减持，等待企稳")
    assert direction == BEARISH
    assert conflict is False


def test_hedge_marker_after_bullish_is_not_conflict():
    direction, conflict = classify_direction_phrase("可以建仓，但需耐心持有")
    assert direction == BULLISH
    assert conflict is False


def test_only_neutral_markers_yield_neutral():
    direction, conflict = classify_direction_phrase("观望等待")
    assert direction == NEUTRAL
    assert conflict is False


def test_repeated_same_side_picks_strongest():
    assert classify_direction_phrase("看多，也偏多") == (BULLISH, False)
    assert classify_direction_phrase("看空，也偏空") == (BEARISH, False)


def test_conflict_is_order_independent():
    """Fail-closed must not depend on keyword order (the old bug)."""
    assert classify_direction_phrase("买入并卖出") == (None, True)
    assert classify_direction_phrase("卖出并买入") == (None, True)


def test_classify_non_direction_is_not_conflict():
    assert classify_direction_phrase("UNKNOWN") == (None, False)


# --- to_trading_decision -----------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (BULLISH, "BUY"),
        (LEAN_BULLISH, "BUY"),
        (NEUTRAL, "HOLD"),
        (LEAN_BEARISH, "SELL"),
        (BEARISH, "SELL"),
        ("买入", "BUY"),
        ("清仓", "SELL"),
    ],
)
def test_to_trading_decision(raw, expected):
    assert to_trading_decision(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "UNKNOWN", "DRY_RUN", "nonsense"])
def test_to_trading_decision_unknown_is_none(raw):
    """No view must not silently become HOLD."""
    assert to_trading_decision(raw) is None


# --- coerce_persistable_decision --------------------------------------------


@pytest.mark.parametrize("value", ["BUY", "SELL", "HOLD", "buy", " sell "])
def test_persistable_decision_passthrough(value):
    assert coerce_persistable_decision(value) in {"BUY", "SELL", "HOLD"}


@pytest.mark.parametrize("value", ["UNKNOWN", "DRY_RUN", None, "", "garbage", "TBD"])
def test_persistable_non_direction_becomes_abstain(value):
    """Regression: 'UNKNOWN' is truthy and used to be persisted as a direction."""
    assert coerce_persistable_decision(value) == ABSTAIN


def test_persistable_accepts_canonical_direction():
    assert coerce_persistable_decision(BULLISH) == "BUY"
    assert coerce_persistable_decision(BEARISH) == "SELL"
    assert coerce_persistable_decision(NEUTRAL) == "HOLD"


# --- direction_from_score ----------------------------------------------------


@pytest.mark.parametrize(
    "score,expected",
    [
        (1.0, BULLISH),
        (0.8, BULLISH),
        (0.75, BULLISH),
        (0.5, LEAN_BULLISH),
        (0.2, LEAN_BULLISH),
        (0.1, NEUTRAL),
        (0.0, NEUTRAL),
        (-0.1, NEUTRAL),
        (-0.2, LEAN_BEARISH),
        (-0.5, LEAN_BEARISH),
        (-0.75, BEARISH),
        (-1.0, BEARISH),
    ],
)
def test_direction_from_score(score, expected):
    assert direction_from_score(score) == expected


def test_direction_from_score_round_trips_with_consensus_thresholds():
    """Thresholds must match consensus_service so both layers agree."""
    assert direction_from_score(0.75) == BULLISH
    assert direction_from_score(0.19999) == NEUTRAL


# --- coerce_confidence -------------------------------------------------------


def test_confidence_passthrough_int():
    assert coerce_confidence(73, BEARISH) == 73


def test_confidence_fraction_scaled():
    assert coerce_confidence(0.55, NEUTRAL) == 55


def test_confidence_digit_string():
    assert coerce_confidence("71", LEAN_BULLISH) == 71


def test_confidence_float_string():
    assert coerce_confidence("72.4", BULLISH) == 72


def test_confidence_qualitative_bucket():
    assert coerce_confidence("高", BULLISH) == 82
    assert coerce_confidence("中", NEUTRAL) == 62
    assert coerce_confidence("低", BEARISH) == 42
    assert coerce_confidence("high", BULLISH) == 82


def test_confidence_clamped():
    assert coerce_confidence(150, BULLISH) == 100
    assert coerce_confidence(-20, BEARISH) == 0


@pytest.mark.parametrize(
    "direction,expected",
    [
        (BULLISH, 68),
        (BEARISH, 68),
        (LEAN_BULLISH, 58),
        (LEAN_BEARISH, 58),
        (NEUTRAL, 48),
        (None, 45),
    ],
)
def test_confidence_missing_uses_deterministic_prior(direction, expected):
    assert coerce_confidence(None, direction) == expected
    assert coerce_confidence("", direction) == expected


def test_confidence_bool_uses_prior():
    assert coerce_confidence(True, BULLISH) == 68


# --- extract_tagged_json -----------------------------------------------------


def test_extract_tagged_json_simple():
    payload = extract_tagged_json('<!-- VERDICT: {"direction": "看多"} -->', "VERDICT")
    assert payload == {"direction": "看多"}


def test_extract_tagged_json_nested_object():
    """Regression: the old non-greedy regex truncated at the first '}'."""
    text = '<!-- RISK_JUDGE: {"verdict": "pass", "meta": {"a": 1, "b": {"c": 2}}} -->'
    payload = extract_tagged_json(text, "RISK_JUDGE")
    assert payload == {"verdict": "pass", "meta": {"a": 1, "b": {"c": 2}}}


def test_extract_tagged_json_brace_inside_string():
    text = '<!-- VERDICT: {"direction": "看多", "reason": "支撑位{关键}"} -->'
    payload = extract_tagged_json(text, "VERDICT")
    assert payload["reason"] == "支撑位{关键}"


def test_extract_tagged_json_escaped_quote_in_string():
    text = '<!-- VERDICT: {"direction": "看多", "reason": "他说\\"买\\""} -->'
    payload = extract_tagged_json(text, "VERDICT")
    assert payload["direction"] == "看多"


def test_extract_tagged_json_missing_closing_comment():
    payload = extract_tagged_json('<!-- VERDICT: {"direction": "偏空"}', "VERDICT")
    assert payload == {"direction": "偏空"}


def test_extract_tagged_json_malformed_returns_none():
    assert extract_tagged_json('<!-- VERDICT: {"direction": 看多} -->', "VERDICT") is None


def test_extract_tagged_json_absent_tag():
    assert extract_tagged_json("no tag here", "VERDICT") is None
    assert extract_tagged_json("", "VERDICT") is None
    assert extract_tagged_json(None, "VERDICT") is None


def test_extract_tagged_json_non_object_payload():
    assert extract_tagged_json("<!-- VERDICT: [1,2] -->", "VERDICT") is None


def test_extract_tagged_json_is_case_insensitive():
    payload = extract_tagged_json('<!-- verdict: {"direction": "看多"} -->', "VERDICT")
    assert payload == {"direction": "看多"}


# --- extract_direction_result ------------------------------------------------


def test_verdict_block_wins():
    text = '文字 <!-- VERDICT: {"direction": "看多", "confidence": 72} --> 最终裁决：卖出'
    result = extract_direction_result(text)
    assert result.direction == BULLISH
    assert result.confidence == 72
    assert result.source == "verdict_block"
    assert result.is_abstain is False


def test_verdict_block_without_confidence_uses_prior():
    text = '<!-- VERDICT: {"direction": "看多"} -->'
    result = extract_direction_result(text)
    assert result.direction == BULLISH
    assert result.confidence == 68


def test_verdict_block_normalizes_direction():
    text = '<!-- VERDICT: {"direction": "NEUTRAL"} -->'
    result = extract_direction_result(text)
    assert result.direction == NEUTRAL


def test_verdict_block_accepts_verdict_key():
    text = '<!-- VERDICT: {"verdict": "偏空", "confidence": 61} -->'
    result = extract_direction_result(text)
    assert result.direction == LEAN_BEARISH
    assert result.confidence == 61


def test_verdict_block_invalid_direction_abstains():
    text = '<!-- VERDICT: {"direction": "UNKNOWN"} -->'
    result = extract_direction_result(text)
    assert result.direction is None
    assert result.source == "verdict_invalid"
    assert result.confidence is None


def test_verdict_block_conflicting_direction_abstains():
    text = '<!-- VERDICT: {"direction": "买入并卖出"} -->'
    result = extract_direction_result(text)
    assert result.direction is None
    assert result.conflict is True
    assert result.source == "verdict_conflict"


def test_labelled_line_parsed():
    text = "分析正文\n最终裁决：建议减持，等待企稳\n"
    result = extract_direction_result(text)
    assert result.direction == BEARISH
    assert result.source == "labelled_line"


def test_labelled_line_conflict_abstains():
    text = "最终裁决：买入还是卖出需观察"
    result = extract_direction_result(text)
    assert result.direction is None
    assert result.conflict is True
    assert result.source == "labelled_conflict"


def test_no_full_text_scan():
    """Regression: prose without a VERDICT block or label must abstain.

    The old parser scanned the entire document, so this text produced BUY.
    """
    text = (
        "该股量价配合良好，主力资金持续净流入，基本面稳健。"
        "但我们也要注意上方压力位，若跌破支撑则应减仓离场。"
        "综合来看，买入需要等待更好的时机。"
    )
    result = extract_direction_result(text)
    assert result.direction is None
    assert result.source == "none"
    assert result.is_abstain is True


def test_labelled_lines_can_be_disabled():
    text = "最终裁决：建议减持"
    result = extract_direction_result(text, allow_labelled_lines=False)
    assert result.direction is None
    assert result.source == "none"


def test_empty_and_none_input_abstain():
    assert extract_direction_result("").direction is None
    assert extract_direction_result(None).direction is None
    assert extract_direction_result("").source == "none"
