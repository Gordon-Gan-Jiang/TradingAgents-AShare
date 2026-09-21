"""筹码分布维度（获利盘 / 平均成本 / 集中度）。

## 为什么筹码是判别力最强的变量之一，而平台此前完全没用

`stock_cyq_em` 提供的获利盘比例直接刻画**上方抛压的来源**：
* 获利盘低（多数持仓浮亏）→ 想跑的人已经跑得差不多 → 抛压释放较充分 → 洗盘侧
* 获利盘高度拥挤（>85%）→ 随时可能兑现 → 派发侧

它比量价形态更接近「谁在什么价位持有」这个本质问题。此前全仓库无
`cyq` / 筹码相关代码，属于能力空白而非「已经有了」。

## 为什么只有成本没有获利比例算「不可得」

平均成本本身不携带方向信息——必须与现价比较才知道是浮盈还是浮亏，而
「浮盈浮亏的分布」才是抛压的度量。用单一成本值替代会伪造出一个
不存在的判据，因此显式标为不可得。
"""

from __future__ import annotations

from .context import SignalContext
from .types import SIDE_DISTRIBUTION, SIDE_WASHOUT, Component, Evidence
from .weights import DEFAULT_WEIGHTS, DIM_CHIPS, ChipParams, WashoutWeights

SOURCE_KEY = "chip_distribution"

# 获利盘被认为「拥挤」的下界。它与 ChipParams.low_profit_ratio 之间的区间是
# 中性带——中间地带不做方向判断，避免把噪声当信号。
CROWDED_PROFIT_RATIO = 0.85


def _chip_source(chips: Any) -> str:
    """取筹码数据的来源标记，兼容 DataFrame 与 Series（见 compute 里的说明）。"""
    if chips is None:
        return ""
    try:
        raw = chips.get("source")
    except Exception:  # noqa: BLE001 — 没有该列就是没有来源标记
        return ""
    if raw is None:
        return ""
    if hasattr(raw, "iloc"):
        try:
            return str(raw.iloc[-1])
        except Exception:  # noqa: BLE001 — 空序列
            return ""
    return str(raw)


def compute(ctx: SignalContext, weights: WashoutWeights = DEFAULT_WEIGHTS) -> Component:
    params: ChipParams = weights.chips
    weight = weights.weight_for(DIM_CHIPS)
    as_of = ctx.as_of

    raw: dict[str, float | None] = {
        "profit_ratio": None,
        "avg_cost": None,
        "concentration_90": None,
        "concentration_70": None,
        "price_vs_avg_cost": None,
    }

    def unavailable(reason: str) -> Component:
        return Component(
            key=DIM_CHIPS,
            core=False,
            available=False,
            magnitude=0.0,
            side=None,
            weight=weight,
            raw=raw,
            evidence=(),
            unavailable_reason=reason,
        )

    if not ctx.has_chips:
        return unavailable("chip distribution unavailable")

    df = ctx.chips
    row = df.iloc[-1]

    def _num(col: str) -> float | None:
        if col not in df.columns:
            return None
        v = row[col]
        if v is None or v != v:
            return None
        return float(v)

    raw["profit_ratio"] = _num("profit_ratio")
    raw["avg_cost"] = _num("avg_cost")
    raw["concentration_90"] = _num("concentration_90")
    raw["concentration_70"] = _num("concentration_70")

    if raw["profit_ratio"] is None:
        return unavailable("profit ratio missing")

    # 来源标记：`cyq_model_estimate` 表示这是**自算的模型估计**（东财被封时
    # 用日线 + 换手率算出来的），不是交易所原始数据。标记会一路传到回答。
    #
    # 取法要兼容 DataFrame 与 Series：`ctx.chips` 在类型上是 DataFrame，但维度
    # 计算里按"最后一行"的语义使用。用 DataFrame 的 `.get("source")` 会拿到
    # 一个 Series，`str(Series)` 永远不以 `model_estimate` 结尾 —— 那样标记会
    # **静默失效**，估计值就又被当成数据了。
    estimated = _chip_source(ctx.chips).endswith("model_estimate")

    last_close = float(ctx.daily["close"].iloc[-1])
    if raw["avg_cost"] is not None and raw["avg_cost"] > 0:
        raw["price_vs_avg_cost"] = (last_close - raw["avg_cost"]) / raw["avg_cost"]

    side: str | None = None
    magnitude = 0.0
    used: list[str] = []

    profit = raw["profit_ratio"]
    if profit <= params.low_profit_ratio:
        side = SIDE_WASHOUT
        magnitude = min((params.low_profit_ratio - profit) / params.low_profit_ratio, 1.0)
        magnitude = max(magnitude, 0.3)
        used.append("profit_ratio")
        if raw["price_vs_avg_cost"] is not None and raw["price_vs_avg_cost"] < 0:
            used.append("price_vs_avg_cost")
    elif profit >= CROWDED_PROFIT_RATIO:
        side = SIDE_DISTRIBUTION
        magnitude = min((profit - CROWDED_PROFIT_RATIO) / (1.0 - CROWDED_PROFIT_RATIO), 1.0)
        magnitude = max(magnitude, 0.3)
        used.append("profit_ratio")
        if raw["avg_cost"] is not None:
            used.append("avg_cost")

    evidence = tuple(
        Evidence(field=f, value=raw[f], unit=_UNITS[f], source_key=SOURCE_KEY, as_of=as_of)
        for f in dict.fromkeys(used)
        if raw[f] is not None
    )

    return Component(
        key=DIM_CHIPS,
        core=False,
        available=True,
        magnitude=float(magnitude),
        side=side,
        weight=weight,
        raw=raw,
        evidence=evidence,
        estimated=estimated,
    )


_UNITS = {
    "profit_ratio": "ratio",
    "avg_cost": "yuan",
    "concentration_90": "ratio",
    "concentration_70": "ratio",
    "price_vs_avg_cost": "ratio",
}
