"""资金构成维度。

## 只看「主力净流出」是不够的

提示词里那条「主力净流出 + 急跌 + 缩量 = 洗盘信号」如果直接按字面实现，
会把两种资金结构完全不同的情形混为一谈：

* 净流出由**超大单主导** → 机构主动出货 → 派发
* 净流出但**超大单占比低** → 更像散户恐慌盘，主力并未主动出货 → 洗盘

因此本维度看的是资金的**构成**（超大单占净流出的比重），而不只是方向。
"""

from __future__ import annotations

from .context import SignalContext
from .types import SIDE_DISTRIBUTION, SIDE_WASHOUT, Component, Evidence
from .weights import DEFAULT_WEIGHTS, DIM_CAPITAL, CapitalParams, WashoutWeights

SOURCE_KEY = "individual_fund_flow"

_FIELDS = ("main_net_inflow", "super_large_net_inflow", "large_net_inflow")


def compute(ctx: SignalContext, weights: WashoutWeights = DEFAULT_WEIGHTS) -> Component:
    params: CapitalParams = weights.capital
    weight = weights.weight_for(DIM_CAPITAL)
    as_of = ctx.as_of

    raw: dict[str, float | None] = {f: None for f in _FIELDS}
    raw["super_large_share_of_outflow"] = None

    def unavailable(reason: str) -> Component:
        return Component(
            key=DIM_CAPITAL,
            core=False,
            available=False,
            magnitude=0.0,
            side=None,
            weight=weight,
            raw=raw,
            evidence=(),
            unavailable_reason=reason,
        )

    if not ctx.has_flow:
        return unavailable("individual fund flow unavailable")

    df = ctx.flow
    row = df.iloc[-1]
    for f in _FIELDS:
        if f in df.columns:
            value = row[f]
            raw[f] = None if value is None or value != value else float(value)

    main_net = raw["main_net_inflow"]
    if main_net is None:
        return unavailable("main net inflow missing")

    super_large = raw["super_large_net_inflow"]
    if main_net < 0 and super_large is not None and super_large < 0:
        raw["super_large_share_of_outflow"] = abs(super_large) / abs(main_net)

    side: str | None = None
    magnitude = 0.0
    used: list[str] = ["main_net_inflow"]

    if main_net <= -params.material_outflow:
        share = raw["super_large_share_of_outflow"]
        if share is not None and share >= params.super_large_dominance:
            side = SIDE_DISTRIBUTION
            magnitude = min(share, 1.0)
            used.append("super_large_net_inflow")
        else:
            side = SIDE_WASHOUT
            magnitude = min(abs(main_net) / (params.material_outflow * 4), 1.0)
            magnitude = max(magnitude, 0.4)
            if super_large is not None:
                used.append("super_large_net_inflow")
    elif main_net >= params.material_outflow:
        side = SIDE_WASHOUT
        magnitude = min(main_net / (params.material_outflow * 4), 1.0)
        magnitude = max(magnitude, 0.5)

    evidence = tuple(
        Evidence(field=f, value=raw[f], unit="yuan", source_key=SOURCE_KEY, as_of=as_of)
        for f in dict.fromkeys(used)
        if raw[f] is not None
    )
    if raw["super_large_share_of_outflow"] is not None:
        evidence = evidence + (
            Evidence(
                field="super_large_share_of_outflow",
                value=raw["super_large_share_of_outflow"],
                unit="ratio",
                source_key=SOURCE_KEY,
                as_of=as_of,
            ),
        )

    return Component(
        key=DIM_CAPITAL,
        core=False,
        available=True,
        magnitude=float(magnitude),
        side=side,
        weight=weight,
        raw=raw,
        evidence=evidence,
    )
