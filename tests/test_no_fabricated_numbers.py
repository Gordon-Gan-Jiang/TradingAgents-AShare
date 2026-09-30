"""不得伪造数字（见 docs/report-judgment-accuracy-audit.md §4.11–§4.14）。

## 共同形状：用 0 冒充"测量结果"

缺失值填 `0` 是最昂贵的一种伪造，因为 `0` 是一个**完全合法的观测值**：
它读起来不像错误，读起来像"市场今天零涨零跌"、"这只票零分红"。
于是没有任何下游机制会触发降级——数据看起来完好无损。

| 位置 | 旧行为 |
|---|---|
| `cn_eastmoney_direct.fetch_em_market_breadth_direct` | 约 5600 只股票的市场被报成 `up=0 down=0` |
| `cn_eastmoney_http_provider._safe_float` | 任何异常 → `0.0` |
| `cn_sina_moneyflow_provider._safe_float` | 非数值 → `0.0` |
| `cn_baostock_provider.get_stock_data` | `Dividends`/`Stock Splits` 恒 `0.0` |

判据：
* 取不到 → `None`/`NaN`/降级，**不得**是 `0`；
* 宽度数据 `total > 0` 时 `up + down + flat > 0`（否则"全市场零涨零跌"是伪事实）；
* 分红/拆股取不到时不得断言「从未分红、从未拆股」。
"""

from __future__ import annotations

import math

import pandas as pd


# ══════════════════════════════════════════════════════════════════════════
# §4.11 市场宽度：total 与 up/down/flat 必须自洽
# ══════════════════════════════════════════════════════════════════════════


def _rows_without_change_field(n=5598):
    """模拟东财返回了 5598 行，但**没有** `f3`（涨跌幅）字段。"""
    return [{"f12": f"6005{i:02d}", "f14": f"股票{i}"} for i in range(n)]


def test_breadth_returns_none_when_change_field_is_absent(monkeypatch):
    """**核心**：没有涨跌幅字段就算不出宽度，必须显式降级（None）。

    旧实现返回 `{up:0, down:0, flat:0, total:5598}`——把约 5600 只股票的
    市场报成「零涨零跌」。而该源是 `market_collector._breadth` 的**首选**
    且不带 `approximate` 标记，这个伪事实会一路进入分析师提示词。
    """
    from tradingagents.dataflows.providers import cn_eastmoney_direct as em

    monkeypatch.setattr(em, "_clist_paged", lambda fs: _rows_without_change_field())

    assert em.fetch_em_market_breadth_direct() is None, (
        "没有涨跌幅字段时无法计算宽度，必须返回 None 让上层降级到其他源"
    )


def test_breadth_never_reports_zero_up_zero_down_for_a_live_market(monkeypatch):
    """只要返回了结果，就不能自相矛盾：total>0 而 up+down+flat==0。"""
    from tradingagents.dataflows.providers import cn_eastmoney_direct as em

    monkeypatch.setattr(em, "_clist_paged", lambda fs: _rows_without_change_field())

    result = em.fetch_em_market_breadth_direct()

    if result is not None:  # pragma: no cover - 上面已断言为 None
        total = result.get("total") or 0
        counted = (result.get("up") or 0) + (result.get("down") or 0) + (result.get("flat") or 0)
        assert not (total > 0 and counted == 0), f"宽度数据自相矛盾：{result}"


def test_breadth_still_counts_real_rises_and_falls(monkeypatch):
    """**反过度修正**：有 f3 时照常统计，一位不差。"""
    from tradingagents.dataflows.providers import cn_eastmoney_direct as em

    rows = [{"f3": 1.2}, {"f3": -0.8}, {"f3": 0.0}, {"f3": 5.0}, {"f3": -3.3}]
    monkeypatch.setattr(em, "_clist_paged", lambda fs: rows)

    result = em.fetch_em_market_breadth_direct()

    assert result is not None
    assert (result["up"], result["down"], result["flat"], result["total"]) == (2, 2, 1, 5)


