from __future__ import annotations

from typing import Any

from .aggregator import build_freshness_pool
from .contract import all_contract_source_keys, get_contract_entry
from .types import FreshnessMeta
from .validator import build_eval_context, evaluate_source


def resolve_freshness_pool(
    state: dict[str, Any] | None,
    data_collector: Any | None,
    ticker: str,
    trade_date: str,
    *,
    fallback_results: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]] | None:
    """Prefer graph state, then DataCollector cache, then on-the-fly evaluation."""
    state_pool = (state or {}).get("freshness_pool")
    if isinstance(state_pool, dict) and state_pool:
        return state_pool
    if data_collector is not None:
        cached = data_collector.get(ticker, trade_date)
        if isinstance(cached, dict):
            pool = cached.get("freshness_pool")
            if isinstance(pool, dict) and pool:
                return pool
            if fallback_results is None and cached:
                fallback_results = cached
    if fallback_results is not None:
        return build_freshness_pool(fallback_results, trade_date, symbol=ticker)
    return None


def build_degraded_freshness_pool(
    trade_date: str,
    *,
    symbol: str = "600519.SH",
    analysis_mode: str | None = None,
    reason: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Evaluate every contract source as missing when pool construction failed."""
    ctx = build_eval_context(trade_date, symbol=symbol, analysis_mode=analysis_mode)
    pool: dict[str, dict[str, Any]] = {}
    stock_meta: FreshnessMeta | None = None
    msg = reason or "freshness 评估降级：数据源未采集"

    for source_key in all_contract_source_keys():
        entry = get_contract_entry(source_key) or {}
        if entry.get("inherit_from") == "stock_data":
            continue
        meta = evaluate_source(source_key, None, ctx, trade_date=trade_date)
        if meta.status == "error":
            meta.error_code = meta.error_code or "pool_build_failed"
            meta.error_message = msg
            meta.lag_note = msg
        pool[source_key] = meta.to_dict()
        if source_key == "stock_data":
            stock_meta = meta

    if stock_meta:
        ind = FreshnessMeta(
            source_key="indicators",
            category="calendar_anchored",
            criticality="critical",
            status=stock_meta.status,
            anchor_expected=stock_meta.anchor_expected,
            anchor_actual=stock_meta.anchor_actual,
            evaluated_at=stock_meta.evaluated_at,
            issue_type=stock_meta.issue_type,
            lag_note=stock_meta.lag_note,
            lag_trading_days=stock_meta.lag_trading_days,
            error_code=stock_meta.error_code,
            error_message=stock_meta.error_message,
        )
        pool["indicators"] = ind.to_dict()
    return pool


def inject_freshness_into_state(
    state: dict[str, Any],
    data_collector: Any | None,
    ticker: str,
    trade_date: str,
) -> dict[str, Any]:
    """Attach freshness_pool from DataCollector cache into graph state."""
    pool: dict[str, dict[str, Any]] | None = None
    if data_collector is not None:
        cached = data_collector.get(ticker, trade_date)
        if isinstance(cached, dict):
            raw_pool = cached.get("freshness_pool")
            if isinstance(raw_pool, dict) and raw_pool:
                pool = raw_pool
            elif cached:
                pool = build_freshness_pool(cached, trade_date, symbol=ticker)
    if not pool:
        pool = build_degraded_freshness_pool(trade_date, symbol=ticker)
    state["freshness_pool"] = pool
    return state


def enrich_freshness_summary_for_display(
    summary: dict[str, Any] | None,
    created_at_iso: str | None,
) -> dict[str, Any] | None:
    if not isinstance(summary, dict):
        return summary
    enriched = dict(summary)
    if enriched.get("report_age_note"):
        return enriched
    if not created_at_iso:
        return enriched
    try:
        from datetime import datetime, timezone

        from .parser import trading_days_between
        from tradingagents.dataflows.trade_calendar import cn_today_str

        created = datetime.fromisoformat(str(created_at_iso).replace("Z", "+00:00"))
        created_date = created.astimezone(timezone.utc).date().strftime("%Y-%m-%d")
        today = cn_today_str()
        age_days = trading_days_between(created_date, today)
        enriched["report_age_days"] = age_days
        if age_days > 3:
            enriched["report_age_note"] = (
                f"报告生成于 {age_days} 个交易日前，市场可能已变化（不影响上方数据状态判定）。"
            )
    except Exception:
        pass
    return enriched


def section_freshness_tooltip(
    summary: dict[str, Any] | None,
    section_key: str,
    section_status: str | None,
) -> str | None:
    if not summary or not section_status or section_status in ("fresh", "not_applicable", "empty_ok"):
        return None
    section_map = summary.get("section_impacts") or {}
    if section_map.get(section_key) != section_status:
        return section_status
    datasets = {d.get("source_key"): d for d in (summary.get("datasets") or []) if d.get("source_key")}
    blocking = {b.get("source_key"): b for b in (summary.get("blocking_sources") or []) if b.get("source_key")}
    fetch_errors = {e.get("source_key"): e for e in (summary.get("fetch_errors") or []) if e.get("source_key")}
    for bucket in (blocking, fetch_errors, datasets):
        for _sk, item in bucket.items():
            if item.get("status") == section_status or section_status == "error":
                note = item.get("lag_note") or item.get("error_message")
                if note:
                    return str(note)
    return section_status
