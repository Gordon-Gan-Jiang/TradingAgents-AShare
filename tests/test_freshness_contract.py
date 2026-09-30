"""Tests for data freshness contract."""

from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import pandas as pd

from tradingagents.dataflows.trade_calendar import CN_TZ
from tradingagents.dataflows.freshness.access import (
    build_degraded_freshness_pool,
    enrich_freshness_summary_for_display,
    inject_freshness_into_state,
    resolve_freshness_pool,
    section_freshness_tooltip,
)
from tradingagents.dataflows.freshness.aggregator import (
    build_freshness_pool,
    build_freshness_summary,
    clamp_confidence,
)
from tradingagents.dataflows.freshness.anchor import is_in_grace_window, resolve_expected_anchor
from tradingagents.dataflows.freshness.integrate import attach_freshness_to_result
from tradingagents.dataflows.freshness.parser import (
    detect_fetch_error,
    disclosure_period_lag,
    expected_disclosure_period,
    parse_latest_event_timestamp,
)
from tradingagents.dataflows.freshness.prompt import (
    format_freshness_context_for_sources,
    format_freshness_context_summary,
)
from tradingagents.dataflows.freshness.validator import (
    build_eval_context,
    evaluate_collector_results,
    evaluate_source,
)
from tradingagents.dataflows.providers.cn_akshare_provider import format_board_fund_flow_ranking


def _load_email_renderer():
    path = Path(__file__).resolve().parents[1] / "api/services/email_report_service.py"
    spec = importlib.util.spec_from_file_location("email_report_service_isolated", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_detect_fetch_error_from_exception_text():
    is_err, code, _ = detect_fetch_error("调用失败：TimeoutError: timed out")
    assert is_err is True
    assert code == "provider_exception"


def test_detect_fetch_error_does_not_false_positive_on_open_word():
    is_err, _, _ = detect_fetch_error("Market opened higher with strong momentum.")
    assert is_err is False


def test_detect_fetch_error_fresh_csv():
    csv = "date,open,high,low,close,volume\n2026-06-20,10,11,9,10.5,1000\n2026-06-23,10.2,11.2,9.8,10.8,1200\n"
    is_err, _, _ = detect_fetch_error(csv)
    assert is_err is False


def test_evaluate_source_error_on_empty():
    ctx = build_eval_context("2026-06-23", symbol="600519.SH", analysis_mode="historical")
    meta = evaluate_source("stock_data", None, ctx)
    assert meta.status == "error"
    assert meta.issue_type == "fetch_error"


def test_evaluate_lhb_empty_ok():
    ctx = build_eval_context("2026-06-23", symbol="600519.SH", analysis_mode="historical")
    text = "600519.SH 在 2026-06-23 无龙虎榜数据（非异动日属正常）。"
    meta = evaluate_source("lhb_detail", text, ctx, trade_date="2026-06-23")
    assert meta.status == "empty_ok"


def test_fund_flow_within_grace_is_warning():
    post_close = datetime(2026, 6, 23, 16, 0, tzinfo=CN_TZ)
    ctx = build_eval_context(
        "2026-06-23", symbol="600519.SH", analysis_mode="post_market", now=post_close
    )
    raw = "600519.SH 近5日主力资金\n数据截止 2026-06-22\n"
    meta = evaluate_source("individual_fund_flow", raw, ctx)
    assert meta.status == "warning"
    assert meta.issue_type == "lag"


def test_fund_flow_beyond_grace_is_stale():
    post_close = datetime(2026, 6, 23, 20, 0, tzinfo=timezone.utc)
    ctx = build_eval_context(
        "2026-06-23", symbol="600519.SH", analysis_mode="post_market", now=post_close
    )
    raw = "600519.SH 近5日主力资金\n数据截止 2026-06-20\n"
    meta = evaluate_source("individual_fund_flow", raw, ctx)
    assert meta.status == "stale"


def test_event_driven_outside_window_is_stale():
    now = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)
    ctx = build_eval_context("2026-06-23", symbol="600519.SH", analysis_mode="historical", now=now)
    raw = "2026-06-01 09:30 旧闻标题\n2026-06-01 10:00 更多内容"
    meta = evaluate_source("news", raw, ctx)
    assert meta.status == "stale"


