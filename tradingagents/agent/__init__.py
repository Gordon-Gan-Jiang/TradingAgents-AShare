"""对话 Agent 编排层。

本包只依赖 `tradingagents.tools` 与 `tradingagents.signals`，**不依赖 api 层**
（由 `tests/test_architecture_boundaries.py` 强制）。LLM、时钟、端口、事件出口
全部由调用方注入——这使循环逻辑可以在没有网络、没有模型、时间冻结的条件下
被完整测试。
"""

from __future__ import annotations

from .routing import (
    TIER_BUDGETS,
    TIER_HEAVY,
    TIER_LIGHT,
    TIER_MEDIUM,
    TIERS,
    DegradingClassifier,
    FixedClassifier,
    Resolution,
    RouteDecision,
    RuleTierClassifier,
    TierBudget,
    TierClassifier,
    SymbolResolver,
    tier_distribution,
)

__all__ = [
    "TIER_LIGHT",
    "TIER_MEDIUM",
    "TIER_HEAVY",
    "TIERS",
    "TIER_BUDGETS",
    "TierBudget",
    "RouteDecision",
    "Resolution",
    "TierClassifier",
    "SymbolResolver",
    "RuleTierClassifier",
    "DegradingClassifier",
    "FixedClassifier",
    "tier_distribution",
]