def test_breadth_empty_payload_is_none(monkeypatch):
    """源返回空列表 = 拿不到，不是"市场没有股票"。"""
    from tradingagents.dataflows.providers import cn_eastmoney_direct as em

    monkeypatch.setattr(em, "_clist_paged", lambda fs: [])

    assert em.fetch_em_market_breadth_direct() is None


# ══════════════════════════════════════════════════════════════════════════
# §4.12 缺失值不得填 0
# ══════════════════════════════════════════════════════════════════════════


def test_eastmoney_http_safe_float_does_not_fabricate_zero():
    """`_safe_float` 在任何异常下返回 0.0 —— 无法区分"是 0"与"读不到"。"""
    from tradingagents.dataflows.providers.cn_eastmoney_http_provider import (
        CnEastmoneyHttpProvider,
    )

    safe = CnEastmoneyHttpProvider._safe_float

    assert safe("1.5") == 1.5
    assert safe(2) == 2.0
    for bad in (None, "--", "", "N/A", object()):
        got = safe(bad)
        assert got is None or (isinstance(got, float) and math.isnan(got)), (
            f"取不到时必须为 None/NaN，不得为 0：_safe_float({bad!r}) → {got!r}"
        )


def test_sina_individual_moneyflow_safe_float_does_not_fabricate_zero():
    """新浪个股资金流同一个形状：非数值 → 0.0。"""
    from tradingagents.dataflows.providers.cn_sina_moneyflow_provider import (
        CnSinaMoneyflowProvider,
    )

    safe = getattr(CnSinaMoneyflowProvider, "_safe_float", None)
    assert safe is not None, "该 provider 应有 _safe_float"

    for bad in (None, "--", "", "N/A"):
        got = safe(bad)
        assert got is None or (isinstance(got, float) and math.isnan(got)), (
            f"取不到时必须为 None/NaN，不得为 0：_safe_float({bad!r}) → {got!r}"
        )


# ══════════════════════════════════════════════════════════════════════════
# §4.14 分红 / 拆股不得恒为 0
# ══════════════════════════════════════════════════════════════════════════


def _parse_provider_csv(text: str) -> pd.DataFrame:
    """`get_stock_data` 返回 `# 注释行 + CSV`，这里剥掉注释再解析。"""
    import io

    body = "\n".join(
        line for line in text.splitlines() if line.strip() and not line.startswith("#")
    )
    return pd.read_csv(io.StringIO(body))


def test_baostock_dividend_columns_are_not_fabricated_zeros(monkeypatch):
    """源不提供分红/拆股时，不得断言「从未分红、从未拆股」。

    `get_stock_data` 的输出会**逐字**进入分析师提示词表格，所以那里的 `0.0`
    等于向模型声明了一个它无权声明的事实。
    """
    from tradingagents.dataflows.providers.cn_baostock_provider import CnBaoStockProvider

    provider = CnBaoStockProvider()
    df = pd.DataFrame(
        {
            "Date": pd.to_datetime(["2026-03-19", "2026-03-20"]),
            "Open": [10.0, 10.2],
            "High": [10.5, 10.4],
            "Low": [9.8, 10.0],
            "Close": [10.2, 10.1],
            "Volume": [1000, 1200],
        }
    )
    monkeypatch.setattr(provider, "_fetch_hist_df", lambda *a, **k: df)

    text = provider.get_stock_data("600519.SH", "2026-03-19", "2026-03-20")
    parsed = _parse_provider_csv(text)

    for col in ("Dividends", "Stock Splits"):
        if col in parsed.columns:
            assert parsed[col].isna().all(), (
                f"{col} 必须表示为「未知」（NaN），不得是 0.0 —— "
                f"0.0 是在断言从未分红/从未拆股：{parsed[col].tolist()}"
            )


def test_akshare_dividend_columns_are_not_fabricated_zeros(monkeypatch):
    """A 股价格主路径（cn_akshare 是 tier 1）同一个伪造，必须一起修。"""
    import tradingagents.dataflows.providers.cn_akshare_provider as ak

    src = ak.__file__
    with open(src, encoding="utf-8") as fh:
        body = fh.read()

    assert 'out["Dividends"] = 0.0' not in body, (
        "cn_akshare_provider 仍在把未提供的分红写成 0.0（断言「从未分红」）"
    )
    assert 'out["Stock Splits"] = 0.0' not in body, (
        "cn_akshare_provider 仍在把未提供的拆股写成 0.0"
    )