def test_disclosure_one_quarter_lag_is_warning():
    ctx = build_eval_context("2026-06-23", symbol="600519.SH", analysis_mode="historical")
    expected = expected_disclosure_period("2026-06-23")
    assert expected == "2026-Q1"
    assert disclosure_period_lag("2025-Q4", expected) == 1
    meta = evaluate_source("fundamentals", "2025-Q4 财报摘要", ctx)
    assert meta.status == "warning"


def test_aggregate_critical_error_overall():
    pool = {
        "stock_data": {
            "source_key": "stock_data",
            "status": "error",
            "criticality": "critical",
            "issue_type": "fetch_error",
            "error_code": "provider_timeout",
            "error_message": "timeout",
        },
        "news": {"source_key": "news", "status": "fresh", "criticality": "informational"},
    }
    summary = build_freshness_summary(pool, "2026-06-23", symbol="600519.SH", analysis_mode="historical")
    assert summary["overall_status"] == "error"
    assert len(summary["fetch_errors"]) == 1
    assert clamp_confidence(90, "error") == 40


def test_aggregate_important_stale_overall_warning():
    pool = {
        "stock_data": {
            "source_key": "stock_data",
            "status": "fresh",
            "criticality": "critical",
        },
        "board_fund_flow": {
            "source_key": "board_fund_flow",
            "status": "stale",
            "criticality": "important",
            "anchor_actual": "2026-06-20",
            "anchor_expected": "2026-06-23",
        },
    }
    summary = build_freshness_summary(pool, "2026-06-23", symbol="600519.SH", analysis_mode="historical")
    assert summary["overall_status"] == "warning"
    assert clamp_confidence(90, "warning") == 70
    assert clamp_confidence(90, "stale") == 50


def test_aggregate_informational_stale_keeps_fresh():
    pool = {
        "stock_data": {
            "source_key": "stock_data",
            "status": "fresh",
            "criticality": "critical",
            "anchor_actual": "2026-06-23",
            "anchor_expected": "2026-06-23",
        },
        "hot_stocks_xq": {
            "source_key": "hot_stocks_xq",
            "status": "stale",
            "criticality": "informational",
        },
    }
    summary = build_freshness_summary(pool, "2026-06-23", symbol="600519.SH", analysis_mode="historical")
    assert summary["overall_status"] == "fresh"


def test_historical_expected_anchor_equals_trade_date():
    anchor = resolve_expected_anchor("2026-06-20", analysis_mode="historical", symbol="600519.SH")
    assert anchor == "2026-06-20"


def test_intraday_expected_anchor_is_previous_trading_day():
    now = datetime(2026, 6, 23, 10, 30, tzinfo=CN_TZ)
    anchor = resolve_expected_anchor("2026-06-23", analysis_mode="intraday", now=now)
    assert anchor < "2026-06-23"


def test_is_in_grace_window_post_market_only():
    assert is_in_grace_window(grace_minutes=120, analysis_mode="post_market") in (True, False)
    assert is_in_grace_window(grace_minutes=120, analysis_mode="historical") is False


def test_parse_latest_event_timestamp():
    ts = parse_latest_event_timestamp("2026-06-22 10:00 标题\n2026-06-23 09:15 更新")
    assert ts is not None
    assert ts.day == 23


def test_degraded_pool_marks_missing_sources_error():
    pool = build_degraded_freshness_pool("2026-06-23", symbol="600519.SH")
    assert pool["stock_data"]["status"] == "error"
    summary = build_freshness_summary(pool, "2026-06-23", symbol="600519.SH", analysis_mode="historical")
    assert summary["overall_status"] == "error"


def test_section_freshness_tooltip_uses_lag_note():
    summary = {
        "section_impacts": {"smart_money_report": "stale"},
        "datasets": [
            {
                "source_key": "individual_fund_flow",
                "status": "stale",
                "lag_note": "数据截止 2026-06-20，预期至少 2026-06-23",
            }
        ],
    }
    tip = section_freshness_tooltip(summary, "smart_money_report", "stale")
    assert "2026-06-20" in (tip or "")


def test_attach_freshness_clamps_confidence_and_sets_status():
    pool = {
        "stock_data": {
            "source_key": "stock_data",
            "status": "stale",
            "criticality": "critical",
            "anchor_actual": "2026-06-20",
            "anchor_expected": "2026-06-23",
        }
    }
    result = {"confidence": 88}
    out = attach_freshness_to_result(result, pool, "2026-06-23", "600519.SH")
    assert out["freshness_status"] == "stale"
    assert out["confidence"] == 50
    assert out["freshness_summary"]["overall_status"] == "stale"


