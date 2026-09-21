"""C3 自动深挖服务测试：可买入筛选、任务创建注入、状态回填。"""

from __future__ import annotations

import os
import tempfile
from unittest.mock import MagicMock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.database import (
    Base,
    MainlineReportDB,
    MainlineTradeCandidateDB,
)
from api.services import mainline_autodive_service as autodive
from api.services import mainline_cycle_service as cycle_svc


def _mk_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _report(mainlines, candidates):
    return MainlineReportDB(
        id="r1", user_id="u1", trade_date="2026-08-27", perspective="short",
        status="completed", mainlines=mainlines, candidates=candidates,
        market_snapshot={"rule_candidates": [], "emotion": {"temperature": 60}},
    )


def _cand(symbol, name, score=80, lianban=0, turnover=5.0, tier="龙头", chg=3.0, source="cons", mainline="AI算力"):
    return {
        "symbol": symbol, "name": name, "mainline": mainline, "tier": tier, "score": score,
        "reasons": ["资金流入"], "entry_hint": "回踩5日线", "risk": "波动大",
        "lianban": lianban, "turnover": turnover, "chg_1d": chg, "source": source,
    }


def test_filter_buyable_gates():
    """数值闸门（fixture 显式提供数值字段时）。

    ⚠️ 注意：`_cand` 造出了 `lianban` / `turnover` / `chg_1d` / `source` 四个字段，
    但线上 `mainline_reports.candidates` **一个都没有**（实测 224 个候选，字段只有
    symbol/name/mainline/tier/score/reasons/entry_hint/risk）。所以本测试验证的是
    "数值字段存在时闸门正确"，**不能**代表生产行为——生产上这三个闸门原本恒不触发。
    真实载荷的覆盖见 `tests/test_chase_risk_filter.py`。
    """
    # 周期主升 + 正常 → buyable
    ok, _ = autodive.filter_buyable(_cand("600111.SH", "北方稀土"), cycle_position="主升")
    assert ok is True
    # 退潮 → 不可买入
    ok, reason = autodive.filter_buyable(_cand("600111.SH", "北方稀土"), cycle_position="退潮")
    assert ok is False and "退潮" in reason
    # 连板 4 → 高位接力
    ok, reason = autodive.filter_buyable(_cand("300308.SZ", "中际旭创", lianban=4), cycle_position="主升")
    assert ok is False and "连板" in reason
    # 换手 1% → 过冷
    ok, reason = autodive.filter_buyable(_cand("300308.SZ", "中际旭创", turnover=1.0), cycle_position="主升")
    assert ok is False and "换手" in reason
    # 单日 +10% → 追高（阈值已从"仅认接近涨停"收紧到 CHASE_1D_PCT）
    ok, reason = autodive.filter_buyable(_cand("300308.SZ", "中际旭创", chg=10.0), cycle_position="主升")
    assert ok is False and "追高" in reason
    # 即使来源不是 cons，追高同样拦截（旧的 source == "cons" 限定无依据）
    ok, reason = autodive.filter_buyable(
        _cand("300308.SZ", "中际旭创", chg=10.0, source="other"), cycle_position="主升"
    )
    assert ok is False and "追高" in reason


def test_run_autodive_creates_tasks():
    db = _mk_db()
    db.add(_report(
        [{"name": "AI算力", "type": "industry", "confidence": 80, "phase": "主升",
          "representative_boards": [{"board": "CPO概念", "chg_1d": 3.0}]}],
        [_cand("600111.SH", "北方稀土", score=90), _cand("300308.SZ", "中际旭创", score=75)],
    ))
    db.commit()
    # 先建周期档案（主升）供筛选
    cycle_svc.run_cycle_analysis(db, "2026-08-27", user_id="u1", report_id="r1")

    created: list[dict] = []
    create_task = MagicMock(side_effect=lambda symbol: created.append(symbol) or f"job-{symbol[:6]}")
    res = autodive.run_autodive(db, "2026-08-27", user_id="u1", report_id="r1", create_task=create_task, max_dive=5)
    assert res["buyable_count"] == 2
    assert len(res["created_tasks"]) == 2
    assert all(t["job_id"].startswith("job-") for t in res["created_tasks"])

    rows = db.query(MainlineTradeCandidateDB).all()
    assert len(rows) == 2
    assert all(r.buyable for r in rows)
    assert all(r.deep_dive_status == "queued" for r in rows)
    # 仓位档位：90 → 重仓，75 → 标准
    tiers = {r.symbol: r.position_tier for r in rows}
    assert tiers["600111.SH"] == "重仓"
    assert tiers["300308.SZ"] == "标准"


def test_run_autodive_skips_retreat_mainline():
    db = _mk_db()
    # 主线退潮（强度连降+资金转负）
    snaps = [("2026-08-21", 80, 5.0), ("2026-08-22", 78, 4.0), ("2026-08-25", 74, 2.0),
             ("2026-08-26", 68, -1.0), ("2026-08-27", 60, -3.0)]
    for i, (d, s, f) in enumerate(snaps):
        db.add(MainlineReportDB(
            id=f"r-{i}", user_id="u1", trade_date=d, perspective="short", status="completed",
            mainlines=[{"name": "退潮主线", "type": "industry", "confidence": s, "phase": "退潮",
                        "representative_boards": [{"board": "旧板块", "chg_1d": 1.0}]}],
            candidates=[_cand("600111.SH", "北方稀土", score=85, mainline="退潮主线")],
            market_snapshot={"rule_candidates": [{"name": "旧板块", "strength": s, "heat": 60, "net_inflow_5d": f, "rsi14": 70}], "emotion": {"temperature": 60}},
        ))
    db.commit()
    for d in ("2026-08-21", "2026-08-22", "2026-08-25", "2026-08-26", "2026-08-27"):
        cycle_svc.run_cycle_analysis(db, d, user_id="u1", report_id=_rid_for(db, d))
    res = autodive.run_autodive(db, "2026-08-27", user_id="u1", create_task=MagicMock(return_value="j"))
    assert res["buyable_count"] == 0
    assert len(res["created_tasks"]) == 0
    assert any("退潮" in s.get("reason", "") for s in res["skipped"])