# ══════════════════════════════════════════════════════════════════════════
# §4.13 交易日历降级：显式报错，不得按工作日推断（§6 第 12 行第三句）
# ══════════════════════════════════════════════════════════════════════════


def _degraded_calendar(monkeypatch) -> None:
    """让日历源真实失败（走通生产代码的失败路径，而不是给被测函数打桩）。"""
    import sys
    import types

    from tradingagents.dataflows import trade_calendar as tc

    fake_ak = types.ModuleType("akshare")

    def _boom(*_a, **_k):
        raise ConnectionError("simulated akshare calendar outage")

    fake_ak.tool_trade_date_hist_sina = _boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "akshare", fake_ak)
    tc._load_cn_trade_dates.cache_clear()
    tc._reset_calendar_state()


def test_calendar_degradation_raises_instead_of_inferring_weekdays(monkeypatch):
    """日历取不到时 **必须显式报错**，不得用 `d.weekday() < 5` 猜。

    旧行为：`2026-10-01`（国庆，周四）被判为交易日、`next_cn_trading_day("2026-09-30")`
    返回 `2026-10-01`（真实答案是 10-08）、`cn_market_phase()` 在假期返回 `in_session`，
    而 `lru_cache` 会把这份降级日历缓存整个进程生命周期且不留任何日志。
    """
    from tradingagents.dataflows import trade_calendar as tc

    _degraded_calendar(monkeypatch)
    try:
        for fn, arg in (
            (tc.is_cn_trading_day, "2026-10-01"),
            (tc.next_cn_trading_day, "2026-09-30"),
            (tc.previous_cn_trading_day, "2026-10-08"),
            (tc.cn_no_data_reason, "2026-03-19"),
        ):
            try:
                got = fn(arg)
            except tc.TradingCalendarUnavailable:
                continue
            pytest.fail(
                f"{fn.__name__}({arg!r}) 在日历不可用时返回了 {got!r}；"
                f"必须抛 TradingCalendarUnavailable，不得按工作日推断"
            )
    finally:
        tc._load_cn_trade_dates.cache_clear()
        tc._reset_calendar_state()


def test_market_phase_never_claims_in_session_on_a_degraded_holiday(monkeypatch):
    """假期不得被说成「盘中」——这是最容易被当成实盘信号的一条。"""
    from datetime import datetime

    from tradingagents.dataflows import trade_calendar as tc

    _degraded_calendar(monkeypatch)
    try:
        try:
            got = tc.cn_market_phase(datetime(2026, 10, 1, 10, 30, tzinfo=tc.CN_TZ))
        except tc.TradingCalendarUnavailable:
            return
        assert got == "closed", (
            f"日历不可用时对 2026-10-01 10:30（国庆）只能报 closed 或显式报错，"
            f"实际 {got!r}"
        )
    finally:
        tc._load_cn_trade_dates.cache_clear()
        tc._reset_calendar_state()


def test_trading_days_between_never_reports_zero_lag_on_calendar_failure(monkeypatch):
    """`except Exception: return 0` 会把「迟到 30 个交易日」报成「0 天滞后」= fresh。"""
    from tradingagents.dataflows.freshness import parser as fp

    _degraded_calendar(monkeypatch)
    try:
        try:
            got = fp.trading_days_between("2026-03-01", "2026-03-19")
        except Exception as exc:
            assert type(exc).__name__ == "TradingCalendarUnavailable", (
                f"应以 TradingCalendarUnavailable 失败，实际 {type(exc).__name__}: {exc}"
            )
            return
        pytest.fail(f"日历不可用时滞后不得为 {got!r}（0 天即 fresh）")
    finally:
        from tradingagents.dataflows import trade_calendar as tc

        tc._load_cn_trade_dates.cache_clear()
        tc._reset_calendar_state()