def test_attach_freshness_empty_pool_uses_degraded():
    result = {"confidence": 90}
    out = attach_freshness_to_result(result, None, "2026-06-23", "600519.SH")
    assert out["freshness_status"] == "error"
    assert out["confidence"] == 40


def test_inject_freshness_into_state_from_cache():
    state = {"freshness_pool": {}}
    collector = SimpleNamespace(
        _cache={
            "600519.SH_2026-06-23": {
                "stock_data": "date,open\n2026-06-23,10\n",
                "news": "2026-06-23 09:00 标题",
            }
        },
        get=lambda ticker, td: collector._cache.get(f"{ticker}_{td}"),
    )
    inject_freshness_into_state(state, collector, "600519.SH", "2026-06-23")
    assert state["freshness_pool"]["stock_data"]["status"] == "fresh"


def test_resolve_freshness_pool_prefers_graph_state():
    state_pool = {"stock_data": {"source_key": "stock_data", "status": "error"}}
    resolved = resolve_freshness_pool(
        {"freshness_pool": state_pool},
        None,
        "600519.SH",
        "2026-06-23",
    )
    assert resolved is state_pool


def test_build_freshness_pool_from_collector_results():
    results = {
        "stock_data": (
            "date,open,high,low,close,volume\n"
            "2026-06-22,10,11,9,10.5,1000\n"
            "2026-06-23,10.2,11.2,9.8,10.8,1200\n"
        ),
        "fund_flow_individual": "600519 数据截止 2026-06-23\n",
        "news": "2026-06-23 10:00 公司公告",
    }
    pool = build_freshness_pool(results, "2026-06-23", symbol="600519.SH")
    assert pool["stock_data"]["status"] == "fresh"
    assert pool["indicators"]["status"] == pool["stock_data"]["status"]
    assert "individual_fund_flow" in pool


def test_evaluate_collector_results_marks_missing_critical_as_error():
    pool = evaluate_collector_results({}, "2026-06-23", symbol="600519.SH")
    assert pool["stock_data"]["status"] == "error"
    assert pool["individual_fund_flow"]["status"] == "error"


def test_format_freshness_context_for_sources_error_and_stale():
    pool = {
        "individual_fund_flow": {
            "status": "error",
            "error_code": "provider_timeout",
            "error_message": "东方财富接口超时",
        },
        "lhb_detail": {
            "status": "stale",
            "anchor_actual": "2026-06-20",
            "anchor_expected": "2026-06-23",
        },
    }
    ctx = format_freshness_context_for_sources(pool, ["individual_fund_flow", "lhb_detail"])
    assert "individual_fund_flow" in ctx
    assert "东方财富接口超时" in ctx
    assert "lhb_detail" in ctx
    assert "请勿" in ctx


def test_format_freshness_context_summary_warning():
    summary = {
        "overall_status": "warning",
        "overall_label": "部分滞后",
        "confidence_cap": 70,
        "blocking_sources": [],
        "fetch_errors": [],
    }
    text = format_freshness_context_summary(summary)
    assert "部分滞后" in text
    assert "70" in text


def test_summary_includes_warning_sources_for_grace():
    pool = {
        "stock_data": {"source_key": "stock_data", "status": "fresh", "criticality": "critical"},
        "individual_fund_flow": {
            "source_key": "individual_fund_flow",
            "status": "warning",
            "criticality": "critical",
            "lag_note": "grace 缓冲",
        },
    }
    summary = build_freshness_summary(pool, "2026-06-23", symbol="600519.SH", analysis_mode="historical")
    assert summary["overall_status"] == "warning"
    assert any(w["source_key"] == "individual_fund_flow" for w in summary["warning_sources"])


def test_enrich_freshness_summary_adds_report_age_note():
    old_created = "2020-01-02T08:00:00+00:00"
    summary = {"overall_status": "fresh", "report_age_note": None}
    enriched = enrich_freshness_summary_for_display(summary, old_created)
    assert enriched is not None
    assert enriched.get("report_age_note")


