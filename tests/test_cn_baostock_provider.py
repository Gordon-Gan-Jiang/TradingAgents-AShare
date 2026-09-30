"""`CnBaoStockProvider` 的行为契约测试。

这一组测试针对本次事故的三条教训，每条都钉住一个**曾经真实发生**的失败：

1. **连不上 ≠ 没实现**。旧版把登录失败写成
   `NotImplementedError("baostock login failed: ...")`，于是一次上游主机名迁移
   （`www.baostock.com` → `public-api.baostock.com`）在系统里表现为"这个 provider
   没实现该功能"，没人去查。现在必须是 `SourceUnavailable`。
2. **必须有超时**。旧版 baostock 的 socket 不设 timeout，实测登录挂死 **75 秒**。
   这里断言调用期间设置了默认超时，并且**调用结束后恢复原值**（不能污染全局）。
3. **帧契约必须与信号层一致**。`get_daily_bars_df` 必须是**小写**
   `date/open/high/low/close/volume`，`date` 为 `YYYY-MM-DD`——大写帧交给信号层会
   变成 `InsufficientHistory`（历史上真的这样翻过车）。
"""

from __future__ import annotations

import socket
from types import SimpleNamespace

import pandas as pd
import pytest

from tradingagents.dataflows.providers.cn_baostock_provider import CnBaoStockProvider
from tradingagents.dataflows.providers.errors import (
    SourceEmpty,
    SourceUnavailable,
    SourceUnsupported,
)
from tradingagents.dataflows.providers import support


class _FakeResult:
    """最小可用的 baostock ResultData 替身。"""

    def __init__(self, fields=(), rows=(), error_code="0", error_msg="success"):
        self.fields = list(fields)
        self._rows = [list(r) for r in rows]
        self.error_code = error_code
        self.error_msg = error_msg
        self._index = -1

    def next(self):
        self._index += 1
        return self._index < len(self._rows)

    def get_row_data(self):
        return self._rows[self._index]


