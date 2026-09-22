"""P1：基准/板块面板缓存的**新鲜度**。

## 为什么这个文件必须存在

这两个缓存此前是"文件存在就无条件返回"（`mainline_backtest.py:45` 与 `:91`），
于是 2026-08-31 写下的缓存被**永久**复用，导致：

    evaluate_t1_for_report 的 check_date <= trade_date 恒真
      → 静默 return []
      → mainline_t1_outcomes 0 行
      → 「昨日主线今日兑现了吗」永远答不出

而它**隐身**的原因正在测试结构里：`tests/test_mainline_t1.py` 与
`tests/test_mainline_backtest.py` **全部 mock 掉 `fetch_benchmark_close`**，
所以缓存这段逻辑从来没有被任何测试碰过。本文件补上这一段。

判据用**数据末日**而不是文件 mtime：mtime 只说"什么时候写的"，
而写的时候数据本身可能已经旧了。
"""
from __future__ import annotations

import pandas as pd
import pytest

from tradingagents.dataflows import mainline_backtest as mb


# ---------------------------------------------------------------- 纯函数判据

def test_cache_is_stale_when_last_data_date_too_old():
    """末行日期超出 TTL → 陈旧。"""
    s = pd.Series([1.0, 2.0], index=["2026-08-28", "2026-08-31"])
    assert mb.cache_is_stale(s, today="2026-09-23", max_age_days=3) is True


def test_cache_is_fresh_within_ttl():
    """末行日期在 TTL 内 → 新鲜。"""
    s = pd.Series([1.0, 2.0], index=["2026-09-22", "2026-09-23"])
    assert mb.cache_is_stale(s, today="2026-09-23", max_age_days=3) is False


def test_cache_freshness_boundary_is_inclusive():
    """恰好等于阈值 → **算新鲜**（边界不抖动）。

    today=2026-09-23, TTL=3 → cutoff=2026-09-20。
    末日 2026-09-20 不早于 cutoff → 新鲜。
    末日 2026-09-19 早于 cutoff → 陈旧。
    """
    at = pd.Series([1.0], index=["2026-09-20"])
    before = pd.Series([1.0], index=["2026-09-19"])
    assert mb.cache_is_stale(at, today="2026-09-23", max_age_days=3) is False
    assert mb.cache_is_stale(before, today="2026-09-23", max_age_days=3) is True


def test_cache_freshness_spans_weekend():
    """跨周末不算陈旧：周一(9-23)看周五(9-20)的缓存，TTL=3 时仍新鲜。

    这是真实场景——周末不产生新数据，若按自然日粗暴判定会把每个周一
    都判成"缓存坏了"，天天触发一次 90 个板块的重抓。
    """
    friday = pd.Series([1.0], index=["2026-09-20"])
    assert mb.cache_is_stale(friday, today="2026-09-23", max_age_days=3) is False


def test_empty_cache_is_stale():
    """空缓存视为陈旧（不能拿空序列当"新鲜"）。"""
    assert mb.cache_is_stale(pd.Series(dtype=float), today="2026-09-23", max_age_days=3) is True


def test_unparsable_index_is_stale():
    """索引解析不出日期 → 视为陈旧，宁可重抓也不要拿它当依据。"""
    s = pd.Series([1.0], index=["not-a-date"])
    assert mb.cache_is_stale(s, today="2026-09-23", max_age_days=3) is True


# ------------------------------------------------------- 基准缓存的 IO 行为

class _FakeAk:
    """假 akshare 模块：记录被调用次数，返回可控的日线。"""

    def __init__(self, frame: pd.DataFrame | None, *, exc: Exception | None = None):
        self._frame = frame
        self._exc = exc
        self.calls = 0

    def stock_zh_index_daily_em(self, symbol: str):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return self._frame

    # 第二个候选源：让它在被测代码遍历候选源时不会被误当成"有实现"
    stock_zh_index_daily = None


