from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.database import Base, ImportedPortfolioPositionDB, UserDB, UserLLMConfigDB, WatchlistItemDB
from api.services import auth_service
from api.services.recommendation_service import recommend_for_user, run_scheduled_recommendation_pushes


def _mk_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def test_recommend_for_user_from_tracking_and_watchlist():
    db = _mk_db()
    db.add(
        ImportedPortfolioPositionDB(
            id="p1",
            user_id="u1",
            source="manual",
            symbol="600519.SH",
            security_name="贵州茅台",
            current_position=100,
            available_position=100,
            average_cost=1700,
            market_value=170000,
            current_position_pct=50,
        )
    )
    db.add(
        WatchlistItemDB(
            id="w1",
            user_id="u1",
            symbol="300750.SZ",
            sort_order=1,
        )
    )
    db.commit()

    fake_quotes = {
        "600519.SH": {"price": 1720, "change_pct": 2.5, "high": 1725, "amount": 2_000_000_000, "volume": 3_000_000},
        "300750.SZ": {"price": 201, "change_pct": 1.2, "high": 202, "amount": 800_000_000, "volume": 2_500_000},
    }
    with patch("api.services.daily_stock_analysis_service.route_to_vendor", return_value=str(fake_quotes).replace("'", '"')):
        out = recommend_for_user(
            db,
            user_id="u1",
            top_k=2,
            include_tracking=True,
            include_watchlist=True,
        )

    assert out["pool_size"] == 2
    assert out["scored_size"] == 2
    assert len(out["items"]) == 2
    assert out["items"][0]["symbol"] == "600519.SH"
    assert out["items"][0]["code"] == "600519.SH"
    assert out["scoring_model"]["market"] == "cn"
    assert out["items"][0]["score"] >= out["items"][1]["score"]


def test_recommend_for_user_supports_seed_symbols():
    db = _mk_db()
    fake_quotes = {
        "000001.SZ": {"price": 12.3, "change_pct": 0.8, "high": 12.4, "amount": 300_000_000, "volume": 1_000_000},
    }
    with patch("api.services.daily_stock_analysis_service.route_to_vendor", return_value=str(fake_quotes).replace("'", '"')):
        out = recommend_for_user(
            db,
            user_id="u1",
            top_k=3,
            include_tracking=False,
            include_watchlist=False,
            seed_symbols=["000001"],
        )
    assert out["pool_size"] == 1
    assert out["items"][0]["symbol"] == "000001.SZ"
    assert out["items"][0]["code"] == "000001.SZ"


def test_recommend_for_user_supports_custom_weights():
    db = _mk_db()
    fake_quotes = {
        "000001.SZ": {"price": 12.3, "change_pct": 5.2, "high": 12.4, "amount": 300_000_000, "volume": 1_000_000},
        "600519.SH": {"price": 1720, "change_pct": 1.0, "high": 1800, "amount": 2_000_000_000, "volume": 2_000_000},
    }
    with patch("api.services.daily_stock_analysis_service.route_to_vendor", return_value=str(fake_quotes).replace("'", '"')):
        out = recommend_for_user(
            db,
            user_id="u1",
            top_k=2,
            include_tracking=False,
            include_watchlist=False,
            seed_symbols=["000001", "600519"],
            market="cn",
            profile="ashare_aggressive",
            momentum_weight=0.8,
            activity_weight=0.1,
            near_high_weight=0.1,
        )
    weights = out["scoring_model"]["weights"]
    assert abs((weights["momentum"] + weights["activity"] + weights["near_high"]) - 1.0) < 1e-6
    assert out["items"][0]["symbol"] == "000001.SZ"


