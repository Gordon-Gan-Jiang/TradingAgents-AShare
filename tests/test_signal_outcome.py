"""判定兑现的确定性评估（T+3 / T+5）。

## 为什么兑现定义必须写死成代码

校准闭环的全部价值取决于「对错怎么算」。如果兑现定义含糊（「走势转弱」
「表现不佳」），T+5 回填就无法机械判定，统计出来的命中率也就没有意义——
而那正是权重校准的唯一依据。

## 三条操作性定义

| 判定 | 兑现条件 |
|---|---|
| `washout` | 窗口内最高价 **超过判定日最高价**（确有新高 = 震仓结束后继续上行） |
| `distribution` | 窗口内**未创新高** 且 期末收盘较判定日收盘跌幅 ≥ 阈值 |
| `trend_break` | 窗口内收盘**未能有效收复**判定日收盘（无像样反弹） |

## 两条必须做对的边界

1. **按交易日计数**，不是自然日。T+5 跨过周末与长假时，用自然日会算错窗口。
2. **停牌/退市 → `evaluable=False`**，且**不计命中也不计未命中**。把不可评估的
   样本当成「未命中」会系统性压低命中率，让权重被噪声往错误方向拉。
"""

from __future__ import annotations

import pandas as pd
import pytest


def _bars(highs, closes, start="2026-01-19"):
    dates = pd.bdate_range(start, periods=len(highs)).strftime("%Y-%m-%d").tolist()
    return pd.DataFrame({"date": dates, "high": highs, "close": closes})


# ══════════════════════════════════════════════════════════════════════════
# washout：兑现 = 创出高于判定日高点的新高
# ══════════════════════════════════════════════════════════════════════════


def test_washout_realised_when_new_high_made():
    from tradingagents.signals.outcome import evaluate_outcome

    r = evaluate_outcome(
        state="washout",
        entry_high=10.0,
        entry_close=9.9,
        bars=_bars([10.1, 10.4, 10.2, 10.3, 10.25], [10.0, 10.3, 10.1, 10.2, 10.15]),
        horizon_days=5,
    )
    assert r.evaluable is True
    assert r.max_high == pytest.approx(10.4)
    assert r.threshold_hit is True


def test_washout_not_realised_without_new_high():
    from tradingagents.signals.outcome import evaluate_outcome

    r = evaluate_outcome(
        state="washout",
        entry_high=10.0,
        entry_close=9.9,
        bars=_bars([9.95, 9.98, 9.9, 9.92, 9.88], [9.9, 9.95, 9.85, 9.88, 9.8]),
        horizon_days=5,
    )
    assert r.evaluable is True
    assert r.threshold_hit is False
    # 未创新高不等于「没有信息」——它是一次可评估的否证
    assert r.reason is None or "new_high" in r.reason


def test_washout_new_high_exactly_equal_is_not_a_breakout():
    """恰好等于判定日高点不算创新高：那是平前高，不是有效突破。"""
    from tradingagents.signals.outcome import evaluate_outcome

    r = evaluate_outcome(
        state="washout", entry_high=10.0, entry_close=9.9,
        bars=_bars([10.0, 9.99], [9.95, 9.9]), horizon_days=5,
    )
    assert r.threshold_hit is False


# ══════════════════════════════════════════════════════════════════════════
# distribution：兑现 = 未创新高 且 显著下跌
# ══════════════════════════════════════════════════════════════════════════


def test_distribution_realised_on_continued_decline():
    from tradingagents.signals.outcome import evaluate_outcome

    r = evaluate_outcome(
        state="distribution", entry_high=10.0, entry_close=9.9,
        bars=_bars([9.85, 9.7, 9.6, 9.55, 9.5], [9.8, 9.65, 9.55, 9.5, 9.45]), horizon_days=5,
    )
    assert r.evaluable is True
    assert r.ret_pct < 0
    assert r.threshold_hit is True


def test_distribution_not_realised_when_price_recovers():
    from tradingagents.signals.outcome import evaluate_outcome

    r = evaluate_outcome(
        state="distribution", entry_high=10.0, entry_close=9.9,
        bars=_bars([10.2, 10.5], [10.1, 10.4]), horizon_days=5,
    )
    assert r.threshold_hit is False


def test_distribution_not_realised_on_flat_drift():
    """小幅阴跌不算派发兑现：派发的可检验含义是「显著下行」。"""
    from tradingagents.signals.outcome import evaluate_outcome

    r = evaluate_outcome(
        state="distribution", entry_high=10.0, entry_close=10.0,
        bars=_bars([10.0, 9.99], [9.99, 9.98]), horizon_days=5,
    )
    assert r.threshold_hit is False


# ══════════════════════════════════════════════════════════════════════════
# trend_break：兑现 = 未能有效收复
# ══════════════════════════════════════════════════════════════════════════


def test_trend_break_realised_when_no_recovery():
    from tradingagents.signals.outcome import evaluate_outcome

    r = evaluate_outcome(
        state="trend_break", entry_high=10.0, entry_close=9.5,
        bars=_bars([9.55, 9.5, 9.45, 9.42, 9.4], [9.5, 9.48, 9.4, 9.38, 9.35]), horizon_days=5,
    )
    assert r.threshold_hit is True


def test_trend_break_not_realised_when_price_reclaims():
    from tradingagents.signals.outcome import evaluate_outcome

    r = evaluate_outcome(
        state="trend_break", entry_high=10.0, entry_close=9.5,
        bars=_bars([9.8, 10.1], [9.75, 10.05]), horizon_days=5,
    )
    assert r.threshold_hit is False


