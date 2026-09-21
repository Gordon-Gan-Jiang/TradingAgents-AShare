"""M1 数据层测试：板块涨幅榜/历史/成分股/资金流的归一化、格式化与 provider 方法。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from tradingagents.dataflows.providers.cn_akshare_provider import (
    CnAkshareProvider,
    fetch_board_cons_df,
    fetch_board_hist_df,
    fetch_board_spot_df,
    fetch_board_spot_df_chain,
    fetch_sector_fund_flow_rank_df,
    format_board_cons_table,
    format_board_hist_table,
    format_board_rank_ranking,
    format_board_spot_ranking,
    _normalize_board_cons_df,
    _normalize_board_hist_df,
    _normalize_board_spot_df,
    _normalize_fund_flow_rank_df,
)


def _spot_raw() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "排名": [1, 2],
            "板块名称": ["小金属", "CPO概念"],
            "板块代码": ["BK1027", "BK0896"],
            "最新价": [100.0, 50.0],
            "涨跌幅": [3.2, -1.5],
            "总市值": [1e12, 5e11],
            "换手率": [2.5, 1.2],
            "上涨家数": [80, 20],
            "下跌家数": [5, 60],
            "领涨股票": ["北方稀土", "中际旭创"],
            "领涨股票-涨跌幅": [10.0, 8.0],
        }
    )


def _hist_raw() -> pd.DataFrame:
    dates = pd.date_range("2026-05-01", periods=20, freq="D")
    return pd.DataFrame(
        {
            "日期": [d.strftime("%Y-%m-%d") for d in dates],
            "开盘": [10.0] * 20,
            "收盘": list(range(100, 120)),
            "最高": [11.0] * 20,
            "最低": [9.0] * 20,
            "涨跌幅": [0.5] * 20,
            "成交量": [1e6] * 20,
            "成交额": [1e8] * 20,
            "换手率": [1.0] * 20,
        }
    )


def _cons_raw() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "代码": ["600111", "300308"],
            "名称": ["北方稀土", "中际旭创"],
            "最新价": [30.0, 100.0],
            "涨跌幅": [10.0, -2.0],
            "换手率": [5.0, 3.0],
            "总市值": [1e11, 2e11],
            "流通市值": [8e10, 1.5e11],
            "市盈率-动态": [20.0, 30.0],
            "市净率": [3.0, 4.0],
        }
    )


def _flow_raw() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "序号": [1, 2],
            "行业": ["小金属", "白酒"],
            "行业指数": [1000.0, 900.0],
            "行业-涨跌幅": [2.0, -0.5],
            "流入资金": [10.0, 5.0],
            "流出资金": [6.0, 5.5],
            "净额": [4.0, -0.5],
            "公司家数": [30, 20],
            "领涨股": ["北方稀土", "贵州茅台"],
            "领涨股-涨跌幅": [10.0, 0.5],
        }
    )


# ── 归一化 ────────────────────────────────────────────────────────


def test_normalize_board_spot_df():
    df = _normalize_board_spot_df(_spot_raw())
    assert list(df.columns) == [
        "name", "code", "latest", "chg_1d", "total_mv", "turnover",
        "up_count", "down_count", "leader", "leader_chg", "net_inflow",
    ]
    assert df.loc[0, "name"] == "小金属"
    assert df.loc[0, "chg_1d"] == 3.2
    assert df.loc[0, "up_count"] == 80
    # 东财 spot 无净流入列 → NaN
    assert pd.isna(df.loc[0, "net_inflow"])


def test_normalize_board_spot_df_ths_columns():
    # 同花顺 summary_ths 列名
    raw = pd.DataFrame(
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
    df = _normalize_board_spot_df(raw)
    assert df.loc[0, "name"] == "影视院线"
    assert df.loc[0, "chg_1d"] == 6.5
    assert df.loc[0, "net_inflow"] == 21.88
    assert df.loc[0, "leader"] == "华策影视"


def test_normalize_board_hist_df():
    df = _normalize_board_hist_df(_hist_raw())
    assert "close" in df.columns and "pct_chg" in df.columns
    assert len(df) == 20
    assert df.loc[0, "close"] == 100


def test_normalize_board_cons_df():
    df = _normalize_board_cons_df(_cons_raw())
    assert "code" in df.columns and "pe" in df.columns and "pb" in df.columns
    assert df.loc[0, "pe"] == 20.0


def test_normalize_fund_flow_rank_df():
    df = _normalize_fund_flow_rank_df(_flow_raw())
    assert df.loc[0, "net_inflow"] == 4.0
    assert df.loc[0, "leader"] == "北方稀土"


# ── fetch 函数 ────────────────────────────────────────────────────


def test_fetch_board_spot_df_calls_correct_api():
    ak = SimpleNamespace(stock_board_industry_spot_em=MagicMock(return_value=_spot_raw()))
    df = fetch_board_spot_df(ak, "industry")
    ak.stock_board_industry_spot_em.assert_called_once()
    assert "name" in df.columns and "chg_1d" in df.columns


def test_fetch_board_spot_df_unknown_type_raises():
    with pytest.raises(ValueError):
        fetch_board_spot_df(SimpleNamespace(), "bogus")


def test_fetch_board_spot_df_missing_api_raises():
    with pytest.raises(NotImplementedError):
        fetch_board_spot_df(SimpleNamespace(), "industry")


def test_fetch_board_hist_df_normalized():
    ak = SimpleNamespace(stock_board_industry_hist_em=MagicMock(return_value=_hist_raw()))
    df = fetch_board_hist_df(ak, "小金属", "industry", start_date="2026-05-01", end_date="2026-06-30")
    ak.stock_board_industry_hist_em.assert_called_once()
    assert "close" in df.columns and "pct_chg" in df.columns
    assert len(df) == 20


def test_fetch_board_cons_df_normalized():
    ak = SimpleNamespace(stock_board_concept_cons_em=MagicMock(return_value=_cons_raw()))
    df = fetch_board_cons_df(ak, "CPO概念", "concept")
    ak.stock_board_concept_cons_em.assert_called_once()
    assert "code" in df.columns and "pe" in df.columns


def test_fetch_sector_fund_flow_rank_df_period_mapping():
    ak = SimpleNamespace(stock_sector_fund_flow_rank=MagicMock(return_value=_flow_raw()))
    df = fetch_sector_fund_flow_rank_df(ak, "industry", "5d")
    ak.stock_sector_fund_flow_rank.assert_called_once_with(
        indicator="5日", sector_type="行业资金流"
    )
    assert df.loc[0, "net_inflow"] == 4.0


# ── 格式化 ────────────────────────────────────────────────────────


def test_format_board_spot_ranking_sorted_by_chg():
    df = _normalize_board_spot_df(_spot_raw())
    text = format_board_spot_ranking(df, sector_type="industry", top_n=10)
    assert "行业板块涨幅榜" in text
    assert "小金属" in text and "CPO概念" in text
    assert text.index("小金属") < text.index("CPO概念")  # 涨幅榜降序


def test_format_board_spot_ranking_empty():
    text = format_board_spot_ranking(pd.DataFrame(), sector_type="concept")
    assert "概念板块涨幅榜数据暂不可用" in text


def test_format_board_rank_ranking():
    df = _normalize_fund_flow_rank_df(_flow_raw())
    text = format_board_rank_ranking(df, sector_type="industry", period="5d")
    assert "行业板块5日资金净流入" in text
    assert "小金属" in text


def test_format_board_hist_table():
    df = _normalize_board_hist_df(_hist_raw())
    text = format_board_hist_table(df, "小金属", sector_type="industry", recent_days=5)
    assert "小金属" in text and "近5日行情" in text
    assert "100" in text


def test_format_board_cons_table():
    df = _normalize_board_cons_df(_cons_raw())
    text = format_board_cons_table(df, "小金属", sector_type="industry")
    assert "成分股" in text and "北方稀土" in text


# ── provider 方法（容错返回文本） ─────────────────────────────────


def test_provider_get_board_spot_returns_text():
    prov = CnAkshareProvider()
    with patch.object(
        prov, "_ak",
        return_value=SimpleNamespace(stock_board_industry_spot_em=MagicMock(return_value=_spot_raw())),
    ):
        text = prov.get_board_spot("industry")
    assert "行业板块涨幅榜" in text


def test_provider_get_board_spot_error_tolerant():
    prov = CnAkshareProvider()
    with patch("tradingagents.dataflows.providers.cn_akshare_provider.time.sleep"):
        with patch.object(
            prov, "_ak",
            return_value=SimpleNamespace(
                stock_board_industry_spot_em=MagicMock(side_effect=ConnectionError("boom"))
            ),
        ):
            text = prov.get_board_spot("industry")
    assert "获取失败" in text


def test_provider_get_board_cons_returns_text():
    prov = CnAkshareProvider()
    with patch.object(
        prov, "_ak",
        return_value=SimpleNamespace(stock_board_concept_cons_em=MagicMock(return_value=_cons_raw())),
    ):
        text = prov.get_board_cons("CPO概念", "concept")
    assert "成分股" in text


# ── 工具路由 ──────────────────────────────────────────────────────


def test_board_tools_category_registered():
    from tradingagents.dataflows.interface import get_category_for_method

    for method in ("get_board_spot", "get_board_rank", "get_board_hist", "get_board_cons"):
        assert get_category_for_method(method) == "cn_board_data"


def test_route_to_vendor_board_spot_hits_akshare():
    from tradingagents.dataflows.interface import route_to_vendor

    with patch.object(
        CnAkshareProvider, "_ak",
        return_value=SimpleNamespace(stock_board_industry_spot_em=MagicMock(return_value=_spot_raw())),
    ):
        text = route_to_vendor("get_board_spot", "industry")
    assert "行业板块涨幅榜" in text


# ── 东财直连（push2delay）：概念板块涨幅榜 + 全市场宽度 ─────────


def _clist_diff_rows():
    return [
        {"f2": 100.0, "f3": 6.22, "f8": 2.5, "f12": "BK0907", "f14": "转基因", "f20": 1e12, "f104": 30, "f105": 2, "f128": "大北农", "f136": 10.0},
        {"f2": 50.0, "f3": 6.08, "f8": 1.2, "f12": "BK1086", "f14": "粮食概念", "f20": 5e11, "f104": 20, "f105": 5, "f128": "金健米业", "f136": 9.5},
    ]


def test_fetch_em_board_spot_direct_normalized():
    from tradingagents.dataflows.providers.cn_eastmoney_direct import fetch_em_board_spot_direct

    with patch(
        "tradingagents.dataflows.providers.cn_eastmoney_direct.http_get_json",
        return_value={"data": {"diff": _clist_diff_rows()}},
    ):
        df = fetch_em_board_spot_direct("concept")
    assert len(df) == 2
    assert df.loc[0, "name"] == "转基因"
    assert df.loc[0, "code"] == "BK0907"
    assert df.loc[0, "chg_1d"] == 6.22
    assert df.loc[0, "leader"] == "大北农"
    assert df.loc[0, "up_count"] == 30
    assert "net_inflow" in df.columns


def test_fetch_em_market_breadth_direct_counts():
    from tradingagents.dataflows.providers.cn_eastmoney_direct import fetch_em_market_breadth_direct

    rows = [{"f3": 1.0}] * 3000 + [{"f3": -1.0}] * 2000 + [{"f3": 0.0}] * 500
    with patch(
        "tradingagents.dataflows.providers.cn_eastmoney_direct._clist_paged", return_value=rows
    ):
        b = fetch_em_market_breadth_direct()
    assert b["up"] == 3000 and b["down"] == 2000 and b["flat"] == 500
    assert b["total"] == 5500


def test_spot_chain_prefers_em_direct_over_akshare():
    from tradingagents.dataflows.providers.cn_eastmoney_direct import fetch_em_board_spot_direct

    direct_df = pd.DataFrame(
        {"name": ["转基因"], "code": ["BK0907"], "chg_1d": [6.22], "leader": ["大北农"]}
    )
    ak = SimpleNamespace(
        stock_board_concept_spot_em=MagicMock(),  # 不应被调用
    )
    with patch(
        "tradingagents.dataflows.providers.cn_eastmoney_direct.fetch_em_board_spot_direct",
        return_value=direct_df,
    ):
        df, source = fetch_board_spot_df_chain(ak, "concept")
    assert source == "em"
    assert df.loc[0, "name"] == "转基因"
    ak.stock_board_concept_spot_em.assert_not_called()
