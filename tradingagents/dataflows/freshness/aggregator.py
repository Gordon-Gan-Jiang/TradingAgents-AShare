from __future__ import annotations

from typing import Any

from .anchor import resolve_expected_anchor
from .contract import load_contract, load_section_map
from .types import CONFIDENCE_CAPS, OVERALL_LABELS
from .validator import build_eval_context, evaluate_collector_results

_STATUS_RANK = {
    "error": 4,
    "stale": 3,
    "warning": 2,
    "fresh": 1,
    "empty_ok": 0,
    "not_applicable": 0,
    "unknown": 0,
}


def build_freshness_pool(
    results: dict[str, Any],
    trade_date: str,
    *,
    symbol: str = "600519.SH",
    analysis_mode: str | None = None,
) -> dict[str, dict[str, Any]]:
    return evaluate_collector_results(
        results, trade_date, symbol=symbol, analysis_mode=analysis_mode
    )


def _worst_status(statuses: list[str]) -> str:
    if not statuses:
        return "fresh"
    return max(statuses, key=lambda s: _STATUS_RANK.get(s, 0))


def _aggregate_overall(pool: dict[str, dict[str, Any]]) -> str:
    contract = load_contract()
    has_critical_error = False
    has_important_error = False
    has_critical_stale = False
    has_important_stale = False
    has_warning = False

    for source_key, meta in pool.items():
        entry = contract.get(source_key) or {}
        crit = str(entry.get("criticality") or meta.get("criticality") or "informational")
        status = str(meta.get("status") or "unknown")
        if status == "error":
            if crit == "critical":
                has_critical_error = True
            elif crit == "important":
                has_important_error = True
        elif status == "stale":
            if crit == "critical":
                has_critical_stale = True
            elif crit == "important":
                has_important_stale = True
        elif status == "warning" and crit in ("critical", "important"):
            has_warning = True

    if has_critical_error or has_important_error:
        return "error"
    if has_critical_stale:
        return "stale"
    if has_important_stale or has_warning:
        return "warning"
    return "fresh"


def _blocking_sources(pool: dict[str, dict[str, Any]], overall: str) -> list[dict[str, Any]]:
    if overall == "fresh":
        return []
    out: list[dict[str, Any]] = []
    contract = load_contract()
    for source_key, meta in pool.items():
        status = str(meta.get("status") or "")
        if status not in ("error", "stale", "warning"):
            continue
        entry = contract.get(source_key) or {}
        crit = str(entry.get("criticality") or meta.get("criticality") or "informational")
        if status == "warning":
            continue
        if status in ("stale", "error") and crit not in ("critical", "important"):
            continue
        item = {"source_key": source_key, **meta}
        out.append(item)
    return out


def _fetch_errors(blocking: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "source_key": b["source_key"],
            "error_code": b.get("error_code"),
            "error_message": b.get("error_message") or b.get("lag_note"),
        }
        for b in blocking
        if b.get("status") == "error" or b.get("issue_type") == "fetch_error"
    ]


def _section_impacts(pool: dict[str, dict[str, Any]]) -> dict[str, str]:
    section_map = load_section_map()
    contract = load_contract()
    impacts: dict[str, str] = {}

    all_ci_statuses: list[str] = []
    for sk, meta in pool.items():
        entry = contract.get(sk) or {}
        crit = str(entry.get("criticality") or meta.get("criticality") or "")
        if crit in ("critical", "important"):
            all_ci_statuses.append(str(meta.get("status") or "fresh"))

    for section_key, cfg in section_map.items():
        if not isinstance(cfg, dict):
            continue
        if cfg.get("inherit") == "all_critical_important":
            impacts[section_key] = _worst_status(all_ci_statuses)
            continue
        sources = cfg.get("sources") or []
        statuses = [str((pool.get(s) or {}).get("status") or "fresh") for s in sources]
        impacts[section_key] = _worst_status(statuses)
    return impacts


def _warning_sources(pool: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    contract = load_contract()
    out: list[dict[str, Any]] = []
    for source_key, meta in pool.items():
        if str(meta.get("status") or "") != "warning":
            continue
        entry = contract.get(source_key) or {}
        crit = str(entry.get("criticality") or meta.get("criticality") or "informational")
        if crit not in ("critical", "important"):
            continue
        out.append({"source_key": source_key, **meta})
    return out


def build_freshness_summary(
    pool: dict[str, dict[str, Any]],
    trade_date: str,
    *,
    symbol: str = "600519.SH",
    analysis_mode: str | None = None,
) -> dict[str, Any]:
    eval_ctx = build_eval_context(trade_date, symbol=symbol, analysis_mode=analysis_mode)
    overall = _aggregate_overall(pool)
    blocking = _blocking_sources(pool, overall)
    return {
        "overall_status": overall,
        "overall_label": OVERALL_LABELS.get(overall, overall),
        "evaluated_at": eval_ctx.now_iso,
        "analysis_trade_date": trade_date,
        "analysis_mode": eval_ctx.analysis_mode,
        "expected_anchor": eval_ctx.expected_anchor,
        "blocking_sources": blocking,
        "fetch_errors": _fetch_errors(blocking),
        "warning_sources": _warning_sources(pool),
        "datasets": [
            {
                "source_key": k,
                "status": v.get("status"),
                "criticality": (load_contract().get(k) or {}).get("criticality"),
                "anchor_actual": v.get("anchor_actual"),
                "lag_note": v.get("lag_note"),
                "error_message": v.get("error_message"),
                "error_code": v.get("error_code"),
            }
            for k, v in sorted(pool.items())
        ],
        "section_impacts": _section_impacts(pool),
        "confidence_cap": CONFIDENCE_CAPS.get(overall, 100),
        "report_age_days": 0,
        "report_age_note": None,
    }


def clamp_confidence(confidence: int | None, overall_status: str) -> int | None:
    if confidence is None:
        return None
    cap = CONFIDENCE_CAPS.get(overall_status, 100)
    return min(int(confidence), cap)
