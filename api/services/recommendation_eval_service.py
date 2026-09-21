from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from io import StringIO
from threading import Lock
from typing import Any
from uuid import uuid4

import pandas as pd
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from api.database import (
    MarketScanResultDB,
    RecommendationEvalItemDB,
    RecommendationEvalRunDB,
)
from tradingagents.dataflows.interface import route_to_vendor

_RECOMMEND_EVAL_REFRESH_LOCK = Lock()


def refresh_eval_run(
    db: Session,
    *,
    user_id: str,
    baseline_profile: str = "ashare_balanced",
    variant_profile: str = "ashare_aggressive",
    lookback_days: int = 60,
    top_k: int = 5,
    benchmark_symbol: str = "000300.SH",
    source_mode: str = "market_scan",
    market: str = "cn",
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    lookback = max(7, min(int(lookback_days), 365))
    k = max(1, min(int(top_k), 20))
    cutoff = now - timedelta(days=lookback)
    rows = (
        db.query(MarketScanResultDB)
        .filter(MarketScanResultDB.user_id == user_id)
        .filter(MarketScanResultDB.feedback_status == "evaluated")
        .filter(MarketScanResultDB.source_mode == source_mode)
        .filter(MarketScanResultDB.market == market)
        .filter(MarketScanResultDB.created_at >= cutoff)
        .filter(MarketScanResultDB.score_profile.in_([baseline_profile, variant_profile]))
        .order_by(MarketScanResultDB.created_at.desc(), MarketScanResultDB.rank.asc())
        .all()
    )

    picked: dict[str, dict[str, list[MarketScanResultDB]]] = {"baseline": {}, "variant": {}}
    for row in rows:
        grp = "baseline" if row.score_profile == baseline_profile else "variant"
        ts = row.created_at or now
        bucket = str(row.run_id or f"day:{ts.strftime('%Y-%m-%d')}")
        b = picked[grp].setdefault(bucket, [])
        if len(b) < k:
            b.append(row)

    bench_cache: dict[tuple[str, str, int], tuple[float | None, float | None, float | None]] = {}
    summaries: dict[str, dict[str, Any]] = {}
    items_by_group: dict[str, list[dict[str, Any]]] = {"baseline": [], "variant": []}
    for grp in ("baseline", "variant"):
        items: list[dict[str, Any]] = []
        for bucket, bucket_rows in picked[grp].items():
            for row in bucket_rows:
                signal_date = _signal_date(row.created_at, now)
                hold_days = max(1, int(row.feedback_horizon_days or 5))
                ret = _to_float(row.realized_return_pct)
                bench_ret, _, _ = _return_and_drawdown(
                    benchmark_symbol,
                    signal_date,
                    hold_days,
                    cache=bench_cache,
                )
                _, mdd, _ = _return_and_drawdown(
                    str(row.symbol or "").upper(),
                    signal_date,
                    hold_days,
                )
                excess = (ret - bench_ret) if (ret is not None and bench_ret is not None) else None
                hit = bool(ret is not None and ret > 0)
                items.append(
                    {
                        "bucket_key": bucket,
                        "symbol": str(row.symbol or "").upper(),
                        "rank": row.rank,
                        "score": _to_float(row.score),
                        "signal_date": signal_date,
                        "hold_days": hold_days,
                        "return_pct": ret,
                        "benchmark_return_pct": bench_ret,
                        "excess_return_pct": excess,
                        "max_drawdown_pct": mdd,
                        "hit": hit if ret is not None else None,
                        "risk_flags": list(row.risk_flags_json or []),
                    }
                )
        items_by_group[grp] = items
        returns = [float(x["return_pct"]) for x in items if x["return_pct"] is not None]
        excess = [float(x["excess_return_pct"]) for x in items if x["excess_return_pct"] is not None]
        drawdowns = [float(x["max_drawdown_pct"]) for x in items if x["max_drawdown_pct"] is not None]
        hit_count = sum(1 for x in items if x["hit"] is True)
        sample_count = len(returns)
        tradability_risk_count = sum(1 for x in items if _has_poor_tradability_risk(x.get("risk_flags")))
        high_impact_risk_count = sum(1 for x in items if _has_high_impact_risk(x.get("risk_flags")))
        summaries[grp] = {
            "sample_count": sample_count,
            "hit_at_k_pct": _round(hit_count / sample_count * 100.0) if sample_count else None,
            "avg_return_pct": _round(_avg(returns)),
            "avg_excess_return_pct": _round(_avg(excess)),
            "max_drawdown_p95_pct": _round(_percentile(drawdowns, 95)),
            "poor_tradability_rate_pct": _round(tradability_risk_count / max(1, len(items)) * 100.0) if items else None,
            "high_impact_risk_rate_pct": _round(high_impact_risk_count / max(1, len(items)) * 100.0) if items else None,
        }

    gate = _build_gate(summaries.get("baseline") or {}, summaries.get("variant") or {})
    summary_json = {
        "baseline_profile": baseline_profile,
        "variant_profile": variant_profile,
        "lookback_days": lookback,
        "top_k": k,
        "benchmark_symbol": benchmark_symbol,
        "groups": summaries,
    }

    run_id = uuid4().hex
    attempts = 3
    last_exc: Exception | None = None
    for i in range(attempts):
        with _RECOMMEND_EVAL_REFRESH_LOCK:
            try:
                run = RecommendationEvalRunDB(
                    id=run_id,
                    user_id=user_id,
                    status="completed",
                    market=market,
                    source_mode=source_mode,
                    baseline_profile=baseline_profile,
                    variant_profile=variant_profile,
                    lookback_days=lookback,
                    top_k=k,
                    benchmark_symbol=benchmark_symbol,
                    summary_json=summary_json,
                    gate_json=gate,
                    created_at=now,
                    evaluated_at=now,
                    updated_at=now,
                )
                db.add(run)
                for grp in ("baseline", "variant"):
                    for item in items_by_group[grp]:
                        db.add(
                            RecommendationEvalItemDB(
                                id=uuid4().hex,
                                run_id=run_id,
                                user_id=user_id,
                                group_tag=grp,
                                bucket_key=str(item["bucket_key"]),
                                symbol=str(item["symbol"]),
                                rank=item["rank"],
                                score=item["score"],
                                signal_date=item["signal_date"],
                                hold_days=item["hold_days"],
                                return_pct=item["return_pct"],
                                benchmark_return_pct=item["benchmark_return_pct"],
                                excess_return_pct=item["excess_return_pct"],
                                max_drawdown_pct=item["max_drawdown_pct"],
                                hit=item["hit"],
                                created_at=now,
                            )
                        )
                db.flush()
                return {
                    "run_id": run_id,
                    "status": "completed",
                    "summary": summary_json,
                    "gate": gate,
                }
            except OperationalError as exc:
                if "database is locked" not in str(exc).lower() or i >= attempts - 1:
                    raise
                last_exc = exc
                db.rollback()
        time.sleep(0.15 * (i + 1))
    if last_exc is not None:
        raise last_exc
    return {"run_id": run_id, "status": "failed", "summary": summary_json, "gate": gate}


def get_latest_eval_run(db: Session, *, user_id: str) -> dict[str, Any] | None:
    row = (
        db.query(RecommendationEvalRunDB)
        .filter(RecommendationEvalRunDB.user_id == user_id)
        .order_by(RecommendationEvalRunDB.created_at.desc())
        .first()
    )
    if not row:
        return None
    return {
        "run_id": row.id,
        "status": row.status,
        "market": row.market,
        "source_mode": row.source_mode,
        "baseline_profile": row.baseline_profile,
        "variant_profile": row.variant_profile,
        "lookback_days": row.lookback_days,
        "top_k": row.top_k,
        "benchmark_symbol": row.benchmark_symbol,
        "summary": dict(row.summary_json or {}),
        "gate": dict(row.gate_json or {}),
        "error": row.error,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "evaluated_at": row.evaluated_at.isoformat() if row.evaluated_at else None,
    }


def _signal_date(created_at: datetime | None, now: datetime) -> str:
    ts = created_at or now
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _return_and_drawdown(
    symbol: str,
    signal_date: str,
    hold_days: int,
    *,
    cache: dict[tuple[str, str, int], tuple[float | None, float | None, float | None]] | None = None,
) -> tuple[float | None, float | None, float | None]:
    key = (str(symbol).upper(), signal_date, int(hold_days))
    if cache is not None and key in cache:
        return cache[key]
    out = _compute_return_and_drawdown(symbol, signal_date, hold_days)
    if cache is not None:
        cache[key] = out
    return out


def _compute_return_and_drawdown(
    symbol: str,
    signal_date: str,
    hold_days: int,
) -> tuple[float | None, float | None, float | None]:
    try:
        start_dt = datetime.strptime(signal_date, "%Y-%m-%d")
        fetch_start = (start_dt + timedelta(days=1)).strftime("%Y-%m-%d")
        fetch_end = (start_dt + timedelta(days=hold_days + 35)).strftime("%Y-%m-%d")
        csv_data = route_to_vendor("get_stock_data", symbol, fetch_start, fetch_end)
        if not csv_data:
            return (None, None, None)
        df = pd.read_csv(StringIO(csv_data), comment="#")
        close_cols = [c for c in df.columns if "close" in c.lower() or "收盘" in c]
        date_cols = [c for c in df.columns if "date" in c.lower() or "日期" in c or "time" in c.lower()]
        if not close_cols or not date_cols:
            return (None, None, None)
        df = df.sort_values(date_cols[0]).reset_index(drop=True)
        closes: list[float] = []
        for _, row in df.iterrows():
            v = _to_float(row.get(close_cols[0]))
            if v is not None and v > 0:
                closes.append(v)
        if len(closes) < 2:
            return (None, None, None)
        hd = min(max(1, hold_days), len(closes) - 1)
        entry = closes[0]
        exit_p = closes[hd]
        ret = (exit_p - entry) / entry * 100.0 if entry > 0 else None
        mdd = _max_drawdown_pct(closes[: hd + 1])
        return (_round(ret), _round(mdd), exit_p)
    except Exception:
        return (None, None, None)


def _max_drawdown_pct(closes: list[float]) -> float | None:
    if not closes:
        return None
    peak = closes[0]
    worst = 0.0
    for p in closes:
        if p > peak:
            peak = p
        if peak > 0:
            dd = (peak - p) / peak * 100.0
            if dd > worst:
                worst = dd
    return worst


def _build_gate(baseline: dict[str, Any], variant: dict[str, Any]) -> dict[str, Any]:
    min_samples = max(30, int(os.getenv("TA_REC_EVAL_MIN_SAMPLES", "60") or "60"))
    min_hit_diff = float(os.getenv("TA_REC_EVAL_MIN_HIT_DIFF_PCT", "1.0") or "1.0")
    min_excess_diff = float(os.getenv("TA_REC_EVAL_MIN_EXCESS_DIFF_PCT", "0.2") or "0.2")
    max_dd_diff = float(os.getenv("TA_REC_EVAL_MAX_DRAWDOWN_DIFF_PCT", "0.5") or "0.5")
    max_tradability_diff = float(os.getenv("TA_REC_EVAL_MAX_TRADABILITY_RISK_DIFF_PCT", "0.0") or "0.0")
    max_high_impact_diff = float(os.getenv("TA_REC_EVAL_MAX_HIGH_IMPACT_RISK_DIFF_PCT", "0.0") or "0.0")

    b_samples = int(baseline.get("sample_count") or 0)
    v_samples = int(variant.get("sample_count") or 0)
    hit_diff = _diff(variant.get("hit_at_k_pct"), baseline.get("hit_at_k_pct"))
    excess_diff = _diff(variant.get("avg_excess_return_pct"), baseline.get("avg_excess_return_pct"))
    dd_diff = _diff(variant.get("max_drawdown_p95_pct"), baseline.get("max_drawdown_p95_pct"))
    tradability_diff = _diff(variant.get("poor_tradability_rate_pct"), baseline.get("poor_tradability_rate_pct"))
    high_impact_diff = _diff(variant.get("high_impact_risk_rate_pct"), baseline.get("high_impact_risk_rate_pct"))

    reasons: list[str] = []
    if b_samples < min_samples or v_samples < min_samples:
        reasons.append("insufficient_samples")
    if hit_diff is None or hit_diff < min_hit_diff:
        reasons.append("hit_at_k_gap_not_met")
    if excess_diff is None or excess_diff < min_excess_diff:
        reasons.append("excess_return_gap_not_met")
    if dd_diff is None or dd_diff > max_dd_diff:
        reasons.append("drawdown_guard_failed")
    if tradability_diff is None or tradability_diff > max_tradability_diff:
        reasons.append("tradability_guard_failed")
    if high_impact_diff is None or high_impact_diff > max_high_impact_diff:
        reasons.append("high_impact_guard_failed")

    return {
        "allow_switch_default": len(reasons) == 0,
        "reasons": reasons,
        "thresholds": {
            "min_samples": min_samples,
            "min_hit_diff_pct": min_hit_diff,
            "min_excess_diff_pct": min_excess_diff,
            "max_drawdown_diff_pct": max_dd_diff,
            "max_tradability_risk_diff_pct": max_tradability_diff,
            "max_high_impact_risk_diff_pct": max_high_impact_diff,
        },
        "diff": {
            "hit_at_k_pct": _round(hit_diff),
            "avg_excess_return_pct": _round(excess_diff),
            "max_drawdown_p95_pct": _round(dd_diff),
            "poor_tradability_rate_pct": _round(tradability_diff),
            "high_impact_risk_rate_pct": _round(high_impact_diff),
        },
        "samples": {"baseline": b_samples, "variant": v_samples},
    }


def _has_poor_tradability_risk(flags: Any) -> bool:
    vals = {str(x or "").strip().lower() for x in (flags or [])}
    keys = {"illiquid_turnover", "borderline_liquidity", "volume_dry_up", "near_limit_up", "near_limit_down"}
    return bool(vals.intersection(keys))


def _has_high_impact_risk(flags: Any) -> bool:
    vals = {str(x or "").strip().lower() for x in (flags or [])}
    keys = {"hot_money_risk", "high_volatility", "near_limit_up", "near_limit_down"}
    return bool(vals.intersection(keys))


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _avg(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _round(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 4)


def _diff(a: Any, b: Any) -> float | None:
    af = _to_float(a)
    bf = _to_float(b)
    if af is None or bf is None:
        return None
    return af - bf


def _percentile(values: list[float], p: int) -> float | None:
    if not values:
        return None
    arr = sorted(values)
    if len(arr) == 1:
        return arr[0]
    idx = (len(arr) - 1) * (p / 100.0)
    lo = int(idx)
    hi = min(lo + 1, len(arr) - 1)
    frac = idx - lo
    return arr[lo] * (1 - frac) + arr[hi] * frac

