"""信号层基础契约：类型不变量、纯函数性、证据锚点完整性。

这些用例守的是 spec 里 `signal-layer` 的几条硬约束：
- 每个维度必须报告原始观测（不只是分数）
- 不可得维度必须显式标记，且**不得**被当作中性 0 混过去
- 参与打分的观测必须能被证据锚定，否则该结果畸形、不得返回
"""

from __future__ import annotations

import dataclasses

import pytest

from tests._signal_fixtures import make_context, make_daily


# ── Component 的结构性不变量 ────────────────────────────────────────────────


def test_component_contribution_is_derived_not_stored():
    """contribution 必须由 side/weight/magnitude 推导，避免二者不一致。

    如果把 contribution 也存成字段，就存在「side=washout 但 contribution<0」
    这种自相矛盾的结果——这类矛盾无法靠测试穷举，只能靠结构消除。
    """
    from tradingagents.signals.types import Component

    c = Component(
        key="volume_contraction",
        core=True,
        available=True,
        magnitude=0.5,
        side="washout",
        weight=1.2,
        raw={"volume_ratio_5d": 0.6},
        evidence=(),
    )
    assert c.contribution == pytest.approx(0.6)

    d = dataclasses.replace(c, side="distribution")
    assert d.contribution == pytest.approx(-0.6)

    n = dataclasses.replace(c, side=None)
    assert n.contribution == 0.0


def test_unavailable_component_contributes_zero_regardless_of_side():
    """不可得维度必须贡献 0，不能因为残留的 side 而进入打分。"""
    from tradingagents.signals.types import Component

    c = Component(
        key="chips",
        core=False,
        available=False,
        magnitude=0.0,
        side="washout",
        weight=1.0,
        raw={},
        evidence=(),
        unavailable_reason="chip data missing",
    )
    assert c.contribution == 0.0


def test_component_is_frozen():
    """组件必须是不可变值对象：聚合过程中的意外改写会污染证据链。"""
    from tradingagents.signals.types import Component

    c = Component(
        key="volume_contraction",
        core=True,
        available=True,
        magnitude=0.1,
        side=None,
        weight=1.0,
        raw={},
        evidence=(),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.magnitude = 0.9  # type: ignore[misc]


# ── Evidence 锚点 ──────────────────────────────────────────────────────────


def test_evidence_requires_source_and_as_of():
    """证据必须能回答「这个数字从哪来、什么时候的」——缺一不可。"""
    from tradingagents.signals.types import Evidence

    e = Evidence(
        field="downside_volume_ratio_5d",
        value=0.63,
        unit="ratio",
        source_key="stock_data",
        as_of="2026-01-16",
    )
    assert e.source_key == "stock_data"
    assert e.as_of == "2026-01-16"

    with pytest.raises(TypeError):
        Evidence(field="x", value=1.0, unit="ratio", as_of="2026-01-16")  # type: ignore[call-arg]


def test_scoring_component_without_evidence_is_rejected():
    """贡献非零却没有证据锚点 = 畸形结果，必须拒绝而不是照常返回。

    对照仓库既有的 `analyst_structured` 提示词要求「value 必须能在上文检索到」
    却**全仓库零校验器**的教训：这里的约束必须是可执行的代码，不是注释。
    """
    from tradingagents.signals.types import Component, assert_well_formed

    bad = Component(
        key="capital_composition",
        core=False,
        available=True,
        magnitude=0.8,
        side="distribution",
        weight=1.0,
        raw={"main_net_inflow": -3.2e7},
        evidence=(),  # ← 有贡献却没有锚点
    )
    with pytest.raises(ValueError, match="evidence"):
        assert_well_formed([bad])


def test_zero_contribution_component_may_omit_evidence():
    """中性/不可得的组件不需要锚点——它没有在断言任何事实。"""
    from tradingagents.signals.types import Component, assert_well_formed

    neutral = Component(
        key="chips",
        core=False,
        available=False,
        magnitude=0.0,
        side=None,
        weight=1.0,
        raw={},
        evidence=(),
        unavailable_reason="missing",
    )
    assert_well_formed([neutral])  # 不应抛出


# ── SignalContext 归一化 ───────────────────────────────────────────────────


def test_context_truncates_daily_to_window_and_as_of():
    """上下文必须按 window_days 截断，且绝不越过 as_of —— 这是防前视的第一道闸。

    回放校准会用它跑历史日期；若维度能看到 as_of 之后的行，回放结果
    就会包含未来信息，整个校准闭环失去意义。
    """
    from tradingagents.signals.context import build_context

    daily = make_daily([10.0 + i * 0.1 for i in range(30)], [1000.0] * 30)
    as_of = str(daily["date"].iloc[-1])[:10]

    class _Port:
        def bars(self, symbol, end, lookback):
            # 端口故意多返回：即使 provider 无视 end，上下文也必须自己截断
            return daily

    from tradingagents.signals.ports import SignalPorts

    ctx = build_context(
        ports=SignalPorts(daily=_Port()),
        symbol="600519.SH",
        as_of=as_of,
        window_days=10,
    )
    assert len(ctx.daily) == 10
    assert str(ctx.daily["date"].iloc[-1])[:10] == as_of
    assert (ctx.daily["date"].astype(str).str[:10] <= as_of).all()


def test_context_requires_minimum_history():
    """历史不足时必须显式失败，让调用方降级为缺失数据，而不是拿到半截窗口。"""
    from tradingagents.signals.context import InsufficientHistory, build_context
    from tradingagents.signals.ports import SignalPorts

    class _Port:
        def bars(self, symbol, end, lookback):
            import pandas as pd

            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])

    with pytest.raises(InsufficientHistory):
        build_context(
            ports=SignalPorts(daily=_Port()),
            symbol="600519.SH",
            as_of="2026-01-16",
            window_days=20,
        )


def test_context_is_frozen():
    from tradingagents.signals.context import SignalContext

    ctx = make_context()
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.symbol = "000001.SZ"  # type: ignore[misc]
