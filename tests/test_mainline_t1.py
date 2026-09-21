"""M6 T+1 测试：主线兑现评估、refresh、overview 统计。"""

from __future__ import annotations

import os
import tempfile
from unittest.mock import patch

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.database import Base, MainlineReportDB, MainlineT1OutcomeDB
from api.services import mainline_service


def _mk_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _run(trade_date="2026-08-25", report_id="r1", user_id="u1"):
    return {
        "id": report_id,
        "user_id": user_id,
        "trade_date": trade_date,
        "mainlines": [
            {
                "name": "AI算力",
                "type": "concept",
                "representative_boards": [{"board": "CPO概念", "chg_1d": 3.2}],
            },
            {"name": "低空经济", "type": "concept", "representative_boards": [{"board": "低空经济", "chg_1d": 1.0}]},
        ],
    }


def _bench_series(start="2026-08-25", end="2026-08-31", base=3000.0):
    idx = pd.bdate_range(start, periods=pd.bdate_range(start, end).shape[0]).strftime("%Y-%m-%d")
    return pd.Series([base * (1 + 0.002 * i) for i in range(len(idx))], index=idx)


def test_evaluate_t1_outcome_fulfilled():
    db = _mk_db()
    run = _run()
    bench = _bench_series()

    def fake_board_hist(ak, board_name, sector_type, trade_date, check_date):
        return pd.Series(
            {"2026-08-25": 100.0, "2026-08-31": 105.0},  # 板块 +5%
        )

    with patch(
        "tradingagents.dataflows.mainline_backtest.fetch_benchmark_close", return_value=bench
    ), patch(
        "api.services.mainline_service._board_hist_close", side_effect=fake_board_hist
    ):
        outs = mainline_service.evaluate_t1_for_report(db, run, as_of_date="2026-08-31", ak_module=object())
    assert len(outs) == 2
    fulfilled = {o["mainline"]: o for o in outs}
    assert fulfilled["AI算力"]["outcome"] == "兑现"
    assert fulfilled["AI算力"]["excess_ret"] > 0

    rows = db.query(MainlineT1OutcomeDB).all()
    assert len(rows) == 2

    # 二次评估 upsert 不产生重复行
    with patch(
        "tradingagents.dataflows.mainline_backtest.fetch_benchmark_close", return_value=bench
    ), patch(
        "api.services.mainline_service._board_hist_close", side_effect=fake_board_hist
    ):
        mainline_service.evaluate_t1_for_report(db, run, as_of_date="2026-08-31", ak_module=object())
    assert db.query(MainlineT1OutcomeDB).count() == 2


def test_evaluate_t1_outcome_falsified():
    db = _mk_db()
    run = _run()
    bench = _bench_series()

    def fake_board_hist(ak, board_name, sector_type, trade_date, check_date):
        return pd.Series({"2026-08-25": 100.0, "2026-08-31": 94.0})  # 板块 -6%

    with patch(
        "tradingagents.dataflows.mainline_backtest.fetch_benchmark_close", return_value=bench
    ), patch(
        "api.services.mainline_service._board_hist_close", side_effect=fake_board_hist
    ):
        outs = mainline_service.evaluate_t1_for_report(db, run, as_of_date="2026-08-31", ak_module=object())
    assert outs[0]["outcome"] == "证伪"
    assert outs[0]["excess_ret"] < 0


def test_evaluate_t1_no_forward_data_returns_empty():
    db = _mk_db()
    run = _run(trade_date="2026-08-31")
    bench = _bench_series("2026-08-31", "2026-08-31")  # 基准只有当天 → 无前瞻
    with patch(
        "tradingagents.dataflows.mainline_backtest.fetch_benchmark_close", return_value=bench
    ):
        outs = mainline_service.evaluate_t1_for_report(db, run, as_of_date="2026-08-31", ak_module=object())
    assert outs == []


def test_refresh_t1_outcomes_skips_no_forward():
    db = _mk_db()
    db.add(
        MainlineReportDB(
            id="r1", user_id="u1", trade_date="2026-08-31",
            perspective="short", status="completed",
        )
    )
    db.commit()
    result = mainline_service.refresh_t1_outcomes(db, as_of_date="2026-08-31")
    assert result["evaluated_reports"] == 0
    assert len(result["skipped"]) == 1


def test_t1_overview_aggregates():
    db = _mk_db()
    rows = [
        MainlineT1OutcomeDB(
            id=f"o{i}", report_id="r1", user_id="u1", mainline=f"M{i}",
            trade_date="2026-08-25", outcome=outcome, excess_ret=excess,
        )
        for i, (outcome, excess) in enumerate(
            [("兑现", 0.03), ("兑现", 0.02), ("证伪", -0.02)]
        )
    ]
    db.add_all(rows)
    db.commit()
    ov = mainline_service.t1_overview(db, days=30)
    assert ov["total"] == 3
    assert ov["by_outcome"]["兑现"] == 2
    assert ov["avg_excess_ret"] == pytest.approx(0.01, abs=1e-9)
    assert ov["hit_rate"] == pytest.approx(2 / 3, abs=1e-4)  # 服务端四舍五入到 4 位

