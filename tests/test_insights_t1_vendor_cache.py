"""T+1 refresh vendor batching — no LLM, fewer OHLC pulls per symbol per cycle."""

from unittest.mock import patch

from api.services.insights_t1_service import T1VendorSeriesCache


def test_prefetch_pairs_one_vendor_call_covers_multiple_signal_dates():
    vendor_calls: list[tuple[str, str, str]] = []

    def fake_fetch(sym: str, start: str, end: str) -> dict[str, float]:
        vendor_calls.append((sym, start, end))
        return {
            "2024-01-02": 100.0,
            "2024-01-03": 101.0,
            "2024-06-05": 200.0,
            "2024-06-06": 201.0,
        }

    with patch("api.services.insights_t1_service._fetch_close_map", side_effect=fake_fetch):
        cache = T1VendorSeriesCache()
        cache.prefetch_pairs(
            [
                ("600519.SH", "2024-01-02", "2024-01-03"),
                ("600519.SH", "2024-06-05", "2024-06-06"),
            ]
        )

    assert len(vendor_calls) == 1
    sym0, fs, fe = vendor_calls[0]
    assert sym0 == "600519.SH"
    assert fs <= "2024-01-02"
    assert fe >= "2024-06-06"

    p1 = cache.lookup_closes("600519.SH", "2024-01-02", "2024-01-03")
    p2 = cache.lookup_closes("600519.SH", "2024-06-05", "2024-06-06")
    assert p1 == (100.0, 101.0)
    assert p2 == (200.0, 201.0)


def test_second_prefetch_widens_span_when_new_dates_needed():
    vendor_calls: list[tuple[str, str, str]] = []

    def fake_fetch(sym: str, start: str, end: str) -> dict[str, float]:
        vendor_calls.append((sym, start, end))
        if len(vendor_calls) == 1:
            return {"2024-01-02": 1.0, "2024-01-03": 1.1}
        return {
            "2024-01-02": 1.0,
            "2024-01-03": 1.1,
            "2024-08-10": 2.0,
            "2024-08-12": 2.05,
        }

    with patch("api.services.insights_t1_service._fetch_close_map", side_effect=fake_fetch):
        cache = T1VendorSeriesCache()
        cache.prefetch_pairs([("AAA", "2024-01-02", "2024-01-03")])
        cache.prefetch_pairs([("AAA", "2024-08-10", "2024-08-12")])

    assert len(vendor_calls) == 2
    b = cache.lookup_closes("AAA", "2024-08-10", "2024-08-12")
    assert b == (2.0, 2.05)
