"""信号工具：把确定性探针暴露成 `ToolResult`。

## 这一层存在的唯一理由

`signals/` 是纯领域层，它不知道 `ToolSpec`、`EvidenceItem` 编号、`Coverage`
这些东西存在（由架构边界测试强制）。本模块是**转换点**：

    ProbeResult（领域契约） → ToolResult（传输契约）

把转换放在这里而不是信号层，意味着「加一种传输（MCP / CLI / gRPC）」永远
只影响适配器，不会波及判定逻辑。

## 证据编号在这里分配

`E1`/`E2`… 是**面向回答层**的引用编号，属于传输关注点，因此不在信号的
`Evidence`（带 field/value/unit/source_key/as_of）里。这里按稳定顺序展开成
`EvidenceItem`，使「模型声明引用 → 校验器查表」这条链路成立。

## 批量变体为什么是必需的而不是优化

「我持仓里哪只最危险」有 30 只。逐只调用需要 30 次工具调用，**任何轮数上限
都装不下**；Agent 只能中途放弃。批量变体把 N 只标的压进一次调用，同时
保持每只各自的证据与弃权状态——这是组合级问句可回答的前提。
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence

from tradingagents.grounding.types import Coverage, EvidenceItem
from tradingagents.signals import (
    DEFAULT_WEIGHTS,
    PROBE_NAME,
    STATE_INSUFFICIENT,
    SignalPorts,
    WashoutWeights,
    evaluate,
    raw_observables,
)
from tradingagents.signals.types import ProbeResult

from .spec import PLAN_LAYER_DECISION, RESEARCH_DISCLAIMER, ToolResult, ToolSpec

# 一次批量评估的标的上限。超过则截断并显式告知——静默只算一部分会让
# 「哪只最危险」的结论在缺少标的的情况下给出错误的全貌。
BATCH_LIMIT = 30

PortsFactory = Callable[[str, str], SignalPorts]

# 组件维度 → freshness 契约的源键。必须显式映射：维度名（intraday_absorption）
# 与契约源名（intraday_bars）并不总是一致，靠字符串巧合匹配迟早失配。
COMPONENT_SOURCES: Mapping[str, tuple[str, ...]] = {
    "volume_contraction": ("stock_data",),
    "price_action": ("stock_data",),
    "ma_structure": ("stock_data",),
    "position": ("stock_data",),
    "relative_strength": ("board_spot",),
    "capital_composition": ("individual_fund_flow",),
    "chip_distribution": ("chip_distribution",),
    "intraday_absorption": ("intraday_bars",),
}

PROBE_SOURCES = ("stock_data", "intraday_bars", "chip_distribution", "individual_fund_flow", "board_spot")


def _freshness_for_probe(result: ProbeResult) -> dict[str, dict]:
    """由判定结果推导各源的 freshness。

    只有**全部贡献组件都不可得**时，才把该源标为不可用——`stock_data` 同时
    供量价与均线使用，仅均线因 bar 不足而缺失时，日线数据本身是好的。
    """
    from .freshness import evaluate_tool_sources

    contributors: dict[str, list[str]] = {key: [] for key in PROBE_SOURCES}
    for component in result.components:
        for source in COMPONENT_SOURCES.get(component.key, ()):
            contributors.setdefault(source, []).append(component.key)

    unavailable: list[str] = []
    for source, component_keys in contributors.items():
        if not component_keys:
            continue
        if all(
            any(c.key == key and not c.available for c in result.components)
            for key in component_keys
        ):
            unavailable.append(source)

    cutoffs = {"stock_data": result.data_cutoff} if result.data_cutoff else {}
    return evaluate_tool_sources(
        source_keys=PROBE_SOURCES,
        as_of=result.as_of,
        unavailable=unavailable,
        cutoffs=cutoffs,
    )

_SYMBOL_SCHEMA = {"type": "string", "description": "A 股代码，如 600519.SH"}
_AS_OF_SCHEMA = {"type": "string", "description": "评估基准日（含），格式 YYYY-MM-DD"}
_WINDOW_SCHEMA = {"type": "integer", "description": "回看交易日数", "default": 20}


def _expand_evidence(result: ProbeResult) -> tuple[EvidenceItem, ...]:
    """把探针的证据展开成带稳定编号的证据项，按 (字段, 值) 去重。"""
    seen: set[tuple[str, str, Any]] = set()
    items: list[EvidenceItem] = []
    sources = [c.evidence for c in result.components] + [m.evidence for m in result.modulators]
    for group in sources:
        for ev in group:
            key = (ev.field, ev.source_key, ev.value)
            if key in seen:
                continue
            seen.add(key)
            items.append(
                EvidenceItem(
                    id=f"E{len(items) + 1}",
                    field=ev.field,
                    value=ev.value,
                    unit=ev.unit,
                    source_key=ev.source_key,
                    as_of=ev.as_of,
                )
            )

    # 输入为**模型估计**的维度必须成为一条可引用证据：否则模型只会把它当数据
    # 复述，"这是估计"这件事在回答里就消失了。用固定字段名 `数据口径`，
    # 提示词据此要求逐项声明（见 agent/loop.py::_build_prompt）。
    estimated = tuple(getattr(result, "estimated_dimensions", ()) or ())
    if estimated:
        items.append(
            EvidenceItem(
                id=f"E{len(items) + 1}",
                field="数据口径",
                value=(
                    "以下维度为模型估计，不是交易所原始数据："
                    + "、".join(estimated)
                ),
                unit="",
                source_key=",".join(estimated),
                as_of=result.as_of,
            )
        )
    return tuple(items)


def _coverage_of(result: ProbeResult) -> Coverage:
    available = tuple(c.key for c in result.components if c.available)
    available += tuple(m.key for m in result.modulators if m.available)
    return Coverage(available=available, unavailable=tuple(result.unavailable))


def _verdict_payload(result: ProbeResult) -> dict[str, Any]:
    """判定主体：只放结构化事实，不放散文。"""
    return {
        "probe": result.probe,
        # 供回答层渲染"模型估计"口径：这是**代码**要写的事实，不该让模型转述
        "estimated_dimensions": list(getattr(result, "estimated_dimensions", ()) or ()),
        "symbol": result.symbol,
        "as_of": result.as_of,
        "state": result.state,
        "confidence": int(result.confidence),
        "margin": round(float(result.margin), 6),
        "score_washout": round(float(result.score_washout), 6),
        "score_distribution": round(float(result.score_distribution), 6),
        "score_trend_break": round(float(result.score_trend_break), 6),
        "high_confidence": bool(result.high_confidence),
        "daily_only": bool(result.daily_only),
        "signal_version": result.signal_version,
        "applied_gates": list(result.applied_gates),
        "unavailable": list(result.unavailable),
        "falsifiers": [
            {
                "observable": f.observable,
                "threshold": f.threshold,
                "window_days": f.window_days,
                "description": f.description,
                "derived_from": f.derived_from,
            }
            for f in result.falsifiers
        ],
        "components": [
            {
                "key": c.key,
                "available": c.available,
                "side": c.side,
                "magnitude": round(float(c.magnitude), 6),
                "weight": float(c.weight),
                "raw": dict(c.raw),
            }
            for c in result.components
        ],
        "modulator": (
            {
                "key": result.modulators[0].key,
                "factor": round(float(result.modulators[0].factor), 6),
                "raw": dict(result.modulators[0].raw),
            }
            if result.modulators
            else None
        ),
    }


def _single_result(
    *,
    ports_factory: PortsFactory,
    weights: WashoutWeights,
    symbol: str,
    as_of: str,
    window_days: int,
) -> tuple[ToolResult, ProbeResult]:
    ports = ports_factory(symbol, as_of)
    result = evaluate(ports=ports, symbol=symbol, as_of=as_of, window_days=window_days, weights=weights)
    tool_result = ToolResult(
        # 判定字段平铺在 data 顶层：消费者（对话层 / MCP / HTTP）直接取 state、confidence，
        # 不必先剥一层包装。嵌套只会让每个消费点多写一次解包。
        data={**_verdict_payload(result), "observables": raw_observables(result)},
        freshness=_freshness_for_probe(result),
        evidence=_expand_evidence(result),
        coverage=_coverage_of(result),
        degraded=result.daily_only or bool(result.unavailable),
        degraded_reason=(
            "仅日线数据，缺少分时承接与筹码证据，置信度已相应限制"
            if result.daily_only
            else None
        ),
        as_of=as_of,
        disclaimer=RESEARCH_DISCLAIMER,
    )
    return tool_result, result


def make_signal_tools(
    ports_factory: PortsFactory,
    *,
    weights: WashoutWeights = DEFAULT_WEIGHTS,
    batch_limit: int = BATCH_LIMIT,
) -> tuple[ToolSpec, ...]:
    """构造信号类工具。`ports_factory` 由组合根注入（应用层提供真实实现）。

    刻意把端口与权重作为**闭包参数**而不是全局单例：并发评估可以用不同的
    权重版本互不干扰，测试可以直接注入内存假端口。
    """

    def washout_probe(symbol: str, as_of: str, window_days: int = 20) -> ToolResult:
        tool_result, _ = _single_result(
            ports_factory=ports_factory,
            weights=weights,
            symbol=symbol,
            as_of=as_of,
            window_days=window_days,
        )
        return tool_result

    def washout_probe_batch(symbols: Sequence[str], as_of: str, window_days: int = 20) -> ToolResult:
        requested = len(symbols)
        selected = list(symbols)[:batch_limit]
        rows: list[dict[str, Any]] = []
        evidence: tuple[EvidenceItem, ...] = ()
        available: list[str] = []
        unavailable: list[str] = []

        for symbol in selected:
            row_result, probe = _single_result(
                ports_factory=ports_factory,
                weights=weights,
                symbol=symbol,
                as_of=as_of,
                window_days=window_days,
            )
            # 合并到一份证据集：编号在 merge 时重排，保持全局唯一
            merged = ToolResult(data={}, evidence=evidence, coverage=Coverage(), as_of=as_of).merged(row_result)
            evidence = merged.evidence
            for key in row_result.coverage.available:
                if key not in available:
                    available.append(key)
            for key in row_result.coverage.unavailable:
                if key not in unavailable:
                    unavailable.append(key)

            rows.append(
                {
                    "symbol": symbol,
                    "state": probe.state,
                    "confidence": int(probe.confidence),
                    "margin": round(float(probe.margin), 6),
                    "score_washout": round(float(probe.score_washout), 6),
                    "score_distribution": round(float(probe.score_distribution), 6),
                    "score_trend_break": round(float(probe.score_trend_break), 6),
                    "daily_only": bool(probe.daily_only),
                    "unavailable": list(probe.unavailable),
                    "falsifiers": [f.description for f in probe.falsifiers],
                }
            )

        # 按证据分离度排序：分离度越高，判定越有依据
        rows.sort(key=lambda r: r["margin"], reverse=True)

        return ToolResult(
            data={
                "results": rows,
                "requested": requested,
                "evaluated": len(selected),
                "truncated": requested > len(selected),
                "as_of": as_of,
            },
            evidence=evidence,
            coverage=Coverage(available=tuple(available), unavailable=tuple(unavailable)),
            degraded=bool(unavailable),
            as_of=as_of,
            disclaimer=RESEARCH_DISCLAIMER,
        )

    probe_spec = ToolSpec(
        name="signal_washout_probe",
        title="洗盘/派发判别探针",
        description=(
            "对单只 A 股判定当前处于洗盘（震仓）、派发（出货）、趋势破坏还是证据不足。"
            "用于回答「这只票是不是在洗盘」「主力是不是在出货」「这波下跌要不要止损」。"
            "返回四态判定、置信度、逐维度证据、以及必须跟踪的失效条件。"
            "判定由确定性信号层给出，不接受模型覆写；数据未覆盖的维度会显式列出。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "symbol": _SYMBOL_SCHEMA,
                "as_of": _AS_OF_SCHEMA,
                "window_days": _WINDOW_SCHEMA,
            },
            "required": ["symbol", "as_of"],
        },
        output_contract="WASHOUT_PROBE",
        handler=washout_probe,
        cost="medium",
        plan_layer=PLAN_LAYER_DECISION,
        # 预算必须反映真实工作量。这个值曾是 15s，而探针要拉日线 + 自算筹码
        # （再取一次日线）+ 分时 + 板块（含首次的行业映射扫描）+ 资金流多通道回退。
        # 实测因此被注册表整体判超时、返回 0 证据，判定只能答"无可用证据"——
        # **声明一个做不到的预算，等于让工具永远失败**。
        # 60s 与 `signal_service.VERDICT_DEADLINE_SECONDS` 对齐：工具预算是内层，
        # 端到端预算是外层，内层不能大于外层。
        latency_budget_ms=60000,
        freshness_sources=("stock_data", "intraday_bars", "chip_distribution", "individual_fund_flow", "board_spot"),
        requires=("position",),
    )

    batch_spec = ToolSpec(
        name="signal_washout_probe_batch",
        title="洗盘/派发批量判别",
        description=(
            "对一组 A 股（例如用户全部持仓）一次性判别洗盘/派发状态并按证据强度排序，"
            "用于回答「我持仓里哪只最危险」这类组合级问题。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "symbols": {
                    "type": "array",
                    "description": f"A 股代码列表，最多 {batch_limit} 只",
                    "items": {"type": "string"},
                },
                "as_of": _AS_OF_SCHEMA,
                "window_days": _WINDOW_SCHEMA,
            },
            "required": ["symbols", "as_of"],
        },
        output_contract="WASHOUT_PROBE_BATCH",
        handler=washout_probe_batch,
        cost="medium",
        plan_layer=PLAN_LAYER_DECISION,
        latency_budget_ms=60000,
        freshness_sources=("stock_data", "intraday_bars", "chip_distribution", "individual_fund_flow", "board_spot"),
        requires=("position",),
    )

    return (probe_spec, batch_spec)


__all__ = ["make_signal_tools", "BATCH_LIMIT", "PROBE_NAME", "STATE_INSUFFICIENT"]
