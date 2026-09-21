"""M4 测试：mainline_service 落库/查询/候选股动作 + run_mainline_job 端到端。"""

from __future__ import annotations

import asyncio
import os
import tempfile
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.database import Base, MainlineCandidateDB, MainlineReportDB
from api.services import mainline_service


def _mk_db():
    # 文件库 + check_same_thread=False：run_mainline_job 内部经 asyncio.to_thread 跨线程写库
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _fake_result() -> dict:
    return {
        "trade_date": "2026-08-31",
        "perspective": "short",
        "market": {
            "sources": {"industry_spot": "ths", "concept_spot": None},
            "emotion": {"temperature": 94, "regime": "亢奋"},
            "breadth": {"up": 3181, "down": 2217, "flat": 152, "total": 5550},
            "benchmark": {"close": 4625.0, "chg_1d": 0.003, "chg_5d": 0.0135},
            "industry_spot": [{"name": "影视院线", "chg_1d": 6.5}],
            "concept_spot": None,
            "zt_heat": [{"industry": "通用设备", "zt_count": 7}],
        },
        "analyst_report": "主线分析全文",
        "selector_report": "选股全文",
        "mainlines": [
            {
                "name": "AI算力",
                "type": "concept",
                "phase": "主升",
                "confidence": 78,
                "status_vs_yesterday": "延续",
            }
        ],
        "candidates": [
            {
                "symbol": "300308.SZ",
                "name": "中际旭创",
                "mainline": "AI算力",
                "tier": "龙头",
                "score": 82,
                "reasons": ["主力净流入居前"],
                "entry_hint": "等回踩5日线",
                "risk": "高位",
            }
        ],
        "gated_out": ["弱主线"],
        "warnings": ["概念板块涨幅榜不可用"],
    }


# ── 落库与查询 ───────────────────────────────────────────────────


def test_create_and_query_run():
    db = _mk_db()
    mainline_service.create_run(
        db, user_id="u1", run_id="r1", trade_date="2026-08-31", perspective="short", job_id="j1"
    )
    rows = db.query(MainlineReportDB).all()
    assert len(rows) == 1
    assert rows[0].status == "pending"

    run = mainline_service.get_run(db, "u1", "r1")
    assert run["trade_date"] == "2026-08-31"
    assert run["perspective"] == "short"
    # 归属校验：其他用户不可见
    assert mainline_service.get_run(db, "other", "r1") is None


def test_save_run_result_persists_candidates():
    db = _mk_db()
    mainline_service.create_run(db, user_id="u1", run_id="r1", trade_date="2026-08-31", perspective="short")
    mainline_service.save_run_result(db, "r1", _fake_result())

    row = db.query(MainlineReportDB).filter(MainlineReportDB.id == "r1").first()
    assert row.status == "completed"
    assert row.mainlines[0]["name"] == "AI算力"
    assert row.market_snapshot["emotion"]["temperature"] == 94
    assert row.gated_out == ["弱主线"]

    cands = db.query(MainlineCandidateDB).filter(MainlineCandidateDB.report_id == "r1").all()
    assert len(cands) == 1
    assert cands[0].symbol == "300308.SZ"
    assert cands[0].tier == "龙头"

    # 查询
    run = mainline_service.get_run(db, "u1", "r1")
    assert run["status"] == "completed"
    latest = mainline_service.get_latest_run(db, "u1", "short")
    assert latest["id"] == "r1"
    cand = mainline_service.get_candidate(db, "u1", cands[0].id)
    assert cand["symbol"] == "300308.SZ"
    # 其他用户不可见候选
    assert mainline_service.get_candidate(db, "other", cands[0].id) is None


