"""价格行为维度：回撤幅度/速度，以及**盘中有没有承接**。

## 核心判据是「收盘位置」而不是「跌了多少」

洗盘与派发在跌幅上可以完全一样，区别在于**下杀过程中有没有人接**：
* 急跌后当日收回、留长下影 → 低位有承接 → 洗盘侧
* 阴跌收在当日低位、无下影   → 无人承接   → 派发侧

## 为什么这里不算累计涨幅

位置是**调制器**（见 `position.py` 与 design D5），必须乘在量价证据上，
而不是作为本维度的一个加成项。若在此处按累计涨幅加权，同一个形态在早期
与晚期得分不同的原因就不可见了，交互项的可解释性随之丢失。
"""

from __future__ import annotations

from .context import SignalContext
from .types import SIDE_DISTRIBUTION, SIDE_WASHOUT, Component, Evidence
from .weights import DEFAULT_WEIGHTS, DIM_PRICE_ACTION, PriceActionParams, WashoutWeights

SOURCE_KEY = "stock_data"


def _bar_geometry(o: float, h: float, l: float, c: float) -> tuple[float | None, float | None]:
    """返回 (下影线占比, 跌幅回收率)。振幅为 0 时两者均不可计算。"""
    span = h - l
    if span <= 0:
        return None, None
    lower_shadow = (min(o, c) - l) / span
    recovery = (c - l) / span
    return lower_shadow, recovery


def compute(ctx: SignalContext, weights: WashoutWeights = DEFAULT_WEIGHTS) -> Component:
    params: PriceActionParams = weights.price_action
    weight = weights.weight_for(DIM_PRICE_ACTION)
    df = ctx.daily
    as_of = ctx.as_of

    closes = [float(c) for c in df["close"].tolist()]
    opens = [float(o) for o in df["open"].tolist()]
    highs = [float(h) for h in df["high"].tolist()]
    lows = [float(l) for l in df["low"].tolist()]

    def unavailable(reason: str) -> Component:
        return Component(
            key=DIM_PRICE_ACTION,
            core=True,
            available=False,
            magnitude=0.0,
            side=None,
            weight=weight,
            raw={
                "drawdown_pct": None,
                "drawdown_bars": None,
                "drawdown_speed_pct_per_bar": None,
                "last_change_pct": None,
                "lower_shadow_ratio": None,
                "intraday_recovery_ratio": None,
            },
            evidence=(),
            unavailable_reason=reason,
        )

    if len(closes) < 2:
        return unavailable("need at least 2 bars")
    if any(not (o == o and h == h and l == l and c == c) for o, h, l, c in zip(opens, highs, lows, closes)):
        return unavailable("OHLC contains missing values")

    # ── 回撤 ───────────────────────────────────────────────────────────
    peak = max(closes)
    peak_idx = closes.index(peak)
    last = closes[-1]
    drawdown_pct = (peak - last) / peak * 100.0 if peak > 0 else 0.0
    drawdown_bars = max(len(closes) - 1 - peak_idx, 0)
    drawdown_speed = drawdown_pct / drawdown_bars if drawdown_bars > 0 else 0.0

    last_change_pct = (closes[-1] - closes[-2]) / closes[-2] * 100.0 if closes[-2] > 0 else 0.0
    lower_shadow, recovery = _bar_geometry(opens[-1], highs[-1], lows[-1], closes[-1])

    raw = {
        "drawdown_pct": drawdown_pct,
        "drawdown_bars": drawdown_bars,
        "drawdown_speed_pct_per_bar": drawdown_speed,
        "last_change_pct": last_change_pct,
        "lower_shadow_ratio": lower_shadow,
        "intraday_recovery_ratio": recovery,
    }

    # ── 判定 ───────────────────────────────────────────────────────────
    side: str | None = None
    magnitude = 0.0
    used: list[str] = []

    if drawdown_pct <= 0.0:
        # 没有回撤 → 洗盘/派发都无从谈起，中性（注意：不是不可得）
        pass
    elif recovery is None or lower_shadow is None:
        pass
    elif lower_shadow >= params.long_lower_shadow and recovery >= params.recovery_ratio:
        # 下影长 + 收在当日上部 = 低位有承接
        side = SIDE_WASHOUT
        magnitude = min(recovery, 1.0)
        if not (last_change_pct <= -params.sharp_drop_pct or drawdown_speed >= params.sharp_drop_pct):
            # 慢磨形态：可能是洗盘，但「快速下杀 + 立刻收回」才是典型形态
            magnitude *= params.slow_grind_discount
        used += ["lower_shadow_ratio", "intraday_recovery_ratio", "drawdown_pct"]
    elif recovery <= params.weak_recovery_ratio:
        # 收在当日低位、几乎没有下影 = 无人接
        side = SIDE_DISTRIBUTION
        magnitude = min((params.weak_recovery_ratio - recovery) / params.weak_recovery_ratio, 1.0)
        magnitude = max(magnitude, 0.3)  # 明确收在低位就已是有效证据
        used += ["intraday_recovery_ratio", "last_change_pct", "drawdown_pct"]

    evidence = tuple(
        Evidence(field=f, value=raw[f], unit=_UNITS[f], source_key=SOURCE_KEY, as_of=as_of)
        for f in dict.fromkeys(used)
        if raw[f] is not None
    )

    return Component(
        key=DIM_PRICE_ACTION,
        core=True,
        available=True,
        magnitude=float(magnitude),
        side=side,
        weight=weight,
        raw=raw,
        evidence=evidence,
    )


_UNITS = {
    "drawdown_pct": "pct",
    "drawdown_bars": "count",
    "drawdown_speed_pct_per_bar": "pct",
    "last_change_pct": "pct",
    "lower_shadow_ratio": "ratio",
    "intraday_recovery_ratio": "ratio",
}
