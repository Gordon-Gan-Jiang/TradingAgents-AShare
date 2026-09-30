"""Provenance tests: ``consensus_strength`` must never be persisted as a stated confidence.

Background (see docs/report-judgment-accuracy-audit.md §2.1)
-----------------------------------------------------------
``reconcile_verdict_with_consensus`` writes a consensus-derived VERDICT block. It
used to place ``consensus_strength`` — a *direction-agreement magnitude*
(``|weighted mean| * 100``) — into the block's ``confidence`` field. Because
``report_service._resolve_confidence`` reads that field as the model's *stated*
confidence, the agreement magnitude was persisted as ``reports.confidence`` and
rendered to the user as "置信度 N%": a number no model ever produced. The
realignment branch additionally destroyed a confidence the judge *had* stated.

``_resolve_confidence``'s own docstring states the rule being enforced here:
a synthesized value "must **not** be persisted here, or every report without a
stated confidence would be indistinguishable from one that stated 68."

Invariants under test
---------------------
1. A synthesized/realigned block never states a confidence that the judge did not.
2. Realignment corrects the *direction* and preserves the judge's stated confidence.
3. With no consensus at all, no block is invented (no ``中性 / 55`` fabrication).
4. Every substitution stays traceable through ``quality_flags``.
"""

from __future__ import annotations

from api.services import consensus_service, report_quality_service
from api.services.report_service import _resolve_confidence
from tradingagents.agents.utils.direction import extract_tagged_json

BULLISH_TRACES = [
    {"agent": "market_analyst", "verdict": "看多", "confidence": 0.8, "horizon": "short"},
    {"agent": "news_analyst", "verdict": "偏多", "confidence": 0.7, "horizon": "short"},
]

BEARISH_TRACES = [
    {"agent": "market_analyst", "verdict": "看空", "confidence": 0.85, "horizon": "short"},
    {"agent": "volume_price_analyst", "verdict": "偏空", "confidence": 0.8, "horizon": "short"},
]


def _consensus_strength(traces: list[dict]) -> int:
    summary = consensus_service.build_consensus_summary(result_data={"analyst_traces": traces})
    assert summary is not None, "测试前置条件失效：这组轨迹本应能形成共识"
    return int(summary["consensus_strength"])


# ---------------------------------------------------------------------------
# 1. 合成块不得自报置信度
# ---------------------------------------------------------------------------


def test_synthesized_verdict_states_no_confidence():
    result = {
        "analyst_traces": BULLISH_TRACES,
        "final_trade_decision": "综合建议：短线跟随趋势。",
    }
    report_quality_service.reconcile_verdict_with_consensus(result)

    assert "verdict_synthesized_from_consensus" in result["quality_flags"]
    payload = extract_tagged_json(result["final_trade_decision"], "VERDICT")
    assert isinstance(payload, dict)
    # 方向可以来自共识，但**置信度**没有任何模型自报过 —— 不得写入数字
    assert payload.get("confidence") is None


def test_synthesized_verdict_confidence_is_not_persisted():
    result = {
        "analyst_traces": BULLISH_TRACES,
        "final_trade_decision": "综合建议：短线跟随趋势。",
    }
    report_quality_service.reconcile_verdict_with_consensus(result)
    assert _resolve_confidence(result["final_trade_decision"]) is None


def test_consensus_strength_never_becomes_reports_confidence():
    """核心不变量：写出的置信度不得等于 consensus_strength。"""
    strength = _consensus_strength(BULLISH_TRACES)
    result = {
        "analyst_traces": BULLISH_TRACES,
        "final_trade_decision": "综合建议：短线跟随趋势。",
    }
    report_quality_service.reconcile_verdict_with_consensus(result)

    payload = extract_tagged_json(result["final_trade_decision"], "VERDICT")
    assert payload.get("confidence") != strength
    assert _resolve_confidence(result["final_trade_decision"]) != strength


# ---------------------------------------------------------------------------
# 2. 校准方向时必须保留法官自报的置信度
# ---------------------------------------------------------------------------


