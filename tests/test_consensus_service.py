from api.services.consensus_service import build_consensus_summary


def test_build_consensus_summary_prefers_weighted_short_term_bullish_views():
    result = {
        "analyst_traces": [
            {"agent": "market_analyst", "horizon": "short", "verdict": "看多", "confidence": "高", "key_finding": "放量突破"},
            {"agent": "smart_money_analyst", "horizon": "short", "verdict": "偏多", "confidence": "中", "key_finding": "主力净流入"},
            {"agent": "volume_price_analyst", "horizon": "short", "verdict": "看多", "confidence": 80, "key_finding": "量价配合"},
            {"agent": "fundamentals_analyst", "horizon": "medium", "verdict": "中性", "confidence": "中", "key_finding": "估值中性"},
        ],
        "risk_feedback_state": {
            "latest_risk_verdict": "pass",
            "execution_preconditions": [],
            "de_risk_triggers": ["跌破 20 日线"],
        },
    }

    summary = build_consensus_summary(result_data=result, final_direction="看多", final_confidence=68)

    assert summary is not None
    assert summary["consensus_direction"] in {"偏多", "看多"}
    assert summary["consensus_strength"] >= 55
    assert summary["execution_mode"] in {"direct", "conditional"}
    assert summary["supporting_agents"]
    assert summary["agent_breakdown"][0]["label"] in {"市场面", "资金面", "量价面"}


def test_build_consensus_summary_detects_horizon_conflict_and_conditional_execution():
    result = {
        "analyst_traces": [
            {"agent": "market_analyst", "horizon": "short", "verdict": "看多", "confidence": "高", "key_finding": "短线趋势向上"},
            {"agent": "smart_money_analyst", "horizon": "short", "verdict": "偏多", "confidence": "中", "key_finding": "资金承接较强"},
            {"agent": "fundamentals_analyst", "horizon": "medium", "verdict": "看空", "confidence": "高", "key_finding": "估值透支"},
            {"agent": "macro_analyst", "horizon": "medium", "verdict": "偏空", "confidence": "中", "key_finding": "行业景气回落"},
        ],
        "risk_feedback_state": {
            "latest_risk_verdict": "revise",
            "execution_preconditions": ["放量站稳前高"],
            "de_risk_triggers": ["跌破前低"],
        },
    }

    summary = build_consensus_summary(result_data=result, final_direction="偏多", final_confidence=60)

    assert summary is not None
    assert summary["horizon_conflict"] is True
    assert summary["execution_mode"] in {"conditional", "observe"}
    assert summary["stability_score"] < 70
    assert "放量站稳前高" in summary["flip_conditions"]
    assert "跌破前低" in summary["flip_conditions"]


def test_build_consensus_summary_downgrades_high_disagreement_to_observe():
    result = {
        "analyst_traces": [
            {"agent": "market_analyst", "horizon": "short", "verdict": "看多", "confidence": "高", "key_finding": "趋势上行"},
            {"agent": "social_media_analyst", "horizon": "short", "verdict": "看空", "confidence": "高", "key_finding": "情绪极热"},
            {"agent": "news_analyst", "horizon": "short", "verdict": "偏空", "confidence": "高", "key_finding": "利空新闻增多"},
            {"agent": "smart_money_analyst", "horizon": "short", "verdict": "偏多", "confidence": "高", "key_finding": "主力仍在吸筹"},
        ],
        "risk_feedback_state": {
            "latest_risk_verdict": "pass",
            "execution_preconditions": [],
            "de_risk_triggers": [],
        },
    }

    summary = build_consensus_summary(result_data=result, final_direction="中性", final_confidence=50)

    assert summary is not None
    assert summary["disagreement_score"] >= 35
    assert summary["execution_mode"] in {"conditional", "observe"}
    assert summary["oppose_ratio"] > 0
