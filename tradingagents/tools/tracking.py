"""L1 决策工具：跟踪看板。

## 「跟踪池怎么样」是一个**判断问题**，不是三次取数

用户问这句话时想要的是：我跟踪的这几只现在什么价位、盈亏如何、平台对它们最新
的分析结论是什么。平台的看板服务（`tracking_board_service.get_tracking_board`）
已经把三件事合到了一起：

1. 导入持仓的数量与成本（组合视角，不是行情视角）；
2. 实时行情下的浮动盈亏与当日涨跌；
3. 每只标的**最近一份分析报告**的结论摘要（取自报告服务）。

让模型自己拼这三件事，必然出现"行情是今天的、结论是上周的、成本是导入那天的"
这类时点混用。看板把它们放在同一份快照里，并把基准交易日明确写出来。

## 逐条声明"无分析结论"

跟踪池里的标的不一定都有报告（新导入的、报告已过期的）。**缺结论不等于中性**：
逐条说出来，模型才不会把"没有结论"读成"没有风险"。同理，看板为空时声明
`unavailable`（"还没导入持仓"与"确实空仓"是两件事，本平台对后者尤其敏感）。

## 用户维度

看板属于某个用户。端口在**闭包里绑定 user_id**，工具参数里没有 user_id。
`api/` 不得被本模块 import（AST 边界测试强制）。
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol

from tradingagents.grounding.types import Coverage, EvidenceItem

from .spec import PLAN_LAYER_DECISION, RESEARCH_DISCLAIMER, ToolResult, ToolSpec

TRACKING_BOARD_CONTRACT = "TRACKING_BOARD"
_SOURCE_KEY = "tracking_board"
MAX_EVIDENCE = 30


class TrackingBoardPort(Protocol):
    """读取当前用户的跟踪看板（实现由应用层提供，闭包内绑定 user_id）。

    返回 `tracking_board_service.get_tracking_board` 的结构：
    `{"previous_trade_date": str, "refresh_interval_seconds": int, "items": [...]}`，
    其中每条形如 `symbol / name / current_position / available_position /
    average_cost / market_value / current_position_pct / live_price / floating_pnl /
    floating_pnl_pct / price_change_pct / quote_time / quote_source / analysis`，
    `analysis` 为报告摘要（含 `direction / decision / trade_date /
    trader_advice_summary`）或 `None`。
    """

    def tracking_board(self) -> Mapping[str, Any]: ...


def _fmt(value: Any, *, digits: int = 2, suffix: str = "") -> str:
    if value in (None, ""):
        return ""
    try:
        return f"{float(value):.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return str(value)


def _signal_text(value: Any) -> str:
    """带正负号：盈亏方向不能被省略（-800 与 800 是相反的结论）。"""
    try:
        return f"{float(value):+.2f}"
    except (TypeError, ValueError):
        return ""


def _signal_pct(value: Any) -> str:
    try:
        return f"{float(value):+.2f}%"
    except (TypeError, ValueError):
        return ""


def _analysis_text(analysis: Any) -> str:
    if not isinstance(analysis, Mapping) or not analysis:
        # 缺结论 ≠ 中性：必须显式，不能留空
        return "最新分析 无分析结论"
    direction = str(analysis.get("direction") or "").strip()
    decision = str(analysis.get("decision") or "").strip()
    trade_date = str(analysis.get("trade_date") or "").strip()
    head = direction or decision or "结论未提供"
    if direction and decision:
        head = f"{direction}（{decision}）"
    suffix = f"（{trade_date}）" if trade_date else ""
    summary = str(
        analysis.get("trader_advice_summary") or analysis.get("summary") or ""
    ).strip()
    text = f"最新分析 {head}{suffix}"
    if summary:
        text += f"：{summary[:120]}"
    return text


def _evidence_parts(row: Mapping[str, Any]) -> list[str]:
    symbol = str(row.get("symbol") or "未提供")
    name = str(row.get("name") or "").strip()
    parts = [f"{symbol}" + (f" {name}" if name and name != symbol else "")]

    price = _fmt(row.get("live_price"))
    if price:
        change = _signal_pct(row.get("price_change_pct"))
        parts.append(f"现价 {price}" + (f"（{change}）" if change else ""))

    pnl = _signal_text(row.get("floating_pnl"))
    pnl_pct = _signal_pct(row.get("floating_pnl_pct"))
    if pnl or pnl_pct:
        parts.append("浮动盈亏 " + " / ".join(x for x in (pnl, pnl_pct) if x))

    market_value = _fmt(row.get("market_value"))
    if market_value:
        parts.append(f"市值 {market_value}")
    weight = _fmt(row.get("current_position_pct"), suffix="%")
    if weight:
        parts.append(f"占比 {weight}")

    position = _fmt(row.get("current_position"))
    if position:
        available = _fmt(row.get("available_position"))
        parts.append(f"持仓 {position}" + (f"（可用 {available}）" if available else ""))

    cost = _fmt(row.get("average_cost"))
    if cost:
        parts.append(f"成本 {cost}")

    quote_time = str(row.get("quote_time") or "").strip()
    if quote_time:
        parts.append(f"行情时间 {quote_time}")

    parts.append(_analysis_text(row.get("analysis")))
    return parts


def make_tracking_tools(board: TrackingBoardPort) -> tuple[ToolSpec, ...]:
    def tracking_board_read(*, limit: int = 20, as_of: str = "") -> ToolResult:
        # 不能写 `limit or 20`：limit=0 会被 falsy 换成 20（本仓库已多次踩坑）
        requested = 20 if limit is None else int(limit)
        capped = max(1, min(requested, MAX_EVIDENCE))
        try:
            payload = board.tracking_board()
        except Exception as exc:  # noqa: BLE001 — 取不到就如实声明，不抛给模型
            return ToolResult(
                data={"items": [], "error": type(exc).__name__},
                evidence=(),
                coverage=Coverage(available=(), unavailable=(_SOURCE_KEY,)),
                degraded=True,
                degraded_reason=f"跟踪看板读取失败（{type(exc).__name__}）",
                as_of=as_of,
                disclaimer=RESEARCH_DISCLAIMER,
            )

        if not isinstance(payload, Mapping):
            return ToolResult(
                data={"items": [], "error": "unparsable_payload"},
                evidence=(),
                coverage=Coverage(available=(), unavailable=(_SOURCE_KEY,)),
                degraded=True,
                degraded_reason="跟踪看板结构无法解析（不是映射）",
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
                degraded_reason="跟踪看板缺少 items 列表，结构无法解析",
                as_of=as_of,
                disclaimer=RESEARCH_DISCLAIMER,
            )

        previous_trade_date = str(payload.get("previous_trade_date") or "")
        if not items:
            # 「解析不到持仓」必须与「空仓」区分：这里声明未覆盖，而不是"你没有持仓"
            return ToolResult(
                data={"items": [], "count": 0, "previous_trade_date": previous_trade_date},
                evidence=(),
                coverage=Coverage(
                    available=(_SOURCE_KEY,),
                    unavailable=("tracking_board（未获取到跟踪标的：尚未导入持仓或跟踪池为空）",),
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
                    field=f"跟踪标的{index}",
                    value="｜".join(_evidence_parts(row)),
                    unit="",
                    source_key=_SOURCE_KEY,
                    as_of=str(row.get("quote_time") or previous_trade_date or as_of)[:10],
                )
            )

        return ToolResult(
            data={
                "items": [dict(row) for row in rows],
                "count": len(rows),
                "previous_trade_date": previous_trade_date,
                "refresh_interval_seconds": payload.get("refresh_interval_seconds"),
            },
            evidence=tuple(evidence),
            coverage=Coverage(available=(_SOURCE_KEY,), unavailable=()),
            as_of=as_of,
            disclaimer=RESEARCH_DISCLAIMER,
        )

    spec = ToolSpec(
        name="tracking_board_read",
        title="跟踪看板（持仓 + 行情 + 最新分析）",
        description=(
            "读取当前用户的**跟踪看板**：每只跟踪标的的持仓数量与成本、实时价与"
            "浮动盈亏、当日涨跌、以及平台最近一份分析报告的结论摘要。"
            "用于回答「我的跟踪池现在怎么样」「这些标的平台怎么看」。"
            "未获取到跟踪标的时会显式声明，不会被当成空仓；某只标的没有分析结论时"
            "会逐条说明「无分析结论」，不会被读成中性。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": f"最多返回几只，1-{MAX_EVIDENCE}，默认 20"},
                "as_of": {"type": "string", "description": "基准日 YYYY-MM-DD"},
            },
            "required": [],
        },
        output_contract=TRACKING_BOARD_CONTRACT,
        handler=tracking_board_read,
        cost="cheap",
        plan_layer=PLAN_LAYER_DECISION,
        latency_budget_ms=10000,
        freshness_sources=(_SOURCE_KEY,),
    )
    return (spec,)