def test_email_render_lists_fetch_error_source():
    if importlib.util.find_spec("markdown") is None:
        pytest.skip("markdown not installed in test env")

    mod = _load_email_renderer()
    report = SimpleNamespace(
        symbol="600519",
        trade_date="2026-06-23",
        decision="HOLD",
        direction="中性",
        confidence=40,
        target_price=None,
        stop_loss_price=None,
        market_report=None,
        sentiment_report=None,
        news_report=None,
        fundamentals_report=None,
        macro_report=None,
        smart_money_report=None,
        game_theory_report=None,
        risk_items=[],
        key_metrics=[],
        final_trade_decision="观望",
        freshness_status="error",
        result_data={
            "freshness_summary": {
                "overall_status": "error",
                "fetch_errors": [
                    {
                        "source_key": "individual_fund_flow",
                        "error_code": "provider_timeout",
                        "error_message": "东方财富接口超时",
                    }
                ],
            }
        },
    )
    html = mod.render_report_html(report)
    assert "数据异常" in html
    assert "individual_fund_flow" in html
    assert "东方财富接口超时" in html


def test_report_summary_columns_include_freshness_status():
    report_service_path = Path(__file__).resolve().parents[1] / "api" / "services" / "report_service.py"
    source = report_service_path.read_text(encoding="utf-8")
    assert "REPORT_SUMMARY_COLUMNS" in source
    assert "ReportDB.freshness_status" in source


def test_intraday_only_realtime_not_applicable_on_historical_analysis():
    pool = evaluate_collector_results(
        {"stock_data": "date,close\n2026-06-26,10.0\n"},
        "2026-06-26",
        analysis_mode="historical",
    )
    rt = pool["realtime_quotes"]
    assert rt["status"] == "not_applicable"
    assert rt.get("issue_type") is None
    summary = build_freshness_summary(pool, "2026-06-26", analysis_mode="historical")
    assert all(e.get("source_key") != "realtime_quotes" for e in summary.get("fetch_errors") or [])


def test_board_fund_flow_success_with_snapshot_date_is_fresh():
    text = format_board_fund_flow_ranking(
        pd.DataFrame({"名称": ["面板"], "今日主力净流入-净额": [1.0]}),
        snapshot_date="2026-06-26",
    )
    pool = evaluate_collector_results(
        {"fund_flow_board": text, "stock_data": "date,close\n2026-06-26,10.0\n"},
        "2026-06-26",
        analysis_mode="historical",
    )
    assert pool["board_fund_flow"]["status"] == "fresh"


# ══ 采集范围 vs. 契约范围：假故障的回归测试 ══════════════════════════════════
# 事故（2026-09-24 起，每条报告）：`intraday_bars` / `chip_distribution` /
# `symbol_news` 是**工具/对话路径**的源，报告管线（DataCollector）从不采它们。
# 但它们一旦进了 contract.yaml，`evaluate_collector_results` 就把"采集结果里没有"
# 一律当成"该采没采到"→ error +「数据源返回空」→ overall 变「数据异常」→
# 置信度被压到 40，并被模型写进终审正文（"L0：intraday_bars 未恢复 → 0% 开仓"）。
# 而那两个源当时实测完全可用。下面几条用例把"没采 ≠ 取数失败"钉住。


def test_contract_scope_declares_pipeline_sources():
    """声明必须存在且非空——缺了它 `report_pipeline_sources()` 会回退到「全部契约源」，
    假故障会原样回来（回退是保守的，但不该被依赖）。"""
    from tradingagents.dataflows.freshness.contract import (
        all_contract_source_keys,
        report_pipeline_sources,
    )

    scope = report_pipeline_sources()
    assert scope, "contract.yaml 必须声明 report_pipeline_sources"
    assert scope <= set(all_contract_source_keys()), "声明里不能有未知源名"


def test_tool_path_sources_are_out_of_report_pipeline_scope():
    """工具/对话路径的源不该在报告采集范围内。"""
    from tradingagents.dataflows.freshness.contract import report_pipeline_sources

    scope = report_pipeline_sources()
    for key in ("intraday_bars", "chip_distribution", "symbol_news"):
        assert key not in scope, f"{key} 由工具路径消费，报告管线不采它"


def test_uncollected_out_of_scope_source_is_not_applicable_not_error():
    """报告里没采到的**范围外**源 → not_applicable，且不得冒充「数据源返回空」。"""
    pool = evaluate_collector_results(
        {"stock_data": "date,close\n2026-06-26,10.0\n"},
        "2026-06-26",
        analysis_mode="historical",
    )
    for key in ("intraday_bars", "chip_distribution", "symbol_news"):
        meta = pool[key]
        assert meta["status"] == "not_applicable", f"{key} 不该被判成故障"
        assert meta.get("issue_type") is None
        assert meta.get("error_message") != "数据源返回空"
        assert "不在本采集管线范围内" in (meta.get("note") or "")


