"""Tushare 通道：补齐资金流与筹码维度。

## 为什么必须有它

东财系接口被 IP 级封禁后，判定的 `capital_composition`（个股资金流）与
`chip_distribution`（筹码分布）**没有任何免费替源**，判定只能停在"证据不足"。
Tushare 是官方积分制源，是最短的补齐路径。

## 本文件锁住的分寸

1. **没有 token 时如实声明不可用**，绝不返回空数据充当"取到了但没有"。
2. 上游报错（积分不足/频次超限/网络）一律归一成 `TushareUnavailable`——
   调用方只需要"能不能用"，那三种都只能声明未覆盖。
3. **不外传上游原文**：错误消息里只给类型与接口名，原文进日志。
4. 请求参数格式要正确（`ts_code` 用 `600519.SH`、日期用 `YYYYMMDD`）。
"""

from __future__ import annotations

import pytest


def _fake_post(monkeypatch, *, rows=None, code=0, fields=("ts_code", "buy_sm_amount"), raise_exc=None):
    """替换 Tushare 的 HTTP 层，并记录请求体。"""
    import json as _json

    from tradingagents.dataflows.providers import cn_tushare

    seen: dict = {}

    class _Response:
        def __init__(self):
            self.status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"code": code, "msg": "insufficient credits" if code else "", "data": {"fields": list(fields), "items": rows or []}}

    class _Requests:
        @staticmethod
        def post(url, data=None, headers=None, timeout=None):
            seen["url"] = url
            seen["payload"] = _json.loads(data)
            seen["timeout"] = timeout
            if raise_exc is not None:
                raise raise_exc
            return _Response()

    monkeypatch.setattr(cn_tushare, "token", lambda: "TEST-TOKEN")
    monkeypatch.setitem(__import__("sys").modules, "requests", _Requests)
    return seen


def test_moneyflow_row_is_parsed(monkeypatch):
    from tradingagents.dataflows.providers import cn_tushare

    _fake_post(
        monkeypatch,
        rows=[["600519.SH", 12345.6]],
        fields=("ts_code", "buy_sm_amount"),
    )
    row = cn_tushare.fetch_moneyflow_row("600519.SH", "2026-09-19")
    assert row is not None
    assert row["buy_sm_amount"] == 12345.6
    assert row["_symbol"] == "600519.SH"


def test_request_uses_tushare_conventions(monkeypatch):
    """`ts_code` 与日期格式必须符合 Tushare 约定，否则会静默查不到。"""
    from tradingagents.dataflows.providers import cn_tushare

    seen = _fake_post(monkeypatch, rows=[["600519.SH", 1.0]])
    cn_tushare.fetch_moneyflow_row("600519.SH", "2026-09-19")

    assert seen["payload"]["api_name"] == "moneyflow"
    assert seen["payload"]["params"]["ts_code"] == "600519.SH"
    assert seen["payload"]["params"]["trade_date"] == "20260919"
    assert seen["timeout"] == cn_tushare.REQUEST_TIMEOUT_SECONDS


def test_missing_token_raises_unavailable(monkeypatch):
    from tradingagents.dataflows.providers import cn_tushare

    monkeypatch.setattr(cn_tushare, "token", lambda: None)
    with pytest.raises(cn_tushare.TushareUnavailable):
        cn_tushare.query("moneyflow")


def test_no_rows_returns_none_not_a_fake_frame(monkeypatch):
    """"上游没这条数据"与"通道不可用"是两件事：前者返回 None，后者抛错。"""
    from tradingagents.dataflows.providers import cn_tushare

    _fake_post(monkeypatch, rows=[])
    assert cn_tushare.fetch_moneyflow_row("600519.SH", "2026-09-19") is None


def test_upstream_error_becomes_unavailable_without_leaking_text(monkeypatch):
    from tradingagents.dataflows.providers import cn_tushare

    _fake_post(monkeypatch, code=2002)
    with pytest.raises(cn_tushare.TushareUnavailable) as excinfo:
        cn_tushare.fetch_chip_row("600519.SH", "2026-09-19")
    assert "insufficient credits" not in str(excinfo.value), "上游原文不进对外消息"


def test_network_error_becomes_unavailable(monkeypatch):
    from tradingagents.dataflows.providers import cn_tushare

    _fake_post(monkeypatch, raise_exc=TimeoutError("boom"))
    with pytest.raises(cn_tushare.TushareUnavailable):
        cn_tushare.query("cyq_perf")


def test_ts_code_normalisation():
    from tradingagents.dataflows.providers import cn_tushare

    assert cn_tushare._ts_code("600519")[0] == "600519.SH"
    assert cn_tushare._ts_code("300750")[0] == "300750.SZ"
    assert cn_tushare._ts_code("000001.SZ")[0] == "000001.SZ"


def test_is_configured_reflects_the_env(monkeypatch):
    from tradingagents.dataflows.providers import cn_tushare

    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    monkeypatch.delenv("TA_TUSHARE_TOKEN", raising=False)
    assert cn_tushare.is_configured() is False
    monkeypatch.setenv("TUSHARE_TOKEN", "abc")
    assert cn_tushare.is_configured() is True
