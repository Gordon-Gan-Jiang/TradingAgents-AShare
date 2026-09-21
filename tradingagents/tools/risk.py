"""L1 决策工具：组合体检（风险排序）。

## 为什么"哪只最危险"必须由代码排序

用户问「我持仓里哪只最危险」。这是一个**排序**问题，而排序依据（什么算危险、
优先级怎么定、同优先级怎么比）如果交给模型临时决定，同一份持仓两次提问会得到
两个不同的"最危险的股票"——而且没有任何东西能说清它凭什么这么排。

平台的退出引擎（`exit_engine_service`）已经把这件事做成了确定性规则：逐只持仓
按规则优先级判定 `清仓 / 减仓 / 持有 / 观察`，给出 `priority`、`triggers`、
`reasons`、可执行量（A 股 T+1 / 整手 / 涨跌停的约束单独列出），并**按
「优先级 → 浮亏从大到小」排序**。本工具只做转换与引用。

## 判定维度由已有的确定性判定喂进去

`evaluate_portfolio(market_states=...)` 接受按标的索引的洗盘/派发/结构破坏判定。
端口把**平台已落库的判定**（`signal_verdicts` 每标的最新一条）喂进去，于是
「组合体检」与「判定历史/兑现」共用同一批结论——不让模型另推一份风险判断。

## 弃权：三种"空"必须分开

1. **没有任何持仓行** → 声明「未获取到持仓」（未导入 / 台账为空）——这是
   `position_context` 一直在守的那条：把"不知道"说成"空仓"是最坏的失败；
2. **有持仓但一条都评估不出来** → 声明「持仓存在但本次无法评估」（行情不可用 /
   全部评估失败），与第 1 种截然不同；
3. 取数抛错 → `degraded=True` + 可读原因，**不外传上游原文**。

## 用户维度

持仓属于某个用户。端口在**闭包里绑定 user_id**，工具参数里没有 user_id。
`api/` 不得被本模块 import（AST 边界测试强制）。
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol

from tradingagents.grounding.types import Coverage, EvidenceItem

from .spec import PLAN_LAYER_DECISION, RESEARCH_DISCLAIMER, ToolResult, ToolSpec

PORTFOLIO_RISK_CONTRACT = "PORTFOLIO_RISK_REVIEW"
_SOURCE_KEY = "portfolio_risk"
MAX_EVIDENCE = 30


class PortfolioRiskPort(Protocol):
    """读取当前用户的组合体检结果（实现由应用层提供，闭包内绑定 user_id）。

    返回：
    `{"items": [HoldingDecision.to_dict(), ...], "summary": {...}, "total": int,
      "position_rows": int}`

    每条形如：`symbol / name / action / priority / reasons / triggers /
    suggested_shares / suggested_pct / price / previous_close / pnl_pct / position /
    available_position / average_cost / constraints / plan_available / plan_direction /
    anchor_gate / is_actionable_sell`。**排序由服务层给出**，工具不得重排。
    """

    def risk_review(self, *, limit: int) -> Mapping[str, Any]: ...


def _fmt(value: Any, *, digits: int = 2, suffix: str = "") -> str:
    if value in (None, ""):
        return ""
    try:
        return f"{float(value):.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return str(value)


def _signed_pct(value: Any) -> str:
    """盈亏方向不能省略：-8% 与 +8% 是相反的结论。"""
    try:
        return f"{float(value):+.2f}%"
    except (TypeError, ValueError):
        return ""


def _summary_text(summary: Mapping[str, Any]) -> str:
    total = summary.get("total")
    parts = [f"共 {int(total)} 只" if total is not None else "共 未提供 只"]
    for key, label in (("清仓", "清仓"), ("减仓", "减仓"), ("持有", "持有"), ("观察", "观察")):
        count = summary.get(key)
        if count:
            parts.append(f"{label} {int(count)}")
    high = summary.get("high_priority")
    if high is not None:
        parts.append(f"高优先级 {int(high)}")
    with_plan = summary.get("with_plan")
    without_plan = summary.get("without_plan")
    if with_plan is not None or without_plan is not None:
        parts.append(f"有计划 {int(with_plan or 0)}/{int((with_plan or 0) + (without_plan or 0))}")
    return "｜".join(parts)


def _decision_parts(row: Mapping[str, Any]) -> list[str]:
    symbol = str(row.get("symbol") or "未提供")
    name = str(row.get("name") or "").strip()
    parts = [f"{symbol}" + (f" {name}" if name and name != symbol else "")]

    action = str(row.get("action") or "未提供")
    priority = str(row.get("priority") or "未提供")
    parts.append(f"处置 {action}（优先级 {priority}）")

    price = _fmt(row.get("price"))
    if price:
        change = _signed_pct(row.get("pnl_pct"))
        pnl_from_cost = _fmt(row.get("average_cost"))
        parts.append(
            f"现价 {price}"
            + (f"（浮亏 {change}）" if change else "")
            + (f"｜成本 {pnl_from_cost}" if pnl_from_cost else "")
        )

    position = _fmt(row.get("position"))
    if position:
        available = _fmt(row.get("available_position"))
        parts.append(f"持仓 {position}" + (f"（可用 {available}）" if available else ""))

    suggested_pct = _fmt(row.get("suggested_pct"), digits=1, suffix="%")
    if suggested_pct and str(row.get("action")) in ("减仓", "清仓"):
        shares = row.get("suggested_shares")
        parts.append(
            f"建议减持 {suggested_pct}"
            + (f"（{int(shares)} 股）" if isinstance(shares, (int, float)) else "")
        )

    triggers = [str(x) for x in (row.get("triggers") or []) if str(x).strip()]
    if triggers:
        parts.append("触发 " + "、".join(triggers[:3]))
    reasons = [str(x) for x in (row.get("reasons") or []) if str(x).strip()]
    if reasons:
        parts.append("依据 " + "、".join(reasons[:2]))

    # 约束必须可引用：把"不可执行"写成"不用执行"是这类工具最危险的失真
    constraints = [str(x) for x in (row.get("constraints") or []) if str(x).strip()]
    if constraints:
        parts.append("约束 " + "、".join(constraints[:3]))
    if row.get("plan_available") is False:
        parts.append("无交易计划（纪律锚点缺失）")
    return parts


def make_risk_tools(risk: PortfolioRiskPort) -> tuple[ToolSpec, ...]:
    def portfolio_risk_review(*, limit: int = 10, as_of: str = "") -> ToolResult:
        # 不能写 `limit or 10`：limit=0 会被 falsy 换成 10（本仓库已多次踩坑）
        requested = 10 if limit is None else int(limit)
        capped = max(1, min(requested, MAX_EVIDENCE))
        try:
            payload = risk.risk_review(limit=capped)
        except Exception as exc:  # noqa: BLE001 — 取不到就如实声明，不抛给模型
            return ToolResult(
                data={"items": [], "error": type(exc).__name__},
                evidence=(),
                coverage=Coverage(available=(), unavailable=(_SOURCE_KEY,)),
                degraded=True,
                degraded_reason=f"组合体检读取失败（{type(exc).__name__}）",
                as_of=as_of,
                disclaimer=RESEARCH_DISCLAIMER,
            )

        if not isinstance(payload, Mapping):
            return ToolResult(
                data={"items": [], "error": "unparsable_payload"},
                evidence=(),
                coverage=Coverage(available=(), unavailable=(_SOURCE_KEY,)),
                degraded=True,
                degraded_reason="组合体检结构无法解析（不是映射）",
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
                degraded_reason="组合体检缺少 items 列表，结构无法解析",
                as_of=as_of,
                disclaimer=RESEARCH_DISCLAIMER,
            )

        position_rows = payload.get("position_rows")
        if not items:
            # 三种"空"必须分开：没有持仓行 ≠ 有持仓但评估不出来
            if position_rows:
                reason = (
                    "portfolio_risk（持仓存在但本次无法评估："
                    "行情不可用或全部标的评估失败）"
                )
            else:
                reason = "portfolio_risk（未获取到持仓：尚未导入持仓或录入台账）"
            return ToolResult(
                data={
                    "items": [],
                    "count": 0,
                    "total": payload.get("total"),
                    "position_rows": position_rows,
                    "summary": payload.get("summary"),
                },
                evidence=(),
                coverage=Coverage(available=(_SOURCE_KEY,), unavailable=(reason,)),
                as_of=as_of,
                disclaimer=RESEARCH_DISCLAIMER,
            )

        rows = [row for row in items if isinstance(row, Mapping)][:capped]
        evidence: list[EvidenceItem] = []
        for index, row in enumerate(rows, start=1):
            evidence.append(
                EvidenceItem(
                    id=f"E{index}",
                    field=f"组合风险{index}",
                    value="｜".join(_decision_parts(row)),
                    unit="",
                    source_key=_SOURCE_KEY,
                    as_of=as_of,
                )
            )
        summary = payload.get("summary")
        if isinstance(summary, Mapping) and summary:
            evidence.append(
                EvidenceItem(
                    id=f"E{len(evidence) + 1}",
                    field="组合体检汇总",
                    value=_summary_text(summary),
                    unit="",
                    source_key=_SOURCE_KEY,
                    as_of=as_of,
                )
            )
        # 排序口径必须可引用：不然"哪只最危险"无法复核
        evidence.append(
            EvidenceItem(
                id=f"E{len(evidence) + 1}",
                field="排序口径",
                value=(
                    "按平台退出引擎的确定性规则排序：优先级（高→中→低）在前，"
                    "同级按浮动盈亏从小到大；处置动作与可执行量均为规则产出，不由模型改写"
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
                "total": payload.get("total"),
                "summary": summary if isinstance(summary, Mapping) else None,
            },
            evidence=tuple(evidence),
            coverage=Coverage(available=(_SOURCE_KEY,), unavailable=()),
            as_of=as_of,
            disclaimer=RESEARCH_DISCLAIMER,
        )

    spec = ToolSpec(
        name="portfolio_risk_review",
        title="组合体检（风险排序）",
        description=(
            "读取当前用户的**组合体检结果**：逐只持仓的处置建议（清仓/减仓/持有/观察）、"
            "优先级、触发条件、依据、建议减持比例与 A 股约束（T+1/整手/涨跌停）、"
            "是否有交易计划锚点，并给出组合级汇总。"
            "用于回答「我持仓里哪只最危险」「我的组合该先处理哪只」。"
            "排序与风险口径由平台确定性退出引擎给出（含已落库的洗盘/派发判定），"
            "**不接受模型自行排序**；未获取到持仓时会显式声明，不会被当成空仓。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": f"最多返回几只，1-{MAX_EVIDENCE}，默认 10"},
                "as_of": {"type": "string", "description": "基准日 YYYY-MM-DD"},
            },
            "required": [],
        },
        output_contract=PORTFOLIO_RISK_CONTRACT,
        handler=portfolio_risk_review,
        cost="cheap",
        plan_layer=PLAN_LAYER_DECISION,
        # 组合体检会取实时行情（走既有 provider 链路），预算给到 12s
        latency_budget_ms=12000,
        freshness_sources=(_SOURCE_KEY,),
    )
    return (spec,)
