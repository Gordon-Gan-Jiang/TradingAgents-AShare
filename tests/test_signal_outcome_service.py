"""回填编排：成熟度筛选、幂等、取数失败处理、端到端落库。

## 这些用例守的是「校准样本不被污染」

命中率是权重校准的唯一依据，因此样本集合的正确性比样本量更重要：

* 窗口没走完就回填 → 用不完整数据判对错（最隐蔽的统计错误）
* 把取数失败记成未命中 → 基础设施问题污染统计
* 重复回填产生第二行 → 同一判定被重复计数
* 改写已记录结果 → 用未来信息修正历史
"""

from __future__ import annotations

import pandas as pd
import pytest


def _frame(rows):
    """rows: [(date, high, close), ...]"""
    return pd.DataFrame(
        [{"date": d, "open": c, "high": h, "low": c, "close": c} for d, h, c in rows]
    )


def _verdict(**kw):
    base = {
        "verdict_id": "v1",
        "user_id": "u1",
        "symbol": "600519.SH",
        "as_of": "2026-01-16",
        "state": "washout",
    }
    base.update(kw)
    return base


# ══════════════════════════════════════════════════════════════════════════
# 成熟度筛选
# ══════════════════════════════════════════════════════════════════════════


def test_immature_verdicts_are_not_backfilled():
    """窗口没走完不得回填——用不完整数据判对错是最隐蔽的统计错误。"""
    from api.services.signal_outcome_service import select_matured

    # as_of 2026-01-16（周五）+ 5 交易日 = 2026-01-23；今天只到 2026-01-20
    matured = select_matured([_verdict()], horizon_days=5, today="2026-01-20")
    assert matured == []


def test_matured_verdicts_are_selected():
    from api.services.signal_outcome_service import select_matured

    matured = select_matured([_verdict()], horizon_days=5, today="2026-01-23")
    assert len(matured) == 1
    assert matured[0].target_date == "2026-01-23"


def test_abstentions_are_never_queued_for_backfill():
    from api.services.signal_outcome_service import select_matured

    matured = select_matured(
        [_verdict(state="insufficient_evidence")], horizon_days=5, today="2026-06-01"
    )
    assert matured == []


def test_already_recorded_pairs_are_skipped():
    """幂等：同一 (verdict, horizon) 已有结果就跳过。"""
    from api.services.signal_outcome_service import select_matured

    matured = select_matured(
        [_verdict()], horizon_days=5, today="2026-06-01", recorded=[("v1", 5)]
    )
    assert matured == []

    # 但另一个 horizon 仍可回填
    other = select_matured(
        [_verdict()], horizon_days=3, today="2026-06-01", recorded=[("v1", 5)]
    )
    assert len(other) == 1


def test_trading_day_maturity_not_calendar():
    """5 个交易日 ≠ 5 个自然日：跨周末时自然日会提前判成熟。"""
    from api.services.signal_outcome_service import select_matured

    # 2026-01-16 周五 + 5 交易日 = 01-23；若按自然日会算成 01-21
    assert select_matured([_verdict()], horizon_days=5, today="2026-01-21") == []
    assert len(select_matured([_verdict()], horizon_days=5, today="2026-01-23")) == 1


# ══════════════════════════════════════════════════════════════════════════
# 评估与落库
# ══════════════════════════════════════════════════════════════════════════


def _loader(rows):
    def load(symbol, start, end):
        return _frame(rows)

    return load


