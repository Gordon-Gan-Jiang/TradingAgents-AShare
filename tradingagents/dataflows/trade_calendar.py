from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime, time as _time
from functools import lru_cache
from zoneinfo import ZoneInfo

CN_TZ = ZoneInfo("Asia/Shanghai")

logger = logging.getLogger(__name__)


class TradingCalendarUnavailable(RuntimeError):
    """交易日历取不到 —— 显式报错，**不得**按工作日猜（审计 §4.13）。

    旧实现（`except Exception: return [], set()` + `d.weekday() < 5`）的代价是：
    日历拉取一旦失败，`2026-10-01`（国庆，周四）会被判成**交易日**，
    `next_cn_trading_day("2026-09-30")` 返回 `10-01`（真实答案是 10-08），
    `cn_market_phase()` 在假期返回 `in_session`，而 `lru_cache` 会把这份降级日历
    **缓存整个进程生命周期**——一次网络抖动污染此后所有交易日判断，且无任何日志。

    "不知道今天是不是交易日"与"今天是交易日"是两件完全不同的事。
    """


def now_cn() -> datetime:
    return datetime.now(CN_TZ)


def cn_today_str() -> str:
    return now_cn().date().strftime("%Y-%m-%d")


def _parse_date(date_str: str) -> date:
    return datetime.strptime(date_str, "%Y-%m-%d").date()


# 日历失败后的冷却：避免 70 个调用点在源持续不可用时各自重试一遍网络请求。
# （与 cn_baostock_provider 的 `_login_cooldown` 同一套做法。）
_CALENDAR_FAILURE_COOLDOWN_S = 60.0
_calendar_failure: tuple[float, str] | None = None


def _reset_calendar_state() -> None:
    """清掉失败冷却状态（测试与显式重试用）。"""
    global _calendar_failure
    _calendar_failure = None


@lru_cache(maxsize=1)
def _load_cn_trade_dates() -> tuple[list[date], set[date]]:
    """加载 A 股交易日历；取不到时抛 `TradingCalendarUnavailable`。

    注意：`lru_cache` **不缓存异常**，所以冷却窗口是防止把不可用源打爆的那道闸。
    """
    global _calendar_failure
    if _calendar_failure is not None:
        _ts, _msg = _calendar_failure
        if time.monotonic() - _ts < _CALENDAR_FAILURE_COOLDOWN_S:
            raise TradingCalendarUnavailable(_msg)
        _calendar_failure = None

    failure_note = ""
    try:
        import akshare as ak  # type: ignore

        df = ak.tool_trade_date_hist_sina()
        if df is None or df.empty or "trade_date" not in df.columns:
            raise ValueError("empty trade date table")
        try:
            import pandas as pd  # type: ignore

            parsed = pd.to_datetime(df["trade_date"], errors="coerce")
            dates = sorted(pd_dt.date() for pd_dt in parsed if str(pd_dt) != "NaT")
        except Exception:
            # Minimal runtime fallback: best-effort parse via stdlib
            dates = []
            for v in df["trade_date"]:
                try:
                    dates.append(datetime.fromisoformat(str(v)).date())
                except Exception:
                    continue
            dates = sorted(set(dates))
        if not dates:
            raise ValueError("trade date table parsed to zero dates")
        return dates, set(dates)
    except TradingCalendarUnavailable:
        raise
    except Exception as exc:
        # 曾经在这里 `return [], set()`，把"取不到日历"静默降级成"按工作日推断"。
        # 现在显式报错：错误必须比错误答案更早出现。
        failure_note = f"{type(exc).__name__}: {exc}"
        msg = (
            f"无法加载 A 股交易日历（{failure_note}）。"
            "交易日判定、市场阶段与'下一个交易日'都无法可信计算；"
            "请勿按工作日推断（国庆/春节等法定假期会被误判为交易日）。"
        )
        _calendar_failure = (time.monotonic(), msg)
        logger.warning("trade calendar unavailable: %s", failure_note)
        raise TradingCalendarUnavailable(msg) from exc


def is_cn_symbol(symbol: str) -> bool:
    s = symbol.strip().upper()
    return bool(re.match(r"^\d{6}(\.(SH|SZ|SS|BJ))?$", s))


def is_cn_trading_day(date_str: str) -> bool:
    d = _parse_date(date_str)
    _, dates_set = _load_cn_trade_dates()
    return d in dates_set


def next_cn_trading_day(date_str: str) -> str | None:
    """Return the next trading day strictly after *date_str* (YYYY-MM-DD), or None if unknown."""
    d = _parse_date(date_str)
    dates, _ = _load_cn_trade_dates()
    lo, hi = 0, len(dates)
    while lo < hi:
        mid = (lo + hi) // 2
        if dates[mid] <= d:
            lo = mid + 1
        else:
            hi = mid
    if lo < len(dates):
        return dates[lo].strftime("%Y-%m-%d")
    return None
    # Fallback: skip weekends only
    cur = d
    for _ in range(10):
        cur = cur.fromordinal(cur.toordinal() + 1)
        if cur.weekday() < 5:
            return cur.strftime("%Y-%m-%d")
    return None


def previous_cn_trading_day(date_str: str) -> str:
    d = _parse_date(date_str)
    dates, _ = _load_cn_trade_dates()
    # 找到首个 >= d 的位置
    lo, hi = 0, len(dates)
    while lo < hi:
        mid = (lo + hi) // 2
        if dates[mid] < d:
            lo = mid + 1
        else:
            hi = mid
    idx = lo - 1
    if idx >= 0:
        return dates[idx].strftime("%Y-%m-%d")
    # 曾经在这里按工作日回推（`if cur.weekday() < 5`）。日历已经加载成功了却仍
    # 查不到，只能说明 `date_str` 早于日历起点（1990-12-19）——这是越界查询，
    # 不是"日历不可用"，所以用 ValueError 而不是猜一个日子（审计 §4.13）。
    raise ValueError(
        f"{date_str} 早于已知交易日历的起点 {dates[0]}，无法回推上一个交易日"
    )


def cn_market_phase(now: datetime | None = None) -> str:
    now_dt = now or now_cn()
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=CN_TZ)
    else:
        now_dt = now_dt.astimezone(CN_TZ)

    today = now_dt.date().strftime("%Y-%m-%d")
    if not is_cn_trading_day(today):
        return "closed"

    t = now_dt.time()
    if t < _time(9, 30):
        return "pre_open"
    if _time(9, 30) <= t < _time(11, 30):
        return "in_session"
    if _time(11, 30) <= t < _time(13, 0):
        return "lunch_break"
    if _time(13, 0) <= t < _time(15, 0):
        return "in_session"
    return "post_close"


def cn_no_data_reason(date_str: str) -> str:
    if not is_cn_trading_day(date_str):
        return "N/A：非交易日（A股休市）"

    today = cn_today_str()
    if date_str == today:
        phase = cn_market_phase()
        if phase == "pre_open":
            return "N/A：今日尚未开盘"
        if phase in ("in_session", "lunch_break"):
            return "N/A：今日盘中，日线未收盘（可参考实时价）"
        if phase == "post_close":
            return "N/A：今日已收盘，数据源尚未更新"

    return "N/A：该交易日暂无数据（可能停牌或数据延迟）"
