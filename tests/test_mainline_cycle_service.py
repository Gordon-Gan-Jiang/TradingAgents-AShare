"""C2 周期服务测试：run_cycle_analysis 主流程、档案更新、决策卡、能力曲线。"""

from __future__ import annotations

import os
import tempfile
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.database import (
    Base,
    MainlineDecisionDB,
    MainlineHistoryDB,
    MainlineReportDB,
    MainlineT1OutcomeDB,
)
from api.services import mainline_cycle_service


def _mk_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _report(trade_date, mainlines, rule_candidates=None, emotion=None):
    return MainlineReportDB(
        id=f"r-{trade_date.replace('-', '')}",
        user_id="u1",
        trade_date=trade_date,
        perspective="short",
        status="completed",
        mainlines=mainlines,
        market_snapshot={
            "rule_candidates": rule_candidates or [],
            "emotion": emotion or {"temperature": 60, "gate": "normal"},
        },
    )


def _ml(name, boards, conf=70, phase="主升"):
    return {
        "name": name,
        "type": "industry",
        "confidence": conf,
        "phase": phase,
        "representative_boards": [{"board": b, "chg_1d": 2.0} for b in boards],
    }


def _cand(name, strength, fund=None, rsi=None):
    return {"name": name, "strength": strength, "heat": 70, "rs20": 0.1,
            "net_inflow_5d": fund, "inflow_persistent_10": bool(fund and fund > 0),
            "rsi14": rsi, "chg_1d": 2.0, "phase_hint": "主升", "passes_gate": True}


def test_run_cycle_analysis_creates_archive_and_decision():
    db = _mk_db()
    db.add(_report("2026-08-25", [_ml("AI算力", ["CPO概念"])], [_cand("CPO概念", 55, 1.0, 55)]))
    db.add(_report("2026-08-26", [_ml("AI算力", ["CPO概念"])], [_cand("CPO概念", 62, 2.0, 60)]))
    db.add(_report("2026-08-27", [_ml("AI算力", ["CPO概念"])], [_cand("CPO概念", 68, 3.0, 62)]))
    db.commit()

    r1 = mainline_cycle_service.run_cycle_analysis(db, "2026-08-25", user_id="u1")
    assert r1["updated"] == 1
    r2 = mainline_cycle_service.run_cycle_analysis(db, "2026-08-26", user_id="u1")
    assert r2["updated"] == 1
    r3 = mainline_cycle_service.run_cycle_analysis(db, "2026-08-27", user_id="u1")
    assert r3["updated"] == 1

    archives = db.query(MainlineHistoryDB).all()
    assert len(archives) == 1          # 同一主线跨天匹配到同一档案
    a = archives[0]
    assert a.mainline_key == "AI算力"
    assert a.first_date == "2026-08-25"
    assert a.last_date == "2026-08-27"
    assert len(a.daily_track) == 3
    assert a.status == "active"

    decisions = db.query(MainlineDecisionDB).all()
    assert len(decisions) == 3
    assert decisions[-1].stage in ("发酵", "主升")
    assert decisions[-1].action in ("布局", "持有")
    assert decisions[-1].position_pct is not None
    assert decisions[-1].verify_conditions


def test_run_cycle_analysis_retreat_stage():
    db = _mk_db()
    # 强度连降 + 资金转负 → 退潮
    snaps = [
        (d, s, f) for d, s, f in [
            ("2026-08-21", 80, 5.0), ("2026-08-22", 78, 4.0), ("2026-08-25", 74, 2.0),
            ("2026-08-26", 68, -1.0), ("2026-08-27", 60, -3.0),
        ]
    ]
    for d, s, f in snaps:
        db.add(_report(d, [_ml("低空经济", ["低空经济"])], [_cand("低空经济", s, f, 72)]))
    db.commit()
    for d, _, _ in snaps:
        mainline_cycle_service.run_cycle_analysis(db, d, user_id="u1")
    a = db.query(MainlineHistoryDB).first()
    assert a.cycle_position == "退潮"
    assert a.action == "规避"
    assert a.position_pct == 0.0
    assert len(a.alerts or []) >= 2


