"""L1 决策工具：一份报告内部的**多视角共识**。

## 为什么"分析师们怎么看"必须由平台给出

一份深度报告是多位分析师（市场/情绪/新闻/基本面/宏观/资金/量价）各自出结论后
合成的。平台把他们的观点做成**确定性共识**（`consensus_service`）：加权方向、
共识强度、分歧度、稳定度、执行模式、风险闸、以及**谁支持谁反对**。

模型读到的正文是散文，凭它总结"分析师们一致看多"极易出错——尤其是当部分
分析师的结论**解析失败**时：服务层刻意记录 `unparsed_analyst_n` 并拒绝把
解析失败当"中性票"。因此本工具把它作为一条独立证据说出来：
「7 位一致看多」与「只有 2 份解析成功、其余 5 份坏了」在界面上必须不一样。

## 弃权：三种"没有"要分开

1. 该标的**没有历史报告** → 声明没有报告；
2. 有报告但**没有共识结果**（报告生成于共识功能上线前，或分析师轨迹为空）→
   声明「没有多视角共识」，与第 1 种不同；
3. 取数失败 / 缺 symbol → `degraded` + 可读原因，**不外传上游原文**。

## 用户维度

报告属于某个用户。端口在**闭包里绑定 user_id**，工具参数里没有 user_id。
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol

from tradingagents.grounding.types import Coverage, EvidenceItem

from .spec import PLAN_LAYER_DECISION, RESEARCH_DISCLAIMER, ToolResult, ToolSpec

REPORT_CONSENSUS_CONTRACT = "REPORT_CONSENSUS"
_SOURCE_KEY = "report_consensus"


class ReportConsensusPort(Protocol):
    """读取某标的最近一份报告的多视角共识（实现由应用层提供，闭包内绑定 user_id）。

    返回 `None`（没有报告）或：
    `{"report_id", "symbol", "trade_date", "direction", "confidence",
      "consensus": {...build_consensus_summary 的输出...}}`；
    `consensus` 为 `None` 表示该报告没有共识结果（与"没有报告"不同）。
    """

    def latest_consensus(self, *, symbol: str) -> Mapping[str, Any] | None: ...


def _fmt(value: Any, *, digits: int = 2, suffix: str = "") -> str:
    if value in (None, ""):
        return ""
    try:
        return f"{float(value):.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return str(value)


def _ratio(value: Any) -> str:
    try:
        return f"{float(value) * 100:.0f}%"
    except (TypeError, ValueError):
        return ""


def make_consensus_tools(consensus: ReportConsensusPort) -> tuple[ToolSpec, ...]:
    def report_consensus_read(*, symbol: str, as_of: str = "") -> ToolResult:
        if not symbol or not str(symbol).strip():
            return ToolResult(
                data={"consensus": None},
                evidence=(),
                coverage=Coverage(available=(), unavailable=(_SOURCE_KEY,)),
                degraded=True,
                degraded_reason="读取多视角共识需要 symbol",
                as_of=as_of,
                disclaimer=RESEARCH_DISCLAIMER,
            )
        try:
            payload = consensus.latest_consensus(symbol=str(symbol).strip())
        except Exception as exc:  # noqa: BLE001 — 取不到就如实声明，不抛给模型
            return ToolResult(
                data={"consensus": None, "error": type(exc).__name__},
                evidence=(),
                coverage=Coverage(available=(), unavailable=(_SOURCE_KEY,)),
                degraded=True,
                degraded_reason=f"多视角共识读取失败（{type(exc).__name__}）",
                as_of=as_of,
                disclaimer=RESEARCH_DISCLAIMER,
            )

        if payload is None:
            return ToolResult(
                data={"consensus": None, "count": 0},
                evidence=(),
                coverage=Coverage(
                    available=(_SOURCE_KEY,),
                    unavailable=(f"report_consensus（{symbol} 没有历史报告）",),
                ),
                as_of=as_of,
                disclaimer=RESEARCH_DISCLAIMER,
            )

        if not isinstance(payload, Mapping):
            return ToolResult(
                data={"consensus": None, "error": "unparsable_payload"},
                evidence=(),
                coverage=Coverage(available=(), unavailable=(_SOURCE_KEY,)),
                degraded=True,
                degraded_reason="多视角共识结构无法解析（不是映射）",
                as_of=as_of,
                disclaimer=RESEARCH_DISCLAIMER,
            )

        summary = payload.get("consensus")
        trade_date = str(payload.get("trade_date") or "")
        if not isinstance(summary, Mapping) or not summary:
            # 有报告但没有共识 ≠ 没有报告：必须区分，否则用户会以为"从没分析过"
            return ToolResult(
                data={"consensus": None, "count": 0, "report_id": payload.get("report_id")},
                evidence=(),
                coverage=Coverage(
                    available=(_SOURCE_KEY,),
                    unavailable=(
                        f"report_consensus（{symbol} {trade_date} 的报告没有多视角共识结果："
                        "报告生成于共识功能上线前，或分析师轨迹为空）",
                    ),
                ),
                as_of=trade_date or as_of,
                disclaimer=RESEARCH_DISCLAIMER,
            )

        evidence: list[EvidenceItem] = []
        head = [
            f"{symbol}（{trade_date or '未提供'}）",
            f"共识方向 {summary.get('consensus_direction') or '未提供'}",
        ]
        score = _fmt(summary.get("consensus_score"), digits=3)
        if score:
            head.append(f"共识得分 {score}")
        strength = str(summary.get("consensus_strength") or "").strip()
        if strength:
            head.append(f"强度 {strength}")
        support = _ratio(summary.get("support_ratio"))
        oppose = _ratio(summary.get("oppose_ratio"))
        neutral = _ratio(summary.get("neutral_ratio"))
        if support or oppose or neutral:
            head.append(f"支持 {support or '—'}／反对 {oppose or '—'}／中性 {neutral or '—'}")
        final_direction = str(summary.get("final_direction") or "").strip()
        if final_direction:
            final_confidence = summary.get("final_confidence")
            head.append(
                f"报告结论 {final_direction}"
                + (f"（置信度 {int(final_confidence)}）" if final_confidence not in (None, "") else "")
            )
        evidence.append(
            EvidenceItem(
                id="E1",
                field="多视角共识",
                value="｜".join(head),
                unit="",
                source_key=_SOURCE_KEY,
                as_of=trade_date or as_of,
            )
        )

        detail = []
        disagreement = _fmt(summary.get("disagreement_score"), digits=3)
        if disagreement:
            detail.append(f"分歧度 {disagreement}")
        stability = _fmt(summary.get("stability_score"), digits=3)
        if stability:
            detail.append(f"稳定度 {stability}")
        horizon = str(summary.get("dominant_horizon") or "").strip()
        if horizon:
            detail.append(f"主导周期 {horizon}")
        detail.append("周期冲突 " + ("是" if summary.get("horizon_conflict") else "否"))
        if detail:
            evidence.append(
                EvidenceItem(
                    id="E2",
                    field="分歧与稳定性",
                    value="｜".join(detail),
                    unit="",
                    source_key=_SOURCE_KEY,
                    as_of=trade_date or as_of,
                )
            )

        execution = [
            f"执行模式 {summary.get('execution_mode_label') or summary.get('execution_mode') or '未提供'}",
            f"风险闸 {summary.get('risk_gate') or '未提供'}",
        ]
        preconditions = [str(x) for x in (summary.get("execution_preconditions") or []) if str(x).strip()]
        if preconditions:
            execution.append("执行前提 " + "；".join(preconditions[:2]))
        de_risk = [str(x) for x in (summary.get("de_risk_triggers") or []) if str(x).strip()]
        if de_risk:
            execution.append("减仓触发 " + "；".join(de_risk[:2]))
        evidence.append(
            EvidenceItem(
                id=f"E{len(evidence) + 1}",
                field="执行模式与风险闸",
                value="｜".join(execution),
                unit="",
                source_key=_SOURCE_KEY,
                as_of=trade_date or as_of,
            )
        )

        supporting = [str(x) for x in (summary.get("supporting_agents") or []) if str(x).strip()]
        opposing = [str(x) for x in (summary.get("opposing_agents") or []) if str(x).strip()]
        if supporting or opposing:
            evidence.append(
                EvidenceItem(
                    id=f"E{len(evidence) + 1}",
                    field="分析师立场",
                    value=(
                        f"支持 {('、'.join(supporting[:6]) or '无')}"
                        f"｜反对 {('、'.join(opposing[:6]) or '无')}"
                    ),
                    unit="",
                    source_key=_SOURCE_KEY,
                    as_of=trade_date or as_of,
                )
            )

        unparsed = summary.get("unparsed_analyst_n")
        if isinstance(unparsed, (int, float)) and int(unparsed) > 0:
            # 解析失败必须可见：否则「一致看多」与「只有两份解析成功」长得一样
            evidence.append(
                EvidenceItem(
                    id=f"E{len(evidence) + 1}",
                    field="未计入的分析师",
                    value=(
                        f"{int(unparsed)} 位分析师的结论解析失败，**未计入加权投票**"
                        "（它们既不是支持也不是中性）"
                    ),
                    unit="",
                    source_key=_SOURCE_KEY,
                    as_of=trade_date or as_of,
                )
            )

        if not evidence:
            return ToolResult(
                data={"consensus": None, "error": "unparsable_payload"},
                evidence=(),
                coverage=Coverage(available=(), unavailable=(_SOURCE_KEY,)),
                degraded=True,
                degraded_reason="多视角共识结构无法解析（无可引用字段）",
                as_of=trade_date or as_of,
                disclaimer=RESEARCH_DISCLAIMER,
            )

        return ToolResult(
            data={
                "consensus": dict(summary),
                "report_id": payload.get("report_id"),
                "trade_date": trade_date,
                "count": 1,
            },
            evidence=tuple(evidence),
            coverage=Coverage(available=(_SOURCE_KEY,), unavailable=()),
            as_of=trade_date or as_of,
            disclaimer=RESEARCH_DISCLAIMER,
        )

    spec = ToolSpec(
        name="report_consensus_read",
        title="报告的多视角共识",
        description=(
            "读取某标的**最近一份报告**的多视角共识：加权共识方向与得分、强度、"
            "支持/反对/中性比例、分歧度与稳定度、主导周期与周期冲突、执行模式与风险闸、"
            "以及支持与反对的分析师名单。用于回答「分析师们怎么看这只」「他们意见一致吗」。"
            "共识由平台的分析师轨迹确定性合成（解析失败的分析师**不计入**并单独声明），"
            "不由模型从正文里自行总结；没有报告与没有共识结果是两种不同的声明。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "标的代码，如 600519.SH"},
                "as_of": {"type": "string", "description": "基准日 YYYY-MM-DD"},
            },
            "required": ["symbol"],
        },
        output_contract=REPORT_CONSENSUS_CONTRACT,
        handler=report_consensus_read,
        cost="cheap",
        plan_layer=PLAN_LAYER_DECISION,
        latency_budget_ms=8000,
        freshness_sources=(_SOURCE_KEY,),
    )
    return (spec,)
