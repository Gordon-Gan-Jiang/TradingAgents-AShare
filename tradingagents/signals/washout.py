"""洗盘探针：把八个维度聚合成四态判定，并给出证伪条件。

## 这个模块的三个关键责任

1. **三侧质量聚合 + 位置乘法调制**（D5）。位置不是第 9 个加权项，而是乘在
   量价证据上的因子；趋势破坏侧不受位置调制（结构是否已坏与涨了多少无关）。

2. **门控**（D6）。缩量是洗盘判据里最容易被误用的一条，因此这里显式检查
   板块中位换手：`broad_contraction` → 撤回缩量侧加分并转入非洗盘侧；
   换手基准缺失 → 撤回加分并标记未覆盖。**不允许静默退回自身比较。**

3. **结构性弃权**（D14）。核心维度缺失、或两侧质量过于接近（margin 低于阈值）
   则弃权。弃权是合法产品结果：`signal_processing.py` 早已确立「无契约时显式
   弃权并计入弃权率」的文化，对话层必须继承它，而不是硬给一个方向。

## 为什么证伪条件由代码生成而不是让模型写

模型写证伪条件会产生无法回测的措辞（「若走势转弱」）。由代码从已计算的
观测推导（`derived_from` 指向具体组件），才能保证阈值可追溯、且 T+5 回填时
能被机械判定是否已失效。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from . import capital, chips, intraday, ma_structure, position, price_action, relative, volume
from .context import SignalContext, build_context
from .ports import SignalPorts
from .types import (
    SIDE_DISTRIBUTION,
    SIDE_TREND_BREAK,
    SIDE_WASHOUT,
    STATE_DISTRIBUTION,
    STATE_INSUFFICIENT,
    STATE_TREND_BREAK,
    STATE_WASHOUT,
    Aggregation,
    Component,
    Falsifier,
    Modulator,
    ProbeResult,
    assert_well_formed,
)
from .weights import (
    CORE_DIMENSIONS,
    DEFAULT_WEIGHTS,
    DIM_CAPITAL,
    DIM_INTRADAY,
    DIM_MA_STRUCTURE,
    DIM_PRICE_ACTION,
    DIM_RELATIVE_STRENGTH,
    DIM_VOLUME,
    MOD_POSITION,
    WashoutWeights,
    confidence_from_margin,
)

PROBE_NAME = "washout"

# 维度求值顺序：位置调制必须在量价之后（它要读同一个窗口），
# 其余维度彼此独立，顺序不影响结果。
DIMENSIONS = (
    volume,
    price_action,
    ma_structure,
    relative,
    capital,
    chips,
    intraday,
)

# 相对换手分类（与 relative.py 的口径一致）
_TURNOVER_BROAD = "broad_contraction"

GATE_BROAD_CONTRACTION = "broad_contraction_blocks_washout"
GATE_NO_PEER = "contraction_credit_withheld_no_peer"

UNAVAILABLE_BOARD_TURNOVER = "board_turnover"


def _by_key(components: Sequence[Component]) -> dict[str, Component]:
    return {c.key: c for c in components}


def aggregate_masses(
    components: Sequence[Component],
    modulator: Modulator,
    weights: WashoutWeights = DEFAULT_WEIGHTS,
) -> Aggregation:
    """把组件聚合成三侧质量，并施加位置调制与门控。

    纯函数：同样的输入永远给出同样的输出，这是回放校准的前提。
    """
    by_key = _by_key(components)
    factor = modulator.factor if modulator.available and modulator.factor > 0 else 1.0

    overrides: dict[str, str | None] = {}
    gates: list[str] = []
    extra_unavailable: list[str] = []

    vol = by_key.get(DIM_VOLUME)
    rel = by_key.get(DIM_RELATIVE_STRENGTH)

    turnover_available = bool(rel.raw.get("turnover_class_available")) if rel is not None else False
    turnover_class = rel.raw.get("turnover_class") if rel is not None else None

    # ── D6 门控：缩量只有在「相对板块不缩量」时才能支撑洗盘 ──────────────
    if vol is not None and vol.available and vol.side == SIDE_WASHOUT:
        if not turnover_available:
            # 无法区分「主力锁仓」与「无人问津」→ 宁可少说
            overrides[DIM_VOLUME] = None
            gates.append(GATE_NO_PEER)
            extra_unavailable.append(UNAVAILABLE_BOARD_TURNOVER)
        elif turnover_class == _TURNOVER_BROAD:
            # 全市场/全板块都在缩量 → 这是流动性问题，不是震仓
            overrides[DIM_VOLUME] = SIDE_TREND_BREAK
            gates.append(GATE_BROAD_CONTRACTION)

    sides = {SIDE_WASHOUT: 0.0, SIDE_DISTRIBUTION: 0.0, SIDE_TREND_BREAK: 0.0}
    for c in components:
        side = overrides.get(c.key, c.side)
        if not c.available or side is None:
            continue
        sides[side] += c.weight * c.magnitude

    # 位置乘法调制（趋势破坏侧不参与）
    sides[SIDE_WASHOUT] *= factor
    sides[SIDE_DISTRIBUTION] *= 1.0 / factor

    return Aggregation(
        sides=sides,
        factor=factor,
        position_raw=dict(modulator.raw) if modulator.available else {},
        gates=tuple(gates),
        unavailable=tuple(extra_unavailable),
    )


# ── 证伪条件 ──────────────────────────────────────────────────────────────


def _build_falsifiers(
    state: str,
    by_key: Mapping[str, Component],
    weights: WashoutWeights,
) -> tuple[Falsifier, ...]:
    """按判定状态生成可检验的失效条件。

    每条必须可追溯到已计算的组件（`derived_from`），让阈值不是凭空写的数字，
    同时保证 T+5 回填时能被机械判定「是否已失效」。
    """
    fp = weights.falsifier

    def ref(preferred: str) -> str:
        """优先用更贴题的组件，缺则退回价格行为——它始终存在。"""
        comp = by_key.get(preferred)
        if comp is not None and comp.available:
            return preferred
        return DIM_PRICE_ACTION

    out: list[Falsifier] = []
    outflow = f"{fp.invalidate_outflow:.0e}"

    if state == STATE_WASHOUT:
        out.append(
            Falsifier(
                observable="next_day_volume_ratio",
                threshold=f"> {fp.invalidate_volume_ratio}",
                window_days=1,
                description=(
                    f"次日放量下跌（量比 > {fp.invalidate_volume_ratio}）→ 缩量震仓假设失效，转趋势破坏"
                ),
                derived_from=DIM_VOLUME,
            )
        )
        out.append(
            Falsifier(
                observable="close_vs_short_ma",
                threshold="收盘站回 5 日线上方",
                window_days=fp.reclaim_window_days,
                description=f"{fp.reclaim_window_days} 个交易日内未站回 5 日线 → 降级为证据不足",
                derived_from=ref(DIM_MA_STRUCTURE),
            )
        )
        out.append(
            Falsifier(
                observable="main_net_inflow",
                threshold=f"<= -{outflow}",
                window_days=2,
                description=f"主力净流出超过 {outflow} 元且继续下破 → 承接判断被证伪",
                derived_from=ref(DIM_CAPITAL),
            )
        )
        out.append(
            Falsifier(
                observable="rebound_volume_recovery",
                threshold=">= 1.0",
                window_days=3,
                description="反抽始终无量（恢复率 < 1.0）→ 更可能是流动性枯竭而非震仓",
                derived_from=DIM_VOLUME,
            )
        )
    elif state == STATE_DISTRIBUTION:
        out.append(
            Falsifier(
                observable="rebound_volume_recovery",
                threshold=">= 1.0 且收盘站回 5 日线",
                window_days=2,
                description="反抽放量并收复 5 日线 → 派发假设减弱",
                derived_from=DIM_VOLUME,
            )
        )
        out.append(
            Falsifier(
                observable="new_low",
                threshold="未创监控期内新低",
                window_days=fp.reclaim_window_days,
                description=f"{fp.reclaim_window_days} 个交易日内未创新低 → 抛压衰竭，派发假设失效",
                derived_from=ref(DIM_PRICE_ACTION),
            )
        )
        out.append(
            Falsifier(
                observable="super_large_net_inflow",
                threshold="> 0",
                window_days=2,
                description="超大单转为净流入 → 机构出货判断被证伪",
                derived_from=ref(DIM_CAPITAL),
            )
        )
    elif state == STATE_TREND_BREAK:
        out.append(
            Falsifier(
                observable="ma20_reclaim",
                threshold="收盘站回 20 日线上方",
                window_days=fp.reclaim_window_days,
                description=f"{fp.reclaim_window_days} 个交易日内收复 20 日线 → 趋势破坏假设失效",
                derived_from=ref(DIM_MA_STRUCTURE),
            )
        )
        out.append(
            Falsifier(
                observable="main_net_inflow",
                threshold="> 0",
                window_days=3,
                description="主力资金连续净流入 → 结构修复，破坏判断需重审",
                derived_from=ref(DIM_CAPITAL),
            )
        )
        out.append(
            Falsifier(
                observable="excess_vs_board_pp",
                threshold=">= 0",
                window_days=3,
                description="相对板块收益转正 → 弱势格局改变",
                derived_from=ref(DIM_RELATIVE_STRENGTH),
            )
        )

    return tuple(out)


# ── 评估入口 ──────────────────────────────────────────────────────────────


def evaluate_context(
    ctx: SignalContext,
    weights: WashoutWeights = DEFAULT_WEIGHTS,
) -> ProbeResult:
    """对已装配的上下文求判定。纯函数（不触端口、不读时钟）。"""
    components = tuple(dim.compute(ctx, weights) for dim in DIMENSIONS)
    modulator = position.compute(ctx, weights)
    assert_well_formed(components)

    agg = aggregate_masses(components, modulator, weights)
    by_key = _by_key(components)

    unavailable = set(agg.unavailable)
    for c in components:
        if not c.available:
            unavailable.add(c.key)
    if not modulator.available:
        unavailable.add(MOD_POSITION)

    core_missing = sorted(
        {c.key for c in components if c.core and not c.available}
        | ({MOD_POSITION} if (not modulator.available and MOD_POSITION in CORE_DIMENSIONS) else set())
    )

    daily_only = not ctx.has_intraday
    data_cutoff = str(ctx.daily["date"].iloc[-1])[:10] if len(ctx.daily) else ""

    # ── 弃权：核心维度缺失 ────────────────────────────────────────────
    if core_missing:
        return ProbeResult(
            probe=PROBE_NAME,
            symbol=ctx.symbol,
            as_of=ctx.as_of,
            state=STATE_INSUFFICIENT,
            confidence=0,
            margin=agg.margin,
            score_washout=agg.sides[SIDE_WASHOUT],
            score_distribution=agg.sides[SIDE_DISTRIBUTION],
            score_trend_break=agg.sides[SIDE_TREND_BREAK],
            components=components,
            estimated_dimensions=tuple(
                sorted(c.key for c in components if getattr(c, "estimated", False))
            ),
            modulators=(modulator,),
            falsifiers=(),
            applied_gates=agg.gates,
            unavailable=tuple(sorted(unavailable | set(core_missing))),
            daily_only=daily_only,
            signal_version=weights.version,
            data_cutoff=data_cutoff,
        )

    # ── 弃权：证据不足或两侧过于接近 ──────────────────────────────────
    if agg.total <= 0 or agg.margin < weights.margin_abstain:
        return ProbeResult(
            probe=PROBE_NAME,
            symbol=ctx.symbol,
            as_of=ctx.as_of,
            state=STATE_INSUFFICIENT,
            confidence=0,
            margin=agg.margin,
            score_washout=agg.sides[SIDE_WASHOUT],
            score_distribution=agg.sides[SIDE_DISTRIBUTION],
            score_trend_break=agg.sides[SIDE_TREND_BREAK],
            components=components,
            estimated_dimensions=tuple(
                sorted(c.key for c in components if getattr(c, "estimated", False))
            ),
            modulators=(modulator,),
            falsifiers=(),
            applied_gates=agg.gates,
            unavailable=tuple(sorted(unavailable)),
            daily_only=daily_only,
            signal_version=weights.version,
            data_cutoff=data_cutoff,
        )

    state = agg.leading_side or STATE_INSUFFICIENT
    confidence = confidence_from_margin(agg.margin, weights)

    # 高置信是**结构条件**：分时承接与筹码都要可得，且分离度达标。
    # 缺分时意味着看不到「下跌中有没有人接」——那正是洗盘最直接的证据，
    # 此时无论分数多高都不能声称高置信。
    high_confidence = (
        not daily_only and ctx.has_chips and agg.margin >= weights.margin_high
    )

    return ProbeResult(
        probe=PROBE_NAME,
        symbol=ctx.symbol,
        as_of=ctx.as_of,
        state=state,
        confidence=confidence,
        margin=agg.margin,
        score_washout=agg.sides[SIDE_WASHOUT],
        score_distribution=agg.sides[SIDE_DISTRIBUTION],
        score_trend_break=agg.sides[SIDE_TREND_BREAK],
        components=components,
        modulators=(modulator,),
        falsifiers=_build_falsifiers(state, by_key, weights),
        high_confidence=high_confidence,
        applied_gates=agg.gates,
        unavailable=tuple(sorted(unavailable)),
        daily_only=daily_only,
        signal_version=weights.version,
        data_cutoff=data_cutoff,
    )


def evaluate(
    *,
    ports: SignalPorts,
    symbol: str,
    as_of: str,
    window_days: int = 20,
    weights: WashoutWeights = DEFAULT_WEIGHTS,
) -> ProbeResult:
    """从端口取数并求判定。这是探针对外的唯一入口。

    注意：`build_context` 会在 as_of 处截断，即使 provider 无视 end 参数
    多返回了未来数据 —— 回放校准依赖这条（否则判定会掺入未来信息）。
    """
    ctx = build_context(ports=ports, symbol=symbol, as_of=as_of, window_days=window_days)
    return evaluate_context(ctx, weights)


def component_map(result: ProbeResult) -> dict[str, Component]:
    """便捷取值：按 key 索引组件（回答层与调试都会用到）。"""
    return _by_key(result.components)


def raw_observables(result: ProbeResult) -> dict[str, Any]:
    """汇总所有组件的原始观测，供回答层构造证据集。"""
    merged: dict[str, Any] = {}
    for c in result.components:
        for key, value in c.raw.items():
            merged[f"{c.key}.{key}"] = value
    for m in result.modulators:
        for key, value in m.raw.items():
            merged[f"{m.key}.{key}"] = value
    return merged