def test_report_payload_exposes_candidate_ids():
    """报告接口必须回填候选股 id。

    回归：candidates 字段原本只是选股 Agent 的原始 JSON（无 id），
    前端「加自选 / 深度分析」按钮拿到 undefined 的 id 后直接 return，点击静默无响应。
    """
    db = _mk_db()
    mainline_service.create_run(db, user_id="u1", run_id="r1", trade_date="2026-08-31", perspective="short")
    mainline_service.save_run_result(db, "r1", _fake_result())

    run = mainline_service.get_run(db, "u1", "r1")
    assert run["candidates"][0].get("id"), "报告 JSON 的候选股必须带 id，否则前端按钮无响应"
    # 回填的 id 必须能被两个候选股动作接口直接命中
    cand = mainline_service.get_candidate(db, "u1", run["candidates"][0]["id"])
    assert cand is not None
    assert cand["symbol"] == "300308.SZ"

    latest = mainline_service.get_latest_run(db, "u1", "short")
    assert latest["candidates"][0]["id"] == run["candidates"][0]["id"]


def test_candidate_ids_paired_by_symbol_and_mainline():
    """多个候选股时：id 必须按 (symbol, mainline) 一一配对，不能错位。"""
    db = _mk_db()
    mainline_service.create_run(db, user_id="u1", run_id="r1", trade_date="2026-08-31", perspective="short")
    res = _fake_result()
    res["candidates"] = [
        dict(res["candidates"][0]),
        {
            "symbol": "688120.SH",
            "name": "华海清科",
            "mainline": "AI算力硬件",
            "tier": "中军",
            "score": 78,
            "reasons": ["资金聚焦度最高"],
            "entry_hint": "等回调",
            "risk": "高位分歧",
        },
    ]
    mainline_service.save_run_result(db, "r1", res)

    run = mainline_service.get_run(db, "u1", "r1")
    pairs = {c["symbol"]: c["id"] for c in run["candidates"]}
    assert len(pairs) == 2
    assert len(set(pairs.values())) == 2, "不同候选股不能共用同一个 id"
    for symbol, candidate_id in pairs.items():
        # 前端就是拿这个 id 去调 /analyze 与 /watchlist，必须落到同一只票
        assert mainline_service.get_candidate(db, "u1", candidate_id)["symbol"] == symbol


def test_system_shared_report_also_exposes_candidate_ids():
    """系统级共享报告（定时任务产物）的候选股同样要带 id。"""
    db = _mk_db()
    mainline_service.create_run(db, user_id="system", run_id="sys-1", trade_date="2026-08-31", perspective="short")
    mainline_service.save_run_result(db, "sys-1", _fake_result())

    # 用户自身无报告 → get_latest_run 回退到系统级共享报告
    latest = mainline_service.get_latest_run(db, "u2", "short")
    assert latest["shared"] is True
    assert latest["candidates"][0].get("id")
    assert mainline_service.get_candidate(db, "u2", latest["candidates"][0]["id"]) is not None


def test_mark_run_failed():
    db = _mk_db()
    mainline_service.create_run(db, user_id="u1", run_id="r1", trade_date="2026-08-31", perspective="short")
    mainline_service.mark_run_failed(db, "r1", "boom")
    row = db.query(MainlineReportDB).filter(MainlineReportDB.id == "r1").first()
    assert row.status == "failed"
    assert "boom" in row.error


def test_list_runs_ordered_and_scoped():
    db = _mk_db()
    mainline_service.create_run(db, user_id="u1", run_id="r1", trade_date="2026-08-30", perspective="short")
    mainline_service.create_run(db, user_id="u1", run_id="r2", trade_date="2026-08-31", perspective="short")
    mainline_service.create_run(db, user_id="u2", run_id="r3", trade_date="2026-08-31", perspective="short")
    runs = mainline_service.list_runs(db, "u1", limit=10)
    assert [r["id"] for r in runs] == ["r2", "r1"]
    assert all(r["user_id"] == "u1" for r in runs)


# ── 任务运行器端到端 ─────────────────────────────────────────────


