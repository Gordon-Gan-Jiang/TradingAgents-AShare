"""T+1 metrics: scan recommendation quality and depth-report direction accuracy (portfolio vs all symbols)."""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from io import StringIO
from typing import Any
from uuid import uuid4

import pandas as pd
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from api.database import (
    ImportedPortfolioPositionDB,
    MarketDailyPriceDB,
    MarketScanResultDB,
    ReportDB,
    ReportT1OutcomeDB,
    T1DailyStatDB,
    TradePlanDB,
)
from api.services import t1_stats
from tradingagents.dataflows.interface import route_to_vendor
from tradingagents.dataflows.trade_calendar import (
    CN_TZ,
    cn_market_phase,
    cn_today_str,
    is_cn_trading_day,
    next_cn_trading_day,
    previous_cn_trading_day,
)

logger = logging.getLogger(__name__)

ClosePair = tuple[float | None, float | None]
CloseCache = dict[tuple[str, str, str], ClosePair]


class T1VendorSeriesCache:
    """DB-backed price store for T+1 refresh.

    Rule: if (symbol, trade_date) already has stored close/open, never call vendor again.
    """

    __slots__ = ("db", "_map", "_span")

    def __init__(self, db: Session | None = None) -> None:
        self.db = db
        # Backward-compatible in-memory mode (mainly for unit tests).
        self._map: dict[str, dict[str, float]] = {}
        self._span: dict[str, tuple[str, str]] = {}

    def prefetch_pairs(self, pairs: list[tuple[str, str, str]]) -> None:
        """Ensure prices are persisted for needed symbol/date pairs.

        OHLC data is written via a *separate*, immediately-committed session so
        that the long-running main session (self.db) does NOT hold a SQLite write
        lock while waiting for external vendor API calls.  This prevents
        "database is locked" errors on concurrent writes (e.g. watchlist inserts).
        """
        grouped: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for sym_raw, t0, t1 in pairs:
            s = str(sym_raw or "").strip().upper()
            if s and t0 and t1:
                grouped[s].append((t0, t1))

        if self.db is None:
            for sym, intervals in grouped.items():
                mins = [datetime.strptime(a, "%Y-%m-%d") for a, _ in intervals]
                maxs = [datetime.strptime(b, "%Y-%m-%d") for _, b in intervals]
                fs = (min(mins) - timedelta(days=5)).strftime("%Y-%m-%d")
                fe = (max(maxs) + timedelta(days=5)).strftime("%Y-%m-%d")
                self._map[sym] = _fetch_close_map(sym, fs, fe)
                self._span[sym] = (fs, fe)
            return

        for sym, intervals in grouped.items():
            mins = [datetime.strptime(a, "%Y-%m-%d") for a, _ in intervals]
            maxs = [datetime.strptime(b, "%Y-%m-%d") for _, b in intervals]
            fs = (min(mins) - timedelta(days=5)).strftime("%Y-%m-%d")
            fe = (max(maxs) + timedelta(days=5)).strftime("%Y-%m-%d")
            needed_dates = {d for t0, t1 in intervals for d in (t0, t1)}
            stored = _db_close_map(self.db, sym, fs, fe)
            if needed_dates.issubset(stored.keys()):
                continue
            # --- external vendor call (no DB lock held here) ---
            ohlc_map = _fetch_ohlc_map(sym, fs, fe)
            if ohlc_map:
                # Write prices in a *dedicated* short-lived session and commit
                # immediately.  This keeps the write-lock window tiny and prevents
                # the main session from blocking other writers (e.g. watchlist).
                _commit_ohlc_in_own_session(sym, ohlc_map)

    def lookup_closes(self, symbol: str, t0: str, t1: str) -> ClosePair:
        sym = str(symbol or "").strip().upper()
        if not sym:
            return (None, None)
        if self.db is None:
            m = self._map.get(sym)
            if m and t0 in m and t1 in m:
                return (m.get(t0), m.get(t1))
            return _t0_t1_closes(sym, t0, t1)
        m = _db_close_map(self.db, sym, min(t0, t1), max(t0, t1))
        if t0 not in m or t1 not in m:
            # prefetch_pairs now writes via a separate committed session,
            # so WAL-mode readers (self.db) can immediately see the new rows.
            self.prefetch_pairs([(sym, t0, t1)])
            m = _db_close_map(self.db, sym, min(t0, t1), max(t0, t1))
        return (m.get(t0), m.get(t1))


def _security_name_from_result_data(result_data: Any) -> str | None:
    """Prefer Chinese display name stored on the report payload (if not just the ticker)."""
    if not isinstance(result_data, dict):
        return None
    ic = result_data.get("instrument_context")
    if not isinstance(ic, dict):
        return None
    sn = str(ic.get("security_name") or "").strip()
    if not sn:
        return None
    sym = str(ic.get("symbol") or "").strip().upper()
    if sym and sn.upper() == sym:
        return None
    if re.fullmatch(r"[0-9A-Za-z.]+", sn) and len(sn) <= 16:
        return None
    return sn


def _cn_listing_name_for_symbol(symbol: str) -> str | None:
    """Code → Chinese name from the warmed cache only (same source as /v1/reports).

    Lazy import avoids import cycles. Keys in the reverse map are normalized
    (e.g. ``600519.SH``); outcomes may store bare ``600519``.

    This is called once per consensus event / scan row inside request handlers, so
    it must NEVER trigger network I/O: it used to call the blocking on-demand map
    loader, which turned a slow AkShare into a per-event hang (the consensus-trend
    30s timeout). On a cold cache it returns None and the caller keeps the symbol.
    """
    try:
        from api.main import _get_reverse_stock_map_cached_only, _normalize_symbol

        s = str(symbol or "").strip().upper()
        if not s:
            return None
        m = _get_reverse_stock_map_cached_only()
        if not m:
            return None
        # use_name_map=False: the resolver must stay network-free.
        return m.get(s) or m.get(_normalize_symbol(symbol, use_name_map=False))
    except Exception as e:
        logger.debug("cn listing name lookup failed for %s: %s", symbol, e)
        return None


def _resolve_depth_report_display_name(
    symbol: str,
    *,
    portfolio_name: str | None,
    result_data: Any,
) -> str | None:
    pn = str(portfolio_name or "").strip()
    if pn:
        return pn
    from_report = _security_name_from_result_data(result_data)
    if from_report:
        return from_report
    return _cn_listing_name_for_symbol(str(symbol or "").strip().upper())


def _extract_report_model_info(result_data: Any) -> dict[str, Any]:
    _empty: dict[str, Any] = {
        "model_profile_id": None,
        "model_profile_name": None,
        "llm_provider": None,
        "quick_think_llm": None,
        "deep_think_llm": None,
    }
    if not isinstance(result_data, dict):
        return _empty

    # Priority 1: dedicated model_info block
    info = result_data.get("model_info")
    if isinstance(info, dict):
        extracted = {
            "model_profile_id": info.get("model_profile_id"),
            "model_profile_name": info.get("model_profile_name"),
            "llm_provider": info.get("llm_provider"),
            "quick_think_llm": info.get("quick_think_llm"),
            "deep_think_llm": info.get("deep_think_llm"),
        }
        if any(v for v in extracted.values()):
            return extracted

    # Priority 2: top-level result_data keys (older report format)
    top = {
        "model_profile_id": result_data.get("model_profile_id"),
        "model_profile_name": result_data.get("model_profile_name") or result_data.get("model_name"),
        "llm_provider": result_data.get("llm_provider") or result_data.get("provider"),
        "quick_think_llm": result_data.get("quick_think_llm") or result_data.get("backbone_llm"),
        "deep_think_llm": result_data.get("deep_think_llm"),
    }
    if any(v for v in top.values()):
        return top

    # Priority 3: nested under analyst_result or report_meta
    for sub_key in ("report_meta", "analyst_result", "config"):
        sub = result_data.get(sub_key)
        if isinstance(sub, dict):
            candidate = {
                "model_profile_id": sub.get("model_profile_id"),
                "model_profile_name": sub.get("model_profile_name") or sub.get("model_name"),
                "llm_provider": sub.get("llm_provider") or sub.get("provider"),
                "quick_think_llm": sub.get("quick_think_llm") or sub.get("backbone_llm"),
                "deep_think_llm": sub.get("deep_think_llm"),
            }
            if any(v for v in candidate.values()):
                return candidate

    return _empty


def _normalize_created_at(created_at: datetime | None) -> datetime | None:
    if created_at is None:
        return None
    if created_at.tzinfo is None:
        return created_at.replace(tzinfo=timezone.utc)
    return created_at


def _report_price_window(trade_date: str | None, created_at: datetime | None) -> tuple[str, str | None]:
    """Return (p0_date, p1_date) close dates for close→close direction evaluation.

    Segments (CN market time):
    - pre_open on T:     P0 = previous trading-day close, P1 = T close
    - in_session on T:   P0 = T close, P1 = next trading-day close (P0 locked after T close)
    - post_close on T:   P0 = T close, P1 = next trading-day close
    - non-trading day:   treat as post-close of the previous trading day
    """
    if created_at is None:
        p0 = str(trade_date or "").strip() or cn_today_str()
        return p0, _resolve_t1_trade_date(p0)

    local = _normalize_created_at(created_at).astimezone(CN_TZ)
    d = local.strftime("%Y-%m-%d")
    if not is_cn_trading_day(d):
        anchor = previous_cn_trading_day(d)
        return anchor, _resolve_t1_trade_date(anchor)

    phase = cn_market_phase(local)
    if phase == "pre_open":
        return previous_cn_trading_day(d), d
    # in_session, lunch_break, post_close
    return d, _resolve_t1_trade_date(d)


def _report_t1_window(trade_date: str | None, created_at: datetime | None) -> tuple[str, str | None]:
    """Return (p0_date, p1_date). Prefer _report_price_window for new code."""
    return _report_price_window(trade_date, created_at)


def _report_signal_trade_date(trade_date: str | None, created_at: datetime | None) -> str:
    """Arena/chart bucket date = P1 (the predicted trading day)."""
    _p0, p1 = _report_price_window(trade_date, created_at)
    if p1:
        return p1
    return _p0 or cn_today_str()


def _signal_trade_date_from_created(created_at: datetime | None) -> str:
    return _report_signal_trade_date(None, created_at)


def _can_evaluate_price_window(p0_date: str, p1_date: str) -> bool:
    """Both anchor closes must have settled before we label an outcome."""
    return _can_evaluate_t1(p0_date) and _can_evaluate_t1(p1_date)


def _direction_bucket(direction: str | None, decision: str | None) -> str:
    dir_str = str(direction or "").strip()
    dec_str = str(decision or "").strip()
    raw = f"{dir_str} {dec_str}".upper()
    combined_orig = f"{dir_str} {dec_str}"

    bullish_patterns = (
        "BULLISH", "LEAN_BULLISH", "STRONG_BUY", "STRONG BUY",
        "看多", "偏多", "看涨", "做多", "强多",
        "BUY", "增持", "买入", "积极买入", "建仓", "加仓",
    )
    bearish_patterns = (
        "BEARISH", "LEAN_BEARISH", "STRONG_SELL", "STRONG SELL",
        "看空", "偏空", "看跌", "做空", "强空",
        "SELL", "减持", "卖出", "清仓", "减仓", "空头",
    )
    neutral_patterns = (
        "NEUTRAL", "HOLD", "OBSERVE",
        "中性", "持有", "观望", "不操作", "保持中性",
    )

    for b in bullish_patterns:
        if b.upper() in raw or b in combined_orig:
            return "bullish"
    for b in bearish_patterns:
        if b.upper() in raw or b in combined_orig:
            return "bearish"
    for n in neutral_patterns:
        if n.upper() in raw or n in combined_orig:
            return "neutral"
    if dir_str or dec_str:
        logger.debug("direction_bucket: unmatched dir=%r dec=%r", dir_str, dec_str)
        return "unknown"
    return "unknown"


