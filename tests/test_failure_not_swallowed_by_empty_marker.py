"""显式失败文案不得被泛化的「无数据」标记吞成 empty（审计 §3.1 同类，本轮自审发现）。

## 症状

`classify_vendor_text` 的判定顺序是 **empty 先、failure 后**：

```python
if looks_like_empty_text(text):
    return "empty"
if looks_like_failure_text(text):
    return "failure"
```

而 `VENDOR_EMPTY_MARKERS` 里有三个**极泛**的子串标记：`"无数据"`、`"暂无数据"`、
`"暂不可用"`。任何同时含失败措辞与「无数据」措辞的文案，都会被判成 empty ——
也就是「源是好的，只是这次没有数据」，而事实是**源坏了**。

实测（本轮自审，真实串）：

| 传入 | 旧分类 | 应为 |
|---|---|---|
| `【取数失败·本项无数据】` | `empty` | `failure` |
| `【取数失败·本项无数据】 get_news 调用失败：Timeout` | `empty` | `failure` |
| `数据获取失败：无数据` | `empty` | `failure` |
| `获取失败（无替代源）：暂无数据` | `empty` | `failure` |

第一条不是假想：`graph/data_collector.py:256` 的 `DATA_FETCH_FAILURE_NOTICE` 原文就是
`"【取数失败·本项无数据】"`，并由 `_safe` 在**任何工具异常**时产出——即一条真实、
高频的路径。而 `vendor_text.py` 把它登记进 `VENDOR_FAILURE_MARKERS` 时还专门写了注释
说明「无论流向哪一层都会被认成没有数据」，但那条意图被判定顺序架空了：
真正命中它的是泛化空标记 `"无数据"`。

后果：`interface.py` 把它记成 `status=empty`（源正常、仅无数据）而非
`status=fallback`（降级），数据源探针于是把**一次取数失败**报告成「源是好的」。

## 判据

同时命中两类标记时，**failure 必须胜出**：失败措辞是更具体、更可诉的主张，
而把失败说成"良性为空"是 fail-open 方向——正是本审计要消灭的方向。

反过度修正：纯空文案（`No data found for …`、`【窗口覆盖不足】`）不含失败措辞，
仍必须是 `empty`，不得被这条规则误伤。
"""

from __future__ import annotations

import pytest

from tradingagents.dataflows.vendor_text import (
    classify_vendor_text,
    looks_like_empty_text,
    looks_like_failure_text,
)


# ══════════════════════════════════════════════════════════════════════════
# 1. 显式失败文案必须判成 failure，即使它同时含「无数据」措辞
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "text",
    [
        "【取数失败·本项无数据】",
        "【取数失败·本项无数据】 get_news 调用失败：Timeout",
        "数据获取失败：无数据",
        "获取失败（无替代源）：暂无数据",
        "调用失败，暂无数据可返回",
        "数据获取异常：无数据",
    ],
    ids=["notice", "notice+exc", "fail+无数据", "fail+暂无数据", "调用失败+暂无数据", "异常+无数据"],
)
def test_explicit_failure_wins_over_generic_empty_marker(text: str) -> None:
    """**核心**：不能把「取数失败」报成「源是好的，只是没数据」。"""
    assert looks_like_failure_text(text), "前置：它含失败措辞"
    assert looks_like_empty_text(text), "前置：它也确实含泛化空标记（所以旧顺序会误判）"
    assert classify_vendor_text(text) == "failure", (
        f"{text!r} 被判成 {classify_vendor_text(text)!r}；"
        f"显式失败文案必须优先判为 failure"
    )


def test_real_data_collector_notice_is_failure_not_empty() -> None:
    """真实路径：`data_collector._safe` 在任何工具异常时产出的通知原文。"""
    from tradingagents.graph.data_collector import DATA_FETCH_FAILURE_NOTICE

    production = f"{DATA_FETCH_FAILURE_NOTICE} get_news 调用失败：TimeoutError"
    assert classify_vendor_text(production) == "failure", (
        f"工具异常必须记成 failure，实际 {classify_vendor_text(production)!r}"
    )


# ══════════════════════════════════════════════════════════════════════════
# 2. 反过度修正：真正的「良性为空」必须仍是 empty
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "text",
    [
        "No data found for symbol '600519' between 2026-01-01 and 2026-03-19",
        "No news found for 600519.SH",
        "No data available for the specified date range.",
        "【窗口覆盖不足】",
        "N/A：该日期暂无数据（可能未收盘、数据延迟或非交易日）",
    ],
    ids=["no-data-found", "no-news", "no-data-available", "窗口覆盖不足", "N/A暂暂无"],
)
def test_genuine_emptiness_stays_empty(text: str) -> None:
    """不含失败措辞的空文案不得被误伤成 failure。"""
    assert not looks_like_failure_text(text), f"前置：{text!r} 不含失败措辞"
    assert classify_vendor_text(text) == "empty", (
        f"{text!r} 应判 empty，实际 {classify_vendor_text(text)!r}"
    )


def test_json_empty_payload_still_empty() -> None:
    """空 JSON 载荷仍是 empty（结构合法、内容为空）。"""
    assert classify_vendor_text("{}") == "empty"
    assert classify_vendor_text("[]") == "empty"


def test_ordinary_data_is_still_data() -> None:
    """普通行情文本不得被任何标记命中。"""
    text = "close,volume\n100.5,1000\n101.2,1200"
    assert classify_vendor_text(text) == "data"


def test_non_string_values_are_still_data() -> None:
    """非字符串一律 data（`[]`、空 DataFrame 都是合法结果形状）。"""
    assert classify_vendor_text([]) == "data"
    assert classify_vendor_text(None) == "data"
