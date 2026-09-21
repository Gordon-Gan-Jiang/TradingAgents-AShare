"""东财直连 + 出口代理：恢复被 IP 封禁的筹码/资金流维度。

## 背景

实测东财系接口 IP 级封禁（`RemoteDisconnected`），而 `capital_composition`
（个股资金流）与 `chip_distribution`（筹码）都要经过它 → 判定长期停在"证据不足"。
换代理是最低成本恢复路径（换出口 IP，而不是换数据源）。

## 本文件锁住

1. **代理必须真的传进请求**（`TA_EM_PROXY` 优先，其次标准 `HTTPS_PROXY`/`HTTP_PROXY`）——
   "读了配置却没传下去"是这类改动的典型失败。
2. 没配代理时**不传 `proxies`**（传 `None`），避免把直连的请求莫名绕一圈。
3. 资金流的归一必须对上内部契约（`main_net_inflow`，单位**元**）。
4. 取不到返回 None（既有的诚实路径），不造空帧。
"""

from __future__ import annotations

import pytest


def _capture_get(monkeypatch, payload):
    from tradingagents.dataflows.providers import _http_utils

    seen: dict = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    class _Requests:
        @staticmethod
        def get(url, **kwargs):
            # 用 **kwargs 才能判断"参数到底传没传"——固定签名会把默认值也记进去，
            # 于是"没传 proxies"这条断言永远为假。
            seen.update(kwargs)
            seen["url"] = url
            return _Resp()

    import sys

    monkeypatch.setitem(sys.modules, "requests", _Requests)
    monkeypatch.setattr(_http_utils, "requests", _Requests, raising=False)
    return seen


