"""工具层：能力定义的单一来源。

`build_registry` 是**唯一**的装配点。刻意采用显式组装而不是装饰器自动注册：
自动注册依赖 import 顺序（谁先被 import 谁在册），在测试里难以构造干净实例，
且一个全局可变注册表会在并发下互相污染——本仓库在
`dataflows/config.py` 的 `global _config` 上已经吃过这个亏。

加一个新工具 = 新文件 + 这里加一行，不需要修改任何既有模块。
"""

from __future__ import annotations

from typing import Callable

from .registry import ToolRegistry
from .spec import RESEARCH_DISCLAIMER, CostClass, ToolResult, ToolSpec

__all__ = [
    "ToolRegistry",
    "ToolSpec",
    "ToolResult",
    "CostClass",
    "RESEARCH_DISCLAIMER",
    "build_registry",
]


def build_registry(
    *,
    ports_factory: Callable[[str, str], object] | None = None,
    news_fetcher: Callable[..., object] | None = None,
    market_screener: object | None = None,
    history_port: object | None = None,
    weight_port: object | None = None,
    holdings_port: object | None = None,
) -> ToolRegistry:
    """组装工具注册表。

    `ports_factory` 与 `news_fetcher` 由应用层提供（真实 provider）；未提供时
    只装配与数据源无关的工具，使工具层可以在没有网络的环境下被测试。

    刻意**不**装配半成品：没有取数器就不注册对应工具，避免出现「在册但一调
    就炸」的条目——那类条目会让 Agent 把预算浪费在必然失败的调用上。
    """
    from .data import make_data_tools
    from .history import make_history_tools, make_weight_tools
    from .portfolio import make_portfolio_tools
    from .news import make_news_tools
    from .screen import make_screen_tools
    from .signals import make_signal_tools

    registry = ToolRegistry()
    # 数据工具不依赖注入的端口（它们直接走既有 vendor 路由），因此默认装配。
    # 这是「工具定义单一来源」的落点：分析图与新入口共用同一份声明。
    for spec in make_data_tools():
        registry.register(spec)
    if ports_factory is not None:
        for spec in make_signal_tools(ports_factory):
            registry.register(spec)
    if news_fetcher is not None:
        for spec in make_news_tools(news_fetcher):
            registry.register(spec)
    if market_screener is not None:
        for spec in make_screen_tools(market_screener):
            registry.register(spec)
    if history_port is not None:
        for spec in make_history_tools(history_port):
            registry.register(spec)
    if weight_port is not None:
        for spec in make_weight_tools(weight_port):
            registry.register(spec)
    if holdings_port is not None:
        for spec in make_portfolio_tools(holdings_port):
            registry.register(spec)
    return registry
