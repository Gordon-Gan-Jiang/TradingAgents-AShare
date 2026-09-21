from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .anchor import is_in_grace_window, resolve_expected_anchor
from .contract import all_contract_source_keys, get_contract_entry, resolve_source_key
from .parser import (
    detect_fetch_error,
    disclosure_period_lag,
    expected_disclosure_period,
    is_lhb_empty_ok,
    parse_cutoff_from_text,
    parse_disclosure_period,
    parse_last_date_from_csv,
    parse_latest_event_timestamp,
    trading_days_between,
)
from .types import EvalContext, FreshnessMeta


def _intraday_only_not_collected(entry: dict[str, Any], raw_result: Any, ctx: EvalContext) -> bool:
    if not entry.get("intraday_only") or raw_result is not None:
        return False
    from tradingagents.dataflows.trade_calendar import cn_today_str

    if ctx.trade_date != cn_today_str():
        return True
    return ctx.analysis_mode in ("historical", "closed", "t_plus_1", "forward_look")


def evaluate_source(
    source_key: str,
    raw_result: Any,
    ctx: EvalContext,
    *,
    trade_date: str | None = None,
) -> FreshnessMeta:
    entry = get_contract_entry(source_key)
    now_iso = ctx.now_iso
    if not entry:
        return FreshnessMeta(
            source_key=source_key,
            category="event_driven",
            criticality="informational",
            status="unknown",
            evaluated_at=now_iso,
            lag_note="未注册的数据源",
        )

    category = entry.get("category", "event_driven")
    criticality = entry.get("criticality", "informational")
    grace = int(entry.get("grace_minutes") or 0)

    is_err, err_code, err_msg = detect_fetch_error(raw_result)
    if _intraday_only_not_collected(entry, raw_result, ctx):
        return FreshnessMeta(
            source_key=source_key,
            category=category,
            criticality=criticality,
            status="not_applicable",
            anchor_expected=ctx.expected_anchor,
            evaluated_at=now_iso,
            lag_note="非盘中实时采集窗口，跳过实时行情新鲜度",
        )

    if category == "conditional_daily" and is_lhb_empty_ok(raw_result, trade_date or ctx.trade_date):
        return FreshnessMeta(
            source_key=source_key,
            category=category,
            criticality=criticality,
            status="empty_ok",
            anchor_expected=ctx.expected_anchor,
            evaluated_at=now_iso,
            empty_ok=True,
            lag_note="非异动日无龙虎榜数据（正常）",
        )

    if is_err:
        return FreshnessMeta(
            source_key=source_key,
            category=category,
            criticality=criticality,
            status="error",
            anchor_expected=ctx.expected_anchor,
            evaluated_at=now_iso,
            issue_type="fetch_error",
            error_code=err_code,
            error_message=err_msg,
            lag_note=err_msg,
        )

    if category == "calendar_anchored":
        return _eval_calendar(source_key, raw_result, entry, ctx, grace)
    if category == "disclosure_period":
        return _eval_disclosure(source_key, raw_result, entry, ctx)
    if category == "event_driven":
        return _eval_event_driven(source_key, raw_result, entry, ctx)
    return FreshnessMeta(
        source_key=source_key,
        category=category,
        criticality=criticality,
        status="fresh",
        evaluated_at=now_iso,
    )


def _eval_calendar(
    source_key: str,
    raw_result: Any,
    entry: dict[str, Any],
    ctx: EvalContext,
    grace: int,
) -> FreshnessMeta:
    category = entry.get("category", "calendar_anchored")
    criticality = entry.get("criticality", "important")
    expected = ctx.expected_anchor

    if source_key == "stock_data":
        actual = parse_last_date_from_csv(str(raw_result))
    elif isinstance(raw_result, str) and raw_result.lstrip().lower().startswith("date,"):
        actual = parse_last_date_from_csv(str(raw_result))
    else:
        actual = parse_cutoff_from_text(str(raw_result))

    if not actual:
        return FreshnessMeta(
            source_key=source_key,
            category=category,
            criticality=criticality,
            status="error",
            anchor_expected=expected,
            evaluated_at=ctx.now_iso,
            issue_type="fetch_error",
            error_code="parse_error",
            error_message="无法解析数据截止日期",
            lag_note="无法解析数据截止日期",
        )

    lag_days = trading_days_between(actual, expected)
    if actual >= expected:
        status = "fresh"
        lag_note = None
    elif is_in_grace_window(
        grace_minutes=grace,
        analysis_mode=ctx.analysis_mode,
        now=datetime.fromisoformat(ctx.now_iso.replace("Z", "+00:00")),
    ):
        status = "warning"
        lag_note = f"数据尚未更新至 {expected}（grace 缓冲期内）"
    else:
        status = "stale"
        lag_note = f"数据截止 {actual}，预期至少 {expected}"

    return FreshnessMeta(
        source_key=source_key,
        category=category,
        criticality=criticality,
        status=status,
        anchor_expected=expected,
        anchor_actual=actual,
        evaluated_at=ctx.now_iso,
        issue_type="lag" if status in ("warning", "stale") else None,
        lag_note=lag_note,
        lag_trading_days=lag_days if lag_days > 0 else None,
    )


