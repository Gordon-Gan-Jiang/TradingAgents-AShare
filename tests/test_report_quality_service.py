"""Tests for VERDICT reconciliation and trace horizon tagging."""

from api.services import report_quality_service


def test_merge_dual_horizon_traces_tags_horizons():
    short = [{"agent": "market_analyst", "verdict": "看多"}]
    medium = [{"agent": "fundamentals_analyst", "verdict": "中性", "horizon": "medium"}]
    merged = report_quality_service.merge_dual_horizon_traces(short, medium)
    assert merged[0]["horizon"] == "short"
    assert merged[1]["horizon"] == "medium"


def test_reconcile_injects_verdict_when_missing():
    result = {
        "analyst_traces": [
            {"agent": "market_analyst", "verdict": "看多", "confidence": 0.8, "horizon": "short"},
            {"agent": "news_analyst", "verdict": "偏多", "confidence": 0.7, "horizon": "short"},
        ],
        "final_trade_decision": "综合建议：短线跟随趋势。",
    }
    report_quality_service.reconcile_verdict_with_consensus(result)
    assert "quality_flags" in result
    assert "verdict_synthesized_from_consensus" in result["quality_flags"]
    assert "<!-- VERDICT:" in result["final_trade_decision"]


def test_reconcile_realigns_opposing_verdict():
    result = {
        "analyst_traces": [
            {"agent": "market_analyst", "verdict": "看空", "confidence": 0.85, "horizon": "short"},
            {"agent": "volume_price_analyst", "verdict": "偏空", "confidence": 0.8, "horizon": "short"},
        ],
        "final_trade_decision": '<!-- VERDICT: {"direction": "看多", "reason": "错误"} -->\n',
    }
    report_quality_service.reconcile_verdict_with_consensus(result)
    assert "verdict_realigned_with_consensus" in (result.get("quality_flags") or [])
    assert "看空" in result["final_trade_decision"] or "偏空" in result["final_trade_decision"]