def test_recommend_for_user_supports_market_scan_source():
    db = _mk_db()
    universe = {
        "000001.SZ": "平安银行",
        "600519.SH": "贵州茅台",
        "300750.SZ": "宁德时代",
    }
    fake_quotes = {
        "000001.SZ": {"price": 12.3, "change_pct": 2.1, "high": 12.35, "amount": 300_000_000, "volume": 1_000_000},
        "600519.SH": {"price": 1720, "change_pct": 6.2, "high": 1723, "amount": 2_000_000_000, "volume": 2_000_000},
        "300750.SZ": {"price": 220, "change_pct": 1.3, "high": 223, "amount": 600_000_000, "volume": 1_500_000},
    }
    with patch("api.services.market_scanner_service._load_cn_universe", return_value=universe), \
         patch("api.services.market_scanner_service.route_to_vendor", return_value=str(fake_quotes).replace("'", '"')):
        out = recommend_for_user(
            db,
            user_id="u1",
            top_k=2,
            source_mode="market_scan",
            market="cn",
            scan_limit=300,
            min_amount=200_000_000,
        )
    assert out["pool_size"] == 3
    assert out["items"][0]["symbol"] == "600519.SH"
    assert "strategy_hits" in out["items"][0]
    assert out["scoring_model"]["engine"] == "market_scan_v3"
    assert "return_score" in out["items"][0]["score_breakdown"]
    assert "quality_score" in out["items"][0]["score_breakdown"]
    assert "tradability_score" in out["items"][0]["score_breakdown"]


def test_recommend_for_user_market_scan_filters_near_limit_up():
    db = _mk_db()
    universe = {
        "000001.SZ": "平安银行",
        "600519.SH": "贵州茅台",
    }
    fake_quotes = {
        "000001.SZ": {"price": 12.3, "change_pct": 9.9, "high": 12.35, "amount": 900_000_000, "volume": 1_000_000},
        "600519.SH": {"price": 1720, "change_pct": 4.2, "high": 1723, "amount": 2_000_000_000, "volume": 2_000_000},
    }
    with patch("api.services.market_scanner_service._load_cn_universe", return_value=universe), \
         patch("api.services.market_scanner_service.route_to_vendor", return_value=str(fake_quotes).replace("'", '"')):
        out = recommend_for_user(
            db,
            user_id="u1",
            top_k=2,
            source_mode="market_scan",
            market="cn",
            scan_limit=300,
            min_amount=200_000_000,
            limit_up_threshold_pct=9.6,
        )
    assert out["pool_size"] == 2
    assert len(out["items"]) == 1
    assert out["items"][0]["symbol"] == "600519.SH"


def test_run_scheduled_recommendation_pushes_send_wecom_and_wps():
    db = _mk_db()
    db.add(
        UserDB(
            id="u1",
            email="u1@test.com",
            is_active=True,
            wecom_report_enabled=True,
            wps_report_enabled=True,
        )
    )
    db.add(
        UserLLMConfigDB(
            user_id="u1",
            wecom_webhook_encrypted=auth_service.encrypt_secret("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"),
            wps_webhook_encrypted=auth_service.encrypt_secret("https://365.kdocs.cn/woa/api/v1/webhook/send?key=xyz"),
        )
    )
    db.add(
        ImportedPortfolioPositionDB(
            id="p1",
            user_id="u1",
            source="manual",
            symbol="600519.SH",
            security_name="贵州茅台",
            current_position=100,
            market_value=170000,
        )
    )
    db.commit()

    fake_quotes = {
        "600519.SH": {"price": 1720, "change_pct": 1.8, "high": 1725, "amount": 1200000000, "volume": 1800000}
    }
    with patch("api.services.recommendation_service.cn_today_str", return_value="2026-04-10"), \
         patch("api.services.recommendation_service.is_cn_trading_day", return_value=True), \
         patch("api.services.recommendation_service._in_push_window", return_value="close"), \
         patch("api.services.daily_stock_analysis_service.route_to_vendor", return_value=str(fake_quotes).replace("'", '"')), \
         patch("api.services.recommendation_service.send_message", return_value=True) as m_wecom, \
         patch("api.services.recommendation_service.send_markdown_message", return_value=True) as m_wps:
        run_scheduled_recommendation_pushes(db)

    assert m_wecom.call_count == 1
    assert m_wps.call_count == 1
