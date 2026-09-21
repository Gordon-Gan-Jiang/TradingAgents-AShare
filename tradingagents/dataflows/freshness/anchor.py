from __future__ import annotations

from datetime import datetime

from tradingagents.dataflows.trade_calendar import (
    CN_TZ,
    cn_today_str,
    is_cn_trading_day,
    previous_cn_trading_day,
)


def _infer_analysis_mode(trade_date: str, now: datetime | None = None) -> str:
    now_dt = now or datetime.now(CN_TZ)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=CN_TZ)
    else:
        now_dt = now_dt.astimezone(CN_TZ)
    today = now_dt.date().strftime("%Y-%m-%d")
    if trade_date > today:
        return "forward_look"
    if trade_date < today:
        return "historical" if is_cn_trading_day(trade_date) else "closed"
    if not is_cn_trading_day(today):
        return "closed"
    t = now_dt.time()
    if t.hour < 9 or (t.hour == 9 and t.minute < 30):
        return "pre_market"
    if (t.hour == 11 and t.minute >= 30) or t.hour == 12:
        return "lunch_break"
    if t.hour >= 15:
        return "post_market"
    if t.hour < 11 or (t.hour == 11 and t.minute < 30) or t.hour >= 13:
        return "intraday"
    return "intraday"


def resolve_expected_anchor(
    trade_date: str,
    *,
    analysis_mode: str | None = None,
    symbol: str = "600519.SH",
    now: datetime | None = None,
) -> str:
    """Return the trading-day anchor calendar-anchored sources should meet."""
    del symbol  # reserved for US market extension
    mode = analysis_mode or _infer_analysis_mode(trade_date, now=now)
    today = cn_today_str() if now is None else now.astimezone(CN_TZ).date().strftime("%Y-%m-%d")

    if mode in ("historical", "t_plus_1", "forward_look"):
        if trade_date <= today and is_cn_trading_day(trade_date):
            return trade_date
        return previous_cn_trading_day(trade_date) if trade_date > today else trade_date

    if mode in ("intraday", "pre_market", "lunch_break"):
        return previous_cn_trading_day(today)

    if mode == "post_market":
        return today if is_cn_trading_day(today) else previous_cn_trading_day(today)

    return previous_cn_trading_day(today)


def is_in_grace_window(
    *,
    grace_minutes: int,
    analysis_mode: str,
    now: datetime | None = None,
) -> bool:
    if grace_minutes <= 0 or analysis_mode != "post_market":
        return False
    now_dt = now or datetime.now(CN_TZ)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=CN_TZ)
    else:
        now_dt = now_dt.astimezone(CN_TZ)
    today = now_dt.date().strftime("%Y-%m-%d")
    if not is_cn_trading_day(today):
        return False
    close_dt = now_dt.replace(hour=15, minute=0, second=0, microsecond=0)
    if now_dt < close_dt:
        return False
    elapsed_min = (now_dt - close_dt).total_seconds() / 60.0
    return elapsed_min <= grace_minutes
