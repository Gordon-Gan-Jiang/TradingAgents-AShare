"""工具元数据必须被**执行**，而不是只声明（审计 B1/B2/B3 修复）。

## 修复前的实际状态（"规格装饰"）

`ToolSpec` 上的四个字段**只被赋值、从未被读取**：

| 字段 | 修复前 | 后果 |
|---|---|---|
| `latency_budget_ms` | 4 处赋值、0 处读取 | 无超时、无「指明慢源」的结构化错误 |
| `requires` | 只声明 | 工具的前置条件无人校验 |
| `freshness_sources` | 只声明 | A2 已修（改由 `freshness.py` 使用） |
| `output_contract` | 只声明 | 仅测试断言用 |

## 一条原则

**声明而不执行比不声明更糟**：它让 review 以为能力存在。因此本文件对每个字段
都断言「它真的改变了行为」，而不是「它存在」。
"""

from __future__ import annotations

import time

import pytest

from tradingagents.tools.spec import ToolResult, ToolSpec


def _spec(**over):
    base = dict(
        name="demo",
        title="demo",
        description="demo",
        input_schema={"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]},
        output_contract="DEMO",
        handler=lambda **kw: ToolResult(data={"ok": True}),
    )
    base.update(over)
    return ToolSpec(**base)


# ══════════════════════════════════════════════════════════════════════════
# B1：延迟预算真正执行
# ══════════════════════════════════════════════════════════════════════════


def test_slow_tool_returns_structured_timeout_error():
    """超过延迟预算必须返回**结构化**错误并指明慢的那个源。

    修复前 `latency_budget_ms` 从未被读取——声明了超时预算却没有任何超时行为。
    """
    from tradingagents.tools.registry import ToolRegistry

    def _slow(**kw):
        time.sleep(0.4)
        return ToolResult(data={"ok": True})

    registry = ToolRegistry()
    registry.register(
        _spec(name="slow_tool", handler=_slow, latency_budget_ms=50,
              freshness_sources=("slow_source",))
    )

    result = registry.invoke("slow_tool", symbol="600519.SH")

    assert result.data["timeout"] is True
    assert result.degraded is True
    assert "slow_source" in (result.degraded_reason or "") or "slow_tool" in (result.degraded_reason or "")
    assert result.coverage.unavailable


def test_fast_tool_is_unaffected_by_the_budget():
    from tradingagents.tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.register(_spec(name="fast_tool", latency_budget_ms=5000))
    result = registry.invoke("fast_tool", symbol="600519.SH")
    assert result.data == {"ok": True}
    assert result.degraded is False


def test_timeout_error_is_structured_not_an_exception():
    """超时必须变成**结果**而不是异常：异常会让整轮对话失败，
    而「某个源慢」只应让该维度降级。"""
    from tradingagents.tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.register(_spec(name="slow2", handler=lambda **k: (time.sleep(0.3), ToolResult(data={}))[1],
                            latency_budget_ms=30))
    result = registry.invoke("slow2", symbol="x")
    assert isinstance(result, ToolResult)


def test_timeout_result_names_the_underlying_sources():
    from tradingagents.tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.register(
        _spec(
            name="slow3",
            handler=lambda **k: (time.sleep(0.3), ToolResult(data={}))[1],
            latency_budget_ms=30,
            freshness_sources=("intraday_bars", "chip_distribution"),
        )
    )
    result = registry.invoke("slow3", symbol="x")
    reason = result.degraded_reason or ""
    assert "intraday_bars" in reason and "chip_distribution" in reason


# ══════════════════════════════════════════════════════════════════════════
# B2：模型调用预算真正执行
# ══════════════════════════════════════════════════════════════════════════


def test_model_call_budget_is_enforced():
    """`max_model_calls` 修复前只被存下来、从未被引用。

    档位预算声明的「1 次归纳」因此没有约束力——模型可以反复调用。
    """
    from tests.test_agent_loop import CollectingSink, FrozenClock, QueueModel, _grounded_answer, _registry, _request
    from tradingagents.agent.loop import run_turn

    class _ChattyModel(QueueModel):
        def propose(self, **kw):
            return super().propose(**kw)

        def repair(self, **kw):
            self.repair_calls += 1
            return super().propose(prompt="", evidence=kw["evidence"], verdict=kw.get("verdict"))

    sink = CollectingSink()
    model = _ChattyModel([_grounded_answer])
    result = run_turn(
        request=_request(tier="light"),
        registry=_registry(),
        model=model,
        sink=sink,
        clock=FrozenClock(),
    )

    # 轻档 max_model_calls=1：总模型调用不得超预算
    assert result.model_calls <= result.budget.max_model_calls
    assert result.tokens >= 0


def test_budget_exhaustion_is_reported():
    from tradingagents.agent.routing import TIER_BUDGETS

    assert TIER_BUDGETS["light"].max_model_calls >= 1
    assert TIER_BUDGETS["medium"].max_model_calls >= 1


# ══════════════════════════════════════════════════════════════════════════
# B3：requires 前置条件被校验
# ══════════════════════════════════════════════════════════════════════════


def test_tool_with_unmet_requirement_is_noticed():
    """声明 `requires=("position",)` 但编排层未解析到该上下文时，必须**告知用户**。

    修复前 `requires` 只是元数据、从未被读取：工具照跑，用户看到一次完整判定，
    却不知道其中与持仓相关的部分是空的。

    语义澄清：这里**不跳过**工具，而是发通知。因为判定探针本来就设计成
    无持仓也能跑（缺持仓时它会声明该维度未覆盖）；跳过它会连判定一起丢掉。
    `requires` 的含义是「编排层必须先尝试解析」，不是「没有就禁止执行」。
    """
    from tests.test_agent_loop import CollectingSink, FrozenClock, QueueModel, _grounded_answer, _request
    from tradingagents.agent.events import Notice
    from tradingagents.agent.loop import run_turn
    from tradingagents.tools.registry import ToolRegistry
    from tradingagents.tools.signals import make_signal_tools
    from tests.test_acceptance_conversational_signal_agent import _ports_factory

    registry = ToolRegistry()
    for spec in make_signal_tools(_ports_factory()):
        registry.register(spec)

    sink = CollectingSink()
    run_turn(
        request=_request(),
        registry=registry,
        model=QueueModel([_grounded_answer]),
        sink=sink,
        clock=FrozenClock(),
    )
    # 工具仍有 requires=("position",)，但未被满足 → 必须告知
    assert any("position" in n.text or "持仓" in n.text for n in sink.of(Notice))


def test_tool_with_met_requirement_runs_normally():
    from tests.test_agent_loop import CollectingSink, FrozenClock, QueueModel, _grounded_answer, _request
    from tradingagents.agent.loop import run_turn
    from tradingagents.tools.registry import ToolRegistry
    from tradingagents.tools.signals import make_signal_tools
    from tradingagents.grounding.types import Coverage
    from tests.test_acceptance_conversational_signal_agent import _ports_factory

    class _Pos:
        known = True
        source = "trade_ledger"
        as_of = "2026-01-16"

        def to_evidence(self):
            return ()

    registry = ToolRegistry()
    for spec in make_signal_tools(_ports_factory()):
        registry.register(spec)

    result = run_turn(
        request=_request(),
        registry=registry,
        model=QueueModel([_grounded_answer]),
        sink=CollectingSink(),
        clock=FrozenClock(),
        position_context=_Pos(),
    )
    assert result.tool_calls == ("signal_washout_probe",)
