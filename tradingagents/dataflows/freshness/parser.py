from __future__ import annotations

import io
import re
from datetime import datetime
from typing import Any

_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_YYYYMMDD_RE = re.compile(r"\b(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])\b")
_CUTOFF_RE = re.compile(r"数据截止\s*(\d{4}-\d{2}-\d{2})")
_DATETIME_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})(?:[ T](\d{2}:\d{2}(?::\d{2})?))?"
)
_ERROR_MARKERS = (
    "调用失败",
    "获取失败",
    "暂不可用",
    "failed on all hosts",
    "N/A：",
)
_ERROR_REGEXES = (
    re.compile(r"(?i)data not found|404 not found|接口不存在"),
)


def detect_fetch_error(raw: Any) -> tuple[bool, str | None, str | None]:
    if raw is None:
        return True, "empty_response", "数据源返回空"
    if isinstance(raw, dict) and raw.get("_fetch_error"):
        return True, str(raw.get("error_code") or "fetch_error"), str(raw.get("error_message") or "拉取失败")
    text = str(raw).strip()
    if not text or text in ("无数据", "None"):
        return True, "empty_response", "数据源返回空"
    for marker in _ERROR_MARKERS:
        if marker in text:
            code = "provider_exception" if "调用失败" in text else "provider_unavailable"
            return True, code, text[:200]
    for pattern in _ERROR_REGEXES:
        if pattern.search(text):
            return True, "provider_unavailable", text[:200]
    return False, None, None


def parse_last_date_from_csv(raw: str) -> str | None:
    if not isinstance(raw, str) or len(raw) < 20:
        return None
    try:
        import pandas as pd

        df = pd.read_csv(io.StringIO(raw), on_bad_lines="skip", comment="#")
    except Exception:
        return _last_date_in_text(raw)
    if df.empty:
        return None
    cols = {c.lower(): c for c in df.columns}
    date_col = cols.get("date")
    if not date_col:
        return _last_date_in_text(raw)
    series = df[date_col].dropna()
    if series.empty:
        return None
    last = str(series.iloc[-1])[:10]
    return last if _DATE_RE.fullmatch(last) else None


def parse_cutoff_from_text(raw: str) -> str | None:
    if not isinstance(raw, str):
        return None
    m = _CUTOFF_RE.search(raw)
    if m:
        return m.group(1)
    header_dates = _DATE_RE.findall(raw[:400])
    if header_dates:
        return header_dates[-1]
    dates = _DATE_RE.findall(raw)
    return dates[-1] if dates else None


def parse_latest_event_timestamp(raw: Any) -> datetime | None:
    text = str(raw or "")
    latest: datetime | None = None
    for match in _DATETIME_RE.finditer(text):
        date_part = match.group(1)
        time_part = match.group(2) or "00:00:00"
        if len(time_part) == 5:
            time_part = f"{time_part}:00"
        try:
            candidate = datetime.strptime(f"{date_part} {time_part}", "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if latest is None or candidate > latest:
            latest = candidate
    return latest


def expected_disclosure_period(trade_date: str) -> str:
    """Latest quarterly report period expected to be disclosed by trade_date."""
    d = datetime.strptime(trade_date, "%Y-%m-%d")
    month, year = d.month, d.year
    if month <= 4:
        return f"{year - 1}-Q4"
    if month <= 8:
        return f"{year}-Q1"
    if month <= 10:
        return f"{year}-Q2"
    return f"{year}-Q3"


def disclosure_period_lag(actual: str, expected: str) -> int:
    """Return how many quarters actual lags behind expected (0 = current)."""
    def _parse(period: str) -> tuple[int, int]:
        m = re.match(r"(20\d{2})-Q([1-4])", period)
        if m:
            return int(m.group(1)), int(m.group(2))
        m2 = re.match(r"(20\d{2})-(\d{2})", period)
        if m2:
            return int(m2.group(1)), (int(m2.group(2)) - 1) // 3 + 1
        return 0, 0

    ay, aq = _parse(actual)
    ey, eq = _parse(expected)
    if not ay or not ey:
        return 0
    return max(0, (ey - ay) * 4 + (eq - aq))


def _last_date_in_text(text: str) -> str | None:
    dates = _DATE_RE.findall(text)
    return dates[-1] if dates else None


def yyyymmdd_to_disclosure_period(value: str) -> str | None:
    """Map A-share report period end (YYYYMMDD) to YYYY-Qn."""
    m = _YYYYMMDD_RE.fullmatch(str(value).strip())
    if not m:
        return None
    year, month = int(m.group(1)), int(m.group(2))
    quarter = (month - 1) // 3 + 1
    if quarter < 1 or quarter > 4:
        return None
    return f"{year}-Q{quarter}"


def _latest_disclosure_period_from_yyyymmdd(text: str) -> str | None:
    candidates: list[tuple[str, str]] = []
    for match in _YYYYMMDD_RE.finditer(text):
        token = match.group(0)
        period = yyyymmdd_to_disclosure_period(token)
        if period:
            candidates.append((token, period))
    if not candidates:
        return None
    _token, period = max(candidates, key=lambda item: item[0])
    return period


def parse_disclosure_period(raw: Any) -> str | None:
    text = str(raw or "")
    explicit = re.search(r"最新披露期\s*[:：]\s*(20\d{2}-Q[1-4])", text)
    if explicit:
        return explicit.group(1)
    latest = _latest_disclosure_period_from_yyyymmdd(text)
    if latest:
        return latest
    m = re.search(r"(20\d{2})[年Qq\-/ ]*([1-4])", text)
    if m:
        return f"{m.group(1)}-Q{m.group(2)}"
    dates = _DATE_RE.findall(text)
    if dates:
        last = dates[-1]
        ym = last[:7]
        year_s, month_s = ym.split("-", 1)
        quarter = (int(month_s) - 1) // 3 + 1
        return f"{year_s}-Q{quarter}"
    return None


def is_lhb_empty_ok(raw: Any, trade_date: str) -> bool:
    text = str(raw or "")
    if "无龙虎榜数据" in text or "非异动日属正常" in text:
        return True
    if "lhb" in text.lower() and "无" in text and trade_date in text:
        return True
    return False


def trading_days_between(anchor_actual: str, anchor_expected: str) -> int:
    try:
        from tradingagents.dataflows.trade_calendar import _load_cn_trade_dates

        dates, dates_set = _load_cn_trade_dates()
        if not dates:
            a = datetime.strptime(anchor_actual, "%Y-%m-%d").date()
            e = datetime.strptime(anchor_expected, "%Y-%m-%d").date()
            return max(0, (e - a).days)
        a = datetime.strptime(anchor_actual, "%Y-%m-%d").date()
        e = datetime.strptime(anchor_expected, "%Y-%m-%d").date()
        if a >= e:
            return 0
        between = [d for d in dates if a < d <= e]
        return len(between)
    except Exception:
        return 0
