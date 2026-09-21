from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Iterable
from uuid import uuid4

from sqlalchemy.orm import Session

from api.database import (
    DailyReviewDB,
    ImportedPortfolioPositionDB,
    PaperPortfolioDB,
    PaperPositionDB,
    PaperTradeDB,
)
from api.services.daily_stock_analysis_service import normalize_symbol
from tradingagents.dataflows.interface import route_to_vendor

logger = logging.getLogger(__name__)

# A 股交易成本参数。历史实现只按 `gross * fee_rate` 记一次佣金，漏掉了卖出印花税
# 与佣金最低收费，使虚拟盘收益系统性偏乐观。
MIN_COMMISSION = 5.0          # 佣金最低收费（元）
STAMP_TAX_RATE = 0.0005       # 印花税 0.05%，仅卖出方缴纳
TRANSFER_FEE_RATE = 0.00001   # 过户费，双向收取


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _normalize_symbol(symbol: Any) -> str | None:
    return normalize_symbol(symbol)


def _fetch_quotes(symbols: Iterable[str]) -> dict[str, dict[str, Any]]:
    symbols = [s for s in dict.fromkeys(str(item).upper() for item in symbols if item)]
    if not symbols:
        return {}
    try:
        raw = route_to_vendor("get_realtime_quotes", symbols)
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _resolve_quote_price(quote: dict[str, Any]) -> float | None:
    for key in ("price", "last", "close"):
        value = _to_float(quote.get(key))
        if value and value > 0:
            return value
    return None


def _ensure_portfolio(db: Session, user_id: str, initial_cash: float | None = None) -> PaperPortfolioDB:
    row = db.query(PaperPortfolioDB).filter(PaperPortfolioDB.user_id == user_id).first()
    if row:
        return row
    cash = max(0.0, float(initial_cash if initial_cash is not None else 1_000_000.0))
    row = PaperPortfolioDB(
        id=str(uuid4()),
        user_id=user_id,
        initial_cash=cash,
        cash_balance=cash,
        total_realized_pnl=0.0,
        total_fees=0.0,
    )
    db.add(row)
    db.flush()
    return row


def _snapshot_positions(db: Session, portfolio_id: str) -> list[PaperPositionDB]:
    return (
        db.query(PaperPositionDB)
        .filter(PaperPositionDB.portfolio_id == portfolio_id, PaperPositionDB.quantity > 0)
        .order_by(PaperPositionDB.symbol)
        .all()
    )


def _portfolio_market_value(positions: list[PaperPositionDB], quotes: dict[str, dict[str, Any]]) -> float:
    total = 0.0
    for pos in positions:
        price = _resolve_quote_price(quotes.get(pos.symbol) or {})
        if price is not None:
            total += price * float(pos.quantity or 0.0)
            continue
        total += float(pos.avg_cost or 0.0) * float(pos.quantity or 0.0)
    return total


def get_portfolio_snapshot(db: Session, user_id: str) -> dict[str, Any]:
    portfolio = _ensure_portfolio(db, user_id)
    positions = _snapshot_positions(db, portfolio.id)
    quotes = _fetch_quotes([p.symbol for p in positions])
    market_value = _portfolio_market_value(positions, quotes)
    equity = float(portfolio.cash_balance or 0.0) + market_value
    initial_cash = float(portfolio.initial_cash or 0.0)
    return {
        "portfolio_id": portfolio.id,
        "cash_balance": round(float(portfolio.cash_balance or 0.0), 2),
        "initial_cash": round(initial_cash, 2),
        "market_value": round(market_value, 2),
        "equity": round(equity, 2),
        "total_return_pct": round(((equity - initial_cash) / initial_cash * 100.0), 2) if initial_cash > 0 else None,
        "positions": [
            {
                "symbol": p.symbol,
                "name": p.security_name,
                "quantity": float(p.quantity or 0.0),
                "avg_cost": round(float(p.avg_cost or 0.0), 4),
                "last_price": _resolve_quote_price(quotes.get(p.symbol) or {}),
            }
            for p in positions
        ],
    }


