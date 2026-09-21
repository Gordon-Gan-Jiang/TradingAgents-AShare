import sys
import types

import pandas as pd


def test_akshare_lhb_uses_positional_args(monkeypatch):
    # Arrange: fake akshare module with strict signature
    calls = {}

    def stock_lhb_detail_em(code, start_date, end_date):
        calls["args"] = (code, start_date, end_date)
        return pd.DataFrame([{"x": 1}])

    fake = types.SimpleNamespace(stock_lhb_detail_em=stock_lhb_detail_em)
    monkeypatch.setitem(sys.modules, "akshare", fake)

    from tradingagents.dataflows.providers.cn_akshare_provider import CnAkshareProvider

    provider = CnAkshareProvider()
    out = provider.get_lhb_detail("600176.SH", "2026-06-16")

    assert calls["args"] == ("600176", "2026-06-16", "2026-06-16")
    assert "龙虎榜明细" in out


def test_eastmoney_http_fund_flow_parses_klines(monkeypatch):
    from tradingagents.dataflows.providers.cn_eastmoney_http_provider import CnEastmoneyHttpProvider

    provider = CnEastmoneyHttpProvider()

    class _Resp:
        def __init__(self, obj):
            self._obj = obj
            self.status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return self._obj

    def fake_get(url, params=None, headers=None, timeout=None):
        assert "fflow/kline/get" in url
        assert params["klt"] == 101
        assert params["lmt"] == 5
        obj = {
            "data": {
                "klines": [
                    "2026-06-12,100000000.0,20000000.0,30000000.0,40000000.0,50000000.0",
                    "2026-06-16,-374645168.0,-80407280.0,455052464.0,348655856.0,-723301024.0",
                ]
            }
        }
        return _Resp(obj)

    monkeypatch.setattr("requests.get", fake_get)

    out = provider.get_individual_fund_flow("600176.SH")
    assert "近5日主力资金净流向" in out
    # Ensure formatting includes converted unit columns
    assert "主力净流向(亿元)" in out


def test_sina_moneyflow_provider(monkeypatch):
    from tradingagents.dataflows.providers.cn_sina_moneyflow_provider import CnSinaMoneyflowProvider

    provider = CnSinaMoneyflowProvider()

    class _Resp:
        def __init__(self, obj):
            self._obj = obj
            self.status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return self._obj

    def fake_get(url, params=None, headers=None, timeout=None):
        assert "MoneyFlow.ssl_qsfx_zjlrqs" in url
        # desc order (latest first)
        obj = [
            {"opendate": "2026-06-16", "r0_net": "100000000.0", "netamount": "50000000.0"},
            {"opendate": "2026-06-15", "r0_net": "-200000000.0", "netamount": "-100000000.0"},
        ]
        return _Resp(obj)

    monkeypatch.setattr("requests.get", fake_get)

    out = provider.get_individual_fund_flow("000657.SZ")
    assert "数据截止 2026-06-16" in out
    assert "06-16" in out

