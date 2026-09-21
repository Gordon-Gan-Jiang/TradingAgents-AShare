"""主线周期服务（C2）：基于历史主线报告的周期档案构建、决策卡生成、查询。

流程（每日定时调用 run_cycle_analysis）：
  读取当天主线报告 → 对每条主线做身份匹配（跨天识别同一主线）→ 组装当日快照 →
  追加档案轨迹 → 计算周期特征 → 判定周期位置 → 退潮预警 → 动作/仓位建议 → 验证条件
  → 更新 mainline_history 档案 + 写 mainline_decisions 决策卡（能力曲线数据源）

纯规则层（mainline_cycle 引擎），LLM 复核由作战日志/前端展示承担。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from api.database import (
    MainlineDecisionDB,
    MainlineHistoryDB,
    MainlineReportDB,
    MainlineT1OutcomeDB,
    get_db_ctx,
)

from tradingagents.dataflows.mainline_cycle import (
    ENDED,
    append_track,
    build_verify_conditions,
    compute_cycle_features,
    judge_cycle_position,
    last_active_days,
    match_by_boards,
    match_mainline_key,
    retreat_alerts,
    suggest_action,
)

_UTC = timezone.utc
ENDED_AFTER_DAYS = 3  # 连续 N 个自然日未出现在主线结果 → 已终结
DECISION_EVAL_MIN_GAP_DAYS = 1  # 决策日之后至少 N 个自然日才具备 T+1 前瞻数据

# 决策动作分组（P2-2 验证语义）：看多动作有仓位；降敞口动作不参与/退出。
_BULLISH_ACTIONS = ("布局", "持有")
_DEFENSIVE_ACTIONS = ("减仓", "规避", "观察")


def _now() -> str:
    return datetime.now(_UTC).isoformat(timespec="seconds")


def _to_dict(row) -> dict:
    d = {c.name: getattr(row, c.name) for c in row.__table__.columns}
    for k in ("created_at", "updated_at"):
        if isinstance(d.get(k), datetime):
            d[k] = d[k].isoformat(timespec="seconds")
    return d


# ── 快照组装 ────────────────────────────────────────────────────


def _build_snapshot(mainline: dict, rule_candidates: list[dict], trade_date: str) -> dict:
    """从主线报告组装当日快照：代表板块匹配规则层特征，缺失留 None（诚实降级）。"""
    boards = mainline.get("representative_boards") or []
    board_names = [b.get("board") if isinstance(b, dict) else str(b) for b in boards]
    cand = None
    for name in board_names:
        cand = next((c for c in rule_candidates if c.get("name") == name), None)
        if cand:
            break
    snap: dict[str, Any] = {"date": trade_date}
    snap["name"] = mainline.get("name")
    if cand:
        for f in ("strength", "heat", "rs20", "net_inflow_5d", "inflow_persistent_10",
                  "rsi14", "chg_1d", "phase_hint", "passes_gate"):
            snap[f] = cand.get(f)
    else:
        # 无规则层特征时，用 LLM 置信度作强度近似（标注）
        conf = mainline.get("confidence")
        if isinstance(conf, (int, float)):
            snap["strength"] = float(conf)
        snap["phase_hint"] = mainline.get("phase")
    return snap


def _known_archives(db: Session, user_id: str) -> list[dict]:
    rows = db.query(MainlineHistoryDB).filter(MainlineHistoryDB.user_id == user_id).all()
    return [
        {"key": r.mainline_key, "boards": r.representative_boards or []}
        for r in rows
    ]


def _get_archive(db: Session, user_id: str, key: str) -> Optional[MainlineHistoryDB]:
    return (
        db.query(MainlineHistoryDB)
        .filter(MainlineHistoryDB.user_id == user_id, MainlineHistoryDB.mainline_key == key)
        .first()
    )


# ── 周期分析主流程 ──────────────────────────────────────────────


def run_cycle_analysis(
    db: Session,
    trade_date: str,
    *,
    user_id: str = "system",
    report_id: Optional[str] = None,
) -> dict:
    """对指定（或最近一条）主线报告执行周期分析，更新档案与决策卡。"""
    if report_id is None:
        row = (
            db.query(MainlineReportDB)
            .filter(MainlineReportDB.user_id == user_id, MainlineReportDB.trade_date == trade_date)
            .order_by(MainlineReportDB.created_at.desc())
            .first()
        )
        if row is None:
            return {"updated": 0, "ended": 0, "message": f"无 {trade_date} 的主线报告"}
        report_id = row.id
        report_row = row
    else:
        report_row = (
            db.query(MainlineReportDB).filter(MainlineReportDB.id == report_id).first()
        )
        if report_row is None:
            return {"updated": 0, "ended": 0, "message": f"报告不存在 {report_id}"}
        trade_date = report_row.trade_date
        user_id = report_row.user_id or user_id

    mainlines = report_row.mainlines or []
    snapshot = report_row.market_snapshot or {}
    rule_candidates = snapshot.get("rule_candidates") or []
    emotion = snapshot.get("emotion") or {}
    temperature = emotion.get("temperature")

    known = _known_archives(db, user_id)
    updated = 0
    new_keys: list[str] = []

    for m in mainlines:
        name = str(m.get("name") or "").strip()
        if not name:
            continue
        boards = [
            (b.get("board") if isinstance(b, dict) else str(b))
            for b in (m.get("representative_boards") or [])
        ]
        # 身份匹配：名称 → 代表板块交集
        key = match_mainline_key(name, boards, [k["key"] for k in known])
        if key is None:
            key = match_by_boards(boards, known)
        if key is None:
            key = name
            known.append({"key": key, "boards": boards})

        archive = _get_archive(db, user_id, key)
        if archive is None:
            archive = MainlineHistoryDB(
                id=uuid.uuid4().hex,
                user_id=user_id,
                mainline_key=key,
                representative_boards=boards,
                first_date=trade_date,
                created_at=datetime.now(_UTC),
            )
            db.add(archive)
            new_keys.append(key)
        else:
            # 更新代表板块并集
            merged = list(dict.fromkeys((archive.representative_boards or []) + boards))
            archive.representative_boards = merged

        snap = _build_snapshot(m, rule_candidates, trade_date)
        track = append_track(archive.daily_track or [], snap)
        archive.daily_track = track
        archive.last_date = trade_date

        features = compute_cycle_features(track)
        stage, reason = judge_cycle_position(track, features)
        alerts = retreat_alerts(features)
        action = suggest_action(stage, emotion_temperature=temperature)
        verify = build_verify_conditions(stage, features)

        archive.cycle_position = stage
        archive.cycle_reason = reason
        archive.progress = features.get("progress")
        archive.peak_strength = features.get("strength_peak")
        archive.peak_gap = features.get("peak_gap")
        archive.alerts = alerts
        archive.action = action.get("action")
        archive.position_pct = action.get("position")
        archive.status = "active"
        archive.updated_at = datetime.now(_UTC)

        db.add(
            MainlineDecisionDB(
                id=uuid.uuid4().hex,
                user_id=user_id,
                report_id=report_id,
                mainline_key=key,
                trade_date=trade_date,
                stage=stage,
                action=action.get("action"),
                position_pct=action.get("position"),
                reason=f"{reason}；{action.get('note') or ''}",
                verify_conditions=verify,
                emotion_temperature=temperature,
                created_at=datetime.now(_UTC),
            )
        )
        updated += 1

    # 已终结：active 档案但连续 ENDED_AFTER_DAYS 天未出现
    ended = 0
    active_rows = (
        db.query(MainlineHistoryDB)
        .filter(MainlineHistoryDB.user_id == user_id, MainlineHistoryDB.status == "active")
        .all()
    )
    for ar in active_rows:
        if ar.mainline_key in new_keys:
            continue
        gap = last_active_days(ar.daily_track or [], trade_date)
        if gap is not None and gap >= ENDED_AFTER_DAYS:
            ar.status = "ended"
            ar.cycle_position = ENDED
            ar.updated_at = datetime.now(_UTC)
            ended += 1

    db.commit()
    return {"updated": updated, "ended": ended, "new_keys": new_keys, "report_id": report_id}


# ── 查询 ────────────────────────────────────────────────────────


def list_cycles(db: Session, *, user_id: str = "system", status: Optional[str] = None, limit: int = 50) -> list[dict]:
    q = db.query(MainlineHistoryDB).filter(MainlineHistoryDB.user_id == user_id)
    if status:
        q = q.filter(MainlineHistoryDB.status == status)
    rows = q.order_by(MainlineHistoryDB.updated_at.desc()).limit(min(max(limit, 1), 200)).all()
    return [_to_dict(r) for r in rows]


def get_cycle(db: Session, *, user_id: str, mainline_key: str) -> Optional[dict]:
    row = _get_archive(db, user_id, mainline_key)
    return _to_dict(row) if row else None


def list_decisions(db: Session, *, user_id: str = "system", days: int = 30, limit: int = 100) -> list[dict]:
    since = (datetime.now(_UTC) - timedelta(days=max(days, 1))).isoformat()
    rows = (
        db.query(MainlineDecisionDB)
        .filter(MainlineDecisionDB.user_id == user_id, MainlineDecisionDB.created_at >= since)
        .order_by(MainlineDecisionDB.created_at.desc())
        .limit(min(max(limit, 1), 300))
        .all()
    )
    return [_to_dict(r) for r in rows]


def rotation_timeline(db: Session, *, user_id: str = "system", days: int = 30) -> list[dict]:
    """近 N 日主线轮动回顾：每天各主线的周期位置与动作。"""
    decisions = list_decisions(db, user_id=user_id, days=days, limit=300)
    by_date: dict[str, list[dict]] = {}
    for d in decisions:
        by_date.setdefault(d["trade_date"], []).append(d)
    out = []
    for date in sorted(by_date.keys(), reverse=True):
        out.append(
            {
                "date": date,
                "mainlines": [
                    {"key": d["mainline_key"], "stage": d.get("stage"), "action": d.get("action")}
                    for d in by_date[date]
                ],
            }
        )
    return out


def capability_curve(db: Session, *, user_id: str = "system", days: int = 90) -> dict:
    """能力曲线：历史决策的验证统计（布局/规避正确率、按周期位置分桶）。"""
    decisions = list_decisions(db, user_id=user_id, days=days, limit=300)
    verified = [d for d in decisions if d.get("outcome") in ("verified", "falsified")]
    by_stage: dict[str, dict] = {}
    total_verified = total_falsified = 0
    for d in verified:
        stage = d.get("stage") or "未知"
        bucket = by_stage.setdefault(stage, {"verified": 0, "falsified": 0, "total": 0})
        bucket["total"] += 1
        if d.get("outcome") == "verified":
            bucket["verified"] += 1
            total_verified += 1
        else:
            bucket["falsified"] += 1
            total_falsified += 1
    n = total_verified + total_falsified
    return {
        "evaluated": n,
        "hit_rate": round(total_verified / n, 4) if n else None,
        "verified": total_verified,
        "falsified": total_falsified,
        "by_stage": {
            k: {**v, "hit_rate": round(v["verified"] / v["total"], 4) if v["total"] else None}
            for k, v in by_stage.items()
        },
        "days": days,
    }


def _score_decision(
    action: Optional[str],
    outcome_text: Optional[str],
    board_fwd_ret: Optional[float],
    excess_ret: Optional[float],
) -> Optional[bool]:
    """按动作语义判定决策是否正确；数据不足以判断时返回 None（不评分）。

    验证口径（保守，避免虚高命中率）：
      - "主线真正兑现" = 板块绝对收益为正 **且** 跑赢基准；仅有其中一项数据时退化为
        该项为正（无基准 → 看绝对收益；无板块收益 → 看超额），两项都缺失则不评分。
        对应 T+1 标签 兑现；走平(跑输基准)/弱兑现(抗跌) 都不算真正兑现。
      - 看多动作（布局/持有，有仓位）：只有真正兑现才算正确；踏空、跑输基准即证伪。
      - 降敞口动作（减仓/规避/观察）：只有主线未真正兑现才算正确；若主线继续兑现，
        说明减仓减早了/规避踏空了 → 证伪。
        （修复点：原实现把"减仓"在兑现与证伪两侧都算正确 → 减仓永远正确、命中率被稀释。）
      - 标签为"数据不足"或收益数据全部缺失 → 不评分，outcome 保持 NULL 等待后续数据。
    """
    if not outcome_text or outcome_text == "数据不足":
        return None
    if board_fwd_ret is None and excess_ret is None:
        return None
    if board_fwd_ret is None:
        validated = excess_ret > 0
    elif excess_ret is None:
        validated = board_fwd_ret > 0
    else:
        validated = board_fwd_ret > 0 and excess_ret > 0
    act = (action or "").strip()
    if act in _BULLISH_ACTIONS:
        return validated
    if act in _DEFENSIVE_ACTIONS:
        return not validated
    return None  # 未知动作不参与评分


def _outcome_note(row: MainlineT1OutcomeDB) -> str:
    """决策卡的验证说明（中文，含板块收益/超额与评估截止日）。"""
    parts = [f"T+1={row.outcome or '未知'}"]
    if row.board_fwd_ret is not None:
        parts.append(f"板块{row.board_fwd_ret * 100:.1f}%")
    if row.excess_ret is not None:
        parts.append(f"超额{row.excess_ret * 100:.1f}%")
    if row.check_date:
        parts.append(f"截至{row.check_date}")
    return " ".join(parts)


def _resolve_decision_mainline_names(
    db: Session,
    decision: MainlineDecisionDB,
    report_cache: dict[str, Any],
    archive_cache: dict[tuple, list],
) -> list[str]:
    """决策卡 → 该报告内对应的主线名（T+1 兑现表以"报告 + 当日原始主线名"为 key）。

    决策卡的 mainline_key 是跨天归一化的 key（通常是主线首次出现时的名称），当日报告
    可能已把同一条主线改名（如"AI算力"→"算力租赁"）。这里复用引擎自身的身份匹配规则
    （名称完全一致 → 名称包含 → 代表板块交集）在同一份报告内解析原始名；
    解析范围严格限制在该报告内，不会跨报告、跨用户。
    """
    key = (decision.mainline_key or "").strip()
    if not key:
        return []
    names = [key]  # 优先按 key 本身匹配（绝大多数情况 key == 当日主线名）
    report = None
    if decision.report_id:
        if decision.report_id not in report_cache:
            report_cache[decision.report_id] = (
                db.query(MainlineReportDB).filter(MainlineReportDB.id == decision.report_id).first()
            )
        report = report_cache[decision.report_id]
    if report is None:
        return names

    cache_key = (decision.user_id, key)
    if cache_key not in archive_cache:
        archive = _get_archive(db, decision.user_id, key) if decision.user_id else None
        archive_cache[cache_key] = list((archive.representative_boards or []) if archive else [])
    known = [{"key": key, "boards": archive_cache[cache_key]}]

    for m in report.mainlines or []:
        name = str(m.get("name") or "").strip()
        if not name or name in names:
            continue
        boards = [
            (b.get("board") if isinstance(b, dict) else str(b))
            for b in (m.get("representative_boards") or [])
        ]
        if match_mainline_key(name, boards, [key]) == key:
            names.append(name)
            continue
        if match_by_boards(boards, known) == key:
            names.append(name)
    return names


def _link_outcomes(db: Session, decisions: list[MainlineDecisionDB]) -> dict:
    """把 T+1 兑现记录关联到决策卡并判定结果（匹配口径见 verify_decisions）。"""
    stats = {"updated": 0, "verified": 0, "falsified": 0, "no_data": 0, "unscored": 0}
    if not decisions:
        return stats

    users = sorted({d.user_id for d in decisions if d.user_id})
    rows = (
        db.query(MainlineT1OutcomeDB).filter(MainlineT1OutcomeDB.user_id.in_(users)).all()
        if users
        else []
    )
    # (用户, 报告) → {主线名: 兑现行}；以及兼容历史行的 (用户, 交易日) 索引
    by_report: dict[tuple, dict[str, MainlineT1OutcomeDB]] = {}
    by_date: dict[tuple, dict[str, MainlineT1OutcomeDB]] = {}
    for r in rows:
        by_report.setdefault((r.user_id, r.report_id), {}).setdefault(r.mainline or "", r)
        by_date.setdefault((r.user_id, r.trade_date), {}).setdefault(r.mainline or "", r)

    report_cache: dict[str, Any] = {}
    archive_cache: dict[tuple, list] = {}

    for d in decisions:
        if not d.user_id:
            stats["no_data"] += 1
            continue
        if d.report_id:
            # 稳定关联：只认同一份报告的兑现记录（outcome 表以 report_id + 主线名唯一）
            index = by_report.get((d.user_id, d.report_id))
        else:
            # 兼容历史行（report_id 为空）：退化为"同用户 + 同交易日"，仍不跨用户
            index = by_date.get((d.user_id, d.trade_date))
        if not index:
            stats["no_data"] += 1
            continue

        row = None
        for name in _resolve_decision_mainline_names(db, d, report_cache, archive_cache):
            row = index.get(name)
            if row is not None:
                break
        if row is None:
            stats["no_data"] += 1
            continue
        if d.trade_date and row.trade_date and d.trade_date != row.trade_date:
            stats["no_data"] += 1
            continue

        verdict = _score_decision(d.action, row.outcome, row.board_fwd_ret, row.excess_ret)
        if verdict is None:
            stats["unscored"] += 1
            continue
        d.outcome = "verified" if verdict else "falsified"
        d.outcome_note = _outcome_note(row)
        d.forward_excess_ret = row.excess_ret
        d.updated_at = datetime.now(_UTC)
        stats["updated"] += 1
        stats["verified" if verdict else "falsified"] += 1

    db.commit()
    return stats


def verify_decisions(
    db: Session,
    *,
    user_id: Optional[str] = "system",
    as_of_date: Optional[str] = None,
    limit: int = 50,
) -> dict:
    """对未验证决策补算事后结果（能力曲线数据）：关联主线 T+1 兑现记录。

    匹配口径（P2-2 修复）：不再把整张 mainline_t1_outcomes 按主线名做全局字典匹配
    （同名主线会跨报告/跨用户串味），而是按「用户 + 报告 report_id + 报告内主线身份
    （名称/代表板块）」定位兑现记录；report_id 为空的历史行退化为「同用户 + 同交易日」。
    判定语义见 _score_decision。
    """
    q = db.query(MainlineDecisionDB).filter(MainlineDecisionDB.outcome.is_(None))
    if user_id is not None:
        q = q.filter(MainlineDecisionDB.user_id == user_id)
    if as_of_date:
        # 决策日当天及之后没有前瞻数据，先不评分（评估窗口未关闭）
        q = q.filter(MainlineDecisionDB.trade_date < as_of_date)
    pending = (
        q.order_by(MainlineDecisionDB.created_at.desc())
        .limit(min(max(limit, 1), 200))
        .all()
    )

    stats = _link_outcomes(db, pending)
    return {"updated": stats["updated"], "pending_total": len(pending), **stats}


def backfill_decision_outcomes(
    db: Session,
    *,
    user_id: Optional[str] = None,
    as_of_date: Optional[str] = None,
    min_gap_days: int = DECISION_EVAL_MIN_GAP_DAYS,
    refresh_t1: bool = True,
    max_refresh_reports: int = 20,
    limit: int = 200,
) -> dict:
    """主线验证闭环回填任务（P2-2）：为评估窗口已关闭的决策补算 outcome / forward_excess_ret。

    流程：
      1. 取 outcome 为空的决策，按「决策日 + min_gap_days 自然日 ≤ as_of」判定窗口已关闭；
      2. 对窗口已关闭、且尚无有效兑现记录的报告，调用 T+1 兑现评估补建
         mainline_t1_outcomes（依赖板块历史数据，网络不可用时诚实跳过，不造假）；
      3. 按「用户 + 报告 + 主线身份」关联兑现记录，按动作语义判定 verified/falsified，
         写入 outcome / outcome_note / forward_excess_ret。

    user_id=None 表示回填所有用户（定时任务用）；API 调用必须传当前用户。
    """
    as_of = (as_of_date or datetime.now(_UTC).strftime("%Y-%m-%d")).strip()
    try:
        cutoff = (
            datetime.strptime(as_of, "%Y-%m-%d") - timedelta(days=max(int(min_gap_days), 0))
        ).strftime("%Y-%m-%d")
    except Exception:
        cutoff = as_of

    pending_q = db.query(MainlineDecisionDB).filter(MainlineDecisionDB.outcome.is_(None))
    if user_id is not None:
        pending_q = pending_q.filter(MainlineDecisionDB.user_id == user_id)
    pending_total = pending_q.count()

    cap = min(max(limit, 1), 500)
    closed = (
        pending_q.filter(
            MainlineDecisionDB.trade_date.isnot(None),
            MainlineDecisionDB.trade_date <= cutoff,
        )
        .order_by(MainlineDecisionDB.trade_date.desc())
        .limit(cap)
        .all()
    )

    refresh_stats: dict[str, Any] = {
        "refreshed_reports": 0,
        "refresh_no_data": 0,
        "refresh_failed": [],
        "refresh_skipped": 0,
    }
    if refresh_t1 and closed:
        refresh_stats = _refresh_t1_for_decisions(db, closed, as_of, max_refresh_reports)

    stats = _link_outcomes(db, closed)
    return {
        "as_of": as_of,
        "cutoff": cutoff,
        "closed": len(closed),
        "pending_total": pending_total,
        **refresh_stats,
        **stats,
    }


def _refresh_t1_for_decisions(
    db: Session,
    decisions: list[MainlineDecisionDB],
    as_of: str,
    max_reports: int,
) -> dict:
    """为窗口已关闭的决策所属报告补建 T+1 兑现记录（网络不可用时诚实跳过）。"""
    report_ids = list(dict.fromkeys([d.report_id for d in decisions if d.report_id]))
    stats: dict[str, Any] = {
        "refreshed_reports": 0,
        "refresh_no_data": 0,
        "refresh_failed": [],
        "refresh_skipped": 0,
    }
    if not report_ids:
        return stats

    # 已有有效兑现记录（非"数据不足"）的报告不必重复拉取网络数据
    settled = {
        r.report_id
        for r in db.query(MainlineT1OutcomeDB)
        .filter(MainlineT1OutcomeDB.report_id.in_(report_ids))
        .all()
        if (r.outcome or "") not in ("", "数据不足")
    }
    todo = [rid for rid in report_ids if rid not in settled][: max(int(max_reports), 0)]

    from api.services import mainline_service  # 延迟导入：避免模块级循环依赖

    failed: list[dict] = []
    for rid in todo:
        row = db.query(MainlineReportDB).filter(MainlineReportDB.id == rid).first()
        if row is None:
            failed.append({"report_id": rid, "reason": "报告不存在"})
            continue
        try:
            outs = mainline_service.evaluate_t1_for_report(db, _to_dict(row), as_of_date=as_of)
            if outs:
                stats["refreshed_reports"] += 1
            else:
                # 报告日之后暂无前瞻数据（如基准/板块历史不可用）→ 保持 pending 等下次重试
                stats["refresh_no_data"] += 1
        except Exception as exc:
            # 板块历史不可用（受限网络等）→ 保留 pending，等后续重试
            failed.append({"report_id": rid, "reason": f"{type(exc).__name__}: {str(exc)[:80]}"})
    stats["refresh_failed"] = failed
    stats["refresh_skipped"] = len(report_ids) - len(todo)
    return stats