def test_backfill_records_hit():
    from api.services.signal_outcome_service import backfill

    recorded: list[dict] = []
    report = backfill(
        horizon_days=5,
        today="2026-06-01",
        loader=_loader(
            [
                ("2026-01-16", 10.0, 9.9),   # 判定日
                ("2026-01-19", 10.1, 10.0),
                ("2026-01-20", 10.4, 10.3),  # 创新高
                ("2026-01-21", 10.2, 10.1),
                ("2026-01-22", 10.3, 10.2),
                ("2026-01-23", 10.25, 10.15),
            ]
        ),
        verdicts=[_verdict()],
        recorder=lambda **kw: recorded.append(kw),
    )

    assert report["candidates"] == 1
    assert report["evaluable"] == 1
    assert report["hits"] == 1
    assert recorded[0]["threshold_hit"] is True
    assert recorded[0]["ref_date"] == "2026-01-23"
    assert recorded[0]["max_high"] == pytest.approx(10.4)


def test_backfill_records_miss_without_marking_not_evaluable():
    from api.services.signal_outcome_service import backfill

    recorded: list[dict] = []
    backfill(
        horizon_days=5,
        today="2026-06-01",
        loader=_loader(
            [
                ("2026-01-16", 10.0, 9.9),
                ("2026-01-19", 9.95, 9.9),
                ("2026-01-20", 9.9, 9.85),
                ("2026-01-21", 9.88, 9.82),
                ("2026-01-22", 9.86, 9.8),
                ("2026-01-23", 9.84, 9.78),
            ]
        ),
        verdicts=[_verdict()],
        recorder=lambda **kw: recorded.append(kw),
    )
    assert recorded[0]["evaluable"] is True
    assert recorded[0]["threshold_hit"] is False


def test_loader_failure_is_recorded_as_not_evaluable_not_as_a_miss():
    """取数失败必须与「未命中」区分：否则基础设施问题会污染命中率。"""
    from api.services.signal_outcome_service import backfill

    def _boom(symbol, start, end):
        raise ConnectionError("network down")

    recorded: list[dict] = []
    report = backfill(
        horizon_days=5,
        today="2026-06-01",
        loader=_boom,
        verdicts=[_verdict()],
        recorder=lambda **kw: recorded.append(kw),
    )

    assert report["candidates"] == 1
    assert report["evaluable"] == 0
    assert report["not_evaluable"] == 1
    assert report["hits"] == 0
    assert recorded[0]["evaluable"] is False
    assert "loader_failed" in recorded[0]["reason"]


def test_suspended_symbol_window_is_not_evaluable():
    """停牌：窗口内 bar 不足 → 不可评估（既不算命中也不算未命中）。"""
    from api.services.signal_outcome_service import backfill

    recorded: list[dict] = []
    backfill(
        horizon_days=5,
        today="2026-06-01",
        loader=_loader([("2026-01-16", 10.0, 9.9), ("2026-01-19", 10.1, 10.0)]),
        verdicts=[_verdict()],
        recorder=lambda **kw: recorded.append(kw),
    )
    assert recorded[0]["evaluable"] is False
    assert "insufficient window" in (recorded[0]["reason"] or "")


def test_entry_bar_missing_is_not_evaluable():
    from api.services.signal_outcome_service import backfill

    recorded: list[dict] = []
    backfill(
        horizon_days=5,
        today="2026-06-01",
        loader=_loader([("2026-02-02", 10.0, 9.9)] * 3),
        verdicts=[_verdict()],
        recorder=lambda **kw: recorded.append(kw),
    )
    assert recorded[0]["evaluable"] is False
    assert "loader_failed" in (recorded[0]["reason"] or "")


def test_batch_continues_after_one_symbol_fails():
    """单只标的取数失败不得中断整批——否则一只停牌股会挡住所有回填。"""
    from api.services.signal_outcome_service import backfill

    good = _frame([("2026-01-16", 10.0, 9.9), ("2026-01-19", 10.1, 10.0),
                   ("2026-01-20", 10.4, 10.3), ("2026-01-21", 10.2, 10.1),
                   ("2026-01-22", 10.3, 10.2), ("2026-01-23", 10.25, 10.15)])

    def loader(symbol, start, end):
        if symbol == "BAD.SH":
            raise ConnectionError("boom")
        return good

    recorded: list[dict] = []
    report = backfill(
        horizon_days=5,
        today="2026-06-01",
        loader=loader,
        verdicts=[_verdict(verdict_id="v1"), _verdict(verdict_id="v2", symbol="BAD.SH")],
        recorder=lambda **kw: recorded.append(kw),
    )
    assert report["candidates"] == 2
    assert report["evaluable"] == 1
    assert report["not_evaluable"] == 1


