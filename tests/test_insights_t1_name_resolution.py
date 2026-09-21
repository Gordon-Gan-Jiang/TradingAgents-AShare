"""Unit tests for T+1 depth-report row display name resolution."""

from datetime import datetime, timezone
import api.main as api_main
from unittest.mock import patch

from api.services import insights_t1_service as m


def test_security_name_from_result_data_rejects_ticker_like() -> None:
    assert m._security_name_from_result_data({}) is None
    assert (
        m._security_name_from_result_data(
            {"instrument_context": {"symbol": "600519.SH", "security_name": "600519.SH"}}
        )
        is None
    )
    assert (
        m._security_name_from_result_data(
            {"instrument_context": {"symbol": "600519.SH", "security_name": "600519.SH"}}
        )
        is None
    )
    assert (
        m._security_name_from_result_data(
            {"instrument_context": {"symbol": "600519.SH", "security_name": "600519"}}
        )
        is None
    )
    assert (
        m._security_name_from_result_data(
            {"instrument_context": {"symbol": "600519.SH", "security_name": "贵州茅台"}}
        )
        == "贵州茅台"
    )


def test_resolve_depth_report_display_name_priority() -> None:
    rd = {"instrument_context": {"symbol": "600519.SH", "security_name": "贵州茅台"}}
    assert (
        m._resolve_depth_report_display_name(
            "600519.SH",
            portfolio_name="  持仓简称  ",
            result_data=rd,
        )
        == "持仓简称"
    )
    assert (
        m._resolve_depth_report_display_name(
            "600519.SH",
            portfolio_name=None,
            result_data=rd,
        )
        == "贵州茅台"
    )
    with patch.object(m, "_cn_listing_name_for_symbol", return_value="贵州茅台"):
        assert (
            m._resolve_depth_report_display_name(
                "600519.SH",
                portfolio_name=None,
                result_data=None,
            )
            == "贵州茅台"
        )


def test_cn_listing_name_uses_normalize_when_raw_key_missing() -> None:
    """Reverse map keys are normalized; bare 6-digit codes must still resolve."""
    with patch.object(api_main, "_get_reverse_stock_map", return_value={"600519.SH": "贵州茅台"}):
        with patch.object(api_main, "_normalize_symbol", return_value="600519.SH"):
            assert m._cn_listing_name_for_symbol("600519") == "贵州茅台"


def test_has_position_snapshot_uses_report_user_context_snapshot() -> None:
    assert m._has_position_snapshot({"user_context": {"current_position": 100}}) is True
    assert m._has_position_snapshot({"user_context": {"current_position_pct": 12.5}}) is True
    assert m._has_position_snapshot({"user_context": {"current_position": 0, "current_position_pct": 0}}) is False
    assert m._has_position_snapshot({"user_context": {}}) is False


def test_resolve_t1_trade_date_always_uses_next_trading_day() -> None:
    with patch.object(m, "next_cn_trading_day", return_value="2026-05-26"):
        assert m._resolve_t1_trade_date("2026-05-25") == "2026-05-26"


def test_signal_trade_date_from_created_post_close_buckets_p1() -> None:
    created = datetime(2026, 5, 25, 7, 5, tzinfo=timezone.utc)  # 15:05 CST
    with patch.object(m, "is_cn_trading_day", return_value=True), patch.object(
        m, "next_cn_trading_day", return_value="2026-05-26"
    ):
        assert m._signal_trade_date_from_created(created) == "2026-05-26"


def test_report_signal_trade_date_post_close_buckets_next_trading_day() -> None:
    created = datetime(2026, 5, 25, 7, 1, tzinfo=timezone.utc)  # 15:01 CST
    with patch.object(m, "is_cn_trading_day", return_value=True), patch.object(
        m, "next_cn_trading_day", return_value="2026-05-26"
    ):
        assert m._report_signal_trade_date("2026-05-25", created) == "2026-05-26"