def test_realign_does_not_attach_judge_confidence_to_a_flipped_direction():
    """校准方向后，法官自报的置信度**不得**被贴到被翻转的方向上。

    设计决策（与测试初稿不同，此处记录理由）：法官说的是「看多，置信度 75」。
    把方向改写成共识的「看空」之后，75 就不再是「对看空的置信度」——
    保留它会让报告断言一句法官从未说过的话（"确信看空 75%"）。因此校准分支
    必须让 confidence 缺席，而**绝不**回填共识强度。
    """
    strength = _consensus_strength(BEARISH_TRACES)
    result = {
        "analyst_traces": BEARISH_TRACES,
        "final_trade_decision": (
            '<!-- VERDICT: {"direction": "看多", "reason": "资金持续流入", "confidence": 75} -->'
        ),
    }
    assert strength != 75, "测试前置条件失效：强度与法官自报值恰好相同会让断言失去意义"

    report_quality_service.reconcile_verdict_with_consensus(result)

    assert "verdict_realigned_with_consensus" in result["quality_flags"]
    payload = extract_tagged_json(result["final_trade_decision"], "VERDICT")
    # 方向被校准为共识方向
    assert payload.get("direction") in {"看空", "偏空"}
    # 置信度：既不是共识强度，也不是被翻转方向的法官旧值 —— 必须缺席
    assert payload.get("confidence") != strength
    assert payload.get("confidence") is None
    assert _resolve_confidence(result["final_trade_decision"]) is None


def test_realign_without_stated_confidence_states_none():
    result = {
        "analyst_traces": BEARISH_TRACES,
        "final_trade_decision": '<!-- VERDICT: {"direction": "看多", "reason": "资金持续流入"} -->',
    }
    report_quality_service.reconcile_verdict_with_consensus(result)

    payload = extract_tagged_json(result["final_trade_decision"], "VERDICT")
    assert payload.get("direction") in {"看空", "偏空"}
    assert payload.get("confidence") is None
    assert _resolve_confidence(result["final_trade_decision"]) is None


# ---------------------------------------------------------------------------
# 3. 没有任何共识时不得凭空写入 中性/55
# ---------------------------------------------------------------------------


def test_no_traces_does_not_fabricate_a_verdict():
    text = "由于数据缺失，本期无法形成明确判断。"
    result = {"analyst_traces": [], "final_trade_decision": text}
    report_quality_service.reconcile_verdict_with_consensus(result)

    assert result["final_trade_decision"] == text, "无共识时不得改写原文"
    assert extract_tagged_json(result["final_trade_decision"], "VERDICT") is None
    assert _resolve_confidence(result["final_trade_decision"]) is None
    # 必须留痕，使「未给出方向」可被测量层识别
    assert "verdict_unstated_no_consensus" in result["quality_flags"]


def test_all_traces_unreadable_does_not_fabricate_a_verdict():
    traces = [
        {"agent": "market_analyst", "verdict": "中性", "verdict_parsed": False, "horizon": "short"},
        {"agent": "news_analyst", "verdict": "中性", "verdict_parsed": False, "horizon": "short"},
    ]
    text = "由于数据缺失，本期无法形成明确判断。"
    result = {"analyst_traces": traces, "final_trade_decision": text}
    report_quality_service.reconcile_verdict_with_consensus(result)

    assert result["final_trade_decision"] == text
    assert extract_tagged_json(result["final_trade_decision"], "VERDICT") is None
    assert "verdict_unstated_no_consensus" in result["quality_flags"]


def test_missing_traces_key_does_not_fabricate_a_verdict():
    result = {"final_trade_decision": "无数据。"}
    report_quality_service.reconcile_verdict_with_consensus(result)
    assert extract_tagged_json(result["final_trade_decision"], "VERDICT") is None
    assert "verdict_unstated_no_consensus" in result["quality_flags"]


# ---------------------------------------------------------------------------
# 4. 既有的方向校准行为不得回退
# ---------------------------------------------------------------------------


def test_existing_behavior_still_flags_and_replaces():
    result = {
        "analyst_traces": BULLISH_TRACES,
        "final_trade_decision": "综合建议：短线跟随趋势。",
    }
    report_quality_service.reconcile_verdict_with_consensus(result)
    assert "verdict_synthesized_from_consensus" in result["quality_flags"]
    assert "<!-- VERDICT:" in result["final_trade_decision"]
    payload = extract_tagged_json(result["final_trade_decision"], "VERDICT")
    assert payload.get("direction") in {"看多", "偏多"}


def test_invalid_direction_still_realigns_but_states_no_confidence():
    result = {
        "analyst_traces": BULLISH_TRACES,
        "final_trade_decision": '<!-- VERDICT: {"direction": "火星", "reason": "x"} -->',
    }
    report_quality_service.reconcile_verdict_with_consensus(result)
    assert "verdict_direction_invalid" in result["quality_flags"]
    payload = extract_tagged_json(result["final_trade_decision"], "VERDICT")
    assert payload.get("direction") in {"看多", "偏多"}
    assert payload.get("confidence") is None
