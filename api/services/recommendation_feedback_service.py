from __future__ import annotations

import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from io import StringIO
from threading import Lock
import time
from typing import Any, Iterable
from uuid import uuid4

import pandas as pd
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from api.database import MarketScanResultDB, StrategyFeedbackStatDB
from api.services.market_scanner_service import STRATEGY_HOLD_DAYS
from tradingagents.dataflows.interface import route_to_vendor

# Maps strategy hit labels → the scoring factor they primarily represent
_STRATEGY_FACTOR_MAP: dict[str, str] = {
    "strong_momentum":    "momentum",
    "trend_up":           "momentum",
    "reversal_dip":       "momentum",   # reversal profile uses inverted momentum
    "active_liquidity":   "activity",
    "breakout_near_high": "near_high",
    "high_tight_range":   "near_high",
    "52w_near_high":      "near_high",
    "52w_middle":         "near_high",
    "sector_rotation":    "sector",
    "volume_surge":       "volume_ratio",
    "intraday_bull_body": "momentum",
    "turnover_quality":   "volatility",  # quality turnover correlates with low-volatility signal
    "low_volatility":     "volatility",
}

_STRATEGY_FEEDBACK_REFRESH_LOCK = Lock()


def _infer_hold_days(strategy_hits: list[str], fallback: int = 5) -> int:
    """Pick the hold period for feedback based on the strategy labels present.

    If a candidate hits multiple strategies we take the maximum horizon so the
    evaluation waits long enough for all signals to play out.
    """
    days = [STRATEGY_HOLD_DAYS.get(hit) for hit in strategy_hits]
    valid = [d for d in days if d is not None]
    return max(valid) if valid else fallback