def bootstrap_from_imported_positions(
    db: Session,
    *,
    user_id: str,
    initial_cash: float | None = None,
    source: str | None = None,
    reset_existing: bool = True,
) -> dict[str, Any]:
    imported_q = db.query(ImportedPortfolioPositionDB).filter(ImportedPortfolioPositionDB.user_id == user_id)
    if source:
        imported_q = imported_q.filter(ImportedPortfolioPositionDB.source == source)
    imported = imported_q.all()
    if not imported:
        raise ValueError("暂无可导入的持仓数据")

    invested = 0.0
    normalized_rows: list[dict[str, Any]] = []
    for row in imported:
        symbol = _normalize_symbol(row.symbol)
        qty = _to_float(row.current_position) or 0.0
        cost = _to_float(row.average_cost) or 0.0
        if not symbol or qty <= 0 or cost <= 0:
            continue
        invested += qty * cost
        normalized_rows.append(
            {
                "symbol": symbol,
                "name": (row.security_name or symbol).strip(),
                "quantity": qty,
                "avg_cost": cost,
            }
        )
    if not normalized_rows:
        raise ValueError("导入数据缺少有效的持仓数量和成本价")

    resolved_initial_cash = float(initial_cash if initial_cash is not None else max(invested * 1.2, invested))
    portfolio = _ensure_portfolio(db, user_id, initial_cash=resolved_initial_cash)
    if reset_existing:
        db.query(PaperPositionDB).filter(PaperPositionDB.portfolio_id == portfolio.id).delete()
        db.query(PaperTradeDB).filter(PaperTradeDB.portfolio_id == portfolio.id).delete()
        portfolio.total_realized_pnl = 0.0
        portfolio.total_fees = 0.0
    portfolio.initial_cash = resolved_initial_cash
    portfolio.cash_balance = max(0.0, resolved_initial_cash - invested)
    portfolio.updated_at = _utc_now()
    db.flush()

    for item in normalized_rows:
        db.add(
            PaperPositionDB(
                id=str(uuid4()),
                portfolio_id=portfolio.id,
                symbol=item["symbol"],
                security_name=item["name"],
                quantity=item["quantity"],
                avg_cost=item["avg_cost"],
            )
        )
    db.commit()
    return get_portfolio_snapshot(db, user_id)


def _resolve_trade_price(db: Session, symbol: str, explicit_price: float | None) -> float:
    if explicit_price is not None and explicit_price > 0:
        return float(explicit_price)
    quotes = _fetch_quotes([symbol])
    live = _resolve_quote_price(quotes.get(symbol) or {})
    if live and live > 0:
        return float(live)
    raise ValueError(f"{symbol} 无可用行情价格，无法成交")


