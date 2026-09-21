"""M2 测试：多源兜底链（em→ths→sina）与 MarketCollector（TTL / force / 降级）。"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from tradingagents.dataflows.market_collector import MarketCollector
from tradingagents.dataflows.providers.cn_akshare_provider import (
    fetch_board_spot_df_chain,
    fetch_market_breadth_df,
    fetch_ths_industry_summary_df,
    fetch_zt_industry_heat_df,
)


def _em_spot_raw() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "板块名称": ["小金属", "CPO概念"],
            "板块代码": ["BK1027", "BK0896"],
            "最新价": [100.0, 50.0],
            "涨跌幅": [3.2, -1.5],
            "换手率": [2.5, 1.2],
            "上涨家数": [80, 20],
            "下跌家数": [5, 60],
            "领涨股票": ["北方稀土", "中际旭创"],
            "领涨股票-涨跌幅": [10.0, 8.0],
        }
    )


def _ths_summary_raw() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "板块": ["影视院线", "文化传媒"],
            "涨跌幅": [6.5, 4.66],
            "净流入": [21.88, 31.22],
            "上涨家数": [20, 81],
            "下跌家数": [0, 1],
            "领涨股": ["华策影视", "流金科技"],
            "领涨股-涨跌幅": [15.77, 29.96],
        }
    )


def _sina_spot_raw() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "板块": ["玻璃行业", "船舶制造"],
            "涨跌幅": [1.88, -0.98],
            "平均价格": [17.2, 13.6],
            "股票名称": ["菲利华", "ST亚光"],
            "个股-涨跌幅": [6.36, 3.1],
        }
    )


# ── 多源链 ───────────────────────────────────────────────────────


def test_spot_chain_prefers_em():
    ak = SimpleNamespace(
        stock_board_industry_spot_em=MagicMock(return_value=_em_spot_raw()),
        stock_board_industry_summary_ths=MagicMock(return_value=_ths_summary_raw()),
        stock_sector_spot=MagicMock(return_value=_sina_spot_raw()),
    )
    direct_df = pd.DataFrame({"name": ["转基因"], "code": ["BK0907"], "chg_1d": [6.22], "leader": ["大北农"]})
    with patch(
        "tradingagents.dataflows.providers.cn_eastmoney_direct.fetch_em_board_spot_direct",
        return_value=direct_df,
    ):
        df, source = fetch_board_spot_df_chain(ak, "industry")
    assert source == "em"
    assert df.loc[0, "name"] == "转基因"
    ak.stock_board_industry_spot_em.assert_not_called()


def test_spot_chain_falls_back_to_ths_when_em_fails():
    ak = SimpleNamespace(
        stock_board_industry_spot_em=MagicMock(side_effect=ConnectionError("push2 reset")),
        stock_board_industry_summary_ths=MagicMock(return_value=_ths_summary_raw()),
        stock_sector_spot=MagicMock(return_value=_sina_spot_raw()),
    )
    with patch(
        "tradingagents.dataflows.providers.cn_eastmoney_direct.fetch_em_board_spot_direct",
        side_effect=ConnectionError("push2delay down"),
    ), patch("tradingagents.dataflows.providers.cn_akshare_provider.time.sleep"):
        df, source = fetch_board_spot_df_chain(ak, "industry")
    assert source == "ths"
    assert df.loc[0, "name"] == "影视院线"
    assert df.loc[0, "net_inflow"] == 21.88


def test_spot_chain_falls_back_to_sina_when_em_and_ths_fail():
    ak = SimpleNamespace(
        stock_board_industry_spot_em=MagicMock(side_effect=ConnectionError("push2 reset")),
        stock_board_industry_summary_ths=MagicMock(side_effect=ConnectionError("ths down")),
        stock_sector_spot=MagicMock(return_value=_sina_spot_raw()),
    )
    with patch(
        "tradingagents.dataflows.providers.cn_eastmoney_direct.fetch_em_board_spot_direct",
        side_effect=ConnectionError("push2delay down"),
    ), patch("tradingagents.dataflows.providers.cn_akshare_provider.time.sleep"):
        df, source = fetch_board_spot_df_chain(ak, "industry")
    assert source == "sina"
    assert df.loc[0, "name"] == "玻璃行业"
    assert df.loc[0, "leader"] == "菲利华"


def test_spot_chain_all_sources_fail_raises():
    ak = SimpleNamespace(
        stock_board_industry_spot_em=MagicMock(side_effect=ConnectionError("x")),
        stock_board_industry_summary_ths=MagicMock(side_effect=ConnectionError("y")),
        stock_sector_spot=MagicMock(side_effect=ConnectionError("z")),
    )
    with patch(
        "tradingagents.dataflows.providers.cn_eastmoney_direct.fetch_em_board_spot_direct",
        side_effect=ConnectionError("d"),
    ), patch("tradingagents.dataflows.providers.cn_akshare_provider.time.sleep"):
        with pytest.raises(RuntimeError):
            fetch_board_spot_df_chain(ak, "industry")


def test_spot_chain_concept_no_fallback():
    # 概念板块无 ths/sina 降级：直连+akshare 都失败即抛，由调用方降级（zt_heat 替代）
    ak = SimpleNamespace(
        stock_board_concept_spot_em=MagicMock(side_effect=ConnectionError("push2 reset"))
    )
    with patch(
        "tradingagents.dataflows.providers.cn_eastmoney_direct.fetch_em_board_spot_direct",
        side_effect=ConnectionError("push2delay down"),
    ), patch("tradingagents.dataflows.providers.cn_akshare_provider.time.sleep"):
        with pytest.raises(ConnectionError):
            fetch_board_spot_df_chain(ak, "concept")


# ── 涨停池行业聚合 ───────────────────────────────────────────────


def test_fetch_zt_industry_heat_df():
    raw = pd.DataFrame(
        {
            "代码": ["000011", "000560", "600111", "002230"],
            "名称": ["深物业A", "我爱我家", "北方稀土", "科大讯飞"],
            "连板数": [2, 3, 1, 5],
            "所属行业": ["房地产", "房地产", "有色", "计算机"],
        }
    )
    ak = SimpleNamespace(stock_zt_pool_em=MagicMock(return_value=raw))
    df = fetch_zt_industry_heat_df(ak, "2026-08-31")
    ak.stock_zt_pool_em.assert_called_once_with(date="20260831")
    assert len(df) == 3
    row = df[df["industry"] == "房地产"].iloc[0]
    assert row["zt_count"] == 2
    assert row["max_lianban"] == 3
    assert row["lianban_ge3"] == 1


# ── 市场宽度 ─────────────────────────────────────────────────────


def test_fetch_market_breadth_df():
    raw = pd.DataFrame(
        {"代码": ["a"] * 6, "名称": ["x"] * 6, "涨跌幅": [3.0, -1.0, 0.0, 2.0, -0.5, 1.5]}
    )
    ak = SimpleNamespace(stock_zh_a_spot=MagicMock(return_value=raw))
    b = fetch_market_breadth_df(ak)
    assert b["up"] == 3
    assert b["down"] == 2
    assert b["flat"] == 1
    assert b["total"] == 6


# ── MarketCollector ──────────────────────────────────────────────


def _collector_payload() -> dict:
    return {
        "trade_date": "2026-08-31",
        "perspective": "short",
        "industry_spot": [{"name": "小金属", "chg_1d": 3.2}],
        "emotion": {"temperature": 60, "gate": "normal"},
    }


def test_collector_single_flight_shares_one_fetch():
    collector = MarketCollector(intraday_ttl=300, closed_ttl=3600, non_trading_ttl=3600)
    started = threading.Event()
    release = threading.Event()
    calls = {"n": 0}

    def fake_fetch(trade_date, perspective, *, include_breadth=True):
        calls["n"] += 1
        started.set()
        release.wait(timeout=2)
        return _collector_payload()

    with patch.object(collector, "_fetch_market", side_effect=fake_fetch):
        with patch(
            "tradingagents.dataflows.market_collector.is_cn_trading_day",
            return_value=True,
        ), patch(
            "tradingagents.dataflows.market_collector.cn_market_phase",
            return_value="in_session",
        ):
            results = []

            def _call():
                results.append(collector.collect("2026-08-31"))

            t1 = threading.Thread(target=_call)
            t2 = threading.Thread(target=_call)
            t1.start()
            assert started.wait(timeout=1)
            t2.start()
            release.set()
            t1.join(timeout=2)
            t2.join(timeout=2)
            assert calls["n"] == 1
            assert len(results) == 2
            assert results[0] is results[1]


def test_collector_does_not_share_inflight_between_breadth_modes():
    """板块页的轻量采集不能阻塞或污染主线任务的完整采集。"""
    collector = MarketCollector(intraday_ttl=300, closed_ttl=3600, non_trading_ttl=3600)
    partial_started = threading.Event()
    release_partial = threading.Event()
    full_done = threading.Event()
    calls: list[bool] = []
    results: dict[str, dict] = {}

    def fake_fetch(trade_date, perspective, *, include_breadth=True):
        calls.append(include_breadth)
        if not include_breadth:
            partial_started.set()
            release_partial.wait(timeout=2)
        payload = _collector_payload()
        payload["breadth"] = {"total": 5000} if include_breadth else None
        return payload

    def collect_partial():
        results["partial"] = collector.collect(
            "2026-08-31", include_breadth=False
        )

    def collect_full():
        results["full"] = collector.collect(
            "2026-08-31", include_breadth=True
        )
        full_done.set()

    with patch.object(collector, "_fetch_market", side_effect=fake_fetch):
        with patch(
            "tradingagents.dataflows.market_collector.is_cn_trading_day",
            return_value=True,
        ), patch(
            "tradingagents.dataflows.market_collector.cn_market_phase",
            return_value="in_session",
        ):
            partial_thread = threading.Thread(target=collect_partial)
            full_thread = threading.Thread(target=collect_full)
            partial_thread.start()
            assert partial_started.wait(timeout=1)
            full_thread.start()
            try:
                assert full_done.wait(timeout=1)
            finally:
                release_partial.set()
                partial_thread.join(timeout=2)
                full_thread.join(timeout=2)

    assert sorted(calls) == [False, True]
    assert results["partial"]["breadth"] is None
    assert results["full"]["breadth"] == {"total": 5000}


def test_collector_ttl_caches_second_call(monkeypatch):
    collector = MarketCollector(intraday_ttl=300, closed_ttl=3600, non_trading_ttl=3600)
    calls = {"n": 0}

    def fake_fetch(trade_date, perspective, *, include_breadth=True):
        calls["n"] += 1
        return _collector_payload()

    with patch.object(collector, "_fetch_market", side_effect=fake_fetch):
        with patch(
            "tradingagents.dataflows.market_collector.is_cn_trading_day",
            return_value=True,
        ), patch(
            "tradingagents.dataflows.market_collector.cn_market_phase",
            return_value="in_session",
        ):
            p1 = collector.collect("2026-08-31")
            p2 = collector.collect("2026-08-31")
            assert p1 is p2  # 命中缓存同一对象
            assert calls["n"] == 1


def test_collector_force_refreshes(monkeypatch):
    collector = MarketCollector(intraday_ttl=300, closed_ttl=3600, non_trading_ttl=3600)
    calls = {"n": 0}

    def fake_fetch(trade_date, perspective, *, include_breadth=True):
        calls["n"] += 1
        return _collector_payload()

    with patch.object(collector, "_fetch_market", side_effect=fake_fetch):
        with patch(
            "tradingagents.dataflows.market_collector.is_cn_trading_day",
            return_value=True,
        ), patch(
            "tradingagents.dataflows.market_collector.cn_market_phase",
            return_value="in_session",
        ):
            collector.collect("2026-08-31")
            collector.collect("2026-08-31", force=True)
            assert calls["n"] == 2


def test_collector_different_date_no_cache_hit(monkeypatch):
    collector = MarketCollector(intraday_ttl=300, closed_ttl=3600, non_trading_ttl=3600)
    calls = {"n": 0}

    def fake_fetch(trade_date, perspective, *, include_breadth=True):
        calls["n"] += 1
        return _collector_payload()

    with patch.object(collector, "_fetch_market", side_effect=fake_fetch):
        with patch(
            "tradingagents.dataflows.market_collector.is_cn_trading_day",
            return_value=True,
        ), patch(
            "tradingagents.dataflows.market_collector.cn_market_phase",
            return_value="in_session",
        ):
            collector.collect("2026-08-31")
            collector.collect("2026-09-01")
            assert calls["n"] == 2


def test_collector_degradation_does_not_raise(monkeypatch):
    """所有数据源失败时：payload 带 sources=None + warnings，不抛异常。"""
    collector = MarketCollector()
    with patch(
        "tradingagents.dataflows.market_collector.is_cn_trading_day",
        return_value=True,
    ), patch(
        "tradingagents.dataflows.market_collector.cn_market_phase",
        return_value="in_session",
    ), patch(
        "tradingagents.dataflows.providers.cn_akshare_provider.fetch_board_spot_df_chain",
        side_effect=ConnectionError("spot down"),
    ), patch(
        "tradingagents.dataflows.providers.cn_akshare_provider.fetch_zt_industry_heat_df",
        side_effect=ConnectionError("zt down"),
    ), patch(
        "tradingagents.dataflows.providers.cn_akshare_provider.fetch_market_breadth_df",
        side_effect=ConnectionError("breadth down"),
    ), patch(
        "tradingagents.methodology.macro_fetch.fetch_ashare_macro_pool_brief",
        return_value="",
    ), patch(
        "tradingagents.dataflows.mainline_scoring.build_mainline_candidates",
        side_effect=ConnectionError("candidates down"),
    ):
        payload = collector.collect("2026-08-31")
    assert "trade_date" in payload
    assert isinstance(payload["warnings"], list)
    assert len(payload["warnings"]) >= 1
    assert payload["mainline_candidates"] == []
    assert payload["sources"]["industry_spot"] is None
    assert payload["sources"]["concept_spot"] is None


def test_df_to_rows_nan_to_none():
    from tradingagents.dataflows.market_collector import _df_to_rows

    df = pd.DataFrame({"name": ["小金属"], "chg_1d": [3.2], "net_inflow": [None]})
    rows = _df_to_rows(df)
    assert rows[0]["name"] == "小金属"
    assert rows[0]["net_inflow"] is None



# ── 数据源失败冷却 + 市场宽度 THS 兜底 ──────────────────────────


def _patch_market_deps(chain, breadth_fn, ths_summary=None):
    from unittest.mock import patch

    def _noop_board(st):
        return (pd.DataFrame(), "em")

    patchers = [
        patch("tradingagents.dataflows.providers.cn_akshare_provider.fetch_board_spot_df_chain", chain or _noop_board),
        patch("tradingagents.dataflows.providers.cn_akshare_provider.fetch_zt_industry_heat_df", return_value=pd.DataFrame()),
        patch("tradingagents.dataflows.mainline_scoring.build_mainline_candidates", return_value={"boards": [], "emotion": {"gate": "normal"}, "warnings": []}),
        patch("tradingagents.methodology.macro_fetch.fetch_ashare_macro_pool_brief", return_value=""),
    ]
    if breadth_fn is not None:
        patchers.append(patch("tradingagents.dataflows.providers.cn_akshare_provider.fetch_market_breadth_df", breadth_fn))
    if ths_summary is not None:
        patchers.append(patch("tradingagents.dataflows.providers.cn_akshare_provider.fetch_ths_industry_summary_df", return_value=ths_summary))
    return patchers


def test_collector_fail_cooldown_skips_repeated_requests():
    """concept_spot 失败后进入冷却，第二次 collect 不再重复请求该源。"""
    from tradingagents.dataflows.market_collector import MarketCollector

    collector = MarketCollector(intraday_ttl=300, closed_ttl=3600, non_trading_ttl=3600, fail_cooldown=300)
    chain = MagicMock(side_effect=lambda *a: (_raise_reset() if (len(a) > 1 and a[1] == "concept") else (pd.DataFrame(), "ths")))

    def _raise_reset():
        raise ConnectionError("Remote end closed connection without response")

    patchers = _patch_market_deps(chain, None)
    for p in patchers:
        p.start()
    try:
        collector._fetch_market("2026-08-31", "short", include_breadth=False)
        collector._fetch_market("2026-08-31", "short", include_breadth=False)
    finally:
        for p in patchers:
            p.stop()
    concept_calls = [c for c in chain.call_args_list if c.args and len(c.args) > 1 and c.args[1] == "concept"]
    assert len(concept_calls) == 1  # 第二次在冷却期被跳过


def test_breadth_falls_back_to_ths_approx():
    """新浪全市场失败时，市场宽度用 THS 行业家数聚合近似（归一化列名）。"""
    from tradingagents.dataflows.market_collector import MarketCollector

    collector = MarketCollector(fail_cooldown=1)
    ths_summary = pd.DataFrame(
        {"name": ["A", "B"], "up_count": [100.0, 200.0], "down_count": [50.0, 30.0]}
    )
    breadth_fn = MagicMock(side_effect=ConnectionResetError(54, "Connection reset by peer"))
    patchers = _patch_market_deps(None, breadth_fn, ths_summary=ths_summary)
    patchers.append(
        patch(
            "tradingagents.dataflows.providers.cn_eastmoney_direct.fetch_em_market_breadth_direct",
            side_effect=ConnectionError("em down"),
        )
    )
    for p in patchers:
        p.start()
    try:
        payload = collector._fetch_market("2026-08-31", "short", include_breadth=True)
    finally:
        for p in patchers:
            p.stop()
    b = payload["breadth"]
    assert b is not None
    assert b["up"] == 300 and b["down"] == 80
    assert b["approximate"] is True
    assert payload["sources"]["breadth"] == "ths_approx"
    # 有可用近似 → 降级而非失败，不产生 breadth 告警
    assert not any("breadth" in w for w in payload["warnings"])