def test_run_mainline_job_persists_and_emits_events():
    db = _mk_db()
    events: list[tuple[str, str, dict]] = []
    job_updates: dict[str, dict] = {}

    def _set_job(job_id, **kw):
        job_updates.setdefault(job_id, {}).update(kw)

    def _emit(job_id, event, data):
        events.append((job_id, event, data))

    class _FakeCtx:
        def __init__(self, session):
            self._s = session

        def __enter__(self):
            return self._s

        def __exit__(self, *a):
            return False

    mainline_service.create_run(
        db, user_id="u1", run_id="j1", trade_date="2026-08-31", perspective="short", job_id="j1"
    )

    async def _fake_run_analysis(*args, **kwargs):
        return _fake_result()

    with patch(
        "tradingagents.graph.mainline_graph.run_mainline_analysis",
        new=_fake_run_analysis,
    ), patch("api.services.mainline_service.get_db_ctx", side_effect=lambda: _FakeCtx(db)):
        result = asyncio.run(
            mainline_service.run_mainline_job(
                "j1", "u1", "2026-08-31", "short",
                set_job=_set_job, emit_event=_emit,
                config={"llm_provider": "openai", "api_key": "test-key", "deep_think_llm": "m", "quick_think_llm": "m"},
            )
        )

    assert result["mainlines"][0]["name"] == "AI算力"
    assert job_updates["j1"]["status"] == "completed"
    assert job_updates["j1"]["result"]["candidates"] == 1
    events_types = [e for _, e, _ in events]
    assert "mainline.started" in events_types
    assert "mainline.phase" in events_types
    assert "mainline.analyst.done" in events_types
    assert "mainline.selector.done" in events_types
    assert "job.completed" in events_types
    assert job_updates["j1"].get("progress") == 100
    assert job_updates["j1"].get("phase") == "completed"

    row = db.query(MainlineReportDB).filter(MainlineReportDB.id == "j1").first()
    assert row.status == "completed"
    assert row.mainlines[0]["name"] == "AI算力"
    cand = db.query(MainlineCandidateDB).filter(MainlineCandidateDB.report_id == "j1").first()
    assert cand is not None and cand.symbol == "300308.SZ"


def test_run_mainline_job_failure_marks_failed():
    db = _mk_db()
    events: list[tuple[str, str, dict]] = []
    job_updates: dict[str, dict] = {}

    def _set_job(job_id, **kw):
        job_updates.setdefault(job_id, {}).update(kw)

    def _emit(job_id, event, data):
        events.append((job_id, event, data))

    class _FakeCtx:
        def __init__(self, session):
            self._s = session

        def __enter__(self):
            return self._s

        def __exit__(self, *a):
            return False

    mainline_service.create_run(
        db, user_id="u1", run_id="j1", trade_date="2026-08-31", perspective="short", job_id="j1"
    )
    with patch(
        "tradingagents.graph.mainline_graph.run_mainline_analysis",
        new=MagicMock(side_effect=RuntimeError("llm down")),
    ), patch("api.services.mainline_service.get_db_ctx", side_effect=lambda: _FakeCtx(db)):
        with __import__("pytest").raises(RuntimeError):
            asyncio.run(
                mainline_service.run_mainline_job(
                    "j1", "u1", "2026-08-31", "short", set_job=_set_job, emit_event=_emit,
                    config={"llm_provider": "openai", "api_key": "test-key", "deep_think_llm": "m", "quick_think_llm": "m"},
                )
            )
    assert job_updates["j1"]["status"] == "failed"
    assert "llm down" in job_updates["j1"]["error"]
    row = db.query(MainlineReportDB).filter(MainlineReportDB.id == "j1").first()
    assert row.status == "failed"


# ── 状态机闭环（自动携带昨日主线） + T+1 明细 ────────────────────