# ══════════════════════════════════════════════════════════════════════════
# 不可评估：停牌 / 退市 / 窗口不足
# ══════════════════════════════════════════════════════════════════════════


def test_missing_bars_are_not_evaluable_and_not_counted():
    """停牌导致窗口内 bar 不足 → evaluable=False。

    把它当成「未命中」会系统性压低命中率，再把权重往错误方向拉——
    统计污染比样本变少更糟。
    """
    from tradingagents.signals.outcome import evaluate_outcome

    r = evaluate_outcome(
        state="washout", entry_high=10.0, entry_close=9.9,
        bars=_bars([10.1], [10.05]), horizon_days=5,
    )
    assert r.evaluable is False
    assert r.threshold_hit is False
    assert r.reason and "insufficient" in r.reason


def test_short_window_is_distinguished_from_a_miss():
    """「窗口不完整」与「未兑现」必须给出不同结果。

    前者是数据问题（停牌），后者是市场给出的否证。把两者混为一谈会让命中率
    被数据问题污染——统计污染比样本变少更糟。
    """
    from tradingagents.signals.outcome import evaluate_outcome

    short = evaluate_outcome(
        state="washout", entry_high=10.0, entry_close=9.9,
        bars=_bars([9.9, 9.85], [9.85, 9.8]), horizon_days=5,
    )
    miss = evaluate_outcome(
        state="washout", entry_high=10.0, entry_close=9.9,
        bars=_bars([9.9, 9.85, 9.8, 9.82, 9.79], [9.85, 9.8, 9.78, 9.8, 9.75]),
        horizon_days=5,
    )
    assert short.evaluable is False
    assert miss.evaluable is True
    assert miss.threshold_hit is False


def test_empty_bars_are_not_evaluable():
    from tradingagents.signals.outcome import evaluate_outcome

    r = evaluate_outcome(
        state="washout", entry_high=10.0, entry_close=9.9,
        bars=pd.DataFrame(columns=["date", "high", "close"]), horizon_days=5,
    )
    assert r.evaluable is False
    assert r.reason


def test_abstention_is_never_scored():
    """弃权判定没有可检验的预测，任何兑现定义对它都不适用。"""
    from tradingagents.signals.outcome import evaluate_outcome

    r = evaluate_outcome(
        state="insufficient_evidence", entry_high=10.0, entry_close=9.9,
        bars=_bars([10.1, 10.4], [10.0, 10.3]), horizon_days=5,
    )
    assert r.evaluable is False
    assert r.threshold_hit is False
    assert "abstention" in (r.reason or "")


# ══════════════════════════════════════════════════════════════════════════
# 窗口截断
# ══════════════════════════════════════════════════════════════════════════


def test_window_is_truncated_to_horizon_trading_days():
    """必须只用**窗口内**的 bars：多算了未来数据等于偷看答案。"""
    from tradingagents.signals.outcome import evaluate_outcome

    bars = _bars([10.1, 10.2, 11.0, 12.0], [10.0, 10.1, 10.9, 11.9])
    r3 = evaluate_outcome(state="washout", entry_high=10.0, entry_close=9.9,
                          bars=bars, horizon_days=3)
    r1 = evaluate_outcome(state="washout", entry_high=10.0, entry_close=9.9,
                          bars=bars, horizon_days=1)
    assert r3.max_high == pytest.approx(11.0)
    assert r1.max_high == pytest.approx(10.1)


# ══════════════════════════════════════════════════════════════════════════
# 交易日推进（注入日历，测试里不依赖真实交易日历）
# ══════════════════════════════════════════════════════════════════════════


def test_horizon_uses_trading_days_not_calendar_days():
    """T+5 必须按**交易日**计数：跨周末/长假时自然日会算错窗口。"""
    from tradingagents.signals.outcome import add_trading_days

    # 2026-01-16 是周五 → 5 个交易日后是 2026-01-23（周五）
    assert add_trading_days("2026-01-16", 5) == "2026-01-23"
    assert add_trading_days("2026-01-16", 1) == "2026-01-19"


def test_add_trading_days_supports_negative_offsets():
    """负值必须真的回退。

    早期实现把负数静默当成 0，于是「3 个交易日前」等于当天——不报错，
    只是结果悄悄变错（新闻回看窗口因此退化成只看当天）。
    """
    from tradingagents.signals.outcome import add_trading_days

    # 2026-01-16 周五 往前 5 个交易日 = 2026-01-09 周五
    assert add_trading_days("2026-01-16", -5) == "2026-01-09"
    assert add_trading_days("2026-01-16", -1) == "2026-01-15"
    assert add_trading_days("2026-01-16", 0) == "2026-01-16"
    # 往返一致性：前进 N 再回退 N 必须回到原点
    assert add_trading_days(add_trading_days("2026-01-16", 3), -3) == "2026-01-16"


def test_add_trading_days_backward_is_injectable():
    from tradingagents.signals.outcome import add_trading_days

    calls: list[str] = []

    def fake_prev(day: str) -> str:
        calls.append(day)
        return "2000-01-01"

    assert add_trading_days("2026-01-16", -2, previous_day=fake_prev) == "2000-01-01"
    assert len(calls) == 2


def test_add_trading_days_is_injectable():
    from tradingagents.signals.outcome import add_trading_days

    calls: list[str] = []

    def fake_next(day: str) -> str:
        calls.append(day)
        return "2099-01-01"

    assert add_trading_days("2026-01-16", 2, next_day=fake_next) == "2099-01-01"
    assert len(calls) == 2