def _calendar_gap_days(signal_trade_date: str | None, t1_trade_date: str | None) -> int | None:
    try:
        if not signal_trade_date or not t1_trade_date:
            return None
        d0 = datetime.strptime(signal_trade_date, "%Y-%m-%d")
        d1 = datetime.strptime(t1_trade_date, "%Y-%m-%d")
        return max(0, (d1 - d0).days)
    except Exception:
        return None


def _can_evaluate_t1(t1_trade_date: str) -> bool:
    """After T1 close we may compute P1."""
    today = cn_today_str()
    if today > t1_trade_date:
        return True
    if today < t1_trade_date:
        return False
    return cn_market_phase() in ("closed", "post_close")


def _resolve_t1_trade_date(signal_trade_date: str | None) -> str | None:
    """Resolve T+1 evaluation date (always next trading day close)."""
    sd = str(signal_trade_date or "").strip()
    if not sd:
        return None
    return next_cn_trading_day(sd)


def _recent_trade_day_floor(lookback_days: int | None) -> str | None:
    if lookback_days is None:
        return None
    n = max(1, int(lookback_days))
    d = cn_today_str()
    for _ in range(max(0, n - 1)):
        d = previous_cn_trading_day(d)
    return d


def _within_created_window(
    created_at: datetime | None,
    *,
    window_start: datetime | None,
    window_end: datetime | None,
) -> bool:
    if created_at is None:
        return False
    ts = created_at if created_at.tzinfo is not None else created_at.replace(tzinfo=timezone.utc)
    if window_start is not None:
        ws = window_start if window_start.tzinfo is not None else window_start.replace(tzinfo=timezone.utc)
        if ts < ws:
            return False
    if window_end is not None:
        we = window_end if window_end.tzinfo is not None else window_end.replace(tzinfo=timezone.utc)
        if ts >= we:
            return False
    return True


def _db_close_map(db: Session, symbol: str, start: str, end: str) -> dict[str, float]:
    sym = str(symbol or "").strip().upper()
    if not sym:
        return {}
    rows = (
        db.query(MarketDailyPriceDB.trade_date, MarketDailyPriceDB.close_price)
        .filter(MarketDailyPriceDB.market == "cn")
        .filter(MarketDailyPriceDB.symbol == sym)
        .filter(MarketDailyPriceDB.trade_date >= start)
        .filter(MarketDailyPriceDB.trade_date <= end)
        .filter(MarketDailyPriceDB.close_price.isnot(None))
        .all()
    )
    out: dict[str, float] = {}
    for trade_date, close_price in rows:
        try:
            out[str(trade_date)] = float(close_price)
        except (TypeError, ValueError):
            continue
    return out


def _fetch_ohlc_map(symbol: str, start: str, end: str) -> dict[str, tuple[float | None, float | None]]:
    try:
        csv_data = route_to_vendor("get_stock_data", symbol, start, end)
        if not csv_data:
            return {}
        # Provider CSVs (AkShare/BaoStock) prefix comment lines with "#".
        df = pd.read_csv(StringIO(csv_data), comment="#")
        open_cols = [c for c in df.columns if "open" in c.lower() or "开盘" in c]
        close_cols = [c for c in df.columns if "close" in c.lower() or "收盘" in c]
        date_cols = [c for c in df.columns if "date" in c.lower() or "日期" in c or "time" in c.lower()]
        if not date_cols or (not open_cols and not close_cols):
            return {}
        df = df.copy()
        df["_d"] = pd.to_datetime(df[date_cols[0]], errors="coerce").dt.strftime("%Y-%m-%d")
        out: dict[str, tuple[float | None, float | None]] = {}
        for _, row in df.iterrows():
            ds = row["_d"]
            if ds and str(ds) != "NaT":
                try:
                    open_v = float(row[open_cols[0]]) if open_cols else None
                except (TypeError, ValueError):
                    open_v = None
                try:
                    close_v = float(row[close_cols[0]]) if close_cols else None
                except (TypeError, ValueError):
                    close_v = None
                out[str(ds)] = (open_v, close_v)
        return out
    except Exception as e:
        logger.debug("ohlc map failed %s: %s", symbol, e)
        return {}


def _commit_ohlc_in_own_session(
    symbol: str,
    ohlc_map: dict[str, tuple[float | None, float | None]],
) -> None:
    """Write OHLC rows in a dedicated session and commit immediately.

    Using a separate session keeps the write-lock window tiny (milliseconds
    instead of the full T+1 refresh duration) so concurrent writers such as
    the watchlist endpoint are never blocked for long.
    """
    from api.database import SessionLocal  # local import avoids circular import

    db = SessionLocal()
    try:
        _upsert_ohlc_rows(db, symbol, ohlc_map)
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning("OHLC upsert failed for %s: %s", symbol, exc)
    finally:
        db.close()


def _upsert_ohlc_rows(
    db: Session,
    symbol: str,
    ohlc_map: dict[str, tuple[float | None, float | None]],
) -> None:
    sym = str(symbol or "").strip().upper()
    if not sym or not ohlc_map:
        return
    dates = sorted(ohlc_map.keys())
    existing_rows = (
        db.query(MarketDailyPriceDB)
        .filter(MarketDailyPriceDB.market == "cn")
        .filter(MarketDailyPriceDB.symbol == sym)
        .filter(MarketDailyPriceDB.trade_date >= dates[0])
        .filter(MarketDailyPriceDB.trade_date <= dates[-1])
        .all()
    )
    existing_by_date = {str(r.trade_date): r for r in existing_rows}
    # SessionLocal uses autoflush=False, so rows added earlier in the same refresh
    # are not visible to queries until commit/flush. Include pending rows explicitly
    # to keep upsert idempotent within one transaction.
    pending_rows = [
        r
        for r in db.new
        if isinstance(r, MarketDailyPriceDB) and r.market == "cn" and str(r.symbol or "").upper() == sym
    ]
    for r in pending_rows:
        td = str(getattr(r, "trade_date", "") or "")
        if td:
            existing_by_date[td] = r
    now = datetime.now(timezone.utc)
    for trade_date, (open_price, close_price) in ohlc_map.items():
        row = existing_by_date.get(str(trade_date))
        if row:
            if open_price is not None:
                row.open_price = open_price
            if close_price is not None:
                row.close_price = close_price
            row.updated_at = now
            continue
        new_row = MarketDailyPriceDB(
            id=uuid4().hex,
            market="cn",
            symbol=sym,
            trade_date=str(trade_date),
            open_price=open_price,
            close_price=close_price,
            source="vendor",
            created_at=now,
            updated_at=now,
        )
        db.add(new_row)
        existing_by_date[str(trade_date)] = new_row


def _fetch_close_map(symbol: str, start: str, end: str) -> dict[str, float]:
    """Backward-compatible helper for tests: returns close-only date map."""
    out: dict[str, float] = {}
    for trade_date, (_, close_price) in _fetch_ohlc_map(symbol, start, end).items():
        if close_price is not None:
            out[trade_date] = close_price
    return out


def _t0_t1_closes(symbol: str, t0: str, t1: str) -> tuple[float | None, float | None]:
    from datetime import timedelta

    fs = (datetime.strptime(t0, "%Y-%m-%d") - timedelta(days=5)).strftime("%Y-%m-%d")
    fe = (datetime.strptime(t1, "%Y-%m-%d") + timedelta(days=5)).strftime("%Y-%m-%d")
    m = _fetch_close_map(symbol, fs, fe)
    p0 = m.get(t0)
    p1 = m.get(t1)
    return (p0, p1)


def _t0_t1_closes_cached(
    cache: CloseCache | T1VendorSeriesCache | None,
    symbol: str,
    t0: str,
    t1: str,
) -> ClosePair:
    """Memoize close fetches within one refresh cycle to reduce repeated vendor calls."""
    if isinstance(cache, T1VendorSeriesCache):
        return cache.lookup_closes(symbol, t0, t1)
    if cache is None:
        return _t0_t1_closes(symbol, t0, t1)
    key = (str(symbol).upper(), t0, t1)
    if key in cache:
        return cache[key]
    pair = _t0_t1_closes(symbol, t0, t1)
    cache[key] = pair
    return pair


def evaluate_market_scan_t1(
    db: Session,
    *,
    user_id: str,
    limit: int = 400,
    min_signal_trade_date: str | None = None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    close_cache: CloseCache | T1VendorSeriesCache | None = None,
) -> dict[str, Any]:
    close_cache_inst: CloseCache | T1VendorSeriesCache | None = close_cache
    if close_cache_inst is None:
        close_cache_inst = T1VendorSeriesCache(db)
    rows = (
        db.query(MarketScanResultDB)
        .filter(MarketScanResultDB.user_id == user_id)
        .filter(
            or_(
                MarketScanResultDB.t1_status.is_(None),
                MarketScanResultDB.t1_status.in_(["pending", "insufficient_data"]),
                and_(
                    MarketScanResultDB.t1_status == "evaluated",
                    MarketScanResultDB.t1_signal_date.isnot(None),
                    MarketScanResultDB.t1_trade_date.isnot(None),
                    MarketScanResultDB.t1_signal_date == MarketScanResultDB.t1_trade_date,
                ),
            )
        )
        # Newest first: users care about recent scans; old insufficient rows no longer block T+1 eval.
        .order_by(MarketScanResultDB.created_at.desc())
        .limit(max(1, min(limit, 800)))
        .all()
    )
    if min_signal_trade_date:
        floor = str(min_signal_trade_date)
        rows = [
            r
            for r in rows
            if str(_signal_trade_date_from_created(r.created_at)) >= floor
        ]
    if window_start is not None or window_end is not None:
        rows = [
            r
            for r in rows
            if _within_created_window(
                getattr(r, "created_at", None),
                window_start=window_start,
                window_end=window_end,
            )
        ]
    if isinstance(close_cache_inst, T1VendorSeriesCache):
        prefetch: list[tuple[str, str, str]] = []
        for row in rows:
            p0p, p1p = _report_price_window(None, row.created_at)
            if p1p and p0p and _can_evaluate_price_window(p0p, p1p):
                prefetch.append((row.symbol, p0p, p1p))
        close_cache_inst.prefetch_pairs(prefetch)

    evaluated = 0
    skipped = 0
    insufficient = 0
    touched_signal_dates: set[str] = set()
    now = datetime.now(timezone.utc)
    for row in rows:
        p0_date, p1_date = _report_price_window(None, row.created_at)
        row.t1_signal_date = p1_date
        touched_signal_dates.add(str(p1_date or ""))
        if not p1_date:
            row.t1_status = "insufficient_data"
            row.t1_trade_date = None
            insufficient += 1
            continue
        row.t1_trade_date = p1_date
        if not _can_evaluate_price_window(p0_date, p1_date):
            row.t1_status = "pending"
            # Clear any stale return data from a previous drift evaluation.
            row.t1_return_pct = None
            row.updated_at = now
            skipped += 1
            continue
        p0, p1 = _t0_t1_closes_cached(close_cache_inst, row.symbol, p0_date, p1_date)
        if p0 and p1 and p0 > 0:
            row.t1_return_pct = round((p1 - p0) / p0 * 100.0, 4)
            row.t1_status = "evaluated"
            evaluated += 1
        else:
            row.t1_status = "insufficient_data"
            insufficient += 1
        row.updated_at = now
    return {
        "processed": len(rows),
        "evaluated": evaluated,
        "skipped": skipped,
        "insufficient": insufficient,
        "touched_signal_dates": sorted(touched_signal_dates),
    }