def test_run_mainline_job_auto_carries_yesterday_mainlines():
    db = _mk_db()
    # 前一天已有一条 completed 报告（含主线）
    db.add(
        __import__("api.database", fromlist=["MainlineReportDB"]).MainlineReportDB(
            id="old-1", user_id="u1", trade_date="2026-08-28",
            perspective="short", status="completed",
            mainlines=[{"name": "AI算力", "type": "concept", "confidence": 78}],
        )
    )
    mainline_service.create_run(
        db, user_id="u1", run_id="j-new", trade_date="2026-08-31", perspective="short", job_id="j-new"
    )
    db.commit()
    events: list[tuple[str, str, dict]] = []
    job_updates: dict[str, dict] = {}
    captured: dict = {}

    def _set_job(job_id, **kw):
        job_updates.setdefault(job_id, {}).update(kw)

    def _emit(job_id, event, data):
        events.append((job_id, event, data))

    class _FakeCtx:
        def __init__(self, session):
            self._s = session

        def __enter__(self):
            return self._s

        def __exit__(self, *a):
            return False

    async def _fake_run_analysis(trade_date, perspective, *, yesterday_mainlines=None, user_focus=None, tracker=None, market_collector=None, include_breadth=False, config=None):
        captured["yesterday_mainlines"] = yesterday_mainlines
        return _fake_result()

    with patch(
        "tradingagents.graph.mainline_graph.run_mainline_analysis", new=_fake_run_analysis
    ), patch("api.services.mainline_service.get_db_ctx", side_effect=lambda: _FakeCtx(db)):
        asyncio.run(
            mainline_service.run_mainline_job(
                "j-new", "u1", "2026-08-31", "short", set_job=_set_job, emit_event=_emit,
                config={"llm_provider": "openai", "api_key": "test-key", "deep_think_llm": "m", "quick_think_llm": "m"},
            )
        )
    # 自动携带昨日主线（状态机闭环）
    assert captured["yesterday_mainlines"] == [{"name": "AI算力", "type": "concept", "confidence": 78}]
    assert any("mainline.started" == e for _, e, _ in events)


def test_list_t1_outcomes_scoped_and_ordered():
    from api.database import MainlineT1OutcomeDB

    db = _mk_db()
    db.add_all(
        [
            MainlineT1OutcomeDB(id="o1", report_id="r1", user_id="u1", mainline="M1", trade_date="2026-08-25", outcome="兑现", excess_ret=0.03),
            MainlineT1OutcomeDB(id="o2", report_id="r1", user_id="u1", mainline="M2", trade_date="2026-08-25", outcome="证伪", excess_ret=-0.02),
            MainlineT1OutcomeDB(id="o3", report_id="r9", user_id="other", mainline="M3", trade_date="2026-08-25", outcome="兑现", excess_ret=0.01),
        ]
    )
    db.commit()
    items = mainline_service.list_t1_outcomes(db, user_id="u1")
    assert len(items) == 2
    by_id = {i["id"]: i for i in items}
    assert by_id["o1"]["outcome"] == "兑现"
    items_r1 = mainline_service.list_t1_outcomes(db, user_id="u1", report_id="r1")
    assert len(items_r1) == 2
    items_other = mainline_service.list_t1_outcomes(db, user_id="other")
    assert [i["id"] for i in items_other] == ["o3"]


# ── 审查修复：system 报告共享回退 + T+1 用户隔离 + market_reading ──


def test_list_runs_falls_back_to_system_shared():
    from api.database import MainlineReportDB

    db = _mk_db()
    # system 有一条已完成报告（模拟定时任务产物），u2 无任何报告
    db.add(
        MainlineReportDB(
            id="sys-1", user_id="system", trade_date="2026-08-30",
            perspective="short", status="completed",
            mainlines=[{"name": "系统主线", "confidence": 70}],
        )
    )
    db.commit()
    runs = mainline_service.list_runs(db, "u2", limit=10)
    assert len(runs) == 1
    assert runs[0]["id"] == "sys-1"
    assert runs[0].get("shared") is True  # 回退系统报告标记共享

    latest = mainline_service.get_latest_run(db, "u2", "short")
    assert latest is not None and latest["id"] == "sys-1" and latest.get("shared") is True

    run = mainline_service.get_run(db, "u2", "sys-1")
    assert run is not None and run.get("shared") is True


def test_list_runs_keeps_user_scoping_when_user_has_runs():
    db = _mk_db()
    mainline_service.create_run(db, user_id="u1", run_id="r1", trade_date="2026-08-31", perspective="short")
    # system 也有报告，但 u1 有自己的 → 不显示 system 的
    db.add(
        __import__("api.database", fromlist=["MainlineReportDB"]).MainlineReportDB(
            id="sys-1", user_id="system", trade_date="2026-08-30",
            perspective="short", status="completed",
        )
    )
    db.commit()
    runs = mainline_service.list_runs(db, "u1", limit=10)
    assert [r["id"] for r in runs] == ["r1"]


