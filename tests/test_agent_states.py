import operator
from tradingagents.agents.utils.agent_states import UserIntent, TraceItem, extract_verdict

def test_user_intent_typeddict():
    intent: UserIntent = {
        "raw_query": "分析600519短线",
        "ticker": "600519",
        "horizons": ["short", "medium"],
        "focus_areas": ["量价关系"],
        "specific_questions": ["能否到目标位"],
    }
    assert intent["ticker"] == "600519"
    assert intent["horizons"] == ["short", "medium"]

def test_trace_item_typeddict():
    trace: TraceItem = {
        "agent": "market_analyst",
        "horizon": "short",
        "data_window": "14天",
        "key_finding": "RSI超买",
        "verdict": "看空",
        "confidence": 62,
    }
    assert trace["verdict"] == "看空"
    assert trace["confidence"] == 62

def test_trace_list_accumulation():
    t1 = [{"agent": "market_analyst", "verdict": "看空"}]
    t2 = [{"agent": "fundamentals_analyst", "verdict": "看多"}]
    merged = operator.add(t1, t2)
    assert len(merged) == 2


def test_extract_verdict_valid():
    text = '分析结论 <!-- VERDICT: {"direction": "看多", "reason": "量价配合"} --> 结束'
    direction, confidence = extract_verdict(text)
    assert direction == "看多"
    assert confidence == 68


def test_extract_verdict_with_numeric_confidence():
    text = 'x <!-- VERDICT: {"direction": "偏空", "reason": "量价背离", "confidence": 73} -->'
    direction, confidence = extract_verdict(text)
    assert direction == "偏空"
    assert confidence == 73


def test_extract_verdict_with_fractional_confidence():
    text = 'x <!-- VERDICT: {"direction": "NEUTRAL", "reason": "mixed", "confidence": 0.55} -->'
    direction, confidence = extract_verdict(text)
    # Direction is now normalized to the canonical vocabulary. Previously the raw
    # model string ("NEUTRAL") was returned, which leaked a non-canonical value
    # into analyst_traces.verdict and skewed the consensus vote. See
    # tradingagents/agents/utils/direction.py.
    assert direction == "中性"
    assert confidence == 55


def test_extract_verdict_confidence_as_digit_string():
    text = 'x <!-- VERDICT: {"direction": "偏多", "reason": "资金承接", "confidence": "71"} -->'
    direction, confidence = extract_verdict(text)
    assert direction == "偏多"
    assert confidence == 71


def test_extract_verdict_missing():
    direction, confidence = extract_verdict("没有VERDICT标签的文本")
    assert direction == "中性"
    assert confidence == 48


def test_extract_verdict_empty():
    direction, confidence = extract_verdict("")
    assert direction == "中性"
    assert confidence == 48
