"""离线回放与阈值标定的**纯逻辑**部分（design D15）。

## 为什么回放的权限必须被代码约束

离线回放有同源偏差：同一份代码既选样本又调参。回放上「调好」的权重可能只是
过拟合了那段行情。因此规格把用途切开：

| 样本 | 可以做什么 | 不可以做什么 |
|---|---|---|
| 离线回放 | **否决**无判别力的维度 | 不能**确认**权重 |
| 前瞻积累 | **确认**并激活权重 | — |

否决一个维度是稳健结论（「它在这批样本上和结果无关」不太可能是过拟合）；
确认权重需要独立样本。这条切分如果不写成代码，几周后就会有人跳过它。

## 为什么按时间切分

金融数据有强自相关：随机切分会让同一波行情同时出现在调参与报告两侧，
测出来的准确率是泄漏出来的。因此切分按时间排序，且**同一天的样本整组落在
同一侧**——否则同一天的多个判定被拆到两侧，仍然泄漏。

## 为什么取平台区而不是最大值

取最大值会把阈值钉在一个由个别样本造成的尖峰上。平台区意味着该区间内结论
不敏感（±容差扰动下命中率不变），可以放心选取。**找不到平台区时不得调参**：
那说明维度集本身没有判别力，调阈值只是把噪声挪个位置。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean
from typing import Any, Iterable, Mapping, Sequence

# 权重激活闸门（design D15）
MIN_FORWARD_VERDICTS = 100
MIN_PER_DIMENSION_SAMPLES = 30
# 单维度「有判别力」的最低样本量：低于此值不给结论（不是给 False）
MIN_DIMENSION_SAMPLES = 10

OFFLINE_REPLAY_TARGET = 500
DEFAULT_TRAIN_RATIO = 0.7


@dataclass(frozen=True)
class ReplaySample:
    """一次回放判定 + 它的兑现结果。"""

    symbol: str
    as_of: str
    state: str
    margin: float
    confidence: int
    evaluable: bool
    threshold_hit: bool
    contributions: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class CurvePoint:
    threshold: float
    sample: int
    hit_rate: float
    abstention_rate: float
    not_evaluable: int = 0


@dataclass(frozen=True)
class PlateauResult:
    found: bool
    threshold: float | None = None
    stable_low: float | None = None
    stable_high: float | None = None
    reason: str = ""


@dataclass(frozen=True)
class ActivationGate:
    allowed: bool
    reason: str
    have_forward: int = 0
    needed_forward: int = MIN_FORWARD_VERDICTS
    blocking_dimensions: tuple[str, ...] = ()


def time_split(
    samples: Sequence[ReplaySample], train_ratio: float = DEFAULT_TRAIN_RATIO
) -> tuple[list[ReplaySample], list[ReplaySample]]:
    """按时间把样本切成「调参」与「报告」两段。

    同一 `as_of` 的样本整组落在同一侧：否则同一天的多个判定被拆到两侧，
    自相关仍然会泄漏。
    """
    if not samples:
        return [], []

    ordered = sorted(samples, key=lambda s: s.as_of)
    unique_dates = sorted({s.as_of for s in ordered})
    if len(unique_dates) == 1:
        # 只有一天的数据无法切分：全部作为调参段，报告段为空，
        # 由调用方看到 holdout 样本不足而不是拿到一个假的准确率
        return list(ordered), []

    cut_index = max(int(len(unique_dates) * train_ratio), 1)
    cut_index = min(cut_index, len(unique_dates) - 1)
    boundary = unique_dates[cut_index]

    train = [s for s in ordered if s.as_of < boundary]
    holdout = [s for s in ordered if s.as_of >= boundary]
    return train, holdout


def calibration_curve(
    samples: Sequence[ReplaySample], thresholds: Iterable[float]
) -> list[CurvePoint]:
    """扫阈值得到「命中率 vs 弃权率」曲线。

    * 不可评估样本**不进命中率分母**（停牌不得压低准确率），单列计数
    * 低于阈值的样本视为弃权，不进命中率分母
    """
    evaluable = [s for s in samples if s.evaluable]
    total = len(samples) or 1

    points: list[CurvePoint] = []
    for threshold in sorted(thresholds):
        kept = [s for s in evaluable if s.margin >= threshold]
        hits = sum(1 for s in kept if s.threshold_hit)
        points.append(
            CurvePoint(
                threshold=float(threshold),
                sample=len(kept),
                hit_rate=(hits / len(kept)) if kept else 0.0,
                abstention_rate=(len(samples) - len(kept)) / total,
                not_evaluable=len(samples) - len(evaluable),
            )
        )
    return points


def find_plateau(
    curve: Sequence[CurvePoint],
    *,
    tolerance: float = 0.2,
    min_sample: int = 10,
) -> PlateauResult:
    """在曲线上寻找稳定平台区，返回其中的阈值。

    判据：在候选阈值附近 ±`tolerance` 的相对区间内，相邻点的命中率变化都很小；
    且候选点及其邻点样本量不低于 `min_sample`（挡掉「2 个样本命中率 100%」的尖峰）。
    """
    usable = [p for p in curve if p.sample >= min_sample]
    if len(usable) < 2:
        return PlateauResult(
            found=False,
            reason="not enough samples above min_sample to locate a plateau; dimension set may lack power",
        )

    # 平台区判据：相邻点的命中率变化不超过容差。
    # 刻意用「命中率的绝对变化」而不是自造的相对跳跃公式——后者难以验证，
    # 且容易在阈值量级不同时给出违反直觉的结果。
    stable_runs: list[list[CurvePoint]] = []
    current: list[CurvePoint] = [usable[0]]
    for point in usable[1:]:
        previous = current[-1]
        if abs(point.hit_rate - previous.hit_rate) <= tolerance:
            current.append(point)
        else:
            stable_runs.append(current)
            current = [point]
    stable_runs.append(current)

    best = max(stable_runs, key=len)
    if len(best) < 2:
        return PlateauResult(
            found=False,
            reason="no stable plateau on the curve; the dimension set lacks discriminative power, do not tune thresholds",
        )

    # 取平台区中位点：平台区两端的样本量通常较少，中位点更稳
    chosen = best[len(best) // 2]
    return PlateauResult(
        found=True,
        threshold=chosen.threshold,
        stable_low=best[0].threshold,
        stable_high=best[-1].threshold,
        reason=f"plateau spans {best[0].threshold}~{best[-1].threshold}",
    )


def dimension_power(
    samples: Sequence[ReplaySample],
    *,
    min_sample: int = MIN_DIMENSION_SAMPLES,
) -> dict[str, dict[str, Any]]:
    """按维度统计判别力。

    `discriminative` 三态：
    * `True` / `False`：样本足够时的结论
    * `None`：**样本不足，不给结论**。把它当成 False 会误杀一个只是样本少的
      有效维度——这正是「样本饥饿」的典型表现。
    """
    keys: list[str] = []
    for sample in samples:
        for key in sample.contributions:
            if key not in keys:
                keys.append(key)

    report: dict[str, dict[str, Any]] = {}
    for key in keys:
        rows = [
            (sample.contributions[key], sample.evaluable, sample.threshold_hit)
            for sample in samples
            if key in sample.contributions and sample.evaluable
        ]
        if len(rows) < min_sample:
            # 样本不足时**不给结论**（None），而不是给 False——后者会误杀一个
            # 只是样本少的有效维度，这正是「样本饥饿」的典型表现
            report[key] = {
                "sample": len(rows),
                "discriminative": None,
                "reason": f"insufficient sample ({len(rows)} < {min_sample})",
            }
            continue

        positive = [hit for contrib, _, hit in rows if contrib > 0]
        negative = [hit for contrib, _, hit in rows if contrib < 0]
        if not positive or not negative:
            report[key] = {
                "sample": len(rows),
                "discriminative": None,
                "reason": "contributions are one-sided; cannot assess discrimination",
            }
            continue

        pos_rate = mean(1.0 if h else 0.0 for h in positive)
        neg_rate = mean(1.0 if h else 0.0 for h in negative)
        hit_rate = mean(1.0 if hit else 0.0 for _, _, hit in rows)
        report[key] = {
            "sample": len(rows),
            "hit_rate": hit_rate,
            "positive_hit_rate": pos_rate,
            "negative_hit_rate": neg_rate,
            "spread": pos_rate - neg_rate,
            "discriminative": pos_rate > neg_rate,
            "reason": "" if pos_rate > neg_rate else "contribution does not separate outcomes",
        }
    return report


def replay_score(
    *,
    components: Sequence[Mapping[str, Any]],
    modulator_factor: float,
) -> dict[str, float]:
    """从**存档的**权重与观测重算三侧得分。

    这是「权重快照可重放」的可执行形式：若存档缺少调制因子（例如只存了 raw
    观测），重算结果就会与原始判定不一致——而那种不一致不会报错，只会让
    校准统计建立在一个与历史判定不同的模型上。

    重算规则必须与 `washout.aggregate_masses` 完全一致：位置是乘法调制，
    且趋势破坏侧不受调制。
    """
    factor = modulator_factor if modulator_factor > 0 else 1.0
    sides = {"washout": 0.0, "distribution": 0.0, "trend_break": 0.0}
    for component in components:
        if not component.get("available"):
            continue
        side = component.get("side")
        if side not in sides:
            continue
        sides[side] += float(component.get("weight") or 0.0) * float(
            component.get("magnitude") or 0.0
        )
    sides["washout"] *= factor
    sides["distribution"] *= 1.0 / factor
    return sides


def non_discriminative_dimensions(report: Mapping[str, Mapping[str, Any]]) -> tuple[str, ...]:
    """可被离线回放**否决**的维度（只含样本足够且判别力为 False 的）。"""
    return tuple(
        key for key, stats in report.items() if stats.get("discriminative") is False
    )


def offline_authority() -> dict[str, Any]:
    """离线回放的权限边界。写成函数而不是注释，便于被测试与端点暴露。"""
    return {
        "may_reject_dimensions": True,
        "may_activate_weights": False,
        "target_samples": OFFLINE_REPLAY_TARGET,
        "reason": (
            "同一份代码既选样本又调参，回放上的最优可能只是过拟合那段行情。"
            "否决一个维度是稳健结论；激活权重必须由前瞻样本支持。"
        ),
    }


def check_activation_gate(
    *,
    offline_samples: int,
    forward_samples: int,
    per_dimension: Mapping[str, int],
    min_forward: int = MIN_FORWARD_VERDICTS,
    min_per_dimension: int = MIN_PER_DIMENSION_SAMPLES,
) -> ActivationGate:
    """权重激活闸门。样本不足时**拒绝改权重**并报告差距。"""
    blocking = tuple(
        sorted(key for key, count in per_dimension.items() if count < min_per_dimension)
    )
    if forward_samples < min_forward:
        return ActivationGate(
            allowed=False,
            reason=(
                f"forward samples {forward_samples} < {min_forward}; "
                "offline replay may only reject dimensions, not activate weights"
            ),
            have_forward=forward_samples,
            needed_forward=min_forward,
            blocking_dimensions=blocking,
        )
    if blocking:
        return ActivationGate(
            allowed=False,
            reason=f"dimensions below per-dimension minimum: {list(blocking)}",
            have_forward=forward_samples,
            needed_forward=min_forward,
            blocking_dimensions=blocking,
        )
    return ActivationGate(
        allowed=True,
        reason="forward sample gate satisfied",
        have_forward=forward_samples,
        needed_forward=min_forward,
    )
