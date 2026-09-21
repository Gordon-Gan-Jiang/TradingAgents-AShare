"""工具结果携带 freshness（审计 A2 修复）。

## 修复前的实际状态

`ToolResult.freshness` 在**所有工具中恒为 `{}`**——`freshness_sources` 声明了、
契约设施也在，但没接。后果：新链路上「这份数据可能过时」这件事完全消失，
而旧管道（7 分析师）是靠 freshness 契约 + confidence 封顶来防这个的。

## 一条不可让步的原则：不得伪造 `fresh`

最容易的"修复"是把所有源一律标 `fresh`——那比不填更糟：它把「未验证」伪装成
「已确认最新」，而下游会据此不做任何降级。

因此本模块的三态判定是：

| 情形 | status | 依据 |
|---|---|---|
| 工具声明该源不可用 | `error` + `issue_type=fetch_error` | 事实 |
| 工具**验证过**数据截止日 == as_of | `fresh` | 事实 |
| 其余（读了但未验证截止日） | `unknown` | 诚实承认不知道 |

`unknown` 按既有契约**不得单独抬高** overall_status——这正是它存在的意义。
"""

from __future__ import annotations

import pytest


# ══════════════════════════════════════════════════════════════════════════
# 三态判定
# ══════════════════════════════════════════════════════════════════════════


def test_unavailable_source_is_error_with_fetch_error_issue():
    """取不到的源必须标 `error` + `fetch_error`，与「数据滞后」区分开。"""
    from tradingagents.tools.freshness import evaluate_tool_sources

    pool = evaluate_tool_sources(
        source_keys=("stock_data", "intraday_bars"),
        as_of="2026-03-04",
        unavailable=("intraday_bars",),
    )

    assert pool["intraday_bars"]["status"] == "error"
    assert pool["intraday_bars"]["issue_type"] == "fetch_error"


def test_verified_cutoff_is_fresh():
    """只有**验证过**截止日等于 as_of 才可标 fresh。"""
    from tradingagents.tools.freshness import evaluate_tool_sources

    pool = evaluate_tool_sources(
        source_keys=("stock_data",),
        as_of="2026-03-04",
        verified=("stock_data",),
    )
    assert pool["stock_data"]["status"] == "fresh"
    assert pool["stock_data"]["anchor_actual"] == "2026-03-04"


def test_unverified_source_is_unknown_not_fresh():
    """**核心断言**：读了但没验证截止日 → `unknown`，不得标 fresh。

    把未验证当 fresh 是最容易的"修复"，也是最有欺骗性的：下游会以为数据
    已确认最新，从而不做任何降级。
    """
    from tradingagents.tools.freshness import evaluate_tool_sources

    pool = evaluate_tool_sources(
        source_keys=("chip_distribution",),
        as_of="2026-03-04",
        verified=("stock_data",),
    )
    assert pool["chip_distribution"]["status"] == "unknown"
    assert pool["chip_distribution"]["status"] != "fresh"
    assert "未验证" in (pool["chip_distribution"].get("note") or "")


def test_lagged_cutoff_is_stale_not_fresh():
    """截止日早于 as_of → 滞后，必须显式标出。"""
    from tradingagents.tools.freshness import evaluate_tool_sources

    pool = evaluate_tool_sources(
        source_keys=("stock_data",),
        as_of="2026-03-04",
        cutoffs={"stock_data": "2026-02-27"},
    )
    assert pool["stock_data"]["status"] in {"stale", "warning"}
    assert pool["stock_data"]["anchor_actual"] == "2026-02-27"
    assert pool["stock_data"]["issue_type"] == "lag"


def test_metadata_comes_from_the_contract():
    """类别与criticality必须取自契约，不得在工具层另写一份。"""
    from tradingagents.dataflows.freshness.contract import get_contract_entry
    from tradingagents.tools.freshness import evaluate_tool_sources

    pool = evaluate_tool_sources(source_keys=("stock_data",), as_of="2026-03-04", verified=("stock_data",))
    entry = get_contract_entry("stock_data")
    assert pool["stock_data"]["category"] == entry["category"]
    assert pool["stock_data"]["criticality"] == entry["criticality"]


def test_unknown_source_key_is_reported_not_crashed():
    """契约里没有的源不得抛错——它标 unknown 并继续。"""
    from tradingagents.tools.freshness import evaluate_tool_sources

    pool = evaluate_tool_sources(source_keys=("totally_new_source",), as_of="2026-03-04")
    assert pool["totally_new_source"]["status"] == "unknown"


# ══════════════════════════════════════════════════════════════════════════
# 汇总：复用既有 aggregator，不重新实现
# ══════════════════════════════════════════════════════════════════════════


def test_summary_uses_contract_aggregation():
    from tradingagents.tools.freshness import summarize_tool_freshness

    summary = summarize_tool_freshness(
        {"stock_data": {"source_key": "stock_data", "category": "calendar_anchored",
                        "criticality": "critical", "status": "fresh"}},
        as_of="2026-03-04",
        symbol="600519.SH",
    )
    assert summary["overall_status"] == "fresh"
    assert summary["confidence_cap"] == 100


def test_unknown_alone_does_not_degrade_overall():
    """`unknown` 不得单独把 overall 拉低——契约的既有语义。"""
    from tradingagents.tools.freshness import summarize_tool_freshness

    summary = summarize_tool_freshness(
        {
            "stock_data": {"source_key": "stock_data", "category": "calendar_anchored",
                           "criticality": "critical", "status": "fresh"},
            "chip_distribution": {"source_key": "chip_distribution", "category": "calendar_anchored",
                                  "criticality": "important", "status": "unknown"},
        },
        as_of="2026-03-04",
        symbol="600519.SH",
    )
    assert summary["overall_status"] == "fresh"


