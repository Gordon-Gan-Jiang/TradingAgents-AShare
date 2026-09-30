"""未登记的源键不得让"故障"在汇总里消失（审计 §4.18，§6 第 11 行）。

## 病根：兜底选的是**最弱**的关键度

`tools/data.py` 把 decl 的 `category`（`board_data` / `core_stock_apis` /
`fundamental_data` / `news_data`，共 4 个）当作 contract key 传给
`evaluate_tool_sources` —— 而 `contract.yaml` 里**一个都没有**。于是：

```python
entry = get_contract_entry(key) or {}
category = entry.get("category", "calendar_anchored")        # 编出来的类别
criticality = entry.get("criticality", "informational")      # 最弱的关键度
```

`aggregator._aggregate_overall` 只在 `crit in ("critical", "important")` 时才把
`status == "error"` 记进汇总量。`informational` 的错误**直接落地无声** →
探针结论：**任一工具故障 → `overall_status='fresh'`、`confidence_cap: 100`**。

这与该模块自己的 docstring（「其余 → unknown，**不得**默认 fresh」）矛盾：
状态确实标了 unknown，但关键度被兜底成"可以忽略"，等于把未知当成无害。

## 判据

1. 契约里查不到的源键，其 `criticality` 不得兜底为 `informational`（"可忽略"）。
   我们**不知道**它重不重要 —— 那就必须让它可见，而不是让它消失。
2. 这类源故障时，汇总不得给出 `overall_status='fresh'` / `confidence_cap=100`。
3. `category` 不得伪造成一个具体类别（`calendar_anchored`），那是在声称一个
   我们并没有的知识。

范围有界：`evaluate_tool_sources` 只服务工具面（`ToolResult.freshness`，经
`api/mcp_server.py` 暴露），不参与报告级 `reports.confidence`。
"""

from __future__ import annotations


def test_unknown_key_does_not_default_to_ignorable_criticality():
    """**核心**：查不到的源键不得被兜底成 `informational`（= 汇总时被忽略）。"""
    from tradingagents.tools.freshness import evaluate_tool_sources

    pool = evaluate_tool_sources(source_keys=("totally_new_source",), as_of="2026-03-04")

    crit = pool["totally_new_source"]["criticality"]
    assert crit != "informational", (
        f"未知源键的关键度不得兜底为 informational —— 那会让它的故障在汇总里消失；"
        f"实际 {crit!r}"
    )
    assert crit in ("critical", "important", "unknown"), (
        f"未知源键的关键度应是保守且可见的取值，实际 {crit!r}"
    )


def test_unknown_key_does_not_fabricate_a_category():
    """不得声称一个我们并没有的类别知识。"""
    from tradingagents.tools.freshness import evaluate_tool_sources

    pool = evaluate_tool_sources(source_keys=("totally_new_source",), as_of="2026-03-04")

    assert pool["totally_new_source"]["category"] != "calendar_anchored", (
        "未知源键的 category 不得伪造成 calendar_anchored —— 那是编造出来的知识"
    )


def test_failing_unregistered_tool_does_not_report_fresh_and_uncapped():
    """审计探针的原始症状：工具故障 → `overall_status='fresh'`、cap 100。"""
    from tradingagents.tools.freshness import evaluate_tool_sources, summarize_tool_freshness

    # tools/data.py 的失败路径：把该源标为 unavailable
    pool = evaluate_tool_sources(
        source_keys=("board_data",),
        as_of="2026-03-04",
        unavailable=("board_data",),
    )
    assert pool["board_data"]["status"] == "error", "前置：失败即 error"

    summary = summarize_tool_freshness(pool, as_of="2026-03-04", symbol="600519.SH")

    assert summary["overall_status"] != "fresh", (
        f"工具明确取不到数据时不得汇总成 fresh：{summary['overall_status']!r}"
    )
    assert summary["confidence_cap"] < 100, (
        f"工具故障必须下压置信度上限，实际 {summary['confidence_cap']}"
    )


def test_known_good_source_still_aggregates_as_before():
    """**反过度修正**：契约里登记过的正常源不受影响。"""
    from tradingagents.tools.freshness import evaluate_tool_sources, summarize_tool_freshness

    pool = evaluate_tool_sources(
        source_keys=("stock_data",),
        as_of="2026-03-04",
        verified=("stock_data",),
    )
    assert pool["stock_data"]["status"] == "fresh"

    summary = summarize_tool_freshness(pool, as_of="2026-03-04", symbol="600519.SH")
    assert summary["overall_status"] == "fresh"
    assert summary["confidence_cap"] == 100


def test_registered_source_keeps_its_contract_category_and_criticality():
    """已登记源仍必须逐字取自契约（不得被本修复影响）。"""
    from tradingagents.dataflows.freshness.contract import get_contract_entry
    from tradingagents.tools.freshness import evaluate_tool_sources

    entry = get_contract_entry("stock_data")
    pool = evaluate_tool_sources(source_keys=("stock_data",), as_of="2026-03-04")

    assert pool["stock_data"]["category"] == entry["category"]
    assert pool["stock_data"]["criticality"] == entry["criticality"]
