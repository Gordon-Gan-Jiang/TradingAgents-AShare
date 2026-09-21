"""位置调制器（**不是**独立加权维度，见 design D5）。

## 为什么位置必须做乘法而不是加法

同样一根「缩量阴线」：
* 发生在启动初期（累计涨幅 < 30%）→ 中继洗盘
* 发生在翻倍高位（累计涨幅 > 80%）→ 派发前兆

如果把位置当成第 8 个独立维度线性相加，这两种情况会得到**相近的总分**——
因为缩量这一项的贡献完全一样，位置项只贡献一个固定的小偏移。这不是调参
能解决的问题，是模型结构错了。

乘法调制让位置成为量价证据的**情境放大器**：
    score_washout      = factor × (洗盘侧证据质量)
    score_distribution = (1/factor) × (派发侧证据质量)

早期 factor=1.5 → 洗盘侧证据被放大、派发侧被压缩；晚期相反。

## 为什么累计涨幅用窗口首日收盘、前高用窗口最高收盘

用收盘价而非最高价，是为了避免单根插针（盘中冲高回落）把「距前高」这个
观测量打成噪声。位置是慢变量，不该被日内波动主导。
"""

from __future__ import annotations

from .context import SignalContext
from .types import Evidence, Modulator
from .weights import DEFAULT_WEIGHTS, MOD_POSITION, PositionParams, WashoutWeights

SOURCE_KEY = "stock_data"


def compute(ctx: SignalContext, weights: WashoutWeights = DEFAULT_WEIGHTS) -> Modulator:
    params: PositionParams = weights.position
    df = ctx.daily
    as_of = ctx.as_of

    closes = [float(c) for c in df["close"].tolist()]
    base = closes[0]
    last = closes[-1]
    prior_high = max(closes)
    window_low = min(closes)

    def unavailable(reason: str) -> Modulator:
        return Modulator(
            key=MOD_POSITION,
            available=False,
            raw={
                "cum_gain": None,
                "prior_high": None,
                "distance_to_prior_high": None,
                "advance_days": None,
                "window_low": None,
            },
            factor=1.0,
            evidence=(),
            unavailable_reason=reason,
        )

    if not closes or base <= 0:
        return unavailable("no usable close prices")

    cum_gain = last / base - 1.0
    distance_to_prior_high = (prior_high - last) / prior_high if prior_high > 0 else 0.0
    advance_days = sum(1 for i in range(1, len(closes)) if closes[i] > closes[i - 1])

    # ── 调制因子：对累计涨幅单调不增 ────────────────────────────────────
    if cum_gain <= params.early_cum_gain:
        factor = params.max_factor
    elif cum_gain >= params.late_cum_gain:
        factor = params.min_factor
    else:
        span = params.late_cum_gain - params.early_cum_gain
        t = (cum_gain - params.early_cum_gain) / span if span > 0 else 1.0
        factor = params.max_factor + (params.min_factor - params.max_factor) * t

    raw = {
        "cum_gain": cum_gain,
        "prior_high": prior_high,
        "distance_to_prior_high": distance_to_prior_high,
        "advance_days": advance_days,
        "window_low": window_low,
    }
    evidence = (
        Evidence(field="cum_gain", value=cum_gain, unit="ratio", source_key=SOURCE_KEY, as_of=as_of),
        Evidence(
            field="distance_to_prior_high",
            value=distance_to_prior_high,
            unit="ratio",
            source_key=SOURCE_KEY,
            as_of=as_of,
        ),
        Evidence(field="advance_days", value=advance_days, unit="count", source_key=SOURCE_KEY, as_of=as_of),
    )

    return Modulator(
        key=MOD_POSITION,
        available=True,
        raw=raw,
        factor=float(factor),
        evidence=evidence,
    )
