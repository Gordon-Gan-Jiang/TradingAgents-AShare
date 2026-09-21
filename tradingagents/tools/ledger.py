"""L1 决策工具：交易流水的盈亏归因。

## 「我这段时间做得怎么样」是一个归因问题，不是一个取数问题

平台把台账流水重放成**已实现盈亏**，并按标的与**卖出原因**聚合
（`trade_ledger_service.attribution_summary`）。这一步不是简单相加：买卖配对、
费用与印花税、摊薄成本都在服务层完成。让模型自己"看一下流水算算赚了多少"，
既算不准，也无法解释口径。

## 归因覆盖率必须跟着结论一起说

卖出流水可能**没有标注原因**（历史导入、截图识别缺字段）。服务层给出
`attribution_coverage` 与 `missing_sell_reason_count`。覆盖率不足时，
「按原因归因」的结论只能覆盖一部分流水——因此本工具在覆盖率 < 100% 时
**显式声明未覆盖**（`unavailable`），而不是把一个不完整的归因表当成全部。

## 弃权与降级

* 没有任何流水 → `unavailable`（不是"盈亏 0"）；
* 结构解析不出来 → `degraded`，与"确实没有流水"走不同分支；
* 取数失败 → `degraded=True`，**不外传上游原文**；
* `limit` clamp 且不写 `x or default`；用户维度在端口闭包里绑定。
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence

from tradingagents.grounding.types import Coverage, EvidenceItem

from .spec import PLAN_LAYER_DECISION, RESEARCH_DISCLAIMER, ToolResult, ToolSpec

TRADE_LEDGER_CONTRACT = "TRADE_LEDGER"
_SOURCE_KEY = "trade_ledger"
MAX_EVIDENCE = 20


class TradeLedgerPort(Protocol):
    """读取当前用户的交易流水与归因（实现由应用层提供，闭包内绑定 user_id）。

    返回：
    `{"summary": attribution_summary(...), "trades": [ ...最近流水... ]}`

    `summary` 含 `total_realized_pnl / trade_count / buy_count / sell_count /
    win_count / loss_count / win_rate / average_win / average_loss /
    attribution_coverage / missing_sell_reason_count / by_symbol /
    by_sell_reason / symbol_count`；
    流水行含 `symbol / name / action / trade_date / price / shares / amount /
    realized_pnl / sell_reason`。
    """

    def ledger(
        self, *, limit: int, symbol: str | None = None
    ) -> Mapping[str, Any]: ...


def _fmt(value: Any, *, digits: int = 2, suffix: str = "") -> str:
    if value in (None, ""):
        return ""
    try:
        return f"{float(value):.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return str(value)


def _signed(value: Any) -> str:
    try:
        return f"{float(value):+.2f}"
    except (TypeError, ValueError):
        return ""


def _summary_parts(summary: Mapping[str, Any]) -> list[str]:
    parts = []
    total = _signed(summary.get("total_realized_pnl"))
    if total:
        parts.append(f"总实现盈亏 {total}")
    trade_count = summary.get("trade_count")
    if trade_count not in (None, ""):
        parts.append(
            f"交易 {int(trade_count)} 笔"
            f"（买 {int(summary.get('buy_count') or 0)}／卖 {int(summary.get('sell_count') or 0)}）"
        )
    win_count = summary.get("win_count")
    loss_count = summary.get("loss_count")
    if win_count not in (None, "") or loss_count not in (None, ""):
        parts.append(f"盈利 {int(win_count or 0)}／亏损 {int(loss_count or 0)}")
    win_rate = _fmt(summary.get("win_rate"), suffix="%")
    if win_rate:
        parts.append(f"胜率 {win_rate}")
    average_win = _signed(summary.get("average_win"))
    if average_win:
        parts.append(f"平均盈利 {average_win}")
    average_loss = _signed(summary.get("average_loss"))
    if average_loss:
        parts.append(f"平均亏损 {average_loss}")
    coverage = _fmt(summary.get("attribution_coverage"), digits=4)
    if coverage:
        parts.append(f"归因覆盖率 {float(coverage) * 100:.2f}%")
    symbol_count = summary.get("symbol_count")
    if symbol_count not in (None, ""):
        parts.append(f"涉及标的 {int(symbol_count)}")
    return parts


def make_ledger_tools(ledger: TradeLedgerPort) -> tuple[ToolSpec, ...]:
    def trade_ledger_read(
        *, limit: int = 10, symbol: str | None = None, as_of: str = ""
    ) -> ToolResult:
        # 不能写 `limit or 10`：limit=0 会被 falsy 换成 10（本仓库已多次踩坑）
        requested = 10 if limit is None else int(limit)
        capped = max(1, min(requested, MAX_EVIDENCE))
        try:
            payload = ledger.ledger(limit=capped, symbol=symbol)
        except Exception as exc:  # noqa: BLE001 — 取不到就如实声明，不抛给模型
            return ToolResult(
                data={"trades": [], "error": type(exc).__name__},
                evidence=(),
                coverage=Coverage(available=(), unavailable=(_SOURCE_KEY,)),
                degraded=True,
                degraded_reason=f"交易流水读取失败（{type(exc).__name__}）",
                as_of=as_of,
                disclaimer=RESEARCH_DISCLAIMER,
            )

        if not isinstance(payload, Mapping):
            return ToolResult(
                data={"trades": [], "error": "unparsable_payload"},
                evidence=(),
                coverage=Coverage(available=(), unavailable=(_SOURCE_KEY,)),
                degraded=True,
                degraded_reason="交易流水结构无法解析（不是映射）",
                as_of=as_of,
                disclaimer=RESEARCH_DISCLAIMER,
            )

        summary = payload.get("summary")
        if not isinstance(summary, Mapping):
            return ToolResult(
                data={"trades": [], "error": "missing_summary"},
                evidence=(),
                coverage=Coverage(available=(), unavailable=(_SOURCE_KEY,)),
                degraded=True,
                degraded_reason="交易流水缺少归因汇总，结构无法解析",
                as_of=as_of,
                disclaimer=RESEARCH_DISCLAIMER,
            )

        trade_count = summary.get("trade_count") or 0
        if not trade_count:
            return ToolResult(
                data={"trades": [], "count": 0, "summary": dict(summary)},
                evidence=(),
                coverage=Coverage(
                    available=(_SOURCE_KEY,),
                    unavailable=("trade_ledger（没有已录入的交易流水）",),
                ),
                as_of=as_of,
                disclaimer=RESEARCH_DISCLAIMER,
            )

        evidence: list[EvidenceItem] = [
            EvidenceItem(
                id="E1",
                field="盈亏归因汇总",
                value="｜".join(_summary_parts(summary)),
                unit="",
                source_key=_SOURCE_KEY,
                as_of=as_of,
            )
        ]

        by_symbol = summary.get("by_symbol") or []
        if isinstance(by_symbol, (list, tuple)):
            for row in list(by_symbol)[:capped]:
                if not isinstance(row, Mapping):
                    continue
                symbol_text = str(row.get("symbol") or "未提供")
                name = str(row.get("name") or "").strip()
                parts = [f"{symbol_text}" + (f" {name}" if name and name != symbol_text else "")]
                realized = _signed(row.get("realized_pnl"))
                if realized:
                    parts.append(f"实现盈亏 {realized}")
                trades = row.get("trade_count")
                if trades not in (None, ""):
                    parts.append(
                        f"交易 {int(trades)} 笔"
                        f"（买 {int(row.get('buy_count') or 0)}／卖 {int(row.get('sell_count') or 0)}）"
                    )
                bought = _fmt(row.get("bought_amount"))
                sold = _fmt(row.get("sold_amount"))
                if bought or sold:
                    parts.append(f"买入额 {bought or '—'}／卖出额 {sold or '—'}")
                evidence.append(
                    EvidenceItem(
                        id=f"E{len(evidence) + 1}",
                        field=f"标的归因{len(evidence)}",
                        value="｜".join(parts),
                        unit="",
                        source_key=_SOURCE_KEY,
                        as_of=as_of,
                    )
                )

        by_reason = summary.get("by_sell_reason") or []
        if isinstance(by_reason, (list, tuple)) and by_reason:
            reason_parts = []
            for row in list(by_reason)[:6]:
                if not isinstance(row, Mapping):
                    continue
                reason = str(row.get("sell_reason") or "未归因")
                realized = _signed(row.get("realized_pnl"))
                count = row.get("trade_count")
                reason_parts.append(
                    f"{reason} {realized}"
                    + (f"（{int(count)} 笔）" if count not in (None, "") else "")
                )
            if reason_parts:
                evidence.append(
                    EvidenceItem(
                        id=f"E{len(evidence) + 1}",
                        field="按卖出原因归因",
                        value="、".join(reason_parts),
                        unit="",
                        source_key=_SOURCE_KEY,
                        as_of=as_of,
                    )
                )

        trades = payload.get("trades")
        if isinstance(trades, (list, tuple)):
            for row in list(trades)[:capped]:
                if not isinstance(row, Mapping):
                    continue
                symbol_text = str(row.get("symbol") or "未提供")
                action = str(row.get("action") or "未提供")
                parts = [
                    f"{row.get('trade_date') or '未提供'} {action} {symbol_text}",
                    f"{_fmt(row.get('shares')) or '—'} 股 @ {_fmt(row.get('price')) or '—'}",
                ]
                amount = _fmt(row.get("amount"))
                if amount:
                    parts.append(f"成交额 {amount}")
                realized = _signed(row.get("realized_pnl"))
                if realized:
                    parts.append(f"实现盈亏 {realized}")
                reason = str(row.get("sell_reason") or "").strip()
                if reason:
                    parts.append(f"卖出原因 {reason}")
                evidence.append(
                    EvidenceItem(
                        id=f"E{len(evidence) + 1}",
                        field=f"最近流水{len(evidence)}",
                        value="｜".join(parts),
                        unit="",
                        source_key=_SOURCE_KEY,
                        as_of=str(row.get("trade_date") or as_of),
                    )
                )

        # 归因覆盖率不足时**必须声明未覆盖**：不完整的归因表不能当全部
        missing = summary.get("missing_sell_reason_count") or 0
        coverage_value = summary.get("attribution_coverage")
        unavailable: tuple[str, ...] = ()
        try:
            coverage_ratio = float(coverage_value) if coverage_value is not None else 1.0
        except (TypeError, ValueError):
            coverage_ratio = 1.0
        if missing and coverage_ratio < 1.0:
            unavailable = (
                f"sell_reason（{int(missing)} 笔卖出未标注原因，"
                f"归因覆盖率 {coverage_ratio * 100:.2f}%）",
            )

        return ToolResult(
            data={
                "summary": dict(summary),
                "trades": [dict(row) for row in (trades or [])[:capped] if isinstance(row, Mapping)],
                "count": len(trades or []),
            },
            evidence=tuple(evidence),
            coverage=Coverage(available=(_SOURCE_KEY,), unavailable=unavailable),
            as_of=as_of,
            disclaimer=RESEARCH_DISCLAIMER,
        )

    spec = ToolSpec(
        name="trade_ledger_read",
        title="交易流水与盈亏归因",
        description=(
            "读取当前用户的**交易台账**：总实现盈亏、买卖笔数、胜率、平均盈亏、"
            "归因覆盖率，按标的与**卖出原因**的归因明细，以及最近若干条流水。"
            "用于回答「我这段时间做得怎么样」「为什么亏/赚」「卖出原因分布」。"
            "盈亏口径由平台台账重放（含费用、印花税、摊薄成本）给出，不由模型自算；"
            "卖出原因覆盖率不足时会显式声明未覆盖；没有流水时会显式声明。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": f"每类最多返回几条，1-{MAX_EVIDENCE}，默认 10"},
                "symbol": {"type": "string", "description": "只看某只标的（可选）"},
                "as_of": {"type": "string", "description": "基准日 YYYY-MM-DD"},
            },
            "required": [],
        },
        output_contract=TRADE_LEDGER_CONTRACT,
        handler=trade_ledger_read,
        cost="cheap",
        plan_layer=PLAN_LAYER_DECISION,
        latency_budget_ms=8000,
        freshness_sources=(_SOURCE_KEY,),
    )
    return (spec,)