def test_critical_error_caps_confidence():
    """关键源 error 必须封顶置信度——否则工具层拿到 error 也不降级。"""
    from tradingagents.tools.freshness import summarize_tool_freshness

    summary = summarize_tool_freshness(
        {"stock_data": {"source_key": "stock_data", "category": "calendar_anchored",
                        "criticality": "critical", "status": "error",
                        "issue_type": "fetch_error"}},
        as_of="2026-03-04",
        symbol="600519.SH",
    )
    assert summary["overall_status"] == "error"
    assert summary["confidence_cap"] == 40


# ══════════════════════════════════════════════════════════════════════════
# 工具层真的填了 freshness（端到端）
# ══════════════════════════════════════════════════════════════════════════


def test_signal_probe_populates_freshness():
    """判定探针必须真的填 freshness，而不是留空字典。"""
    from tests.test_acceptance_conversational_signal_agent import _ports_factory
    from tradingagents.tools.signals import make_signal_tools

    tools = {s.name: s for s in make_signal_tools(_ports_factory())}
    result = tools["signal_washout_probe"].handler(symbol="600519.SH", as_of="2026-01-16")

    assert result.freshness, "freshness 不得为空"
    assert "stock_data" in result.freshness
    # 未提供的源必须标 error，而不是缺席
    assert result.freshness["intraday_bars"]["status"] in {"error", "unknown"}


def test_news_tool_populates_freshness():
    from tradingagents.tools.news import make_news_tools

    tools = {s.name: s for s in make_news_tools(lambda *a: [])}
    result = tools["data_symbol_news"].handler(symbol="600519.SH", as_of="2026-03-04")
    assert "symbol_news" in result.freshness
    # 没取到新闻 → 该源 error
    assert result.freshness["symbol_news"]["status"] == "error"


def test_data_tool_populates_freshness():
    from tradingagents.tools.data import make_data_tools

    tools = {s.name: s for s in make_data_tools()}
    spec = tools["get_stock_data"]
    orig = spec.handler

    import tradingagents.tools.data as data_module

    saved = data_module.call_vendor
    data_module.call_vendor = lambda *a, **k: "表格"
    try:
        result = spec.handler(symbol="600519.SH", start_date="2026-01-01", end_date="2026-03-04")
    finally:
        data_module.call_vendor = saved

    # 数据工具无法确证截止日 → 必须标 unknown，而不是 fresh
    assert result.freshness
    assert result.freshness["core_stock_apis"]["status"] == "unknown"


# ══════════════════════════════════════════════════════════════════════════
# A3：覆盖声明不得伪造
# ══════════════════════════════════════════════════════════════════════════


def test_vendor_failure_text_does_not_claim_the_source_is_available():
    """**审计 A3 的核心回归**。

    vendor 在三级源全失败时**返回错误文本而非抛异常**。原实现恒声明
    `available=(category,)`，于是调用方一边拿到「cn_akshare is temporarily
    unavailable ... all failed」，一边读到「该源可用」——伪造的覆盖声明，
    正是 D23「拿不到 ≠ 没有」要堵的漏洞。
    """
    import tradingagents.tools.data as data_module

    saved = data_module.call_vendor
    data_module.call_vendor = lambda *a, **k: (
        "cn_akshare is temporarily unavailable for price history "
        "(eastmoney/sina/tencent all failed): timeout"
    )
    try:
        spec = {s.name: s for s in data_module.make_data_tools()}["get_stock_data"]
        result = spec.handler(symbol="600519.SH", start_date="2026-01-01", end_date="2026-03-04")
    finally:
        data_module.call_vendor = saved

    assert result.coverage.available == ()
    assert "core_stock_apis" in result.coverage.unavailable
    assert result.degraded is True
    assert result.degraded_reason
    assert result.freshness["core_stock_apis"]["status"] == "error"


def test_empty_vendor_output_is_treated_as_unavailable():
    """空返回同样是「拿不到」——不能声明该源可用。"""
    import tradingagents.tools.data as data_module

    saved = data_module.call_vendor
    data_module.call_vendor = lambda *a, **k: "   "
    try:
        spec = {s.name: s for s in data_module.make_data_tools()}["get_stock_data"]
        result = spec.handler(symbol="600519.SH", start_date="2026-01-01", end_date="2026-03-04")
    finally:
        data_module.call_vendor = saved

    assert result.coverage.available == ()
    assert result.degraded is True


def test_successful_vendor_output_claims_available():
    import tradingagents.tools.data as data_module

    saved = data_module.call_vendor
    data_module.call_vendor = lambda *a, **k: "| date | close |\n| 2026-03-04 | 10.0 |"
    try:
        spec = {s.name: s for s in data_module.make_data_tools()}["get_stock_data"]
        result = spec.handler(symbol="600519.SH", start_date="2026-01-01", end_date="2026-03-04")
    finally:
        data_module.call_vendor = saved

    assert result.coverage.available == ("core_stock_apis",)
    assert result.degraded is False


def test_chinese_failure_text_is_recognised():
    import tradingagents.tools.data as data_module

    saved = data_module.call_vendor
    data_module.call_vendor = lambda *a, **k: "板块数据获取失败：接口超时"
    try:
        spec = {s.name: s for s in data_module.make_data_tools()}["get_board_spot"]
        result = spec.handler()
    finally:
        data_module.call_vendor = saved

    assert result.coverage.available == ()
    assert result.degraded is True
