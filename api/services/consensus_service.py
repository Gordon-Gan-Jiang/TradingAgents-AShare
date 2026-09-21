"""Deterministic consensus scoring for multi-agent stock analysis.

This service borrows the practical part of the "debate + weighted fusion"
pattern from open-source multi-agent investing projects: let LLMs produce the
arguments, then let a deterministic layer score agreement, disagreement, and
execution readiness so the final UX is stable across reruns.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional

from tradingagents.agents.utils.direction import (
    direction_from_score,
    normalize_direction,
)


_DIRECTION_SCORE = {
    "看多": 1.0,
    "偏多": 0.5,
    "中性": 0.0,
    "偏空": -0.5,
    "看空": -1.0,
}

_AGENT_LABELS = {
    "market_analyst": "市场面",
    "social_media_analyst": "情绪面",
    "news_analyst": "消息面",
    "fundamentals_analyst": "基本面",
    "macro_analyst": "宏观面",
    "smart_money_analyst": "资金面",
    "volume_price_analyst": "量价面",
}

_BASE_WEIGHTS = {
    "market_analyst": 1.0,
    "social_media_analyst": 0.85,
    "news_analyst": 0.9,
    "fundamentals_analyst": 1.15,
    "macro_analyst": 1.0,
    "smart_money_analyst": 1.1,
    "volume_price_analyst": 1.05,
}

_HORIZON_WEIGHT_MULTIPLIER = {
    "short": {
        "market_analyst": 1.15,
        "social_media_analyst": 1.1,
        "news_analyst": 1.0,
        "fundamentals_analyst": 0.85,
        "macro_analyst": 0.9,
        "smart_money_analyst": 1.15,
        "volume_price_analyst": 1.15,
    },
    "medium": {
        "market_analyst": 0.9,
        "social_media_analyst": 0.85,
        "news_analyst": 1.0,
        "fundamentals_analyst": 1.2,
        "macro_analyst": 1.15,
        "smart_money_analyst": 0.95,
        "volume_price_analyst": 0.9,
    },
}


def _normalize_direction(value: Any) -> Optional[str]:
    """Normalize a direction label to a canonical value, or ``None``.

    Delegates to :mod:`tradingagents.agents.utils.direction`. The previous local
    table recognised only ``bullish``/``lean_bullish``/``neutral``/
    ``lean_bearish``/``bearish`` plus the Chinese labels, and then fell back to a
    substring scan in canonical order. Two consequences were shipped:

    * ``BUY`` / ``SELL`` / ``HOLD`` — the vocabulary the TRADE_PLAN block actually
      uses — matched nothing and scored **0.0**, so every plan direction was
      silently discarded from the consensus vote;
    * the substring scan let an inner phrase win, so ``strong_buy`` and
      ``谨慎看多`` resolved by table order rather than by meaning.
    """
    return normalize_direction(value)


def _direction_score(value: Any) -> float:
    direction = _normalize_direction(value)
    if not direction:
        return 0.0
    return _DIRECTION_SCORE[direction]


def _direction_from_score(score: float) -> str:
    """Map a numeric consensus score back to a canonical direction.

    Delegates to :func:`tradingagents.agents.utils.direction.direction_from_score`,
    which is the same threshold ladder; keeping one copy means the consensus score
    and the parser cannot drift apart.
    """
    return direction_from_score(score)


_CONFIDENCE_WORDS = {
    "高": 0.85,
    "中": 0.6,
    "低": 0.35,
    "high": 0.85,
    "medium": 0.6,
    "mid": 0.6,
    "low": 0.35,
}


def _confidence_to_float(value: Any) -> Optional[float]:
    """把分析师自报的 confidence 归一化到 0-1，供**展示与溯源**使用。

    返回 ``None`` 表示分析师没有给出置信度。旧实现在缺省时按"方向有多明确"
    反推一个数值（``0.45 + |score| * 0.35``），等于把"语气坚定"伪装成"模型自报的
    置信度"展示给用户——一个用户以为来自模型、实则由代码编造的数字。

    该值不再参与任何加权计算：实测它与 T+1 正确率的相关系数仅 r=+0.033，
    且最高分档是反向的（[80,101) → 46.7%）。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        if numeric > 1:
            numeric = numeric / 100.0
        return max(0.0, min(numeric, 1.0))

    text = str(value or "").strip().lower()
    if text in _CONFIDENCE_WORDS:
        return _CONFIDENCE_WORDS[text]
    return None


def _agent_weight(agent: str, horizon: str) -> float:
    base = _BASE_WEIGHTS.get(agent, 0.85)
    horizon_weights = _HORIZON_WEIGHT_MULTIPLIER.get(horizon or "short", {})
    return base * horizon_weights.get(agent, 1.0)


