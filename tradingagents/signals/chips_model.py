"""筹码分布**模型估计**：用日线 + 换手率自算，不依赖东财。

## 为什么自己算

实测东财 API 被 IP 级封禁（`RemoteDisconnected`），而 `chip_distribution`
（获利盘比例、平均成本、集中度）是判别洗盘/派发最有效的维度之一。查 akshare
实现时发现一件关键事实：**`stock_cyq_em` 的"筹码分布"并不是东财提供的原始数据**
——它取东财 K 线（含换手率）后，用内嵌的 JS **三角形分布算法在本地算出**。

既然它本来就是模型估计，我们就能用**已有的免费日线**（新浪帧自带 `turnover`）
自己算：不花 Tushare 的 1000 元，也不受东财封禁影响。

## 必须标注为估计，不得与交易所数据混同

本模块产出的是**模型估计**，不是原始数据。它的假设（当日成交量在当日价格区间内
按三角形分布、历史筹码按换手率线性衰减）在真实市场中只是近似。
因此调用方**必须**把来源标为估计——把估计说成事实，正是本平台最不容忍的失败。

放在 `signals/` 是刻意的：这里保持纯函数（无网络、无时钟、无 LLM，AST 测试强制），
所以它可以被回测与复现；取数由上层注入。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

# 参与计算的历史窗口与价格分桶数：与 akshare 的实现取值一致（120 / 150），
# 换一个值会让同一只票的估计结果与业界口径不可比。
DEFAULT_WINDOW = 120
DEFAULT_BUCKETS = 150

# 来源标记：调用方据此声明"这是估计"。
SOURCE_ESTIMATED = "cyq_model_estimate"


def _to_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def compute_chip_distribution(
    bars: Sequence[Mapping[str, Any]],
    *,
    window: int = DEFAULT_WINDOW,
    buckets: int = DEFAULT_BUCKETS,
) -> dict[str, Any] | None:
    """由日线算出筹码分布的三个关键量。

    `bars` 需要按时间升序，每条含 `open/high/low/close/turnover`（换手率，百分数）。
    缺任何一个字段的行会被跳过；有效行少于 2 条时返回 None——**样本不足就不猜**。

    返回：`profit_ratio`（获利盘比例，0-1）、`avg_cost`（加权平均成本）、
    `concentration_90`（90% 筹码的价格区间宽度 / 平均价格，越小越集中）。
    """
    rows: list[dict[str, float]] = []
    for bar in bars or ():
        close = _to_float(bar.get("close"))
        high = _to_float(bar.get("high"))
        low = _to_float(bar.get("low"))
        open_ = _to_float(bar.get("open"))
        turnover = _to_float(bar.get("turnover"))
        if None in (close, high, low, open_) or turnover is None:
            continue
        rows.append(
            {"open": open_, "high": high, "low": low, "close": close, "turnover": turnover}
        )

    if len(rows) < 2:
        return None
    rows = rows[-window:]

    low_price = min(row["low"] for row in rows)
    high_price = max(row["high"] for row in rows)
    if high_price <= low_price:
        # 全窗口一字板：分布退化成一个点，仍可给出成本与获利比例
        high_price = low_price + 1e-6
    step = (high_price - low_price) / (buckets - 1)
    prices = [low_price + step * index for index in range(buckets)]

    weights = [0.0] * buckets
    for row in rows:
        # ① 历史筹码按**当日换手率**衰减（换手越多，旧筹码被替换得越多）
        decay = 1.0 - min(1.0, max(0.0, row["turnover"]) / 100.0)
        weights = [value * decay for value in weights]

        # ② 当日新增筹码：在 [low, high] 之间按三角形分布，顶点在均价
        average = (row["open"] + row["close"] + row["high"] + row["low"]) / 4.0
        low_index = max(0, min(buckets - 1, int((row["low"] - low_price) / step)))
        high_index = max(0, min(buckets - 1, int((row["high"] - low_price) / step + 0.999)))
        span = max(1, high_index - low_index)
        added = [0.0] * buckets
        total = 0.0
        for index in range(low_index, high_index + 1):
            price = prices[index]
            distance = abs(price - average) / (span * step) if span * step else 0.0
            weight = max(0.0, 1.0 - distance)
            added[index] = weight
            total += weight
        if total <= 0:
            added[low_index] = 1.0
            total = 1.0
        # 当日换手率决定"有多少筹码被换掉"，新增部分按该比例注入
        inject = min(1.0, max(0.0, row["turnover"]) / 100.0)
        weights = [value + inject * added[index] / total for index, value in enumerate(weights)]

    total_weight = sum(weights)
    if total_weight <= 0:
        return None

    close_last = rows[-1]["close"]
    profit = sum(
        weight for price, weight in zip(prices, weights) if price <= close_last
    ) / total_weight
    avg_cost = sum(price * weight for price, weight in zip(prices, weights)) / total_weight

    # 90% 筹码区间：按价格从低到高累积到 5% / 95% 分位
    cumulative = 0.0
    low_5 = prices[0]
    high_95 = prices[-1]
    for price, weight in zip(prices, weights):
        cumulative += weight
        share = cumulative / total_weight
        if share >= 0.05 and low_5 == prices[0]:
            low_5 = price
        if share >= 0.95:
            high_95 = price
            break
    concentration = (high_95 - low_5) / avg_cost if avg_cost else None

    return {
        "profit_ratio": round(profit, 4),
        "avg_cost": round(avg_cost, 4),
        "concentration_90": round(concentration, 4) if concentration is not None else None,
        "source": SOURCE_ESTIMATED,
    }
