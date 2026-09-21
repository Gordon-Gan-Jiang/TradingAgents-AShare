"""L1 决策工具：读取当前用户的持仓清单。

## 定位：**只读事实**，不做风险判断

「我持仓里哪只最危险」需要两步：先拿到持仓清单，再逐只判定。风险判断由确定性
信号层（`signal_washout_probe_batch`）负责，本工具只提供**清单**——把清单和判断
分成两个工具，是为了让规划层能组合它们，且让"读了什么"与"判了什么"各自可追溯。

## 为什么 user_id 不经模型

持仓属于某个用户。端口在**闭包里绑定 user_id**，工具参数里没有 user_id——
让模型有机会传 user_id 就等于给它越权的可能。这与 `verdict_history_read` 同一条纪律。

## 为什么"没有持仓"要显式声明

空持仓与"我不知道你有没有持仓"是两件完全不同的事：
* 真的空仓 → 回答"你当前没有持仓"；
* 解析不到（未导入/未记账）→ 必须声明"未获取到持仓"，否则模型会按"空仓"给建议，
  那是本平台明令禁止的失败形态（`position` 一直在覆盖声明里就是这个原因）。
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence

from tradingagents.grounding.types import Coverage, EvidenceItem

from .spec import PLAN_LAYER_DECISION, RESEARCH_DISCLAIMER, ToolResult, ToolSpec

HOLDINGS_CONTRACT = "PORTFOLIO_HOLDINGS"
_SOURCE_KEY = "portfolio_holdings"
MAX_EVIDENCE = 30


class HoldingsPort(Protocol):
    """读取当前用户持仓。实现由应用层提供（闭包内绑定 user_id）。"""

    def list_holdings(self, *, limit: int) -> Sequence[Mapping[str, Any]]: ...


def _fmt(value: Any, *, digits: int = 2, suffix: str = "") -> str:
    if value in (None, ""):
        return "未提供"
    try:
        return f"{float(value):.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return str(value)


def make_portfolio_tools(holdings: HoldingsPort) -> tuple[ToolSpec, ...]:
    def portfolio_holdings_read(*, limit: int = 20, as_of: str = "") -> ToolResult:
        requested = 20 if limit is None else int(limit)
        capped = max(1, min(requested, MAX_EVIDENCE))
        try:
            rows = list(holdings.list_holdings(limit=capped) or [])
        except Exception as exc:  # noqa: BLE001 — 取不到就声明，不抛给模型
            return ToolResult(
                data={"holdings": [], "error": type(exc).__name__},
                evidence=(),
                coverage=Coverage(available=(), unavailable=("portfolio_holdings",)),
                degraded=True,
                degraded_reason=f"持仓读取失败（{type(exc).__name__}）",
                as_of=as_of,
                disclaimer=RESEARCH_DISCLAIMER,
            )

        if not rows:
            # 「解析不到持仓」必须与「空仓」区分：这里声明未覆盖，而不是"你空仓"
            return ToolResult(
                data={"holdings": [], "count": 0},
                evidence=(),
                coverage=Coverage(
                    available=("portfolio_holdings",), unavailable=("portfolio_holdings（未获取到持仓）",)
                ),
                as_of=as_of,
                disclaimer=RESEARCH_DISCLAIMER,
            )

        evidence: list[EvidenceItem] = []
        for index, row in enumerate(rows[:capped], start=1):
            parts = [
                f"{row.get('symbol')} {row.get('security_name') or ''}".strip(),
                f"持仓 {_fmt(row.get('current_position'))}",
                f"可用 {_fmt(row.get('available_position'))}",
                f"成本 {_fmt(row.get('average_cost'))}",
                f"市值 {_fmt(row.get('market_value'))}",
                f"占比 {_fmt(row.get('current_position_pct'), suffix='%')}",
            ]
            evidence.append(
                EvidenceItem(
                    id=f"E{index}",
                    field=f"持仓{index}",
                    value="｜".join(parts),
                    unit="",
                    source_key=_SOURCE_KEY,
                    as_of=as_of,
                )
            )

        return ToolResult(
            data={"holdings": [dict(row) for row in rows[:capped]], "count": len(rows)},
            evidence=tuple(evidence),
            coverage=Coverage(available=(_SOURCE_KEY,), unavailable=()),
            as_of=as_of,
            disclaimer=RESEARCH_DISCLAIMER,
        )

    spec = ToolSpec(
        name="portfolio_holdings_read",
        title="当前持仓清单",
        description=(
            "读取当前用户的持仓清单（代码、名称、数量、可用数量、成本、市值、占比）。"
            "用于回答「我持仓里哪只最危险」「我的仓位情况」这类**组合级**问题；"
            "风险判断请配合 signal_washout_probe_batch 使用。"
            "未获取到持仓时会显式声明，不会被当成空仓。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": f"最多返回多少只，1-{MAX_EVIDENCE}，默认 20"},
                "as_of": {"type": "string", "description": "基准日 YYYY-MM-DD"},
            },
            "required": [],
        },
        output_contract=HOLDINGS_CONTRACT,
        handler=portfolio_holdings_read,
        cost="cheap",
        plan_layer=PLAN_LAYER_DECISION,
        latency_budget_ms=8000,
        freshness_sources=(_SOURCE_KEY,),
    )
    return (spec,)


# ── 市场主线（L1 决策）────────────────────────────────────────────────────
#
# 「最近哪个板块最强」「当前主线是什么」——答案来自平台的**主线判定**（定时任务
# 产出、全用户只读共享）。Agent 的职责是读取与解释，而不是临场推一个主线出来：
# 主线是平台的模型产出，模型改写它等于把"平台结论"和"模型即兴"混为一谈。


class MainlinePort(Protocol):
    def list_runs(self, *, limit: int) -> Sequence[Mapping[str, Any]]: ...


def make_mainline_tools(mainline: MainlinePort) -> tuple[ToolSpec, ...]:
    def market_mainline_view(*, limit: int = 3, as_of: str = "") -> ToolResult:
        requested = 3 if limit is None else int(limit)
        capped = max(1, min(requested, 10))
        try:
            rows = list(mainline.list_runs(limit=capped) or [])
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                data={"runs": [], "error": type(exc).__name__},
                evidence=(),
                coverage=Coverage(available=(), unavailable=("market_mainline",)),
                degraded=True,
                degraded_reason=f"主线读取失败（{type(exc).__name__}）",
                as_of=as_of,
                disclaimer=RESEARCH_DISCLAIMER,
            )

        if not rows:
            return ToolResult(
                data={"runs": [], "count": 0},
                evidence=(),
                coverage=Coverage(
                    available=("market_mainline",),
                    unavailable=("market_mainline（尚未生成主线报告）",),
                ),
                as_of=as_of,
                disclaimer=RESEARCH_DISCLAIMER,
            )

        evidence: list[EvidenceItem] = []
        for index, row in enumerate(rows, start=1):
            summary = row.get("summary") or row.get("headline") or row.get("title") or ""
            parts = [
                f"{row.get('trade_date') or row.get('as_of') or '未提供'}",
                f"状态 {row.get('status') or '未提供'}",
            ]
            if summary:
                parts.append(str(summary)[:200])
            # 主线的核心是**板块强弱排序**：把前几名逐条列出，供逐一引用
            boards = row.get("boards") or row.get("sectors") or []
            if isinstance(boards, (list, tuple)) and boards:
                ranked = []
                for board in list(boards)[:5]:
                    if isinstance(board, Mapping):
                        ranked.append(
                            f"{board.get('name') or board.get('board') or '?'}"
                            f"（{_fmt(board.get('change_pct'), suffix='%')}）"
                        )
                    else:
                        ranked.append(str(board))
                parts.append("板块强弱：" + "、".join(ranked))
            evidence.append(
                EvidenceItem(
                    id=f"E{index}",
                    field=f"主线报告{index}",
                    value="｜".join(parts),
                    unit="",
                    source_key="market_mainline",
                    as_of=str(row.get("trade_date") or as_of),
                )
            )

        return ToolResult(
            data={"runs": [dict(row) for row in rows], "count": len(rows)},
            evidence=tuple(evidence),
            coverage=Coverage(available=("market_mainline",), unavailable=()),
            as_of=as_of,
            disclaimer=RESEARCH_DISCLAIMER,
        )

    spec = ToolSpec(
        name="market_mainline_view",
        title="市场主线（板块强弱）",
        description=(
            "读取平台生成的**市场主线报告**（板块强弱与主线判断，定时任务产出）。"
            "用于回答「最近哪个板块最强」「当前市场主线是什么」。"
            "结论来自平台的主线判定，不接受模型改写；没有报告时会显式声明。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "返回最近几份，1-10，默认 3"},
                "as_of": {"type": "string", "description": "基准日 YYYY-MM-DD"},
            },
            "required": [],
        },
        output_contract="MARKET_MAINLINE",
        handler=market_mainline_view,
        cost="cheap",
        plan_layer=PLAN_LAYER_DECISION,
        latency_budget_ms=8000,
        freshness_sources=("market_mainline",),
    )
    return (spec,)