def test_distribution_state_uses_its_own_definition():
    from api.services.signal_outcome_service import backfill

    recorded: list[dict] = []
    backfill(
        horizon_days=5,
        today="2026-06-01",
        loader=_loader(
            [
                ("2026-01-16", 10.0, 9.9),
                ("2026-01-19", 9.85, 9.8),
                ("2026-01-20", 9.7, 9.65),
                ("2026-01-21", 9.6, 9.55),
                ("2026-01-22", 9.55, 9.5),
                ("2026-01-23", 9.5, 9.45),
            ]
        ),
        verdicts=[_verdict(state="distribution")],
        recorder=lambda **kw: recorded.append(kw),
    )
    assert recorded[0]["evaluable"] is True
    assert recorded[0]["threshold_hit"] is True


# ══════════════════════════════════════════════════════════════════════════
# 端到端：DB 落库 + 幂等 + 用户隔离
# ══════════════════════════════════════════════════════════════════════════


def _seed_verdict(user_id: str, symbol: str = "600519.SH", as_of: str = "2026-01-16") -> str:
    from api.database import SignalVerdictDB, get_db_ctx
    from tradingagents.agent.repository import new_id

    vid = new_id()
    with get_db_ctx() as db:
        db.add(
            SignalVerdictDB(
                id=vid, user_id=user_id, symbol=symbol, as_of=as_of, probe="washout",
                state="washout", confidence=61, margin=0.8, signal_version="washout-v1.0.0",
            )
        )
        db.commit()
    return vid


def _rows_loader():
    data = _frame(
        [
            ("2026-01-16", 10.0, 9.9),
            ("2026-01-19", 10.1, 10.0),
            ("2026-01-20", 10.4, 10.3),
            ("2026-01-21", 10.2, 10.1),
            ("2026-01-22", 10.3, 10.2),
            ("2026-01-23", 10.25, 10.15),
        ]
    )

    def load(symbol, start, end):
        return data

    return load


def test_run_backfill_persists_outcomes():
    from api.database import SignalVerdictOutcomeDB, get_db_ctx
    from api.services.signal_outcome_service import run_backfill

    vid = _seed_verdict("backfill-user")
    report = run_backfill(today="2026-06-01", loader=_rows_loader(), user_id="backfill-user")

    assert report["5"]["recorded"] == 1
    assert report["3"]["recorded"] == 1
    with get_db_ctx() as db:
        rows = db.query(SignalVerdictOutcomeDB).filter(SignalVerdictOutcomeDB.verdict_id == vid).all()
    # 两个 horizon 各一行：T+3 与 T+5 是两次独立评估，不是同一行的覆盖
    assert {r.horizon_days for r in rows} == {3, 5}
    five = next(r for r in rows if r.horizon_days == 5)
    assert five.evaluable is True
    assert five.threshold_hit is True


