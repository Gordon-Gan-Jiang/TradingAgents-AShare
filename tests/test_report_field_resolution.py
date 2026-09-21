"""Tests for deterministic report-field resolution (A3 / B1).

The confidence written to ``reports.confidence`` used to come from a second LLM
pass whose prompt said "若文中未明确给出则根据语气判断" — the model was asked to
invent a number from the tone of the prose, and that number was then persisted and
bucketed as if it were a measurement.

These tests pin the replacement contract:

* a *stated* confidence is used (from the VERDICT block or an explicit 置信度 line);
* an *unstated* confidence resolves to ``None`` and is never guessed;
* the direction parser's synthesized default (68/58/48...) never leaks into the
  persisted column, because that default is a UI convenience, not a measurement.
"""

import inspect

import pytest

from api.services import report_service
from api.services.report_service import _resolve_confidence, resolve_report_fields


def _verdict(direction="看多", **extra):
    import json

    payload = {"direction": direction, "reason": "r", **extra}
    return f"分析正文。\n<!-- VERDICT: {json.dumps(payload, ensure_ascii=False)} -->"


# ---------------------------------------------------------------------------
# stated confidence is used
# ---------------------------------------------------------------------------

def test_stated_confidence_in_verdict_block_is_used():
    assert _resolve_confidence(_verdict(confidence=72)) == 72


def test_stated_confidence_survives_shared_coercion():
    # "高" is part of the shared coercion policy (see tests/test_direction.py).
    assert _resolve_confidence(_verdict(confidence="高")) == 82
    assert _resolve_confidence(_verdict(confidence="低")) == 42


def test_out_of_range_confidence_is_clamped():
    assert _resolve_confidence(_verdict(confidence=150)) == 100
    assert _resolve_confidence(_verdict(confidence=-20)) == 0


def test_explicit_percent_line_is_used_when_block_omits_it():
    text = _verdict() + "\n置信度：65%"
    assert _resolve_confidence(text) == 65


def test_confidence_falls_back_to_trader_plan():
    assert _resolve_confidence("无机读块", "交易计划\n置信度：55%") == 55


# ---------------------------------------------------------------------------
# unstated confidence must stay absent — this is the A3 regression
# ---------------------------------------------------------------------------

def test_unstated_confidence_is_none_not_guessed():
    """The whole point of A3: no tone-based invention."""
    text = _verdict(reason="语气非常强烈，极度看好，措辞极其肯定")
    assert _resolve_confidence(text) is None


@pytest.mark.parametrize("direction", ["看多", "偏多", "中性", "偏空", "看空"])
def test_synthesized_direction_default_never_leaks(direction):
    """``extract_direction_result`` fills 68/58/48 in when confidence is absent.

    That default must not reach ``reports.confidence``: otherwise a report that
    stated nothing is indistinguishable from one that stated 68.
    """
    assert _resolve_confidence(_verdict(direction=direction)) is None


@pytest.mark.parametrize("bad", ["", "unknown", "n/a", "  ", True, False])
def test_unreadable_stated_confidence_stays_absent(bad):
    """键存在但值读不出来，必须当作「没说」，不能合成 68。

    这是 B1a 遗留的一扇门：`coerce_confidence` 对 `""` / `"unknown"` / `True`
    都回退成**按方向推导的** 68/58/48。于是 `{"confidence": ""}` 会被落库成 68，
    与「真的报了 68」完全无法区分——正是本函数 docstring 承诺不会发生的事。
    """
    assert _resolve_confidence(_verdict(confidence=bad)) is None


def test_unreadable_confidence_in_every_direction_stays_absent():
    """逐个方向确认：合成值不会从任何方向那条路漏进来。"""
    for direction in ["看多", "偏多", "中性", "偏空", "看空"]:
        assert _resolve_confidence(_verdict(direction=direction, confidence="")) is None
        assert _resolve_confidence(_verdict(direction=direction, confidence="unknown")) is None


def test_empty_and_none_inputs_resolve_to_none():
    assert _resolve_confidence("") is None
    assert _resolve_confidence(None) is None
    assert _resolve_confidence(None, None) is None


def test_prose_without_block_is_not_guessed():
    assert _resolve_confidence("这只股票非常值得买入，信心十足。") is None


# ---------------------------------------------------------------------------
# the override parameter is gone, so a fabricated value cannot be injected
# ---------------------------------------------------------------------------

def test_resolve_report_fields_rejects_confidence_override():
    params = inspect.signature(resolve_report_fields).parameters
    assert "confidence_override" not in params
    with pytest.raises(TypeError):
        resolve_report_fields(result_data={}, confidence_override=99)


def test_create_report_rejects_confidence_override():
    params = inspect.signature(report_service.create_report).parameters
    assert "confidence_override" not in params


# ---------------------------------------------------------------------------
# end-to-end field resolution
# ---------------------------------------------------------------------------

def test_resolve_is_traceable_and_direction_independent():
    resolved = resolve_report_fields(result_data={"final_trade_decision": _verdict(confidence=72)})
    assert resolved["confidence"] == 72
    assert resolved["direction"] == "看多"


def test_resolve_reports_none_confidence_but_keeps_direction():
    """A missing confidence must not erase an otherwise valid direction."""
    resolved = resolve_report_fields(result_data={"final_trade_decision": _verdict()})
    assert resolved["confidence"] is None
    assert resolved["direction"] == "看多"


def test_resolve_direction_is_none_when_block_is_absent():
    resolved = resolve_report_fields(result_data={"final_trade_decision": "只有散文，没有机读块。"})
    assert resolved["direction"] is None
    assert resolved["confidence"] is None
