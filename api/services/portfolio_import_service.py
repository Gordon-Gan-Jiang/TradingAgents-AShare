"""Generic portfolio position import service.

Manages imported holdings from any source. Positions are stored as snapshots
in ``ImportedPortfolioPositionDB`` with a configurable ``source`` tag.
No dependency on any specific broker SDK.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from api.database import ImportedPortfolioPositionDB, ScheduledAnalysisDB
from api.services import scheduled_service
from tradingagents.agents.utils.context_utils import normalize_user_context


logger = logging.getLogger(__name__)

_CODE_RE = re.compile(r"^(\d{6})\.(SH|SZ|BJ)$")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _row_to_merge_base(row: ImportedPortfolioPositionDB) -> dict[str, Any]:
    return {
        "symbol": row.symbol,
        "name": row.security_name,
        "current_position": row.current_position,
        "available_position": row.available_position,
        "average_cost": row.average_cost,
        "market_value": row.market_value,
        "current_position_pct": row.current_position_pct,
    }


def _empty_merge_base(symbol: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "name": None,
        "current_position": None,
        "available_position": None,
        "average_cost": None,
        "market_value": None,
        "current_position_pct": None,
    }


def _normalize_merge_patch(raw: dict[str, Any]) -> dict[str, Any] | None:
    symbol = _normalize_code(raw.get("symbol"))
    if not symbol:
        return None
    patch: dict[str, Any] = {"symbol": symbol}
    if "name" in raw:
        patch["name"] = (raw.get("name") or "").strip() or None
    for key in (
        "current_position",
        "available_position",
        "average_cost",
        "market_value",
        "current_position_pct",
    ):
        if key in raw:
            patch[key] = _to_float(raw.get(key))
    return patch


def _apply_position_patch(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in patch.items():
        if k == "symbol":
            continue
        out[k] = v
    return out


def merge_imported_positions(
    db: Session,
    user_id: str,
    positions: list[dict[str, Any]],
    source: str = "manual",
    auto_apply_scheduled: bool = True,
) -> dict[str, Any]:
    """在指定 source 上与现有持仓合并：新代码追加，已有代码按请求字段覆盖（未传的字段保留）。"""
    if not isinstance(positions, list) or not positions:
        raise ValueError("positions 至少包含一条记录")

    source = (source or "manual").strip()
    patches: list[dict[str, Any]] = []
    for raw in positions:
        p = _normalize_merge_patch(raw)
        if p:
            patches.append(p)
    if not patches:
        raise ValueError("没有有效的股票代码")

    rows = (
        db.query(ImportedPortfolioPositionDB)
        .filter(
            ImportedPortfolioPositionDB.user_id == user_id,
            ImportedPortfolioPositionDB.source == source,
        )
        .order_by(
            ImportedPortfolioPositionDB.market_value.desc(),
            ImportedPortfolioPositionDB.current_position.desc(),
            ImportedPortfolioPositionDB.symbol,
        )
        .all()
    )

    merged_by_symbol: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row in rows:
        merged_by_symbol[row.symbol] = _row_to_merge_base(row)
        order.append(row.symbol)

    for patch in patches:
        sym = patch["symbol"]
        if sym in merged_by_symbol:
            merged_by_symbol[sym] = _apply_position_patch(merged_by_symbol[sym], patch)
        else:
            merged_by_symbol[sym] = _apply_position_patch(_empty_merge_base(sym), patch)
            order.append(sym)

    merged_list = [merged_by_symbol[s] for s in order]
    return sync_positions(db, user_id, merged_list, source=source, auto_apply_scheduled=auto_apply_scheduled)


def sync_positions(
    db: Session,
    user_id: str,
    positions: list[dict[str, Any]],
    source: str = "manual",
    auto_apply_scheduled: bool = True,
) -> dict[str, Any]:
    """Replace the position snapshot for *source* with *positions*.

    Each item in *positions* should contain at minimum ``symbol`` (e.g.
    ``"600519.SH"``).  Optional fields: ``name``, ``current_position``,
    ``available_position``, ``average_cost``, ``market_value``,
    ``current_position_pct``.
    """
    if not isinstance(positions, list):
        raise ValueError("positions 必须为列表")

    source = (source or "manual").strip()
    now = datetime.now(timezone.utc)

    # Normalize & deduplicate
    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in positions:
        symbol = _normalize_code(raw.get("symbol"))
        if symbol is None:
            continue
        if symbol in seen:
            continue
        seen.add(symbol)
        cleaned.append({
            "symbol": symbol,
            "name": (raw.get("name") or "").strip() or None,
            "current_position": _to_float(raw.get("current_position")),
            "available_position": _to_float(raw.get("available_position")),
            "average_cost": _to_float(raw.get("average_cost")),
            "market_value": _to_float(raw.get("market_value")),
            "current_position_pct": _to_float(raw.get("current_position_pct")),
        })

    # Compute position_pct if not provided but market_value is available
    total_mv = sum(p["market_value"] or 0 for p in cleaned if (p["market_value"] or 0) > 0)
    if total_mv > 0:
        for p in cleaned:
            if p["current_position_pct"] is None and p["market_value"] and p["market_value"] > 0:
                p["current_position_pct"] = round((p["market_value"] / total_mv) * 100, 4)

    if not cleaned:
        raise ValueError("没有有效的持仓记录，请检查输入格式")

    # Replace snapshot for this source
    db.query(ImportedPortfolioPositionDB).filter(
        ImportedPortfolioPositionDB.user_id == user_id,
        ImportedPortfolioPositionDB.source == source,
    ).delete()

    for p in cleaned:
        db.add(ImportedPortfolioPositionDB(
            id=uuid4().hex,
            user_id=user_id,
            source=source,
            symbol=p["symbol"],
            security_name=p["name"],
            current_position=p["current_position"],
            available_position=p["available_position"],
            average_cost=p["average_cost"],
            market_value=p["market_value"],
            current_position_pct=p["current_position_pct"],
            trade_points_json=[],
            trade_points_count=0,
            latest_trade_at=None,
            latest_trade_action=None,
            last_imported_at=now,
        ))

    scheduled_sync: dict[str, list] = {"created": [], "existing": [], "skipped_limit": []}
    if auto_apply_scheduled:
        ordered = [p["symbol"] for p in cleaned if (p["current_position"] or 0) > 0]
        scheduled_sync = scheduled_service.ensure_scheduled_for_symbols(
            db=db,
            user_id=user_id,
            symbols=ordered,
        )

    db.commit()
    return get_import_state(db, user_id, scheduled_sync=scheduled_sync)


def get_import_state(
    db: Session,
    user_id: str,
    scheduled_sync: dict[str, Any] | None = None,
) -> dict[str, Any]:
    positions = list_imported_positions(db, user_id)
    return {
        "auto_apply_scheduled": True,
        "last_synced_at": _latest_imported_at(positions),
        "last_error": None,
        "summary": {"positions": len(positions)},
        "scheduled_sync": scheduled_sync or {"created": [], "existing": [], "skipped_limit": []},
        "positions": positions,
    }


def list_imported_positions(db: Session, user_id: str) -> list[dict[str, Any]]:
    """List all imported positions for a user, regardless of source."""
    rows = (
        db.query(ImportedPortfolioPositionDB)
        .filter(ImportedPortfolioPositionDB.user_id == user_id)
        .order_by(
            ImportedPortfolioPositionDB.market_value.desc(),
            ImportedPortfolioPositionDB.current_position.desc(),
            ImportedPortfolioPositionDB.symbol,
        )
        .all()
    )
    return [
        {
            "symbol": row.symbol,
            "name": row.security_name or row.symbol,
            "source": row.source,
            "current_position": row.current_position,
            "available_position": row.available_position,
            "average_cost": row.average_cost,
            "market_value": row.market_value,
            "current_position_pct": row.current_position_pct,
            "trade_points_count": row.trade_points_count or 0,
            "last_imported_at": row.last_imported_at.isoformat() if row.last_imported_at else None,
        }
        for row in rows
    ]


def build_scheduled_user_context(db: Session, user_id: str, symbol: str) -> dict[str, Any]:
    """Build user context for a scheduled analysis from any imported source."""
    row = (
        db.query(ImportedPortfolioPositionDB)
        .filter(
            ImportedPortfolioPositionDB.user_id == user_id,
            ImportedPortfolioPositionDB.symbol == (symbol or "").strip().upper(),
        )
        .first()
    )
    if not row:
        return {}

    payload: dict[str, Any] = {
        "objective": "持有处理" if (row.current_position or 0) > 0 else "观察",
        "current_position": row.current_position,
        "current_position_pct": row.current_position_pct,
        "average_cost": row.average_cost,
        "user_notes": f"来源：持仓导入（{row.source}）",
    }
    return normalize_user_context(payload)


def clear_imported_portfolio(db: Session, user_id: str) -> None:
    """Clear all imported positions for a user, regardless of source."""
    db.query(ImportedPortfolioPositionDB).filter(
        ImportedPortfolioPositionDB.user_id == user_id,
    ).delete()
    db.commit()


def delete_imported_positions_for_symbol(db: Session, user_id: str, symbol: str) -> dict[str, Any]:
    """Remove all imported rows for this user+标的（跨 source），并删除同标的的定时分析任务。"""
    norm = _normalize_code(symbol)
    if not norm:
        raise ValueError("无效的股票代码，请使用如 600519.SH / 000001.SZ")

    deleted = (
        db.query(ImportedPortfolioPositionDB)
        .filter(
            ImportedPortfolioPositionDB.user_id == user_id,
            ImportedPortfolioPositionDB.symbol == norm,
        )
        .delete(synchronize_session=False)
    )

    scheduled = (
        db.query(ScheduledAnalysisDB)
        .filter(
            ScheduledAnalysisDB.user_id == user_id,
            ScheduledAnalysisDB.symbol == norm,
        )
        .first()
    )
    scheduled_removed = False
    if scheduled:
        db.delete(scheduled)
        scheduled_removed = True

    db.commit()
    return {
        "symbol": norm,
        "deleted_positions": int(deleted or 0),
        "scheduled_removed": scheduled_removed,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_code(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    if not text:
        return None
    if _CODE_RE.match(text):
        return text
    if re.match(r"^\d{6}$", text):
        if text.startswith("6"):
            return f"{text}.SH"
        if text.startswith(("0", "3")):
            return f"{text}.SZ"
        if text.startswith(("4", "8")):
            return f"{text}.BJ"
    return None


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _latest_imported_at(positions: list[dict[str, Any]]) -> str | None:
    dates = [p["last_imported_at"] for p in positions if p.get("last_imported_at")]
    return max(dates) if dates else None