def _portfolio_symbols_set(db: Session, user_id: str) -> set[str]:
    symbols = {
        str(r.symbol or "").strip().upper()
        for r in db.query(ImportedPortfolioPositionDB.symbol)
        .filter(ImportedPortfolioPositionDB.user_id == user_id)
        .all()
    }
    symbols.discard("")
    return symbols


def _to_float_or_none(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _has_position_snapshot(result_data: Any) -> bool:
    """Whether a report captured holding context at generation time."""
    if not isinstance(result_data, dict):
        return False
    user_ctx = result_data.get("user_context")
    if not isinstance(user_ctx, dict):
        return False
    current_position = _to_float_or_none(user_ctx.get("current_position"))
    current_position_pct = _to_float_or_none(user_ctx.get("current_position_pct"))
    return bool((current_position is not None and current_position > 0) or (current_position_pct is not None and current_position_pct > 0))


def _portfolio_snapshot_report_ids(
    db: Session,
    *,
    user_id: str,
    report_ids: set[str] | None = None,
) -> set[str]:
    """Report ids whose saved payload indicates user had a position at report time."""
    q = (
        db.query(ReportDB.id, ReportDB.result_data)
        .filter(ReportDB.user_id == user_id)
        .filter(ReportDB.status == "completed")
    )
    if report_ids is not None:
        if not report_ids:
            return set()
        q = q.filter(ReportDB.id.in_(list(report_ids)))
    out: set[str] = set()
    for report_id, result_data in q.all():
        if _has_position_snapshot(result_data):
            out.add(str(report_id))
    return out


def _plan_horizon_days_by_report(
    db: Session,
    report_ids: set[str] | None = None,
) -> dict[str, int | None]:
    """Map report id -> its trade plan's holding period in natural days (F1).

    Returns only reports that actually carry a plan. A missing entry means "no plan",
    which is different from "a plan whose horizon is unknown" — the caller stores
    ``None`` either way, but the summary counts the two separately via the plan's own
    ``horizon`` vocabulary.
    """
    q = db.query(TradePlanDB.report_id, TradePlanDB.horizon_days, TradePlanDB.horizon)
    if report_ids is not None:
        if not report_ids:
            return {}
        q = q.filter(TradePlanDB.report_id.in_(list(report_ids)))

    out: dict[str, int | None] = {}
    for report_id, horizon_days, horizon in q.all():
        rid = str(report_id or "")
        if not rid:
            continue
        days: int | None = None
        if horizon_days is not None:
            try:
                days = int(horizon_days)
            except (TypeError, ValueError):
                days = None
        if days is None:
            # 计划只记了周期词（short|medium|dual）时退化为该周期的典型长度，
            # 以便 F1 的错配统计仍有可用的量纲；dual 取中线的长度（更保守）。
            days = {"short": 5, "medium": 20, "dual": 20}.get(str(horizon or "").strip().lower())
        out[rid] = days
    return out


def _fill_report_t1_outcome(
    pr: ReportT1OutcomeDB,
    *,
    symbol: str,
    trade_date: str | None,
    created_at: datetime | None,
    direction: str | None,
    decision: str | None,
    close_cache_inst: CloseCache | T1VendorSeriesCache,
    now: datetime,
    direction_bucket: str | None = None,
    plan_horizon_days: int | None = None,
) -> str:
    """Apply P0→P1 window, fetch closes, set label. Returns outcome status."""
    p0_date, p1_date = _report_price_window(trade_date, created_at)
    bucket = direction_bucket or _direction_bucket(direction, decision)
    pr.signal_trade_date = p1_date or p0_date
    pr.t1_trade_date = p1_date
    pr.direction_bucket = bucket
    # A5/D5：本窗口衡量的周期。价格窗口固定为一个交易日（收盘→收盘），故恒为 "t1"。
    pr.horizon = "t1"
    # F1：被评报告所附计划自己的持有期。目标价/止损/时间止损是约 20 天期的论点，
    # 却由一日收益打分；记下计划周期，才能统计有多少 T+1 窗口其实承载的是中期方案。
    pr.plan_horizon_days = plan_horizon_days
    sym = str(symbol or pr.symbol or "").strip().upper()
    if sym:
        pr.symbol = sym

    if not p1_date:
        pr.status = "insufficient_data"
        pr.reason = "no_next_trading_day"
        pr.label_correct = None
        pr.p0 = None
        pr.p1 = None
        pr.return_t1_pct = None
        pr.evaluated_at = now
        pr.updated_at = now
        return "insufficient_data"

    if not _can_evaluate_price_window(p0_date, p1_date):
        pr.status = "pending"
        pr.reason = None
        pr.label_correct = None
        pr.p0 = None
        pr.p1 = None
        pr.return_t1_pct = None
        pr.evaluated_at = None
        pr.updated_at = now
        return "pending"

    p0, p1 = _t0_t1_closes_cached(close_cache_inst, sym or pr.symbol, p0_date, p1_date)
    label: bool | None = None
    if bucket == "neutral":
        label = None
    elif p0 and p1 and p0 > 0:
        ret = (p1 - p0) / p0 * 100.0
        if bucket == "bullish":
            label = ret > 0
        elif bucket == "bearish":
            label = ret < 0
    if p0 and p1 and p0 > 0:
        pr.p0 = p0
        pr.p1 = p1
        pr.return_t1_pct = round((p1 - p0) / p0 * 100.0, 4)
        pr.label_correct = label
        pr.status = "evaluated"
        pr.reason = None
        pr.evaluated_at = now
        pr.updated_at = now
        return "evaluated"

    pr.p0 = p0
    pr.p1 = p1
    pr.return_t1_pct = None
    pr.label_correct = None
    pr.status = "insufficient_data"
    pr.reason = "missing_ohlc"
    pr.evaluated_at = now
    pr.updated_at = now
    return "insufficient_data"


def reconcile_report_t1_outcomes(
    db: Session,
    *,
    user_id: str,
    lookback_days: int = 7,
    close_cache: CloseCache | T1VendorSeriesCache | None = None,
) -> dict[str, Any]:
    """Re-apply P0→P1 window and prices for recent report outcomes (fixes legacy signal bucketing)."""
    close_cache_inst: CloseCache | T1VendorSeriesCache = close_cache or T1VendorSeriesCache(db)
    floor = (
        datetime.now(CN_TZ) - timedelta(days=max(1, int(lookback_days)))
    ).astimezone(timezone.utc).replace(tzinfo=None)

    pairs = (
        db.query(ReportT1OutcomeDB, ReportDB)
        .join(ReportDB, ReportDB.id == ReportT1OutcomeDB.report_id)
        .filter(ReportT1OutcomeDB.user_id == user_id)
        .filter(ReportDB.created_at >= floor)
        .all()
    )
    prefetch: list[tuple[str, str, str]] = []
    for pr, rep in pairs:
        p0d, p1d = _report_price_window(rep.trade_date, rep.created_at)
        if p0d and p1d:
            prefetch.append((str(rep.symbol or pr.symbol or ""), p0d, p1d))
    if prefetch and isinstance(close_cache_inst, T1VendorSeriesCache):
        close_cache_inst.prefetch_pairs(prefetch)

    now = datetime.now(timezone.utc)
    updated = evaluated = pending = insufficient = 0
    touched_signal_dates: set[str] = set()
    plan_days = _plan_horizon_days_by_report(db, {str(pr.report_id) for pr, _rep in pairs})
    for pr, rep in pairs:
        status = _fill_report_t1_outcome(
            pr,
            symbol=str(rep.symbol or ""),
            trade_date=rep.trade_date,
            created_at=rep.created_at,
            direction=rep.direction,
            decision=rep.decision,
            close_cache_inst=close_cache_inst,
            now=now,
            plan_horizon_days=plan_days.get(str(pr.report_id)),
        )
        touched_signal_dates.add(str(pr.signal_trade_date or ""))
        updated += 1
        if status == "evaluated":
            evaluated += 1
        elif status == "pending":
            pending += 1
        else:
            insufficient += 1

    return {
        "lookback_days": int(lookback_days),
        "processed": len(pairs),
        "updated": updated,
        "evaluated": evaluated,
        "pending": pending,
        "insufficient": insufficient,
        "touched_signal_dates": sorted(touched_signal_dates),
    }


def evaluate_report_t1_completed(
    db: Session,
    *,
    user_id: str,
    limit: int = 400,
    min_signal_trade_date: str | None = None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    close_cache: CloseCache | T1VendorSeriesCache | None = None,
) -> dict[str, Any]:
    """Evaluate T+1 outcomes for all completed depth reports (no portfolio filter)."""
    close_cache_inst: CloseCache | T1VendorSeriesCache | None = close_cache
    if close_cache_inst is None:
        close_cache_inst = T1VendorSeriesCache(db)
    existing = {
        r.report_id
        for r in db.query(ReportT1OutcomeDB.report_id).filter(ReportT1OutcomeDB.user_id == user_id).all()
    }
    reports = (
        db.query(ReportDB)
        .filter(ReportDB.user_id == user_id)
        .filter(ReportDB.status == "completed")
        .order_by(ReportDB.trade_date.desc(), ReportDB.created_at.desc())
        .limit(max(1, min(limit, 800)))
        .all()
    )
    if min_signal_trade_date:
        floor = str(min_signal_trade_date)
        reports = [rep for rep in reports if _report_signal_trade_date(rep.trade_date, rep.created_at) >= floor]
    if window_start is not None or window_end is not None:
        reports = [
            rep
            for rep in reports
            if _within_created_window(
                getattr(rep, "created_at", None),
                window_start=window_start,
                window_end=window_end,
            )
        ]

    retry_rows = (
        db.query(ReportT1OutcomeDB)
        .filter(ReportT1OutcomeDB.user_id == user_id)
        .filter(
            or_(
                ReportT1OutcomeDB.status == "pending",
                and_(ReportT1OutcomeDB.status == "insufficient_data", ReportT1OutcomeDB.reason == "missing_ohlc"),
                # Price-drift retry: p0 == p1 means we fetched the same day's price for both
                # sides. Re-evaluate after T+1 close has data.
                and_(
                    ReportT1OutcomeDB.status == "evaluated",
                    ReportT1OutcomeDB.p0.isnot(None),
                    ReportT1OutcomeDB.p1.isnot(None),
                    ReportT1OutcomeDB.return_t1_pct == 0.0,
                    ReportT1OutcomeDB.p0 == ReportT1OutcomeDB.p1,
                ),
            )
        )
        .all()
    )
    if min_signal_trade_date:
        floor = str(min_signal_trade_date)
        retry_rows = [pr for pr in retry_rows if str(pr.signal_trade_date or "") >= floor]
    if window_start is not None or window_end is not None:
        retry_report_ids_w = [str(pr.report_id) for pr in retry_rows if getattr(pr, "report_id", None)]
        retry_reports_w = (
            db.query(ReportDB.id, ReportDB.created_at)
            .filter(ReportDB.user_id == user_id)
            .filter(ReportDB.id.in_(retry_report_ids_w))
            .all()
            if retry_report_ids_w
            else []
        )
        retry_created_map = {str(rid): created_at for rid, created_at in retry_reports_w}
        retry_rows = [
            pr
            for pr in retry_rows
            if _within_created_window(
                retry_created_map.get(str(getattr(pr, "report_id", "")).strip()),
                window_start=window_start,
                window_end=window_end,
            )
        ]

    if isinstance(close_cache_inst, T1VendorSeriesCache):
        retry_report_ids = [str(pr.report_id) for pr in retry_rows if getattr(pr, "report_id", None)]
        retry_reports = (
            db.query(ReportDB.id, ReportDB.trade_date, ReportDB.created_at)
            .filter(ReportDB.user_id == user_id)
            .filter(ReportDB.id.in_(retry_report_ids))
            .all()
            if retry_report_ids
            else []
        )
        retry_report_map = {str(rid): (trade_date, created_at) for rid, trade_date, created_at in retry_reports}
        prefetch_r: list[tuple[str, str, str]] = []
        for rep in reports:
            if rep.id in existing:
                continue
            p0p, p1p = _report_price_window(rep.trade_date, rep.created_at)
            if p1p and p0p and _can_evaluate_price_window(p0p, p1p):
                prefetch_r.append((rep.symbol, p0p, p1p))
        for pr in retry_rows:
            rep_meta = retry_report_map.get(str(getattr(pr, "report_id", "")).strip())
            if rep_meta:
                p0p, p1p = _report_price_window(rep_meta[0], rep_meta[1])
            else:
                p0p, p1p = _report_price_window(None, None)
            if p1p and p0p and _can_evaluate_price_window(p0p, p1p):
                prefetch_r.append((pr.symbol, p0p, p1p))
        close_cache_inst.prefetch_pairs(prefetch_r)

    evaluated = skipped = insufficient = 0
    touched_signal_dates: set[str] = set()
    now = datetime.now(timezone.utc)
    plan_days = _plan_horizon_days_by_report(db, {str(rep.id) for rep in reports} | {str(getattr(pr, "report_id", "")) for pr in retry_rows})
    for rep in reports:
        if rep.id in existing:
            continue
        signal_day = _report_signal_trade_date(rep.trade_date, rep.created_at)
        if not signal_day:
            continue
        touched_signal_dates.add(str(signal_day))
        row = ReportT1OutcomeDB(
            id=uuid4().hex,
            user_id=user_id,
            report_id=rep.id,
            symbol=str(rep.symbol).upper(),
            created_at=now,
        )
        status = _fill_report_t1_outcome(
            row,
            symbol=str(rep.symbol or ""),
            trade_date=rep.trade_date,
            created_at=rep.created_at,
            direction=rep.direction,
            decision=rep.decision,
            close_cache_inst=close_cache_inst,
            now=now,
            plan_horizon_days=plan_days.get(str(rep.id)),
        )
        db.add(row)
        existing.add(rep.id)
        if status == "evaluated":
            evaluated += 1
        elif status == "pending":
            skipped += 1
        else:
            insufficient += 1

    retry_report_ids = [str(pr.report_id) for pr in retry_rows if getattr(pr, "report_id", None)]
    retry_reports = (
        db.query(ReportDB.id, ReportDB.trade_date, ReportDB.created_at, ReportDB.symbol, ReportDB.direction, ReportDB.decision)
        .filter(ReportDB.user_id == user_id)
        .filter(ReportDB.id.in_(retry_report_ids))
        .all()
        if retry_report_ids
        else []
    )
    retry_report_map = {
        str(rid): (trade_date, created_at, symbol, direction, decision)
        for rid, trade_date, created_at, symbol, direction, decision in retry_reports
    }
    for pr in retry_rows:
        meta = retry_report_map.get(str(getattr(pr, "report_id", "")).strip())
        if meta:
            trade_date, created_at, symbol, direction, decision = meta
        else:
            trade_date = created_at = symbol = direction = decision = None
        signal_day = _report_signal_trade_date(trade_date, created_at)
        if min_signal_trade_date and signal_day and signal_day < str(min_signal_trade_date):
            continue
        touched_signal_dates.add(str(signal_day or pr.signal_trade_date or ""))
        status = _fill_report_t1_outcome(
            pr,
            symbol=str(symbol or pr.symbol or ""),
            trade_date=trade_date,
            created_at=created_at,
            direction=direction,
            decision=decision,
            close_cache_inst=close_cache_inst,
            now=now,
            plan_horizon_days=plan_days.get(str(getattr(pr, "report_id", "")).strip()),
        )
        if status == "evaluated":
            evaluated += 1
        elif status == "pending":
            skipped += 1
        else:
            insufficient += 1

    return {
        "processed": len(reports) + len(retry_rows),
        "evaluated": evaluated,
        "skipped": skipped,
        "insufficient": insufficient,
        "touched_signal_dates": sorted(touched_signal_dates),
    }


def _upsert_t1_daily_stat(
    db: Session,
    *,
    user_id: str,
    metric: str,
    scope: str,
    signal_trade_date: str,
    t1_trade_date: str | None,
    sample_count: int,
    avg_return_pct: float | None = None,
    win_rate_pct: float | None = None,
    accuracy_pct: float | None = None,
    avg_calendar_gap_days: float | None = None,
    poor_tradability_rate_pct: float | None = None,
    high_impact_risk_rate_pct: float | None = None,
    effective_n: int | None = None,
    unique_symbols: int | None = None,
    top_symbol_share: float | None = None,
    ci_low: float | None = None,
    ci_high: float | None = None,
    abstain_count: int | None = None,
    conflict_count: int | None = None,
) -> None:
    row = (
        db.query(T1DailyStatDB)
        .filter(T1DailyStatDB.user_id == user_id)
        .filter(T1DailyStatDB.metric == metric)
        .filter(T1DailyStatDB.scope == scope)
        .filter(T1DailyStatDB.signal_trade_date == signal_trade_date)
        .one_or_none()
    )
    now = datetime.now(timezone.utc)
    if row is None:
        row = T1DailyStatDB(
            id=uuid4().hex,
            user_id=user_id,
            metric=metric,
            scope=scope,
            signal_trade_date=signal_trade_date,
            t1_trade_date=t1_trade_date,
            sample_count=sample_count,
            avg_return_pct=avg_return_pct,
            win_rate_pct=win_rate_pct,
            accuracy_pct=accuracy_pct,
            avg_calendar_gap_days=avg_calendar_gap_days,
            poor_tradability_rate_pct=poor_tradability_rate_pct,
            high_impact_risk_rate_pct=high_impact_risk_rate_pct,
            effective_n=effective_n,
            unique_symbols=unique_symbols,
            top_symbol_share=top_symbol_share,
            ci_low=ci_low,
            ci_high=ci_high,
            abstain_count=abstain_count,
            conflict_count=conflict_count,
            updated_at=now,
        )
        db.add(row)
        return
    row.t1_trade_date = t1_trade_date
    row.sample_count = sample_count
    row.avg_return_pct = avg_return_pct
    row.win_rate_pct = win_rate_pct
    row.accuracy_pct = accuracy_pct
    row.avg_calendar_gap_days = avg_calendar_gap_days
    row.poor_tradability_rate_pct = poor_tradability_rate_pct
    row.high_impact_risk_rate_pct = high_impact_risk_rate_pct
    row.effective_n = effective_n
    row.unique_symbols = unique_symbols
    row.top_symbol_share = top_symbol_share
    row.ci_low = ci_low
    row.ci_high = ci_high
    row.abstain_count = abstain_count
    row.conflict_count = conflict_count
    row.updated_at = now


def _refresh_recommendation_daily_stats(
    db: Session,
    *,
    user_id: str,
    signal_dates: set[str] | None = None,
) -> None:
    q = (
        db.query(MarketScanResultDB)
        .filter(MarketScanResultDB.user_id == user_id)
        .filter(MarketScanResultDB.t1_status == "evaluated")
        .filter(MarketScanResultDB.t1_signal_date.isnot(None))
        .filter(MarketScanResultDB.t1_trade_date.isnot(None))
        .filter(MarketScanResultDB.t1_return_pct.isnot(None))
    )
    if signal_dates:
        q = q.filter(MarketScanResultDB.t1_signal_date.in_(list(signal_dates)))
    rows = q.all()
    by_day: dict[str, list[MarketScanResultDB]] = defaultdict(list)
    for row in rows:
        by_day[str(row.t1_signal_date)].append(row)
    if signal_dates:
        db.query(T1DailyStatDB).filter(T1DailyStatDB.user_id == user_id).filter(
            T1DailyStatDB.metric == "recommendation_t1"
        ).filter(T1DailyStatDB.scope == "all").filter(T1DailyStatDB.signal_trade_date.in_(list(signal_dates))).delete(
            synchronize_session=False
        )
    for day, day_rows in by_day.items():
        returns = [float(r.t1_return_pct or 0.0) for r in day_rows]
        wins = sum(1 for r in day_rows if float(r.t1_return_pct or 0.0) > 0)
        gaps = [
            _calendar_gap_days(str(r.t1_signal_date), str(r.t1_trade_date))
            for r in day_rows
        ]
        gaps = [g for g in gaps if g is not None]
        risk_rows = [list(r.risk_flags_json or []) for r in day_rows]
        poor_tradability = sum(1 for flags in risk_rows if _has_poor_tradability_risk(flags))
        high_impact = sum(1 for flags in risk_rows if _has_high_impact_risk(flags))
        t1d = str(day_rows[0].t1_trade_date or "") if day_rows else None
        _upsert_t1_daily_stat(
            db,
            user_id=user_id,
            metric="recommendation_t1",
            scope="all",
            signal_trade_date=day,
            t1_trade_date=t1d or None,
            sample_count=len(returns),
            avg_return_pct=round(sum(returns) / len(returns), 4) if returns else None,
            win_rate_pct=round(wins / len(returns) * 100.0, 2) if returns else None,
            avg_calendar_gap_days=round(sum(gaps) / len(gaps), 2) if gaps else None,
            poor_tradability_rate_pct=round(poor_tradability / max(1, len(risk_rows)) * 100.0, 2) if risk_rows else None,
            high_impact_risk_rate_pct=round(high_impact / max(1, len(risk_rows)) * 100.0, 2) if risk_rows else None,
        )


def _refresh_report_daily_stats(
    db: Session,
    *,
    user_id: str,
    signal_dates: set[str] | None = None,
) -> None:
    q = (
        db.query(ReportT1OutcomeDB)
        .filter(ReportT1OutcomeDB.user_id == user_id)
        .filter(ReportT1OutcomeDB.status == "evaluated")
        # 注意：这里**不能**再 filter(label_correct.isnot(None))。
        #
        # 弃权行（看多/看空都没给）的 label_correct 是 NULL，旧查询把它们直接滤掉，
        # 于是"今天有多少判断被放弃了"无从知晓。加上 F3 允许弃权之后，这个过滤会把
        # 弃权变成一条免费提分通道：难样本被弃权、幸存集合的命中率上升，而覆盖率
        # 下滑没有任何信号。现在把全部已评估行都取出来，可判分的部分在下游筛。
        .filter(ReportT1OutcomeDB.signal_trade_date.isnot(None))
    )
    if signal_dates:
        q = q.filter(ReportT1OutcomeDB.signal_trade_date.in_(list(signal_dates)))
    rows = q.all()
    report_ids = {
        str(getattr(r, "report_id", "")).strip()
        for r in rows
        if getattr(r, "report_id", None)
    }
    report_meta: dict[str, tuple[str | None, datetime | None]] = {}
    if report_ids:
        for rid, trade_date, created_at in (
            db.query(ReportDB.id, ReportDB.trade_date, ReportDB.created_at)
            .filter(ReportDB.user_id == user_id)
            .filter(ReportDB.id.in_(list(report_ids)))
            .all()
        ):
            report_meta[str(rid)] = (trade_date, created_at)

    # D2：可判分集合。`neutral`/`unknown`/无方向的行不进分子分母——但也**不能就这样
    # 消失**，否则"允许弃权"（F3）会变成一个免费的分数提升：模型对越难的样本越倾向
    # 弃权，命中率于是自动上升，而覆盖率下滑没有任何信号。所以下面把弃权数单独记下来。
    all_rows: list[ReportT1OutcomeDB] = [
        r
        for r in rows
        if r.direction_bucket not in (None, "neutral", "unknown") and r.label_correct is not None
    ]
    portfolio_report_ids = _portfolio_snapshot_report_ids(
        db,
        user_id=user_id,
        report_ids={str(getattr(r, "report_id", "")).strip() for r in all_rows if getattr(r, "report_id", None)},
    )
    portfolio_rows = [r for r in all_rows if str(getattr(r, "report_id", "")).strip() in portfolio_report_ids]
    # 弃权（含无方向）按 report_id 归属到组合/全量两个口径。
    all_ids = {str(getattr(r, "report_id", "")).strip() for r in rows if getattr(r, "report_id", None)}
    all_report_ids = {str(getattr(r, "report_id", "")).strip() for r in all_rows if getattr(r, "report_id", None)}
    abstain_report_ids_all = all_ids - all_report_ids
    abstain_report_ids_portfolio = abstain_report_ids_all & portfolio_report_ids

    if signal_dates:
        db.query(T1DailyStatDB).filter(T1DailyStatDB.user_id == user_id).filter(
            T1DailyStatDB.metric == "report_accuracy_t1"
        ).filter(T1DailyStatDB.signal_trade_date.in_(list(signal_dates))).delete(synchronize_session=False)

    def _as_window_row(row: ReportT1OutcomeDB) -> dict[str, Any]:
        return {
            "symbol": str(getattr(row, "symbol", "") or "").strip().upper(),
            "signal_date": str(row.signal_trade_date or ""),
            # 报告 T+1 的价格窗口恒为"一个交易日 close→close"，所以 window_days 固定为 1。
            "window_days": 1,
            "direction_bucket": str(row.direction_bucket or "").strip().upper(),
            "label_correct": bool(row.label_correct) if row.label_correct is not None else None,
            "return_t1_pct": row.return_t1_pct,
        }

    def _emit(scope: str, dataset: list[ReportT1OutcomeDB], abstain_ids: set[str]) -> None:
        by_day: dict[str, list[ReportT1OutcomeDB]] = defaultdict(list)
        for row in dataset:
            by_day[str(row.signal_trade_date)].append(row)
        # 弃权行还没进 by_day（它们不可判分），按 signal_trade_date 单独归日。
        abstain_by_day: dict[str, int] = defaultdict(int)
        for row in rows:
            rid = str(getattr(row, "report_id", "") or "").strip()
            if rid and rid in abstain_ids:
                abstain_by_day[str(row.signal_trade_date)] += 1

        for day in sorted(set(by_day) | set(abstain_by_day)):
            day_rows = by_day.get(day, [])

            def _gap_for(row: ReportT1OutcomeDB) -> int | None:
                meta = report_meta.get(str(getattr(row, "report_id", "")).strip())
                if meta:
                    p0d, p1d = _report_price_window(meta[0], meta[1])
                    return _calendar_gap_days(p0d, p1d)
                return _calendar_gap_days(str(row.signal_trade_date), str(row.t1_trade_date))

            gaps = [g for g in (_gap_for(r) for r in day_rows) if g is not None]
            t1d = str(day_rows[0].t1_trade_date or "") if day_rows else None

            # D1：先按价格窗口去重再算命中率。
            #
            # 同一 (标的, 信号日) 上的多份研报共享同一次前瞻收益；按行数算等于把一次
            # 观测重复计入。实测 4601 行 → 1583 个唯一窗口（单键最多 100 行，两只标的
            # 占 31.5%），虚增约 3 倍。
            windows = t1_stats.dedupe_by_window([_as_window_row(r) for r in day_rows])
            scored = [w for w in windows if not w.get("conflict") and w.get("direction_bucket") in ("BULLISH", "BEARISH")]
            hits = sum(1 for w in scored if w.get("label_correct"))
            ci_low, ci_high = t1_stats.wilson_interval(hits, len(scored))
            conc = t1_stats.concentration(scored)

            _upsert_t1_daily_stat(
                db,
                user_id=user_id,
                metric="report_accuracy_t1",
                scope=scope,
                signal_trade_date=day,
                t1_trade_date=t1d or None,
                # sample_count 保持"原始行数"语义，供既有读取方与覆盖率对比使用。
                sample_count=len(day_rows),
                accuracy_pct=round(hits / len(scored) * 100.0, 2) if scored else None,
                avg_calendar_gap_days=round(sum(gaps) / len(gaps), 2) if gaps else None,
                effective_n=len(scored),
                unique_symbols=conc.get("unique_symbols"),
                top_symbol_share=conc.get("top_symbol_share"),
                ci_low=round(ci_low, 6) if ci_low is not None else None,
                ci_high=round(ci_high, 6) if ci_high is not None else None,
                abstain_count=abstain_by_day.get(day, 0),
                conflict_count=sum(1 for w in windows if w.get("conflict")),
            )

    _emit("all", all_rows, abstain_report_ids_all)
    _emit("portfolio", portfolio_rows, abstain_report_ids_portfolio)


def refresh_t1_daily_stats(
    db: Session,
    *,
    user_id: str,
    signal_dates: set[str] | None = None,
) -> None:
    _refresh_recommendation_daily_stats(db, user_id=user_id, signal_dates=signal_dates)
    _refresh_report_daily_stats(db, user_id=user_id, signal_dates=signal_dates)


def _recommendation_t1_trend_from_raw(
    db: Session,
    *,
    user_id: str,
    days: int,
    start_date: str | None,
    end_date: str | None,
) -> list[dict[str, Any]]:
    rows = (
        db.query(MarketScanResultDB)
        .filter(MarketScanResultDB.user_id == user_id)
        .filter(MarketScanResultDB.t1_status == "evaluated")
        .filter(MarketScanResultDB.t1_signal_date.isnot(None))
        .filter(MarketScanResultDB.t1_return_pct.isnot(None))
        .all()
    )
    by_day: dict[str, list[MarketScanResultDB]] = defaultdict(list)
    for row in rows:
        sd = row.t1_signal_date
        if not sd:
            continue
        by_day[str(sd)].append(row)
    if start_date and end_date:
        all_days = sorted(d for d in by_day if start_date <= d <= end_date)
    else:
        all_days = sorted(by_day.keys(), reverse=True)[: max(1, min(days, 400))]
        all_days.sort()
    out: list[dict[str, Any]] = []
    for d in all_days:
        day_rows = by_day[d]
        vals = [float(r.t1_return_pct or 0.0) for r in day_rows]
        wins = sum(1 for v in vals if v > 0)
        risk_rows = [list(r.risk_flags_json or []) for r in day_rows]
        poor_tradability = sum(1 for flags in risk_rows if _has_poor_tradability_risk(flags))
        high_impact = sum(1 for flags in risk_rows if _has_high_impact_risk(flags))
        gaps = [_calendar_gap_days(str(r.t1_signal_date), str(r.t1_trade_date)) for r in day_rows]
        gaps = [g for g in gaps if g is not None]
        out.append(
            {
                "date": d,
                "t1_trade_date": str(day_rows[0].t1_trade_date or "") if day_rows else None,
                "sample_count": len(vals),
                "avg_return_pct": round(sum(vals) / len(vals), 4) if vals else None,
                "win_rate_pct": round(wins / len(vals) * 100.0, 2) if vals else None,
                "avg_calendar_gap_days": round(sum(gaps) / len(gaps), 2) if gaps else None,
                "poor_tradability_rate_pct": round(poor_tradability / max(1, len(risk_rows)) * 100.0, 2) if risk_rows else None,
                "high_impact_risk_rate_pct": round(high_impact / max(1, len(risk_rows)) * 100.0, 2) if risk_rows else None,
            }
        )
    return out


def _report_accuracy_t1_trend_from_raw(
    db: Session,
    *,
    user_id: str,
    days: int,
    start_date: str | None,
    end_date: str | None,
    scope: str,
) -> list[dict[str, Any]]:
    rows = (
        db.query(ReportT1OutcomeDB)
        .filter(ReportT1OutcomeDB.user_id == user_id)
        .filter(ReportT1OutcomeDB.status == "evaluated")
        .filter(ReportT1OutcomeDB.label_correct.isnot(None))
        .all()
    )
    # D2：弃权（中性/无方向）数按日单独统计。
    #
    # 上面那条 `label_correct.isnot(None)` 把弃权行排除在外——它们不可判分，进分子
    # 分母都是错的。但**弃权数本身必须报出来**，否则"允许弃权"（F3）会变成免费的分数
    # 提升：模型越难越弃权，命中率自动上升，而覆盖率下滑没有任何信号。聚合写入端
    # 已经在做这件事（`abstain_by_day`），回退路径此前返回 None，于是同一张图会随
    # "数据是否已刷新"而少一列——正是本轮要修的那种遮蔽。
    abstain_by_day: dict[str, int] = defaultdict(int)
    for r in (
        db.query(ReportT1OutcomeDB)
        .filter(ReportT1OutcomeDB.user_id == user_id)
        .filter(ReportT1OutcomeDB.status == "evaluated")
        .all()
    ):
        bucket = r.direction_bucket
        if bucket in (None, "neutral", "unknown") or r.label_correct is None:
            abstain_by_day[str(r.signal_trade_date)] += 1
    portfolio_report_ids = None
    if scope == "portfolio":
        report_ids = {str(getattr(row, "report_id", "")).strip() for row in rows if getattr(row, "report_id", None)}
        report_ids.discard("")
        portfolio_report_ids = _portfolio_snapshot_report_ids(db, user_id=user_id, report_ids=report_ids)
    by_day: dict[str, list[ReportT1OutcomeDB]] = defaultdict(list)
    for row in rows:
        if portfolio_report_ids is not None:
            rid = str(getattr(row, "report_id", "")).strip()
            if not rid or rid not in portfolio_report_ids:
                continue
        if row.direction_bucket in (None, "neutral", "unknown"):
            continue
        if row.label_correct is None:
            continue
        by_day[str(row.signal_trade_date)].append(row)
    if start_date and end_date:
        all_days = sorted(d for d in set(by_day) | set(abstain_by_day) if start_date <= d <= end_date)
    else:
        # 与聚合写入端一致：只按**可判分**的日取最近 N 天，否则一段"全是弃权"的日子会
        # 把有信息的交易日挤出窗口。取完再并回这些日子里的弃权日。
        primary = sorted(by_day.keys(), reverse=True)[: max(1, min(days, 400))]
        all_days = sorted(set(primary))
    out: list[dict[str, Any]] = []
    for d in all_days:
        day_rows = by_day[d]
        if not day_rows:
            continue
        # 与 `_refresh_report_daily_stats` 用同一套口径（按价格窗口去重 + Wilson 区间），
        # 否则"聚合表为空时的回退路径"会给出与聚合表不同的命中率，同一张图随数据是否
        # 已刷新而变化。
        windows = t1_stats.dedupe_by_window(
            [
                {
                    "symbol": str(getattr(r, "symbol", "") or "").strip().upper(),
                    "signal_date": str(r.signal_trade_date or ""),
                    "window_days": 1,
                    "direction_bucket": str(r.direction_bucket or "").strip().upper(),
                    "label_correct": bool(r.label_correct) if r.label_correct is not None else None,
                }
                for r in day_rows
            ]
        )
        scored = [
            w
            for w in windows
            if not w.get("conflict") and w.get("direction_bucket") in ("BULLISH", "BEARISH")
        ]
        hits = sum(1 for w in scored if w.get("label_correct"))
        ci_low, ci_high = t1_stats.wilson_interval(hits, len(scored))
        conc = t1_stats.concentration(scored)
        gaps = [_calendar_gap_days(str(r.signal_trade_date), str(r.t1_trade_date)) for r in day_rows]
        gaps = [g for g in gaps if g is not None]
        out.append(
            {
                "date": d,
                "t1_trade_date": str(day_rows[0].t1_trade_date or "") if day_rows else None,
                "sample_count": len(day_rows),
                "accuracy_pct": round(hits / len(scored) * 100.0, 2) if scored else None,
                "avg_calendar_gap_days": round(sum(gaps) / len(gaps), 2) if gaps else None,
                "effective_n": len(scored),
                "unique_symbols": conc.get("unique_symbols"),
                "top_symbol_share": conc.get("top_symbol_share"),
                "ci_low_pct": round(ci_low * 100.0, 2) if ci_low is not None else None,
                "ci_high_pct": round(ci_high * 100.0, 2) if ci_high is not None else None,
                "abstain_count": abstain_by_day.get(d, 0),
                "conflict_count": sum(1 for w in windows if w.get("conflict")),
            }
        )
    return out


def evaluate_report_t1_for_portfolio(
    db: Session,
    *,
    user_id: str,
    limit: int = 400,
    close_cache: CloseCache | T1VendorSeriesCache | None = None,
) -> dict[str, Any]:
    """Backward-compatible alias: all completed reports are evaluated (same as evaluate_report_t1_completed)."""
    return evaluate_report_t1_completed(db, user_id=user_id, limit=limit, close_cache=close_cache)


def recommendation_t1_trend(
    db: Session,
    *,
    user_id: str,
    days: int = 90,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict[str, Any]]:
    """One point per signal date: avg T+1 return & win rate (DB aggregated)."""
    q = (
        db.query(T1DailyStatDB)
        .filter(T1DailyStatDB.user_id == user_id)
        .filter(T1DailyStatDB.metric == "recommendation_t1")
        .filter(T1DailyStatDB.scope == "all")
    )
    if start_date and end_date:
        q = q.filter(T1DailyStatDB.signal_trade_date >= start_date).filter(T1DailyStatDB.signal_trade_date <= end_date)
    rows = q.all()
    if not rows:
        return _recommendation_t1_trend_from_raw(
            db,
            user_id=user_id,
            days=days,
            start_date=start_date,
            end_date=end_date,
        )
    rows = sorted(rows, key=lambda r: str(r.signal_trade_date))
    if not (start_date and end_date):
        rows = rows[-max(1, min(days, 400)) :]
    return [
        {
            "date": str(r.signal_trade_date),
            "t1_trade_date": r.t1_trade_date,
            "sample_count": int(r.sample_count or 0),
            "avg_return_pct": r.avg_return_pct,
            "win_rate_pct": r.win_rate_pct,
            "avg_calendar_gap_days": r.avg_calendar_gap_days,
            "poor_tradability_rate_pct": r.poor_tradability_rate_pct,
            "high_impact_risk_rate_pct": r.high_impact_risk_rate_pct,
        }
        for r in rows
    ]


def _daily_stat_is_prefix(row: Any) -> bool:
    """这一行是否来自 D2/D3 修复**之前**的写入（即诚实列从未被写过）。

    判据取 `effective_n IS NULL`，因为写入端是无条件赋值
    （`effective_n=len(scored)`，见 `_refresh_report_daily_stats`），所以修复后的行
    一定有值（可以是 0，但不会是 NULL）。

    刻意**不**用 `ci_low`/`ci_high` 当判据：`scored` 为空时它们的 None 是合法值，
    用它会把正常行误判成存量行。
    """
    return getattr(row, "effective_n", None) is None


def report_accuracy_t1_trend(
    db: Session,
    *,
    user_id: str,
    days: int = 90,
    start_date: str | None = None,
    end_date: str | None = None,
    scope: str = "portfolio",
) -> list[dict[str, Any]]:
    """One point per signal date: directional accuracy on T+1 (DB aggregated)."""
    if scope not in ("all", "portfolio"):
        scope = "portfolio"
    q = (
        db.query(T1DailyStatDB)
        .filter(T1DailyStatDB.user_id == user_id)
        .filter(T1DailyStatDB.metric == "report_accuracy_t1")
        .filter(T1DailyStatDB.scope == scope)
    )
    if start_date and end_date:
        q = q.filter(T1DailyStatDB.signal_trade_date >= start_date).filter(T1DailyStatDB.signal_trade_date <= end_date)
    rows = q.all()
    # 存量聚合行会**遮蔽**诚实度量 —— 这是 D2/D3 最隐蔽的失效方式。
    #
    # `t1_daily_stats` 里既有行只要存在，下面就会原样返回；而 D2/D3 之前的 921 行
    # 的 `effective_n` / `ci_low` / `ci_high` / `abstain_count` / `conflict_count`
    # **全是 NULL**。于是接口对每一个数据点都返回 None：去重后的分母、置信区间、弃权
    # 与矛盾计数统统消失，界面上那些"诚实度量"字段静默变成空白，而写入路径其实完全
    # 正常（在库副本上跑 refresh 能正常填出 104 行）。
    #
    # 所以：只要数据落在**修复之前**，就不能相信这张表，改为从原始 outcome 现算。
    # 回退路径与聚合写入端共用同一套口径（按价格窗口去重 + Wilson），这是刻意的，
    # 否则同一张图会因为"数据是否已刷新"而给出不同命中率。
    #
    # 只要**窗口内任意一行**是存量的就整体重算，而不是逐行拼接：半新半旧的序列会比
    # 全旧更难解释——同一张图上有的点有区间、有的没有，读者无从判断该信哪一段。
    if not rows or any(_daily_stat_is_prefix(r) for r in rows):
        return _report_accuracy_t1_trend_from_raw(
            db,
            user_id=user_id,
            days=days,
            start_date=start_date,
            end_date=end_date,
            scope=scope,
        )
    rows = sorted(rows, key=lambda r: str(r.signal_trade_date))
    if not (start_date and end_date):
        rows = rows[-max(1, min(days, 400)) :]
    return [
        {
            "date": str(r.signal_trade_date),
            "t1_trade_date": r.t1_trade_date,
            "sample_count": int(r.sample_count or 0),
            "accuracy_pct": r.accuracy_pct,
            "avg_calendar_gap_days": r.avg_calendar_gap_days,
            # D1/D2/D3：诚实度量。`sample_count` 是研报行数，`effective_n` 是去重后的
            # 唯一价格窗口数——后者才是命中率的分母。两者差距大就说明这一天被少数
            # (标的,日期) 上的重复研报主导。`ci_low/ci_high` 是 Wilson 区间；
            # 单日 n 很小，区间宽是常态，不要把点估计当结论。
            "effective_n": r.effective_n,
            "unique_symbols": r.unique_symbols,
            "top_symbol_share": r.top_symbol_share,
            "ci_low_pct": round(float(r.ci_low) * 100.0, 2) if r.ci_low is not None else None,
            "ci_high_pct": round(float(r.ci_high) * 100.0, 2) if r.ci_high is not None else None,
            "abstain_count": r.abstain_count,
            "conflict_count": r.conflict_count,
        }
        for r in rows
    ]


def report_accuracy_t1_summary(
    db: Session,
    *,
    user_id: str,
    days: int = 90,
    start_date: str | None = None,
    end_date: str | None = None,
    scope: str = "all",
) -> dict[str, Any]:
    """D3：区间整体（而非单日）的方向命中率，带有效样本量与显著性判定。

    单日的 `effective_n` 常常只有个位数，Wilson 区间宽到没有信息量；把整个区间
    的窗口汇总起来才可能得到一个有意义的结论。这里刻意**跨日去重**：同一
    (标的, 信号日) 的价格窗口在整段区间里只算一次，所以
    ``effective_n`` 是真正的独立观测数。

    判定逻辑（对应用户最关心的问题"这东西到底有没有用"）：

    * ``beats_coin_flip``：区间下界 > 50%，即**有统计证据**优于掷硬币；
    * ``not_significant``：区间跨过 50%，即现有样本**无法区分**于掷硬币；
    * ``required_n``：若要点估计为真，需要多少独立样本才能在 80% 功效下
      把它与 50% 区分开。``effective_n`` 远低于它时，任何"命中率 53%"的
      说法都不该被当成结论。
    """
    if scope not in ("all", "portfolio"):
        scope = "all"
    q = (
        db.query(ReportT1OutcomeDB)
        .filter(ReportT1OutcomeDB.user_id == user_id)
        .filter(ReportT1OutcomeDB.status == "evaluated")
        .filter(ReportT1OutcomeDB.signal_trade_date.isnot(None))
    )
    if start_date and end_date:
        q = q.filter(ReportT1OutcomeDB.signal_trade_date >= start_date).filter(
            ReportT1OutcomeDB.signal_trade_date <= end_date
        )
    rows = q.all()
    if not rows:
        return {
            "scope": scope,
            "effective_n": 0,
            "accuracy_pct": None,
            "note": "无已评估样本",
        }
    if scope == "portfolio":
        ids = {str(getattr(r, "report_id", "") or "").strip() for r in rows}
        ids.discard("")
        portfolio_ids = _portfolio_snapshot_report_ids(db, user_id=user_id, report_ids=ids)
        rows = [r for r in rows if str(getattr(r, "report_id", "") or "").strip() in portfolio_ids]

    windows = t1_stats.dedupe_by_window(
        [
            {
                "symbol": str(getattr(r, "symbol", "") or "").strip().upper(),
                "signal_date": str(r.signal_trade_date or ""),
                "window_days": 1,
                "direction_bucket": str(r.direction_bucket or "").strip().upper(),
                "label_correct": bool(r.label_correct) if r.label_correct is not None else None,
                # 必须带上收益：去 beta 时需要它算超额，`dedupe_by_window` 会把第一行的
                # 原始值透传出来。漏掉的话超额命中率会静默变成空集。
                "forward_return": getattr(r, "return_t1_pct", None),
                # F1：计划自己的持有期，用来度量期限错配（同样需要透传）。
                "plan_horizon_days": getattr(r, "plan_horizon_days", None),
                # A5/D5：本条打分衡量的周期。
                "horizon": getattr(r, "horizon", None),
                # 用于把窗口连回其交易计划（F1 的未回填计划计数）。
                "report_id": str(getattr(r, "report_id", "") or ""),
            }
            for r in rows
        ]
    )
    scored = [
        w for w in windows if not w.get("conflict") and w.get("direction_bucket") in ("BULLISH", "BEARISH")
    ]
    hits = sum(1 for w in scored if w.get("label_correct"))
    n = len(scored)
    ci_low, ci_high = t1_stats.wilson_interval(hits, n)
    rate = (hits / n) if n else None
    conc = t1_stats.concentration(scored)
    abstain = sum(
        1
        for r in rows
        if str(r.direction_bucket or "").strip().lower() in ("", "none", "neutral", "unknown")
        or r.label_correct is None
    )

    # P5（去 beta）：绝对命中率里绝大部分是市场 beta。
    #
    # 池子基准上涨率约 51.8%、赔率约 +0.551%/日——只要大盘在涨，闭眼买入就有约 52%
    # 命中率。所以"绝对命中率"几乎不反映研判能力。这里用**同一天其它标的的收益均值**
    # （逐行留一，见 `leave_one_out_excess`）作为基准：只有跑赢同侪才算对。
    #
    # 单标的日没有同侪可比，直接不算——返回它自己的收益会让超额恒为 0，等于凭空
    # 造出一个 50% 的超额命中率。那些日子的数量单独报出来。
    l1o_rows = [
        {
            "signal_date": str(getattr(r, "signal_trade_date", "") or ""),
            "forward_return": getattr(r, "return_t1_pct", None),
        }
        for r in rows
    ]
    bench_means, bench_counts = t1_stats.daily_mean_and_count(l1o_rows)

    excess_windows: list[dict[str, Any]] = []
    for w in scored:
        day = str(w.get("signal_date") or "")
        if int(bench_counts.get(day, 0)) < 2:
            continue
        ret = w.get("forward_return")
        if ret is None:
            continue
        excess = t1_stats.leave_one_out_excess(
            {"signal_date": day, "forward_return": ret}, bench_means, bench_counts
        )
        if excess is None:
            continue
        direction = w.get("direction_bucket")
        excess_windows.append(
            {
                "signal_date": day,
                "direction_bucket": direction,
                "excess": excess,
                # 相对口径下的"对"：看多且跑赢同侪，或看空且跑输同侪。
                "excess_correct": (excess > 0.0) if direction == "BULLISH" else (excess < 0.0),
            }
        )

    n_excess = len(excess_windows)
    excess_hits = sum(1 for w in excess_windows if w["excess_correct"])
    ex_lo, ex_hi = t1_stats.wilson_interval(excess_hits, n_excess)
    ex_rate = (excess_hits / n_excess) if n_excess else None
    scored_days = {str(w.get("signal_date") or "") for w in scored}
    excess_days = {w["signal_date"] for w in excess_windows}
    excess_block: dict[str, Any] = {
        "effective_n": n_excess,
        "accuracy_pct": round(ex_rate * 100.0, 2) if ex_rate is not None else None,
        "ci_low_pct": round(ex_lo * 100.0, 2) if ex_lo is not None else None,
        "ci_high_pct": round(ex_hi * 100.0, 2) if ex_hi is not None else None,
        "days_measured": len(excess_days),
        "days_without_peers": len(scored_days) - len(excess_days),
        "benchmark": "同日其它已评估标的的 T+1 收益均值（逐行留一）",
        "note": "去掉市场 beta 后的方向命中率；与 50% 比较才有意义",
    }
    if n_excess:
        ex_st = t1_stats.day_clustered_stats(
            [(w["signal_date"], (1.0 if w["excess_correct"] else 0.0) - 0.5) for w in excess_windows]
        )
        ex_acc = 0.5 + ex_st.mean
        excess_block["clustered"] = {
            "accuracy_pct": round(ex_acc * 100.0, 2),
            "se_pct": round(ex_st.se * 100.0, 2) if ex_st.se is not None else None,
            "t": round(ex_st.t, 2) if ex_st.t is not None else None,
            "ci_low_pct": round((ex_acc - 1.96 * ex_st.se) * 100.0, 2) if ex_st.se else None,
            "ci_high_pct": round((ex_acc + 1.96 * ex_st.se) * 100.0, 2) if ex_st.se else None,
            "n_days": ex_st.n_days,
        }
        excess_block["verdict"] = (
            "beats_coin_flip"
            if ex_lo is not None and ex_lo > 0.5
            else ("not_significant" if ex_lo is not None and ex_hi is not None and ex_lo <= 0.5 <= ex_hi else "worse_than_coin_flip")
        )
        excess_block["not_significant"] = excess_block["verdict"] == "not_significant"

    # 多空价差（这才是与成本可比的那个数）：看多标的的平均超额 − 看空标的的平均超额。
    #
    # 单位注意：`return_t1_pct` 已经是**百分点**（实测范围 −13.4 ~ +20.0，均值 0.377），
    # 不是小数收益率。所以这里不要再乘 100——否则价差会被放大 100 倍，把一个
    # "低于交易成本"的结果误报成"远超成本"。
    bull_ex = [w["excess"] for w in excess_windows if w["direction_bucket"] == "BULLISH"]
    bear_ex = [w["excess"] for w in excess_windows if w["direction_bucket"] == "BEARISH"]
    spread: dict[str, Any] = {}
    if bull_ex or bear_ex:
        bull_mean = sum(bull_ex) / len(bull_ex) if bull_ex else None
        bear_mean = sum(bear_ex) / len(bear_ex) if bear_ex else None
        spread = {
            "unit": "percentage_points_per_day",
            "bullish_n": len(bull_ex),
            "bearish_n": len(bear_ex),
            "bullish_mean_excess_pct": round(bull_mean, 4) if bull_mean is not None else None,
            "bearish_mean_excess_pct": round(bear_mean, 4) if bear_mean is not None else None,
        }
        if bull_mean is not None and bear_mean is not None:
            ls = bull_mean - bear_mean
            spread["long_short_spread_pct"] = round(ls, 4)
            spread["cost_round_trip_pct"] = 0.25
            spread["covers_cost"] = bool(ls > 0.25)

    # `required_n_for_accuracy` 要求 target 严格落在 (0, 1)：0.0 与 1.0 都会抛错。
    # 而 100% 或 0% 的命中率在一个小样本里是常事（例如 3/3），不是异常输入。
    # 这里把点估计夹到 (0,1) 开区间内再算所需样本量——夹逼只在小数点后极远处发生，
    # 对结果无实质影响，但避免了"样本很小"反而触发异常的荒谬情况。
    if rate is None or rate <= 0.5:
        required = 0
    else:
        required = t1_stats.required_n_for_accuracy(min(max(rate, 1e-6), 1.0 - 1e-6))

    if n == 0:
        verdict = "no_sample"
    elif ci_low is not None and ci_low > 0.5:
        verdict = "beats_coin_flip"
    elif ci_low is not None and ci_high is not None and ci_low <= 0.5 <= ci_high:
        verdict = "not_significant"
    else:
        verdict = "worse_than_coin_flip"

    n_days = len({str(w.get("signal_date") or "") for w in scored})

    # 第二种估计量：**按日等权**。
    #
    # 池化命中率（上面的 `accuracy_pct`）把每个判断当独立观测，但同一交易日内的
    # 判断是横截面相关的（大盘同涨同跌）。按日聚类后取"每日均值再平均"，等于让
    # 每个交易日只投一票——样本量大的一天不会因为研报多就压过样本量小的一天。
    #
    # 两者在真实库上差距很大（池化 52.34% vs 按日等权 47.66%），说明池化那个数
    # 是被少数高产的交易日抬起来的。所以两个都要报，且以按日等权的区间更保守、
    # 更可信。
    clustered: dict[str, Any] = {}
    if n_days >= 2:
        st = t1_stats.day_clustered_stats(
            [(w.get("signal_date"), (1.0 if w.get("label_correct") else 0.0) - 0.5) for w in scored]
        )
        boot_low, boot_high = t1_stats.bootstrap_ci_by_date(
            [(w.get("signal_date"), 1.0 if w.get("label_correct") else 0.0) for w in scored]
        )
        acc_cl = 0.5 + st.mean
        clustered = {
            "accuracy_pct": round(acc_cl * 100.0, 2),
            "se_pct": round(st.se * 100.0, 2) if st.se is not None else None,
            "t": round(st.t, 2) if st.t is not None else None,
            "ci_low_pct": round((acc_cl - 1.96 * st.se) * 100.0, 2) if st.se else None,
            "ci_high_pct": round((acc_cl + 1.96 * st.se) * 100.0, 2) if st.se else None,
            "bootstrap_ci_low_pct": round(boot_low * 100.0, 2) if boot_low is not None else None,
            "bootstrap_ci_high_pct": round(boot_high * 100.0, 2) if boot_high is not None else None,
            "n_days": st.n_days,
            "note": "按日等权（每日一票）+ 按日聚类 SE；对少数高产交易日的支配更稳健",
        }

    # F1：期限错配的可见化。
    #
    # 本函数打的是**次日方向**（价格窗口固定为一个交易日），但被评报告往往附着一份
    # 约 20 天期的交易计划（目标价/止损/时间止损）。若不说明，读者会以为这个命中率
    # 评的是那些中期目标是否达成。这里统计被评窗口所附计划的持有期分布，让错配程度
    # 可度量：如果多数窗口承载的是 20 天期方案，那么这个"次日命中率"的解释力就更弱。
    _plan_days = [
        int(w["plan_horizon_days"])
        for w in scored
        if w.get("plan_horizon_days") is not None
    ]
    # 还有一类窗口：报告**确实**附了计划，但该窗口是在本列存在之前打的分，所以
    # `plan_horizon_days` 为 NULL。必须与"压根没有计划"区分开，否则读者会得出
    # "计划表是空的"这一错误结论，而真相是"计划存在、只是还没重新评估"。
    # 刻意**不做**回填：按周期词猜持有期会把估计值伪装成记录值。
    _scored_report_ids = {
        str(w.get("report_id") or "").strip() for w in scored if w.get("report_id")
    }
    _unrecorded = 0
    if _scored_report_ids:
        # `distinct()`：现在 `trade_plans.report_id` 有唯一约束（`uq_trade_plans_report_id`），
        # 所以这个 join 是 1:1、行数就是窗口数。但一旦该约束被放开，多份计划会让计数
        # 悄悄翻倍，而这个字段的名字说的是「窗口」。加 distinct 后语义不依赖约束存在。
        _unrecorded = (
            db.query(ReportT1OutcomeDB.report_id)
            .join(TradePlanDB, TradePlanDB.report_id == ReportT1OutcomeDB.report_id)
            .filter(ReportT1OutcomeDB.report_id.in_(list(_scored_report_ids)))
            .filter(ReportT1OutcomeDB.plan_horizon_days.is_(None))
            .distinct()
            .count()
        )
    # 口径不能只靠一句硬编码的声明。
    #
    # `horizon` 曾是写死的 `"t1"`，而 `report_t1_outcomes.horizon` 列虽然落了库却没人读——
    # 那是「加了列但没接线」。这里改为**从列里数出来**：窗口里出现过的 horizon 取值分布。
    # 存量行是 NULL（改造前打分，周期真的不可知，不做回填），所以 `null` 一定存在；
    # 若将来有人把非 t1 的行混进本指标，`values` 里就会出现第二个键，口径声明随之失效，
    # 而不是继续自称只评次日。
    _horizon_values: dict[str, int] = {}
    for w in scored:
        key = str(w.get("horizon") or "").strip().lower() or "null"
        _horizon_values[key] = _horizon_values.get(key, 0) + 1
    # 本函数的价格窗口恒为一个交易日（`_report_price_window`），所以只要窗口里出现了
    # 除 t1/null 以外的取值，就说明这个指标被喂了不该喂的行。
    _foreign = sorted(k for k in _horizon_values if k not in ("t1", "null"))

    # `reports.horizon`（A5 的另一半）：这份**报告本身**属于哪个周期。
    #
    # 与 `report_t1_outcomes.horizon` 不是一回事：后者说的是「这次打分衡量的是一天」，
    # 前者说的是「这条结论原本是给哪个周期下的」。A5 要解决的歧义正在这里——一份
    # `dual`（当时同时给了短/中期两套结论）的报告，其被持久化的方向其实取自短期那套，
    # 但此前两者在库里长得完全一样。该列曾长期只建不写（0/7504），现在
    # `create_report` 会写；这里把它读出来，让错配可见。
    _report_horizon_values: dict[str, int] = {}
    if _scored_report_ids:
        for (rh,) in (
            db.query(ReportDB.horizon)
            .filter(ReportDB.id.in_(list(_scored_report_ids)))
            .all()
        ):
            key = str(rh or "").strip().lower() or "null"
            _report_horizon_values[key] = _report_horizon_values.get(key, 0) + 1

    plan_horizon: dict[str, Any] = {
        "measured_horizon": "t1",
        "unit": "natural_days",
        "scored_horizon_values": _horizon_values,
        "foreign_horizon_values": _foreign,
        # 被评报告自身的周期分布（`reports.horizon`）。`dual` 表示该报告当时给了
        # 短/中期两套结论而持久化的是短期那套——这些窗口按次日方向打分是合理的，
        # 但不应被读成「这份报告只对次日负责」。
        "graded_report_horizon_values": _report_horizon_values,
        "windows_with_plan": len(_plan_days),
        "windows_without_plan": n - len(_plan_days),
        "windows_with_unrecorded_plan_horizon": _unrecorded,
        "median_plan_days": (
            sorted(_plan_days)[len(_plan_days) // 2] if _plan_days else None
        ),
        "multi_day_plan_windows": sum(1 for d in _plan_days if d > 1),
        "note": (
            "本命中率只评**次日方向**；计划的目标价/止损/时间止损属更长期限，"
            "其达成情况不在此指标内。multi_day_plan_windows 越大，期限错配越严重；"
            "windows_with_unrecorded_plan_horizon > 0 表示这些窗口附有计划但尚未按新列"
            "重新评估（不做回填，以免把猜测当成记录）。"
        ),
    }

    return {
        "scope": scope,
        "effective_n": n,
        "horizon": "t1",
        "plan_horizon": plan_horizon,
        "hits": hits,
        "accuracy_pct": round(rate * 100.0, 2) if rate is not None else None,
        "ci_low_pct": round(ci_low * 100.0, 2) if ci_low is not None else None,
        "ci_high_pct": round(ci_high * 100.0, 2) if ci_high is not None else None,
        "n_days": n_days,
        "unique_symbols": conc.get("unique_symbols"),
        "top_symbol_share": conc.get("top_symbol_share"),
        "conflict_n": sum(1 for w in windows if w.get("conflict")),
        "row_count": len(rows),
        "abstain_count": abstain,
        "coverage_pct": round(n / len(rows) * 100.0, 2) if rows else None,
        "required_n": required,
        "underpowered": bool(required and n < required),
        "beats_coin_flip": verdict == "beats_coin_flip",
        "not_significant": verdict == "not_significant",
        "verdict": verdict,
        "clustered": clustered,
        # P5：绝对口径一定被 beta 抬高，相对口径才是能力的体现。
        "excess": excess_block,
        "excess_spread": spread,
    }


def recommendation_t1_detail(db: Session, *, user_id: str, date: str) -> list[dict[str, Any]]:
    """Return individual scan rows for a specific signal date, sorted by T+1 return desc."""
    rows = (
        db.query(MarketScanResultDB)
        .filter(MarketScanResultDB.user_id == user_id)
        .filter(MarketScanResultDB.t1_signal_date == date)
        .filter(MarketScanResultDB.t1_status == "evaluated")
        .order_by(MarketScanResultDB.t1_return_pct.desc())
        .all()
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        nm = row.name
        sym_u = str(row.symbol or "").strip().upper()
        if not nm or str(nm).strip().upper() == sym_u:
            resolved = _cn_listing_name_for_symbol(str(row.symbol or ""))
            if resolved:
                nm = resolved
        created_iso: str | None = None
        if row.created_at:
            ca = row.created_at
            if ca.tzinfo is None:
                ca = ca.replace(tzinfo=timezone.utc)
            created_iso = ca.astimezone(CN_TZ).strftime("%Y-%m-%d %H:%M")
        out.append(
            {
                "id": row.id,
                "symbol": row.symbol,
                "name": nm,
                "score": row.score,
                "rank": row.rank,
                "t1_signal_date": row.t1_signal_date,
                "t1_trade_date": row.t1_trade_date,
                "t1_return_pct": row.t1_return_pct,
                "entry_price": row.entry_price,
                "source_mode": row.source_mode,
                "scan_created_at": created_iso,
            }
        )
    return out


def report_accuracy_t1_detail(
    db: Session, *, user_id: str, date: str, scope: str = "portfolio"
) -> list[dict[str, Any]]:
    """Return individual report T+1 outcome rows for a specific signal date with report details.

    scope ``portfolio``: reports with non-zero position snapshot at generation time, ``status == evaluated`` only (与持仓曲线一致).

    scope ``all``: 该信号日下当前账户全部深度报告对应的 T+1 结果行（含 pending / insufficient_data / evaluated），不限持仓。
    """
    q = (
        db.query(ReportT1OutcomeDB)
        .filter(ReportT1OutcomeDB.user_id == user_id)
        .filter(ReportT1OutcomeDB.signal_trade_date == date)
    )
    if scope == "portfolio":
        q = q.filter(ReportT1OutcomeDB.status == "evaluated")
        report_ids_for_day = {
            str(rid or "").strip()
            for (rid,) in db.query(ReportT1OutcomeDB.report_id)
            .filter(ReportT1OutcomeDB.user_id == user_id)
            .filter(ReportT1OutcomeDB.signal_trade_date == date)
            .all()
        }
        report_ids_for_day.discard("")
        portfolio_report_ids = _portfolio_snapshot_report_ids(db, user_id=user_id, report_ids=report_ids_for_day)
        if not portfolio_report_ids:
            return []
        q = q.filter(ReportT1OutcomeDB.report_id.in_(list(portfolio_report_ids)))
    outcomes = q.all()
    report_ids = [str(o.report_id) for o in outcomes if getattr(o, "report_id", None)]
    reports_by_id: dict[str, ReportDB] = {}
    if report_ids:
        for r in db.query(ReportDB).filter(ReportDB.id.in_(report_ids)).all():
            reports_by_id[str(r.id)] = r

    out: list[dict[str, Any]] = []
    for o in outcomes:
        rep = reports_by_id.get(str(o.report_id)) if getattr(o, "report_id", None) else None
        model_info = _extract_report_model_info(rep.result_data if rep else None)
        display_name = _resolve_depth_report_display_name(
            o.symbol,
            portfolio_name=None,
            result_data=rep.result_data if rep else None,
        )
        # Report creation time in CST for display
        report_created_iso: str | None = None
        if rep and rep.created_at:
            ca = rep.created_at
            if ca.tzinfo is None:
                ca = ca.replace(tzinfo=timezone.utc)
            report_created_iso = ca.astimezone(CN_TZ).strftime("%Y-%m-%d %H:%M")
        out.append(
            {
                "symbol": o.symbol,
                "name": display_name,
                "signal_trade_date": o.signal_trade_date,
                "t1_trade_date": o.t1_trade_date,
                "report_trade_date": rep.trade_date if rep else None,
                "report_created_at": report_created_iso,
                "p0": o.p0,
                "p1": o.p1,
                "return_t1_pct": o.return_t1_pct,
                "direction_bucket": o.direction_bucket,
                "label_correct": o.label_correct,
                "direction": rep.direction if rep else None,
                "decision": rep.decision if rep else None,
                "report_id": o.report_id,
                "evaluation_status": o.status,
                "evaluation_reason": o.reason,
                "model_profile_id": model_info.get("model_profile_id"),
                "model_profile_name": model_info.get("model_profile_name"),
                "llm_provider": model_info.get("llm_provider"),
                "quick_think_llm": model_info.get("quick_think_llm"),
                "deep_think_llm": model_info.get("deep_think_llm"),
            }
        )
    if scope == "portfolio":
        out.sort(key=lambda x: (x["label_correct"] is False, x["symbol"] or ""))
    else:
        _order = {"evaluated": 0, "pending": 1, "insufficient_data": 2}
        out.sort(
            key=lambda x: (
                _order.get(str(x.get("evaluation_status") or ""), 9),
                x["label_correct"] is False,
                x["symbol"] or "",
            )
        )
    return out


def refresh_all_t1(
    db: Session,
    *,
    user_id: str,
    scope: str = "all",
    lookback_days: int = 3,
    limit: int = 500,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
) -> dict[str, Any]:
    """Refresh T+1 rows; default manual window is controlled by endpoint."""
    _ = scope
    _ = lookback_days
    _ = limit
    close_cache = T1VendorSeriesCache(db)
    reconcile = reconcile_report_t1_outcomes(
        db,
        user_id=user_id,
        lookback_days=7,
        close_cache=close_cache,
    )
    a = evaluate_market_scan_t1(
        db,
        user_id=user_id,
        limit=500,
        min_signal_trade_date=None,
        window_start=window_start,
        window_end=window_end,
        close_cache=close_cache,
    )
    b = evaluate_report_t1_completed(
        db,
        user_id=user_id,
        limit=500,
        min_signal_trade_date=None,
        window_start=window_start,
        window_end=window_end,
        close_cache=close_cache,
    )
    touched_signal_dates = set(a.get("touched_signal_dates") or []).union(set(b.get("touched_signal_dates") or []))
    touched_signal_dates.update(reconcile.get("touched_signal_dates") or [])
    refresh_t1_daily_stats(db, user_id=user_id, signal_dates=touched_signal_dates or None)
    return {
        "scope": "all",
        "lookback_days": None,
        "window_start": window_start.isoformat() if window_start else None,
        "window_end": window_end.isoformat() if window_end else None,
        "reconcile": reconcile,
        "market_scan": a,
        "reports": b,
    }


def _has_poor_tradability_risk(flags: list[str]) -> bool:
    vals = {str(x or "").strip().lower() for x in (flags or [])}
    keys = {"illiquid_turnover", "borderline_liquidity", "volume_dry_up", "near_limit_up", "near_limit_down"}
    return bool(vals.intersection(keys))


def _has_high_impact_risk(flags: list[str]) -> bool:
    vals = {str(x or "").strip().lower() for x in (flags or [])}
    keys = {"hot_money_risk", "high_volatility", "near_limit_up", "near_limit_down"}
    return bool(vals.intersection(keys))
