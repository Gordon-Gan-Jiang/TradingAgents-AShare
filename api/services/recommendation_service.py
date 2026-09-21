from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from api.database import ImportedPortfolioPositionDB, UserDB, WatchlistItemDB
from api.services import auth_service
from api.services import daily_stock_analysis_service
from api.services import market_scanner_service
from api.services.wecom_notification_service import send_message
from api.services.wps_notification_service import send_markdown_message
from tradingagents.dataflows.trade_calendar import cn_today_str, is_cn_trading_day

_TZ = ZoneInfo("Asia/Shanghai")
_recommendation_sent: dict[tuple[str, str], float] = {}


def recommend_for_user(
    db: Session,
    *,
    user_id: str,
    top_k: int = 5,
    candidate_limit: int = 80,
    include_tracking: bool = True,
    include_watchlist: bool = True,
    seed_symbols: Iterable[str] | None = None,
    min_change_pct: float = -100.0,
    market: str = "cn",
    profile: str | None = None,
    momentum_weight: float | None = None,
    activity_weight: float | None = None,
    near_high_weight: float | None = None,
    source_mode: str = "user_pool",
    scan_limit: int | None = None,
    min_price: float = 2.0,
    max_price: float = 10000.0,
    min_amount: float = 2e8,
    min_turnover_rate: float = 0.8,
    min_volume_ratio: float = 0.6,
    limit_up_threshold_pct: float = 9.6,
    limit_down_threshold_pct: float = -9.6,
    enforce_tradability: bool = True,
) -> dict[str, Any]:
    """从用户关注池中给出候选推荐（不直接下单）。"""
    top_k = max(1, min(int(top_k), 20))
    candidate_limit = max(10, min(int(candidate_limit), 300))

    if str(source_mode).lower() == "market_scan" and str(market).lower() == "cn":
        out = market_scanner_service.scan_market_candidates(
            top_k=top_k,
            min_change_pct=min_change_pct,
            profile=profile,
            momentum_weight=momentum_weight,
            activity_weight=activity_weight,
            near_high_weight=near_high_weight,
            scan_limit=scan_limit,
            min_price=min_price,
            max_price=max_price,
            min_amount=min_amount,
            min_turnover_rate=min_turnover_rate,
            min_volume_ratio=min_volume_ratio,
            limit_up_threshold_pct=limit_up_threshold_pct,
            limit_down_threshold_pct=limit_down_threshold_pct,
            enforce_tradability=enforce_tradability,
        )
        return _apply_tradability_guard(
            out,
            top_k=top_k,
            min_amount=min_amount,
            min_turnover_rate=min_turnover_rate,
            min_volume_ratio=min_volume_ratio,
            limit_up_threshold_pct=limit_up_threshold_pct,
            limit_down_threshold_pct=limit_down_threshold_pct,
            enforce_tradability=enforce_tradability,
        )

    symbol_to_name: dict[str, str] = {}

    if include_tracking:
        rows = (
            db.query(ImportedPortfolioPositionDB)
            .filter(ImportedPortfolioPositionDB.user_id == user_id)
            .order_by(
                ImportedPortfolioPositionDB.market_value.desc(),
                ImportedPortfolioPositionDB.current_position.desc(),
                ImportedPortfolioPositionDB.symbol,
            )
            .limit(candidate_limit)
            .all()
        )
        for row in rows:
            if row.symbol and row.symbol not in symbol_to_name:
                symbol_to_name[row.symbol] = (row.security_name or row.symbol).strip()

    if include_watchlist:
        rows = (
            db.query(WatchlistItemDB)
            .filter(WatchlistItemDB.user_id == user_id)
            .order_by(WatchlistItemDB.sort_order, WatchlistItemDB.created_at.desc())
            .limit(candidate_limit)
            .all()
        )
        for row in rows:
            norm = _normalize_symbol(row.symbol)
            if norm and norm not in symbol_to_name:
                symbol_to_name[norm] = norm

    daily_stock_analysis_service.merge_seed_symbols(symbol_to_name, seed_symbols)
    symbols = list(symbol_to_name.keys())[:candidate_limit]
    code_to_name = _get_code_to_name_map()
    for symbol in symbols:
        existing_name = str(symbol_to_name.get(symbol) or "").strip()
        # If we only have a raw code-like name, prefer stock display name from map.
        if not existing_name or existing_name.upper() == symbol.upper():
            symbol_to_name[symbol] = code_to_name.get(symbol) or symbol
        else:
            symbol_to_name[symbol] = existing_name
    out = daily_stock_analysis_service.analyze_daily_candidates(
        symbol_to_name=symbol_to_name,
        top_k=top_k,
        min_change_pct=min_change_pct,
        market=("us" if str(market).lower() == "us" else "cn"),
        profile=profile,
        momentum_weight=momentum_weight,
        activity_weight=activity_weight,
        near_high_weight=near_high_weight,
    )
    return _apply_tradability_guard(
        out,
        top_k=top_k,
        min_amount=min_amount,
        min_turnover_rate=min_turnover_rate,
        min_volume_ratio=min_volume_ratio,
        limit_up_threshold_pct=limit_up_threshold_pct,
        limit_down_threshold_pct=limit_down_threshold_pct,
        enforce_tradability=enforce_tradability,
    )


