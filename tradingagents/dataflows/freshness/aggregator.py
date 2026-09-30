from __future__ import annotations

from typing import Any

from .anchor import resolve_expected_anchor
from .contract import load_contract, load_section_map
from .types import CONFIDENCE_CAPS, OVERALL_LABELS
from .validator import build_eval_context, evaluate_collector_results

_STATUS_RANK = {
    "error": 4,
    "stale": 3,
    "warning": 2,
    "fresh": 1,
    "empty_ok": 0,
    "not_applicable": 0,
    "unknown": 0,
}


def build_freshness_pool(
    results: dict[str, Any],
    trade_date: str,
    *,
    symbol: str = "600519.SH",
    analysis_mode: str | None = None,
) -> dict[str, dict[str, Any]]:
    return evaluate_collector_results(
        results, trade_date, symbol=symbol, analysis_mode=analysis_mode
    )


def _worst_status(statuses: list[str]) -> str:
    if not statuses:
        return "fresh"
    return max(statuses, key=lambda s: _STATUS_RANK.get(s, 0))


def _aggregate_overall(pool: dict[str, dict[str, Any]]) -> str:
    contract = load_contract()
    has_critical_error = False
    has_important_error = False
    has_critical_stale = False
    has_important_stale = False
    has_warning = False
    has_unknown = False
    has_fresh = False

    for source_key, meta in pool.items():
        entry = contract.get(source_key) or {}
        crit = str(entry.get("criticality") or meta.get("criticality") or "informational")
        status = str(meta.get("status") or "unknown")
        if status == "unknown":
            has_unknown = True
        elif status == "fresh":
            has_fresh = True
        if status == "error":
            if crit == "critical":
                has_critical_error = True
            elif crit == "important":
                has_important_error = True
            elif crit == "informational":
                pass  # 明确标注为可忽略的源，其故障不进汇总量（保持原语义）
            else:
                # 关键度未知（契约里查不到这个源键，见 tools/freshness.py 的兜底）。
                # 我们**不知道**它重不重要 ⇒ 不能让故障消失。按"至少 important"处理：
                # 旧实现把它当 informational 丢掉，于是任一未登记工具的故障都能得到
                # overall_status='fresh'、confidence_cap=100（审计 §4.18）。
                has_important_error = True
        elif status == "stale":
            if crit == "critical":
                has_critical_stale = True
            elif crit == "important":
                has_important_stale = True
            elif crit not in ("informational",):
                has_important_stale = True
        elif status == "warning" and crit != "informational":
            has_warning = True

    if has_critical_error or has_important_error:
        return "error"
    if has_critical_stale:
        return "stale"
    if has_important_stale or has_warning:
        return "warning"
    # 有源处于 unknown（**连新鲜度都判定不了**），且没有任何源被验证为 fresh
    # → 整个池子是"不知道"，不是"最新"。
    #
    # 旧实现在这里 `return "fresh"`：于是「信息缺失」拿到了最乐观的结论。
    # 之所以要看 `has_fresh`：只要有一个源确证新鲜，就说明校验链本身在正常工作，
    # 其余 unknown 是局部现象，交给 `confidence_cap`/`lag_note` 表达即可。
    if has_unknown and not has_fresh:
        return "unknown"
    return "fresh"


def _blocking_sources(pool: dict[str, dict[str, Any]], overall: str) -> list[dict[str, Any]]:
    if overall == "fresh":
        return []
    out: list[dict[str, Any]] = []
    contract = load_contract()
    for source_key, meta in pool.items():
        status = str(meta.get("status") or "")
        if status not in ("error", "stale", "warning"):
            continue
        entry = contract.get(source_key) or {}
        crit = str(entry.get("criticality") or meta.get("criticality") or "informational")
        if status == "warning":
            continue
        if status in ("stale", "error") and crit not in ("critical", "important"):
            continue
        item = {"source_key": source_key, **meta}
        out.append(item)
    return out


def _fetch_errors(blocking: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "source_key": b["source_key"],
            "error_code": b.get("error_code"),
            "error_message": b.get("error_message") or b.get("lag_note"),
        }
        for b in blocking
        if b.get("status") == "error" or b.get("issue_type") == "fetch_error"
    ]


