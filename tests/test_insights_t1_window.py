"""T+1 evaluation window: pre-open / in-session / post-close close→close."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from api.services import insights_t1_service as m

CN = ZoneInfo("Asia/Shanghai")


def _cn(y, mo, d, hh, mm):
    return datetime(y, mo, d, hh, mm, tzinfo=CN).astimezone(timezone.utc)


def test_post_close_evening_uses_same_day_close_to_next_day() -> None:
    """6/1 22:12 → P0=6/1 close, P1=6/2 close, bucket=6/2."""
    created = _cn(2026, 6, 1, 22, 12)
    p0, p1 = m._report_price_window("2026-06-01", created)
    assert p0 == "2026-06-01"
    assert p1 == "2026-06-02"
    assert m._report_signal_trade_date("2026-06-01", created) == "2026-06-02"


def test_pre_open_uses_prev_close_to_today_close() -> None:
    """6/2 08:00 → P0=6/1, P1=6/2, bucket=6/2."""
    created = _cn(2026, 6, 2, 8, 0)
    p0, p1 = m._report_price_window("2026-06-02", created)
    assert p0 == "2026-06-01"
    assert p1 == "2026-06-02"
    assert m._report_signal_trade_date("2026-06-02", created) == "2026-06-02"


def test_intraday_uses_today_close_to_next_close() -> None:
    """6/1 09:57 → P0=6/1, P1=6/2 (not prior trading day)."""
    created = _cn(2026, 6, 1, 9, 57)
    p0, p1 = m._report_price_window("2026-06-01", created)
    assert p0 == "2026-06-01"
    assert p1 == "2026-06-02"
    assert m._report_signal_trade_date("2026-06-01", created) == "2026-06-02"


def test_signal_trade_date_post_close_buckets_predicted_day() -> None:
    created = _cn(2026, 5, 25, 15, 5)
    assert m._report_signal_trade_date("2026-05-25", created) == "2026-05-26"


def test_pre_open_signal_buckets_today() -> None:
    created = _cn(2026, 5, 26, 9, 0)
    assert m._report_signal_trade_date("2026-05-26", created) == "2026-05-26"
