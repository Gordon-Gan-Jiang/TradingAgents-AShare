"""``get_lhb_detail``: the symbol must never be passed where a date belongs.

## The bug this file pins

``CnAkshareProvider.get_lhb_detail`` tried a ladder of call shapes because akshare's
signature "varies between versions". It does not. The installed akshare is::

    stock_lhb_detail_em(start_date="20230403", end_date="20230417")   # NO symbol parameter

The ladder's first attempt ``func(code, date, date)`` raises ``TypeError`` (3 args, 2
params), so execution fell through to ``func(code, date)`` -- which akshare accepts,
binding ``start_date=code``. akshare then does::

    start_date = "-".join([start_date[:4], start_date[4:6], start_date[6:]])
    # "600519.SH" -> "6005-19-.SH"
    filter = f"(TRADE_DATE<='{end_date}')(TRADE_DATE>='{start_date}')"

``"6005-19-.SH"`` sorts before every ISO date, so the filter matched essentially the
entire LHB history. One call therefore crawled the **whole market, 402 pages x 5000
rows**, taking minutes and dying part-way through with::

    ChunkedEncodingError: Response ended prematurely   (or ProxyError under a proxy)

Two consequences, both user-visible:

* **every** deep analysis paid this crawl, and usually failed it;
* the returned frame was ``df.head(20)`` of *other companies'* LHB rows, printed under
  the heading ``"<symbol> 龙虎榜明细（<date>）"`` -- silently mislabelled data fed to
  the smart-money analyst.

## Why the old test did not catch it

``tests/test_cn_market_data_providers.py::test_akshare_lhb_uses_positional_args``
invented a three-argument fake (``def stock_lhb_detail_em(code, start_date, end_date)``)
that akshare does not have, then asserted ``("600176", "2026-06-16", "2026-06-16")`` --
i.e. it asserted the defect. It was green for as long as the bug existed, because it
tested the code's own assumption instead of the real library.

**So this file tests against the real installed akshare signature**, not a
hand-written one. That is the point: a double that mirrors our guess cannot falsify it.
"""

import inspect
from types import SimpleNamespace

import pandas as pd
import pytest

from tradingagents.dataflows.providers.cn_akshare_provider import CnAkshareProvider


# ---------------------------------------------------------------------------
# contract: the real library, not our idea of it
# ---------------------------------------------------------------------------

def test_real_akshare_lhb_signature_is_a_pure_date_window():
    """If this fails, akshare changed and the call shape below must be revisited."""
    import akshare

    params = list(inspect.signature(akshare.stock_lhb_detail_em).parameters)
    assert params == ["start_date", "end_date"], (
        f"akshare.stock_lhb_detail_em signature changed: {params}"
    )
    assert not any(p in params for p in ("code", "symbol", "stock", "secu_code")), (
        "a symbol parameter appeared; get_lhb_detail may need a per-symbol endpoint"
    )


def _lhb_frame(codes, *, date="2026-06-16"):
    """A frame shaped like akshare's real output (Chinese column names)."""
    return pd.DataFrame([
        {
            "序号": i + 1, "代码": c, "名称": f"股票{c}", "上榜日": date,
            "解读": "日涨幅偏离值达7%", "收盘价": 10.0 + i, "涨跌幅": 5.0,
            "龙虎榜净买额": 1e7, "龙虎榜买入额": 2e7, "龙虎榜卖出额": 1e7,
            "龙虎榜成交额": 3e7, "市场总成交额": 1e9,
            "净买额占总成交比": 1.0, "成交额占总成交比": 3.0,
            "换手率": 2.0, "流通市值": 5e9,
        }
        for i, c in enumerate(codes)
    ])


class _FakeAk:
    """Records how it was called; rejects any symbol-shaped date like akshare would."""

    def __init__(self, frame):
        self._frame = frame
        self.calls = []

    def stock_lhb_detail_em(self, start_date="20230403", end_date="20230417"):
        self.calls.append({"start_date": start_date, "end_date": end_date})
        # Mirror akshare: it slices the date into YYYY-MM-DD. A symbol would be mangled
        # into nonsense here, so assert the caller handed us something date-shaped.
        for label, value in (("start_date", start_date), ("end_date", end_date)):
            assert len(str(value)) == 8 and str(value).isdigit(), (
                f"{label}={value!r} is not a YYYYMMDD date -- a symbol must never land here"
            )
        return self._frame


