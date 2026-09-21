"""L1 决策工具：模拟盘（paper portfolio）当前状态。

## 「空仓」是事实，「读不到」才是未覆盖

模拟盘与真实持仓是两套账：模拟盘用于在**不动真钱**的前提下检验策略。
用户问「模拟盘现在怎么样」时，平台能给出初始资金、现金、持仓市值、总权益与
总收益——这些是可确证的事实。

因此这里与 `portfolio_holdings_read`（真实持仓，读不到时必须声明未覆盖）有一处
**刻意不同**：模拟盘的空仓是服务层明确告诉我们的状态（`positions` 为空列表），
那是**可回答的事实**，要作为一条证据说出来，而不是留空或标成"未覆盖"。
把它声明成未覆盖会在回答里凭空多出一条噪音，而用户真正需要知道的是
"模拟盘现在是空仓"。

## 弃权与降级

* 结构解析不出来 → `degraded` + 说明（与"空仓"走不同分支）；
* 取数失败 → `degraded=True`，**不外传上游原文**；
* 上限 clamp 且不写 `x or default`（`limit=0` 会被 falsy 换掉）；
* 用户维度在端口闭包里绑定，工具参数里没有 user_id。
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol

from tradingagents.grounding.types import Coverage, EvidenceItem

from .spec import PLAN_LAYER_DECISION, RESEARCH_DISCLAIMER, ToolResult, ToolSpec

PAPER_PORTFOLIO_CONTRACT = "PAPER_PORTFOLIO"
_SOURCE_KEY = "paper_portfolio"
MAX_EVIDENCE = 30


class PaperPortfolioPort(Protocol):
    """读取当前用户的模拟盘快照（实现由应用层提供，闭包内绑定 user_id）。

    返回 `paper_trading_service.get_portfolio_snapshot` 的结构：
    `{"portfolio_id", "cash_balance", "initial_cash", "market_value", "equity",
    "total_return_pct", "positions": [{"symbol", "name", "quantity", "avg_cost",
    "last_price"}, ...]}`。
    """

    def portfolio_snapshot(self) -> Mapping[str, Any]: ...


def _fmt(value: Any, *, digits: int = 2, suffix: str = "") -> str:
    if value in (None, ""):
        return ""
    try:
        return f"{float(value):.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return str(value)


def _signed_pct(value: Any) -> str:
    try:
        return f"{float(value):+.2f}%"
    except (TypeError, ValueError):
        return ""


def make_paper_tools(paper: PaperPortfolioPort) -> tuple[ToolSpec, ...]:
    def paper_portfolio_read(*, limit: int = 20, as_of: str = "") -> ToolResult:
        # 不能写 `limit or 20`：limit=0 会被 falsy 换成 20（本仓库已多次踩坑）
        requested = 20 if limit is None else int(limit)
        capped = max(1, min(requested, MAX_EVIDENCE))
        try:
            payload = paper.portfolio_snapshot()
        except Exception as exc:  # noqa: BLE001 — 取不到就如实声明，不抛给模型
            return ToolResult(
                data={"positions": [], "error": type(exc).__name__},
                evidence=(),
                coverage=Coverage(available=(), unavailable=(_SOURCE_KEY,)),
                degraded=True,
                degraded_reason=f"模拟盘读取失败（{type(exc).__name__}）",
                as_of=as_of,
                disclaimer=RESEARCH_DISCLAIMER,
            )

        if not isinstance(payload, Mapping):
            return ToolResult(
                data={"positions": [], "error": "unparsable_payload"},
                evidence=(),
                coverage=Coverage(available=(), unavailable=(_SOURCE_KEY,)),
                degraded=True,
                degraded_reason="模拟盘结构无法解析（不是映射）",
                as_of=as_of,
                disclaimer=RESEARCH_DISCLAIMER,
            )

        positions = payload.get("positions")
        if not isinstance(positions, (list, tuple)):
            return ToolResult(
                data={"positions": [], "error": "missing_positions"},
                evidence=(),
                coverage=Coverage(available=(), unavailable=(_SOURCE_KEY,)),
                degraded=True,
                degraded_reason="模拟盘缺少 positions 列表，结构无法解析",
                as_of=as_of,
                disclaimer=RESEARCH_DISCLAIMER,
            )

        overview_parts = []
        initial_cash = _fmt(payload.get("initial_cash"))
        if initial_cash:
            overview_parts.append(f"初始资金 {initial_cash}")
        cash = _fmt(payload.get("cash_balance"))
        if cash:
            overview_parts.append(f"现金 {cash}")
        market_value = _fmt(payload.get("market_value"))
        if market_value:
            overview_parts.append(f"持仓市值 {market_value}")
        equity = _fmt(payload.get("equity"))
        if equity:
            overview_parts.append(f"总权益 {equity}")
        total_return = _signed_pct(payload.get("total_return_pct"))
        if total_return:
            overview_parts.append(f"总收益 {total_return}")

        evidence: list[EvidenceItem] = []
        if overview_parts:
            evidence.append(
                EvidenceItem(
                    id="E1",
                    field="模拟盘总览",
                    value="｜".join(overview_parts),
                    unit="",
                    source_key=_SOURCE_KEY,
                    as_of=as_of,
                )
            )

        rows = [row for row in positions if isinstance(row, Mapping)][:capped]
        for index, row in enumerate(rows, start=1):
            symbol = str(row.get("symbol") or "未提供")
            name = str(row.get("name") or "").strip()
            parts = [f"{symbol}" + (f" {name}" if name and name != symbol else "")]
            quantity = _fmt(row.get("quantity"))
            if quantity:
                parts.append(f"数量 {quantity}")
            cost = _fmt(row.get("avg_cost"))
            if cost:
                parts.append(f"成本 {cost}")
            last = _fmt(row.get("last_price"))
            if last:
                parts.append(f"现价 {last}")
            evidence.append(
                EvidenceItem(
                    id=f"E{len(evidence) + 1}",
                    field=f"模拟盘持仓{index}",
                    value="｜".join(parts),
                    unit="",
                    source_key=_SOURCE_KEY,
                    as_of=as_of,
                )
            )

        if not rows:
            # 空仓是**可确证的事实**（服务明确返回空列表），不是"未覆盖"：
            # 把它标成未覆盖会在回答里多出一条噪音，而用户需要知道的是"现在是空仓"
            evidence.append(
                EvidenceItem(
                    id=f"E{len(evidence) + 1}",
                    field="模拟盘持仓",
                    value="当前无持仓（模拟盘空仓）",
                    unit="",
                    source_key=_SOURCE_KEY,
                    as_of=as_of,
                )
            )

        if not evidence:
            return ToolResult(
                data={"positions": [], "error": "unparsable_payload"},
                evidence=(),
                coverage=Coverage(available=(), unavailable=(_SOURCE_KEY,)),
                degraded=True,
                degraded_reason="模拟盘结构无法解析（无可引用字段）",
                as_of=as_of,
                disclaimer=RESEARCH_DISCLAIMER,
            )

        return ToolResult(
            data={
                "positions": [dict(row) for row in rows],
                "count": len(rows),
                "equity": payload.get("equity"),
                "cash_balance": payload.get("cash_balance"),
                "market_value": payload.get("market_value"),
                "total_return_pct": payload.get("total_return_pct"),
            },
            evidence=tuple(evidence),
            coverage=Coverage(available=(_SOURCE_KEY,), unavailable=()),
            as_of=as_of,
            disclaimer=RESEARCH_DISCLAIMER,
        )

    spec = ToolSpec(
        name="paper_portfolio_read",
        title="模拟盘持仓与收益",
        description=(
            "读取当前用户的**模拟盘**状态：初始资金、现金、持仓市值、总权益、总收益，"
            "以及逐只模拟持仓的数量/成本/现价。用于回答「模拟盘现在怎么样」"
            "「不动真钱试的策略跑成什么样」。"
            "空仓会被如实说成「空仓」（那是可确证的事实）；读不到时才声明未覆盖。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": f"最多返回几只持仓，1-{MAX_EVIDENCE}，默认 20"},
                "as_of": {"type": "string", "description": "基准日 YYYY-MM-DD"},
            },
            "required": [],
        },
        output_contract=PAPER_PORTFOLIO_CONTRACT,
        handler=paper_portfolio_read,
        cost="cheap",
        plan_layer=PLAN_LAYER_DECISION,
        # 会取实时行情算市值（走既有 provider 链路）
        latency_budget_ms=12000,
        freshness_sources=(_SOURCE_KEY,),
    )
    return (spec,)
