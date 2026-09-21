"""Model Arena analytics over report T+1 outcomes."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from statistics import pstdev
from typing import Any, Optional

from sqlalchemy.orm import Session

from api.database import ReportDB, ReportT1OutcomeDB
from api.services import insights_t1_service, model_profile_service


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _resolve_date_range(days: int, start_date: str | None, end_date: str | None) -> tuple[str, str]:
    if start_date and end_date:
        return start_date, end_date
    end = datetime.now()
    start = end - timedelta(days=max(1, min(days, 400)) - 1)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def _to_float_or_none(value: Any) -> Optional[float]:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_model_info(result_data: Any) -> dict[str, Any]:
    if not isinstance(result_data, dict):
        return {}
    payload = result_data.get("model_info")
    payload = payload if isinstance(payload, dict) else result_data
    return {
        "model_profile_id": payload.get("model_profile_id"),
        "model_profile_name": payload.get("model_profile_name"),
        "llm_provider": payload.get("llm_provider"),
        "quick_think_llm": payload.get("quick_think_llm"),
        "deep_think_llm": payload.get("deep_think_llm"),
        "backend_url": payload.get("backend_url"),
    }


def _model_key(info: dict[str, Any]) -> str:
    """Aggregate by effective LLM name so that profile-based and ad-hoc runs of
    the same model are merged into one leaderboard row.

    Key hierarchy:
      1. model:{deep_think_llm}  — preferred: model name is the natural identity
      2. model:{quick_think_llm} — when only quick LLM is recorded
      3. profile:{profile_id}    — fallback when no model name is available
      4. adhoc:{provider}        — last resort
    """
    deep = str(info.get("deep_think_llm") or "").strip()
    quick = str(info.get("quick_think_llm") or "").strip()
    primary = deep or quick
    if primary:
        return f"model:{primary}"
    profile_id = str(info.get("model_profile_id") or "").strip()
    if profile_id:
        return f"profile:{profile_id}"
    provider = str(info.get("llm_provider") or "unknown").strip()
    return f"adhoc:{provider}"


def _model_label(info: dict[str, Any]) -> str:
    profile_name = str(info.get("model_profile_name") or "").strip()
    if profile_name:
        return profile_name
    deep = str(info.get("deep_think_llm") or "").strip()
    quick = str(info.get("quick_think_llm") or "").strip()
    provider = str(info.get("llm_provider") or "").strip()
    return deep or quick or provider or "unknown"


def _load_outcomes(
    db: Session,
    *,
    user_id: str,
    start_date: str,
    end_date: str,
    scope: str,
    symbol: str | None = None,
) -> list[ReportT1OutcomeDB]:
    q = (
        db.query(ReportT1OutcomeDB)
        .filter(ReportT1OutcomeDB.user_id == user_id)
        .filter(ReportT1OutcomeDB.status == "evaluated")
        .filter(ReportT1OutcomeDB.label_correct.isnot(None))
        .filter(ReportT1OutcomeDB.signal_trade_date >= start_date)
        .filter(ReportT1OutcomeDB.signal_trade_date <= end_date)
    )
    if symbol:
        q = q.filter(ReportT1OutcomeDB.symbol == symbol)
    rows = q.all()
    if scope != "portfolio":
        return rows
    return _filter_rows_to_portfolio(db, user_id=user_id, rows=rows)


def _filter_rows_to_portfolio(
    db: Session,
    *,
    user_id: str,
    rows: list[ReportT1OutcomeDB],
) -> list[ReportT1OutcomeDB]:
    """Keep only rows whose report belongs to a portfolio snapshot report."""
    report_ids = {str(getattr(row, "report_id", "")).strip() for row in rows if getattr(row, "report_id", None)}
    report_ids.discard("")
    if not report_ids:
        return []
    portfolio_ids = insights_t1_service._portfolio_snapshot_report_ids(
        db, user_id=user_id, report_ids=report_ids
    )
    if not portfolio_ids:
        return []
    return [row for row in rows if str(getattr(row, "report_id", "")).strip() in portfolio_ids]


def _load_report_model_map(
    db: Session,
    *,
    user_id: str,
    report_ids: set[str],
) -> dict[str, dict[str, Any]]:
    if not report_ids:
        return {}
    rows = (
        db.query(ReportDB.id, ReportDB.result_data, ReportDB.decision, ReportDB.direction, ReportDB.created_at)
        .filter(ReportDB.user_id == user_id)
        .filter(ReportDB.id.in_(list(report_ids)))
        .all()
    )
    out: dict[str, dict[str, Any]] = {}
    for report_id, result_data, decision, direction, created_at in rows:
        info = _extract_model_info(result_data)
        info["decision"] = decision
        info["direction"] = direction
        info["report_created_at"] = created_at.isoformat() if created_at else None
        info["result_data"] = result_data
        out[str(report_id)] = info
    return out


def _resolve_symbol_display_name(symbol: str, result_data: Any = None) -> str | None:
    return insights_t1_service._resolve_depth_report_display_name(
        str(symbol or "").strip().upper(),
        portfolio_name=None,
        result_data=result_data,
    )


def _calendar_gap_days(t0: str | None, t1: str | None) -> Optional[int]:
    """Calendar days between signal date and T+1 date (None if either missing)."""
    try:
        if not t0 or not t1:
            return None
        from datetime import datetime
        d0 = datetime.strptime(t0, "%Y-%m-%d")
        d1 = datetime.strptime(t1, "%Y-%m-%d")
        return max(0, (d1 - d0).days)
    except Exception:
        return None


def _detect_date_drift(
    *,
    signal_trade_date: Any,
    t1_trade_date: Any,
    p0: Any,
    p1: Any,
    return_t1_pct: Any,
) -> bool:
    """Detect unreliable prices: same close fetched for both P0 and P1 anchors."""
    _ = signal_trade_date
    _ = t1_trade_date
    try:
        v0 = float(p0) if p0 is not None else None
        v1 = float(p1) if p1 is not None else None
        if v0 is not None and v1 is not None and v0 > 0 and v1 > 0 and v0 == v1:
            return True
    except (TypeError, ValueError):
        pass
    return False


def build_model_leaderboard(
    db: Session,
    *,
    user_id: str,
    days: int = 90,
    start_date: str | None = None,
    end_date: str | None = None,
    scope: str = "portfolio",
    min_samples: int = 1,
) -> dict[str, Any]:
    start, end = _resolve_date_range(days, start_date, end_date)
    rows = _load_outcomes(db, user_id=user_id, start_date=start, end_date=end, scope=scope)
    report_ids = {str(getattr(row, "report_id", "")).strip() for row in rows if getattr(row, "report_id", None)}
    report_map = _load_report_model_map(db, user_id=user_id, report_ids=report_ids)

    buckets: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "samples": 0,
            "correct": 0,
            "returns": [],
            "wins": 0,
            "symbols": set(),
            "dates": set(),
            "meta": {},
        }
    )
    for row in rows:
        rid = str(getattr(row, "report_id", "") or "").strip()
        info = report_map.get(rid) or {}
        key = _model_key(info)
        p0_v = _to_float_or_none(getattr(row, "p0", None))
        p1_v = _to_float_or_none(getattr(row, "p1", None))
        ret_v = _to_float_or_none(getattr(row, "return_t1_pct", None))
        # Skip date-drift records — prices are unreliable and would corrupt stats.
        if _detect_date_drift(
            signal_trade_date=row.signal_trade_date,
            t1_trade_date=row.t1_trade_date,
            p0=p0_v,
            p1=p1_v,
            return_t1_pct=ret_v,
        ):
            continue
        item = buckets[key]
        item["samples"] += 1
        item["correct"] += 1 if bool(row.label_correct) else 0
        if ret_v is not None:
            item["returns"].append(ret_v)
            if ret_v > 0:
                item["wins"] += 1
        symbol = str(getattr(row, "symbol", "") or "").strip().upper()
        if symbol:
            item["symbols"].add(symbol)
        date = str(getattr(row, "signal_trade_date", "") or "").strip()
        if date:
            item["dates"].add(date)
        # Prefer meta that carries a model_profile_id so the promote/rollback
        # buttons remain functional even when merging ad-hoc + profile rows.
        if info.get("model_profile_id") or not item["meta"].get("model_profile_id"):
            item["meta"] = info

    leaderboard: list[dict[str, Any]] = []
    for key, item in buckets.items():
        samples = int(item["samples"])
        if samples < max(1, int(min_samples)):
            continue
        # Exclude records where the model could not be identified — they carry no
        # useful signal for model comparison and would pollute the leaderboard.
        if key in ("adhoc:unknown", "model:unknown", "adhoc:"):
            continue
        returns = [float(x) for x in item["returns"]]
        avg_return = round(sum(returns) / len(returns), 4) if returns else None
        acc = round(item["correct"] / samples * 100.0, 2) if samples else 0.0
        win_rate = round(item["wins"] / len(returns) * 100.0, 2) if returns else None
        volatility = round(float(pstdev(returns)), 4) if len(returns) >= 2 else 0.0
        return_score = (avg_return or 0.0) * 5.0
        score = round(acc * 0.65 + return_score * 0.30 - volatility * 3.0 * 0.05, 2)
        meta = item["meta"]
        leaderboard.append(
            {
                "model_key": key,
                "model_profile_id": meta.get("model_profile_id"),
                "model_profile_name": meta.get("model_profile_name"),
                "model_display_name": _model_label(meta),
                "llm_provider": meta.get("llm_provider"),
                "quick_think_llm": meta.get("quick_think_llm"),
                "deep_think_llm": meta.get("deep_think_llm"),
                "backend_url": meta.get("backend_url"),
                "sample_count": samples,
                "symbol_count": len(item["symbols"]),
                "date_count": len(item["dates"]),
                "accuracy_pct": acc,
                "win_rate_pct": win_rate,
                "avg_return_t1_pct": avg_return,
                "return_volatility": volatility,
                "composite_score": score,
            }
        )

    leaderboard.sort(
        key=lambda x: (
            float(x.get("composite_score") or 0.0),
            float(x.get("accuracy_pct") or 0.0),
            int(x.get("sample_count") or 0),
        ),
        reverse=True,
    )
    for idx, row in enumerate(leaderboard, start=1):
        row["rank"] = idx
    return {
        "window_start": start,
        "window_end": end,
        "scope": scope,
        "total_samples": len(rows),
        "leaderboard": leaderboard,
        "generated_at": _today_str(),
    }


def build_symbol_compare(
    db: Session,
    *,
    user_id: str,
    symbol: str,
    days: int = 30,
    start_date: str | None = None,
    end_date: str | None = None,
    scope: str = "all",
) -> dict[str, Any]:
    sym = str(symbol or "").strip().upper()
    if not sym:
        return {"symbol": "", "rows": [], "by_date": {}, "model_summaries": []}
    start, end = _resolve_date_range(days, start_date, end_date)
    rows = _load_outcomes(
        db,
        user_id=user_id,
        start_date=start,
        end_date=end,
        scope=scope,
        symbol=sym,
    )
    report_ids = {str(getattr(row, "report_id", "")).strip() for row in rows if getattr(row, "report_id", None)}
    report_map = _load_report_model_map(db, user_id=user_id, report_ids=report_ids)

    flat_rows: list[dict[str, Any]] = []
    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_model: dict[str, dict[str, Any]] = defaultdict(lambda: {"samples": 0, "correct": 0, "returns": [], "meta": {}})
    for row in rows:
        rid = str(getattr(row, "report_id", "") or "").strip()
        info = report_map.get(rid) or {}
        key = _model_key(info)
        item = {
            "signal_trade_date": row.signal_trade_date,
            "t1_trade_date": row.t1_trade_date,
            "report_id": rid or None,
            "symbol": sym,
            "return_t1_pct": _to_float_or_none(getattr(row, "return_t1_pct", None)),
            "label_correct": bool(row.label_correct),
            "direction_bucket": row.direction_bucket,
            "decision": info.get("decision"),
            "direction": info.get("direction"),
            "model_key": key,
            "model_profile_id": info.get("model_profile_id"),
            "model_profile_name": info.get("model_profile_name"),
            "model_display_name": _model_label(info),
            "llm_provider": info.get("llm_provider"),
            "quick_think_llm": info.get("quick_think_llm"),
            "deep_think_llm": info.get("deep_think_llm"),
        }
        flat_rows.append(item)
        by_date[str(row.signal_trade_date)].append(item)
        agg = by_model[key]
        agg["samples"] += 1
        agg["correct"] += 1 if bool(row.label_correct) else 0
        if item["return_t1_pct"] is not None:
            agg["returns"].append(float(item["return_t1_pct"]))
        agg["meta"] = info

    for d in list(by_date.keys()):
        by_date[d].sort(
            key=lambda x: (
                x.get("model_profile_name") or "",
                x.get("deep_think_llm") or "",
                x.get("quick_think_llm") or "",
            )
        )

    model_summaries: list[dict[str, Any]] = []
    for key, agg in by_model.items():
        samples = int(agg["samples"])
        returns = [float(x) for x in agg["returns"]]
        avg_return = round(sum(returns) / len(returns), 4) if returns else None
        acc = round(agg["correct"] / samples * 100.0, 2) if samples else None
        meta = agg["meta"]
        model_summaries.append(
            {
                "model_key": key,
                "model_profile_id": meta.get("model_profile_id"),
                "model_profile_name": meta.get("model_profile_name"),
                "model_display_name": _model_label(meta),
                "llm_provider": meta.get("llm_provider"),
                "quick_think_llm": meta.get("quick_think_llm"),
                "deep_think_llm": meta.get("deep_think_llm"),
                "sample_count": samples,
                "accuracy_pct": acc,
                "avg_return_t1_pct": avg_return,
            }
        )
    model_summaries.sort(key=lambda x: (float(x.get("accuracy_pct") or 0), int(x.get("sample_count") or 0)), reverse=True)
    return {
        "symbol": sym,
        "window_start": start,
        "window_end": end,
        "scope": scope,
        "rows": flat_rows,
        "by_date": dict(by_date),
        "model_summaries": model_summaries,
    }


def build_model_t1_detail(
    db: Session,
    *,
    user_id: str,
    model_profile_id: str | None = None,
    model_key: str | None = None,
    days: int = 90,
    start_date: str | None = None,
    end_date: str | None = None,
    scope: str = "portfolio",
    limit: int = 300,
) -> dict[str, Any]:
    target_profile_id = str(model_profile_id or "").strip()
    target_model_key = str(model_key or "").strip()
    if not target_profile_id and not target_model_key:
        return {
            "window_start": "",
            "window_end": "",
            "scope": scope,
            "total_samples": 0,
            "accuracy_pct": None,
            "rows": [],
        }

    start, end = _resolve_date_range(days, start_date, end_date)
    outcomes = _load_outcomes(db, user_id=user_id, start_date=start, end_date=end, scope=scope)
    report_ids = {str(getattr(row, "report_id", "")).strip() for row in outcomes if getattr(row, "report_id", None)}
    report_map = _load_report_model_map(db, user_id=user_id, report_ids=report_ids)

    # When filtering by profile_id, also derive the canonical model_key so that
    # ad-hoc records with the same underlying model (but no saved profile) are
    # included in the detail view — especially after the leaderboard merges them.
    effective_model_key = target_model_key
    if target_profile_id and not effective_model_key:
        for row in outcomes:
            rid = str(getattr(row, "report_id", "") or "").strip()
            info = report_map.get(rid) or {}
            if str(info.get("model_profile_id") or "").strip() == target_profile_id:
                effective_model_key = _model_key(info)
                break

    rows: list[dict[str, Any]] = []
    for row in outcomes:
        rid = str(getattr(row, "report_id", "") or "").strip()
        info = report_map.get(rid) or {}
        current_key = _model_key(info)
        if current_key in _UNKNOWN_MODEL_KEYS:
            continue
        # Match by model_key (covers merged profile + adhoc rows), or fall back
        # to exact profile_id match if no model_key could be derived.
        if effective_model_key:
            if current_key != effective_model_key:
                continue
        elif target_profile_id:
            if str(info.get("model_profile_id") or "").strip() != target_profile_id:
                continue
        else:
            continue
        p0_val = _to_float_or_none(getattr(row, "p0", None))
        p1_val = _to_float_or_none(getattr(row, "p1", None))
        ret_val = _to_float_or_none(getattr(row, "return_t1_pct", None))
        gap = _calendar_gap_days(
            str(row.signal_trade_date or "").strip(),
            str(row.t1_trade_date or "").strip(),
        )
        drift = _detect_date_drift(
            signal_trade_date=row.signal_trade_date,
            t1_trade_date=row.t1_trade_date,
            p0=p0_val,
            p1=p1_val,
            return_t1_pct=ret_val,
        )
        rows.append(
            {
                "signal_trade_date": row.signal_trade_date,
                "t1_trade_date": row.t1_trade_date,
                "symbol": row.symbol,
                "name": _resolve_symbol_display_name(str(row.symbol or ""), info.get("result_data")),
                "report_id": rid or None,
                "report_created_at": info.get("report_created_at"),
                "decision": info.get("decision"),
                "direction": info.get("direction"),
                "direction_bucket": row.direction_bucket,
                "label_correct": bool(row.label_correct),
                "return_t1_pct": ret_val,
                "p0": p0_val,
                "p1": p1_val,
                "calendar_gap_days": gap,
                "date_drift_flag": drift,
                "model_key": current_key,
                "model_profile_id": info.get("model_profile_id"),
                "model_profile_name": info.get("model_profile_name"),
                "model_display_name": _model_label(info),
                "llm_provider": info.get("llm_provider"),
                "quick_think_llm": info.get("quick_think_llm"),
                "deep_think_llm": info.get("deep_think_llm"),
            }
        )

    rows.sort(
        key=lambda x: (
            str(x.get("signal_trade_date") or ""),
            str(x.get("symbol") or ""),
            str(x.get("report_id") or ""),
        ),
        reverse=True,
    )

    drift_rows = [item for item in rows if item.get("date_drift_flag")]
    clean_rows = [item for item in rows if not item.get("date_drift_flag")]
    if limit > 0:
        clean_rows = clean_rows[: max(1, min(limit, 1000))]
    drift_warning_count = len(drift_rows)
    total = len(clean_rows)
    correct = sum(1 for item in clean_rows if bool(item.get("label_correct")))
    accuracy = round(correct / total * 100.0, 2) if total else None
    return {
        "window_start": start,
        "window_end": end,
        "scope": scope,
        "model_profile_id": target_profile_id or None,
        "model_key": target_model_key or (clean_rows[0].get("model_key") if clean_rows else (drift_rows[0].get("model_key") if drift_rows else None)),
        "total_samples": total,
        "accuracy_pct": accuracy,
        "drift_warning_count": drift_warning_count,
        "rows": clean_rows,
        "drift_rows": drift_rows,
    }


_UNKNOWN_MODEL_KEYS = frozenset({"adhoc:unknown", "model:unknown", "adhoc:"})


def _consensus_label(direction: str, return_t1_pct: float | None) -> bool | None:
    if return_t1_pct is None:
        return None
    if direction == "bullish":
        return return_t1_pct > 0
    if direction == "bearish":
        return return_t1_pct < 0
    return None


def _consensus_events_from_rows(
    *,
    outcomes: list[ReportT1OutcomeDB],
    report_map: dict[str, dict[str, Any]],
    min_models: int = 2,
    signal_date: str | None = None,
) -> list[dict[str, Any]]:
    """Pure grouping step: same symbol + signal day, >= min_models distinct LLMs, same direction.

    Split out from _collect_multi_model_consensus_events() so a single load of
    outcomes/report metadata can feed both the portfolio and all-report scopes.
    """
    if signal_date:
        outcomes = [row for row in outcomes if str(row.signal_trade_date or "") == signal_date]

    by_group: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in outcomes:
        if _detect_date_drift(
            signal_trade_date=row.signal_trade_date,
            t1_trade_date=row.t1_trade_date,
            p0=_to_float_or_none(getattr(row, "p0", None)),
            p1=_to_float_or_none(getattr(row, "p1", None)),
            return_t1_pct=_to_float_or_none(getattr(row, "return_t1_pct", None)),
        ):
            continue
        bucket = str(getattr(row, "direction_bucket", "") or "").strip()
        if bucket not in ("bullish", "bearish"):
            continue
        rid = str(getattr(row, "report_id", "") or "").strip()
        info = report_map.get(rid) or {}
        model_key = _model_key(info)
        if model_key in _UNKNOWN_MODEL_KEYS:
            continue
        sym = str(getattr(row, "symbol", "") or "").strip().upper()
        day = str(getattr(row, "signal_trade_date", "") or "").strip()
        if not sym or not day:
            continue
        created = str(info.get("report_created_at") or "")
        group_key = (sym, day)
        existing = by_group[group_key].get(model_key)
        if existing is None or created >= str(existing.get("report_created_at") or ""):
            by_group[group_key][model_key] = {
                "row": row,
                "info": info,
                "model_key": model_key,
                "direction_bucket": bucket,
                "report_created_at": created,
                "report_id": rid,
            }

    min_n = max(2, int(min_models))
    events: list[dict[str, Any]] = []
    for (sym, day), models_map in by_group.items():
        bullish = [entry for entry in models_map.values() if entry["direction_bucket"] == "bullish"]
        bearish = [entry for entry in models_map.values() if entry["direction_bucket"] == "bearish"]
        if len(bullish) >= min_n and not bearish:
            events.append(_build_consensus_event(sym, day, "bullish", bullish))
        if len(bearish) >= min_n and not bullish:
            events.append(_build_consensus_event(sym, day, "bearish", bearish))
    events.sort(key=lambda item: (str(item.get("signal_trade_date") or ""), str(item.get("symbol") or "")))
    return events


def _collect_multi_model_consensus_events(
    db: Session,
    *,
    user_id: str,
    start_date: str,
    end_date: str,
    scope: str,
    min_models: int = 2,
    signal_date: str | None = None,
) -> list[dict[str, Any]]:
    """Load the rows a scope needs, then group them into consensus events."""
    outcomes = _load_outcomes(
        db,
        user_id=user_id,
        start_date=start_date,
        end_date=end_date,
        scope=scope,
    )
    report_ids = {str(getattr(row, "report_id", "")).strip() for row in outcomes if getattr(row, "report_id", None)}
    report_ids.discard("")
    report_map = _load_report_model_map(db, user_id=user_id, report_ids=report_ids)
    return _consensus_events_from_rows(
        outcomes=outcomes,
        report_map=report_map,
        min_models=min_models,
        signal_date=signal_date,
    )


def _build_consensus_event(
    symbol: str,
    signal_trade_date: str,
    direction: str,
    model_entries: list[dict[str, Any]],
) -> dict[str, Any]:
    ref_row = model_entries[0]["row"]
    ret = _to_float_or_none(getattr(ref_row, "return_t1_pct", None))
    label = _consensus_label(direction, ret)
    models: list[dict[str, Any]] = []
    for entry in model_entries:
        info = entry["info"]
        models.append(
            {
                "model_key": entry["model_key"],
                "model_profile_id": info.get("model_profile_id"),
                "model_profile_name": info.get("model_profile_name"),
                "model_display_name": _model_label(info),
                "llm_provider": info.get("llm_provider"),
                "quick_think_llm": info.get("quick_think_llm"),
                "deep_think_llm": info.get("deep_think_llm"),
                "report_id": entry["report_id"],
                "direction_bucket": entry["direction_bucket"],
                "report_created_at": entry.get("report_created_at"),
            }
        )
    models.sort(key=lambda item: str(item.get("model_display_name") or ""))
    result_data = next(
        ((entry.get("info") or {}).get("result_data") for entry in model_entries if (entry.get("info") or {}).get("result_data")),
        None,
    )
    return {
        "symbol": symbol,
        "name": _resolve_symbol_display_name(symbol, result_data),
        "signal_trade_date": signal_trade_date,
        "t1_trade_date": getattr(ref_row, "t1_trade_date", None),
        "consensus_direction": direction,
        "model_count": len(model_entries),
        "models": models,
        "return_t1_pct": ret,
        "label_correct": label,
        "p0": _to_float_or_none(getattr(ref_row, "p0", None)),
        "p1": _to_float_or_none(getattr(ref_row, "p1", None)),
    }


def _aggregate_consensus_trend(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_day: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: {"bullish": [], "bearish": []})
    for event in events:
        day = str(event.get("signal_trade_date") or "")
        direction = str(event.get("consensus_direction") or "")
        if day and direction in ("bullish", "bearish"):
            by_day[day][direction].append(event)
    out: list[dict[str, Any]] = []
    for day in sorted(by_day.keys()):
        bulls = by_day[day]["bullish"]
        bears = by_day[day]["bearish"]
        bull_labels = [bool(item.get("label_correct")) for item in bulls if item.get("label_correct") is not None]
        bear_labels = [bool(item.get("label_correct")) for item in bears if item.get("label_correct") is not None]
        t1d = None
        if bulls:
            t1d = bulls[0].get("t1_trade_date")
        elif bears:
            t1d = bears[0].get("t1_trade_date")
        out.append(
            {
                "date": day,
                "t1_trade_date": t1d,
                "unanimous_bullish_count": len(bulls),
                "unanimous_bullish_accuracy_pct": round(sum(bull_labels) / len(bull_labels) * 100.0, 2)
                if bull_labels
                else None,
                "unanimous_bearish_count": len(bears),
                "unanimous_bearish_accuracy_pct": round(sum(bear_labels) / len(bear_labels) * 100.0, 2)
                if bear_labels
                else None,
            }
        )
    return out


def build_multi_model_consensus_t1_trend(
    db: Session,
    *,
    user_id: str,
    days: int = 90,
    start_date: str | None = None,
    end_date: str | None = None,
    scope: str = "portfolio",
    min_models: int = 2,
) -> dict[str, Any]:
    start, end = _resolve_date_range(days, start_date, end_date)
    events = _collect_multi_model_consensus_events(
        db,
        user_id=user_id,
        start_date=start,
        end_date=end,
        scope=scope,
        min_models=min_models,
    )
    return {
        "window_start": start,
        "window_end": end,
        "scope": scope,
        "min_models": max(2, int(min_models)),
        "series": _aggregate_consensus_trend(events),
    }


def build_multi_model_consensus_t1_trends(
    db: Session,
    *,
    user_id: str,
    days: int = 90,
    start_date: str | None = None,
    end_date: str | None = None,
    min_models: int = 2,
) -> dict[str, Any]:
    """Build the portfolio AND all-report consensus trends in a single pass.

    Loading the T+1 outcomes and the per-report metadata is the expensive part, so
    it happens once here and both scopes reuse the same rows. The trend endpoint
    used to call build_multi_model_consensus_t1_trend() twice (once per scope),
    paying for every query and every name resolution twice over.
    """
    start, end = _resolve_date_range(days, start_date, end_date)
    all_rows = _load_outcomes(db, user_id=user_id, start_date=start, end_date=end, scope="all")
    report_ids = {str(getattr(row, "report_id", "")).strip() for row in all_rows if getattr(row, "report_id", None)}
    report_ids.discard("")
    report_map = _load_report_model_map(db, user_id=user_id, report_ids=report_ids)
    portfolio_rows = _filter_rows_to_portfolio(db, user_id=user_id, rows=all_rows)
    min_n = max(2, int(min_models))
    return {
        "window_start": start,
        "window_end": end,
        "min_models": min_n,
        "series": _aggregate_consensus_trend(
            _consensus_events_from_rows(
                outcomes=portfolio_rows, report_map=report_map, min_models=min_n
            )
        ),
        "series_all": _aggregate_consensus_trend(
            _consensus_events_from_rows(
                outcomes=all_rows, report_map=report_map, min_models=min_n
            )
        ),
    }


def build_multi_model_consensus_t1_detail(
    db: Session,
    *,
    user_id: str,
    date: str,
    scope: str = "portfolio",
    direction: str | None = None,
    min_models: int = 2,
) -> dict[str, Any]:
    day = str(date or "").strip()
    if not day:
        return {"date": "", "scope": scope, "items": []}
    events = _collect_multi_model_consensus_events(
        db,
        user_id=user_id,
        start_date=day,
        end_date=day,
        scope=scope,
        min_models=min_models,
        signal_date=day,
    )
    if direction in ("bullish", "bearish"):
        events = [item for item in events if item.get("consensus_direction") == direction]
    return {"date": day, "scope": scope, "direction": direction, "items": events}


def build_model_accuracy_trend(
    db: Session,
    *,
    user_id: str,
    days: int = 90,
    start_date: str | None = None,
    end_date: str | None = None,
    scope: str = "portfolio",
    top_n: int = 5,
    min_samples: int = 10,
    model_keys: list[str] | None = None,
) -> dict[str, Any]:
    start, end = _resolve_date_range(days, start_date, end_date)
    outcomes = _load_outcomes(db, user_id=user_id, start_date=start, end_date=end, scope=scope)
    report_ids = {str(getattr(row, "report_id", "")).strip() for row in outcomes if getattr(row, "report_id", None)}
    report_map = _load_report_model_map(db, user_id=user_id, report_ids=report_ids)

    by_model_totals: dict[str, int] = defaultdict(int)
    by_model_meta: dict[str, dict[str, Any]] = {}
    by_model_day_vals: dict[str, dict[str, list[bool]]] = defaultdict(lambda: defaultdict(list))
    for row in outcomes:
        rid = str(getattr(row, "report_id", "") or "").strip()
        info = report_map.get(rid) or {}
        # Skip drift records — corrupt prices make accuracy unreliable.
        if _detect_date_drift(
            signal_trade_date=row.signal_trade_date,
            t1_trade_date=row.t1_trade_date,
            p0=_to_float_or_none(getattr(row, "p0", None)),
            p1=_to_float_or_none(getattr(row, "p1", None)),
            return_t1_pct=_to_float_or_none(getattr(row, "return_t1_pct", None)),
        ):
            continue
        model_key = _model_key(info)
        by_model_totals[model_key] += 1
        # Prefer meta with profile_id so promote/rollback remain available.
        if info.get("model_profile_id") or model_key not in by_model_meta:
            by_model_meta[model_key] = info
        day = str(getattr(row, "signal_trade_date", "") or "").strip()
        if day:
            by_model_day_vals[model_key][day].append(bool(row.label_correct))

    _unknown_keys = {"adhoc:unknown", "model:unknown", "adhoc:"}
    filter_keys = {str(k).strip() for k in (model_keys or []) if str(k).strip()}
    if filter_keys:
        candidate_keys = [
            k for k in filter_keys
            if k in by_model_totals and k not in _unknown_keys
        ]
    else:
        candidate_keys = [
            k for k, c in sorted(by_model_totals.items(), key=lambda x: x[1], reverse=True)
            if c >= max(1, int(min_samples)) and k not in _unknown_keys
        ][: max(1, min(int(top_n), 12))]

    all_days = sorted({d for key in candidate_keys for d in by_model_day_vals.get(key, {}).keys()})
    series: list[dict[str, Any]] = []
    for key in candidate_keys:
        meta = by_model_meta.get(key) or {}
        points: list[dict[str, Any]] = []
        for d in all_days:
            vals = by_model_day_vals.get(key, {}).get(d) or []
            if not vals:
                points.append({"date": d, "accuracy_pct": None, "sample_count": 0})
                continue
            acc = round(sum(1 for v in vals if v) / len(vals) * 100.0, 2)
            points.append({"date": d, "accuracy_pct": acc, "sample_count": len(vals)})
        series.append(
            {
                "model_key": key,
                "model_profile_id": meta.get("model_profile_id"),
                "model_profile_name": meta.get("model_profile_name"),
                "model_display_name": _model_label(meta),
                "quick_think_llm": meta.get("quick_think_llm"),
                "deep_think_llm": meta.get("deep_think_llm"),
                "total_samples": by_model_totals.get(key, 0),
                "points": points,
            }
        )

    return {
        "window_start": start,
        "window_end": end,
        "scope": scope,
        "days": all_days,
        "series": series,
    }


def evaluate_promotion_gate(
    db: Session,
    *,
    user_id: str,
    target_profile_id: str,
    lookback_days: int = 90,
    scope: str = "portfolio",
    min_samples: int = 20,
    min_accuracy_improvement_pct: float = 0.0,
    max_return_drop_pct: float = 0.5,
) -> dict[str, Any]:
    board = build_model_leaderboard(
        db,
        user_id=user_id,
        days=lookback_days,
        scope=scope,
        min_samples=1,
    )
    target = next((r for r in board["leaderboard"] if r.get("model_profile_id") == target_profile_id), None)
    profiles = model_profile_service.list_model_profiles(db, user_id, include_inactive=True)
    current_default = next((p for p in profiles if bool(p.get("is_default"))), None)
    baseline = None
    if current_default and current_default.get("id") != target_profile_id:
        baseline = next((r for r in board["leaderboard"] if r.get("model_profile_id") == current_default.get("id")), None)

    reasons: list[str] = []
    passed = True
    if target is None:
        passed = False
        reasons.append("目标模型在评估窗口内没有可用 T+1 样本")
    else:
        sample_count = int(target.get("sample_count") or 0)
        if sample_count < int(min_samples):
            passed = False
            reasons.append(f"样本量不足：{sample_count} < {int(min_samples)}")
        if baseline is not None:
            acc_delta = float(target.get("accuracy_pct") or 0.0) - float(baseline.get("accuracy_pct") or 0.0)
            if acc_delta < float(min_accuracy_improvement_pct):
                passed = False
                reasons.append(
                    f"准确率提升不足：{round(acc_delta, 2)}% < {float(min_accuracy_improvement_pct)}%"
                )
            target_ret = _to_float_or_none(target.get("avg_return_t1_pct"))
            base_ret = _to_float_or_none(baseline.get("avg_return_t1_pct"))
            if target_ret is not None and base_ret is not None:
                ret_delta = target_ret - base_ret
                if ret_delta < -abs(float(max_return_drop_pct)):
                    passed = False
                    reasons.append(
                        f"收益回撤超阈值：{round(ret_delta, 4)} < -{abs(float(max_return_drop_pct))}"
                    )

    return {
        "passed": passed,
        "reasons": reasons,
        "target_profile_id": target_profile_id,
        "target": target,
        "current_default_profile_id": current_default.get("id") if current_default else None,
        "baseline": baseline,
        "gate": {
            "lookback_days": lookback_days,
            "scope": scope,
            "min_samples": int(min_samples),
            "min_accuracy_improvement_pct": float(min_accuracy_improvement_pct),
            "max_return_drop_pct": float(max_return_drop_pct),
        },
    }


def detect_model_drift(
    db: Session,
    *,
    user_id: str,
    scope: str = "portfolio",
    lookback_days: int = 120,
    recent_days: int = 7,
    baseline_days: int = 30,
    min_recent_samples: int = 8,
    alert_drop_pct: float = 8.0,
) -> dict[str, Any]:
    board = build_model_leaderboard(
        db,
        user_id=user_id,
        days=lookback_days,
        scope=scope,
        min_samples=1,
    )
    start, end = board["window_start"], board["window_end"]
    outcomes = _load_outcomes(db, user_id=user_id, start_date=start, end_date=end, scope=scope)
    report_ids = {str(getattr(row, "report_id", "")).strip() for row in outcomes if getattr(row, "report_id", None)}
    report_map = _load_report_model_map(db, user_id=user_id, report_ids=report_ids)

    by_model_by_day: dict[str, dict[str, list[bool]]] = defaultdict(lambda: defaultdict(list))
    for row in outcomes:
        rid = str(getattr(row, "report_id", "") or "").strip()
        info = report_map.get(rid) or {}
        if _detect_date_drift(
            signal_trade_date=row.signal_trade_date,
            t1_trade_date=row.t1_trade_date,
            p0=_to_float_or_none(getattr(row, "p0", None)),
            p1=_to_float_or_none(getattr(row, "p1", None)),
            return_t1_pct=_to_float_or_none(getattr(row, "return_t1_pct", None)),
        ):
            continue
        key = _model_key(info)
        day = str(getattr(row, "signal_trade_date", "") or "").strip()
        if not day:
            continue
        by_model_by_day[key][day].append(bool(row.label_correct))

    alerts: list[dict[str, Any]] = []
    model_meta = {item["model_key"]: item for item in board["leaderboard"]}
    for model_key, day_map in by_model_by_day.items():
        days_sorted = sorted(day_map.keys())
        if not days_sorted:
            continue
        recent_slice = days_sorted[-max(1, recent_days):]
        baseline_slice = days_sorted[-max(1, recent_days + baseline_days):-max(1, recent_days)]
        if not baseline_slice:
            baseline_slice = days_sorted[:-max(1, recent_days)]
        recent_vals = [v for d in recent_slice for v in day_map[d]]
        baseline_vals = [v for d in baseline_slice for v in day_map[d]]
        if len(recent_vals) < max(1, min_recent_samples) or len(baseline_vals) < max(1, min_recent_samples):
            continue
        recent_acc = sum(1 for x in recent_vals if x) / len(recent_vals) * 100.0
        baseline_acc = sum(1 for x in baseline_vals if x) / len(baseline_vals) * 100.0
        drop = baseline_acc - recent_acc
        if drop < float(alert_drop_pct):
            continue
        meta = model_meta.get(model_key) or {}
        alerts.append(
            {
                "model_key": model_key,
                "model_profile_id": meta.get("model_profile_id"),
                "model_profile_name": meta.get("model_profile_name"),
                "model_display_name": meta.get("model_display_name"),
                "baseline_accuracy_pct": round(baseline_acc, 2),
                "recent_accuracy_pct": round(recent_acc, 2),
                "drop_pct": round(drop, 2),
                "recent_sample_count": len(recent_vals),
                "baseline_sample_count": len(baseline_vals),
                "recent_window_days": recent_days,
                "baseline_window_days": baseline_days,
            }
        )
    alerts.sort(key=lambda x: float(x.get("drop_pct") or 0.0), reverse=True)
    return {
        "scope": scope,
        "window_start": start,
        "window_end": end,
        "alerts": alerts,
    }
