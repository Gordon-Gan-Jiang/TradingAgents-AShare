"""交易日历降级必须**显式报错**，不得按工作日推断（审计 §4.13，§6 第 12 行第三句）。

## 旧行为

```python
@lru_cache(maxsize=1)
def _load_cn_trade_dates():
    try:
        df = ak.tool_trade_date_hist_sina()
        ...
    except Exception:
        return [], set()          # ← 静默降级

def is_cn_trading_day(date_str):
    dates, dates_set = _load_cn_trade_dates()
    if dates:
        return d in dates_set
    return d.weekday() < 5        # ← 猜：工作日就是交易日
```

后果（审计探针实测）：日历加载失败时

* `2026-10-01`（国庆，周四）被判为**交易日**；
* `next_cn_trading_day("2026-09-30")` 返回 `2026-10-01`（真实答案是 10-08）；
* `cn_market_phase()` 返回 `in_session`；
* `cn_no_data_reason()` 把**真实的数据缺口**误诊成「停牌或数据延迟」。

最恶劣的一点：`lru_cache` 把这份**降级日历缓存整个进程生命周期**，于是一次网络抖动
会污染此后所有的交易日判断，且没有任何日志。

## 判据

日历经不可用时，`is_cn_trading_day` / `next_cn_trading_day` / `previous_cn_trading_day` /
`cn_market_phase` / `cn_no_data_reason` 必须抛出 `TradingCalendarUnavailable`
（`RuntimeError` 子类），**不得**返回一个按工作日猜出来的答案。

「不知道今天是不是交易日」和「今天是交易日」是两件完全不同的事，
用 `weekday() < 5` 抹平它们的代价是：在国庆假期照常给出"盘中"结论。
"""

from __future__ import annotations

import sys
import types

import pytest


@pytest.fixture
def degraded_calendar(monkeypatch):
    """让日历源真实失败（akshare 取不到交易日历），走通生产代码的失败路径。"""
    from tradingagents.dataflows import trade_calendar as tc

    fake_ak = types.ModuleType("akshare")

    def _boom(*_a, **_k):
        raise ConnectionError("simulated akshare calendar outage")

    fake_ak.tool_trade_date_hist_sina = _boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "akshare", fake_ak)
    tc._load_cn_trade_dates.cache_clear()
    reset = getattr(tc, "_reset_calendar_state", None)
    if reset is not None:
        reset()
    yield tc
    tc._load_cn_trade_dates.cache_clear()
    if reset is not None:
        reset()


@pytest.fixture
def healthy_calendar():
    from tradingagents.dataflows import trade_calendar as tc

    tc._load_cn_trade_dates.cache_clear()
    yield tc
    tc._load_cn_trade_dates.cache_clear()


# ══════════════════════════════════════════════════════════════════════════
# 0. 异常类型本身
# ══════════════════════════════════════════════════════════════════════════


def test_calendar_unavailable_exception_exists():
    from tradingagents.dataflows.trade_calendar import TradingCalendarUnavailable

    assert issubclass(TradingCalendarUnavailable, RuntimeError), (
        "必须是 RuntimeError 子类：调用方可能没写 try/except，"
        "而静默返回错误答案比抛出异常危险得多"
    )


# ══════════════════════════════════════════════════════════════════════════
# 1. 五个查询函数在降级时都必须显式报错
# ══════════════════════════════════════════════════════════════════════════


def test_is_cn_trading_day_raises_instead_of_guessing(degraded_calendar):
    tc = degraded_calendar

    with pytest.raises(tc.TradingCalendarUnavailable):
        tc.is_cn_trading_day("2026-10-01")


def test_national_day_is_never_a_trading_day_when_calendar_is_degraded(degraded_calendar):
    """审计探针的原始症状：国庆节被判为交易日。"""
    tc = degraded_calendar

    try:
        result = tc.is_cn_trading_day("2026-10-01")
    except tc.TradingCalendarUnavailable:
        return  # 正确答案：显式报错
    pytest.fail(
        f"日历不可用时不得声称 2026-10-01（国庆，周四）是交易日，实际返回 {result!r}"
    )


def test_next_cn_trading_day_does_not_guess_a_holiday(degraded_calendar):
    """旧行为：next(2026-09-30) → 2026-10-01（应为 10-08）。"""
    tc = degraded_calendar

    try:
        result = tc.next_cn_trading_day("2026-09-30")
    except tc.TradingCalendarUnavailable:
        return
    pytest.fail(f"日历不可用时不得按工作日推断下一个交易日，实际返回 {result!r}")


def test_previous_cn_trading_day_does_not_guess(degraded_calendar):
    tc = degraded_calendar

    try:
        result = tc.previous_cn_trading_day("2026-10-08")
    except tc.TradingCalendarUnavailable:
        return
    pytest.fail(f"日历不可用时不得按工作日回推上一个交易日，实际返回 {result!r}")


