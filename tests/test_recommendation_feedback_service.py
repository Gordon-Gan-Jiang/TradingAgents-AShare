from datetime import datetime, timedelta, timezone
import sqlite3

from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from api.database import Base
from api.services import recommendation_feedback_service as svc


def _mk_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def test_persist_and_aggregate_strategy_feedback(monkeypatch):
    db = _mk_db()
    svc.persist_scan_results(
        db,
        user_id="u1",
        run_id="run1",
        source_mode="market_scan",
        market="cn",
        score_profile="ashare_balanced",
        items=[
            {
                "symbol": "600519.SH",
                "name": "贵州茅台",
                "score": 88.5,
                "reasons": ["涨幅强势"],
                "strategy_hits": ["strong_momentum", "active_liquidity"],
                "risk_flags": [],
                "score_breakdown": {"momentum": 90.0},
                "live_price": 1700.0,
            },
            {
                "symbol": "300750.SZ",
                "name": "宁德时代",
                "score": 82.1,
                "reasons": ["逼近日高"],
                "strategy_hits": ["breakout_near_high"],
                "risk_flags": [],
                "score_breakdown": {"near_high": 85.0},
                "live_price": 200.0,
            },
        ],
        selected_symbols=["600519.SH"],
        feedback_horizon_days=5,
    )
    for row in db.query(svc.MarketScanResultDB).all():
        row.created_at = datetime.now(timezone.utc) - timedelta(days=10)
    db.commit()

    monkeypatch.setattr(svc, "_get_price_after", lambda symbol, base_date, hold_days: 1800.0 if symbol == "600519.SH" else 190.0)
    result = svc.evaluate_pending_feedback(db, user_id="u1", limit=10)
    assert result["evaluated"] == 2

    refreshed = svc.refresh_strategy_feedback_stats(db, user_id="u1")
    assert refreshed["strategies"] >= 2
    assert "tradability" in refreshed

    stats = svc.list_strategy_feedback_stats(db, user_id="u1")
    assert any(item["strategy_key"] == "strong_momentum" for item in stats)

    learned, info = svc.build_learning_weight_adjustment(
        db,
        user_id="u1",
        base_weights={"momentum": 0.30, "activity": 0.25, "near_high": 0.20, "sector": 0.15, "volume_ratio": 0.10},
    )
    assert info["applied"] is False or isinstance(info["applied"], bool)
    assert abs(sum(learned.values()) - 1.0) < 1e-6


def test_get_price_after_parses_provider_csv_with_hash_comments(monkeypatch):
    """AkShare/BaoStock get_stock_data returns # comment lines; parser must use comment='#'."""
    csv_out = (
        "# Stock data for 600519.SH from 2026-04-10 to 2026-04-15\n"
        "# Total records: 2\n"
        "# Data retrieved on: 2026-04-13 12:00:00\n\n"
        "Date,Open,High,Low,Close,Volume,Dividends,Stock Splits\n"
        "2026-04-10,1,1,1,100.0,1,0.0,0.0\n"
        "2026-04-11,1,1,1,105.5,1,0.0,0.0\n"
    )

    def _fake_route(_method: str, _symbol: str, _start: str, _end: str) -> str:
        return csv_out

    monkeypatch.setattr("api.services.recommendation_feedback_service.route_to_vendor", _fake_route)
    # base_date 2026-04-09 → fetch from 2026-04-10; hold_days=2 → second row close
    price = svc._get_price_after("600519.SH", "2026-04-09", 2)
    assert price == 105.5


def test_refresh_feedback_pipeline_retries_database_locked(monkeypatch):
    db = _mk_db()
    attempts = {"n": 0}

    def _fake_eval(_db, *, user_id=None, limit=80):
        return {"processed": 0, "evaluated": 0, "skipped": 0}

    def _fake_refresh(_db, *, user_id=None):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise OperationalError(
                "DELETE FROM strategy_feedback_stats WHERE user_id=?",
                ("u1",),
                sqlite3.OperationalError("database is locked"),
            )
        return {"strategies": 1, "users": 1}

    monkeypatch.setattr(svc, "evaluate_pending_feedback", _fake_eval)
    monkeypatch.setattr(svc, "refresh_strategy_feedback_stats", _fake_refresh)

    out = svc.refresh_feedback_pipeline(db, user_id="u1", eval_limit=10, retries=3)
    assert out["refreshed"]["strategies"] == 1
    assert attempts["n"] == 3
