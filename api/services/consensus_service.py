"""Deterministic consensus scoring for multi-agent stock analysis.

This service borrows the practical part of the "debate + weighted fusion"
pattern from open-source multi-agent investing projects: let LLMs produce the
arguments, then let a deterministic layer score agreement, disagreement, and
execution readiness so the final UX is stable across reruns.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional

from tradingagents.agents.utils.analyst_structured import resolve_trace_direction
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

#: 参与度下限（投票人数 / 出报告的分析师数）。低于此比例时，共识只代表面板里的
#: 少数声音：`consensus_strength` 仍然按「投票者内部有多一致」计算（那是它精确的
#: 定义，不能与覆盖率混为一谈），但执行模式与稳定性必须对覆盖率有反应。
_LOW_PARTICIPATION_RATIO = 0.5

#: 「直接执行」所需的最少投票人数。即使比例达标，1-2 条声音也不足以支撑「直接下单」。
_DIRECT_MIN_PARTICIPANTS = 3

#: 参与度不足时稳定性最多扣分值（按缺失覆盖率线性扣分，见 stability_score 计算）。
_PARTICIPATION_STABILITY_PENALTY = 25


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
    recovered_n = 0
    for raw_trace in traces:
        agent = str(raw_trace.get("agent") or "").strip()
        horizon = str(raw_trace.get("horizon") or "short").strip().lower() or "short"
        # 方向取自该分析师真正写下的机读块，而不是某一个固定块。
        #
        # 两个教训叠在这里：
        #
        # 1）`extract_verdict` 为了让 trace UI 总有内容可显示，在**解析失败**时也会回退成
        #    中性。若照单全收，一份没给出方向的研报就会以满权重投出「中性」票——把
        #    「这位分析师没表态」伪装成「这位分析师看平」。那是 fail-open：共识被一个
        #    并不存在的观点稀释，而所有界面看起来都正常。所以必须区分「没方向」和
        #    「真·中性」。
        #
        # 2）但「没解析到」曾经被等同于「这份报告坏了」，而实际上代码只认 `VERDICT`
        #    块，分析师却在 `ANALYST_JSON` 块里写着方向。实测 262 条 trace 中 114 条
        #    （43.5%）判为解析失败，其中 103 条（90.4%）的 ANALYST_JSON 里方向完好——
        #    于是共识卡片把「2/7 位分析师看多」显示成「共识强度 100/100」，被丢掉的
        #    5 份里还有中性票。那是 fail-closed 到丢掉真实意见，并把它包装成高确定性。
        #
        # `resolve_trace_direction` 是唯一判定入口（VERDICT → ANALYST_JSON → 无），
        # 与研究经理共用，两者不会再对同一份报告给出不同结论。
        # 只有「两个块都没有方向」才计入 unparsed 并从加权中剔除，与 D2「弃权不计入
        # 分母但要可见」同一原则；从 ANALYST_JSON 取到方向的单独计数，保持可复核。
        decision = resolve_trace_direction(raw_trace)
        if decision.direction is None:
            unparsed_n += 1
            continue
        if decision.source == "analyst_json":
            recovered_n += 1
        verdict = decision.direction
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
        # 展示用 confidence 必须来自「方向所在的那个块」，不能拿另一个块的数字冒充。
        # 尤其是：VERDICT 没解析出来时 trace 的 confidence 是按中性反推的缺省值，
        # 显示它会让用户以为模型自报了这个数。
        raw_confidence = (
            decision.confidence
            if decision.source == "analyst_json"
            else raw_trace.get("confidence")
        )
        confidence = _confidence_to_float(raw_confidence)
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

        # 注意：支持/反对/中性**不在这里**按 verdict_score 的正负累计。
        #
        # 那正是审计 §4.3 的缺陷：按正负累计 = 以 **0** 为参照系（看多 vs 看空），
        # 而下面的 `stance` 分类以**最终结论**为参照系。两个参照系共用「支持/反对」
        # 一个词，于是名单与比例会互相矛盾；对**看空报告**尤其致命——共识方向为看空
        # 时，多数投看空的分析师会被算进 `oppose_ratio`，卡片显示
        # 「共识方向 看空｜支持 20%／反对 65%」，字面意思是"多数人反对这个看空结论"。
        # 现在两者的唯一来源是 `stance`（见下方分类循环之后的汇总），结构上不可能漂移。

        breakdown.append(
            {
                "agent": agent,
                "label": _AGENT_LABELS.get(agent, agent or "未知分析师"),
                "horizon": horizon,
                "verdict": verdict,
                # 方向来自哪个机读块：verdict / analyst_json。
                # 必须可见：否则「这块报告本来就好」与「从 ANALYST_JSON 兜回来的」
                # 长得一样，提示词回归就无从发现。
                "verdict_source": decision.source,
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

    # 参与度：真正投票的分析师数 / 出报告的分析师数。
    #
    # 为什么必须显式暴露：`consensus_strength` 只衡量「投票者内部有多一致」，弃权者
    # 按约定 D2 不计入分母。于是 7 人面板里只有 1 人看多时，强度同样显示 100/100
    # —— 与 7 人一致时**逐字节相同**，而执行模式据此给出「直接执行」。
    # 实测（审计 §2.2）：6 份已落库报告强度 ≥85 而参与者 ≤3；f7e60b27 为 1/7 → 100。
    participant_n = len(breakdown)
    expected_n = len(traces)
    participation_ratio = (participant_n / expected_n) if expected_n else 0.0
    low_participation = (
        participation_ratio < _LOW_PARTICIPATION_RATIO
        or participant_n < _DIRECT_MIN_PARTICIPANTS
    )

    stability_score = 100 - disagreement_score
    if horizon_conflict:
        stability_score -= 18
    if latest_risk_verdict in {"revise", "reject"}:
        stability_score -= 10
    if len(flip_conditions) >= 3:
        stability_score -= 6
    # 稳定性是「这个结论有多可依赖」的判断，而只有少数人投票的结论本来就更不可依赖。
    # 按缺失覆盖率线性扣分（1/7 → 扣 21 分），上限 _PARTICIPATION_STABILITY_PENALTY。
    stability_score -= _safe_int(
        (1.0 - participation_ratio) * _PARTICIPATION_STABILITY_PENALTY
    )
    stability_score = max(0, min(100, _safe_int(stability_score)))

    if consensus_strength < 35 or disagreement_score >= 58:
        execution_mode = "observe"
    elif (
        low_participation
        or disagreement_score >= 35
        or latest_risk_verdict in {"revise", "reject"}
        or execution_preconditions
    ):
        execution_mode = "conditional"
    else:
        execution_mode = "direct"

    # ── 立场分类：**唯一**的"支持/反对/中性"判定 ────────────────────────────
    #
    # 参照系（`stance_reference`）必须显式：有最终结论时以最终结论为准，
    # 否则退回面板均值。读者不能靠猜"这个支持是在支持什么"。
    if final_direction_score:
        stance_reference = "final_direction"
        reference_score = final_direction_score
    else:
        stance_reference = "panel_mean"
        reference_score = mean_score

    for item in breakdown:
        verdict_score = _direction_score(item["verdict"])
        if abs(verdict_score) < 0.01:
            item["stance"] = "neutral"
        elif reference_score and verdict_score * reference_score > 0:
            item["stance"] = "supporting"
        elif reference_score and verdict_score * reference_score < 0:
            item["stance"] = "opposing"
        elif verdict_score * mean_score > 0:
            item["stance"] = "supporting"
        else:
            item["stance"] = "opposing"

    # 比例与名单**同源**：都从上面的 `stance` 导出，因此不可能出现
    # 「支持 0.38，支持者：一个投看空的人」这种自相矛盾。
    supporting_weight = 0.0
    opposing_weight = 0.0
    neutral_weight = 0.0
    for item in breakdown:
        weight = float(item.get("effective_weight") or 0.0)
        if item["stance"] == "supporting":
            supporting_weight += weight
        elif item["stance"] == "opposing":
            opposing_weight += weight
        else:
            neutral_weight += weight

    breakdown.sort(key=lambda item: abs(float(item["contribution"])), reverse=True)

    support_ratio = supporting_weight / total_weight if total_weight else 0.0
    oppose_ratio = opposing_weight / total_weight if total_weight else 0.0
    neutral_ratio = neutral_weight / total_weight if total_weight else 0.0

    supporting_labels = [item["label"] for item in breakdown if item["stance"] == "supporting"][:3]
    opposing_labels = [item["label"] for item in breakdown if item["stance"] == "opposing"][:3]
    participation_text = f"{expected_n} 位分析师中 {participant_n} 位参与"
    if low_participation:
        participation_text += "（参与度低，结论仅代表少数声音）"
    # ── 周期叙述：说什么就必须是什么 ─────────────────────────────────────────
    #
    # 旧写法 `f"{dominant_horizon} 视角权重更强"` 把两个不同的量混为一谈：
    # `dominant_horizon` 取的是各周期 `|sum/weight|`（**方向信号强度**）的 argmax，
    # 叙述却称"权重"。实测探针：short 权重 4.862、medium 权重 1.990（short 是 2.44 倍），
    # 叙述照样写「medium 视角权重更强」，因为 medium 的方向信号更强。
    #
    # 现在分开说，且**各自说自己的口径**：
    #   * 权重句：由各周期实际 `effective_weight` 之和的 argmax 决定，并给出倍数
    #     （可核对，不是"更重"这种无法验证的说法）；
    #   * 方向句：只在两个方向**不同**时补充说明，避免读者把两者当成同一件事。
    #
    # 单周期时**没有可比对象**，不得出现任何"更强/更重"的比较。
    horizon_weights = {
        h: float(v.get("weight") or 0.0)
        for h, v in horizon_scores.items()
        if float(v.get("weight") or 0.0) > 0
    }
    if len(horizon_weights) >= 2:
        heaviest_horizon = max(horizon_weights, key=lambda h: horizon_weights[h])
        lightest_horizon = min(horizon_weights, key=lambda h: horizon_weights[h])
        if horizon_weights[lightest_horizon] > 0:
            multiple = horizon_weights[heaviest_horizon] / horizon_weights[lightest_horizon]
            horizon_sentence = (
                f"{heaviest_horizon} 视角权重更重"
                f"（{heaviest_horizon} {horizon_weights[heaviest_horizon]:.2f}"
                f" / {lightest_horizon} {horizon_weights[lightest_horizon]:.2f}"
                f"，{multiple:.2f} 倍）"
            )
        else:
            horizon_sentence = f"{heaviest_horizon} 视角权重更重"
        if heaviest_horizon != dominant_horizon:
            horizon_sentence += f"；方向信号更强的是 {dominant_horizon}"
    else:
        only_horizon = next(iter(horizon_weights), dominant_horizon)
        horizon_sentence = f"仅 {only_horizon} 一个周期参与，无周期对比"

    summary_parts = [
        f"当前共识{consensus_direction}，共识强度 {consensus_strength}/100",
        participation_text,
        horizon_sentence,
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
        # 「支持/反对」的**参照系**：有最终结论时是 `final_direction`，否则是
        # `panel_mean`（面板均值）。必须显式暴露——否则读者无法判断"支持"是在支持
        # 什么，而这正是审计 §4.3「一个词两套参照系」的成因。
        # 比例与 `supporting_agents`/`opposing_agents` 名单**同源**（都来自 stance），
        # 所以不可能再出现"支持 38%，支持者却是个投看空的人"。
        "stance_reference": stance_reference,
        # 没有风控结论时写 None，**绝不写 "pass"**。
        #
        # 「风控没有反对」与「风控没有运行」是完全不同的两件事：前者是经审查后的
        # 放行，后者是未经审查。旧实现写 `latest_risk_verdict or "pass"`，于是当
        # `risk_feedback_state` 缺失/为空（风控环节根本没跑）时，界面与下游工具都显示
        # 「风险闸 通过」。实测：2026-09 的 80 份报告中 24 份如此；926 份显示「直接
        # 执行」的报告**全部**没有风控状态 —— 即「未经风控 + 直接执行」被呈现成
        # 「已通过风控 + 直接执行」。下游 `tools/consensus.py` 的「风险闸 未提供」
        # 分支也因此永远不可达。
        "risk_gate": latest_risk_verdict or None,
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
        #
        # 语义已收紧：这里只统计**两个机读块都没有给出方向**的分析师。旧实现把
        # 「VERDICT 块缺失」也算进来，而分析师的方向其实写在 ANALYST_JSON 里，
        # 于是 43.5% 的正常报告被显示成「解析失败」。
        # 参与度三件套 + 低参与度标记。必须与 consensus_strength 同时可见：只看强度
        # 无法区分「7 人一致」（7/7 → 100）与「1 人投票、6 人弃权」（1/7 → 也 100）。
        "participant_n": participant_n,
        "expected_n": expected_n,
        "participation_ratio": round(participation_ratio, 3),
        "low_participation": low_participation,
        "unparsed_analyst_n": unparsed_n,
        # 方向取自 ANALYST_JSON（VERDICT 块缺失或不可用）的分析师数量。
        # 单独计数=共识可复核：用户能看出有多少票是兜回来的。
        "recovered_analyst_n": recovered_n,
        "summary": "；".join(summary_parts),
    }