def execute_trade(
    db: Session,
    *,
    user_id: str,
    symbol: str,
    security_name: str | None = None,
    side: str,
    quantity: float,
    price: float | None = None,
    fee_rate: float = 0.0003,
    min_commission: float = MIN_COMMISSION,
    reason: str | None = None,
    trade_date: str | None = None,
) -> dict[str, Any]:
    norm_symbol = _normalize_symbol(symbol)
    if not norm_symbol:
        raise ValueError("symbol 格式错误")
    side = str(side or "").upper()
    if side not in {"BUY", "SELL"}:
        raise ValueError("side 仅支持 BUY/SELL")
    qty = float(quantity)
    if qty <= 0:
        raise ValueError("quantity 必须大于 0")

    portfolio = _ensure_portfolio(db, user_id)
    position = (
        db.query(PaperPositionDB)
        .filter(PaperPositionDB.portfolio_id == portfolio.id, PaperPositionDB.symbol == norm_symbol)
        .first()
    )
    if position is None:
        position = PaperPositionDB(
            id=str(uuid4()),
            portfolio_id=portfolio.id,
            symbol=norm_symbol,
            security_name=(security_name or norm_symbol).strip(),
            quantity=0.0,
            avg_cost=0.0,
        )
        db.add(position)
        db.flush()
    elif security_name and (not position.security_name or position.security_name == norm_symbol):
        position.security_name = security_name.strip()

    deal_price = _resolve_trade_price(db, norm_symbol, price)
    gross = qty * deal_price
    # A 股真实成本：佣金（含最低收费）+ 印花税（仅卖出）+ 过户费。
    commission = max(gross * max(0.0, float(fee_rate)), float(min_commission)) if gross > 0 else 0.0
    stamp_tax = gross * STAMP_TAX_RATE if side == "SELL" else 0.0
    transfer_fee = gross * TRANSFER_FEE_RATE
    fee = commission + stamp_tax + transfer_fee
    realized_pnl = 0.0

    if side == "BUY":
        total_cost = gross + fee
        if float(portfolio.cash_balance or 0.0) + 1e-9 < total_cost:
            raise ValueError("可用资金不足")
        old_qty = float(position.quantity or 0.0)
        old_cost = float(position.avg_cost or 0.0)
        new_qty = old_qty + qty
        position.avg_cost = ((old_qty * old_cost) + gross + fee) / new_qty
        position.quantity = new_qty
        portfolio.cash_balance = float(portfolio.cash_balance or 0.0) - total_cost
    else:
        old_qty = float(position.quantity or 0.0)
        if old_qty + 1e-9 < qty:
            raise ValueError("卖出数量超过当前持仓")
        old_cost = float(position.avg_cost or 0.0)
        proceeds = gross - fee
        realized_pnl = (deal_price - old_cost) * qty - fee
        position.quantity = old_qty - qty
        if position.quantity <= 1e-9:
            position.quantity = 0.0
            position.avg_cost = 0.0
        portfolio.cash_balance = float(portfolio.cash_balance or 0.0) + proceeds
        portfolio.total_realized_pnl = float(portfolio.total_realized_pnl or 0.0) + realized_pnl

    portfolio.total_fees = float(portfolio.total_fees or 0.0) + fee
    now = _utc_now()
    portfolio.updated_at = now
    position.updated_at = now

    trade = PaperTradeDB(
        id=str(uuid4()),
        portfolio_id=portfolio.id,
        user_id=user_id,
        symbol=norm_symbol,
        side=side,
        quantity=qty,
        price=deal_price,
        fee=fee,
        gross_amount=gross,
        realized_pnl=realized_pnl if side == "SELL" else None,
        reason=(reason or "").strip() or None,
        trade_date=(trade_date or now.date().isoformat()),
    )
    db.add(trade)
    db.commit()
    return {
        "trade_id": trade.id,
        "symbol": norm_symbol,
        "name": position.security_name,
        "side": side,
        "quantity": qty,
        "price": round(deal_price, 4),
        "fee": round(fee, 4),
        "realized_pnl": round(realized_pnl, 4) if side == "SELL" else None,
    }


def _plan_driven_action(
    db: Session,
    *,
    user_id: str,
    symbol: str,
    name: str,
    quantity: float,
    avg_cost: float,
    live_price: float,
    pnl_pct: float,
    quote: dict[str, Any],
    trade_date: str | None = None,
) -> dict[str, Any] | None:
    """按该标的自己那份交易计划给出建议；无可用计划时返回 None 由调用方兜底。

    历史实现只用 -6% / +10% 两个全局阈值，且与研报结论无关：一只日波动 3% 的
    银行股和一只日波动 9% 的题材股被套用同一条止损线。这里改为优先使用研报
    自己给出的硬止损、分批止盈、移动止盈与时间止损。
    """
    try:
        from api.services import exit_engine_service, trade_plan_service

        plan_row = trade_plan_service.get_monitor_plan(db, user_id=user_id, symbol=symbol)
        plan = trade_plan_service.plan_dict_from_row(plan_row) if plan_row else None
        if not plan or not trade_plan_service.is_monitorable(plan):
            return None

        decision = exit_engine_service.evaluate_holding(
            symbol=symbol,
            name=name,
            position=quantity,
            available_position=quantity,
            average_cost=avg_cost,
            price=live_price,
            previous_close=_to_float(quote.get("previous_close")),
            change_pct=_to_float(quote.get("change_pct")),
            plan=plan,
            plan_id=getattr(plan_row, "id", None),
            plan_source=getattr(plan_row, "source", None),
            # 传 today 才能按交易日推算持有天数，否则时间止损永不触发
            today=trade_date,
        )
    except Exception as exc:  # 引擎异常时退回通用阈值，绝不影响主流程
        logger.warning("[paper-plan] 计划驱动评估失败 symbol=%s: %s", symbol, exc)
        return None

    # 方向/锚点前置校验（退出引擎规则 0）：清仓/减仓只有在计划方向明确为多头、
    # 价格锚点自洽时才有意义。否则那条"止损位"可能是看空计划的失效价，据此生成
    # 的模拟卖单会污染模拟盘业绩。此时只记录不生成卖单。
    if decision.action in ("清仓", "减仓") and not decision.is_actionable_sell:
        logger.info(
            "[paper-plan] 已抑制卖单（方向校验未通过）symbol=%s action=%s gate=%s",
            symbol, decision.action, decision.anchor_gate,
        )
        action_name, sell_qty = "HOLD", 0.0
    elif decision.action == "清仓":
        action_name, sell_qty = "SELL", quantity
    elif decision.action == "减仓":
        action_name = "SELL"
        sell_qty = float(decision.suggested_shares or 0.0)
        if sell_qty <= 0:
            sell_qty = round(max(1.0, quantity * max(1.0, float(decision.suggested_pct or 0.0)) / 100.0), 2)
    else:
        action_name, sell_qty = "HOLD", 0.0

    reasons = "；".join(decision.reasons) or "未触及计划中的止损/止盈条件"
    return {
        "symbol": symbol,
        "name": name,
        "action": action_name,
        "quantity": round(sell_qty, 2) if action_name == "SELL" else 0.0,
        "price": live_price,
        "reason": f"{reasons}（浮动 {pnl_pct:.2f}%）",
        "plan_triggered": list(decision.triggers),
        "plan_source": decision.plan_source,
        "plan_direction": decision.plan_direction,
        "anchor_gate": decision.anchor_gate,
    }