def persist_scan_results(
    db: Session,
    *,
    user_id: str,
    run_id: str | None,
    source_mode: str,
    market: str,
    score_profile: str | None,
    items: Iterable[dict[str, Any]],
    selected_symbols: Iterable[str],
    feedback_horizon_days: int | None = None,
) -> None:
    """Persist market scan results to DB.

    If ``feedback_horizon_days`` is *None* (recommended), the hold period is
    inferred per-item from the strategy_hits using ``STRATEGY_HOLD_DAYS``.
    Pass an explicit integer only when you want to override (e.g. in tests).
    """
    selected = {str(x or "").strip().upper() for x in selected_symbols}
    if run_id:
        db.query(MarketScanResultDB).filter(MarketScanResultDB.run_id == run_id).delete()

    now = datetime.now(timezone.utc)
    rows: list[MarketScanResultDB] = []
    for idx, item in enumerate(items, start=1):
        symbol = str(item.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        strategy_hits = list(item.get("strategy_hits") or [])
        hold_days = (
            feedback_horizon_days
            if feedback_horizon_days is not None
            else _infer_hold_days(strategy_hits)
        )
        rows.append(
            MarketScanResultDB(
                id=uuid4().hex,
                run_id=run_id,
                user_id=user_id,
                source_mode=source_mode,
                market=market,
                score_profile=score_profile,
                symbol=symbol,
                name=str(item.get("name") or symbol),
                rank=idx,
                score=_to_float(item.get("score")),
                reasons_json=list(item.get("reasons") or []),
                strategy_hits_json=strategy_hits,
                risk_flags_json=list(item.get("risk_flags") or []),
                score_breakdown_json=dict(item.get("score_breakdown") or {}),
                quote_json={
                    "price": item.get("live_price"),
                    "change_pct": item.get("price_change_pct"),
                    "high": item.get("day_high"),
                    "open": item.get("day_open"),
                    "amount": item.get("amount"),
                    "volume": item.get("volume"),
                    "volume_ratio": item.get("volume_ratio"),
                    "turnover_rate": item.get("turnover_rate"),
                    "sector": item.get("sector"),
                    "quote_time": item.get("quote_time"),
                    "source": item.get("quote_source"),
                },
                selected_for_analysis=symbol in selected,
                entry_price=_to_float(item.get("live_price")),
                feedback_horizon_days=max(1, int(hold_days)),
                feedback_status="pending",
                created_at=now,
                updated_at=now,
            )
        )
    if rows:
        db.add_all(rows)


def list_scan_results(
    db: Session,
    *,
    user_id: str,
    limit: int = 50,
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    query = db.query(MarketScanResultDB).filter(MarketScanResultDB.user_id == user_id)
    if run_id:
        query = query.filter(MarketScanResultDB.run_id == run_id)
    rows = (
        query.order_by(MarketScanResultDB.created_at.desc(), MarketScanResultDB.rank.asc())
        .limit(max(1, min(int(limit), 200)))
        .all()
    )
    return [_scan_row_to_dict(row) for row in rows]


def evaluate_pending_feedback(
    db: Session,
    *,
    user_id: str | None = None,
    limit: int = 80,
) -> dict[str, Any]:
    query = db.query(MarketScanResultDB).filter(MarketScanResultDB.feedback_status == "pending")
    if user_id:
        query = query.filter(MarketScanResultDB.user_id == user_id)
    rows = query.order_by(MarketScanResultDB.created_at.asc()).limit(max(1, min(int(limit), 300))).all()

    evaluated = 0
    skipped = 0
    now = datetime.now(timezone.utc)
    for row in rows:
        created_at = row.created_at or now
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        if created_at + timedelta(days=int(row.feedback_horizon_days or 5)) > now:
            skipped += 1
            continue
        exit_price = _get_price_after(row.symbol, created_at.strftime("%Y-%m-%d"), int(row.feedback_horizon_days or 5))
        if row.entry_price and exit_price and row.entry_price > 0:
            row.exit_price = round(exit_price, 4)
            row.realized_return_pct = round((exit_price - row.entry_price) / row.entry_price * 100.0, 2)
            row.feedback_status = "evaluated"
        else:
            row.feedback_status = "insufficient_data"
        row.feedback_evaluated_at = now
        row.updated_at = now
        evaluated += 1
    return {"processed": len(rows), "evaluated": evaluated, "skipped": skipped}


def refresh_strategy_feedback_stats(db: Session, *, user_id: str | None = None) -> dict[str, Any]:
    query = db.query(MarketScanResultDB).filter(MarketScanResultDB.feedback_status == "evaluated")
    if user_id:
        query = query.filter(MarketScanResultDB.user_id == user_id)
    lookback = int(os.getenv("TA_STRATEGY_FEEDBACK_LOOKBACK_DAYS", "0") or "0")
    if lookback > 0:
        lb = max(1, min(lookback, 3650))
        cutoff = datetime.now(timezone.utc) - timedelta(days=lb)
        query = query.filter(MarketScanResultDB.created_at >= cutoff)
    rows = query.all()

    grouped: dict[tuple[str, str], list[MarketScanResultDB]] = defaultdict(list)
    for row in rows:
        for strategy_key in list(row.strategy_hits_json or []):
            grouped[(row.user_id, strategy_key)].append(row)

    target_user_ids = sorted({uid for uid, _ in grouped.keys()})
    if user_id:
        existing_rows = (
            db.query(StrategyFeedbackStatDB)
            .filter(StrategyFeedbackStatDB.user_id == user_id)
            .all()
        )
    elif target_user_ids:
        existing_rows = (
            db.query(StrategyFeedbackStatDB)
            .filter(StrategyFeedbackStatDB.user_id.in_(target_user_ids))
            .all()
        )
    else:
        existing_rows = []
    existing_map: dict[tuple[str, str], StrategyFeedbackStatDB] = {
        (str(r.user_id), str(r.strategy_key)): r for r in existing_rows
    }
    touched_keys: set[tuple[str, str]] = set()

    now = datetime.now(timezone.utc)
    created = 0
    for (uid, strategy_key), items in grouped.items():
        returns = [float(r.realized_return_pct or 0.0) for r in items if r.realized_return_pct is not None]
        if not returns:
            continue
        wins = sum(1 for val in returns if val > 0)
        sample_count = len(returns)
        avg_ret = sum(returns) / sample_count
        avg_score = sum(float(r.score or 0.0) for r in items) / max(1, len(items))
        win_rate = wins / sample_count * 100.0
        edge = ((win_rate - 50.0) / 50.0) * 0.05 + (avg_ret / 10.0) * 0.05
        weight_delta = max(-0.08, min(0.08, edge))
        last_entry_at = max((r.created_at for r in items if r.created_at), default=None)
        key = (uid, strategy_key)
        touched_keys.add(key)
        row = existing_map.get(key)
        if row is None:
            row = StrategyFeedbackStatDB(
                id=uuid4().hex,
                user_id=uid,
                strategy_key=strategy_key,
                factor_bucket=_STRATEGY_FACTOR_MAP.get(strategy_key, "momentum"),
                sample_count=sample_count,
                win_rate=round(win_rate, 2),
                avg_return_pct=round(avg_ret, 2),
                avg_score=round(avg_score, 2),
                weight_delta=round(weight_delta, 4),
                last_entry_at=last_entry_at,
                updated_at=now,
            )
            db.add(row)
        else:
            row.factor_bucket = _STRATEGY_FACTOR_MAP.get(strategy_key, "momentum")
            row.sample_count = sample_count
            row.win_rate = round(win_rate, 2)
            row.avg_return_pct = round(avg_ret, 2)
            row.avg_score = round(avg_score, 2)
            row.weight_delta = round(weight_delta, 4)
            row.last_entry_at = last_entry_at
            row.updated_at = now
        created += 1
    # Remove stale rows no longer present in current grouped result.
    stale_ids = [r.id for k, r in existing_map.items() if k not in touched_keys]
    if stale_ids:
        db.query(StrategyFeedbackStatDB).filter(StrategyFeedbackStatDB.id.in_(stale_ids)).delete(synchronize_session=False)
    tradability_samples = [row for row in rows if row.risk_flags_json]
    poor_tradability = sum(1 for row in tradability_samples if _has_poor_tradability_risk(row.risk_flags_json))
    high_impact = sum(1 for row in tradability_samples if _has_high_impact_risk(row.risk_flags_json))
    return {
        "strategies": created,
        "users": len(target_user_ids),
        "tradability": {
            "sample_count": len(tradability_samples),
            "poor_tradability_rate_pct": round((poor_tradability / max(1, len(tradability_samples))) * 100.0, 2)
            if tradability_samples
            else None,
            "high_impact_risk_rate_pct": round((high_impact / max(1, len(tradability_samples))) * 100.0, 2)
            if tradability_samples
            else None,
        },
    }


def refresh_feedback_pipeline(
    db: Session,
    *,
    user_id: str | None = None,
    eval_limit: int = 80,
    retries: int = 3,
) -> dict[str, Any]:
    """Run evaluate+refresh atomically (per-process) and retry transient SQLite locks."""
    attempts = max(1, int(retries))
    last_exc: Exception | None = None
    for i in range(attempts):
        with _STRATEGY_FEEDBACK_REFRESH_LOCK:
            try:
                evaluated = evaluate_pending_feedback(db, user_id=user_id, limit=eval_limit)
                refreshed = refresh_strategy_feedback_stats(db, user_id=user_id)
                return {"evaluated": evaluated, "refreshed": refreshed}
            except OperationalError as exc:
                msg = str(exc).lower()
                if "database is locked" not in msg or i >= attempts - 1:
                    raise
                last_exc = exc
                # Ensure failed write txn is released before retrying.
                db.rollback()
        time.sleep(0.15 * (i + 1))
    if last_exc is not None:
        raise last_exc
    return {"evaluated": {"processed": 0, "evaluated": 0, "skipped": 0}, "refreshed": {"strategies": 0, "users": 0}}


def list_strategy_feedback_stats(db: Session, *, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """Scanner factor-weight statistics, **not** a T+1 accuracy signal.

    Boundary (E3): ``weight_delta`` moves the market scanner's factor weights
    (momentum / activity / near_high / sector / volume_ratio) by at most ±0.08, and
    those weights only re-rank *candidates*. Nothing here feeds the deep-analysis
    direction 判断 (the VERDICT path) — so a good ``win_rate`` in this table says
    nothing about whether the system can call the next day's direction.

    It is surfaced by name so a reader cannot mistake it for a learning loop that
    improves T+1 judgement.
    """
    rows = (
        db.query(StrategyFeedbackStatDB)
        .filter(StrategyFeedbackStatDB.user_id == user_id)
        .order_by(StrategyFeedbackStatDB.weight_delta.desc(), StrategyFeedbackStatDB.sample_count.desc())
        .limit(max(1, min(int(limit), 200)))
        .all()
    )
    return [
        {
            "strategy_key": row.strategy_key,
            "factor_bucket": row.factor_bucket,
            "sample_count": row.sample_count,
            "win_rate": row.win_rate,
            "avg_return_pct": row.avg_return_pct,
            "avg_score": row.avg_score,
            "weight_delta": row.weight_delta,
            "last_entry_at": row.last_entry_at.isoformat() if row.last_entry_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
        for row in rows
    ]


def build_learning_weight_adjustment(
    db: Session,
    *,
    user_id: str,
    base_weights: dict[str, float],
) -> tuple[dict[str, float], dict[str, Any]]:
    """Adjust ``base_weights`` using aggregated strategy-feedback statistics.

    Works for both the 3-factor legacy model (momentum/activity/near_high) and
    the new 5-factor model (adds sector/volume_ratio).  Unknown factors in
    ``base_weights`` are kept unchanged.
    """
    rows = db.query(StrategyFeedbackStatDB).filter(StrategyFeedbackStatDB.user_id == user_id).all()
    min_strat_samples = max(3, int(os.getenv("TA_STRATEGY_LEARN_MIN_SAMPLES", "5") or "5"))
    if not rows:
        return base_weights, {"applied": False, "reason": "no_strategy_feedback", "min_strategy_samples": min_strat_samples}

    # Initialise deltas for every factor present in base_weights
    factor_delta: dict[str, float] = {k: 0.0 for k in base_weights}
    samples_used = 0
    for row in rows:
        if int(row.sample_count or 0) < min_strat_samples:
            continue
        bucket = str(row.factor_bucket or "momentum")
        if bucket not in factor_delta:
            continue
        factor_delta[bucket] += float(row.weight_delta or 0.0)
        samples_used += int(row.sample_count or 0)
    if samples_used <= 0:
        return base_weights, {
            "applied": False,
            "reason": "insufficient_strategy_samples",
            "min_strategy_samples": min_strat_samples,
        }

    adjusted = {
        k: max(0.01, base_weights[k] + factor_delta.get(k, 0.0))
        for k in base_weights
    }
    total = sum(adjusted.values())
    normalized = {k: round(v / total, 4) for k, v in adjusted.items()}
    return normalized, {
        "applied": True,
        "samples_used": samples_used,
        "factor_delta": {k: round(v, 4) for k, v in factor_delta.items()},
        "min_strategy_samples": min_strat_samples,
    }


def learned_weights_for_profile(
    db: Session,
    *,
    user_id: str,
    score_profile: str | None,
) -> tuple[dict[str, float] | None, dict[str, Any]]:
    """Baseline = scanner resolve_weights(profile); optionally adjust from ``StrategyFeedbackStatDB``."""

    from api.services.market_scanner_service import resolve_weights as scan_resolve_weights

    prof = (
        str(score_profile).strip()
        if score_profile is not None and str(score_profile).strip()
        else "ashare_balanced"
    )
    base = dict(scan_resolve_weights(prof))
    adjusted, info = build_learning_weight_adjustment(db, user_id=user_id, base_weights=base)
    if not info.get("applied"):
        return None, info
    return adjusted, info


def _scan_row_to_dict(row: MarketScanResultDB) -> dict[str, Any]:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "symbol": row.symbol,
        "name": row.name,
        "rank": row.rank,
        "score": row.score,
        "reasons": list(row.reasons_json or []),
        "strategy_hits": list(row.strategy_hits_json or []),
        "risk_flags": list(row.risk_flags_json or []),
        "score_breakdown": dict(row.score_breakdown_json or {}),
        "quote": dict(row.quote_json or {}),
        "selected_for_analysis": bool(row.selected_for_analysis),
        "feedback_status": row.feedback_status,
        "feedback_horizon_days": row.feedback_horizon_days,
        "entry_price": row.entry_price,
        "exit_price": row.exit_price,
        "realized_return_pct": row.realized_return_pct,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "feedback_evaluated_at": row.feedback_evaluated_at.isoformat() if row.feedback_evaluated_at else None,
        "source_mode": row.source_mode,
        "market": row.market,
        "score_profile": row.score_profile,
    }


def _get_price_after(symbol: str, base_date: str, hold_days: int) -> float | None:
    try:
        fmt = "%Y-%m-%d"
        start_dt = datetime.strptime(base_date, fmt)
        fetch_start = (start_dt + timedelta(days=1)).strftime(fmt)
        fetch_end = (start_dt + timedelta(days=hold_days + 30)).strftime(fmt)
        csv_data = route_to_vendor("get_stock_data", symbol, fetch_start, fetch_end)
        if not csv_data:
            return None
        df = pd.read_csv(StringIO(csv_data), comment="#")
        close_cols = [c for c in df.columns if "close" in c.lower() or "收盘" in c]
        date_cols = [c for c in df.columns if "date" in c.lower() or "日期" in c or "time" in c.lower()]
        if not close_cols or not date_cols:
            return None
        df = df.sort_values(date_cols[0]).reset_index(drop=True)
        if len(df) < hold_days:
            hold_days = len(df) - 1
        if hold_days < 1:
            return None
        return float(df[close_cols[0]].iloc[hold_days - 1])
    except Exception:
        return None


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _has_poor_tradability_risk(flags: Any) -> bool:
    vals = {str(x or "").strip().lower() for x in (flags or [])}
    keys = {"illiquid_turnover", "borderline_liquidity", "volume_dry_up", "near_limit_up", "near_limit_down"}
    return bool(vals.intersection(keys))


def _has_high_impact_risk(flags: Any) -> bool:
    vals = {str(x or "").strip().lower() for x in (flags or [])}
    keys = {"hot_money_risk", "high_volatility", "near_limit_up", "near_limit_down"}
    return bool(vals.intersection(keys))