def _rid_for(db, d):
    return db.query(MainlineReportDB).filter_by(trade_date=d).first().id


def test_mark_deep_dive_completed():
    db = _mk_db()
    db.add(_report(
        [{"name": "AI算力", "type": "industry", "confidence": 80, "phase": "主升", "representative_boards": [{"board": "CPO概念"}]}],
        [_cand("600111.SH", "北方稀土", score=90)],
    ))
    db.commit()
    cycle_svc.run_cycle_analysis(db, "2026-08-27", user_id="u1", report_id="r1")
    autodive.run_autodive(db, "2026-08-27", user_id="u1", report_id="r1", create_task=lambda s: "job-abc")
    n = autodive.mark_deep_dive_completed(db, job_id="job-abc", deep_dive_report_id="rep-xyz")
    assert n == 1
    row = db.query(MainlineTradeCandidateDB).first()
    assert row.deep_dive_status == "completed"
    assert row.deep_dive_report_id == "rep-xyz"


def test_max_daily_dive_limit():
    db = _mk_db()
    db.add(_report(
        [{"name": "AI算力", "type": "industry", "confidence": 80, "phase": "主升", "representative_boards": [{"board": "CPO概念"}]}],
        [_cand(f"60010{i}.SH", f"股{i}", score=80) for i in range(1, 8)],
    ))
    db.commit()
    cycle_svc.run_cycle_analysis(db, "2026-08-27", user_id="u1", report_id="r1")
    res = autodive.run_autodive(db, "2026-08-27", user_id="u1", report_id="r1",
                                create_task=lambda s: f"j-{s}", max_dive=3)
    assert res["buyable_count"] == 7
    assert len(res["created_tasks"]) == 3          # 上限 3
    assert any("上限" in s.get("reason", "") for s in res["skipped"])


def test_run_autodive_idempotent_rerun():
    """回归：同一报告重复触发自动深挖不应再报 UNIQUE 约束错误。

    历史 bug：run_autodive 每次无条件 INSERT 全部候选行，第二次运行撞
    (report_id, symbol) 唯一约束 → IntegrityError → 端点 500。
    """
    db = _mk_db()
    db.add(_report(
        [{"name": "AI算力", "type": "industry", "confidence": 80, "phase": "主升", "representative_boards": [{"board": "CPO概念"}]}],
        [_cand("600111.SH", "北方稀土", score=90), _cand("300308.SZ", "中际旭创", score=75)],
    ))
    db.commit()
    cycle_svc.run_cycle_analysis(db, "2026-08-27", user_id="u1", report_id="r1")

    # 第一次运行：创建 2 个任务
    res1 = autodive.run_autodive(db, "2026-08-27", user_id="u1", report_id="r1",
                                 create_task=lambda s: f"job-{s[:6]}", max_dive=5)
    assert res1["buyable_count"] == 2
    assert len(res1["created_tasks"]) == 2
    rows1 = db.query(MainlineTradeCandidateDB).all()
    assert len(rows1) == 2
    assert all(r.deep_dive_status == "queued" for r in rows1)

    # 第二次运行：不再重复创建（queued 被跳过），行数不变、无异常
    res2 = autodive.run_autodive(db, "2026-08-27", user_id="u1", report_id="r1",
                                 create_task=lambda s: f"job-{s[:6]}", max_dive=5)
    assert res2["buyable_count"] == 2
    assert len(res2["created_tasks"]) == 0          # 无新任务
    assert all("已有深挖任务" in s.get("reason", "") for s in res2["skipped"])
    rows2 = db.query(MainlineTradeCandidateDB).all()
    assert len(rows2) == 2                           # 未重复 INSERT
    assert all(r.deep_dive_status == "queued" for r in rows2)

    # 第三次运行：failed 状态（无 job）会被重试重新创建
    from sqlalchemy import update
    db.execute(
        update(MainlineTradeCandidateDB)
        .where(MainlineTradeCandidateDB.symbol == "600111.SH")
        .values(deep_dive_status="failed", deep_dive_error="RuntimeError: no running event loop", deep_dive_job_id=None)
    )
    db.commit()
    res3 = autodive.run_autodive(db, "2026-08-27", user_id="u1", report_id="r1",
                                 create_task=lambda s: f"job-{s[:6]}", max_dive=5)
    assert any(t["symbol"] == "600111.SH" for t in res3["created_tasks"])   # failed → 重试创建
    assert len(db.query(MainlineTradeCandidateDB).all()) == 2               # 仍无重复行
    row = db.query(MainlineTradeCandidateDB).filter_by(symbol="600111.SH").first()
    assert row.deep_dive_status == "queued"
    assert row.deep_dive_job_id is not None