def test_run_cycle_analysis_identity_match_by_boards():
    db = _mk_db()
    # 第一天名"AI算力"，第二天名"算力租赁"但代表板块交集 → 同一档案
    db.add(_report("2026-08-25", [_ml("AI算力", ["CPO概念"])], [_cand("CPO概念", 55, 1.0, 50)]))
    db.add(_report("2026-08-26", [_ml("算力租赁", ["CPO概念", "液冷"])], [_cand("CPO概念", 60, 2.0, 52)]))
    db.commit()
    mainline_cycle_service.run_cycle_analysis(db, "2026-08-25", user_id="u1")
    mainline_cycle_service.run_cycle_analysis(db, "2026-08-26", user_id="u1")
    archives = db.query(MainlineHistoryDB).all()
    assert len(archives) == 1          # 板块交集识别为同一主线
    assert archives[0].mainline_key == "AI算力"
    assert "液冷" in (archives[0].representative_boards or [])


def test_run_cycle_analysis_marks_ended():
    db = _mk_db()
    db.add(_report("2026-08-25", [_ml("旧主线", ["旧板块"])], [_cand("旧板块", 70, 3.0, 65)]))
    db.commit()
    mainline_cycle_service.run_cycle_analysis(db, "2026-08-25", user_id="u1")
    # 3 天后新主线出现，旧主线未出现 → ended
    db.add(_report("2026-08-28", [_ml("新主线", ["新板块"])], [_cand("新板块", 60, 1.0, 55)]))
    db.commit()
    res = mainline_cycle_service.run_cycle_analysis(db, "2026-08-28", user_id="u1")
    assert res["ended"] >= 1
    old = db.query(MainlineHistoryDB).filter_by(mainline_key="旧主线").first()
    assert old.status == "ended"
    assert old.cycle_position == "已终结"


