"""输入为**模型估计**的维度必须被标出来。

## 为什么这是一条硬要求

东财 API 被封后，筹码维度改由「日线 + 换手率」**自算**（三角形分布模型）。
它算出来是**估计**，不是交易所原始数据。若按"数据"参与判定并在回答里被当成
事实陈述，那就是把估计说成真相——本平台最不容忍的失败类型。

"可用"不等于"原始"：这两件事必须在类型上分开，否则下游没有任何依据去声明口径。

## 本文件锁住

1. 来源是估计时，组件必须带 `estimated=True`。
2. 估计维度必须聚合进 `ProbeResult.estimated_dimensions`（供回答层声明）。
3. 原始数据源**不得**被误标为估计（否则声明会变成噪音，用户学会忽略它）。
"""

from __future__ import annotations

import pandas as pd
import pytest

from tradingagents.signals.chips import DIM_CHIPS


def _daily(n: int = 30) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": [f"2026-08-{index + 1:02d}" for index in range(n)],
            "open": [10.0 + index * 0.1 for index in range(n)],
            "high": [10.2 + index * 0.1 for index in range(n)],
            "low": [9.8 + index * 0.1 for index in range(n)],
            "close": [10.1 + index * 0.1 for index in range(n)],
            "volume": [1000.0] * n,
        }
    )


def _chips(*, source: str) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "date": "2026-09-21",
                "profit_ratio": 0.28,
                "avg_cost": 12.9,
                "concentration_90": 0.18,
                "source": source,
            }
        ]
    )


def _ctx(*, chips):
    from tradingagents.signals.context import SignalContext

    return SignalContext(
        symbol="600519.SH", as_of="2026-09-21", window_days=20,
        daily=_daily(), chips=chips,
    )


def test_model_estimate_is_flagged():
    """来源是自算估计 → 必须标 estimated。"""
    from tradingagents.signals import chips as chips_mod

    component = chips_mod.compute(_ctx(chips=_chips(source="cyq_model_estimate")))
    assert component.key == DIM_CHIPS
    assert component.available is True, "估计值仍然是可用的输入，只是口径不同"
    assert component.estimated is True, (
        "自算筹码必须标记为估计，否则它会被当成交易所数据陈述"
    )


def test_raw_source_is_not_flagged():
    """真实来源不得被误标为估计（否则声明变成噪音，用户会学会忽略它）。"""
    from tradingagents.signals import chips as chips_mod

    component = chips_mod.compute(_ctx(chips=_chips(source="eastmoney")))
    assert component.estimated is False


def test_missing_source_column_is_not_flagged():
    """没有 source 列时不得猜成估计。"""
    from tradingagents.signals import chips as chips_mod

    frame = pd.DataFrame([{"date": "2026-09-21", "profit_ratio": 0.28, "avg_cost": 12.9}])
    component = chips_mod.compute(_ctx(chips=frame))
    assert component.estimated is False


def test_estimate_becomes_a_citable_evidence_item():
    """估计口径必须变成一条可引用证据——否则模型只会把它当数据复述。"""
    from tradingagents.signals.types import ProbeResult
    from tradingagents.tools.signals import _expand_evidence

    result = ProbeResult(
        probe="signal_washout", symbol="600519.SH", as_of="2026-09-21",
        state="distribution", confidence=66, margin=0.3,
        score_washout=0.2, score_distribution=0.6,
        components=(), estimated_dimensions=("chip_distribution",),
    )
    items = _expand_evidence(result)

    assert items, "估计维度必须产出证据项"
    assert any("数据口径" == item.field for item in items), (
        f"缺少口径声明证据，实际字段：{[i.field for i in items]}"
    )
    declaring = next(i for i in items if i.field == "数据口径")
    assert "chip_distribution" in str(declaring.value)
    assert "模型估计" in str(declaring.value)


def test_no_estimate_no_extra_evidence():
    """没有估计维度时不得凭空加口径声明（否则声明变成噪音）。"""
    from tradingagents.signals.types import ProbeResult
    from tradingagents.tools.signals import _expand_evidence

    result = ProbeResult(
        probe="signal_washout", symbol="600519.SH", as_of="2026-09-21",
        state="distribution", confidence=66, margin=0.3,
        score_washout=0.2, score_distribution=0.6, components=(),
    )
    assert _expand_evidence(result) == ()


def test_prompt_requires_disclosing_the_estimate():
    """提示词必须要求声明估计口径——只加证据不够，模型可能不提。"""
    from tradingagents.agent.loop import TurnRequest, _build_prompt
    from tradingagents.agent.middleware import TurnContext
    from tradingagents.grounding.types import EvidenceItem, EvidenceSet

    evidence = EvidenceSet(items=(
        EvidenceItem(id="E1", field="数据口径", value="以下维度为模型估计：chip_distribution",
                     unit="", source_key="chip_distribution", as_of="2026-09-21"),
    ))
    prompt = _build_prompt(
        TurnRequest(text="600519 是不是在洗盘", user_id="u1", symbol="600519.SH"),
        evidence,
        TurnContext(user_id="u1", tier="medium", text="600519 是不是在洗盘"),
    )
    assert "数据口径" in prompt, "提示词要让模型看到口径字段"
    assert "模型估计" in prompt, "提示词要明确要求声明为估计"