def _safe_int(value: float) -> int:
    return int(max(0, min(100, round(value))))


def _format_mode(mode: str) -> str:
    labels = {
        "direct": "直接执行",
        "conditional": "条件执行",
        "observe": "等待验证",
    }
    return labels.get(mode, "等待验证")


def build_consensus_summary(
    *,
    result_data: Optional[Dict[str, Any]],
    final_direction: Optional[str] = None,
    final_confidence: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Build a deterministic consensus summary from analyst traces.

    The output is intentionally JSON-friendly so it can live inside
    ``result_data`` and be rendered by the frontend without an additional DB
    migration.
    """

    if not result_data:
        return None

    traces = list(result_data.get("analyst_traces") or [])
    if not traces:
        return None

    weighted_sum = 0.0
    total_weight = 0.0
    weighted_deviation_sum = 0.0
    horizon_scores: dict[str, dict[str, float]] = defaultdict(lambda: {"sum": 0.0, "weight": 0.0})
    supporting_weight = 0.0
    opposing_weight = 0.0
    neutral_weight = 0.0
    breakdown: List[Dict[str, Any]] = []

    final_direction_norm = _normalize_direction(final_direction)
    final_direction_score = _direction_score(final_direction_norm)

    unparsed_n = 0
    for raw_trace in traces:
        agent = str(raw_trace.get("agent") or "").strip()
        horizon = str(raw_trace.get("horizon") or "short").strip().lower() or "short"
        # 解析失败 ≠ 中性。
        #
        # `extract_verdict` 为了让 trace UI 总有内容可显示，在**解析失败**时也会回退成
        # 中性。若这里照单全收，一份解析失败的研报就会以满权重投出「中性」票——把
        # 「这份报告坏了」伪装成「这位分析师看平」。那是 fail-open：共识方向被一个
        # 并不存在的观点稀释，而所有界面看起来都正常。
        #
        # 因此显式区分两者：只有解析成功（或老 trace 未带该字段）才计入投票，解析失败
        # 的单独计数并从加权中剔除，与 D2「弃权不计入分母但要可见」同一原则。
        if raw_trace.get("verdict_parsed") is False:
            unparsed_n += 1
            continue
        verdict = _normalize_direction(raw_trace.get("verdict")) or "中性"
        verdict_score = _direction_score(verdict)
        # 自报 confidence 只用于展示溯源，**不参与加权**。
        #
        # 实测（3497 份研报）：自报置信度与 T+1 方向正确率的相关系数仅 r=+0.033，
        # 且分档非单调——[70,75) 51.2%、[75,80) 55.3%、[80,101) 反而 **46.7%**，
        # 最高档是反向的。把它乘进权重，等于让"喊得最响"的分析师主导共识方向、
        # 共识强度与执行模式，而那个信号已被证伪。
        #
        # 缺省时更糟：原实现按"方向有多明确"反推一个 confidence，于是
        # "语气最坚定"自动获得最高权重——这会把自信的猜测放大成共识。
        # 现在权重只来自分析师角色权重与证据质量（等权思想，见方案 §5.1 免调参协议）。
        confidence = _confidence_to_float(raw_trace.get("confidence"))
        evidence_quality = 1.0 if str(raw_trace.get("key_finding") or "").strip() else 0.9
        agent_weight = _agent_weight(agent, horizon)
        effective_weight = agent_weight * evidence_quality

        if effective_weight <= 0:
            continue

        contribution = verdict_score * effective_weight
        weighted_sum += contribution
        total_weight += effective_weight
        horizon_scores[horizon]["sum"] += contribution
        horizon_scores[horizon]["weight"] += effective_weight

        if verdict_score > 0:
            supporting_weight += effective_weight
        elif verdict_score < 0:
            opposing_weight += effective_weight
        else:
            neutral_weight += effective_weight

        breakdown.append(
            {
                "agent": agent,
                "label": _AGENT_LABELS.get(agent, agent or "未知分析师"),
                "horizon": horizon,
                "verdict": verdict,
                "confidence": _safe_int(confidence * 100) if confidence is not None else None,
                "weight": round(agent_weight, 2),
                "effective_weight": effective_weight,
                "contribution": round(contribution, 3),
                "key_finding": str(raw_trace.get("key_finding") or "").strip(),
            }
        )

    if total_weight <= 0:
        return None

    mean_score = weighted_sum / total_weight
    consensus_direction = _direction_from_score(mean_score)
    consensus_strength = _safe_int(abs(mean_score) * 100)

    for item in breakdown:
        verdict_score = _direction_score(item["verdict"])
        # 用与共识方向完全相同的权重，避免"算方向时一套权重、算分歧时另一套"。
        weight = float(item.get("effective_weight") or 0.0)
        weighted_deviation_sum += abs(verdict_score - mean_score) * weight

    disagreement_score = _safe_int((weighted_deviation_sum / total_weight) * 50)

    dominant_horizon = "short"
    if horizon_scores:
        dominant_horizon = max(
            horizon_scores.items(),
            key=lambda kv: abs(kv[1]["sum"] / kv[1]["weight"]) if kv[1]["weight"] else 0.0,
        )[0]

    short_score = 0.0
    medium_score = 0.0
    if horizon_scores.get("short", {}).get("weight"):
        short_score = horizon_scores["short"]["sum"] / horizon_scores["short"]["weight"]
    if horizon_scores.get("medium", {}).get("weight"):
        medium_score = horizon_scores["medium"]["sum"] / horizon_scores["medium"]["weight"]
    horizon_conflict = short_score * medium_score < 0

    risk_feedback = dict(result_data.get("risk_feedback_state") or {})
    latest_risk_verdict = str(risk_feedback.get("latest_risk_verdict") or "").strip().lower()
    execution_preconditions = [str(x).strip() for x in (risk_feedback.get("execution_preconditions") or []) if str(x).strip()]
    de_risk_triggers = [str(x).strip() for x in (risk_feedback.get("de_risk_triggers") or []) if str(x).strip()]
    flip_conditions = list(dict.fromkeys(execution_preconditions + de_risk_triggers))[:5]

    stability_score = 100 - disagreement_score
    if horizon_conflict:
        stability_score -= 18
    if latest_risk_verdict in {"revise", "reject"}:
        stability_score -= 10
    if len(flip_conditions) >= 3:
        stability_score -= 6
    stability_score = _safe_int(stability_score)

    if consensus_strength < 35 or disagreement_score >= 58:
        execution_mode = "observe"
    elif disagreement_score >= 35 or latest_risk_verdict in {"revise", "reject"} or execution_preconditions:
        execution_mode = "conditional"
    else:
        execution_mode = "direct"

    support_ratio = supporting_weight / total_weight if total_weight else 0.0
    oppose_ratio = opposing_weight / total_weight if total_weight else 0.0
    neutral_ratio = neutral_weight / total_weight if total_weight else 0.0

    for item in breakdown:
        verdict_score = _direction_score(item["verdict"])
        if abs(verdict_score) < 0.01:
            item["stance"] = "neutral"
        elif final_direction_score and verdict_score * final_direction_score > 0:
            item["stance"] = "supporting"
        elif final_direction_score and verdict_score * final_direction_score < 0:
            item["stance"] = "opposing"
        elif verdict_score * mean_score > 0:
            item["stance"] = "supporting"
        else:
            item["stance"] = "opposing"

    breakdown.sort(key=lambda item: abs(float(item["contribution"])), reverse=True)

    supporting_labels = [item["label"] for item in breakdown if item["stance"] == "supporting"][:3]
    opposing_labels = [item["label"] for item in breakdown if item["stance"] == "opposing"][:3]
    summary_parts = [
        f"当前共识{consensus_direction}，共识强度 {consensus_strength}/100",
        f"{dominant_horizon} 视角权重更强",
    ]
    if opposing_labels:
        summary_parts.append(f"主要分歧来自{'、'.join(opposing_labels)}")
    summary_parts.append(f"建议{_format_mode(execution_mode)}")

    return {
        "consensus_direction": consensus_direction,
        "consensus_score": round(mean_score, 3),
        "consensus_strength": consensus_strength,
        "disagreement_score": disagreement_score,
        "stability_score": stability_score,
        "execution_mode": execution_mode,
        "execution_mode_label": _format_mode(execution_mode),
        "dominant_horizon": dominant_horizon,
        "horizon_conflict": horizon_conflict,
        "support_ratio": round(support_ratio, 3),
        "oppose_ratio": round(oppose_ratio, 3),
        "neutral_ratio": round(neutral_ratio, 3),
        "risk_gate": latest_risk_verdict or "pass",
        "execution_preconditions": execution_preconditions[:5],
        "de_risk_triggers": de_risk_triggers[:5],
        "flip_conditions": flip_conditions,
        "final_direction": final_direction_norm,
        "final_confidence": final_confidence,
        "supporting_agents": supporting_labels,
        "opposing_agents": opposing_labels,
        "agent_breakdown": breakdown,
        # 解析失败、未计入加权投票的分析师数量。必须可见：否则「7 位分析师一致看多」
        # 与「只有 2 份报告解析成功、其余 5 份坏了」在界面上完全一样。
        "unparsed_analyst_n": unparsed_n,
        "summary": "；".join(summary_parts),
    }