def recommendation_pushes_enabled() -> bool:
    return str(os.environ.get("TA_RECOMMEND_PUSH_ENABLED", "1")).strip().lower() not in (
        "0",
        "false",
        "no",
    )


def recommendation_push_interval_sec() -> int:
    return max(60, int(os.environ.get("TA_RECOMMEND_PUSH_INTERVAL_SEC", "120")))


def _in_push_window(now: datetime) -> str | None:
    # 开盘观察窗口 09:35-10:00；收盘复盘窗口 14:55-15:20
    minutes = now.hour * 60 + now.minute
    if 9 * 60 + 35 <= minutes <= 10 * 60:
        return "open"
    if 14 * 60 + 55 <= minutes <= 15 * 60 + 20:
        return "close"
    return None


def _to_cny_amount(v: float | None) -> str:
    if v is None:
        return "--"
    a = abs(v)
    if a >= 1e8:
        return f"{v/1e8:.2f}亿"
    if a >= 1e4:
        return f"{v/1e4:.2f}万"
    return f"{v:.0f}"


def _build_push_lines(items: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for i, it in enumerate(items, start=1):
        sym = str(it.get("symbol") or "")
        name = str(it.get("name") or sym)
        chg = _to_float(it.get("price_change_pct"))
        chg_s = "--" if chg is None else f"{chg:+.2f}%"
        score = _to_float(it.get("score")) or 0.0
        amount = _to_float(it.get("amount"))
        lines.append(f"{i}. {name}（{sym}） 评分{score:.1f} 涨跌{chg_s} 成交额{_to_cny_amount(amount)}")
    return lines


def _build_wecom_recommendation_message(phase: str, items: list[dict[str, Any]], today: str) -> str:
    title = "开盘观察推荐" if phase == "open" else "收盘复盘推荐"
    lines = [f"【{title}】", f"日期：{today}", ""]
    lines.extend(_build_push_lines(items))
    lines.append("")
    lines.append("说明：仅供研究参考，不构成投资建议。")
    return "\n".join(lines)[:1800]


def _build_wps_recommendation_markdown(phase: str, items: list[dict[str, Any]], today: str) -> str:
    title = "开盘观察推荐" if phase == "open" else "收盘复盘推荐"
    lines = [f"## {title}", "", f"**日期**：{today}", ""]
    for line in _build_push_lines(items):
        lines.append(f"- {line}")
    lines.extend(["", "> 仅供研究参考，不构成投资建议。"])
    return "\n".join(lines)[:5000]


def _send_recommendation_push_for_user(
    db: Session,
    *,
    user: UserDB,
    phase: str,
    today: str,
    force: bool = False,
) -> dict[str, Any]:
    key = (user.id, f"{today}:{phase}")
    if not force and key in _recommendation_sent:
        return {"status": "skipped_dedup", "items_count": 0, "sent": False}

    rec = recommend_for_user(
        db=db,
        user_id=user.id,
        top_k=3,
        candidate_limit=80,
        include_tracking=True,
        include_watchlist=True,
        min_change_pct=-2.0,
    )
    items = rec.get("items") or []
    if not items:
        return {"status": "no_items", "items_count": 0, "sent": False}

    cfg = auth_service.get_user_llm_config(db, user.id)
    wecom_hook = auth_service.decrypt_secret(getattr(cfg, "wecom_webhook_encrypted", None))
    wps_hook = auth_service.decrypt_secret(getattr(cfg, "wps_webhook_encrypted", None))

    sent = False
    sent_wecom = False
    sent_wps = False
    if getattr(user, "wecom_report_enabled", True) and wecom_hook:
        try:
            sent_wecom = bool(send_message(_build_wecom_recommendation_message(phase, items, today), wecom_hook))
            sent = sent_wecom or sent
        except Exception:
            pass
    if getattr(user, "wps_report_enabled", True) and wps_hook:
        try:
            sent_wps = bool(send_markdown_message(_build_wps_recommendation_markdown(phase, items, today), wps_hook))
            sent = sent_wps or sent
        except Exception:
            pass
    if sent:
        _recommendation_sent[key] = time.monotonic()

    return {
        "status": "sent" if sent else "no_channel_or_failed",
        "items_count": len(items),
        "sent": sent,
        "sent_wecom": sent_wecom,
        "sent_wps": sent_wps,
    }


def run_scheduled_recommendation_pushes(db: Session) -> None:
    if not recommendation_pushes_enabled():
        return
    today = cn_today_str()
    if not is_cn_trading_day(today):
        return
    now = datetime.now(tz=_TZ)
    phase = _in_push_window(now)
    if not phase:
        return

    users = db.query(UserDB).filter(UserDB.is_active == True).all()  # noqa: E712
    for user in users:
        _send_recommendation_push_for_user(db, user=user, phase=phase, today=today, force=False)


def manual_trigger_recommendation_push(
    db: Session,
    *,
    user_id: str,
    phase: str = "close",
    force: bool = True,
) -> dict[str, Any]:
    phase = str(phase or "close").strip().lower()
    if phase not in {"open", "close"}:
        raise ValueError("phase must be 'open' or 'close'")
    user = db.query(UserDB).filter(UserDB.id == user_id, UserDB.is_active == True).first()  # noqa: E712
    if not user:
        raise ValueError("user not found or inactive")
    today = cn_today_str()
    result = _send_recommendation_push_for_user(db, user=user, phase=phase, today=today, force=force)
    result["today"] = today
    result["phase"] = phase
    return result


def _normalize_symbol(value: Any) -> str | None:
    return daily_stock_analysis_service.normalize_symbol(value)


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), 4)
    except Exception:
        return None


