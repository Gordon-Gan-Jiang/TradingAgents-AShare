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