class _FakeBaoStock:
    """按调用名分派结果的假 SDK，并记录全部调用参数。"""

    def __init__(self, *, login_error="0", results=None, login_raises=None):
        self.login_error = login_error
        self.login_raises = login_raises
        self.results = dict(results or {})
        self.calls = []
        self.logged_out = False

    def login(self):
        if self.login_raises is not None:
            raise self.login_raises
        return SimpleNamespace(error_code=self.login_error, error_msg="login-msg")

    def logout(self):
        self.logged_out = True
        return SimpleNamespace(error_code="0", error_msg="success")

    def _dispatch(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        if name not in self.results:
            return _FakeResult()
        value = self.results[name]
        return value() if callable(value) else value

    def query_history_k_data_plus(self, *a, **kw):
        return self._dispatch("query_history_k_data_plus", *a, **kw)

    def query_profit_data(self, *a, **kw):
        return self._dispatch("query_profit_data", *a, **kw)

    def query_balance_data(self, *a, **kw):
        return self._dispatch("query_balance_data", *a, **kw)

    def query_cash_flow_data(self, *a, **kw):
        return self._dispatch("query_cash_flow_data", *a, **kw)

    def query_growth_data(self, *a, **kw):
        return self._dispatch("query_growth_data", *a, **kw)

    def query_operation_data(self, *a, **kw):
        return self._dispatch("query_operation_data", *a, **kw)

    def query_dupont_data(self, *a, **kw):
        return self._dispatch("query_dupont_data", *a, **kw)


@pytest.fixture
def provider():
    return CnBaoStockProvider()


def _install(monkeypatch, provider, fake):
    monkeypatch.setattr(provider, "_bs", lambda: fake, raising=True)
    return fake


_D_ROW = ["2026-09-23", "1255.03", "1271.50", "1250.89", "1251.24", "3098122"]


# ---------------------------------------------------------------- 能力声明


def test_capability_matrix_methods_are_really_implemented(provider):
    """矩阵把 Baostock 当历史地基，这些方法必须真的实现（不能是占位）。"""
    impl = support.implemented_methods(type(provider))
    for method in (
        "get_stock_data",
        "get_indicators",
        "get_daily_bars_df",
        "get_intraday_bars",
        "get_fundamentals",
        "get_balance_sheet",
        "get_cashflow",
        "get_income_statement",
    ):
        assert method in impl, f"{method} 仍未实现"


def test_news_capabilities_are_declared_unsupported(provider):
    """Baostock 没有新闻能力：必须是"没实现"（矩阵据此不把它排进 news_data 链）。

    这与 SourceUnavailable 的分工正是本次事故的核心区分。
    """
    impl = support.implemented_methods(type(provider))
    for method in (
        "get_news",
        "get_global_news",
        "get_insider_transactions",
        "get_realtime_quotes",
    ):
        assert method not in impl


# ---------------------------------------------------------------- 错误语义


def test_login_failure_raises_source_unavailable_not_notimplemented(monkeypatch, provider):
    """事故回归：连不上必须是"不可用"，不能伪装成"未实现"。"""
    _install(monkeypatch, provider, _FakeBaoStock(login_error="10002007"))

    with pytest.raises(SourceUnavailable) as excinfo:
        provider.get_daily_bars_df("600519.SH", "2026-09-01", "2026-09-23")

    # 关键：不能再落进 router 里与占位实现同一个分支
    assert not isinstance(excinfo.value, NotImplementedError)
    assert "10002007" in str(excinfo.value)


def test_login_exception_is_converted_to_source_unavailable(monkeypatch, provider):
    """socket 层异常（Broken pipe / 超时）同样必须表达为"不可用"。"""
    _install(monkeypatch, provider, _FakeBaoStock(login_raises=BrokenPipeError("boom")))

    with pytest.raises(SourceUnavailable):
        provider.get_stock_data("600519.SH", "2026-09-01", "2026-09-23")


def test_query_error_raises_source_unavailable(monkeypatch, provider):
    fake = _FakeBaoStock(
        results={
            "query_history_k_data_plus": _FakeResult(
                error_code="10002007", error_msg="网络接收错误。"
            )
        }
    )
    _install(monkeypatch, provider, fake)

    with pytest.raises(SourceUnavailable):
        provider.get_daily_bars_df("600519.SH", "2026-09-01", "2026-09-23")


def test_empty_history_raises_source_empty(monkeypatch, provider):
    """取到了但没数据 → SourceEmpty，好让链继续问下一个源（而不是伪装成成功）。"""
    _install(monkeypatch, provider, _FakeBaoStock(results={"query_history_k_data_plus": _FakeResult(fields=["date"])}))

    with pytest.raises(SourceEmpty):
        provider.get_daily_bars_df("600519.SH", "2026-09-01", "2026-09-23")


def test_non_a_share_symbol_is_unsupported(provider):
    """非 6 位数字代码：结构性不支持 → SourceUnsupported（矩阵可据此把它排除）。"""
    with pytest.raises(SourceUnsupported):
        provider.get_daily_bars_df("AAPL", "2026-09-01", "2026-09-23")


def test_news_raises_source_unsupported(provider):
    with pytest.raises(SourceUnsupported):
        provider.get_news("600519.SH", "2026-09-01", "2026-09-23")


# ---------------------------------------------------------------- 超时


def test_socket_default_timeout_is_set_during_call_and_restored_after(monkeypatch, provider):
    """事故回归：旧版 socket 无 timeout，登录挂死 75 秒。

    断言调用期间有超时，且结束后**恢复原值**——探测/分析跑在线程里，
    污染全局 socket 默认超时会波及其它 HTTP 调用。
    """
    original = socket.getdefaulttimeout()
    monkeypatch.setattr(socket, "setdefaulttimeout", socket.setdefaulttimeout)
    seen: dict[str, float | None] = {}

    fake = _FakeBaoStock(
        results={
            "query_history_k_data_plus": lambda: (
                seen.setdefault("during", socket.getdefaulttimeout()),
                _FakeResult(
                    fields=["date", "open", "high", "low", "close", "volume"],
                    rows=[_D_ROW],
                ),
            )[1]
        }
    )
    _install(monkeypatch, provider, fake)

    provider.get_daily_bars_df("600519.SH", "2026-09-01", "2026-09-23")

    assert seen["during"] is not None and seen["during"] > 0, "调用期间没有设置 socket 超时"
    assert socket.getdefaulttimeout() == original, "socket 默认超时没有被恢复"


def test_timeout_is_restored_even_when_call_fails(monkeypatch, provider):
    original = socket.getdefaulttimeout()
    _install(monkeypatch, provider, _FakeBaoStock(login_error="1"))

    with pytest.raises(SourceUnavailable):
        provider.get_stock_data("600519.SH", "2026-09-01", "2026-09-23")

    assert socket.getdefaulttimeout() == original


# ---------------------------------------------------------------- 帧契约


def test_daily_bars_follow_signal_contract(monkeypatch, provider):
    _install(
        monkeypatch,
        provider,
        _FakeBaoStock(
            results={
                "query_history_k_data_plus": _FakeResult(
                    fields=["date", "open", "high", "low", "close", "volume"],
                    rows=[_D_ROW],
                )
            }
        ),
    )

    frame = provider.get_daily_bars_df("600519.SH", "2026-09-01", "2026-09-23")

    assert list(frame.columns) == ["date", "open", "high", "low", "close", "volume"]
    assert frame.loc[0, "date"] == "2026-09-23"
    assert frame.loc[0, "close"] == pytest.approx(1251.24)
    assert str(frame["close"].dtype).startswith("float")


def test_daily_bars_request_uses_forward_adjusted_prices(monkeypatch, provider):
    fake = _install(
        monkeypatch,
        provider,
        _FakeBaoStock(
            results={
                "query_history_k_data_plus": _FakeResult(
                    fields=["date", "open", "high", "low", "close", "volume"],
                    rows=[_D_ROW],
                )
            }
        ),
    )

    provider.get_daily_bars_df("600519.SH", "2026-09-01", "2026-09-23")

    _, args, kwargs = fake.calls[0]
    assert args[0] == "sh.600519", "未按 baostock 的 sh./sz. 前缀规范化代码"
    assert kwargs["frequency"] == "d"
    assert kwargs["adjustflag"] == "2", "adjustflag=2 是前复权，改成别的会静默改变价格含义"


def test_intraday_bars_use_minute_frequency(monkeypatch, provider):
    fake = _install(
        monkeypatch,
        provider,
        _FakeBaoStock(
            results={
                "query_history_k_data_plus": _FakeResult(
                    fields=["date", "time", "open", "high", "low", "close", "volume"],
                    rows=[["2026-09-23", "20260923100500000", "1255.0", "1256.0", "1254.0", "1255.5", "1200"]],
                )
            }
        ),
    )

    frame = provider.get_intraday_bars("600519.SH", "2026-09-23", freq="5")

    _, _, kwargs = fake.calls[0]
    assert kwargs["frequency"] == "5"
    assert "time" in frame.columns
    assert "close" in frame.columns


# --------------------------------------------------------------- 指数


def test_index_daily_bars_follow_signal_contract(monkeypatch, provider):
    """指数日线必须与个股日线**同契约**：小写 OHLCV + date=YYYY-MM-DD。"""
    _install(
        monkeypatch,
        provider,
        _FakeBaoStock(
            results={
                "query_history_k_data_plus": _FakeResult(
                    fields=["date", "open", "high", "low", "close", "volume"],
                    rows=[["2026-09-23", "4547.61", "4547.69", "4513.45", "4517.28", "15714382900"]],
                )
            }
        ),
    )

    frame = provider.get_index_daily_bars_df("sh000300", "2026-09-15", "2026-09-23")

    assert list(frame.columns) == ["date", "open", "high", "low", "close", "volume"]
    assert frame.loc[0, "date"] == "2026-09-23"
    assert frame.loc[0, "close"] == pytest.approx(4517.28)


def test_index_daily_bars_normalize_code_with_dot(monkeypatch, provider):
    """指数代码必须转成 baostock 的 `sh.000300`。"""
    fake = _install(
        monkeypatch,
        provider,
        _FakeBaoStock(
            results={
                "query_history_k_data_plus": _FakeResult(
                    fields=["date", "open", "high", "low", "close", "volume"],
                    rows=[["2026-09-23", "1", "1", "1", "1", "1"]],
                )
            }
        ),
    )

    provider.get_index_daily_bars_df("sh000300", "2026-09-15", "2026-09-23")

    _, args, kwargs = fake.calls[0]
    assert args[0] == "sh.000300"
    assert kwargs["frequency"] == "d"


def test_index_code_normalization_rules(provider):
    """指数代码口径**不同于个股**——这是本组测试的核心。

    `000300` 用个股规则会得到 `sz.000300`（一只不相干的股票），
    `000001` 则会变成平安银行而不是上证指数。取错标的**不会报错**，
    所以必须用指数的规则单独钉住。
    """
    assert provider._normalize_index_symbol("sh000300") == "sh.000300"
    assert provider._normalize_index_symbol("000300") == "sh.000300"
    assert provider._normalize_index_symbol("000001") == "sh.000001", (
        "裸码 000001 在指数口径下是上证指数，不是平安银行"
    )
    assert provider._normalize_index_symbol("399006") == "sz.399006"
    assert provider._normalize_index_symbol("SZ399001") == "sz.399001"
    assert provider._normalize_index_symbol("sh.000905") == "sh.000905"


def test_index_code_differs_from_stock_code_for_the_same_digits(provider):
    """同一个 `000300` 在两条口径下必须落到不同市场——否则就是静默取错标的。"""
    assert provider._normalize_symbol("000300") != provider._normalize_index_symbol("000300")


def test_index_unparseable_code_is_unsupported(provider):
    from tradingagents.dataflows.providers.errors import SourceUnsupported

    with pytest.raises(SourceUnsupported):
        provider._normalize_index_symbol("not-a-code")


def test_index_empty_result_raises_source_empty(monkeypatch, provider):
    """空结果抛 SourceEmpty（而不是返回空帧），让链继续问下一个源。"""
    from tradingagents.dataflows.providers.errors import SourceEmpty

    _install(
        monkeypatch,
        provider,
        _FakeBaoStock(
            results={
                "query_history_k_data_plus": _FakeResult(
                    fields=["date", "open", "high", "low", "close", "volume"], rows=[]
                )
            }
        ),
    )

    with pytest.raises(SourceEmpty):
        provider.get_index_daily_bars_df("sh000300", "2026-09-15", "2026-09-23")


def test_symbol_normalization_rules(provider):
    assert provider._normalize_symbol("600519.SH") == "sh.600519"
    assert provider._normalize_symbol("600519") == "sh.600519"
    assert provider._normalize_symbol("300750.SZ") == "sz.300750"
    assert provider._normalize_symbol("000001") == "sz.000001"
    # 指数走同一路径（sh.000001）
    assert provider._normalize_symbol("sh.000001") == "sh.000001"


# ---------------------------------------------------------------- 财务


def test_fundamentals_maps_ratio_tables_and_labels_provenance(monkeypatch, provider):
    """Baostock 提供的是**季频财务指标**，不是完整报表——输出必须说明来源与口径。"""
    _install(
        monkeypatch,
        provider,
        _FakeBaoStock(
            results={
                "query_profit_data": _FakeResult(
                    fields=["code", "pubDate", "statDate", "roeAvg", "netProfit"],
                    rows=[["sh.600519", "2026-04-30", "2026-03-31", "0.0912", "2.4e10"]],
                ),
                "query_balance_data": _FakeResult(
                    fields=["code", "pubDate", "statDate", "currentRatio", "liabilityToAsset"],
                    rows=[["sh.600519", "2026-04-30", "2026-03-31", "3.2", "0.18"]],
                ),
                "query_cash_flow_data": _FakeResult(
                    fields=["code", "pubDate", "statDate", "CFOToNP"],
                    rows=[["sh.600519", "2026-04-30", "2026-03-31", "1.05"]],
                ),
                "query_growth_data": _FakeResult(fields=["code"], rows=[["sh.600519"]]),
                "query_operation_data": _FakeResult(fields=["code"], rows=[["sh.600519"]]),
                "query_dupont_data": _FakeResult(fields=["code"], rows=[["sh.600519"]]),
            }
        ),
    )

    text = provider.get_fundamentals("600519.SH", curr_date="2026-09-23")

    assert "600519.SH" in text
    assert "roeAvg" in text or "净资产收益率" in text
    assert "baostock" in text.lower(), "输出必须标明数据来自 baostock（口径与 AKShare 不同）"


def test_balance_sheet_returns_profitability_and_solvency_table(monkeypatch, provider):
    _install(
        monkeypatch,
        provider,
        _FakeBaoStock(
            results={
                "query_balance_data": _FakeResult(
                    fields=["code", "pubDate", "statDate", "currentRatio"],
                    rows=[["sh.600519", "2026-04-30", "2026-03-31", "3.2"]],
                )
            }
        ),
    )
    text = provider.get_balance_sheet("600519.SH", curr_date="2026-09-23")
    assert "currentRatio" in text


def test_fundamentals_walk_back_quarters_when_latest_is_empty(monkeypatch, provider):
    """最新季度还没披露时要回退到上一季度，而不是直接报"无数据"。"""
    calls: list[tuple] = []

    def profit(*args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) < 3:
            return _FakeResult(fields=["code"])
        return _FakeResult(fields=["code", "statDate"], rows=[["sh.600519", "2026-03-31"]])

    _install(
        monkeypatch,
        provider,
        _FakeBaoStock(results={"query_profit_data": profit}),
    )

    text = provider.get_fundamentals("600519.SH", curr_date="2026-09-23")

    assert len(calls) >= 3, "没有回退季度重试"
    assert "2026-03-31" in text