def _apply_tradability_guard(
    payload: dict[str, Any],
    *,
    top_k: int,
    min_amount: float,
    min_turnover_rate: float,
    min_volume_ratio: float,
    limit_up_threshold_pct: float,
    limit_down_threshold_pct: float,
    enforce_tradability: bool,
) -> dict[str, Any]:
    if not enforce_tradability:
        return payload
    items = list(payload.get("items") or [])
    if not items:
        return payload
    kept: list[dict[str, Any]] = []
    for item in items:
        change_pct = _to_float(item.get("price_change_pct"))
        amount = _to_float(item.get("amount"))
        turnover_rate = _to_float(item.get("turnover_rate"))
        volume_ratio = _to_float(item.get("volume_ratio"))
        if change_pct is not None and (
            change_pct >= float(limit_up_threshold_pct) or change_pct <= float(limit_down_threshold_pct)
        ):
            continue
        if amount is not None and amount < float(min_amount):
            continue
        if turnover_rate is not None and turnover_rate < float(min_turnover_rate):
            continue
        if volume_ratio is not None and volume_ratio < float(min_volume_ratio):
            continue
        kept.append(item)
    payload["items"] = kept[: max(1, int(top_k))]
    payload["scored_size"] = len(kept)
    scoring_model = payload.get("scoring_model")
    if isinstance(scoring_model, dict):
        filters = dict(scoring_model.get("filters") or {})
        filters.update(
            {
                "min_turnover_rate": min_turnover_rate,
                "min_volume_ratio": min_volume_ratio,
                "limit_up_threshold_pct": limit_up_threshold_pct,
                "limit_down_threshold_pct": limit_down_threshold_pct,
                "enforce_tradability": bool(enforce_tradability),
            }
        )
        scoring_model["filters"] = filters
    return payload


def _get_code_to_name_map() -> dict[str, str]:
    try:
        from api.main import _get_reverse_stock_map

        return _get_reverse_stock_map()
    except Exception:
        return {}
