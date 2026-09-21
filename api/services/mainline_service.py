"""市场主线服务（M4）：主线任务编排、落库、查询、候选股动作。

依赖：
- tradingagents.graph.mainline_graph.run_mainline_analysis（M3 Agent 链路）
- tradingagents.dataflows.market_collector.MarketCollector（M2 多源采集）
- api.database.MainlineReportDB / MainlineCandidateDB

任务运行器 run_mainline_job 通过注入的 set_job / emit_event 回调接入
api/main.py 的 in-memory job 系统（避免本模块反向依赖 main.py）。
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

import pandas as pd
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from api.database import (
    MainlineCandidateDB,
    MainlineReportDB,
    MainlineT1OutcomeDB,
    get_db_ctx,
)

_UTC = timezone.utc

# 主线任务默认 10 分钟超时（可用 TA_MAINLINE_JOB_TIMEOUT 覆盖，单位秒）
MAINLINE_JOB_TIMEOUT_SECONDS = int(os.getenv("TA_MAINLINE_JOB_TIMEOUT", "600"))

_PHASE_PROGRESS: dict[str, tuple[int, str]] = {
    "pending": (8, "任务已创建，等待执行"),
    "started": (16, "正在准备市场数据与模型"),
    "collecting": (28, "正在采集东财/同花顺/新浪板块数据"),
    "analyst": (52, "大模型正在识别市场主线"),
    "selector": (78, "大模型正在主线内选股"),
    "persisting": (94, "正在保存主线报告"),
    "completed": (100, "主线报告已生成"),
    "failed": (100, "主线分析失败"),
}


def _utcnow() -> str:
    return datetime.now(_UTC).isoformat(timespec="seconds")


def _to_dict(row) -> dict:
    d = {c.name: getattr(row, c.name) for c in row.__table__.columns}
    for k in ("created_at", "started_at", "finished_at", "updated_at"):
        if isinstance(d.get(k), datetime):
            d[k] = d[k].isoformat(timespec="seconds")
    return d


# ── 落库 ─────────────────────────────────────────────────────────


def create_run(
    db: Session,
    *,
    user_id: str,
    run_id: str,
    trade_date: str,
    perspective: str = "short",
    job_id: Optional[str] = None,
) -> str:
    """创建主线任务记录（status=pending）。"""
    row = MainlineReportDB(
        id=run_id,
        user_id=user_id,
        trade_date=trade_date,
        perspective=perspective,
        status="pending",
        job_id=job_id or run_id,
        created_at=datetime.now(_UTC),
    )
    db.add(row)
    db.commit()
    return run_id


def save_run_result(db: Session, run_id: str, result: dict) -> None:
    """主线任务完成：更新报告 + 写候选股明细。"""
    now = datetime.now(_UTC)
    row = db.query(MainlineReportDB).filter(MainlineReportDB.id == run_id).first()
    if row is None:
        raise ValueError(f"mainline run not found: {run_id}")
    mainlines = result.get("mainlines") or []
    candidates = result.get("candidates") or []
    row.status = "completed"
    row.summary = str(
        result.get("market_reading")
        or ("；".join(str(m.get("name")) for m in mainlines[:3]) + "。" if mainlines else "")
        or (result.get("analyst_report") or "")[:200]
        or "（无结论）"
    )
    row.mainlines = mainlines
    row.candidates = candidates
    row.analyst_report = result.get("analyst_report")
    row.selector_report = result.get("selector_report", "")
    row.gated_out = result.get("gated_out") or []
    row.warnings = result.get("warnings") or []
    market = result.get("market") or {}
    row.market_snapshot = {
        "sources": market.get("sources"),
        "emotion": market.get("emotion"),
        "breadth": market.get("breadth"),
        "benchmark": market.get("benchmark"),
        "industry_spot_top10": (market.get("industry_spot") or [])[:10],
        "concept_spot_top10": (market.get("concept_spot") or [])[:10],
        "zt_heat_top10": (market.get("zt_heat") or [])[:10],
        # 规则层 v2 候选特征（RS20/均线/RSI/硬门槛/阶段预判/双维度分数），供前端展示
        "rule_candidates": (market.get("mainline_candidates") or [])[:15],
    }
    row.finished_at = now
    row.updated_at = now
    db.add(row)
    # 候选股明细
    db.query(MainlineCandidateDB).filter(MainlineCandidateDB.report_id == run_id).delete()
    for c in candidates:
        db.add(
            MainlineCandidateDB(
                id=uuid.uuid4().hex,
                report_id=run_id,
                user_id=row.user_id,
                mainline=str(c.get("mainline") or "")[:100],
                symbol=str(c.get("symbol") or ""),
                name=str(c.get("name") or "")[:50],
                tier=str(c.get("tier") or "")[:20],
                score=c.get("score"),
                reasons=c.get("reasons"),
                entry_hint=c.get("entry_hint"),
                risk=c.get("risk"),
                created_at=now,
            )
        )
    db.commit()


def mark_run_failed(db: Session, run_id: str, error: str) -> None:
    row = db.query(MainlineReportDB).filter(MainlineReportDB.id == run_id).first()
    if row is None:
        return
    row.status = "failed"
    row.error = str(error)[:2000]
    row.finished_at = datetime.now(_UTC)
    row.updated_at = row.finished_at
    db.commit()


def mark_run_running(db: Session, run_id: str) -> None:
    row = db.query(MainlineReportDB).filter(MainlineReportDB.id == run_id).first()
    if row is None:
        return
    row.status = "running"
    row.started_at = datetime.now(_UTC)
    db.commit()


def update_run_progress(
    db: Session,
    run_id: str,
    *,
    phase: str,
    detail: Optional[str] = None,
    log: Optional[str] = None,
) -> None:
    """把阶段进度写入 market_snapshot，刷新页面后仍可恢复。"""
    row = db.query(MainlineReportDB).filter(MainlineReportDB.id == run_id).first()
    if row is None:
        return
    percent, default_detail = _PHASE_PROGRESS.get(phase, (20, phase))
    snap = dict(row.market_snapshot or {})
    logs = list(snap.get("progress_logs") or [])
    message = (log or detail or default_detail or phase).strip()
    if message:
        logs.append({"at": _utcnow(), "phase": phase, "message": message[:500]})
        logs = logs[-50:]
    snap["progress"] = {
        "phase": phase,
        "percent": percent,
        "detail": (detail or default_detail)[:200],
        "updated_at": _utcnow(),
    }
    snap["progress_logs"] = logs
    row.market_snapshot = snap
    flag_modified(row, "market_snapshot")
    row.updated_at = datetime.now(_UTC)
    db.commit()


def recover_stale_runs(db: Session, *, older_than_seconds: Optional[int] = None) -> int:
    """启动时回收超时仍停在 pending/running 的主线任务，避免幽灵挂起。"""
    timeout = older_than_seconds if older_than_seconds is not None else MAINLINE_JOB_TIMEOUT_SECONDS
    cutoff = datetime.now(_UTC) - timedelta(seconds=max(60, timeout))
    rows = (
        db.query(MainlineReportDB)
        .filter(
            MainlineReportDB.status.in_(("pending", "running")),
            MainlineReportDB.created_at < cutoff,
        )
        .all()
    )
    if not rows:
        return 0
    err = f"任务超时或服务重启（超过 {timeout} 秒未完成），已自动终止"
    now = datetime.now(_UTC)
    for row in rows:
        row.status = "failed"
        row.error = (row.error or err)[:2000]
        row.finished_at = now
        row.updated_at = now
        snap = dict(row.market_snapshot or {})
        logs = list(snap.get("progress_logs") or [])
        logs.append({"at": _utcnow(), "phase": "failed", "message": err})
        snap["progress_logs"] = logs[-50:]
        snap["progress"] = {"phase": "failed", "percent": 100, "detail": err, "updated_at": _utcnow()}
        row.market_snapshot = snap
        flag_modified(row, "market_snapshot")
    db.commit()
    return len(rows)


# ── 查询 ─────────────────────────────────────────────────────────


def _strip_payload(d: dict) -> dict:
    d.pop("analyst_report", None)
    d.pop("selector_report", None)
    d.pop("market_snapshot", None)
    return d


def _attach_candidate_ids(db: Session, report: Optional[dict]) -> Optional[dict]:
    """把候选股明细表的主键回填进报告 JSON。

    报告的 candidates 字段是选股 Agent 的原始输出（只有 symbol/name/tier/score 等），
    而「一键加自选 / 深度分析」两个接口是按 mainline_candidates.id 定位标的的，
    因此这里按 (symbol, mainline) 把落库时生成的 id 补回去。
    否则前端拿到的是没有 id 的候选股，按钮会因为 `if (!c.id) return` 而静默无响应。
    """
    if not report:
        return report
    cands = report.get("candidates") or []
    report_id = report.get("id")
    if not cands or not report_id:
        return report
    rows = (
        db.query(MainlineCandidateDB)
        .filter(MainlineCandidateDB.report_id == report_id)
        .order_by(MainlineCandidateDB.created_at.asc())
        .all()
    )
    if not rows:
        return report
    buckets: dict[tuple[str, str], list[str]] = {}
    for r in rows:
        buckets.setdefault((str(r.symbol or ""), str(r.mainline or "")), []).append(r.id)
    # 复制一份，避免就地修改挂在 ORM 实例上的 JSON 属性
    merged: list[Any] = []
    for c in cands:
        item = dict(c) if isinstance(c, dict) else c
        if isinstance(item, dict) and not item.get("id"):
            ids = buckets.get((str(item.get("symbol") or ""), str(item.get("mainline") or "")))
            if ids:
                item["id"] = ids.pop(0)
        merged.append(item)
    report["candidates"] = merged
    return report


def list_runs(db: Session, user_id: str, limit: int = 20) -> list[dict]:
    """当前用户的主线报告列表；用户无数据时回退显示系统级（定时任务）报告（shared=True）。"""
    cap = min(max(limit, 1), 100)
    rows = (
        db.query(MainlineReportDB)
        .filter(MainlineReportDB.user_id == user_id)
        .order_by(MainlineReportDB.created_at.desc())
        .limit(cap)
        .all()
    )
    out = []
    for r in rows:
        out.append(_strip_payload(_to_dict(r)))
    if not out:
        # 回退：系统级每日主线（定时任务产物，全用户只读共享）
        sys_rows = (
            db.query(MainlineReportDB)
            .filter(MainlineReportDB.user_id == "system")
            .order_by(MainlineReportDB.created_at.desc())
            .limit(cap)
            .all()
        )
        for r in sys_rows:
            d = _strip_payload(_to_dict(r))
            d["shared"] = True
            out.append(d)
    return out


def get_run(db: Session, user_id: str, run_id: str) -> Optional[dict]:
    row = (
        db.query(MainlineReportDB)
        .filter(MainlineReportDB.id == run_id, MainlineReportDB.user_id == user_id)
        .first()
    )
    if row is None:
        # 回退：系统级共享报告
        row = (
            db.query(MainlineReportDB)
            .filter(MainlineReportDB.id == run_id, MainlineReportDB.user_id == "system")
            .first()
        )
    if row is None:
        return None
    d = _to_dict(row)
    if row.user_id == "system":
        d["shared"] = True
    return _attach_candidate_ids(db, d)


def get_latest_run(db: Session, user_id: str, perspective: str = "short") -> Optional[dict]:
    row = (
        db.query(MainlineReportDB)
        .filter(MainlineReportDB.user_id == user_id, MainlineReportDB.perspective == perspective)
        .order_by(MainlineReportDB.created_at.desc())
        .first()
    )
    shared = False
    if row is None:
        row = (
            db.query(MainlineReportDB)
            .filter(MainlineReportDB.user_id == "system", MainlineReportDB.perspective == perspective)
            .order_by(MainlineReportDB.created_at.desc())
            .first()
        )
        shared = row is not None
    if row is None:
        return None
    d = _to_dict(row)
    if shared:
        d["shared"] = True
    return _attach_candidate_ids(db, d)


def get_candidate(db: Session, user_id: str, candidate_id: str) -> Optional[dict]:
    row = (
        db.query(MainlineCandidateDB)
        .join(MainlineReportDB, MainlineCandidateDB.report_id == MainlineReportDB.id)
        .filter(
            MainlineCandidateDB.id == candidate_id,
            MainlineReportDB.user_id == user_id,
        )
        .first()
    )
    if row is None:
        row = (
            db.query(MainlineCandidateDB)
            .join(MainlineReportDB, MainlineCandidateDB.report_id == MainlineReportDB.id)
            .filter(
                MainlineCandidateDB.id == candidate_id,
                MainlineReportDB.user_id == "system",
            )
            .first()
        )
    return _to_dict(row) if row else None


# ── 任务运行器 ───────────────────────────────────────────────────


class _MainlineTracker:
    """轻量 tracker：把 Agent token 流转发为 job 事件（复用现有 SSE 事件流）。"""

    def __init__(self, job_id: str, emit_event: Callable, on_phase: Optional[Callable] = None):
        self._job_id = job_id
        self._emit = emit_event
        self._on_phase = on_phase
        self._seen: set[str] = set()

    def _emit_token(self, agent: str, key: str, content: str) -> None:
        try:
            if agent not in self._seen:
                self._seen.add(agent)
                phase = "analyst" if "Analyst" in agent else "selector"
                if self._on_phase is not None:
                    self._on_phase(phase, f"{agent} 正在撰写报告")
                self._emit(
                    self._job_id,
                    "agent.milestone",
                    {"title": agent, "agent": agent, "phase": phase},
                )
            self._emit(
                self._job_id,
                "agent.token",
                {"agent": agent, "key": key, "content": content},
            )
        except Exception:
            pass


def _latest_completed_mainlines(db: Session, before_date: str, exclude_run_id: Optional[str] = None, limit: int = 3) -> list[dict]:
    """取最近一条已完成主线报告的主线列表，作为状态机输入（昨日主线闭环）。"""
    rows = (
        db.query(MainlineReportDB)
        .filter(
            MainlineReportDB.status == "completed",
            MainlineReportDB.trade_date < before_date,
        )
        .order_by(MainlineReportDB.created_at.desc())
        .limit(limit)
        .all()
    )
    out: list[dict] = []
    for r in rows:
        if r.id == exclude_run_id:
            continue
        out.extend(r.mainlines or [])
        if out:
            break
    return out


def _persist_phase(run_id: str, phase: str, detail: Optional[str] = None, log: Optional[str] = None) -> None:
    try:
        with get_db_ctx() as db:
            update_run_progress(db, run_id, phase=phase, detail=detail, log=log)
    except Exception:
        pass


async def run_mainline_job(
    job_id: str,
    user_id: str,
    trade_date: str,
    perspective: str = "short",
    *,
    user_focus: Optional[str] = None,
    yesterday_mainlines: Optional[list] = None,
    set_job: Optional[Callable] = None,
    emit_event: Optional[Callable] = None,
    market_collector=None,
    config: Optional[dict] = None,
) -> dict:
    """主线任务执行器：采集 → 主线识别 → 选股 → 落库 → 更新 job。

    set_job(job_id, **kwargs) / emit_event(job_id, event, data) 由 api/main.py 注入。
    - yesterday_mainlines 未传时自动携带最近一条已完成报告的主线（状态机闭环）；
    - config 可覆盖 LLM 运行时配置（provider/base_url/api_key/模型），默认用 DEFAULT_CONFIG；
    - 返回 run_mainline_analysis 的完整结果（供测试直接断言）。
    """
    from tradingagents.graph.mainline_graph import run_mainline_analysis

    def _set(**kw):
        if set_job is not None:
            set_job(job_id, **kw)

    def _emit(event: str, data: dict):
        if emit_event is not None:
            emit_event(job_id, event, data)

    def _phase(phase: str, detail: Optional[str] = None, log: Optional[str] = None) -> None:
        percent, default_detail = _PHASE_PROGRESS.get(phase, (20, phase))
        text = detail or default_detail
        _set(status="running", phase=phase, progress=percent, progress_detail=text)
        _emit("mainline.phase", {"phase": phase, "progress": percent, "detail": text})
        _persist_phase(job_id, phase, detail=text, log=log or text)

    _set(status="running", started_at=_utcnow(), phase="started", progress=16, progress_detail=_PHASE_PROGRESS["started"][1])
    with get_db_ctx() as db:
        mark_run_running(db, run_id=job_id)
        if yesterday_mainlines is None:
            yesterday_mainlines = _latest_completed_mainlines(db, before_date=trade_date, exclude_run_id=job_id)
    _emit("mainline.started", {"trade_date": trade_date, "perspective": perspective, "yesterday_mainlines": len(yesterday_mainlines)})
    _phase("collecting", log="开始采集板块数据")

    tracker = _MainlineTracker(job_id, emit_event, on_phase=_phase) if emit_event else _MainlineTracker(job_id, lambda *a, **k: None, on_phase=_phase)
    try:
        # 前置校验：LLM 配置必须可用，否则给出可读指引（而不是裸 OpenAIError）
        _cfg = dict(config or {})
        if not _cfg.get("api_key"):
            raise ValueError(
                "未配置 LLM API Key：请在【设置】→模型配置中保存 API Key，"
                "或在【模型管理】中新建/启用一个模型配置后重试"
            )
        if not _cfg.get("deep_think_llm") or not _cfg.get("quick_think_llm"):
            raise ValueError(
                "未配置 LLM 模型：请在【设置】→模型配置中填写深度/快速模型名，"
                "或确认【模型管理】中启用的模型配置完整"
            )
        result = await run_mainline_analysis(
            trade_date,
            perspective,
            yesterday_mainlines=yesterday_mainlines,
            user_focus=user_focus,
            tracker=tracker,
            market_collector=market_collector,
            include_breadth=True,
            config=config,
        )
        mainlines = result.get("mainlines") or []
        candidates = result.get("candidates") or []
        _emit(
            "mainline.analyst.done",
            {"mainlines": len(mainlines), "names": [m.get("name") for m in mainlines]},
        )
        _emit(
            "mainline.selector.done",
            {"candidates": len(candidates), "gated_out": result.get("gated_out") or []},
        )
        _phase("persisting", log=f"识别到 {len(mainlines)} 条主线，候选股 {len(candidates)} 只，正在落库")

        def _persist():
            with get_db_ctx() as db:
                save_run_result(db, job_id, result)

        await asyncio.to_thread(_persist)

        # 闭环：顺带评估该用户既往主线报告的 T+1 兑现（昨日主线今日是否兑现，无需手动刷新）
        try:
            def _eval_t1():
                with get_db_ctx() as db:
                    return refresh_t1_outcomes(db, user_id=user_id)

            await asyncio.to_thread(_eval_t1)
        except Exception as exc:
            _log_error(f"[Mainline {job_id}] T+1 auto-eval skipped: {type(exc).__name__}: {exc}")
        summary = {
            "trade_date": trade_date,
            "perspective": perspective,
            "mainlines": [m.get("name") for m in mainlines],
            "candidates": len(candidates),
        }
        _set(status="completed", finished_at=_utcnow(), result=summary, decision=None, phase="completed", progress=100, progress_detail="主线报告已生成")
        _emit("job.completed", {"job_id": job_id, "summary": summary})
        return result
    except Exception as exc:
        _log_error(f"[Mainline {job_id}] failed: {type(exc).__name__}: {exc}")
        err_msg = f"{type(exc).__name__}: {str(exc)[:2000]}"

        def _mark_failed():
            with get_db_ctx() as db:
                mark_run_failed(db, job_id, err_msg)
                update_run_progress(db, job_id, phase="failed", detail=err_msg, log=err_msg)

        try:
            await asyncio.to_thread(_mark_failed)
        except Exception:
            pass
        _set(status="failed", error=err_msg, finished_at=_utcnow(), phase="failed", progress=100, progress_detail=err_msg)
        _emit("job.failed", {"job_id": job_id, "error": err_msg})
        raise


async def run_mainline_job_with_timeout(
    job_id: str,
    user_id: str,
    trade_date: str,
    perspective: str = "short",
    *,
    timeout_seconds: Optional[int] = None,
    user_focus: Optional[str] = None,
    yesterday_mainlines: Optional[list] = None,
    set_job: Optional[Callable] = None,
    emit_event: Optional[Callable] = None,
    market_collector=None,
    config: Optional[dict] = None,
) -> Optional[dict]:
    """带超时的主线任务：超时后标记失败，不 cancel 内部协程（避免卡在 to_thread）。"""
    timeout = MAINLINE_JOB_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    inner = asyncio.create_task(
        run_mainline_job(
            job_id,
            user_id,
            trade_date,
            perspective,
            user_focus=user_focus,
            yesterday_mainlines=yesterday_mainlines,
            set_job=set_job,
            emit_event=emit_event,
            market_collector=market_collector,
            config=config,
        )
    )
    done, _ = await asyncio.wait({inner}, timeout=timeout)
    if inner in done:
        if not inner.cancelled() and inner.exception():
            _log_error(f"[Mainline {job_id}] failed: {inner.exception()}")
            return None
        return inner.result()

    err_msg = f"任务超时（超过 {timeout} 秒 / {max(1, timeout // 60)} 分钟），已自动终止"
    _log_error(f"[Mainline {job_id}] {err_msg}")
    if set_job is not None:
        set_job(
            job_id,
            status="failed",
            error=err_msg,
            finished_at=_utcnow(),
            phase="failed",
            progress=100,
            progress_detail=err_msg,
        )
    if emit_event is not None:
        emit_event(job_id, "job.failed", {"job_id": job_id, "error": err_msg})
    try:
        with get_db_ctx() as db:
            mark_run_failed(db, job_id, err_msg)
            update_run_progress(db, job_id, phase="failed", detail=err_msg, log=err_msg)
    except Exception:
        pass
    return None


def _log_error(msg: str) -> None:
    import logging

    logging.getLogger(__name__).error(msg)


# ── 序列化辅助（SSE/前端友好） ───────────────────────────────────


def run_to_summary_dict(result: dict) -> dict:
    """把 run_mainline_analysis 结果压成前端可直接渲染的轻量结构。"""
    market = result.get("market") or {}
    return {
        "trade_date": result.get("trade_date"),
        "perspective": result.get("perspective"),
        "emotion": market.get("emotion"),
        "breadth": market.get("breadth"),
        "benchmark": market.get("benchmark"),
        "sources": market.get("sources"),
        "mainlines": result.get("mainlines") or [],
        "candidates": result.get("candidates") or [],
        "gated_out": result.get("gated_out") or [],
        "warnings": result.get("warnings") or [],
        "analyst_report": result.get("analyst_report") or "",
    }


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


# ── T+1 主线兑现跟踪（M6） ───────────────────────────────────────


def _board_hist_close(ak_module, board_name: str, sector_type: str, trade_date: str, check_date: str) -> Optional[pd.Series]:
    """板块历史收盘（东财优先，THS 兜底），返回 index=date(str) 的 Series。

    只接受能覆盖到 check_date 附近的近期数据；THS 板块指数历史停更于 2024-01，
    若最后日期距 check_date 超过 15 个自然日则视为陈旧，返回 None（诚实降级，不造假）。
    """
    from tradingagents.dataflows.providers.cn_akshare_provider import (
        fetch_board_hist_df,
        fetch_ths_board_index_df,
    )

    def _fresh(s: pd.Series) -> Optional[pd.Series]:
        if s is None or len(s) < 2:
            return None
        try:
            last = pd.to_datetime(s.index).max()
            ref = pd.to_datetime(check_date)
            if (ref - last).days > 15:
                return None
        except Exception:
            return None
        return s

    try:
        h = fetch_board_hist_df(ak_module, board_name, sector_type, start_date=trade_date, end_date=check_date)
        if h is not None and not h.empty and "close" in h.columns and "date" in h.columns:
            s = pd.to_numeric(h["close"], errors="coerce")
            s.index = h["date"].astype(str)
            s = s[~s.index.duplicated(keep="last")].sort_index()
            fresh = _fresh(s)
            if fresh is not None:
                return fresh
    except Exception:
        pass
    try:
        h = fetch_ths_board_index_df(ak_module, board_name, sector_type)
        if h is not None and not h.empty and "close" in h.columns and "date" in h.columns:
            s = pd.to_numeric(h["close"], errors="coerce")
            s.index = h["date"].astype(str)
            s = s[~s.index.duplicated(keep="last")].sort_index()
            return _fresh(s)
    except Exception:
        pass
    return None


def evaluate_t1_for_report(
    db: Session,
    run: dict,
    *,
    as_of_date: Optional[str] = None,
    ak_module=None,
) -> list[dict]:
    """评估一条主线报告的 T+1 兑现：主线代表板块自 report.trade_date 起的前瞻收益 vs 基准。

    判定规则：
      board>0 且 excess>0 → 兑现；board<0 且 excess<0 → 证伪；
      board>0 但跑输基准 → 走平(跑输基准)；board<=0 但抗跌 → 弱兑现(抗跌)；无数据 → 数据不足。
    结果 upsert 到 mainline_t1_outcomes（report_id+mainline 唯一）。
    """
    from tradingagents.dataflows.mainline_backtest import fetch_benchmark_close

    if ak_module is None:
        import akshare as ak  # type: ignore

        ak_module = ak
    trade_date = run.get("trade_date")
    mainlines = run.get("mainlines") or []
    report_id = run.get("id")
    if not trade_date or not mainlines or not report_id:
        return []

    try:
        bench = fetch_benchmark_close(ak_module, cache=True)
    except Exception:
        bench = pd.Series(dtype=float)

    le = bench.index[bench.index <= (as_of_date or trade_date)]
    if len(le) == 0:
        return []
    check_date = str(le[-1])
    if check_date <= trade_date:
        return []  # 需要至少一个交易日的前瞻数据

    bench_ret: Optional[float] = None
    try:
        bench_ret = float(bench[check_date] / bench[trade_date] - 1)
    except Exception:
        pass

    out: list[dict] = []
    for m in mainlines:
        name = str(m.get("name") or "").strip()
        if not name:
            continue
        boards = m.get("representative_boards") or []
        board_name = None
        if boards:
            board_name = boards[0].get("board") if isinstance(boards[0], dict) else str(boards[0])
        board_ret: Optional[float] = None
        if board_name:
            s = _board_hist_close(ak_module, board_name, m.get("type", "concept"), trade_date, check_date)
            if s is not None and len(s) >= 2:
                # 严格按 [trade_date, check_date] 窗口切片（THS 兜底返回全量历史，必须切窗）
                sub = s[(s.index >= trade_date) & (s.index <= check_date)]
                if len(sub) >= 2:
                    try:
                        board_ret = float(sub.iloc[-1] / sub.iloc[0] - 1)
                    except Exception:
                        board_ret = None

        if board_ret is None:
            outcome = "数据不足"
            notes = "板块历史不可用"
            excess: Optional[float] = None
        elif bench_ret is None:
            excess = None
            outcome = "兑现" if board_ret > 0 else "证伪"
            notes = "无基准数据，按绝对收益判定"
        else:
            excess = board_ret - bench_ret
            if board_ret > 0 and excess > 0:
                outcome = "兑现"
            elif board_ret < 0 and excess < 0:
                outcome = "证伪"
            elif board_ret > 0:
                outcome = "走平(跑输基准)"
            else:
                outcome = "弱兑现(抗跌)"
            notes = f"板块{board_ret * 100:.1f}% vs 基准{bench_ret * 100:.1f}%"

        row = (
            db.query(MainlineT1OutcomeDB)
            .filter(MainlineT1OutcomeDB.report_id == report_id, MainlineT1OutcomeDB.mainline == name)
            .first()
        )
        if row is None:
            row = MainlineT1OutcomeDB(
                id=uuid.uuid4().hex,
                report_id=report_id,
                user_id=run.get("user_id"),
                mainline=name,
                created_at=datetime.now(_UTC),
            )
            db.add(row)
        row.trade_date = trade_date
        row.check_date = check_date
        row.board = board_name
        row.board_fwd_ret = round(board_ret, 6) if board_ret is not None else None
        row.benchmark_fwd_ret = round(bench_ret, 6) if bench_ret is not None else None
        row.excess_ret = round(excess, 6) if excess is not None else None
        row.outcome = outcome
        row.notes = notes
        out.append(
            {
                "report_id": report_id,
                "mainline": name,
                "trade_date": trade_date,
                "check_date": check_date,
                "board": board_name,
                "board_fwd_ret": row.board_fwd_ret,
                "benchmark_fwd_ret": row.benchmark_fwd_ret,
                "excess_ret": row.excess_ret,
                "outcome": outcome,
                "notes": notes,
            }
        )
    db.commit()
    return out


def refresh_t1_outcomes(db: Session, *, user_id: Optional[str] = None, as_of_date: Optional[str] = None, limit: int = 20) -> dict:
    """对指定用户（或全部）已完成主线报告补算 T+1 兑现（无前瞻数据的跳过）。

    user_id=None 表示评估所有用户（定时任务用）；API 调用必须传当前用户。
    """
    q = db.query(MainlineReportDB).filter(MainlineReportDB.status == "completed")
    if user_id:
        q = q.filter(MainlineReportDB.user_id == user_id)
    runs = q.order_by(MainlineReportDB.created_at.desc()).limit(min(max(limit, 1), 100)).all()
    evaluated = 0
    skipped: list[dict] = []
    for r in runs:
        if not r.trade_date:
            skipped.append({"id": r.id, "reason": "no trade_date"})
            continue
        if as_of_date and r.trade_date >= as_of_date:
            skipped.append({"id": r.id, "reason": "no forward data yet"})
            continue
        try:
            outs = evaluate_t1_for_report(db, _to_dict(r), as_of_date=as_of_date)
            if outs:
                evaluated += 1
        except Exception as exc:
            skipped.append({"id": r.id, "reason": f"{type(exc).__name__}: {str(exc)[:80]}"})
    return {"evaluated_reports": evaluated, "skipped": skipped}


def t1_overview(db: Session, *, user_id: Optional[str] = None, days: int = 30) -> dict:
    """T+1 兑现统计：总数、按 outcome 分布、平均超额、胜率（按用户隔离）。"""
    since = (datetime.now(_UTC) - timedelta(days=max(days, 1))).isoformat()
    q = db.query(MainlineT1OutcomeDB).filter(MainlineT1OutcomeDB.created_at >= since)
    if user_id:
        q = q.filter(MainlineT1OutcomeDB.user_id == user_id)
    rows = q.all()
    by_outcome: dict[str, int] = {}
    excesses: list[float] = []
    for r in rows:
        by_outcome[r.outcome or "未知"] = by_outcome.get(r.outcome or "未知", 0) + 1
        if r.excess_ret is not None:
            excesses.append(float(r.excess_ret))
    return {
        "total": len(rows),
        "by_outcome": by_outcome,
        "avg_excess_ret": round(sum(excesses) / len(excesses), 6) if excesses else None,
        "hit_rate": round(sum(1 for e in excesses if e > 0) / len(excesses), 4) if excesses else None,
        "days": days,
    }


def list_t1_outcomes(db: Session, *, user_id: Optional[str] = None, report_id: Optional[str] = None, limit: int = 50) -> list[dict]:
    """T+1 兑现明细列表（可按报告/用户过滤，按评估时间倒序）。"""
    q = db.query(MainlineT1OutcomeDB)
    if user_id:
        q = q.filter(MainlineT1OutcomeDB.user_id == user_id)
    if report_id:
        q = q.filter(MainlineT1OutcomeDB.report_id == report_id)
    rows = q.order_by(MainlineT1OutcomeDB.created_at.desc()).limit(min(max(limit, 1), 200)).all()
    return [_to_dict(r) for r in rows]
