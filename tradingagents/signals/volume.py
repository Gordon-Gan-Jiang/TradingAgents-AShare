"""量能维度：缩量/放量与反抽量能恢复率。

## 为什么这个维度需要两条观测量而不是一条

「缩量」是洗盘判据里最容易被误用的一条：**缩量本身有致命歧义**——
它既可能是主力锁仓震仓（筹码没换手），也可能是流动性枯竭无人问津
（根本没人要）。两种情况在「成交量 / 5 日均量」这一个数字上完全一致。

区分办法只有一个：**看反抽时量能是否回来**。
* 缩量下跌 + 反抽放量  → 有人在接 → 洗盘侧证据
* 缩量下跌 + 反抽无量  → 无人问津 → **不给洗盘分**（也不给派发分：派发要放量）
* 放量下跌             → 主动出货 → 派发侧证据

第三、四种形态的差别正是这个模块存在的理由。
"""

from __future__ import annotations

import pandas as pd

from .context import SignalContext
from .types import SIDE_DISTRIBUTION, SIDE_WASHOUT, Component, Evidence
from .weights import DIM_VOLUME, VolumeParams, WashoutWeights
from .weights import DEFAULT_WEIGHTS

SOURCE_KEY = "stock_data"

# 计算「相对 5 日均量」时需要的回看根数
_LOOKBACK = 5


def _down_flags(closes: list[float]) -> list[bool]:
    """逐根标记阴线。首根无前收，视为非阴线。"""
    return [False] + [closes[i] < closes[i - 1] for i in range(1, len(closes))]


def _up_flags(closes: list[float]) -> list[bool]:
    return [False] + [closes[i] > closes[i - 1] for i in range(1, len(closes))]


def _volume_ratio_5d(volumes: list[float]) -> float | None:
    """末根量 / 前 5 根均量。回看不足则返回 None。"""
    if len(volumes) < _LOOKBACK + 1:
        return None
    prior = volumes[-(_LOOKBACK + 1):-1]
    mean = sum(prior) / len(prior)
    if mean <= 0:
        return None
    return volumes[-1] / mean


def _tail_down_run(down: list[bool]) -> int:
    """末尾连续阴线根数（末根必须是阴线才有意义）。"""
    n = 0
    for flag in reversed(down):
        if not flag:
            break
        n += 1
    return n


def _most_recent_down_index(down: list[bool]) -> int | None:
    for i in range(len(down) - 1, -1, -1):
        if down[i]:
            return i
    return None


def compute(ctx: SignalContext, weights: WashoutWeights = DEFAULT_WEIGHTS) -> Component:
    """计算量能组件。纯函数：只依赖 ctx 与 weights。"""
    params: VolumeParams = weights.volume
    weight = weights.weight_for(DIM_VOLUME)

    df: pd.DataFrame = ctx.daily
    volumes = [float(v) for v in df["volume"].tolist()]
    closes = [float(c) for c in df["close"].tolist()]

    def unavailable(reason: str) -> Component:
        return Component(
            key=DIM_VOLUME,
            core=True,
            available=False,
            magnitude=0.0,
            side=None,
            weight=weight,
            raw={
                "volume_ratio_5d": None,
                "downside_volume_ratio_5d": None,
                "rebound_volume_recovery": None,
                "downside_volume_share": None,
                "tail_down_bars": None,
            },
            evidence=(),
            unavailable_reason=reason,
        )

    # 量能是洗盘判据的主干，缺它就没有判定可言 —— 显式不可得，而不是当 0
    if any(v is None or pd.isna(v) for v in volumes):
        return unavailable("daily volume contains missing values")
    if len(volumes) < _LOOKBACK + 1:
        return unavailable(f"need >= {_LOOKBACK + 1} bars for volume ratio")
    total = sum(volumes)
    if total <= 0:
        return unavailable("total volume is zero")

    down = _down_flags(closes)
    up = _up_flags(closes)
    as_of = ctx.as_of

    # ── 原始观测量 ─────────────────────────────────────────────────────
    volume_ratio_5d = _volume_ratio_5d(volumes)

    downside_volume_ratio_5d: float | None = None
    down_idx = _most_recent_down_index(down)
    if down_idx is not None and down_idx >= _LOOKBACK:
        prior = volumes[down_idx - _LOOKBACK:down_idx]
        mean = sum(prior) / len(prior)
        if mean > 0:
            downside_volume_ratio_5d = volumes[down_idx] / mean

    tail_down = _tail_down_run(down)
    rebound_volume_recovery: float | None = None
    if up[-1] and tail_down == 0:
        # 末根为阳线：反抽段 = 它之前连续阴线的那一段
        run = 0
        for flag in reversed(down[:-1]):
            if not flag:
                break
            run += 1
        if run > 0:
            ref = volumes[len(volumes) - 1 - run:len(volumes) - 1]
            mean = sum(ref) / len(ref)
            if mean > 0:
                rebound_volume_recovery = volumes[-1] / mean

    downside_volume_share = sum(v for v, d in zip(volumes, down) if d) / total

    raw = {
        "volume_ratio_5d": volume_ratio_5d,
        "downside_volume_ratio_5d": downside_volume_ratio_5d,
        "rebound_volume_recovery": rebound_volume_recovery,
        "downside_volume_share": downside_volume_share,
        "tail_down_bars": tail_down,
    }

    # ── 判定 ───────────────────────────────────────────────────────────
    side: str | None = None
    magnitude = 0.0
    used: list[str] = []

    if down[-1]:
        # 仍在下杀途中
        if downside_volume_ratio_5d is None:
            return unavailable("no down bar with enough preceding bars to size volume")
        if downside_volume_ratio_5d <= params.contract_threshold:
            # 缩量下杀，但**反抽尚未发生** → 只能给「待确认」分
            side = SIDE_WASHOUT
            magnitude = params.pending_magnitude
            used += ["downside_volume_ratio_5d", "tail_down_bars"]
        elif downside_volume_ratio_5d >= params.expand_threshold:
            side = SIDE_DISTRIBUTION
            overshoot = (downside_volume_ratio_5d - params.expand_threshold) / params.expand_threshold
            magnitude = min(overshoot, 1.0)
            used += ["downside_volume_ratio_5d", "tail_down_bars"]
    else:
        # 末根为阳线：必须有反抽可比对象
        if rebound_volume_recovery is None:
            return unavailable("last bar is up but no preceding down run to compare against")
        if rebound_volume_recovery >= 1.0:
            side = SIDE_WASHOUT
            magnitude = min(
                params.rebound_base_magnitude + (rebound_volume_recovery - 1.0),
                1.0,
            )
            used += ["rebound_volume_recovery"]
        elif rebound_volume_recovery <= params.weak_rebound_threshold:
            # ★ D6 的核心分支：缩量下跌 + 反抽无量 → 不给洗盘分
            side = None
            magnitude = 0.0
            used += ["rebound_volume_recovery"]

    evidence = tuple(
        Evidence(field=field, value=raw[field], unit=_UNITS[field], source_key=SOURCE_KEY, as_of=as_of)
        for field in dict.fromkeys(used)
        if raw[field] is not None
    )

    return Component(
        key=DIM_VOLUME,
        core=True,
        available=True,
        magnitude=float(magnitude),
        side=side,
        weight=weight,
        raw=raw,
        evidence=evidence,
    )


_UNITS = {
    "volume_ratio_5d": "ratio",
    "downside_volume_ratio_5d": "ratio",
    "rebound_volume_recovery": "ratio",
    "downside_volume_share": "ratio",
    "tail_down_bars": "count",
}
