"""端口适配层的口径测试。

重点是 `build_peer_snapshot` 的口径——D6 要求用**板块成分股换手率中位数**
消解「缩量」歧义。若错取板块指数自身的换手率，与个股换手不在同一量纲上，
比较没有意义，而这个错误在真实数据上不报错、只是结论悄悄变错。

取数本身（akshare 调用）不做单测：它是薄封装，且当前环境无网络。
"""

from __future__ import annotations

import pandas as pd
import pytest


def test_peer_snapshot_takes_constituent_median_not_index_turnover():
    """板块中位换手必须来自**成分股**，不是板块自身。"""
    from api.services.signal_ports import build_peer_snapshot

    cons = pd.DataFrame({"代码": ["600519", "600809", "000858"], "换手率": [0.5, 1.8, 3.1]})
    board = pd.DataFrame({"板块名称": ["白酒"], "涨跌幅": [-2.4], "换手率": [99.9]})

    snap = build_peer_snapshot(
        symbol="600519.SH",
        board_name="白酒",
        board_frame=board,
        cons_frame=cons,
        stock_turnover=0.6,
    )

    assert snap is not None
    # 成分股中位数 = 1.8，绝不是板块自身的 99.9
    assert snap.board_median_turnover == pytest.approx(1.8)
    assert snap.board_return_pct == pytest.approx(-2.4)
    assert snap.stock_turnover == pytest.approx(0.6)


def test_peer_snapshot_tolerates_alternate_column_names():
    """不同数据源的列名口径不同（中文/英文），逐个尝试而不是硬编码一个。"""
    from api.services.signal_ports import build_peer_snapshot

    cons = pd.DataFrame({"code": ["600519", "600809"], "turnover": [0.4, 2.0]})
    board = pd.DataFrame({"name": ["白酒"], "change_pct": [-1.2]})

    snap = build_peer_snapshot(
        symbol="600519.SH", board_name="白酒", board_frame=board,
        cons_frame=cons, stock_turnover=0.5,
    )
    assert snap is not None
    assert snap.board_median_turnover == pytest.approx(1.2)
    assert snap.board_return_pct == pytest.approx(-1.2)


def test_peer_snapshot_returns_none_when_nothing_usable():
    """什么都算不出来时返回 None，让维度显式报不可得，而不是编一个中性值。"""
    from api.services.signal_ports import build_peer_snapshot

    snap = build_peer_snapshot(
        symbol="600519.SH", board_name=None, board_frame=None,
        cons_frame=pd.DataFrame(), stock_turnover=None,
    )
    assert snap is None


def test_peer_snapshot_includes_mainline_for_divergence():
    """主线基准与行业基准分开保存——背离本身就是信号（D16）。"""
    from api.services.signal_ports import build_peer_snapshot

    board = pd.DataFrame({"板块名称": ["白酒"], "涨跌幅": [-0.2]})
    mainline = pd.DataFrame({"板块名称": ["AI算力"], "涨跌幅": [-3.5]})
    cons = pd.DataFrame({"换手率": [1.0, 2.0]})

    snap = build_peer_snapshot(
        symbol="600519.SH", board_name="白酒", board_frame=board, cons_frame=cons,
        stock_turnover=1.5, mainline_board_name="AI算力", mainline_frame=mainline,
    )
    assert snap is not None
    assert snap.board_return_pct == pytest.approx(-0.2)
    assert snap.mainline_return_pct == pytest.approx(-3.5)


def test_daily_port_converts_as_of_into_a_date_range():
    from api.services.signal_ports import ProviderDailyBarsPort

    seen: dict[str, str] = {}

    class _Provider:
        def get_daily_bars_df(self, symbol, start, end):
            seen["start"], seen["end"], seen["symbol"] = start, end, symbol
            return pd.DataFrame({"date": [end], "close": [10.0], "volume": [100.0]})

    frame = ProviderDailyBarsPort(provider=_Provider()).bars("600519.SH", "2026-01-16", 20)
    assert len(frame) == 1
    assert seen["end"] == "2026-01-16"
    assert seen["start"] < seen["end"]
    assert seen["symbol"] == "600519.SH"


def test_daily_port_raises_on_empty_so_context_reports_insufficient_history():
    """日线是核心输入：拿不到必须显式失败，让上层降级为「缺数据」，
    而不是返回空表让维度算出无意义的 0。"""
    from api.services.signal_ports import ProviderDailyBarsPort

    class _Provider:
        def get_daily_bars_df(self, symbol, start, end):
            return pd.DataFrame()

    with pytest.raises(RuntimeError):
        ProviderDailyBarsPort(provider=_Provider()).bars("600519.SH", "2026-01-16", 20)


def test_optional_ports_degrade_to_none_instead_of_raising():
    """可选源（分时/筹码/资金流）失败必须降级为 None。

    缺它们是常态；抛异常会把「数据不全」变成「功能不可用」。
    """
    from api.services.signal_ports import (
        ProviderCapitalFlowPort,
        ProviderChipPort,
        ProviderIntradayPort,
    )

    def _boom(*_args, **_kw):
        raise ConnectionError("network down")

    assert ProviderIntradayPort(fetcher=_boom).bars("600519.SH", "2026-01-16", "5") is None
    assert ProviderChipPort(fetcher=_boom).chips("600519.SH", "2026-01-16") is None
    assert ProviderCapitalFlowPort(fetcher=_boom).flow("600519.SH", "2026-01-16", 20) is None


def test_peer_port_degrades_to_none_on_loader_failure():
    from api.services.signal_ports import ProviderPeerPort

    def _boom(_symbol, _day):
        raise ConnectionError("network down")

    assert ProviderPeerPort(loader=_boom).snapshot("600519.SH", "2026-01-16") is None


def test_peer_port_composes_loader_and_optional_lookups():
    from api.services.signal_ports import ProviderPeerPort

    board = pd.DataFrame({"板块名称": ["白酒"], "涨跌幅": [-1.0]})
    cons = pd.DataFrame({"换手率": [1.0, 3.0]})

    port = ProviderPeerPort(
        loader=lambda symbol, day: ("白酒", board, cons),
        turnover_lookup=lambda symbol, day: 2.0,
    )
    snap = port.snapshot("600519.SH", "2026-01-16")
    assert snap is not None
    assert snap.board_median_turnover == pytest.approx(2.0)
    assert snap.stock_turnover == pytest.approx(2.0)
