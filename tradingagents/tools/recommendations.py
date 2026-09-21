"""L1 决策工具：读**平台已生成**的推荐结果。

## 为什么必须区分「平台推荐」与「模型推荐」

平台有一条确定性的选股管道：候选池（跟踪持仓 + 自选 + 可选 seed）→ 因子打分
→ 可交易性闸门 → 落库，并带 T+N 兑现回填（`recommendation_feedback_service`）。
问「平台推荐了什么」问的是**这条管道的产出**。让模型临场"推荐几只"，等于把
一个没有样本、没有评分、没有兑现记录的即兴判断，说成平台的推荐结果。

## 只读，不触发生成

本工具**不发起全市场扫描**：它读平台已经生成并落库的那一批结果
（`recommendation_feedback_service.list_scan_results`——`/v1/recommendations`
端点算完就会落库）。生成是动作，读取是事实，两者不能混在一个只读工具里。

## 证据里为什么要带"兑现"

一批推荐如果只有得分和理由，"平台推荐得准不准"无从回答。落库行自带
`feedback_status / feedback_horizon_days / realized_return_pct`，因此证据里把
「已评估 / 待评估 / 数据不足」逐条说出来——**待评估不得被读成已验证有效**。

## 弃权与降级

* 没有任何已落库推荐 → `unavailable`（不是空列表当成功）；
* payload 结构解析不出来（不是映射、没有 items 列表）→ `degraded` 并说明
  「解析不到」，与「确实是空」走**不同**分支；
* 取数失败 → `degraded=True`，**不外传上游原文**。
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol

from tradingagents.grounding.types import Coverage, EvidenceItem

from .spec import PLAN_LAYER_DECISION, RESEARCH_DISCLAIMER, ToolResult, ToolSpec

RECOMMENDATION_CONTRACT = "PLATFORM_RECOMMENDATION"
_SOURCE_KEY = "recommendation"
MAX_EVIDENCE = 20
_EVIDENCE_FIELD = "平台推荐"


class RecommendationPort(Protocol):
    """读取当前用户已落库的推荐结果（实现由应用层提供，闭包内绑定 user_id）。

    返回一个映射：`{"items": [...], "source": str, "score_profile": str,
    "generated_at": str}`。每条形如 `recommendation_feedback_service` 的
    `_scan_row_to_dict`：`symbol / name / rank / score / reasons / strategy_hits /
    risk_flags / quote / feedback_status / feedback_horizon_days /
    realized_return_pct / source_mode`。
    """

    def list_recommendations(self, *, limit: int) -> Mapping[str, Any]: ...


def _fmt(value: Any, *, digits: int = 2, suffix: str = "") -> str:
    if value in (None, ""):
        return ""
    try:
        return f"{float(value):.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return str(value)


def _quote_of(row: Mapping[str, Any]) -> tuple[Any, Any]:
    """兼容两种落库形状：持久化行的 quote 子对象与实时返回的平铺字段。"""
    quote = row.get("quote")
    quote = quote if isinstance(quote, Mapping) else {}
    price = quote.get("price", row.get("live_price", row.get("entry_price")))
    change = quote.get("change_pct", row.get("price_change_pct"))
    return price, change


def _outcome_text(row: Mapping[str, Any]) -> str:
    """兑现状态必须逐条说出来：待评估 ≠ 已验证有效。"""
    status = str(row.get("feedback_status") or "").strip()
    horizon = row.get("feedback_horizon_days")
    suffix = f"T+{horizon}" if horizon not in (None, "") else "持有期"
    if status == "evaluated":
        realized = _fmt(row.get("realized_return_pct"), suffix="%")
        return f"已评估 {suffix} {realized}" if realized else f"已评估 {suffix}（收益未提供）"
    if status == "pending":
        return f"待评估（{suffix} 窗口未走完）"
    if status == "insufficient_data":
        return "数据不足，无法评估（不计入命中统计）"
    return f"兑现状态未提供（{suffix}）"


def _evidence_parts(row: Mapping[str, Any], index: int) -> list[str]:
    rank = row.get("rank")
    head = f"第{rank}名" if rank not in (None, "") else f"第{index}条"
    symbol = str(row.get("symbol") or "未提供")
    name = str(row.get("name") or "").strip()
    parts = [f"{head} {symbol}" + (f" {name}" if name else "")]

    score = _fmt(row.get("score"))
    if score:
        parts.append(f"得分 {score}")

    price, change = _quote_of(row)
    price_text = _fmt(price)
    if price_text:
        try:
            change_text = f"{float(change):+.2f}%"
        except (TypeError, ValueError):
            change_text = ""
        parts.append(f"现价 {price_text}" + (f"（{change_text}）" if change_text else ""))

    reasons = [str(x) for x in (row.get("reasons") or []) if str(x).strip()]
    if reasons:
        parts.append("入选理由 " + "、".join(reasons[:2]))

    hits = [str(x) for x in (row.get("strategy_hits") or []) if str(x).strip()]
    if hits:
        parts.append("策略 " + "、".join(hits[:3]))

    flags = [str(x) for x in (row.get("risk_flags") or []) if str(x).strip()]
    if flags:
        # 风险标记必须可引用：只报喜不报忧的推荐无法被解释
        parts.append("风险 " + "、".join(flags[:3]))

    parts.append("兑现 " + _outcome_text(row))
    return parts


def make_recommendation_tools(recommendations: RecommendationPort) -> tuple[ToolSpec, ...]:
    def recommendation_read(*, limit: int = 5, as_of: str = "") -> ToolResult:
        # 不能写 `limit or 5`：limit=0 会被 falsy 换成 5（本仓库已多次踩坑）
        requested = 5 if limit is None else int(limit)
        capped = max(1, min(requested, MAX_EVIDENCE))
        try:
            payload = recommendations.list_recommendations(limit=capped)
        except Exception as exc:  # noqa: BLE001 — 取不到就如实声明，不抛给模型
            return ToolResult(
                data={"items": [], "error": type(exc).__name__},
                evidence=(),
                coverage=Coverage(available=(), unavailable=(_SOURCE_KEY,)),
                degraded=True,
                degraded_reason=f"推荐结果读取失败（{type(exc).__name__}）",
                as_of=as_of,
                disclaimer=RESEARCH_DISCLAIMER,
            )

        if not isinstance(payload, Mapping):
            # 解析不到 ≠ 空：结构不对时必须降级说明，不能退化成"平台没推荐"
            return ToolResult(
                data={"items": [], "error": "unparsable_payload"},
                evidence=(),
                coverage=Coverage(available=(), unavailable=(_SOURCE_KEY,)),
                degraded=True,
                degraded_reason="推荐结果结构无法解析（不是映射）",
                as_of=as_of,
                disclaimer=RESEARCH_DISCLAIMER,
            )

        items = payload.get("items")
        if not isinstance(items, (list, tuple)):
            return ToolResult(
                data={"items": [], "error": "missing_items"},
                evidence=(),
                coverage=Coverage(available=(), unavailable=(_SOURCE_KEY,)),
                degraded=True,
                degraded_reason="推荐结果缺少 items 列表，结构无法解析",
                as_of=as_of,
                disclaimer=RESEARCH_DISCLAIMER,
            )

        if not items:
            # 结构正确但确实空：这是可回答的事实（平台还没为这个用户生成推荐），
            # 但仍必须声明"这条维度没有内容"，不得当成"推荐过了、没问题"
            return ToolResult(
                data={"items": [], "count": 0, "source": payload.get("source")},
                evidence=(),
                coverage=Coverage(
                    available=(_SOURCE_KEY,),
                    unavailable=("recommendation（平台尚未为该用户生成推荐结果）",),
                ),
                as_of=as_of,
                disclaimer=RESEARCH_DISCLAIMER,
            )

        rows = [row for row in items if isinstance(row, Mapping)][:capped]
        evidence: list[EvidenceItem] = []
        for index, row in enumerate(rows, start=1):
            evidence.append(
                EvidenceItem(
                    id=f"E{index}",
                    field=f"{_EVIDENCE_FIELD}{index}",
                    value="｜".join(_evidence_parts(row, index)),
                    unit="",
                    source_key=_SOURCE_KEY,
                    as_of=str(row.get("created_at") or as_of)[:10],
                )
            )
        # 批次口径必须可引用：不说生成时间的推荐无法判断新鲜度
        generated_at = str(payload.get("generated_at") or "").strip()
        source = str(payload.get("source") or "").strip()
        if generated_at or source:
            evidence.append(
                EvidenceItem(
                    id=f"E{len(evidence) + 1}",
                    field="推荐批次口径",
                    value="｜".join(
                        part
                        for part in (
                            f"生成于 {generated_at}" if generated_at else "",
                            f"数据来源 {source}" if source else "",
                            f"评分档 {payload.get('score_profile')}"
                            if payload.get("score_profile")
                            else "",
                        )
                        if part
                    ),
                    unit="",
                    source_key=_SOURCE_KEY,
                    as_of=as_of,
                )
            )

        return ToolResult(
            data={
                "items": [dict(row) for row in rows],
                "count": len(rows),
                "source": payload.get("source"),
                "score_profile": payload.get("score_profile"),
                "generated_at": payload.get("generated_at"),
            },
            evidence=tuple(evidence),
            coverage=Coverage(available=(_SOURCE_KEY,), unavailable=()),
            as_of=as_of,
            disclaimer=RESEARCH_DISCLAIMER,
        )

    spec = ToolSpec(
        name="recommendation_read",
        title="平台推荐结果",
        description=(
            "读取**平台已生成并落库**的推荐结果（候选池、得分、入选理由、策略标记、"
            "风险标记，以及该条推荐之后的兑现状态）。用于回答「平台推荐了什么」"
            "「这些推荐后来兑现了吗」。推荐来自平台的确定性选股管道与兑现回填，"
            "**不是**让模型临场生成；没有已落库结果时会显式声明。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": f"返回条数，1-{MAX_EVIDENCE}，默认 5"},
                "as_of": {"type": "string", "description": "基准日 YYYY-MM-DD"},
            },
            "required": [],
        },
        output_contract=RECOMMENDATION_CONTRACT,
        handler=recommendation_read,
        cost="cheap",
        plan_layer=PLAN_LAYER_DECISION,
        latency_budget_ms=8000,
        freshness_sources=(_SOURCE_KEY,),
    )
    return (spec,)