def test_capability_curve():
    db = _mk_db()
    db.add_all([
        MainlineDecisionDB(id="d1", user_id="u1", mainline_key="A", trade_date="2026-08-01", stage="主升", action="持有", outcome="verified"),
        MainlineDecisionDB(id="d2", user_id="u1", mainline_key="B", trade_date="2026-08-02", stage="退潮", action="规避", outcome="verified"),
        MainlineDecisionDB(id="d3", user_id="u1", mainline_key="C", trade_date="2026-08-03", stage="主升", action="持有", outcome="falsified"),
    ])
    db.commit()
    import pytest

    curve = mainline_cycle_service.capability_curve(db, user_id="u1", days=90)
    assert curve["evaluated"] == 3
    assert curve["hit_rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert curve["by_stage"]["主升"]["verified"] == 1
    assert curve["by_stage"]["退潮"]["verified"] == 1


def test_verify_decisions_links_t1_outcome():
    db = _mk_db()
    db.add(MainlineDecisionDB(id="d1", user_id="u1", mainline_key="AI算力", trade_date="2026-08-25", stage="主升", action="持有", outcome=None))
    db.add(MainlineT1OutcomeDB(id="o1", report_id="r1", user_id="u1", mainline="AI算力", trade_date="2026-08-25", outcome="兑现", excess_ret=0.03))
    db.commit()
    res = mainline_cycle_service.verify_decisions(db, user_id="u1")
    assert res["updated"] == 1
    d = db.query(MainlineDecisionDB).first()
    assert d.outcome == "verified"
    assert d.forward_excess_ret == 0.03


def test_rotation_timeline():
    db = _mk_db()
    db.add_all([
        MainlineDecisionDB(id="d1", user_id="u1", mainline_key="A", trade_date="2026-08-25", stage="主升", action="持有"),
        MainlineDecisionDB(id="d2", user_id="u1", mainline_key="B", trade_date="2026-08-26", stage="发酵", action="布局"),
    ])
    db.commit()
    tl = mainline_cycle_service.rotation_timeline(db, user_id="u1", days=30)
    assert len(tl) == 2
    assert tl[0]["date"] == "2026-08-26"
    assert tl[0]["mainlines"][0]["key"] == "B"


# ── P2-2 主线验证闭环：判定语义 / 匹配口径 / 回填 / 仓位落库 ──────


def _report_row(rid, user, trade_date, name, boards=("CPO概念",)):
    """单主线报告行（用于严格按 report_id 关联的验证测试）。"""
    return MainlineReportDB(
        id=rid, user_id=user, trade_date=trade_date, perspective="short", status="completed",
        mainlines=[{
            "name": name, "type": "concept", "confidence": 70,
            "representative_boards": [{"board": b} for b in boards],
        }],
    )


def _decision(did, *, user="u1", report_id=None, key="AI算力", trade_date="2026-08-25",
              action="持有", stage="主升"):
    return MainlineDecisionDB(
        id=did, user_id=user, report_id=report_id, mainline_key=key, trade_date=trade_date,
        stage=stage, action=action, outcome=None,
    )


def _outcome(oid, *, user="u1", report_id="r1", mainline="AI算力", trade_date="2026-08-25",
             outcome="兑现", board_ret=0.03, excess=0.02):
    return MainlineT1OutcomeDB(
        id=oid, report_id=report_id, user_id=user, mainline=mainline, trade_date=trade_date,
        check_date="2026-08-26", board_fwd_ret=board_ret, excess_ret=excess, outcome=outcome,
    )


def test_verify_decisions_action_semantics_conservative():
    """验证语义：看多动作须真正兑现，降敞口动作须主线未兑现；减仓不再"两头都对"。"""
    db = _mk_db()
    cases = [
        # (主线名, 动作, T+1 标签, 板块收益, 超额, 期望 outcome)
        ("M01", "布局", "兑现", 0.03, 0.02, "verified"),
        ("M02", "持有", "兑现", 0.03, 0.02, "verified"),
        ("M03", "减仓", "兑现", 0.03, 0.02, "falsified"),        # 修复点：踏空/减早了
        ("M04", "减仓", "证伪", -0.03, -0.02, "verified"),
        ("M05", "规避", "兑现", 0.03, 0.02, "falsified"),
        ("M06", "规避", "证伪", -0.03, -0.02, "verified"),
        ("M07", "持有", "走平(跑输基准)", 0.01, -0.02, "falsified"),
        ("M08", "减仓", "走平(跑输基准)", 0.01, -0.02, "verified"),
        ("M09", "持有", "弱兑现(抗跌)", -0.01, 0.02, "falsified"),  # 绝对收益为负，看多不成立
        ("M10", "减仓", "弱兑现(抗跌)", -0.01, 0.02, "verified"),
        ("M11", "观察", "兑现", 0.03, 0.02, "falsified"),
        ("M12", "观察", "证伪", -0.03, -0.02, "verified"),
        ("M13", "持有", "数据不足", None, None, None),             # 不评分
    ]
    names = [c[0] for c in cases]
    db.add(MainlineReportDB(
        id="r1", user_id="u1", trade_date="2026-08-25", perspective="short", status="completed",
        mainlines=[{"name": n, "type": "concept",
                    "representative_boards": [{"board": "CPO概念"}]} for n in names],
    ))
    for i, (name, action, label, bret, ex, _) in enumerate(cases):
        db.add(_decision(f"d{i}", report_id="r1", key=name, action=action))
        db.add(_outcome(f"o{i}", report_id="r1", mainline=name,
                        outcome=label, board_ret=bret, excess=ex))
    db.commit()

    res = mainline_cycle_service.verify_decisions(db, user_id="u1")
    assert res["pending_total"] == len(cases)
    assert res["no_data"] == 0
    assert res["unscored"] == 1
    assert res["updated"] == len(cases) - 1
    assert res["verified"] + res["falsified"] == res["updated"]
    for i, (_, _, _, _, _, expect) in enumerate(cases):
        row = db.query(MainlineDecisionDB).filter_by(id=f"d{i}").first()
        assert row.outcome == expect, (i, row.mainline_key, row.outcome, expect)
    d12 = db.query(MainlineDecisionDB).filter_by(id="d12").first()
    assert d12.outcome is None and d12.forward_excess_ret is None


def test_verify_decisions_scoped_by_user_and_report():
    """匹配口径：同名主线不得跨报告/跨用户串味。"""
    db = _mk_db()
    db.add_all([
        _report_row("r1", "u1", "2026-08-25", "AI算力"),
        _report_row("r2", "u2", "2026-08-25", "AI算力"),
        _report_row("r9", "u1", "2026-08-25", "AI算力"),   # u1 的另一份报告，无兑现记录
    ])
    db.add_all([
        _outcome("o1", user="u1", report_id="r1", mainline="AI算力",
                 outcome="证伪", board_ret=-0.03, excess=-0.02),
        _outcome("o2", user="u2", report_id="r2", mainline="AI算力",
                 outcome="兑现", board_ret=0.03, excess=0.02),
    ])
    db.add_all([
        _decision("d-u1-r1", user="u1", report_id="r1", action="持有"),
        _decision("d-u1-r9", user="u1", report_id="r9", action="持有"),
        _decision("d-u2-r2", user="u2", report_id="r2", action="持有"),
    ])
    db.commit()

    mainline_cycle_service.verify_decisions(db, user_id="u1")
    # u1/r1 用自己的"证伪"记录 → 持有被证伪（若串到 u2 的"兑现"会误判 verified）
    assert db.query(MainlineDecisionDB).filter_by(id="d-u1-r1").first().outcome == "falsified"
    # 同用户同日期同名但报告不同 → 不得匹配
    assert db.query(MainlineDecisionDB).filter_by(id="d-u1-r9").first().outcome is None
    # 用户隔离：u2 的决策不在 u1 的调用范围内
    assert db.query(MainlineDecisionDB).filter_by(id="d-u2-r2").first().outcome is None


def test_verify_decisions_matches_renamed_mainline_in_same_report():
    """同一报告内主线改名（key=AI算力 / 当日名=算力租赁）仍能按代表板块关联。"""
    db = _mk_db()
    db.add(_report_row("r1", "u1", "2026-08-25", "算力租赁"))
    db.add(MainlineHistoryDB(
        id="h1", user_id="u1", mainline_key="AI算力",
        representative_boards=["CPO概念"], first_date="2026-08-20",
    ))
    db.add(_outcome("o1", user="u1", report_id="r1", mainline="算力租赁",
                    outcome="兑现", board_ret=0.04, excess=0.03))
    db.add(_decision("d1", user="u1", report_id="r1", key="AI算力", action="布局"))
    db.commit()

    res = mainline_cycle_service.verify_decisions(db, user_id="u1")
    assert res["updated"] == 1
    d = db.query(MainlineDecisionDB).filter_by(id="d1").first()
    assert d.outcome == "verified"
    assert d.forward_excess_ret == pytest.approx(0.03)


def test_backfill_decision_outcomes_fills_closed_window():
    """回填任务：窗口已关闭的决策补建 T+1 兑现并写入 outcome / forward_excess_ret。"""
    db = _mk_db()
    db.add(_report_row("r-old", "u1", "2026-08-20", "AI算力"))
    db.add(_decision("d-old", report_id="r-old", trade_date="2026-08-20", action="布局"))
    db.add(_decision("d-new", report_id="r-old", trade_date="2026-08-28", action="布局"))  # 窗口未关闭
    db.commit()

    def _fake_evaluate(db_, run, *, as_of_date=None, ak_module=None):
        # 模拟板块历史可用时的 T+1 兑现评估（真实实现依赖网络）
        db_.add(_outcome(
            "o-old", user=run.get("user_id"), report_id=run["id"], mainline="AI算力",
            trade_date=run["trade_date"], outcome="兑现", board_ret=0.05, excess=0.04,
        ))
        db_.commit()
        return [{"report_id": run["id"], "mainline": "AI算力", "outcome": "兑现"}]

    with patch("api.services.mainline_service.evaluate_t1_for_report", new=_fake_evaluate):
        res = mainline_cycle_service.backfill_decision_outcomes(
            db, user_id="u1", as_of_date="2026-08-28"
        )
    assert res["closed"] == 1
    assert res["refreshed_reports"] == 1
    assert res["updated"] == 1 and res["verified"] == 1
    old = db.query(MainlineDecisionDB).filter_by(id="d-old").first()
    assert old.outcome == "verified"
    assert old.forward_excess_ret == pytest.approx(0.04)
    assert "超额" in (old.outcome_note or "")
    # 窗口未关闭的决策不动
    assert db.query(MainlineDecisionDB).filter_by(id="d-new").first().outcome is None


def test_backfill_decision_outcomes_survives_t1_failure():
    """板块历史不可用（网络受限）时诚实跳过：保持 pending，不伪造结果。"""
    db = _mk_db()
    db.add(_report_row("r-old", "u1", "2026-08-20", "AI算力"))
    db.add(_decision("d-old", report_id="r-old", trade_date="2026-08-20", action="持有"))
    db.commit()

    def _boom(db_, run, *, as_of_date=None, ak_module=None):
        raise RuntimeError("板块历史不可用")

    with patch("api.services.mainline_service.evaluate_t1_for_report", new=_boom):
        res = mainline_cycle_service.backfill_decision_outcomes(
            db, user_id="u1", as_of_date="2026-08-28"
        )
    assert res["closed"] == 1
    assert res["updated"] == 0 and res["no_data"] == 1
    assert res["refresh_failed"] and "板块历史不可用" in res["refresh_failed"][0]["reason"]
    assert db.query(MainlineDecisionDB).filter_by(id="d-old").first().outcome is None


def test_backfill_decision_outcomes_records_empty_refresh():
    """评估返回空（暂无前瞻数据）时计入 refresh_no_data，决策保持 pending。"""
    db = _mk_db()
    db.add(_report_row("r-old", "u1", "2026-08-20", "AI算力"))
    db.add(_decision("d-old", report_id="r-old", trade_date="2026-08-20", action="持有"))
    db.commit()

    def _empty(db_, run, *, as_of_date=None, ak_module=None):
        return []

    with patch("api.services.mainline_service.evaluate_t1_for_report", new=_empty):
        res = mainline_cycle_service.backfill_decision_outcomes(
            db, user_id="u1", as_of_date="2026-08-28"
        )
    assert res["closed"] == 1
    assert res["refreshed_reports"] == 0 and res["refresh_no_data"] == 1
    assert res["refresh_failed"] == []
    assert res["updated"] == 0 and res["no_data"] == 1


def test_backfill_decision_outcomes_without_refresh_keeps_pending():
    """关闭补建开关时不触发网络评估，决策保持 pending。"""
    db = _mk_db()
    db.add(_report_row("r-old", "u1", "2026-08-20", "AI算力"))
    db.add(_decision("d-old", report_id="r-old", trade_date="2026-08-20", action="持有"))
    db.commit()

    res = mainline_cycle_service.backfill_decision_outcomes(
        db, user_id="u1", as_of_date="2026-08-28", refresh_t1=False
    )
    assert res["closed"] == 1
    assert res["refreshed_reports"] == 0 and res["updated"] == 0 and res["no_data"] == 1
    assert db.query(MainlineDecisionDB).filter_by(id="d-old").first().outcome is None


def test_decision_position_pct_persisted_from_suggestion_tiers():
    """决策卡仓位必须来自 suggest_action（POSITION_TIERS + 情绪 regime 调节）。"""
    db = _mk_db()
    # 主升 + 情绪亢奋(>85) → 0.15 × 0.5 = 0.075
    for d, s, f, r in [("2026-08-25", 55, 1.0, 50), ("2026-08-26", 62, 2.0, 55), ("2026-08-27", 68, 3.0, 60)]:
        db.add(_report(d, [_ml("AI算力", ["CPO概念"])], [_cand("CPO概念", s, f, r)],
                       emotion={"temperature": 90, "gate": "normal"}))
    db.commit()
    for d in ("2026-08-25", "2026-08-26", "2026-08-27"):
        mainline_cycle_service.run_cycle_analysis(db, d, user_id="u1")

    dec = (
        db.query(MainlineDecisionDB)
        .order_by(MainlineDecisionDB.trade_date.desc())
        .first()
    )
    arc = db.query(MainlineHistoryDB).first()
    assert dec.stage == "主升" and dec.action == "持有"
    assert dec.position_pct == pytest.approx(0.075)
    assert arc.position_pct == pytest.approx(0.075)
    assert dec.emotion_temperature == 90
    assert "仓位减半" in (dec.reason or "")
