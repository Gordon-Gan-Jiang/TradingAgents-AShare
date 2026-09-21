"""Structured ANALYST_JSON extraction and research-manager brief formatting."""

from tradingagents.agents.utils.analyst_structured import (
    build_structured_brief_for_research_manager,
    extract_analyst_structured_json,
    get_analyst_json_instruction,
)


def test_extract_analyst_structured_json():
    text = '分析结论…\n<!-- ANALYST_JSON: {"agent_role":"market","direction":"看多","confidence":70,"evidence_anchors":[{"field":"rsi","value":"55","source":"indicators"}],"data_gaps":[]} -->'
    d = extract_analyst_structured_json(text)
    assert d is not None
    assert d["direction"] == "看多"
    assert len(d["evidence_anchors"]) == 1


def test_build_structured_brief():
    traces = [
        {
            "agent": "market_analyst",
            "verdict": "看多",
            "structured": {
                "direction": "看多",
                "confidence": 70,
                "evidence_anchors": [{"field": "close", "value": "100", "source": "get_stock_data"}],
                "data_gaps": [],
            },
        }
    ]
    brief = build_structured_brief_for_research_manager(traces)
    assert "market_analyst" in brief
    assert "100" in brief


def test_get_analyst_json_instruction_zh():
    instr = get_analyst_json_instruction(agent_role="news", config={"prompt_language": "zh"})
    assert "ANALYST_JSON" in instr
    assert "news" in instr
