"""工具面分层：L1 决策 / L2 证据 / L3 任务。

## 为什么分层不是"元数据好看"

工具面要长大（当前 24 个，规划里还要加 19 个）。**全量注入提示词会撑爆上下文并
降低选择准确率**，所以必须能按层取清单：L1 常驻（少、语义强），L2 按意图检索。

另外一条硬约束：**L3（分钟级任务）必须异步**。同步工具面里放分钟级管道会让
MCP 客户端超时重试、对话循环卡住、预算记账失真——今天 7 分析师管道无法被对话
调用就是这个原因。这条用**构造期校验**强制，不靠约定。
"""

from __future__ import annotations

import pytest

from tradingagents.tools.spec import (
    PLAN_LAYER_DECISION,
    PLAN_LAYER_EVIDENCE,
    PLAN_LAYER_TASK,
    PLAN_LAYERS,
    ToolResult,
    ToolSpec,
)


def _spec(**kw) -> ToolSpec:
    base = dict(
        name="t", title="t", description="d", input_schema={}, output_contract="X",
        handler=lambda **kwargs: ToolResult(data={}),
    )
    base.update(kw)
    return ToolSpec(**base)


def test_default_layer_is_evidence():
    assert _spec().plan_layer == PLAN_LAYER_EVIDENCE
    assert _spec().asynchronous is False


def test_invalid_layer_is_rejected_at_construction():
    with pytest.raises(ValueError):
        _spec(plan_layer="L9")


def test_task_layer_must_be_asynchronous():
    """同步的 L3 工具 = 客户端必然超时，属于协议错误，必须在构造期就炸。"""
    with pytest.raises(ValueError):
        _spec(plan_layer=PLAN_LAYER_TASK, asynchronous=False)

    ok = _spec(plan_layer=PLAN_LAYER_TASK, asynchronous=True)
    assert ok.asynchronous is True


def test_descriptor_exposes_layer_and_async():
    descriptor = _spec(plan_layer=PLAN_LAYER_DECISION).as_descriptor()
    assert descriptor["plan_layer"] == PLAN_LAYER_DECISION
    assert descriptor["asynchronous"] is False


def test_registry_lists_by_layer():
    """必须能按层取清单——规划层据此决定注入哪些工具。"""
    from tradingagents.tools import build_registry

    registry = build_registry(
        ports_factory=lambda *a: object(), news_fetcher=lambda *a: [], market_screener=object()
    )
    decisions = registry.names_by_layer(PLAN_LAYER_DECISION)
    evidence = registry.names_by_layer(PLAN_LAYER_EVIDENCE)

    assert "signal_washout_probe" in decisions
    assert "signal_washout_probe_batch" in decisions
    assert "market_screen" in decisions
    assert "get_realtime_quotes" in evidence
    assert not set(decisions) & set(evidence), "同一工具不该同时属两层"
    assert set(decisions) | set(evidence) == set(registry.names()), "每个工具都必须有层"


def test_every_registered_tool_has_a_valid_layer():
    """结构性守卫：任何新加的工具都必须落在三层之内。"""
    from tradingagents.tools import build_registry

    registry = build_registry(
        ports_factory=lambda *a: object(), news_fetcher=lambda *a: [], market_screener=object()
    )
    for name in registry.names():
        spec = registry.get(name)
        assert spec.plan_layer in PLAN_LAYERS, f"{name} 的层非法：{spec.plan_layer}"