def test_t1_overview_user_scoped():
    from api.database import MainlineT1OutcomeDB

    db = _mk_db()
    db.add_all(
        [
            MainlineT1OutcomeDB(id="o1", report_id="r1", user_id="u1", mainline="M1", trade_date="2026-08-25", outcome="兑现", excess_ret=0.03),
            MainlineT1OutcomeDB(id="o2", report_id="r2", user_id="u2", mainline="M2", trade_date="2026-08-25", outcome="证伪", excess_ret=-0.02),
        ]
    )
    db.commit()
    ov1 = mainline_service.t1_overview(db, user_id="u1", days=30)
    assert ov1["total"] == 1
    assert ov1["by_outcome"]["兑现"] == 1
    ov2 = mainline_service.t1_overview(db, user_id="u2", days=30)
    assert ov2["total"] == 1
    assert ov2["by_outcome"]["证伪"] == 1


def test_save_run_result_market_snapshot_includes_rule_candidates():
    db = _mk_db()
    mainline_service.create_run(db, user_id="u1", run_id="r1", trade_date="2026-08-31", perspective="short")
    res = _fake_result()
    # 给 market 补规则层候选特征
    res["market"]["mainline_candidates"] = [
        {"name": "影视院线", "rs20": 0.152, "ma_bullish": True, "rsi14": 68.5, "passes_gate": True, "phase_hint": "主升", "heat_score": 92.5, "strength_score": 75.0, "composite_score": 84.5}
    ]
    mainline_service.save_run_result(db, "r1", res)
    row = db.query(__import__("api.database", fromlist=["MainlineReportDB"]).MainlineReportDB).filter_by(id="r1").first()
    assert row.market_snapshot["rule_candidates"][0]["name"] == "影视院线"
    assert row.market_snapshot["rule_candidates"][0]["passes_gate"] is True


def test_run_mainline_job_with_timeout_marks_failed():
    db = _mk_db()
    job_updates: dict[str, dict] = {}
    events: list[tuple[str, str, dict]] = []

    def _set_job(job_id, **kw):
        job_updates.setdefault(job_id, {}).update(kw)

    def _emit(job_id, event, data):
        events.append((job_id, event, data))

    class _FakeCtx:
        def __init__(self, session):
            self._s = session

        def __enter__(self):
            return self._s

        def __exit__(self, *a):
            return False

    mainline_service.create_run(
        db, user_id="u1", run_id="j-timeout", trade_date="2026-08-31", perspective="short", job_id="j-timeout"
    )

    async def _hang(*args, **kwargs):
        await asyncio.sleep(2)
        return _fake_result()

    with patch(
        "tradingagents.graph.mainline_graph.run_mainline_analysis",
        new=_hang,
    ), patch("api.services.mainline_service.get_db_ctx", side_effect=lambda: _FakeCtx(db)):
        result = asyncio.run(
            mainline_service.run_mainline_job_with_timeout(
                "j-timeout", "u1", "2026-08-31", "short",
                timeout_seconds=0.05,
                set_job=_set_job, emit_event=_emit,
                config={"llm_provider": "openai", "api_key": "test-key", "deep_think_llm": "m", "quick_think_llm": "m"},
            )
        )

    assert result is None
    assert job_updates["j-timeout"]["status"] == "failed"
    assert "超时" in job_updates["j-timeout"]["error"]
    assert any(evt == "job.failed" for _, evt, _ in events)
    row = db.query(MainlineReportDB).filter(MainlineReportDB.id == "j-timeout").first()
    assert row.status == "failed"
    assert "超时" in (row.error or "")


def test_recover_stale_runs_marks_old_pending_failed():
    db = _mk_db()
    from datetime import datetime, timedelta, timezone

    old = datetime.now(timezone.utc) - timedelta(minutes=20)
    db.add(
        MainlineReportDB(
            id="stale-1",
            user_id="u1",
            trade_date="2026-08-31",
            perspective="short",
            status="running",
            created_at=old,
        )
    )
    db.commit()
    n = mainline_service.recover_stale_runs(db, older_than_seconds=600)
    assert n == 1
    row = db.query(MainlineReportDB).filter(MainlineReportDB.id == "stale-1").first()
    assert row.status == "failed"
    assert "超时" in (row.error or "")
    assert (row.market_snapshot or {}).get("progress_logs")