def _bench_frame(dates: list[str]) -> pd.DataFrame:
    return pd.DataFrame({"date": dates, "close": [3000.0 + i for i in range(len(dates))]})


@pytest.fixture()
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "_CACHE_DIR", tmp_path)
    return tmp_path


def test_stale_benchmark_cache_triggers_refresh(cache_dir):
    """**核心回归**：陈旧缓存必须触发实时抓取，不能无条件复用。

    修复前：直接返回缓存，calls == 0 → 本测试失败。
    """
    stale = pd.Series([1.0, 2.0], index=["2026-08-28", "2026-08-31"])
    stale.to_csv(cache_dir / "benchmark_sh000300.csv")

    fake = _FakeAk(_bench_frame(["2026-09-22", "2026-09-23"]))
    out = mb.fetch_benchmark_close(fake, cache=True, today="2026-09-23", max_age_days=3)

    assert fake.calls == 1, "陈旧缓存没有触发实时抓取"
    assert str(out.index[-1]) == "2026-09-23", "没有用新抓到的数据"
    # 抓到新数据后应覆盖缓存，否则下一次还要再抓
    rewritten = pd.read_csv(cache_dir / "benchmark_sh000300.csv", index_col=0).iloc[:, 0]
    assert str(rewritten.index[-1]) == "2026-09-23"


def test_fresh_benchmark_cache_skips_refresh(cache_dir):
    """新鲜缓存**必须**秒回，不得发网络请求（这是缓存的既有承诺）。"""
    fresh = pd.Series([1.0, 2.0], index=["2026-09-22", "2026-09-23"])
    fresh.to_csv(cache_dir / "benchmark_sh000300.csv")

    fake = _FakeAk(_bench_frame(["2026-09-24"]))
    out = mb.fetch_benchmark_close(fake, cache=True, today="2026-09-23", max_age_days=3)

    assert fake.calls == 0, "新鲜缓存不该触发抓取"
    assert len(out) == 2


def test_stale_benchmark_falls_back_to_cache_when_refresh_fails(cache_dir):
    """刷新失败时**仍然返回陈旧缓存**：可用性优先于新鲜度。

    但要保证（a）不抛异常，（b）返回的是那份旧数据——让上层有机会
    判断"拿到的是旧的"并声明出来（见 P2/T3）。
    """
    stale = pd.Series([1.0, 2.0], index=["2026-08-28", "2026-08-31"])
    stale.to_csv(cache_dir / "benchmark_sh000300.csv")

    fake = _FakeAk(None, exc=ConnectionError("东财封禁"))
    out = mb.fetch_benchmark_close(fake, cache=True, today="2026-09-23", max_age_days=3)

    assert fake.calls >= 1, "应当尝试过刷新"
    assert len(out) == 2, "刷新失败后没有回退到陈旧缓存"
    assert str(out.index[-1]) == "2026-08-31"


def test_refresh_failure_without_cache_still_raises(cache_dir):
    """没有缓存又抓不到 → 必须抛，不能返回空序列。

    返回空序列正是 P2 静默失败的源头（`bench = pd.Series(dtype=float)`）。
    """
    fake = _FakeAk(None, exc=ConnectionError("东财封禁"))
    with pytest.raises(Exception):
        mb.fetch_benchmark_close(fake, cache=True, today="2026-09-23", max_age_days=3)


def test_cache_false_always_fetches(cache_dir):
    """cache=False 的语义不变：永远实时抓。"""
    fresh = pd.Series([1.0], index=["2026-09-23"])
    fresh.to_csv(cache_dir / "benchmark_sh000300.csv")

    fake = _FakeAk(_bench_frame(["2026-09-23", "2026-09-24"]))
    out = mb.fetch_benchmark_close(fake, cache=False, today="2026-09-23", max_age_days=3)

    assert fake.calls == 1
    assert len(out) == 2


# --------------------------------------------------- 板块面板缓存的 IO 行为

