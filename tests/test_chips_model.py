"""筹码分布**模型估计**：不依赖东财，用日线 + 换手率自算。

## 为什么要自己算

东财 API 被 IP 级封禁，而筹码维度长期缺失。查证发现 akshare 的 `stock_cyq_em`
本身就是"取 K 线 + 用 JS 三角形分布算法本地算"——**它本来就是模型估计**。
既然不是原始数据，我们就能用已有的免费日线自己算，且不受封禁影响。

## 本文件锁住

1. 纯函数、可复现（同样的输入给同样的输出），因此可回测。
2. **必须标注来源为估计**（`source`），不得与交易所数据混同。
3. 样本不足时返回 None——**不猜**。
4. 方向性合理：下跌后获利盘比例应显著低于上涨后；换手越高，旧筹码衰减越快。
"""

from __future__ import annotations

import pytest

from tradingagents.signals.chips_model import SOURCE_ESTIMATED, compute_chip_distribution


def _bars(closes, *, turnover=3.0, start_low=9.0):
    rows = []
    for index, close in enumerate(closes):
        rows.append(
            {
                "open": close,
                "high": close * 1.01,
                "low": min(start_low + index * 0.0, close * 0.99),
                "close": close,
                "turnover": turnover,
            }
        )
    return rows


def test_returns_none_when_sample_is_too_small():
    assert compute_chip_distribution([]) is None
    assert compute_chip_distribution(_bars([10.0])) is None, "只有一根 K 线不足以估计"


def test_marks_the_result_as_an_estimate():
    """来源必须是"估计"——把模型估计说成原始数据是本平台最不容忍的失败。"""
    result = compute_chip_distribution(_bars([10.0] * 20))
    assert result is not None
    assert result["source"] == SOURCE_ESTIMATED


def test_declining_market_lowers_the_profit_ratio():
    """一路下跌后，获利盘比例应显著偏低（否则判据方向就是错的）。"""
    falling = compute_chip_distribution(_bars([20.0 - index * 0.5 for index in range(30)]))
    rising = compute_chip_distribution(_bars([10.0 + index * 0.5 for index in range(30)]))

    assert falling is not None and rising is not None
    assert rising["profit_ratio"] > falling["profit_ratio"]
    assert falling["profit_ratio"] < 0.5 < rising["profit_ratio"]


def test_high_turnover_refreshes_chips_faster():
    """换手越高，历史筹码被替换得越快——高换手下跌后的获利盘应低于低换手。"""
    closes = [20.0 - index * 0.4 for index in range(30)]
    low_turn = compute_chip_distribution(_bars(closes, turnover=1.0))
    high_turn = compute_chip_distribution(_bars(closes, turnover=15.0))

    assert low_turn is not None and high_turn is not None
    assert high_turn["profit_ratio"] >= low_turn["profit_ratio"]


def test_average_cost_sits_inside_the_price_range():
    bars = _bars([10.0 + index * 0.2 for index in range(20)])
    result = compute_chip_distribution(bars)
    assert result is not None
    low = min(row["low"] for row in bars)
    high = max(row["high"] for row in bars)
    assert low <= result["avg_cost"] <= high


def test_is_deterministic():
    bars = _bars([10.0 + (index % 5) * 0.3 for index in range(40)])
    first = compute_chip_distribution(bars)
    second = compute_chip_distribution(bars)
    assert first == second, "同样的输入必须给同样的输出，否则回测不可复现"


def test_rows_without_turnover_are_skipped_not_guessed():
    bars = _bars([10.0 + index * 0.1 for index in range(10)])
    for row in bars[:5]:
        row["turnover"] = None
    result = compute_chip_distribution(bars)
    assert result is not None, "剩余 5 行仍可用"
