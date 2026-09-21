"""判定兑现的确定性评估（T+3 / T+5）。

## 为什么兑现定义必须写死在代码里

校准闭环的全部价值取决于「对错怎么算」。如果兑现定义含糊（「走势转弱」
「表现不佳」），T+5 回填就无法机械判定，算出来的命中率也就没有意义——
而它是权重校准的唯一依据。因此这里把三种判定的操作性定义写成纯函数，
可穷举、可单测、可回放。

## 两条必须做对的边界

1. **按交易日计数**，不是自然日。T+5 跨过周末与长假时用自然日会算错窗口。
2. **停牌/退市/窗口不足 → `evaluable=False`**，且**不计命中也不计未命中**。
   把不可评估样本当成「未命中」会系统性压低命中率，把权重往错误方向拉——
   统计污染比样本变少更糟。

## 这些阈值是先验值

`distribution_ret_threshold_pct` 与 `trend_break_reclaim_pct` 是**形状参数**，
带版本号上线，由回放样本标定（见 design D12/D14）。它们不是「拍出来的真理」，
但必须有确定取值，否则无法机械判定对错。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Callable

import pandas as pd

# 判定状态（与 signals.types 保持一致，这里不 import 以避免循环）
STATE_WASHOUT = "washout"
STATE_DISTRIBUTION = "distribution"
STATE_TREND_BREAK = "trend_break"
STATE_INSUFFICIENT = "insufficient_evidence"
DIRECTIONAL_STATES = (STATE_WASHOUT, STATE_DISTRIBUTION, STATE_TREND_BREAK)

OUTCOME_VERSION = "outcome-v1.0.0"


@dataclass(frozen=True)
class OutcomeParams:
    """兑现判据的参数。变更时必须同时更新 `OUTCOME_VERSION`。"""

    version: str = OUTCOME_VERSION
    # 派发兑现所需的期末跌幅（%，相对判定日收盘）
    distribution_ret_threshold_pct: float = 3.0
    # 趋势破坏「有效收复」的幅度门槛（%，相对判定日收盘）
    trend_break_reclaim_pct: float = 1.0


DEFAULT_OUTCOME_PARAMS = OutcomeParams()


@dataclass(frozen=True)
class OutcomeResult:
    evaluable: bool
    threshold_hit: bool
    max_high: float | None = None
    close: float | None = None
    ret_pct: float | None = None
    reason: str | None = None
    bars_used: int = 0


def _next_day_default(day: str) -> str:
    from tradingagents.dataflows.trade_calendar import next_cn_trading_day

    return next_cn_trading_day(day) or day


def _previous_day_default(day: str) -> str:
    from tradingagents.dataflows.trade_calendar import previous_cn_trading_day

    return previous_cn_trading_day(day) or day


def add_trading_days(
    start: str,
    days: int,
    *,
    next_day: Callable[[str], str] | None = None,
    previous_day: Callable[[str], str] | None = None,
) -> str:
    """按**交易日**推进，负值表示回退。

    `next_day` / `previous_day` 可注入，使测试不依赖真实交易日历。

    早期实现写成 `range(max(int(days), 0))`，于是**负数被静默当成 0**：
    调用方以为拿到「3 个交易日前」，实际拿到当天。这个错误不报错、只是结果
    悄悄变错——新闻回看窗口因此退化成「只看当天」，所有历史条目被排除。
    """
    count = int(days)
    if count >= 0:
        step = next_day or _next_day_default
        current = start
        for _ in range(count):
            current = step(current)
        return current

    back = previous_day or _previous_day_default
    current = start
    for _ in range(-count):
        current = back(current)
    return current


def _not_evaluable(reason: str, *, bars_used: int = 0) -> OutcomeResult:
    return OutcomeResult(evaluable=False, threshold_hit=False, reason=reason, bars_used=bars_used)


def evaluate_outcome(
    *,
    state: str,
    entry_high: float,
    entry_close: float,
    bars: pd.DataFrame,
    horizon_days: int,
    params: OutcomeParams = DEFAULT_OUTCOME_PARAMS,
) -> OutcomeResult:
    """评估一个判定在给定窗口上的兑现情况。

    `bars` 必须是**判定日之后**的行情（不含判定日），按时间升序。
    只使用前 `horizon_days` 根——多算等于偷看答案。
    """
    if state == STATE_INSUFFICIENT:
        # 弃权没有可检验的预测，任何兑现定义对它都不适用
        return _not_evaluable("abstention carries no testable prediction")
    if state not in DIRECTIONAL_STATES:
        return _not_evaluable(f"unknown state: {state}")

    if bars is None or len(bars) == 0:
        return _not_evaluable("insufficient window: no bars after the evaluation date")

    window = bars.head(max(int(horizon_days), 1))
    if len(window) < int(horizon_days):
        # 停牌/退市：窗口不完整，既不算命中也不算未命中
        return _not_evaluable(
            f"insufficient window: expected {horizon_days} bars, got {len(window)}",
            bars_used=len(window),
        )

    try:
        highs = [float(v) for v in window["high"].tolist()]
        closes = [float(v) for v in window["close"].tolist()]
    except (KeyError, TypeError, ValueError) as exc:
        return _not_evaluable(f"malformed window: {type(exc).__name__}")

    max_high = max(highs)
    last_close = closes[-1]
    ret_pct = (last_close - entry_close) / entry_close * 100.0 if entry_close else None

    if state == STATE_WASHOUT:
        # 震仓结束后继续上行 → 创出高于判定日高点的**新高**。
        # 恰好等于不算：那是平前高，不是有效突破。
        hit = max_high > entry_high
        reason = None if hit else "no_new_high"
    elif state == STATE_DISTRIBUTION:
        no_new_high = max_high <= entry_high
        hit = bool(no_new_high and ret_pct is not None
                   and ret_pct <= -params.distribution_ret_threshold_pct)
        reason = None if hit else ("made_new_high" if not no_new_high else "decline_below_threshold")
    else:  # STATE_TREND_BREAK
        # 结构已坏的可检验含义：窗口内未能有效收复判定日收盘
        reclaim_level = entry_close * (1.0 + params.trend_break_reclaim_pct / 100.0)
        hit = max(closes) < reclaim_level
        reason = None if hit else "price_reclaimed"

    return OutcomeResult(
        evaluable=True,
        threshold_hit=hit,
        max_high=max_high,
        close=last_close,
        ret_pct=ret_pct,
        reason=reason,
        bars_used=len(window),
    )


def with_threshold(params: OutcomeParams, **overrides: Any) -> OutcomeParams:
    """生成参数变体（校准与测试用）。变更即产生新的形状参数取值。"""
    return replace(params, **overrides)