def _eval_disclosure(
    source_key: str,
    raw_result: Any,
    entry: dict[str, Any],
    ctx: EvalContext,
) -> FreshnessMeta:
    category = entry.get("category", "disclosure_period")
    criticality = entry.get("criticality", "important")
    period = parse_disclosure_period(raw_result)
    expected = expected_disclosure_period(ctx.trade_date)
    if not period:
        return FreshnessMeta(
            source_key=source_key,
            category=category,
            criticality=criticality,
            status="warning",
            anchor_expected=expected,
            evaluated_at=ctx.now_iso,
            lag_note="未能解析财报披露期",
        )
    lag_q = disclosure_period_lag(period, expected)
    if lag_q <= 0:
        status = "fresh"
        lag_note = None
    elif lag_q == 1:
        status = "warning"
        lag_note = f"披露期 {period} 滞后于预期 {expected}（约 1 个报告期）"
    else:
        status = "stale"
        lag_note = f"披露期 {period} 滞后于预期 {expected}（约 {lag_q} 个报告期）"
    return FreshnessMeta(
        source_key=source_key,
        category=category,
        criticality=criticality,
        status=status,
        anchor_expected=expected,
        anchor_actual=period,
        evaluated_at=ctx.now_iso,
        issue_type="lag" if status in ("warning", "stale") else None,
        lag_note=lag_note,
    )


def _eval_event_driven(
    source_key: str,
    raw_result: Any,
    entry: dict[str, Any],
    ctx: EvalContext,
) -> FreshnessMeta:
    category = entry.get("category", "event_driven")
    criticality = entry.get("criticality", "informational")
    text = str(raw_result or "").strip()
    if not text:
        return FreshnessMeta(
            source_key=source_key,
            category=category,
            criticality=criticality,
            status="not_applicable",
            evaluated_at=ctx.now_iso,
        )
    window_hours = int(entry.get("window_hours") or 0)
    if window_hours <= 0:
        return FreshnessMeta(
            source_key=source_key,
            category=category,
            criticality=criticality,
            status="fresh",
            evaluated_at=ctx.now_iso,
        )
    latest = parse_latest_event_timestamp(raw_result)
    if latest is None:
        return FreshnessMeta(
            source_key=source_key,
            category=category,
            criticality=criticality,
            status="fresh",
            evaluated_at=ctx.now_iso,
            lag_note="未能解析事件时间戳，跳过窗口校验",
        )
    from datetime import timezone

    now_dt = datetime.fromisoformat(ctx.now_iso.replace("Z", "+00:00"))
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    age_hours = (now_dt - latest.replace(tzinfo=timezone.utc)).total_seconds() / 3600.0
    if age_hours <= window_hours:
        return FreshnessMeta(
            source_key=source_key,
            category=category,
            criticality=criticality,
            status="fresh",
            anchor_actual=latest.strftime("%Y-%m-%d %H:%M:%S"),
            evaluated_at=ctx.now_iso,
        )
    return FreshnessMeta(
        source_key=source_key,
        category=category,
        criticality=criticality,
        status="stale",
        anchor_actual=latest.strftime("%Y-%m-%d %H:%M:%S"),
        evaluated_at=ctx.now_iso,
        issue_type="lag",
        lag_note=f"最新事件时间 {latest.strftime('%Y-%m-%d %H:%M')} 超出 {window_hours}h 窗口",
    )


def build_eval_context(
    trade_date: str,
    *,
    symbol: str = "600519.SH",
    analysis_mode: str | None = None,
    now: datetime | None = None,
) -> EvalContext:
    from .anchor import _infer_analysis_mode

    del symbol
    mode = analysis_mode or _infer_analysis_mode(trade_date, now=now)
    now_dt = now or datetime.now(timezone.utc)
    return EvalContext(
        trade_date=trade_date,
        analysis_mode=mode,
        expected_anchor=resolve_expected_anchor(trade_date, analysis_mode=mode, now=now),
        now_iso=now_dt.astimezone(timezone.utc).isoformat(),
    )


def evaluate_collector_results(
    results: dict[str, Any],
    trade_date: str,
    *,
    symbol: str = "600519.SH",
    analysis_mode: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Evaluate all collector keys and return freshness_pool keyed by contract source_key."""
    eval_ctx = build_eval_context(trade_date, symbol=symbol, analysis_mode=analysis_mode)
    pool: dict[str, dict[str, Any]] = {}

    stock_meta: FreshnessMeta | None = None
    for collector_key, raw in results.items():
        if collector_key.startswith("_") or collector_key in ("vpa_indicators", "macro_ashare_brief"):
            continue
        source_key = resolve_source_key(collector_key)
        entry = get_contract_entry(source_key)
        if entry and entry.get("inherit_from") == "stock_data":
            continue
        meta = evaluate_source(source_key, raw, eval_ctx, trade_date=trade_date)
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

    for source_key in ("individual_fund_flow", "board_fund_flow", "zt_pool", "lhb_detail"):
        if source_key not in pool:
            meta = evaluate_source(source_key, None, eval_ctx, trade_date=trade_date)
            pool[source_key] = meta.to_dict()

    for source_key in all_contract_source_keys():
        if source_key in pool:
            continue
        entry = get_contract_entry(source_key) or {}
        if entry.get("inherit_from") == "stock_data":
            continue
        meta = evaluate_source(source_key, None, eval_ctx, trade_date=trade_date)
        pool[source_key] = meta.to_dict()

    return pool
