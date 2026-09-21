"""确定性信号层。

本包内的所有模块必须满足（由 `tests/test_architecture_boundaries.py` 强制）：

* **禁止网络与 LLM**：不 import requests / akshare / baostock / openai /
  anthropic / langchain_core。
* **禁止环境时钟**：不调用 `datetime.now()` / `time.time()`；时间只能由
  `as_of` 参数显式传入。这使回放能精确复现历史某一天的判定。
* **不依赖工具层与应用层**：本包不知道 `ToolSpec` 存在，转换发生在
  `tradingagents/tools/signals.py`。

这三条不是风格偏好：洗盘判定要能被回测校准，前提是同一份输入永远
给出同一份输出，且回放时不掺入未来信息。
"""

from __future__ import annotations

from .context import InsufficientHistory, SignalContext, build_context
from .ports import (
    CapitalFlowPort,
    ChipPort,
    DailyBarsPort,
    IntradayBarsPort,
    PeerPort,
    PeerSnapshot,
    SignalPorts,
)
from .washout import (
    PROBE_NAME,
    aggregate_masses,
    component_map,
    evaluate,
    evaluate_context,
    raw_observables,
)
from .weights import DEFAULT_WEIGHTS, SIGNAL_VERSION, WashoutWeights, confidence_from_margin
from .types import (
    SIDE_DISTRIBUTION,
    SIDE_TREND_BREAK,
    SIDE_WASHOUT,
    Aggregation,
    STATE_DISTRIBUTION,
    STATE_INSUFFICIENT,
    STATE_TREND_BREAK,
    STATE_WASHOUT,
    VERDICT_STATES,
    Component,
    Evidence,
    Falsifier,
    Modulator,
    ProbeResult,
    assert_well_formed,
)

__all__ = [
    # probe
    "PROBE_NAME",
    "evaluate",
    "evaluate_context",
    "aggregate_masses",
    "component_map",
    "raw_observables",
    "WashoutWeights",
    "DEFAULT_WEIGHTS",
    "SIGNAL_VERSION",
    "confidence_from_margin",
    "Aggregation",
    "SIDE_WASHOUT",
    "SIDE_DISTRIBUTION",
    "SIDE_TREND_BREAK",
    # types
    "Component",
    "Evidence",
    "Falsifier",
    "Modulator",
    "ProbeResult",
    "assert_well_formed",
    "STATE_WASHOUT",
    "STATE_DISTRIBUTION",
    "STATE_TREND_BREAK",
    "STATE_INSUFFICIENT",
    "VERDICT_STATES",
    # context
    "SignalContext",
    "InsufficientHistory",
    "build_context",
    # ports
    "SignalPorts",
    "DailyBarsPort",
    "IntradayBarsPort",
    "ChipPort",
    "CapitalFlowPort",
    "PeerPort",
    "PeerSnapshot",
]
