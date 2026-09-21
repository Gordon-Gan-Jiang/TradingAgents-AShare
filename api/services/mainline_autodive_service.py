"""主线自动深挖服务（C3）：从主线候选股中筛"可买入"，自动创建个股深度分析任务。

设计（docs/mainline-cycle-design.md v2 第四节）：
- 可买入 = 风险收益比框架：主线周期闸门 + 个股位置/资金闸门 + 层级档位
- 任务创建采用**依赖注入**（create_task 可调用对象），服务层不依赖 api/main，
  由定时流水线/API 端点注入真实的任务创建器，保证可单测、可复用。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

from api.database import (
    MainlineReportDB,
    MainlineTradeCandidateDB,
    get_db_ctx,
)
from api.services import mainline_cycle_service

_UTC = timezone.utc

# 可买入硬闸门
MAX_DAILY_DEEP_DIVE = 5     # 每日自动深挖上限（防刷爆任务队列）
MAX_LIANBAN = 3             # 连板 ≥4 视为高位接力，不可买入
TURNOVER_MIN, TURNOVER_MAX = 2.0, 25.0   # 换手区间（活跃非疯狂）
CHASE_1D_PCT = 7.0          # 单日涨幅超过该值视为追涨（原为 9.9，仅认"接近涨停"）
POSITION_TIERS = {          # score → 仓位档位
    "heavy": 85,            # score >= 85 重仓档
    "std": 70,              # score >= 70 标准档
    "light": 55,            # score >= 55 轻仓档
}

# 候选自述里的"别追高"措辞。命中即视为追涨风险。
#
# 为什么必须读文本：线上 `mainline_reports.candidates` 的实际字段是
# symbol/name/mainline/tier/score/reasons/entry_hint/risk —— **没有** lianban /
# turnover / chg_1d / source。也就是说本模块原有的三个数值闸门在真实数据上永远
# 不触发（`candidate.get("lianban") or 0` 恒为 0、turnover 恒为 None、
# source 恒为 None，于是 `source == "cons"` 恒为假）。
#
# 与此同时 LLM 在 entry_hint 里已经明确写了"不建议次日高开追入""建议不追高"
# "禁止追高""避免在当日高点附近接盘"——闸门却把这些全部忽略，照样把候选标成
# 可买入并给出仓位档位。系统自己的建议被自己的闸门丢弃，这是实测的追涨来源。
#
# 只收录**明确的**追涨表述；不收"已涨"这类不带幅度的词，否则"已涨0.3%"也会被拦。
_CHASE_PATTERNS = (
    "禁止追高", "不建议追高", "建议不追高", "不要追高", "不宜追高", "不宜追",
    "不追高", "追高风险", "追涨风险", "避免追", "高点附近接盘",
    "高位分歧", "高位接力", "强势末端", "不建议次日高开", "不宜盲目打板",
    "盲目追", "追入",
)


def _to_dict(row) -> dict:
    d = {c.name: getattr(row, c.name) for c in row.__table__.columns}
    for k in ("created_at", "updated_at"):
        if isinstance(d.get(k), datetime):
            d[k] = d[k].isoformat(timespec="seconds")
    return d


def _position_tier(score: Optional[int]) -> str:
    s = score or 0
    if s >= POSITION_TIERS["heavy"]:
        return "重仓"
    if s >= POSITION_TIERS["std"]:
        return "标准"
    if s >= POSITION_TIERS["light"]:
        return "轻仓"
    return "观察"


_BUYABLE_STAGES = ("发酵", "主升", "发酵(待确认)")  # 待确认=档案数据不足但当日 LLM 判定发酵/主升


def _candidate_text(candidate: dict) -> str:
    """候选自述文本（entry_hint / risk / reasons），用于识别追涨措辞。"""
    parts = [
        str(candidate.get("entry_hint") or ""),
        str(candidate.get("risk") or ""),
    ]
    reasons = candidate.get("reasons")
    if isinstance(reasons, (list, tuple)):
        parts.extend(str(x) for x in reasons)
    elif reasons:
        parts.append(str(reasons))
    return " ".join(parts)


def detect_chase_risk(candidate: dict) -> Optional[str]:
    """判断该候选是否属于"已经涨过头、此刻介入就是追高"。

    实测依据（`docs/short-horizon-alpha-probe.md` Table A，310 只标的、462 个交易日）：

    * ``mom_5`` 对次日的秩相关 **−0.0212**（t=−2.51），5 日动量越高、次日越差；
    * ``dist_ma20`` −0.0294、``range_10`` **−0.0419**（t=−3.53）、``vol_shock``
      **−0.0331**（t=−5.36）——全部为负，即"已经涨多/波动放大"的标的次日跑输。

    组合层面同样：恒满仓 T+1 之后 20 日 **−1.59%**、上涨率 **42.1%**。
    所以"涨过头的标的不追"不是直觉，而是这批数据里方向一致的负 IC。

    两级判定，都只在**有证据**时拦截：

    1. 数值证据：单日涨幅（``chg_1d``）、连板数、换手率——字段存在时才算；
    2. 文本证据：候选自述里的追涨措辞（见 ``_CHASE_PATTERNS``）。

    原实现只认 ``chg_1d >= 9.9`` 且限定 ``source == "cons"``。两个限定都站不住：
    9.9% 相当于只拦"接近涨停"，而实测的负 IC 在 +7% 量级就已经成立；而按来源
    区别对待没有任何依据——追高是不是风险，与候选由哪条链路产出无关。
    """
    chg_1d = candidate.get("chg_1d")
    if isinstance(chg_1d, (int, float)) and chg_1d >= CHASE_1D_PCT:
        return f"当日已涨{chg_1d:.1f}%，追高区间"

    lianban = candidate.get("lianban")
    if isinstance(lianban, (int, float)) and lianban >= MAX_LIANBAN:
        return f"连板{lianban:.0f} 高位接力风险"

    turnover = candidate.get("turnover")
    if isinstance(turnover, (int, float)) and turnover > TURNOVER_MAX:
        return f"换手{turnover:.1f}% 过热"

    text = _candidate_text(candidate)
    if text:
        for pattern in _CHASE_PATTERNS:
            if pattern in text:
                return f"候选自述提示追涨风险（命中「{pattern}」）"
    return None


def filter_buyable(candidate: dict, *, cycle_position: str) -> tuple[bool, str]:
    """单个候选的可买入判定（风险收益比框架）。

    返回 (buyable, reason)。主线周期不在布局窗口一律不可买入。

    闸门顺序：先周期（环境），再个体过热（追涨），再活跃度区间。
    """
    if cycle_position not in _BUYABLE_STAGES:
        return False, f"主线周期[{cycle_position}]非布局窗口"
    lianban = candidate.get("lianban") or 0
    if lianban >= MAX_LIANBAN + 1:
        return False, f"连板{lianban} 高位接力风险"
    turnover = candidate.get("turnover")
    if turnover is not None and (turnover < TURNOVER_MIN or turnover > TURNOVER_MAX):
        return False, f"换手{turnover:.1f}% 不在{ TURNOVER_MIN}-{TURNOVER_MAX}%活跃区间"
    # 追涨闸门：数值字段缺失时退回候选自述文本，避免闸门在真实载荷上形同虚设。
    chase = detect_chase_risk(candidate)
    if chase:
        return False, chase
    return True, "通过可买入闸门"


def _match_mainline_name(cand_mainline: str, mainlines: dict[str, Any]) -> Optional[str]:
    """候选的 mainline 字段可能是报告主线名的截断/缩写 → 包含匹配。"""
    if not cand_mainline:
        return None
    if cand_mainline in mainlines:
        return cand_mainline
    for name in mainlines:
        if cand_mainline in name or name in cand_mainline:
            return name
    return None


def run_autodive(
    db: Session,
    trade_date: str,
    *,
    user_id: str = "system",
    report_id: Optional[str] = None,
    create_task: Optional[Callable[[str], str]] = None,
    max_dive: int = MAX_DAILY_DEEP_DIVE,
) -> dict:
    """对当日主线报告执行可买入筛选 + 自动创建深度分析任务。

    create_task(symbol) -> job_id：注入的任务创建器（api/main 用 _run_job 包装）。
    返回 {"candidates": [...], "created_tasks": [...], "skipped": [...]}
    """
    if report_id is None:
        row = (
            db.query(MainlineReportDB)
            .filter(MainlineReportDB.user_id == user_id, MainlineReportDB.trade_date == trade_date)
            .order_by(MainlineReportDB.created_at.desc())
            .first()
        )
        if row is None:
            return {"candidates": [], "created_tasks": [], "skipped": [], "message": f"无 {trade_date} 主线报告"}
        report_id = row.id
    report = db.query(MainlineReportDB).filter(MainlineReportDB.id == report_id).first()
    if report is None:
        return {"candidates": [], "created_tasks": [], "skipped": [], "message": f"报告不存在 {report_id}"}
    trade_date = report.trade_date

    # 主线周期位置（来自周期档案）
    cycles = {c["mainline_key"]: c for c in mainline_cycle_service.list_cycles(db, user_id=user_id, limit=100)}
    candidates = report.candidates or []
    mainlines = {m.get("name"): m for m in (report.mainlines or [])}

    out_rows: list[MainlineTradeCandidateDB] = []
    created: list[dict] = []
    skipped: list[dict] = []
    dive_count = 0

    # 幂等：已有行按 (report_id, symbol) 复用（更新元数据），避免重复 INSERT
    # 触发 UNIQUE 约束。已进行中/已完成的深挖任务不会被重复创建。
    existing_by_symbol = {
        r.symbol: r
        for r in db.query(MainlineTradeCandidateDB)
        .filter(MainlineTradeCandidateDB.report_id == report_id)
        .all()
    }

    for c in candidates:
        mname = _match_mainline_name(str(c.get("mainline") or ""), mainlines) or str(c.get("mainline") or "")
        cycle = cycles.get(mname) or {}
        stage = cycle.get("cycle_position") or "数据不足"
        # 档案数据不足时，用当日 LLM phase 判定（发酵/主升 → 待确认布局窗口）
        if stage == "数据不足":
            llm_phase = str((mainlines.get(mname) or {}).get("phase") or "")
            if llm_phase in ("发酵", "主升"):
                stage = "发酵(待确认)"
        buyable, reason = filter_buyable(c, cycle_position=stage)
        tier = _position_tier(c.get("score"))
        symbol = str(c.get("symbol") or "")
        row = existing_by_symbol.get(symbol)
        if row is None:
            row = MainlineTradeCandidateDB(
                id=uuid.uuid4().hex,
                user_id=user_id,
                report_id=report_id,
                mainline_key=mname,
                symbol=symbol,
                name=str(c.get("name") or ""),
                tier=c.get("tier"),
                score=c.get("score"),
                buyable=buyable,
                timing=c.get("entry_hint"),
                position_tier=tier if buyable else None,
                deep_dive_status="none",
                created_at=datetime.now(_UTC),
            )
            db.add(row)
        else:
            # 复用已有行：仅刷新候选元数据，保留深挖任务状态（job_id/status）
            row.mainline_key = mname
            row.name = str(c.get("name") or "")
            row.tier = c.get("tier")
            row.score = c.get("score")
            row.buyable = buyable
            row.timing = c.get("entry_hint")
            row.position_tier = tier if buyable else None
            row.updated_at = datetime.now(_UTC)
        out_rows.append(row)
        if not buyable:
            skipped.append({"symbol": row.symbol, "name": row.name, "reason": reason, "stage": stage})
            continue
        # 可买入 → 自动深挖（上限内）
        if dive_count >= max_dive:
            skipped.append({"symbol": row.symbol, "name": row.name, "reason": "当日深挖数量已达上限", "stage": stage})
            continue
        # 已有进行中/已完成的深挖任务 → 不重复创建
        if row.deep_dive_status in ("queued", "running", "completed"):
            skipped.append({"symbol": row.symbol, "name": row.name, "reason": f"已有深挖任务（{row.deep_dive_status}）", "stage": stage})
            continue
        if create_task is not None and row.symbol:
            try:
                job_id = create_task(row.symbol)
                row.deep_dive_job_id = job_id
                row.deep_dive_status = "queued"
                row.deep_dive_error = None  # 清空历史失败信息（重试成功）
                created.append({"symbol": row.symbol, "name": row.name, "job_id": job_id, "position_tier": tier})
                dive_count += 1
            except Exception as exc:
                row.deep_dive_status = "failed"
                row.deep_dive_error = f"{type(exc).__name__}: {str(exc)[:200]}"
                skipped.append({"symbol": row.symbol, "name": row.name, "reason": f"任务创建失败: {row.deep_dive_error}", "stage": stage})
        else:
            # 未注入 create_task（如仅筛选预览）→ 标记可买入但不创建任务
            row.deep_dive_status = "none"
            created.append({"symbol": row.symbol, "name": row.name, "job_id": None, "position_tier": tier})

    db.commit()
    rows = db.query(MainlineTradeCandidateDB).filter(MainlineTradeCandidateDB.report_id == report_id).all()
    return {
        "report_id": report_id,
        "trade_date": trade_date,
        "candidates": [_to_dict(r) for r in rows],
        "created_tasks": created,
        "skipped": skipped,
        "buyable_count": sum(1 for r in rows if r.buyable),
    }


def list_trade_candidates(
    db: Session,
    *,
    user_id: str = "system",
    trade_date: Optional[str] = None,
    buyable_only: bool = False,
    limit: int = 100,
) -> list[dict]:
    q = db.query(MainlineTradeCandidateDB).filter(MainlineTradeCandidateDB.user_id == user_id)
    if trade_date:
        q = q.join(MainlineReportDB, MainlineReportDB.id == MainlineTradeCandidateDB.report_id).filter(
            MainlineReportDB.trade_date == trade_date
        )
    if buyable_only:
        q = q.filter(MainlineTradeCandidateDB.buyable.is_(True))
    rows = q.order_by(MainlineTradeCandidateDB.created_at.desc()).limit(min(max(limit, 1), 300)).all()
    return [_to_dict(r) for r in rows]


def mark_deep_dive_completed(
    db: Session,
    *,
    job_id: str,
    report_id: Optional[str] = None,
    status: str = "completed",
    deep_dive_report_id: Optional[str] = None,
    error: Optional[str] = None,
) -> int:
    """回填自动深挖任务状态（由 API 层在任务完成/失败时调用）。"""
    q = db.query(MainlineTradeCandidateDB).filter(MainlineTradeCandidateDB.deep_dive_job_id == job_id)
    if report_id:
        q = q.filter(MainlineTradeCandidateDB.report_id == report_id)
    rows = q.all()
    for r in rows:
        r.deep_dive_status = status
        if deep_dive_report_id:
            r.deep_dive_report_id = deep_dive_report_id
        if error:
            r.deep_dive_error = error
        r.updated_at = datetime.now(_UTC)
    db.commit()
    return len(rows)


def reconcile_deep_dive_statuses(db: Session, *, user_id: str = "system") -> dict:
    """回填自动深挖任务状态：以个股深度分析报告（report_id == job_id）为准。

    深挖任务走 /v1/analyze 链路，报告 id 即 job_id；查 reports 表状态即可对齐。
    """
    from api.database import ReportDB

    rows = (
        db.query(MainlineTradeCandidateDB)
        .filter(
            MainlineTradeCandidateDB.user_id == user_id,
            MainlineTradeCandidateDB.deep_dive_job_id.isnot(None),
            MainlineTradeCandidateDB.deep_dive_status.in_(("queued", "running")),
        )
        .all()
    )
    updated = 0
    for r in rows:
        report = db.query(ReportDB).filter(ReportDB.id == r.deep_dive_job_id).first()
        if report is None:
            continue
        if report.status == "completed":
            r.deep_dive_status = "completed"
            r.deep_dive_report_id = report.id
            r.updated_at = datetime.now(_UTC)
            updated += 1
        elif report.status == "failed":
            r.deep_dive_status = "failed"
            r.deep_dive_error = report.error or "深度分析任务失败"
            r.updated_at = datetime.now(_UTC)
            updated += 1
    db.commit()
    return {"reconciled": updated, "pending": len(rows) - updated}


def daily_operation_log(db: Session, *, user_id: str = "system", trade_date: str) -> dict:
    """作战日志：当日主线决策 + 可买入 + 深挖状态 + 退潮预警汇总。"""
    from api.database import MainlineReportDB, MainlineT1OutcomeDB

    report = (
        db.query(MainlineReportDB)
        .filter(MainlineReportDB.user_id == user_id, MainlineReportDB.trade_date == trade_date)
        .order_by(MainlineReportDB.created_at.desc())
        .first()
    )
    cycles = mainline_cycle_service.list_cycles(db, user_id=user_id, status="active", limit=100)
    candidates = list_trade_candidates(db, user_id=user_id, trade_date=trade_date)
    decisions = [
        d for d in mainline_cycle_service.list_decisions(db, user_id=user_id, days=30, limit=300)
        if d.get("trade_date") == trade_date
    ]
    retreat = [
        {"key": c["mainline_key"], "alerts": c.get("alerts") or [], "stage": c.get("cycle_position")}
        for c in cycles if c.get("cycle_position") in ("退潮", "高位分歧") and c.get("alerts")
    ]
    yesterday = (
        db.query(MainlineReportDB)
        .filter(MainlineReportDB.user_id == user_id, MainlineReportDB.trade_date < trade_date, MainlineReportDB.status == "completed")
        .order_by(MainlineReportDB.created_at.desc())
        .first()
    )
    yesterday_verify: list[dict] = []
    if yesterday is not None:
        for d in decisions:
            pass  # 昨日决策验证见 verify_decisions/decisions 的 outcome 字段
    return {
        "trade_date": trade_date,
        "report_id": report.id if report else None,
        "report_status": report.status if report else "none",
        "mainlines": [
            {"key": d.get("mainline_key"), "stage": d.get("stage"), "action": d.get("action"),
             "position_pct": d.get("position_pct"), "reason": d.get("reason")}
            for d in decisions
        ],
        "retreat_alerts": retreat,
        "buyable": [
            {"symbol": c.get("symbol"), "name": c.get("name"), "tier": c.get("position_tier"),
             "timing": c.get("timing"), "deep_dive_status": c.get("deep_dive_status"),
             "deep_dive_report_id": c.get("deep_dive_report_id")}
            for c in candidates if c.get("buyable")
        ],
        "deep_dive_pending": sum(1 for c in candidates if c.get("deep_dive_status") in ("queued", "running")),
    }
