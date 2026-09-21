"""Watchlist service for database operations."""

import time
from typing import List
from uuid import uuid4

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from api.database import WatchlistItemDB, ScheduledAnalysisDB

MAX_WATCHLIST_ITEMS = 200


def list_watchlist(db: Session, user_id: str) -> List[dict]:
    """List user's watchlist items with scheduled status."""
    items = (
        db.query(WatchlistItemDB)
        .filter(WatchlistItemDB.user_id == user_id)
        .order_by(WatchlistItemDB.sort_order, WatchlistItemDB.created_at)
        .all()
    )
    scheduled_symbols = set(
        row.symbol for row in
        db.query(ScheduledAnalysisDB.symbol)
        .filter(ScheduledAnalysisDB.user_id == user_id)
        .all()
    )
    return [
        {
            "id": item.id,
            "symbol": item.symbol,
            "sort_order": item.sort_order,
            "created_at": item.created_at.isoformat() if item.created_at else None,
            "has_scheduled": item.symbol in scheduled_symbols,
        }
        for item in items
    ]


def add_watchlist_item(db: Session, user_id: str, symbol: str) -> dict:
    """Add a stock to user's watchlist."""
    count = db.query(WatchlistItemDB).filter(WatchlistItemDB.user_id == user_id).count()
    if count >= MAX_WATCHLIST_ITEMS:
        raise ValueError(f"自选股数量已达上限 ({MAX_WATCHLIST_ITEMS})")

    existing = (
        db.query(WatchlistItemDB)
        .filter(WatchlistItemDB.user_id == user_id, WatchlistItemDB.symbol == symbol)
        .first()
    )
    if existing:
        raise ValueError(f"{symbol} 已在自选列表中")

    current_item = WatchlistItemDB(id=uuid4().hex, user_id=user_id, symbol=symbol)
    db.add(current_item)

    # Retry commit on transient "database is locked" (e.g. concurrent T+1 refresh).
    for attempt in range(4):
        try:
            db.commit()
            break
        except OperationalError as exc:
            if "database is locked" in str(exc).lower() and attempt < 3:
                db.rollback()
                time.sleep(0.3 * (2 ** attempt))  # 0.3 s, 0.6 s, 1.2 s
                # After rollback the old object is detached; create a fresh one.
                current_item = WatchlistItemDB(id=uuid4().hex, user_id=user_id, symbol=symbol)
                db.add(current_item)
            else:
                raise

    db.refresh(current_item)
    return {
        "id": current_item.id,
        "symbol": current_item.symbol,
        "sort_order": current_item.sort_order,
        "created_at": current_item.created_at.isoformat() if current_item.created_at else None,
    }


def add_watchlist_items(db: Session, user_id: str, symbols: List[str]) -> List[dict]:
    """Add multiple stocks to user's watchlist and return per-item results."""
    results: List[dict] = []
    for symbol in symbols:
        try:
            item = add_watchlist_item(db, user_id, symbol)
            results.append({
                "symbol": symbol,
                "status": "added",
                "item": item,
                "message": "已添加到自选列表",
            })
        except ValueError as exc:
            message = str(exc)
            status = "duplicate" if "已在自选列表" in message else "failed"
            results.append({
                "symbol": symbol,
                "status": status,
                "message": message,
            })
    return results


def delete_watchlist_item(db: Session, user_id: str, item_id: str) -> bool:
    """Delete a watchlist item. Returns True if found and deleted."""
    item = (
        db.query(WatchlistItemDB)
        .filter(WatchlistItemDB.id == item_id, WatchlistItemDB.user_id == user_id)
        .first()
    )
    if not item:
        return False
    db.delete(item)
    db.commit()
    return True


def batch_delete_watchlist_items(db: Session, user_id: str, item_ids: List[str]) -> dict:
    """Delete multiple watchlist items in one request."""
    normalized_ids: list[str] = []
    seen: set[str] = set()
    for raw in item_ids:
        item_id = (raw or "").strip()
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)
        normalized_ids.append(item_id)
    if not normalized_ids:
        raise ValueError("请至少选择一个自选股")

    items = (
        db.query(WatchlistItemDB)
        .filter(
            WatchlistItemDB.user_id == user_id,
            WatchlistItemDB.id.in_(normalized_ids),
        )
        .all()
    )
    item_map = {item.id: item for item in items}
    deleted_ids: list[str] = []
    deleted_symbols: list[str] = []
    missing_ids: list[str] = []

    for item_id in normalized_ids:
        item = item_map.get(item_id)
        if item is None:
            missing_ids.append(item_id)
            continue
        deleted_symbols.append(item.symbol)
        db.delete(item)
        deleted_ids.append(item_id)

    if deleted_ids:
        db.commit()

    return {
        "deleted_ids": deleted_ids,
        "deleted_symbols": deleted_symbols,
        "missing_ids": missing_ids,
    }
