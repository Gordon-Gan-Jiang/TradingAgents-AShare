"""分时承接维度：**洗盘最直接的证据**。

## 为什么这条维度不可替代

日线形态只能告诉我们「跌了多少」，而洗盘的定义是「主力在跌的过程中把
不坚定的筹码洗掉」——它的直接证据是**下跌过程中有人在接**。这件事只在
分时上可见：

* 日内砸坑后尾盘拉回、收在日内上部        → 有承接 → 洗盘
* 尾盘继续走弱、收在日内低位、VWAP 之下   → 无承接 → 派发/趋势破坏

正因如此，缺分时数据时探针**必须**标注「日线近似」并封顶置信度
（design D8）：不是礼貌性免责，而是承认此时的判定确实看不到最关键的一层。
"""

from __future__ import annotations

from .context import SignalContext
from .types import SIDE_DISTRIBUTION, SIDE_WASHOUT, Component, Evidence
from .weights import DEFAULT_WEIGHTS, DIM_INTRADAY, WashoutWeights

SOURCE_KEY = "intraday_bars"

# 尾盘强度使用的最后一段占比（至少 1 根）
_TAIL_FRACTION = 0.1
_WASHOUT_RECOVERY = 0.6
_DISTRIBUTION_RECOVERY = 0.3


def _vwap(prices: list[float], volumes: list[float]) -> float | None:
    total_volume = sum(volumes)
    if total_volume <= 0:
        return None
    return sum(p * v for p, v in zip(prices, volumes)) / total_volume


def compute(ctx: SignalContext, weights: WashoutWeights = DEFAULT_WEIGHTS) -> Component:
    weight = weights.weight_for(DIM_INTRADAY)
    as_of = ctx.as_of

    raw: dict[str, float | None] = {
        "day_high": None,
        "day_low": None,
        "day_recovery_ratio": None,
        "tail_return_pct": None,
        "vwap": None,
        "close_vs_vwap_pct": None,
        "down_volume_share": None,
    }

    def unavailable(reason: str) -> Component:
        return Component(
            key=DIM_INTRADAY,
            core=False,
            available=False,
            magnitude=0.0,
            side=None,
            weight=weight,
            raw=raw,
            evidence=(),
            unavailable_reason=reason,
        )

    if not ctx.has_intraday:
        return unavailable("intraday bars unavailable for the evaluation date")

    df = ctx.intraday
    if len(df) < 2:
        return unavailable("intraday bars insufficient")

    prices = [float(c) for c in df["close"].tolist()]
    volumes = [float(v) for v in df["volume"].tolist()]
    highs = [float(h) for h in df["high"].tolist()]
    lows = [float(l) for l in df["low"].tolist()]

    day_high = max(highs)
    day_low = min(lows)
    span = day_high - day_low
    if span <= 0:
        return unavailable("intraday range is zero")

    last = prices[-1]
    day_recovery = (last - day_low) / span

    tail_n = max(int(len(prices) * _TAIL_FRACTION), 1)
    tail_start = prices[-tail_n - 1] if len(prices) > tail_n else prices[0]
    tail_return_pct = (last - tail_start) / tail_start * 100.0 if tail_start > 0 else 0.0

    vwap = _vwap(prices, volumes)
    close_vs_vwap_pct = (last - vwap) / vwap * 100.0 if vwap else None

    down_volume = 0.0
    for i in range(1, len(prices)):
        if prices[i] < prices[i - 1]:
            down_volume += volumes[i]
    total_volume = sum(volumes)
    down_volume_share = down_volume / total_volume if total_volume > 0 else None

    raw.update(
        {
            "day_high": day_high,
            "day_low": day_low,
            "day_recovery_ratio": day_recovery,
            "tail_return_pct": tail_return_pct,
            "vwap": vwap,
            "close_vs_vwap_pct": close_vs_vwap_pct,
            "down_volume_share": down_volume_share,
        }
    )

    side: str | None = None
    magnitude = 0.0
    used: list[str] = []

    if day_recovery >= _WASHOUT_RECOVERY and tail_return_pct > 0:
        side = SIDE_WASHOUT
        magnitude = min(day_recovery, 1.0)
        used += ["day_recovery_ratio", "tail_return_pct"]
        if close_vs_vwap_pct is not None and close_vs_vwap_pct > 0:
            used.append("close_vs_vwap_pct")
    elif day_recovery <= _DISTRIBUTION_RECOVERY and tail_return_pct < 0:
        side = SIDE_DISTRIBUTION
        magnitude = min((_DISTRIBUTION_RECOVERY - day_recovery) / _DISTRIBUTION_RECOVERY, 1.0)
        magnitude = max(magnitude, 0.3)
        used += ["day_recovery_ratio", "tail_return_pct"]
        if close_vs_vwap_pct is not None and close_vs_vwap_pct < 0:
            used.append("close_vs_vwap_pct")

    evidence = tuple(
        Evidence(field=f, value=raw[f], unit=_UNITS[f], source_key=SOURCE_KEY, as_of=as_of)
        for f in dict.fromkeys(used)
        if raw[f] is not None
    )

    return Component(
        key=DIM_INTRADAY,
        core=False,
        available=True,
        magnitude=float(magnitude),
        side=side,
        weight=weight,
        raw=raw,
        evidence=evidence,
    )


_UNITS = {
    "day_high": "yuan",
    "day_low": "yuan",
    "day_recovery_ratio": "ratio",
    "tail_return_pct": "pct",
    "vwap": "yuan",
    "close_vs_vwap_pct": "pct",
    "down_volume_share": "ratio",
}
