"""均线结构维度。

## 为什么「趋势破坏」是独立方向而不是派发的一种

派发（主力主动出货）与趋势破坏（结构已坏）在**交易含义**上不同：
前者可能仍有余波、有反弹可做；后者意味着任何持有理由都需要重审。
把两者合并成一个「非洗盘」的负数，回答层就只能给出含混的表述——
这正是本仓库路线图批评过的「把结论丢掉」的一种形式。

## 为什么刚破位 1-2 日算中性

洗盘的典型形态本就是「破了再收回」。把刚破位当成趋势破坏会造成大量误报；
真正的趋势破坏需要**数日未收复**来确认。
"""

from __future__ import annotations

from .context import SignalContext
from .types import SIDE_TREND_BREAK, SIDE_WASHOUT, Component, Evidence
from .weights import DEFAULT_WEIGHTS, DIM_MA_STRUCTURE, WashoutWeights

SOURCE_KEY = "stock_data"


def _sma(values: list[float], n: int) -> float | None:
    if len(values) < n or n <= 0:
        return None
    window = values[-n:]
    return sum(window) / n


def _below_ma_days(closes: list[float], ma: list[float | None]) -> int:
    """末尾连续收盘低于均线的天数。"""
    days = 0
    for c, m in zip(reversed(closes), reversed(ma)):
        if m is None or c >= m:
            break
        days += 1
    return days


def compute(ctx: SignalContext, weights: WashoutWeights = DEFAULT_WEIGHTS) -> Component:
    weight = weights.weight_for(DIM_MA_STRUCTURE)
    as_of = ctx.as_of
    closes = [float(c) for c in ctx.daily["close"].tolist()]

    ma5_series: list[float | None] = [_sma(closes[: i + 1], 5) for i in range(len(closes))]
    ma10_series: list[float | None] = [_sma(closes[: i + 1], 10) for i in range(len(closes))]
    ma20_series: list[float | None] = [_sma(closes[: i + 1], 20) for i in range(len(closes))]

    raw = {
        "ma5": ma5_series[-1],
        "ma10": ma10_series[-1],
        "ma20": ma20_series[-1],
        "above_ma20": None,
        "below_ma20_days": None,
        "reclaimed_ma5": None,
        "ma5_gt_ma10": None,
    }

    if ma5_series[-1] is None or ma20_series[-1] is None:
        return Component(
            key=DIM_MA_STRUCTURE,
            core=False,
            available=False,
            magnitude=0.0,
            side=None,
            weight=weight,
            raw=raw,
            evidence=(),
            unavailable_reason="insufficient bars for MA20",
        )

    last = closes[-1]
    ma5 = float(ma5_series[-1])
    ma20 = float(ma20_series[-1])
    ma10 = ma10_series[-1]

    above_ma20 = last >= ma20
    below_ma20_days = _below_ma_days(closes, ma20_series)
    reclaimed_ma5 = last >= ma5
    ma5_gt_ma10 = None if ma10 is None else ma5 >= float(ma10)

    raw.update(
        {
            "ma5": ma5,
            "ma10": None if ma10 is None else float(ma10),
            "ma20": ma20,
            "above_ma20": above_ma20,
            "below_ma20_days": below_ma20_days,
            "reclaimed_ma5": reclaimed_ma5,
            "ma5_gt_ma10": ma5_gt_ma10,
        }
    )

    side: str | None = None
    magnitude = 0.0
    used: list[str] = []

    if above_ma20 and reclaimed_ma5:
        side = SIDE_WASHOUT
        magnitude = 0.6
        if ma5_gt_ma10:
            magnitude = min(magnitude + 0.2, 1.0)
        used += ["above_ma20", "reclaimed_ma5"]
        if ma5_gt_ma10:
            used.append("ma5_gt_ma10")
    elif not above_ma20 and below_ma20_days >= 3:
        side = SIDE_TREND_BREAK
        magnitude = min(below_ma20_days / 6.0, 1.0)
        used += ["below_ma20_days", "ma20"]

    evidence = tuple(
        Evidence(field=f, value=raw[f], unit=_UNITS[f], source_key=SOURCE_KEY, as_of=as_of)
        for f in dict.fromkeys(used)
        if raw[f] is not None
    )

    return Component(
        key=DIM_MA_STRUCTURE,
        core=False,
        available=True,
        magnitude=float(magnitude),
        side=side,
        weight=weight,
        raw=raw,
        evidence=evidence,
    )


_UNITS = {
    "ma5": "yuan",
    "ma10": "yuan",
    "ma20": "yuan",
    "above_ma20": "bool",
    "below_ma20_days": "count",
    "reclaimed_ma5": "bool",
    "ma5_gt_ma10": "bool",
}