def test_ta_em_proxy_takes_precedence(monkeypatch):
    from tradingagents.dataflows.providers._http_utils import resolve_proxies

    monkeypatch.setenv("TA_EM_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("HTTPS_PROXY", "http://other:1080")
    assert resolve_proxies() == {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}


def test_standard_proxy_env_is_honoured(monkeypatch):
    from tradingagents.dataflows.providers._http_utils import resolve_proxies

    monkeypatch.delenv("TA_EM_PROXY", raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.local:8080")
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    assert resolve_proxies() == {"http": "http://proxy.local:8080", "https": "http://proxy.local:8080"}


def test_no_proxy_configured_returns_none(monkeypatch):
    from tradingagents.dataflows.providers._http_utils import resolve_proxies

    for key in ("TA_EM_PROXY", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(key, raising=False)
    assert resolve_proxies() is None, "没配代理就不该传 proxies，别把直连请求绕一圈"


def test_no_proxy_kwarg_when_unconfigured(monkeypatch):
    """没配代理时**不得**传 `proxies` —— 保持既有调用契约不变。

    这不是洁癖：无条件传 `proxies=None` 会让所有按老签名实现的调用方
    （包括既有测试替身）直接 TypeError。兼容性回归就是这么来的。
    """
    from tradingagents.dataflows.providers import _http_utils

    for key in ("TA_EM_PROXY", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(key, raising=False)
    seen = _capture_get(monkeypatch, {"data": {}})
    _http_utils.http_get_json("https://push2his.eastmoney.com/api/test")

    assert "proxies" not in seen, "未配置代理时不该出现 proxies 参数"


def test_proxy_is_actually_passed_to_the_request(monkeypatch):
    from tradingagents.dataflows.providers import _http_utils

    monkeypatch.setenv("TA_EM_PROXY", "http://127.0.0.1:7890")
    seen = _capture_get(monkeypatch, {"data": {}})
    _http_utils.http_get_json("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get")

    assert seen["proxies"] == {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}, (
        "读了代理配置却没传进请求 = 没生效"
    )


def test_fund_flow_direct_normalises_to_contract(monkeypatch):
    """资金流必须归一成 `main_net_inflow`（元）——单位错会让结论反向。"""
    from tradingagents.dataflows.providers import cn_eastmoney_direct as mod

    payload = {
        "data": {
            "klines": [
                # 日期, 主力净额, 小单, 中单, 大单, 超大单, 占比..., 收盘, 涨跌幅, ...
                "2026-09-18,-12345678.0,100.0,200.0,300.0,400.0,-1.2,-0.1,-0.2,-0.3,-0.4,1257.12,-0.36,-,-",
                "2026-09-19,-23456789.0,100.0,200.0,300.0,400.0,-1.5,-0.1,-0.2,-0.3,-0.4,1252.57,-0.36,-,-",
            ]
        }
    }
    _capture_get(monkeypatch, payload)
    frame = mod.fetch_em_individual_fund_flow_direct("600519.SH")

    assert frame is not None and len(frame) == 2
    assert "main_net_inflow" in frame.columns, f"契约列缺失：{list(frame.columns)}"
    assert "date" in frame.columns
    assert float(frame["main_net_inflow"].iloc[-1]) == pytest.approx(-23456789.0)


def test_fund_flow_direct_returns_none_on_empty(monkeypatch):
    from tradingagents.dataflows.providers import cn_eastmoney_direct as mod

    _capture_get(monkeypatch, {"data": {"klines": []}})
    assert mod.fetch_em_individual_fund_flow_direct("600519.SH") is None


def test_fund_flow_direct_returns_none_on_error(monkeypatch):
    from tradingagents.dataflows.providers import cn_eastmoney_direct as mod

    class _Requests:
        @staticmethod
        def get(*a, **kw):
            raise ConnectionError("Remote end closed connection")

    import sys

    monkeypatch.setitem(sys.modules, "requests", _Requests)
    assert mod.fetch_em_individual_fund_flow_direct("600519.SH") is None


def test_flow_frame_uses_em_direct_before_tushare(monkeypatch):
    """链路顺序：akshare → 东财直连（代理） → Tushare。

    东财直连排在 Tushare 前面：它是免费的，且不消耗积分配额。
    """
    import pandas as pd

    from tradingagents.dataflows.providers import cn_eastmoney_direct as em
    from tradingagents.dataflows.providers import cn_research_data as mod
    from tradingagents.dataflows.providers import cn_tushare

    class _FakeAk:
        def stock_individual_fund_flow(self, **kwargs):
            raise ConnectionError("Remote end closed connection")

    monkeypatch.setattr(mod, "_ak", lambda: _FakeAk())
    monkeypatch.setattr(
        em,
        "fetch_em_individual_fund_flow_direct",
        lambda symbol: pd.DataFrame(
            [{"date": "2026-09-18", "main_net_inflow": 1.0},
             {"date": "2026-09-19", "main_net_inflow": -8888.0}]
        ),
    )

    def _tushare_should_not_be_needed(symbol, day):
        raise AssertionError("东财直连成功时不应再打 Tushare（免费通道优先）")

    monkeypatch.setattr(cn_tushare, "fetch_moneyflow_row", _tushare_should_not_be_needed)

    frame = mod.fetch_flow_frame("600519.SH", "2026-09-19")
    assert frame is not None
    assert float(frame["main_net_inflow"].iloc[0]) == pytest.approx(-8888.0)


def test_flow_frame_falls_through_to_tushare_when_direct_fails(monkeypatch):
    from tradingagents.dataflows.providers import cn_eastmoney_direct as em
    from tradingagents.dataflows.providers import cn_research_data as mod
    from tradingagents.dataflows.providers import cn_tushare

    class _FakeAk:
        def stock_individual_fund_flow(self, **kwargs):
            raise ConnectionError("blocked")

    monkeypatch.setattr(mod, "_ak", lambda: _FakeAk())
    monkeypatch.setattr(em, "fetch_em_individual_fund_flow_direct", lambda symbol: None)
    monkeypatch.setattr(
        cn_tushare, "fetch_moneyflow_row",
        lambda symbol, day: {"net_mf_amount": -100.0, "trade_date": "20260919"},
    )

    frame = mod.fetch_flow_frame("600519.SH", "2026-09-19")
    assert frame is not None, "三通道里前两个都不可用时，Tushare 应兜住"
    assert float(frame["main_net_inflow"].iloc[0]) == pytest.approx(-100.0 * 10_000)


def test_direct_mode_bypasses_system_proxy(monkeypatch):
    """未配代理时必须**显式直连**，不能让系统代理静默接管。

    实测这台机器：`urllib.request.getproxies()` 有值（Clash 类工具），而
    `requests` 默认 `trust_env=True` 会走它——对东财直接 `ProxyError`。
    所以要能区分"没配代理"（=直连）与"交给 requests 决定"。
    """
    from tradingagents.dataflows.providers import _http_utils

    for key in ("TA_EM_PROXY", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(key, raising=False)
    seen = _capture_get(monkeypatch, {"data": {}})

    _http_utils.http_get_json("https://push2his.eastmoney.com/api/test", direct=True)

    assert seen.get("proxies") == {"http": None, "https": None}, (
        "直连时必须显式传空代理，否则系统代理会静默接管"
    )


def test_explicit_proxy_wins_over_direct_flag(monkeypatch):
    """显式配了代理时，即使调用点要求直连也**以显式配置为准**。

    理由：显式配置是运维的明确意图；忽略它会让"我配了代理却没有生效"再次出现。
    """
    from tradingagents.dataflows.providers import _http_utils

    monkeypatch.setenv("TA_EM_PROXY", "http://127.0.0.1:7890")
    seen = _capture_get(monkeypatch, {"data": {}})

    _http_utils.http_get_json("https://push2his.eastmoney.com/api/test", direct=True)

    assert seen.get("proxies") == {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}
