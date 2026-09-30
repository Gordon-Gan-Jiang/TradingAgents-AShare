"""非异动日不得被报成「龙虎榜数据获取失败」（TDD，根治 akshare 的 result:null 崩溃）。

## 症状（用户截图）

分析 电科思仪 301689.SZ 时，「数据异常」红框列出：

    lhb_detail: 龙虎榜数据获取失败: TypeError: 'NoneType' object is not subscriptable

## 根因（实测钉死，非推测）

`akshare.stock_lhb_detail_em` 的解析体第一行就是：

```python
data_json = r.json()
total_page_num = data_json["result"]["pages"]      # ← result 为 None 时炸在这里
```

而东财该端点对**没有龙虎榜行的窗口**返回 **HTTP 200 + `result: null`**，并附业务消息
`code=9201 / message="返回数据为空"`。实测：

| TRADE_DATE | HTTP | `result` | 上游 code/message |
|---|---|---|---|
| 2026-09-26（周六） | 200 | **None** | `9201 / 返回数据为空` |
| 2020-01-01（元旦） | 200 | **None** | `9201 / 返回数据为空` |
| 2026-09-29（有异动） | 200 | dict（72 行） | `0 / ok` |

于是 `None["pages"]` → `TypeError: 'NoneType' object is not subscriptable`。

**这不是故障，是「该窗口本来就没有数据」。** 上游把「结果为空」当成了解析错误，
再被 `get_lhb_detail` 的 `except Exception` 兜成「龙虎榜数据获取失败：TypeError…」，
用户就在报告里看到一个红色的「数据异常」。

而 `get_lhb_detail` 的 docstring 明写「非异动日返回空提示（**属正常**）」，下面也确有
`df is None or df.empty → "无龙虎榜数据（非异动日属正常）"` 分支——**但永远走不到**，
因为 akshare 在返回空表之前就先抛了。

这属于本会话反复消灭的同一类反模式：**把「信息缺失」报成「失败」**。

## 修法边界（不得越界）

akshare 仍是主路径（既有测试钉死了它的单日窗口调用形状）。只有当它抛出**解析形态
崩溃**（`result` 为 null 之类的下标/取键错误）时，才回退到直连端点去**核实**这个窗口
到底是真的空、还是有数据被上游解析器丢了：

* 直连得到 `code=9201 / result:null` ⇒ 真空窗口 ⇒ 中性「无龙虎榜数据」；
* 直连拿到行 ⇒ 数据被上游解析器丢了 ⇒ 把数据还给用户（而不是放弃）；
* 直连也失败 / 未知业务码 ⇒ 如实报失败。

**非解析类错误（网络异常、缺函数）绝不触发回退**，该报错就报错。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pytest

from tradingagents.dataflows.providers.cn_akshare_provider import CnAkshareProvider

_HTTP = "tradingagents.dataflows.providers.cn_eastmoney_direct.http_get_json"
#: 生产里真实出现的那条异常（用户截图）。
_UPSTREAM_CRASH = TypeError("'NoneType' object is not subscriptable")

EMPTY_WINDOW_RESPONSE = {
    "version": None, "result": None, "success": False,
    "message": "返回数据为空", "code": 9201,
}
ROW = {
    "SECURITY_CODE": "301689", "SECUCODE": "301689.SZ",
    "SECURITY_NAME_ABBR": "电科思仪", "TRADE_DATE": "2026-09-29 00:00:00",
    "EXPLAIN": "4家机构卖出", "CLOSE_PRICE": 77.51, "CHANGE_RATE": -8.9189,
}
DATA_WINDOW_RESPONSE = {
    "version": "x", "result": {"pages": 1, "data": [ROW], "count": 1},
    "success": True, "message": "ok", "code": 0,
}


class _AkRaising:
    """akshare 桩：模拟它在 result:null 时的解析崩溃。"""

    def __init__(self, exc: BaseException):
        self._exc = exc

    def stock_lhb_detail_em(self, **kwargs):
        raise self._exc


def _provider_whose_akshare_crashes(exc: BaseException = _UPSTREAM_CRASH) -> CnAkshareProvider:
    p = CnAkshareProvider()
    p._ak = lambda: _AkRaising(exc)
    return p


# ══════════════════════════════════════════════════════════════════════════
# 1. 核心：上游解析崩溃时去核实窗口，空窗口 ⇒ 中性「无数据」
# ══════════════════════════════════════════════════════════════════════════


def test_null_result_is_reported_as_no_data_not_as_failure():
    """**核心**：东财 code=9201 / result=null 的窗口不得报成获取失败。"""
    with patch(_HTTP, return_value=EMPTY_WINDOW_RESPONSE):
        out = _provider_whose_akshare_crashes().get_lhb_detail("301689.SZ", "2026-09-26")

    assert "龙虎榜数据获取失败" not in out, (
        f"空窗口被报成失败：{out!r}；它应当是「无龙虎榜数据（非异动日属正常）」"
    )
    assert "TypeError" not in out, f"不得把上游解析崩溃泄漏给用户：{out!r}"
    assert "无龙虎榜数据" in out, f"应给出中性的「无数据」提示，实际 {out!r}"


def test_the_exact_error_from_the_screenshot_no_longer_appears():
    """用户截图里那条文案不得再出现。"""
    with patch(_HTTP, return_value=EMPTY_WINDOW_RESPONSE):
        out = _provider_whose_akshare_crashes().get_lhb_detail("301689.SZ", "2020-01-01")

    assert "'NoneType' object is not subscriptable" not in out, out
    assert "龙虎榜数据获取失败" not in out, out


def test_rows_lost_by_the_upstream_parser_are_recovered():
    """有数据的窗口：数据是被上游解析器丢的，必须还给用户而不是放弃。"""
    with patch(_HTTP, return_value=DATA_WINDOW_RESPONSE):
        out = _provider_whose_akshare_crashes().get_lhb_detail("301689.SZ", "2026-09-29")

    assert "电科思仪" in out, f"直连已拿到数据，必须返回内容，实际 {out!r}"
    assert "龙虎榜数据获取失败" not in out, out


def test_other_symbol_on_the_same_day_is_not_reported():
    """**归属**：同一天别的股票的行不得挂到本标的标题下。"""
    other = dict(ROW, SECURITY_CODE="000001", SECURITY_NAME_ABBR="平安银行")
    payload = {"result": {"pages": 1, "data": [other, ROW]}, "success": True, "code": 0}
    with patch(_HTTP, return_value=payload):
        out = _provider_whose_akshare_crashes().get_lhb_detail("301689.SZ", "2026-09-29")

    assert "电科思仪" in out
    assert "平安银行" not in out, f"不得把别的标的的行混进来：{out!r}"


# ══════════════════════════════════════════════════════════════════════════
# 2. 反过度修正：真失败仍必须是失败，且不得无故回退
# ══════════════════════════════════════════════════════════════════════════


def test_unknown_business_error_code_is_still_a_failure():
    """未知业务错误码不得被当成"没有数据"。"""
    payload = {"result": None, "success": False, "code": 9999, "message": "系统繁忙"}
    with patch(_HTTP, return_value=payload):
        out = _provider_whose_akshare_crashes().get_lhb_detail("301689.SZ", "2026-09-29")

    assert "龙虎榜数据获取失败" in out, f"未知错误码应如实报失败，实际 {out!r}"


def test_direct_path_failure_is_still_a_failure():
    """akshare 崩了、直连也连不上 ⇒ 如实报失败（不得 fail-open）。"""
    with patch(_HTTP, side_effect=ConnectionError("boom")):
        out = _provider_whose_akshare_crashes().get_lhb_detail("301689.SZ", "2026-09-29")

    assert "龙虎榜数据获取失败" in out, out


def test_network_error_on_akshare_does_not_trigger_the_direct_fallback():
    """**关键边界**：网络异常不是「解析崩溃」，不得触发回退。

    如果这里回退，一次真实网络故障就可能被直连的偶发成功掩盖，
    或者被误报成「无数据」——那是 fail-open。
    """
    def _must_not_be_called(*a, **k):
        raise AssertionError("非解析类错误不得触发直连回退")

    with patch(_HTTP, side_effect=_must_not_be_called):
        out = _provider_whose_akshare_crashes(ConnectionError("ChunkedEncodingError")).get_lhb_detail(
            "301689.SZ", "2026-09-29"
        )

    assert "龙虎榜数据获取失败" in out, out
    assert "ChunkedEncodingError" in out, "原始错误必须保留，便于定位"


def test_missing_akshare_function_does_not_trigger_the_direct_fallback():
    """缺函数同样是"能力不足"，不是解析崩溃。"""
    def _must_not_be_called(*a, **k):
        raise AssertionError("缺函数不得触发直连回退")

    provider = CnAkshareProvider()
    provider._ak = lambda: SimpleNamespace()  # 没有 stock_lhb_detail_em
    with patch(_HTTP, side_effect=_must_not_be_called):
        out = provider.get_lhb_detail("600176.SH", "2026-06-16")

    assert "龙虎榜数据获取失败" in out, out


def test_unrecognisable_date_is_still_a_hard_error():
    """日期无法识别仍按原样报错，且不应白跑一次网络请求。"""
    def _must_not_be_called(*a, **k):
        raise AssertionError("非法日期不得发起网络请求")

    with patch(_HTTP, side_effect=_must_not_be_called):
        out = _provider_whose_akshare_crashes().get_lhb_detail("301689.SZ", "not-a-date")

    assert "日期无法识别" in out, out


# ══════════════════════════════════════════════════════════════════════════
# 3. 直连函数自身的语义（单元级）
# ══════════════════════════════════════════════════════════════════════════


def test_direct_fetch_returns_empty_frame_for_the_empty_window_code():
    from tradingagents.dataflows.providers.cn_eastmoney_direct import fetch_em_lhb_detail_direct

    with patch(_HTTP, return_value=EMPTY_WINDOW_RESPONSE):
        df = fetch_em_lhb_detail_direct("2026-09-26", "2026-09-26")

    assert isinstance(df, pd.DataFrame)
    assert df.empty, "空窗口必须是空表，而不是异常"


def test_direct_fetch_returns_rows_when_present():
    from tradingagents.dataflows.providers.cn_eastmoney_direct import fetch_em_lhb_detail_direct

    with patch(_HTTP, return_value=DATA_WINDOW_RESPONSE):
        df = fetch_em_lhb_detail_direct("2026-09-29", "2026-09-29")

    assert len(df) == 1
    assert df.iloc[0]["SECURITY_CODE"] == "301689"


def test_direct_fetch_raises_on_unknown_business_code():
    """未知业务码必须抛错，绝不能吞成空表。"""
    from tradingagents.dataflows.providers.cn_eastmoney_direct import fetch_em_lhb_detail_direct

    payload = {"result": None, "success": False, "code": 9999, "message": "系统繁忙"}
    with patch(_HTTP, return_value=payload):
        with pytest.raises(RuntimeError):
            fetch_em_lhb_detail_direct("2026-09-29", "2026-09-29")


def test_direct_fetch_rejects_a_bad_date():
    from tradingagents.dataflows.providers.cn_eastmoney_direct import fetch_em_lhb_detail_direct

    with pytest.raises(ValueError):
        fetch_em_lhb_detail_direct("not-a-date", "2026-09-29")


def test_missing_code_column_still_refuses_to_guess():
    """没有代码列时不得退化成返回全表（那正是旧 bug 的形态）。"""
    from tradingagents.dataflows.providers.cn_akshare_provider import _filter_lhb_rows
    from tradingagents.dataflows.providers.errors import SourceUnsupported

    with pytest.raises(SourceUnsupported):
        _filter_lhb_rows(pd.DataFrame([{"名称": "电科思仪", "涨跌幅": -8.9}]), "301689")