def _patch_panel_sources(monkeypatch, *, calls: dict, frame_dates=None, raise_exc=None):
    """把面板抓取的两个 provider 函数换成替身，记录调用次数。"""
    from tradingagents.dataflows.providers import cn_akshare_provider as prov

    def fake_summary(ak):
        calls["summary"] = calls.get("summary", 0) + 1
        return pd.DataFrame({"name": ["板块A", "板块B"]})

    def fake_board_index(ak, name, sector_type):
        calls["board"] = calls.get("board", 0) + 1
        if raise_exc is not None:
            raise raise_exc
        dates = frame_dates or ["2026-09-22", "2026-09-23"]
        return pd.DataFrame({"date": dates, "close": [100.0 + i for i in range(len(dates))]})

    monkeypatch.setattr(prov, "fetch_ths_industry_summary_df", fake_summary)
    monkeypatch.setattr(prov, "fetch_ths_board_index_df", fake_board_index)


def test_stale_panel_cache_triggers_refetch(cache_dir, monkeypatch):
    """**核心回归（第二处）**：面板缓存同样不能无条件复用。

    修复前：直接返回缓存，calls 为空 → 本测试失败。
    """
    from tradingagents.dataflows.mainline_backtest import fetch_industry_panel

    stale = pd.DataFrame({"板块A": [1.0, 2.0]}, index=["2026-08-28", "2026-08-31"])
    stale.to_csv(cache_dir / "ths_industry_panel.csv")

    calls: dict = {}
    _patch_panel_sources(monkeypatch, calls=calls)
    out = fetch_industry_panel(None, cache=True, today="2026-09-23", max_age_days=3)

    assert calls.get("summary") == 1 and calls.get("board"), "陈旧面板缓存没有触发重抓"
    assert str(out.index[-1]) == "2026-09-23"


def test_fresh_panel_cache_skips_refetch(cache_dir, monkeypatch):
    """新鲜面板缓存必须秒回，不得重抓 90 个板块。"""
    from tradingagents.dataflows.mainline_backtest import fetch_industry_panel

    fresh = pd.DataFrame({"板块A": [1.0, 2.0]}, index=["2026-09-22", "2026-09-23"])
    fresh.to_csv(cache_dir / "ths_industry_panel.csv")

    calls: dict = {}
    _patch_panel_sources(monkeypatch, calls=calls)
    out = fetch_industry_panel(None, cache=True, today="2026-09-23", max_age_days=3)

    assert calls == {}, "新鲜缓存不该触发任何抓取"
    assert list(out.columns) == ["板块A"]


def test_stale_panel_falls_back_to_cache_when_refetch_fails(cache_dir, monkeypatch):
    """重抓失败时回退到陈旧面板，不抛异常。"""
    from tradingagents.dataflows.mainline_backtest import fetch_industry_panel

    stale = pd.DataFrame({"板块A": [1.0, 2.0]}, index=["2026-08-28", "2026-08-31"])
    stale.to_csv(cache_dir / "ths_industry_panel.csv")

    calls: dict = {}
    _patch_panel_sources(monkeypatch, calls=calls, raise_exc=ConnectionError("东财封禁"))
    out = fetch_industry_panel(None, cache=True, today="2026-09-23", max_age_days=3)

    assert str(out.index[-1]) == "2026-08-31", "重抓失败后没有回退到陈旧面板"


def test_corrupt_panel_cache_is_refetched(cache_dir, monkeypatch):
    """缓存文件损坏（读不出）→ 视为陈旧并重抓，而不是崩掉。"""
    from tradingagents.dataflows.mainline_backtest import fetch_industry_panel

    (cache_dir / "ths_industry_panel.csv").write_text("", encoding="utf-8")

    calls: dict = {}
    _patch_panel_sources(monkeypatch, calls=calls)
    out = fetch_industry_panel(None, cache=True, today="2026-09-23", max_age_days=3)

    assert calls.get("summary") == 1, "损坏缓存没有触发重抓"
    assert len(out) == 2
