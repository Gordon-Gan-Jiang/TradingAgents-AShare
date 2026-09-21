"""Tests for multi-model same-day consensus T+1 accuracy analytics."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from api.database import ReportDB, ReportT1OutcomeDB, UserDB, get_db_ctx, init_db
from api.services import model_arena_service, model_profile_service


@pytest.fixture(autouse=True)
def _init_db():
    init_db()


def _seed_user(db) -> str:
    user_id = str(uuid4())
    db.add(
        UserDB(
            id=user_id,
            email=f"consensus-{user_id[:8]}@test.com",
            is_active=True,
        )
    )
    db.commit()
    return user_id


def _add_report_with_outcome(
    db,
    *,
    user_id: str,
    profile: dict,
    symbol: str,
    signal_day: str,
    t1_day: str,
    direction_bucket: str,
    p0: float,
    p1: float,
    created_at: datetime | None = None,
) -> tuple[str, str]:
    now = created_at or datetime.now(timezone.utc)
    report_id = uuid4().hex
    ret = round((p1 - p0) / p0 * 100.0, 4)
    label = ret > 0 if direction_bucket == "bullish" else ret < 0 if direction_bucket == "bearish" else None
    db.add(
        ReportDB(
            id=report_id,
            user_id=user_id,
            symbol=symbol,
            trade_date=signal_day,
            status="completed",
            decision="BUY" if direction_bucket == "bullish" else "SELL",
            direction=direction_bucket,
            result_data={
                "model_info": {
                    "model_profile_id": profile["id"],
                    "model_profile_name": profile["name"],
                    "llm_provider": profile["llm_provider"],
                    "quick_think_llm": profile["quick_think_llm"],
                    "deep_think_llm": profile["deep_think_llm"],
                    "backend_url": profile["backend_url"],
                }
            },
            created_at=now,
            updated_at=now,
        )
    )
    db.add(
        ReportT1OutcomeDB(
            id=uuid4().hex,
            user_id=user_id,
            report_id=report_id,
            symbol=symbol,
            signal_trade_date=signal_day,
            t1_trade_date=t1_day,
            p0=p0,
            p1=p1,
            return_t1_pct=ret,
            direction_bucket=direction_bucket,
            label_correct=label,
            status="evaluated",
            evaluated_at=now,
            created_at=now,
            updated_at=now,
        )
    )
    return report_id, profile["id"]


def test_unanimous_bullish_consensus_counts_as_correct():
    signal_day = "2026-05-20"
    t1_day = "2026-05-21"
    with get_db_ctx() as db:
        user_id = _seed_user(db)
        p1 = model_profile_service.create_model_profile(
            db,
            user_id=user_id,
            name=f"P1-{uuid4().hex[:6]}",
            llm_provider="openai",
            quick_think_llm="model-alpha",
            deep_think_llm="model-alpha",
            backend_url="https://example.com/v1",
            is_default=True,
            is_active=True,
        )
        p2 = model_profile_service.create_model_profile(
            db,
            user_id=user_id,
            name=f"P2-{uuid4().hex[:6]}",
            llm_provider="openai",
            quick_think_llm="model-beta",
            deep_think_llm="model-beta",
            backend_url="https://example.com/v1",
            is_default=False,
            is_active=True,
        )
        _add_report_with_outcome(
            db,
            user_id=user_id,
            profile=p1,
            symbol="600519.SH",
            signal_day=signal_day,
            t1_day=t1_day,
            direction_bucket="bullish",
            p0=100.0,
            p1=102.0,
        )
        _add_report_with_outcome(
            db,
            user_id=user_id,
            profile=p2,
            symbol="600519.SH",
            signal_day=signal_day,
            t1_day=t1_day,
            direction_bucket="bullish",
            p0=100.0,
            p1=102.0,
        )
        db.commit()

        trend = model_arena_service.build_multi_model_consensus_t1_trend(
            db,
            user_id=user_id,
            start_date=signal_day,
            end_date=signal_day,
            scope="all",
            min_models=2,
        )
        assert len(trend["series"]) == 1
        point = trend["series"][0]
        assert point["date"] == signal_day
        assert point["unanimous_bullish_count"] == 1
        assert point["unanimous_bullish_accuracy_pct"] == 100.0
        assert point["unanimous_bearish_count"] == 0

        detail = model_arena_service.build_multi_model_consensus_t1_detail(
            db,
            user_id=user_id,
            date=signal_day,
            scope="all",
            min_models=2,
        )
        assert len(detail["items"]) == 1
        item = detail["items"][0]
        assert item["symbol"] == "600519.SH"
        assert item["consensus_direction"] == "bullish"
        assert item["model_count"] == 2
        assert item["label_correct"] is True
        assert "name" in item


def test_mixed_directions_do_not_form_consensus():
    signal_day = "2026-05-22"
    t1_day = "2026-05-23"
    with get_db_ctx() as db:
        user_id = _seed_user(db)
        p1 = model_profile_service.create_model_profile(
            db,
            user_id=user_id,
            name=f"P1-{uuid4().hex[:6]}",
            llm_provider="openai",
            quick_think_llm="model-alpha",
            deep_think_llm="model-alpha",
            backend_url="https://example.com/v1",
            is_default=True,
            is_active=True,
        )
        p2 = model_profile_service.create_model_profile(
            db,
            user_id=user_id,
            name=f"P2-{uuid4().hex[:6]}",
            llm_provider="openai",
            quick_think_llm="model-beta",
            deep_think_llm="model-beta",
            backend_url="https://example.com/v1",
            is_default=False,
            is_active=True,
        )
        _add_report_with_outcome(
            db,
            user_id=user_id,
            profile=p1,
            symbol="000001.SZ",
            signal_day=signal_day,
            t1_day=t1_day,
            direction_bucket="bullish",
            p0=10.0,
            p1=10.5,
        )
        _add_report_with_outcome(
            db,
            user_id=user_id,
            profile=p2,
            symbol="000001.SZ",
            signal_day=signal_day,
            t1_day=t1_day,
            direction_bucket="bearish",
            p0=10.0,
            p1=10.5,
        )
        db.commit()

        trend = model_arena_service.build_multi_model_consensus_t1_trend(
            db,
            user_id=user_id,
            start_date=signal_day,
            end_date=signal_day,
            scope="all",
            min_models=2,
        )
        assert trend["series"] == []


def test_duplicate_model_reports_deduped_before_consensus():
    signal_day = "2026-05-24"
    t1_day = "2026-05-27"
    with get_db_ctx() as db:
        user_id = _seed_user(db)
        profile = model_profile_service.create_model_profile(
            db,
            user_id=user_id,
            name=f"P1-{uuid4().hex[:6]}",
            llm_provider="openai",
            quick_think_llm="model-alpha",
            deep_think_llm="model-alpha",
            backend_url="https://example.com/v1",
            is_default=True,
            is_active=True,
        )
        early = datetime(2026, 5, 24, 10, 0, tzinfo=timezone.utc)
        late = datetime(2026, 5, 24, 16, 0, tzinfo=timezone.utc)
        _add_report_with_outcome(
            db,
            user_id=user_id,
            profile=profile,
            symbol="600036.SH",
            signal_day=signal_day,
            t1_day=t1_day,
            direction_bucket="bearish",
            p0=20.0,
            p1=19.0,
            created_at=early,
        )
        _add_report_with_outcome(
            db,
            user_id=user_id,
            profile=profile,
            symbol="600036.SH",
            signal_day=signal_day,
            t1_day=t1_day,
            direction_bucket="bearish",
            p0=20.0,
            p1=19.0,
            created_at=late,
        )
        db.commit()

        trend = model_arena_service.build_multi_model_consensus_t1_trend(
            db,
            user_id=user_id,
            start_date=signal_day,
            end_date=signal_day,
            scope="all",
            min_models=2,
        )
        assert trend["series"] == []


def test_unanimous_bearish_consensus_in_daily_aggregate():
    signal_day = "2026-05-25"
    t1_day = "2026-05-26"
    with get_db_ctx() as db:
        user_id = _seed_user(db)
        profiles = []
        for idx, model_name in enumerate(("model-x", "model-y")):
            profiles.append(
                model_profile_service.create_model_profile(
                    db,
                    user_id=user_id,
                    name=f"P-{idx}-{uuid4().hex[:4]}",
                    llm_provider="openai",
                    quick_think_llm=model_name,
                    deep_think_llm=model_name,
                    backend_url="https://example.com/v1",
                    is_default=idx == 0,
                    is_active=True,
                )
            )
        for profile in profiles:
            _add_report_with_outcome(
                db,
                user_id=user_id,
                profile=profile,
                symbol="601318.SH",
                signal_day=signal_day,
                t1_day=t1_day,
                direction_bucket="bearish",
                p0=50.0,
                p1=48.0,
            )
        db.commit()

        trend = model_arena_service.build_multi_model_consensus_t1_trend(
            db,
            user_id=user_id,
            start_date=signal_day,
            end_date=signal_day,
            scope="all",
            min_models=2,
        )
        assert len(trend["series"]) == 1
        point = trend["series"][0]
        assert point["unanimous_bearish_count"] == 1
        assert point["unanimous_bearish_accuracy_pct"] == 100.0


def test_consensus_detail_includes_name_from_report_payload():
    signal_day = "2026-05-28"
    t1_day = "2026-05-29"
    with get_db_ctx() as db:
        user_id = _seed_user(db)
        profiles = []
        for idx, model_name in enumerate(("model-a", "model-b")):
            profiles.append(
                model_profile_service.create_model_profile(
                    db,
                    user_id=user_id,
                    name=f"P-{idx}-{uuid4().hex[:4]}",
                    llm_provider="openai",
                    quick_think_llm=model_name,
                    deep_think_llm=model_name,
                    backend_url="https://example.com/v1",
                    is_default=idx == 0,
                    is_active=True,
                )
            )
        now = datetime.now(timezone.utc)
        for profile in profiles:
            report_id = uuid4().hex
            db.add(
                ReportDB(
                    id=report_id,
                    user_id=user_id,
                    symbol="600519.SH",
                    trade_date=signal_day,
                    status="completed",
                    decision="BUY",
                    direction="bullish",
                    result_data={
                        "instrument_context": {"symbol": "600519.SH", "security_name": "贵州茅台"},
                        "model_info": {
                            "model_profile_id": profile["id"],
                            "model_profile_name": profile["name"],
                            "llm_provider": profile["llm_provider"],
                            "quick_think_llm": profile["quick_think_llm"],
                            "deep_think_llm": profile["deep_think_llm"],
                        },
                    },
                    created_at=now,
                    updated_at=now,
                )
            )
            db.add(
                ReportT1OutcomeDB(
                    id=uuid4().hex,
                    user_id=user_id,
                    report_id=report_id,
                    symbol="600519.SH",
                    signal_trade_date=signal_day,
                    t1_trade_date=t1_day,
                    p0=100.0,
                    p1=101.0,
                    return_t1_pct=1.0,
                    direction_bucket="bullish",
                    label_correct=True,
                    status="evaluated",
                    evaluated_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
        db.commit()

        detail = model_arena_service.build_multi_model_consensus_t1_detail(
            db,
            user_id=user_id,
            date=signal_day,
            scope="all",
            min_models=2,
        )
        assert len(detail["items"]) == 1
        assert detail["items"][0]["name"] == "贵州茅台"


def test_model_t1_detail_includes_symbol_name():
    signal_day = "2026-05-30"
    t1_day = "2026-06-03"
    with get_db_ctx() as db:
        user_id = _seed_user(db)
        profile = model_profile_service.create_model_profile(
            db,
            user_id=user_id,
            name=f"P-{uuid4().hex[:6]}",
            llm_provider="openai",
            quick_think_llm="model-detail",
            deep_think_llm="model-detail",
            backend_url="https://example.com/v1",
            is_default=True,
            is_active=True,
        )
        report_id = uuid4().hex
        now = datetime.now(timezone.utc)
        db.add(
            ReportDB(
                id=report_id,
                user_id=user_id,
                symbol="000001.SZ",
                trade_date=signal_day,
                status="completed",
                decision="BUY",
                direction="bullish",
                result_data={
                    "instrument_context": {"symbol": "000001.SZ", "security_name": "平安银行"},
                    "model_info": {
                        "model_profile_id": profile["id"],
                        "model_profile_name": profile["name"],
                        "llm_provider": profile["llm_provider"],
                        "quick_think_llm": profile["quick_think_llm"],
                        "deep_think_llm": profile["deep_think_llm"],
                    },
                },
                created_at=now,
                updated_at=now,
            )
        )
        db.add(
            ReportT1OutcomeDB(
                id=uuid4().hex,
                user_id=user_id,
                report_id=report_id,
                symbol="000001.SZ",
                signal_trade_date=signal_day,
                t1_trade_date=t1_day,
                p0=10.0,
                p1=10.2,
                return_t1_pct=2.0,
                direction_bucket="bullish",
                label_correct=True,
                status="evaluated",
                evaluated_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        db.commit()

        payload = model_arena_service.build_model_t1_detail(
            db,
            user_id=user_id,
            model_key="model:model-detail",
            start_date=signal_day,
            end_date=signal_day,
            scope="all",
        )
        assert payload["total_samples"] == 1
        assert payload["rows"][0]["symbol"] == "000001.SZ"
        assert payload["rows"][0]["name"] == "平安银行"
        assert payload.get("drift_rows") == []


def test_model_t1_detail_splits_drift_rows_from_accuracy_samples():
    signal_day = "2026-06-04"
    t1_day = "2026-06-05"
    with get_db_ctx() as db:
        user_id = _seed_user(db)
        profile = model_profile_service.create_model_profile(
            db,
            user_id=user_id,
            name=f"P-{uuid4().hex[:6]}",
            llm_provider="openai",
            quick_think_llm="model-drift",
            deep_think_llm="model-drift",
            backend_url="https://example.com/v1",
            is_default=True,
            is_active=True,
        )
        now = datetime.now(timezone.utc)
        clean_report_id = uuid4().hex
        drift_report_id = uuid4().hex
        for report_id, p0, p1, ret, correct in (
            (clean_report_id, 10.0, 10.5, 5.0, True),
            (drift_report_id, 10.0, 10.0, 0.0, False),
        ):
            db.add(
                ReportDB(
                    id=report_id,
                    user_id=user_id,
                    symbol="601398.SH",
                    trade_date=signal_day,
                    status="completed",
                    decision="BUY",
                    direction="bullish",
                    result_data={
                        "instrument_context": {"symbol": "601398.SH", "security_name": "工商银行"},
                        "model_info": {
                            "model_profile_id": profile["id"],
                            "model_profile_name": profile["name"],
                            "llm_provider": profile["llm_provider"],
                            "quick_think_llm": profile["quick_think_llm"],
                            "deep_think_llm": profile["deep_think_llm"],
                        },
                    },
                    created_at=now,
                    updated_at=now,
                )
            )
            db.add(
                ReportT1OutcomeDB(
                    id=uuid4().hex,
                    user_id=user_id,
                    report_id=report_id,
                    symbol="601398.SH",
                    signal_trade_date=signal_day,
                    t1_trade_date=t1_day,
                    p0=p0,
                    p1=p1,
                    return_t1_pct=ret,
                    direction_bucket="bullish",
                    label_correct=correct,
                    status="evaluated",
                    evaluated_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
        db.commit()

        payload = model_arena_service.build_model_t1_detail(
            db,
            user_id=user_id,
            model_key="model:model-drift",
            start_date=signal_day,
            end_date=signal_day,
            scope="all",
        )
        assert payload["total_samples"] == 1
        assert len(payload["rows"]) == 1
        assert payload["rows"][0]["name"] == "工商银行"
        assert len(payload["drift_rows"]) == 1
        assert payload["drift_warning_count"] == 1
        assert payload["drift_rows"][0]["date_drift_flag"] is True

        trend = model_arena_service.build_model_accuracy_trend(
            db,
            user_id=user_id,
            start_date=signal_day,
            end_date=signal_day,
            scope="all",
            top_n=3,
            min_samples=1,
        )
        series = trend["series"][0]
        point = next(p for p in series["points"] if p["date"] == signal_day)
        assert point["sample_count"] == 1
        assert point["accuracy_pct"] == 100.0
