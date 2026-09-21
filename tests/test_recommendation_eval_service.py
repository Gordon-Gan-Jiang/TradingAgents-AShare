from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.database import Base, MarketScanResultDB
from api.services import recommendation_eval_service as svc


def _mk_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def test_refresh_eval_run_builds_summary_and_gate(monkeypatch):
    db = _mk_db()
    now = datetime.now(timezone.utc) - timedelta(days=10)

    def _mk_row(pid: str, profile: str, sym: str, rank: int, ret: float):
        return MarketScanResultDB(
            id=f"{pid}-{sym}",
            run_id=pid,
            user_id="u1",
            source_mode="market_scan",
            market="cn",
            score_profile=profile,
            symbol=sym,
            name=sym,
            rank=rank,
            score=80.0 - rank,
            feedback_horizon_days=5,
            feedback_status="evaluated",
            realized_return_pct=ret,
            created_at=now + timedelta(minutes=rank),
            updated_at=now + timedelta(minutes=rank),
        )

    db.add_all(
        [
            _mk_row("r1", "ashare_balanced", "600519.SH", 1, 1.2),
            _mk_row("r1", "ashare_balanced", "000001.SZ", 2, -0.4),
            _mk_row("r2", "ashare_aggressive", "600519.SH", 1, 2.1),
            _mk_row("r2", "ashare_aggressive", "300750.SZ", 2, 0.8),
        ]
    )
    db.commit()

    csv_out = (
        "# comment\n"
        "Date,Open,High,Low,Close,Volume\n"
        "2026-05-01,1,1,1,100,1\n"
        "2026-05-02,1,1,1,101,1\n"
        "2026-05-03,1,1,1,102,1\n"
        "2026-05-04,1,1,1,103,1\n"
        "2026-05-05,1,1,1,104,1\n"
        "2026-05-06,1,1,1,105,1\n"
    )
    monkeypatch.setattr("api.services.recommendation_eval_service.route_to_vendor", lambda *_a, **_k: csv_out)

    out = svc.refresh_eval_run(
        db,
        user_id="u1",
        baseline_profile="ashare_balanced",
        variant_profile="ashare_aggressive",
        lookback_days=60,
        top_k=2,
    )
    assert out["status"] == "completed"
    assert out["summary"]["groups"]["baseline"]["sample_count"] >= 1
    assert out["summary"]["groups"]["variant"]["sample_count"] >= 1
    assert "allow_switch_default" in out["gate"]
    assert "poor_tradability_rate_pct" in out["summary"]["groups"]["baseline"]
    assert "high_impact_risk_rate_pct" in out["summary"]["groups"]["variant"]
    assert "max_tradability_risk_diff_pct" in out["gate"]["thresholds"]

