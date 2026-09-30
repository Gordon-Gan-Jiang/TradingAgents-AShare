"""基准指数（沪深300）取数的兜底测试。

## 为什么这条路值得单独测

主线上涨评分要拿沪深300算**相对强弱**，而这条路此前只有 AKShare 一家
（东财 `stock_zh_index_daily_em` / 新浪 `stock_zh_index_daily`）。
实测东财整片不通（`docs/data-source-strategy.md`）——基准指数一取不到，
主线评分整块失效，且**不会报错**，只会给出退化的结论。

因此这里钉住两件事：
1. AKShare 正常时**仍然优先 AKShare**（不改变既有数字的含义）；
2. AKShare 全挂时**能落到 BaoStock** 并返回**同形**的帧
   （`{date, close}`）——两条路列名不同会让兜底"看起来成功、实际算错"。
"""

from __future__ import annotations

import pandas as pd
import pytest

from tradingagents.dataflows import mainline_scoring as ms


@pytest.fixture
def no_benchmark_cache(monkeypatch, tmp_path):
    """隔离缓存：避免上一次的缓存把兜底路径掩盖掉。"""
    monkeypatch.setenv("TA_MAINLINE_CACHE_DIR", str(tmp_path))
    return tmp_path


def _ak_module(*, raise_all: bool = False, empty: bool = False):
    """构造一个假的 akshare 模块。"""

    class _Ak:
        if not raise_all:

            @staticmethod
            def stock_zh_index_daily_em(symbol: str):
                if empty:
                    return pd.DataFrame()
                return pd.DataFrame(
                    {"date": ["2026-09-22", "2026-09-23"], "close": [4544.58, 4517.28]}
                )

            stock_zh_index_daily = stock_zh_index_daily_em

    return _Ak


# ------------------------------------------------------------- AKShare 优先


def test_akshare_is_used_first_when_it_works(no_benchmark_cache, monkeypatch):
    """既有行为不能变：AKShare 能用就绝不去碰 BaoStock。"""
    called: list[str] = []

    def _fail_baostock():
        called.append("baostock")
        raise AssertionError("AKShare 可用时不应该调用 BaoStock 兜底")

    monkeypatch.setattr(ms, "_fetch_benchmark_via_baostock", _fail_baostock, raising=False)

    frame = ms._default_fetch_benchmark(_ak_module())()

    assert called == []
    assert list(frame["close"]) == [4544.58, 4517.28]


# ------------------------------------------------------------ 兜底生效


def test_falls_back_to_baostock_when_akshare_raises(no_benchmark_cache, monkeypatch):
    """东财/新浪都挂时，必须落到 BaoStock——而不是让基准整块失效。"""

    def _ok_baostock():
        return pd.DataFrame({"close": [4544.58, 4517.28], "date": ["2026-09-22", "2026-09-23"]})

    monkeypatch.setattr(ms, "_fetch_benchmark_via_baostock", _ok_baostock, raising=False)

    frame = ms._default_fetch_benchmark(_ak_module(raise_all=True))()

    assert list(frame.columns) == ["close", "date"], "兜底帧必须与 AKShare 分支同形"
    assert list(frame["close"]) == [4544.58, 4517.28]


def test_falls_back_to_baostock_when_akshare_returns_empty(no_benchmark_cache, monkeypatch):
    """`raise` 与"返回空帧"是两种失败形态，兜底对两者都要生效。"""
    monkeypatch.setattr(
        ms,
        "_fetch_benchmark_via_baostock",
        lambda: pd.DataFrame({"close": [4517.28], "date": ["2026-09-23"]}),
        raising=False,
    )

    frame = ms._default_fetch_benchmark(_ak_module(empty=True))()

    assert list(frame["close"]) == [4517.28]


def test_raises_when_every_source_fails(no_benchmark_cache, monkeypatch):
    """全挂时必须**抛错**，不能返回空帧——空基准会被静默当成"没有相对强弱"。"""

    def _fail_baostock():
        raise RuntimeError("baostock down")

    monkeypatch.setattr(ms, "_fetch_benchmark_via_baostock", _fail_baostock, raising=False)

    with pytest.raises(Exception):
        ms._default_fetch_benchmark(_ak_module(raise_all=True))()


# ----------------------------------------------------- BaoStock 分支的真实行为


def test_baostock_branch_returns_same_shape_as_akshare(monkeypatch):
    """`_fetch_benchmark_via_baostock()` 的列名必须与 AKShare 分支一致。"""
    from tradingagents.dataflows.providers.cn_baostock_provider import CnBaoStockProvider

    monkeypatch.setattr(
        CnBaoStockProvider,
        "get_index_daily_bars_df",
        lambda self, code, start, end: pd.DataFrame(
            {
                "date": ["2026-09-22", "2026-09-23"],
                "open": [1.0, 2.0],
                "high": [1.0, 2.0],
                "low": [1.0, 2.0],
                "close": [4544.58, 4517.28],
                "volume": [100, 200],
            }
        ),
    )

    frame = ms._fetch_benchmark_via_baostock()

    assert list(frame.columns) == ["close", "date"]
    assert list(frame["close"]) == [4544.58, 4517.28]


def test_baostock_branch_uses_the_hs300_code(monkeypatch):
    """基准就是沪深300——代码传错会静默算成另一个指数的相对强弱。"""
    from tradingagents.dataflows.providers.cn_baostock_provider import CnBaoStockProvider

    seen: list[str] = []

    def _capture(self, code, start, end):
        seen.append(code)
        return pd.DataFrame({"date": ["2026-09-23"], "close": [4517.28], "volume": [1]})

    monkeypatch.setattr(CnBaoStockProvider, "get_index_daily_bars_df", _capture)

    ms._fetch_benchmark_via_baostock()

    assert seen == ["sh000300"]