def test_run_backfill_is_idempotent_and_never_rewrites():
    """重复回填不得产生第二行，也不得改写已记录的结果。"""
    from api.database import SignalVerdictOutcomeDB, get_db_ctx
    from api.services.signal_outcome_service import run_backfill

    vid = _seed_verdict("idem-user")
    run_backfill(today="2026-06-01", loader=_rows_loader(), user_id="idem-user")

    with get_db_ctx() as db:
        first = (
            db.query(SignalVerdictOutcomeDB)
            .filter(SignalVerdictOutcomeDB.verdict_id == vid,
                    SignalVerdictOutcomeDB.horizon_days == 5)
            .one()
        )
        first.threshold_hit = False  # 人为改动，验证第二次回填不会覆盖回来
        db.commit()

    second = run_backfill(today="2026-06-01", loader=_rows_loader(), user_id="idem-user")
    assert second["5"]["candidates"] == 0  # 已记录 → 不再进队列
    assert second["3"]["candidates"] == 0

    with get_db_ctx() as db:
        rows = db.query(SignalVerdictOutcomeDB).filter(
            SignalVerdictOutcomeDB.verdict_id == vid
        ).all()
    assert len(rows) == 2  # 每个 horizon 恰好一行，没有重复
    five = next(r for r in rows if r.horizon_days == 5)
    assert five.threshold_hit is False  # 历史不可变，未被回填改写


def test_run_backfill_respects_user_scope():
    from api.database import SignalVerdictOutcomeDB, get_db_ctx
    from api.services.signal_outcome_service import run_backfill

    _seed_verdict("scope-a")
    other = _seed_verdict("scope-b")

    run_backfill(today="2026-06-01", loader=_rows_loader(), user_id="scope-a")

    with get_db_ctx() as db:
        mine = db.query(SignalVerdictOutcomeDB).filter(
            SignalVerdictOutcomeDB.verdict_id != other
        ).count()
        theirs = db.query(SignalVerdictOutcomeDB).filter(
            SignalVerdictOutcomeDB.verdict_id == other
        ).count()
    assert mine >= 1
    assert theirs == 0


def test_run_backfill_skips_abstentions():
    from api.database import SignalVerdictDB, SignalVerdictOutcomeDB, get_db_ctx
    from api.services.signal_outcome_service import run_backfill
    from tradingagents.agent.repository import new_id

    vid = new_id()
    with get_db_ctx() as db:
        db.add(
            SignalVerdictDB(
                id=vid, user_id="abstain-user", symbol="600519.SH", as_of="2026-01-16",
                probe="washout", state="insufficient_evidence", confidence=0, margin=0.02,
                signal_version="washout-v1.0.0",
            )
        )
        db.commit()

    run_backfill(today="2026-06-01", loader=_rows_loader(), user_id="abstain-user")
    with get_db_ctx() as db:
        assert db.query(SignalVerdictOutcomeDB).filter(
            SignalVerdictOutcomeDB.verdict_id == vid
        ).count() == 0


def test_calibration_report_includes_per_state_samples():
    """分状态样本量必须显式可见。

    合并统计会掩盖「洗盘样本只有 3 个」这类样本饥饿问题，而分状态才是权重
    校准真正需要的最小样本单位——本平台有真实的样本饥饿前科。
    """
    from api.services import signal_service
    from api.services.signal_outcome_service import run_backfill

    vid = _seed_verdict("calib-state-user")
    run_backfill(today="2026-06-01", loader=_rows_loader(), user_id="calib-state-user")

    report = signal_service.calibration_report(user_id="calib-state-user")
    five = report["monitored"]["per_horizon"][5]
    assert five["sample"] == 1
    assert five["by_state"]["washout"]["sample"] == 1
    assert five["by_state"]["washout"]["hit"] == 1
    assert five["by_state"]["distribution"]["sample"] == 0


def test_calibration_dedupes_identical_judgements_across_users():
    """同一标的同一天的判定是**同一个样本**，不因用户数而重复计数。

    若按行统计，多用户部署下命中率会随用户数漂移——统计量不该依赖有多少人
    问了同一个问题。
    """
    from api.services import signal_service

    _seed_verdict("dup-a", symbol="000001.SZ", as_of="2026-03-02")
    _seed_verdict("dup-b", symbol="000001.SZ", as_of="2026-03-02")

    report_a = signal_service.calibration_report(user_id="dup-a")
    report_b = signal_service.calibration_report(user_id="dup-b")
    assert report_a["sample"]["total"] == 1
    assert report_b["sample"]["total"] == 1
