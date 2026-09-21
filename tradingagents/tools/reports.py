"""L1 决策工具：历史报告的**结论**。

## 为什么这是 L1 而不是"再读一份报告文本"

「之前那份报告结论是什么」问的是一个**已经做出的判断**。报告正文（七个分析师
的长文）是 L2 证据，按需检索；而结论字段（方向 / 置信度 / 目标价 / 止损 / 周期）
是平台的确定性产出，**不可被模型改写**。让模型从正文里"再总结一次结论"，等于
在同一份材料上产生第二个可能不一致的结论源——这正是路线图里 99.5% 盲评问题
的成因之一。

## 「报告还没跑完」不等于「有一份结论」

`reports.status` 可能是 `pending` / `running` / `failed`。这三种状态下**没有**
可引用的结论。把 `direction=None` 当作"中性"或干脆省略，会让模型按"没有方向
约束"回答——那是凭空制造结论。因此每条证据都必须把状态说出来，未完成时明说
「结论尚未生成」。

## 用户维度

报告属于某个用户。端口在**闭包里绑定 user_id**，工具参数里没有 user_id——
与 `verdict_history_read` / `portfolio_holdings_read` 同一条纪律。
`api/` 不得被本模块 import（AST 边界测试强制）。
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence

from tradingagents.grounding.types import Coverage, EvidenceItem

from .spec import PLAN_LAYER_DECISION, RESEARCH_DISCLAIMER, ToolResult, ToolSpec

REPORT_CONCLUSION_CONTRACT = "REPORT_CONCLUSION"
_SOURCE_KEY = "report_conclusion"
MAX_EVIDENCE = 20

# 与 `report_service.ACTIVE_REPORT_STATUSES` 同一语义：这两个状态下报告还没产出结论
_RUNNING_STATUSES = ("pending", "running", "queued")


class ReportConclusionPort(Protocol):
    """读取当前用户的报告结论（实现由应用层提供，闭包内绑定 user_id）。

    返回行：
    `report_id / symbol / trade_date / status / decision / direction / confidence /
      target_price / stop_loss_price / horizon / freshness_status / created_at`
    """

    def recent_reports(
        self, *, limit: int, symbol: str | None = None
    ) -> Sequence[Mapping[str, Any]]: ...


def _fmt(value: Any, *, digits: int = 2, suffix: str = "") -> str:
    if value in (None, ""):
        return ""
    try:
        return f"{float(value):.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return str(value)


def _conclusion_text(row: Mapping[str, Any]) -> str:
    """把一行报告压成一句可引用的结论；未完成/失败时明说。"""
    status = str(row.get("status") or "").strip()
    if status in _RUNNING_STATUSES:
        return f"结论尚未生成（报告状态 {status}）"
    if status == "failed":
        # error 列可能含上游异常原文，不复述；只声明失败这一事实
        return "报告生成失败，没有可引用的结论"

    direction = str(row.get("direction") or "").strip()
    decision = str(row.get("decision") or "").strip()
    if not direction and not decision:
        return f"结论未提供（报告状态 {status or '未提供'}）"

    # 方向优先，但**只有 decision 也算结论**：实测库里有大量历史报告的
    # direction 列为空、只有 BUY/SELL/HOLD，那不是「结论未提供」。
    head = direction or decision
    parts = [f"结论 {head}" + (f"（{decision}）" if direction and decision else "")]
    confidence = row.get("confidence")
    if confidence not in (None, ""):
        parts.append(f"置信度 {int(confidence)}")
    target = _fmt(row.get("target_price"))
    stop = _fmt(row.get("stop_loss_price"))
    if target:
        parts.append(f"目标价 {target}")
    if stop:
        parts.append(f"止损 {stop}")
    horizon = str(row.get("horizon") or "").strip()
    if horizon:
        parts.append(f"周期 {horizon}")
    return "｜".join(parts)


def make_report_tools(reports: ReportConclusionPort) -> tuple[ToolSpec, ...]:
    def report_conclusion_lookup(
        *, limit: int = 5, symbol: str | None = None, as_of: str = ""
    ) -> ToolResult:
        # 不能写 `limit or 5`：limit=0 会被 falsy 换成 5（本仓库已多次踩坑）
        requested = 5 if limit is None else int(limit)
        capped = max(1, min(requested, MAX_EVIDENCE))
        try:
            rows = list(reports.recent_reports(limit=capped, symbol=symbol) or [])
        except Exception as exc:  # noqa: BLE001 — 取不到就如实声明，不抛给模型
            return ToolResult(
                data={"reports": [], "error": type(exc).__name__},
                evidence=(),
                coverage=Coverage(available=(), unavailable=(_SOURCE_KEY,)),
                degraded=True,
                degraded_reason=f"历史报告读取失败（{type(exc).__name__}）",
                as_of=as_of,
                disclaimer=RESEARCH_DISCLAIMER,
            )

        if not rows:
            return ToolResult(
                data={"reports": [], "count": 0},
                evidence=(),
                coverage=Coverage(
                    available=(_SOURCE_KEY,),
                    unavailable=("report_conclusion（尚无历史报告：结论无从谈起）",),
                ),
                as_of=as_of,
                disclaimer=RESEARCH_DISCLAIMER,
            )

        evidence: list[EvidenceItem] = []
        for index, row in enumerate(rows[:capped], start=1):
            status = str(row.get("status") or "未提供")
            parts = [
                f"{row.get('symbol') or '未提供'}（{row.get('trade_date') or '未提供'}）",
                _conclusion_text(row),
            ]
            if status not in _RUNNING_STATUSES:
                parts.append(f"报告状态 {status}")
                freshness = str(row.get("freshness_status") or "").strip()
                if freshness:
                    parts.append(f"时效 {freshness}")
            evidence.append(
                EvidenceItem(
                    id=f"E{index}",
                    field=f"历史报告{index}",
                    value="｜".join(part for part in parts if part),
                    unit="",
                    source_key=_SOURCE_KEY,
                    as_of=str(row.get("trade_date") or as_of),
                )
            )

        return ToolResult(
            data={"reports": [dict(row) for row in rows[:capped]], "count": len(rows[:capped])},
            evidence=tuple(evidence),
            coverage=Coverage(available=(_SOURCE_KEY,), unavailable=()),
            as_of=as_of,
            disclaimer=RESEARCH_DISCLAIMER,
        )

    spec = ToolSpec(
        name="report_conclusion_lookup",
        title="历史报告结论",
        description=(
            "读取当前用户**历史分析报告的结论**（方向、置信度、目标价、止损、周期与报告状态），"
            "最近 N 份。用于回答「之前那份报告结论是什么」「上次分析怎么说的」。"
            "结论来自平台已落库的报告，不接受模型改写；报告还在生成中或已失败时会"
            "显式声明「结论尚未生成」，不得被当成中性结论。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": f"返回最近几份，1-{MAX_EVIDENCE}，默认 5"},
                "symbol": {"type": "string", "description": "只看某只标的（可选）"},
                "as_of": {"type": "string", "description": "基准日 YYYY-MM-DD"},
            },
            "required": [],
        },
        output_contract=REPORT_CONCLUSION_CONTRACT,
        handler=report_conclusion_lookup,
        cost="cheap",
        plan_layer=PLAN_LAYER_DECISION,
        latency_budget_ms=8000,
        freshness_sources=(_SOURCE_KEY,),
    )
    return (spec,)
