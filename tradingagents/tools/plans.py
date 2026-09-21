"""L1 决策工具：当前有效的交易计划（纪律锚点）。

## 为什么这是 L1 而不是"读一条记录"

问「我的交易计划是什么」「这只有没有止损」问的是**平台给出的纪律约束**：
入场区间、硬止损、止盈分档、移动止盈回撤、时间止损、失效条件、仓位上限。
这些是判定"该不该动手"的**锚点**，也是组合体检里"无交易计划（纪律锚点缺失）"
那句话的对立面——没有计划，退出引擎只能给"观察"。

交易计划由平台的报告管线解析落库（`trade_plan_service`），模型只能引用。
若让模型"记一个止损价"，同一只标的两次提问会得到两个止损——这正是本平台
反复强调不能接受的失真。

## 只读"当前有效且可监控"的计划

端口走 `trade_plan_service.list_active_plans`：它已经过滤掉非 active 状态与
**没有锚点**的计划（`_row_has_anchors`）。工具不再自己筛一遍——两处口径迟早分叉。

## 弃权与降级

* 没有有效计划 → `unavailable`（不是空列表当成功：用户会以为"我没设过计划"）；
* 结构解析不出来 → `degraded` 并说明「解析不到」，与「确实没有」走不同分支；
* 取数失败 → `degraded=True`，**不外传上游原文**；
* 用户维度在端口闭包里绑定，工具参数里没有 user_id。
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence

from tradingagents.grounding.types import Coverage, EvidenceItem

from .spec import PLAN_LAYER_DECISION, RESEARCH_DISCLAIMER, ToolResult, ToolSpec

TRADE_PLAN_CONTRACT = "TRADE_PLAN"
_SOURCE_KEY = "trade_plan"
MAX_EVIDENCE = 20


class TradePlanPort(Protocol):
    """读取当前用户当前有效的交易计划（实现由应用层提供，闭包内绑定 user_id）。

    每条形如 `trade_plan_service.plan_dict_from_row` 的输出：
    `symbol / name / direction / horizon / horizon_days / entry_low / entry_high /
    position_cap_pct / first_tranche_pct / hard_stop_price / take_profit_ladder /
    target_price / trailing_stop_pct / time_stop_days / invalidation_conditions /
    de_risk_triggers / risk_gate / confidence / status / source / signal_trade_date`。
    """

    def active_plans(
        self, *, limit: int, symbol: str | None = None
    ) -> Sequence[Mapping[str, Any]]: ...


def _fmt(value: Any, *, digits: int = 2, suffix: str = "") -> str:
    if value in (None, ""):
        return ""
    try:
        return f"{float(value):.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return str(value)


def _ladder_text(ladder: Any) -> str:
    """止盈分档：`[[price, reduce_pct], ...]` → 「1800.00(-30%)」可引用文本。"""
    if not isinstance(ladder, (list, tuple)) or not ladder:
        return ""
    parts = []
    for rung in ladder[:4]:
        if isinstance(rung, (list, tuple)) and len(rung) >= 2:
            price = _fmt(rung[0])
            reduce_pct = _fmt(rung[1], digits=0)
            if price:
                parts.append(f"{price}(-{reduce_pct}%)" if reduce_pct else price)
        else:
            text = _fmt(rung)
            if text:
                parts.append(text)
    return "、".join(parts)


def _plan_parts(row: Mapping[str, Any]) -> list[str]:
    symbol = str(row.get("symbol") or "未提供")
    name = str(row.get("name") or "").strip()
    parts = [f"{symbol}" + (f" {name}" if name and name != symbol else "")]
    # 计划是哪天的必须可引用：锚点会随新报告更新，不说日期就等于不知道拿的是哪一版
    signal_date = str(row.get("signal_trade_date") or "").strip()
    if signal_date:
        parts.append(f"信号日 {signal_date}")

    direction = str(row.get("direction") or "").strip()
    horizon = str(row.get("horizon") or "").strip()
    horizon_days = row.get("horizon_days")
    head = f"方向 {direction or '未提供'}"
    if horizon or horizon_days not in (None, ""):
        head += f"｜周期 {horizon or '未提供'}"
        if horizon_days not in (None, ""):
            head += f"（{int(horizon_days)} 日）"
    parts.append(head)

    entry_low = _fmt(row.get("entry_low"))
    entry_high = _fmt(row.get("entry_high"))
    if entry_low or entry_high:
        parts.append(f"入场 {entry_low or '—'}–{entry_high or '—'}")

    # 止损/止盈是纪律锚点本体，必须逐项可引用
    stop = _fmt(row.get("hard_stop_price"))
    if stop:
        parts.append(f"硬止损 {stop}")
    ladder = _ladder_text(row.get("take_profit_ladder"))
    if ladder:
        parts.append(f"止盈分档 {ladder}")
    target = _fmt(row.get("target_price"))
    if target:
        parts.append(f"目标价 {target}")
    trailing = _fmt(row.get("trailing_stop_pct"), suffix="%")
    if trailing:
        parts.append(f"移动止盈回撤 {trailing}")
    time_stop = row.get("time_stop_days")
    if time_stop not in (None, ""):
        parts.append(f"时间止损 {int(time_stop)} 日")

    cap = _fmt(row.get("position_cap_pct"), suffix="%")
    first = _fmt(row.get("first_tranche_pct"), suffix="%")
    if cap or first:
        parts.append("仓位上限 " + (cap or "—") + (f"（首仓 {first}）" if first else ""))

    conditions = [str(x) for x in (row.get("invalidation_conditions") or []) if str(x).strip()]
    if conditions:
        parts.append("失效条件 " + "；".join(conditions[:2]))
    triggers = [str(x) for x in (row.get("de_risk_triggers") or []) if str(x).strip()]
    if triggers:
        parts.append("减仓触发 " + "；".join(triggers[:2]))

    gate = str(row.get("risk_gate") or "").strip()
    if gate:
        parts.append(f"风险闸 {gate}")
    confidence = row.get("confidence")
    if confidence not in (None, ""):
        parts.append(f"置信度 {int(confidence)}")
    status = str(row.get("status") or "").strip()
    if status:
        parts.append(f"状态 {status}")
    return parts


def make_plan_tools(plans: TradePlanPort) -> tuple[ToolSpec, ...]:
    def trade_plan_read(
        *, limit: int = 5, symbol: str | None = None, as_of: str = ""
    ) -> ToolResult:
        # 不能写 `limit or 5`：limit=0 会被 falsy 换成 5（本仓库已多次踩坑）
        requested = 5 if limit is None else int(limit)
        capped = max(1, min(requested, MAX_EVIDENCE))
        try:
            rows = list(plans.active_plans(limit=capped, symbol=symbol) or [])
        except Exception as exc:  # noqa: BLE001 — 取不到就如实声明，不抛给模型
            return ToolResult(
                data={"plans": [], "error": type(exc).__name__},
                evidence=(),
                coverage=Coverage(available=(), unavailable=(_SOURCE_KEY,)),
                degraded=True,
                degraded_reason=f"交易计划读取失败（{type(exc).__name__}）",
                as_of=as_of,
                disclaimer=RESEARCH_DISCLAIMER,
            )

        if not rows:
            return ToolResult(
                data={"plans": [], "count": 0},
                evidence=(),
                coverage=Coverage(
                    available=(_SOURCE_KEY,),
                    unavailable=("trade_plan（当前没有有效且可监控的交易计划）",),
                ),
                as_of=as_of,
                disclaimer=RESEARCH_DISCLAIMER,
            )

        evidence: list[EvidenceItem] = []
        for index, row in enumerate(rows[:capped], start=1):
            if not isinstance(row, Mapping):
                continue
            evidence.append(
                EvidenceItem(
                    id=f"E{index}",
                    field=f"交易计划{index}",
                    value="｜".join(_plan_parts(row)),
                    unit="",
                    source_key=_SOURCE_KEY,
                    as_of=str(row.get("signal_trade_date") or as_of),
                )
            )
        if not evidence:
            # 行存在但一条都解析不出来：这是**解析不到**，不是"没有计划"
            return ToolResult(
                data={"plans": [], "error": "unparsable_rows"},
                evidence=(),
                coverage=Coverage(available=(), unavailable=(_SOURCE_KEY,)),
                degraded=True,
                degraded_reason="交易计划结构无法解析（行不是映射）",
                as_of=as_of,
                disclaimer=RESEARCH_DISCLAIMER,
            )

        return ToolResult(
            data={"plans": [dict(row) for row in rows[:capped] if isinstance(row, Mapping)],
                  "count": len(evidence)},
            evidence=tuple(evidence),
            coverage=Coverage(available=(_SOURCE_KEY,), unavailable=()),
            as_of=as_of,
            disclaimer=RESEARCH_DISCLAIMER,
        )

    spec = ToolSpec(
        name="trade_plan_read",
        title="当前交易计划（纪律锚点）",
        description=(
            "读取当前用户**当前有效且可监控的交易计划**：入场区间、硬止损、止盈分档、"
            "移动止盈回撤、时间止损、失效条件、减仓触发、仓位上限与风险闸。"
            "用于回答「我的交易计划是什么」「这只有没有止损」「止损在哪」。"
            "锚点来自平台已落库的计划（报告管线解析产出），模型不得自行编造或改写；"
            "没有有效计划时会显式声明。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": f"返回几条，1-{MAX_EVIDENCE}，默认 5"},
                "symbol": {"type": "string", "description": "只看某只标的（可选）"},
                "as_of": {"type": "string", "description": "基准日 YYYY-MM-DD"},
            },
            "required": [],
        },
        output_contract=TRADE_PLAN_CONTRACT,
        handler=trade_plan_read,
        cost="cheap",
        plan_layer=PLAN_LAYER_DECISION,
        latency_budget_ms=8000,
        freshness_sources=(_SOURCE_KEY,),
    )
    return (spec,)