def _section_impacts(pool: dict[str, dict[str, Any]]) -> dict[str, str]:
    section_map = load_section_map()
    contract = load_contract()
    impacts: dict[str, str] = {}

    all_ci_statuses: list[str] = []
    for sk, meta in pool.items():
        entry = contract.get(sk) or {}
        crit = str(entry.get("criticality") or meta.get("criticality") or "")
        if crit in ("critical", "important"):
            all_ci_statuses.append(str(meta.get("status") or "fresh"))

    for section_key, cfg in section_map.items():
        if not isinstance(cfg, dict):
            continue
        if cfg.get("inherit") == "all_critical_important":
            impacts[section_key] = _worst_status(all_ci_statuses)
            continue
        sources = cfg.get("sources") or []
        statuses = [str((pool.get(s) or {}).get("status") or "fresh") for s in sources]
        impacts[section_key] = _worst_status(statuses)
    return impacts


def _warning_sources(pool: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    contract = load_contract()
    out: list[dict[str, Any]] = []
    for source_key, meta in pool.items():
        if str(meta.get("status") or "") != "warning":
            continue
        entry = contract.get(source_key) or {}
        crit = str(entry.get("criticality") or meta.get("criticality") or "informational")
        if crit not in ("critical", "important"):
            continue
        out.append({"source_key": source_key, **meta})
    return out


def build_freshness_summary(
    pool: dict[str, dict[str, Any]],
    trade_date: str,
    *,
    symbol: str = "600519.SH",
    analysis_mode: str | None = None,
) -> dict[str, Any]:
    eval_ctx = build_eval_context(trade_date, symbol=symbol, analysis_mode=analysis_mode)
    overall = _aggregate_overall(pool)
    blocking = _blocking_sources(pool, overall)
    return {
        "overall_status": overall,
        "overall_label": OVERALL_LABELS.get(overall, overall),
        "evaluated_at": eval_ctx.now_iso,
        "analysis_trade_date": trade_date,
        "analysis_mode": eval_ctx.analysis_mode,
        "expected_anchor": eval_ctx.expected_anchor,
        "blocking_sources": blocking,
        "fetch_errors": _fetch_errors(blocking),
        "warning_sources": _warning_sources(pool),
        "datasets": [
            {
                "source_key": k,
                "status": v.get("status"),
                "criticality": (load_contract().get(k) or {}).get("criticality"),
                "anchor_actual": v.get("anchor_actual"),
                "lag_note": v.get("lag_note"),
                "error_message": v.get("error_message"),
                "error_code": v.get("error_code"),
            }
            for k, v in sorted(pool.items())
        ],
        "section_impacts": _section_impacts(pool),
        "confidence_cap": CONFIDENCE_CAPS.get(overall, 100),
        # 报告年龄必须**如实**计算，不能写死 0。
        #
        # 旧实现这里恒为 `0` / `None`，于是 30 天前的报告在**直读 DB** 的统计、
        # T+1 评估、导出里一律读作「0 天前、无告警」；只有 `GET /v1/reports/{id}`
        # 走 `enrich_report_freshness_display` 在**内存里**补正，才显示 21 天。
        # 落库值才是所有离线消费者的输入，必须自己就是对的。
        #
        # 语义 = 分析基准日（trade_date）距**现在**多少天；刚跑完的分析为 0，
        # 历史日期/回补的分析会得到真实年龄。
        "report_age_days": _report_age_days(eval_ctx.now_iso, trade_date),
        "report_age_note": None,
    }


def _report_age_days(now_iso: str, trade_date: str) -> int | None:
    """`trade_date` 距 `now_iso` 的自然日数；无法解析时返回 None。

    返回 None（而不是 0）是刻意的：`0` 的意思是"就是今天"，而解析失败的真实
    含义是"不知道"。用 0 顶替会让"不知道"伪装成"最新"——本审计反复出现的形状。
    """
    from datetime import date as _date
    from datetime import datetime as _dt

    try:
        now_date = _dt.fromisoformat(str(now_iso).replace("Z", "+00:00")).date()
        trade = _dt.strptime(str(trade_date)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    if not isinstance(now_date, _date) or not isinstance(trade, _date):
        return None
    return (now_date - trade).days


def clamp_confidence(confidence: int | None, overall_status: str) -> int | None:
    if confidence is None:
        return None
    cap = CONFIDENCE_CAPS.get(overall_status, 100)
    return min(int(confidence), cap)