def test_out_of_scope_sources_do_not_trigger_data_anomaly_banner():
    """端到端：整份摘要不得因为这三个源而变「数据异常」，置信度也不得被封到 40。"""
    bars = "date,close\n2026-06-25,10.0\n2026-06-26,10.5\n"
    results = {
        "stock_data": bars,
        "news": "2026-06-26 10:00 公司公告",
        "global_news": "2026-06-26 宏观",
        "fund_flow_board": "板块资金流\n快照日期 2026-06-26",
        "fund_flow_individual": "600519 数据截止 2026-06-26\n",
        "lhb": "600519.SH 在 2026-06-26 无龙虎榜数据（非异动日属正常）。",
        "insider_transactions": "2026-06-20 高管变动",
        "zt_pool": "涨停池 请求日期 2026-06-26",
        "hot_stocks": "2026-06-26 09:30 雪球热榜",
        "fundamentals": "最新披露期：2026-Q2",
        "balance_sheet": "最新披露期：2026-Q2",
        "cashflow": "最新披露期：2026-Q2",
        "income_statement": "最新披露期：2026-Q2",
    }
    pool = build_freshness_pool(results, "2026-06-26", analysis_mode="historical")
    summary = build_freshness_summary(pool, "2026-06-26", analysis_mode="historical")

    assert summary["overall_status"] != "error", (
        f"范围外未采集的源不该把整份报告判成数据异常：{summary['fetch_errors']}"
    )
    assert summary["confidence_cap"] > 40
    offenders = {"intraday_bars", "chip_distribution", "symbol_news"}
    assert not offenders & {e["source_key"] for e in summary["fetch_errors"]}
    assert all(
        s.get("source_key") not in offenders for s in summary.get("blocking_sources") or []
    )


def test_in_scope_missing_source_still_errors():
    """守卫不能被我这次改动削弱：**范围内**的源没采到，仍然必须是 error。"""
    from tradingagents.dataflows.freshness.contract import report_pipeline_sources

    pool = evaluate_collector_results({}, "2026-06-26")
    for key in ("stock_data", "individual_fund_flow"):
        assert key in report_pipeline_sources()
        assert pool[key]["status"] == "error"
        assert pool[key]["issue_type"] == "fetch_error"


def test_scope_parameter_can_be_overridden():
    """自定义管线可以显式给出自己的采集范围（默认才用报告管线那张表）。"""
    pool = evaluate_collector_results(
        {"stock_data": "date,close\n2026-06-26,10.0\n"},
        "2026-06-26",
        analysis_mode="historical",
        scope=("stock_data", "intraday_bars"),
    )
    assert "intraday_bars" in pool
    assert pool["intraday_bars"]["status"] != "not_applicable", "显式声明在范围内就不再是不适用"


# ══ 契约的 empty_ok 必须真的生效 ══════════════════════════════════════════════


def test_empty_ok_flag_is_honoured_when_source_answers_no_data():
    """`empty_ok: true` 曾是死配置（只有龙虎榜文案匹配生效），现在真的被读。"""
    ctx = build_eval_context("2026-06-23", symbol="600519.SH", analysis_mode="historical")
    meta = evaluate_source("lhb_detail", "无数据", ctx, trade_date="2026-06-23")
    assert meta.status == "empty_ok"
    assert meta.empty_ok is True


def test_empty_ok_does_not_swallow_a_missing_answer():
    """收紧条件：没有回包（None）意味着采集方**根本没拿到答案**，不能被 empty_ok 抹平。"""
    ctx = build_eval_context("2026-06-23", symbol="600519.SH", analysis_mode="historical")
    assert evaluate_source("lhb_detail", None, ctx, trade_date="2026-06-23").status == "error"
    assert evaluate_source("lhb_detail", "", ctx, trade_date="2026-06-23").status == "error"


def test_empty_ok_does_not_apply_to_sources_without_the_flag():
    ctx = build_eval_context("2026-06-23", symbol="600519.SH", analysis_mode="historical")
    meta = evaluate_source("stock_data", "无数据", ctx, trade_date="2026-06-23")
    assert meta.status == "error", "没声明 empty_ok 的源，空回包仍是故障"

