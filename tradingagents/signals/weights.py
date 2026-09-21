"""带版本号的判定权重与阈值。

## 为什么所有阈值都必须在这里、而不是写在维度里

判定权重会被 T+3/T+5 回填结果校准（见 design D12/D15）。如果阈值散落在
各维度模块里，校准就只能改代码；集中在这里之后，校准只产出新的
`WashoutWeights` 实例，历史判定靠 `version` 区分而不必重写。

## 关于当前取值的诚实说明

以下是**先验值**，不是经过实测标定的最优值。它们必须带 `version` 上线，
被回放样本推翻时走版本化激活流程。特别地：

* `margin_abstain = 0.15` 的语义是「两侧证据质量差距小于 15% 时差距落在
  单一数据源修正的噪声之内」。它是曲线形状参数，不是判定门槛。
* `confidence_anchors` 是三点锚定映射（margin → confidence），用于让置信度
  成为证据分离度的单调函数，而不是一个凭感觉填的数字。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Mapping

SIGNAL_VERSION = "washout-v1.0.0"

# 维度键（同时也是持久化与校准统计的分组键）
DIM_VOLUME = "volume_contraction"
DIM_PRICE_ACTION = "price_action"
DIM_MA_STRUCTURE = "ma_structure"
DIM_RELATIVE_STRENGTH = "relative_strength"
DIM_CAPITAL = "capital_composition"
DIM_CHIPS = "chip_distribution"
DIM_INTRADAY = "intraday_absorption"

MOD_POSITION = "position"

# 核心维度：任一不可得则必须弃权（design D14）。
# 位置虽然以调制器形式参与（D5），但同样属于「不知道位置就不该给判定」
# 的核心输入，所以列在这里作为可用性要求。
CORE_DIMENSIONS = (
    DIM_VOLUME,
    DIM_PRICE_ACTION,
    MOD_POSITION,
    DIM_RELATIVE_STRENGTH,
)


@dataclass(frozen=True)
class VolumeParams:
    """量能判据阈值。"""

    # 下跌日量能 / 前 5 日均量 ≤ 此值 → 缩量
    contract_threshold: float = 0.7
    # ≥ 此值 → 放量
    expand_threshold: float = 1.3
    # 反抽量能 / 下跌段均量 ≤ 此值 → 反抽无量（缩量歧义指向「无人问津」）
    weak_rebound_threshold: float = 0.7
    # 反抽量能恢复达到 1.0 时给的基础分；恢复越好越高，上限 1.0
    rebound_base_magnitude: float = 0.5
    # 尚未出现反抽时给的「待确认」分
    pending_magnitude: float = 0.5


@dataclass(frozen=True)
class PriceActionParams:
    """价格行为阈值。"""

    # 单日跌幅超过此值（%）视为「急跌」——洗盘的典型形态是快速下杀。
    # 注意它是**幅度加成**条件而非门槛：慢磨下跌同样可能是洗盘，
    # 只是证据更弱（见 slow_grind_discount）。
    sharp_drop_pct: float = 3.0
    # 下影线占比（下影 / 当日振幅）≥ 此值 → 盘中有承接
    long_lower_shadow: float = 0.4
    # 当日跌幅回收率（(收-低)/(高-低)）≥ 此值 → 尾盘收回
    recovery_ratio: float = 0.6
    # 回收率 ≤ 此值 → 收在当日低位、无承接
    weak_recovery_ratio: float = 0.3
    # 无急跌（慢磨）形态的证据折减系数
    slow_grind_discount: float = 0.8


@dataclass(frozen=True)
class PositionParams:
    """位置调制参数（交互项，见 D5）。"""

    # 累计涨幅（window 内）低于此值 → 早期（洗盘更可能）
    early_cum_gain: float = 0.30
    # 高于此值 → 晚期（派发更可能）
    late_cum_gain: float = 0.80
    # 调制因子上下界
    min_factor: float = 0.5
    max_factor: float = 1.5


@dataclass(frozen=True)
class RelativeParams:
    """相对强度与相对换手参数。"""

    # 个股跑赢板块超过此差值（百分点）→ 相对强
    outperformance_pp: float = 0.5
    # 满分所需的超额幅度（百分点）——避免 0.6pp 与 3pp 都给满分
    full_credit_pp: float = 3.0
    # 相对换手：自身换手 / 板块中位换手 低于此值 → broad_contraction
    broad_turnover_ratio: float = 1.0
    # 双基准背离判据（D16）：板块「稳」的收益下界 / 「跌」的上界（百分点）
    board_stable_pp: float = -0.5
    board_falling_pp: float = -1.5
    # 主线「退潮」的收益上界 / 「稳」的下界（百分点）
    mainline_receding_pp: float = -2.0
    mainline_stable_pp: float = -0.5
    # 背离情形的证据强度下限（背离是强信号，不应被弱超额淹没）
    divergence_magnitude: float = 0.7


@dataclass(frozen=True)
class CapitalParams:
    """资金构成参数。"""

    # 超大单净流出占主力净流出比重超过此值 → 机构主导出货
    super_large_dominance: float = 0.5
    # 主力净流出金额绝对值超过此值（元）才算显著
    material_outflow: float = 3.0e7


@dataclass(frozen=True)
class ChipParams:
    """筹码参数。"""

    # 获利盘比例低于此值 → 多数持仓浮亏，抛压释放较充分
    low_profit_ratio: float = 0.5


@dataclass(frozen=True)
class FalsifierParams:
    """证伪条件的阈值来源。"""

    # 次日量比高于此值 + 跌破关键位 → 洗盘假设失效
    invalidate_volume_ratio: float = 1.5
    # 主力净流出超过此值（元）+ 跌破关键位 → 失效
    invalidate_outflow: float = 5.0e7
    # 若干交易日内未收复参考位 → 降级
    reclaim_window_days: int = 3


@dataclass(frozen=True)
class WashoutWeights:
    """一次判定所用的完整权重与阈值集合。

    不可变：证据链要求「同输入同输出」，可变权重会让并发评估互相污染。
    校准产出新实例，而不是就地修改。
    """

    version: str = SIGNAL_VERSION

    volume: VolumeParams = field(default_factory=VolumeParams)
    price_action: PriceActionParams = field(default_factory=PriceActionParams)
    position: PositionParams = field(default_factory=PositionParams)
    relative: RelativeParams = field(default_factory=RelativeParams)
    capital: CapitalParams = field(default_factory=CapitalParams)
    chips: ChipParams = field(default_factory=ChipParams)
    falsifier: FalsifierParams = field(default_factory=FalsifierParams)

    weight_volume_contraction: float = 1.2
    weight_price_action: float = 1.0
    weight_ma_structure: float = 0.8
    weight_relative_strength: float = 1.1
    weight_capital_composition: float = 0.6
    weight_chip_distribution: float = 0.7
    weight_intraday_absorption: float = 1.0

    # 弃权判据：两侧证据质量差距 / 总量 低于此值 → 证据不足（D14）
    margin_abstain: float = 0.15
    # 高置信标签所需的分离度
    margin_high: float = 0.30
    # margin → confidence 的三点锚定（线性分段）
    confidence_anchors: tuple[tuple[float, int], ...] = ((0.15, 40), (0.30, 70), (0.45, 85))

    def weight_for(self, dimension: str) -> float:
        try:
            attr = _WEIGHT_ATTRS[dimension]
        except KeyError as exc:  # pragma: no cover — 编程错误，尽早暴露
            raise KeyError(f"unknown dimension: {dimension!r}") from exc
        return float(getattr(self, attr))

    def with_weight(self, dimension: str, value: float) -> "WashoutWeights":
        return replace(self, **{_WEIGHT_ATTRS[dimension]: value})


_WEIGHT_ATTRS: Mapping[str, str] = {
    DIM_VOLUME: "weight_volume_contraction",
    DIM_PRICE_ACTION: "weight_price_action",
    DIM_MA_STRUCTURE: "weight_ma_structure",
    DIM_RELATIVE_STRENGTH: "weight_relative_strength",
    DIM_CAPITAL: "weight_capital_composition",
    DIM_CHIPS: "weight_chip_distribution",
    DIM_INTRADAY: "weight_intraday_absorption",
}

DEFAULT_WEIGHTS = WashoutWeights()


def confidence_from_margin(margin: float, weights: WashoutWeights = DEFAULT_WEIGHTS) -> int:
    """把证据分离度映射为 0-100 的置信度（分段线性，两端收敛）。

    锚点由权重集提供，因此校准改变锚点时置信度的语义随之更新，而不需要
    改代码。低于第一个锚点返回其置信度（调用方在此之前已弃权）。
    """
    anchors = sorted(weights.confidence_anchors)
    if not anchors:
        return 0
    if margin <= anchors[0][0]:
        return anchors[0][1]
    if margin >= anchors[-1][0]:
        return anchors[-1][1]
    for (m0, c0), (m1, c1) in zip(anchors, anchors[1:]):
        if m0 <= margin <= m1:
            span = m1 - m0
            if span <= 0:
                return c1
            ratio = (margin - m0) / span
            return int(round(c0 + (c1 - c0) * ratio))
    return anchors[-1][1]