def _provider(fake_ak):
    provider = CnAkshareProvider()
    provider._ak = lambda: fake_ak  # bypass the lazy import/cache
    return provider


# ---------------------------------------------------------------------------
# the defect
# ---------------------------------------------------------------------------

def test_the_symbol_is_never_passed_as_a_date():
    """The regression: start_date used to receive '600176'."""
    fake = _FakeAk(_lhb_frame(["600176"]))
    _provider(fake).get_lhb_detail("600176.SH", "2026-06-16")

    assert fake.calls, "akshare was never called"
    call = fake.calls[0]
    assert call["start_date"] == "20260616"
    assert call["end_date"] == "20260616"
    assert "600176" not in (call["start_date"], call["end_date"]), \
        "the stock code must be filtered after the fetch, never sent as a date"


def test_a_single_day_window_is_requested_not_a_wide_one():
    """A one-day window is what keeps this call small (0.3s vs a 402-page crawl)."""
    fake = _FakeAk(_lhb_frame(["600176"]))
    _provider(fake).get_lhb_detail("600176.SH", "2026-06-16")
    assert fake.calls[0]["start_date"] == fake.calls[0]["end_date"], \
        "start and end must be the same day"


def test_only_the_requested_symbol_is_reported():
    """A whole-market frame must be filtered, not dumped under the symbol's heading."""
    frame = _lhb_frame(["000001", "600176", "600519"])
    out = _provider(_FakeAk(frame)).get_lhb_detail("600176.SH", "2026-06-16")

    # Require the success path first: otherwise a degraded "龙虎榜数据获取失败" string
    # would satisfy the negative assertions below for the wrong reason.
    assert "龙虎榜明细" in out and "获取失败" not in out, f"fetch did not succeed: {out[:200]}"
    assert "600176" in out
    assert "600519" not in out, "another company's LHB rows leaked into this symbol's report"
    assert "000001" not in out


def test_a_day_without_lhb_data_says_so_instead_of_showing_others():
    """600519 was not on the LHB on 2026-09-29; the honest answer is the empty-day note."""
    frame = _lhb_frame(["000001", "600519"])
    out = _provider(_FakeAk(frame)).get_lhb_detail("600176.SH", "2026-06-16")

    assert "无龙虎榜数据" in out
    # the old code printed the market dump here, so this is the substantive assertion
    assert "000001" not in out and "600519" not in out


def test_empty_frame_is_also_the_normal_no_data_case():
    out = _provider(_FakeAk(_lhb_frame([]))).get_lhb_detail("600176.SH", "2026-06-16")
    assert "无龙虎榜数据" in out


def test_symbol_with_suffix_and_without_both_normalise():
    for symbol in ("600176.SH", "600176"):
        fake = _FakeAk(_lhb_frame(["600176"]))
        out = _provider(fake).get_lhb_detail(symbol, "2026-06-16")
        assert "600176" in out
        assert fake.calls[0]["start_date"] == "20260616"


# ---------------------------------------------------------------------------
# guards that must keep holding
# ---------------------------------------------------------------------------

def test_a_missing_akshare_function_degrades_to_a_string():
    provider = CnAkshareProvider()
    provider._ak = lambda: SimpleNamespace()
    out = provider.get_lhb_detail("600176.SH", "2026-06-16")
    assert "龙虎榜数据获取失败" in out


def test_network_failure_degrades_to_a_string_not_an_exception():
    """This path is why a fetch failure shows up as report text instead of a crash."""
    class _Boom:
        def stock_lhb_detail_em(self, **kwargs):
            raise ConnectionError("ChunkedEncodingError: Response ended prematurely")

    provider = CnAkshareProvider()
    provider._ak = lambda: _Boom()
    out = provider.get_lhb_detail("600176.SH", "2026-06-16")
    assert "龙虎榜数据获取失败" in out
    assert "ChunkedEncodingError" in out


@pytest.mark.parametrize("date", ["2026-06-16", "20260616"])
def test_both_date_formats_are_accepted(date):
    fake = _FakeAk(_lhb_frame(["600176"]))
    _provider(fake).get_lhb_detail("600176.SH", date)
    assert fake.calls[0]["start_date"] == "20260616"