def generate_daily_operation_plan(
    db: Session,
    *,
    user_id: str,
    trade_date: str,
    include_recommendations: bool = True,
    recommendation_top_k: int = 3,
) -> dict[str, Any]:
    from api.services import recommendation_service

    portfolio = _ensure_portfolio(db, user_id)
    positions = _snapshot_positions(db, portfolio.id)
    symbols = [item.symbol for item in positions]
    quotes = _fetch_quotes(symbols)

    actions: list[dict[str, Any]] = []
    holding_symbols: set[str] = set()
    for pos in positions:
        holding_symbols.add(pos.symbol)
        qty = float(pos.quantity or 0.0)
        avg_cost = float(pos.avg_cost or 0.0)
        live_price = _resolve_quote_price(quotes.get(pos.symbol) or {})
        if qty <= 0 or avg_cost <= 0 or live_price is None:
            actions.append(
                {
                    "symbol": pos.symbol,
                    "name": pos.security_name or pos.symbol,
                    "action": "HOLD",
                    "quantity": 0.0,
                    "price": live_price,
                    "reason": "行情或持仓信息不足，保持观察",
                }
            )
            continue
        pnl_pct = (live_price - avg_cost) / avg_cost * 100.0

        plan_hint = _plan_driven_action(
            db,
            user_id=user_id,
            symbol=pos.symbol,
            name=pos.security_name or pos.symbol,
            quantity=qty,
            avg_cost=avg_cost,
            live_price=live_price,
            pnl_pct=pnl_pct,
            quote=quotes.get(pos.symbol) or {},
            trade_date=trade_date,
        )
        if plan_hint is not None:
            actions.append(plan_hint)
            continue

        # 兜底：该标的没有可用的交易计划时，沿用通用阈值，避免完全无提示。
        if pnl_pct <= -6.0:
            actions.append(
                {
                    "symbol": pos.symbol,
                    "name": pos.security_name or pos.symbol,
                    "action": "SELL",
                    "quantity": round(qty, 2),
                    "price": live_price,
                    "reason": f"触发止损阈值({pnl_pct:.2f}%)",
                }
            )
        elif pnl_pct >= 10.0:
            actions.append(
                {
                    "symbol": pos.symbol,
                    "name": pos.security_name or pos.symbol,
                    "action": "SELL",
                    "quantity": round(max(1.0, qty * 0.3), 2),
                    "price": live_price,
                    "reason": f"达到止盈区间({pnl_pct:.2f}%)，分批落袋",
                }
            )
        elif pnl_pct >= 4.0:
            actions.append(
                {
                    "symbol": pos.symbol,
                    "name": pos.security_name or pos.symbol,
                    "action": "HOLD",
                    "quantity": 0.0,
                    "price": live_price,
                    "reason": f"趋势偏强({pnl_pct:.2f}%)，继续持有",
                }
            )
        else:
            actions.append(
                {
                    "symbol": pos.symbol,
                    "name": pos.security_name or pos.symbol,
                    "action": "HOLD",
                    "quantity": 0.0,
                    "price": live_price,
                    "reason": f"波动中性({pnl_pct:.2f}%)，等待确认",
                }
            )

    if include_recommendations and float(portfolio.cash_balance or 0.0) > 0:
        rec = recommendation_service.recommend_for_user(
            db,
            user_id=user_id,
            top_k=max(1, min(int(recommendation_top_k), 10)),
            include_tracking=True,
            include_watchlist=True,
            source_mode="market_scan",
            min_change_pct=-1.0,
        )
        candidates = [item for item in (rec.get("items") or []) if str(item.get("symbol") or "").upper() not in holding_symbols]
        buy_budget = float(portfolio.cash_balance or 0.0) * 0.5
        for item in candidates[:2]:
            symbol = str(item.get("symbol") or "").upper()
            price = _to_float(item.get("live_price"))
            if not symbol or not price or price <= 0:
                continue
            each_budget = buy_budget / max(1, min(2, len(candidates)))
            quantity = int(each_budget // price)
            if quantity <= 0:
                continue
            actions.append(
                {
                    "symbol": symbol,
                    "name": str(item.get("name") or symbol),
                    "action": "BUY",
                    "quantity": float(quantity),
                    "price": price,
                    "reason": f"推荐池入选(score={_to_float(item.get('score')) or 0:.1f})，补充仓位",
                }
            )

    return {
        "trade_date": trade_date,
        "generated_at": _utc_now().isoformat(),
        "cash_balance": round(float(portfolio.cash_balance or 0.0), 2),
        "actions": actions,
    }


def create_daily_review(db: Session, *, user_id: str, trade_date: str) -> dict[str, Any]:
    portfolio = _ensure_portfolio(db, user_id)
    positions = _snapshot_positions(db, portfolio.id)
    quotes = _fetch_quotes([item.symbol for item in positions])
    market_value = _portfolio_market_value(positions, quotes)
    equity = float(portfolio.cash_balance or 0.0) + market_value
    initial_cash = float(portfolio.initial_cash or 0.0)
    total_return_pct = ((equity - initial_cash) / initial_cash * 100.0) if initial_cash > 0 else 0.0

    day_trades = (
        db.query(PaperTradeDB)
        .filter(PaperTradeDB.user_id == user_id, PaperTradeDB.trade_date == trade_date)
        .order_by(PaperTradeDB.created_at.desc())
        .all()
    )
    day_realized = sum(float(item.realized_pnl or 0.0) for item in day_trades)
    day_fees = sum(float(item.fee or 0.0) for item in day_trades)

    summary = {
        "trade_date": trade_date,
        "equity": round(equity, 2),
        "market_value": round(market_value, 2),
        "cash_balance": round(float(portfolio.cash_balance or 0.0), 2),
        "total_return_pct": round(total_return_pct, 2),
        "day_realized_pnl": round(day_realized, 2),
        "day_fees": round(day_fees, 2),
        "trade_count": len(day_trades),
        "positions": [
            {
                "symbol": pos.symbol,
                "name": pos.security_name,
                "quantity": float(pos.quantity or 0.0),
                "avg_cost": round(float(pos.avg_cost or 0.0), 4),
                "last_price": _resolve_quote_price(quotes.get(pos.symbol) or {}),
            }
            for pos in positions
        ],
    }

    row = (
        db.query(DailyReviewDB)
        .filter(DailyReviewDB.user_id == user_id, DailyReviewDB.trade_date == trade_date)
        .first()
    )
    if row is None:
        row = DailyReviewDB(id=str(uuid4()), user_id=user_id, trade_date=trade_date)
        db.add(row)
    row.portfolio_id = portfolio.id
    row.summary_json = summary
    row.updated_at = _utc_now()
    db.commit()
    return summary
