"""Regression tests for akshare board fund-flow API fallback."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from tradingagents.dataflows.providers.cn_akshare_provider import (
    CnAkshareProvider,
    fetch_board_fund_flow_df,
    fetch_industry_board_fund_flow_df,
    format_board_fund_flow_ranking,
)
from tradingagents.dataflows.providers.cn_sina_moneyflow_provider import (
    CnSinaMoneyflowProvider,
    fetch_sina_industry_board_fund_flow_df,
)


def test_fetch_industry_board_fund_flow_df_falls_back_to_sector_rank():
    sample = pd.DataFrame({"名称": ["面板"], "今日主力净流入-净额": [1.0]})
    rank_fn = MagicMock(return_value=sample)
    ak = SimpleNamespace(stock_sector_fund_flow_rank=rank_fn)

    df = fetch_industry_board_fund_flow_df(ak)

    assert df is sample
    rank_fn.assert_called_once_with(indicator="今日", sector_type="行业资金流")


def test_fetch_industry_board_fund_flow_df_prefers_legacy_api():
    sample = pd.DataFrame({"名称": ["面板"], "今日主力净流入-净额": [1.0]})
    legacy_fn = MagicMock(return_value=sample)
    rank_fn = MagicMock()
    ak = SimpleNamespace(
        stock_board_industry_fund_flow_em=legacy_fn,
        stock_sector_fund_flow_rank=rank_fn,
    )

    df = fetch_industry_board_fund_flow_df(ak)

    assert df is sample
    legacy_fn.assert_called_once_with(symbol="今日")
    rank_fn.assert_not_called()


def test_fetch_industry_board_fund_flow_df_falls_back_when_legacy_connection_fails():
    sample = pd.DataFrame({"名称": ["面板"], "今日主力净流入-净额": [1.0]})
    legacy_fn = MagicMock(
        side_effect=ConnectionError(
            "('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))"
        )
    )
    rank_fn = MagicMock(return_value=sample)
    ak = SimpleNamespace(
        stock_board_industry_fund_flow_em=legacy_fn,
        stock_sector_fund_flow_rank=rank_fn,
    )

    df = fetch_industry_board_fund_flow_df(ak)

    assert df is sample
    assert legacy_fn.call_count == 3
    rank_fn.assert_called_once_with(indicator="今日", sector_type="行业资金流")


def test_fetch_sina_industry_board_fund_flow_df_parses_ranking(monkeypatch):
    payload = [
        {
            "name": "电子器件",
            "avg_changeratio": "0.03",
            "netamount": "100.0",
            "ratioamount": "0.07",
        }
    ]

    def fake_http_get_json(url, *, params=None, headers=None, retry=None):
        assert "MoneyFlow.ssl_bkzj_bk" in url
        assert params["fenlei"] == 0
        return payload

    monkeypatch.setattr(
        "tradingagents.dataflows.providers.cn_sina_moneyflow_provider.http_get_json",
        fake_http_get_json,
    )

    df = fetch_sina_industry_board_fund_flow_df()

    assert len(df) == 1
    assert df.iloc[0]["名称"] == "电子器件"
    assert df.iloc[0]["今日主力净流入-净额"] == 100.0


def test_fetch_board_fund_flow_df_prefers_sina(monkeypatch):
    sample = pd.DataFrame({"名称": ["面板"], "今日主力净流入-净额": [1.0]})
    ak_rank = MagicMock(side_effect=AssertionError("akshare should not be called"))

    monkeypatch.setattr(
        "tradingagents.dataflows.providers.cn_sina_moneyflow_provider.fetch_sina_industry_board_fund_flow_df",
        lambda: sample,
    )

    df = fetch_board_fund_flow_df(ak_module=SimpleNamespace(stock_sector_fund_flow_rank=ak_rank))

    assert df is sample


def test_fetch_board_fund_flow_df_falls_back_to_akshare_when_sina_fails(monkeypatch):
    sample = pd.DataFrame({"名称": ["面板"], "今日主力净流入-净额": [1.0]})
    rank_fn = MagicMock(return_value=sample)

    monkeypatch.setattr(
        "tradingagents.dataflows.providers.cn_sina_moneyflow_provider.fetch_sina_industry_board_fund_flow_df",
        lambda: (_ for _ in ()).throw(RuntimeError("sina down")),
    )

    df = fetch_board_fund_flow_df(
        ak_module=SimpleNamespace(stock_sector_fund_flow_rank=rank_fn)
    )

    assert df is sample
    rank_fn.assert_called_once()


def test_sina_provider_get_board_fund_flow(monkeypatch):
    sample = pd.DataFrame({"名称": ["面板"], "今日主力净流入-净额": [1.0]})
    monkeypatch.setattr(
        "tradingagents.dataflows.providers.cn_sina_moneyflow_provider.fetch_sina_industry_board_fund_flow_df",
        lambda: sample,
    )
    provider = CnSinaMoneyflowProvider()

    result = provider.get_board_fund_flow()

    assert "板块资金流向数据获取失败" not in result
    assert "数据截止" in result
    assert "面板" in result


def test_format_board_fund_flow_ranking_includes_snapshot_date():
    df = pd.DataFrame({"名称": ["面板", "光学光电子"], "今日主力净流入-净额": [1.0, 2.0]})
    text = format_board_fund_flow_ranking(df, snapshot_date="2026-06-26")
    assert "数据截止 2026-06-26" in text
    assert "面板" in text


def test_get_board_fund_flow_uses_sector_rank_when_legacy_missing(monkeypatch):
    provider = CnAkshareProvider()
    sample = pd.DataFrame({"名称": ["面板"], "今日主力净流入-净额": [1.0]})

    ak = SimpleNamespace(stock_sector_fund_flow_rank=MagicMock(return_value=sample))
    monkeypatch.setattr(
        "tradingagents.dataflows.providers.cn_sina_moneyflow_provider.fetch_sina_industry_board_fund_flow_df",
        lambda: (_ for _ in ()).throw(RuntimeError("sina down")),
    )
    provider._ak = lambda: ak  # type: ignore[method-assign]

    result = provider.get_board_fund_flow()

    assert "板块资金流向数据获取失败" not in result
    assert "数据截止" in result
    assert "面板" in result


def test_get_board_fund_flow_reports_failure_when_no_api(monkeypatch):
    provider = CnAkshareProvider()
    provider._ak = lambda: SimpleNamespace()  # type: ignore[method-assign]
    monkeypatch.setattr(
        "tradingagents.dataflows.providers.cn_sina_moneyflow_provider.fetch_sina_industry_board_fund_flow_df",
        lambda: (_ for _ in ()).throw(RuntimeError("sina down")),
    )

    result = provider.get_board_fund_flow()

    assert "板块资金流向数据获取失败" in result
    assert "AttributeError" in result