def test_market_phase_does_not_claim_in_session_on_a_degraded_calendar(degraded_calendar):
    """国庆假期不该被说成"盘中"。"""
    tc = degraded_calendar

    from datetime import datetime

    moment = datetime(2026, 10, 1, 10, 30, tzinfo=tc.CN_TZ)

    try:
        result = tc.cn_market_phase(moment)
    except tc.TradingCalendarUnavailable:
        return
    assert result == "closed", (
        f"日历不可用时对 2026-10-01 10:30 的结论只能是 closed 或显式报错，"
        f"绝不能是 {result!r}"
    )


def test_no_data_reason_does_not_misdiagnose_a_gap_as_a_holiday(degraded_calendar):
    """真实的数据缺口曾被说成「非交易日（A股休市）」。"""
    tc = degraded_calendar

    try:
        result = tc.cn_no_data_reason("2026-03-19")
    except tc.TradingCalendarUnavailable:
        return
    pytest.fail(
        f"日历不可用时不得把缺数据误诊成休市（2026-03-19 是真实交易日），"
        f"实际返回 {result!r}"
    )


# ══════════════════════════════════════════════════════════════════════════
# 2. 反过度修正：日历正常时必须一切照旧
# ══════════════════════════════════════════════════════════════════════════


def test_healthy_calendar_still_answers_normally(healthy_calendar):
    tc = healthy_calendar
    tc._load_cn_trade_dates()  # 确保能加载；加载不了则本环境无网络，跳过断言

    dates, _ = tc._load_cn_trade_dates()
    if not dates:
        pytest.skip("本环境无法加载交易日历，无法验证正常路径")

    assert tc.is_cn_trading_day("2026-10-01") is False, "国庆节必须不是交易日"
    assert tc.is_cn_trading_day("2026-03-19") is True, "普通周四必须是交易日"

    nxt = tc.next_cn_trading_day("2026-09-30")
    assert nxt is not None and nxt >= "2026-10-08", (
        f"国庆后第一个交易日应不早于 2026-10-08，实际 {nxt}"
    )


def test_degradation_recovers_after_the_source_comes_back(monkeypatch):
    """降级不得被永久缓存：源恢复后必须立刻恢复正确判断。"""
    from tradingagents.dataflows import trade_calendar as tc

    tc._load_cn_trade_dates.cache_clear()
    reset = getattr(tc, "_reset_calendar_state", None)
    if reset is not None:
        reset()

    fake_ak = types.ModuleType("akshare")
    fake_ak.tool_trade_date_hist_sina = lambda *a, **k: (_ for _ in ()).throw(  # type: ignore[attr-defined]
        ConnectionError("outage")
    )
    monkeypatch.setitem(sys.modules, "akshare", fake_ak)

    with pytest.raises(tc.TradingCalendarUnavailable):
        tc.is_cn_trading_day("2026-10-01")

    # 现在把 akshare 换成好的实现
    monkeypatch.undo()
    tc._load_cn_trade_dates.cache_clear()
    if reset is not None:
        reset()

    dates, _ = tc._load_cn_trade_dates()
    if not dates:
        pytest.skip("本环境无法加载交易日历，无法验证恢复路径")

    assert tc.is_cn_trading_day("2026-10-01") is False


# ══════════════════════════════════════════════════════════════════════════
# 3. 同一个"读不出即 fresh"的另一个出口：trading_days_between（§4.16）
# ══════════════════════════════════════════════════════════════════════════


def test_trading_days_between_does_not_report_zero_lag_when_calendar_is_down(
    degraded_calendar,
):
    """旧 `except Exception: return 0` 把「算不出来」写成「滞后 0 个交易日」＝最fresh。"""
    from tradingagents.dataflows.freshness import parser as fp

    try:
        result = fp.trading_days_between("2026-03-01", "2026-03-19")
    except Exception as exc:
        assert type(exc).__name__ == "TradingCalendarUnavailable", (
            f"应以 TradingCalendarUnavailable 显式失败，实际 {type(exc).__name__}: {exc}"
        )
        return
    pytest.fail(
        f"日历不可用时不得把滞后报成 {result!r}（0 天恰恰是 fresh 的判据）"
    )


def test_trading_days_between_uses_trading_days_not_calendar_days(healthy_calendar):
    """旧兜底用自然日冒充交易日，会把滞后凭空放大。"""
    from tradingagents.dataflows.freshness import parser as fp

    tc = healthy_calendar
    dates, _ = tc._load_cn_trade_dates()
    if not dates:
        pytest.skip("本环境无法加载交易日历")

    # 2026-09-30(三) → 2026-10-09(五)：自然日 9 天，交易日只有 10-08、10-09 两天。
    assert fp.trading_days_between("2026-09-30", "2026-10-09") == 2


def test_report_age_is_unknown_not_zero_when_calendar_is_down(degraded_calendar):
    """`enrich_report_freshness_display` 曾把「算不出年龄」写成 0 天前。"""
    from tradingagents.dataflows.freshness import access

    summary = {"overall_status": "fresh"}
    out = access.enrich_freshness_summary_for_display(
        summary, created_at_iso="2026-03-01T00:00:00+00:00"
    )

    age = out.get("report_age_days")
    assert age != 0, f"日历不可用时不得把报告年龄报成 0 天（那等于说它是最新的）：{out}"
    assert out.get("report_age_note"), "必须给出可见的说明，而不是静默地当作最新"
