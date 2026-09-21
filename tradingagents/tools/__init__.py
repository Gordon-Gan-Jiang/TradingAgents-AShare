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
    mainline_port: object | None = None,
    task_port: object | None = None,
    evidence_fetchers: object | None = None,
    watchlist_port: object | None = None,
    announcements_port: object | None = None,
    freshness_port: object | None = None,
    outcome_port: object | None = None,
    report_port: object | None = None,
    recommendation_port: object | None = None,
    tracking_port: object | None = None,
    arena_port: object | None = None,
    risk_port: object | None = None,
    plan_port: object | None = None,
    quality_port: object | None = None,
    paper_port: object | None = None,
    ledger_port: object | None = None,
    consensus_port: object | None = None,
) -> ToolRegistry:
    """组装工具注册表。

    `ports_factory` 与 `news_fetcher` 由应用层提供（真实 provider）；未提供时
    只装配与数据源无关的工具，使工具层可以在没有网络的环境下被测试。

    刻意**不**装配半成品：没有取数器就不注册对应工具，避免出现「在册但一调
    就炸」的条目——那类条目会让 Agent 把预算浪费在必然失败的调用上。

    L2 证据工具的四个注入点同理（见 `tools/evidence.py`）：
    `evidence_fetchers`（市场类取数集合，组合根通常传
    `default_market_fetchers()`）、`watchlist_port`、`announcements_port`、
    `freshness_port`。未注入的那个工具不会出现在注册表里。

    `outcome_port` / `report_port` / `recommendation_port` / `tracking_port` /
    `arena_port` / `risk_port` / `plan_port` / `quality_port` / `paper_port` /
    `ledger_port` / `consensus_port` 是 L1 决策工具的注入点（判定兑现、历史报告
    结论、平台推荐、跟踪看板、模型对比榜、组合体检、交易计划、T+1 质量、模拟盘、
    交易归因、多视角共识），同样遵循「未注入就不注册」。
    """
    from .arena import make_arena_tools
    from .consensus import make_consensus_tools
    from .data import make_data_tools
    from .history import make_history_tools, make_weight_tools
    from .ledger import make_ledger_tools
    from .outcomes import make_outcome_tools
    from .paper import make_paper_tools
    from .plans import make_plan_tools
    from .portfolio import make_mainline_tools, make_portfolio_tools
    from .quality import make_quality_tools
    from .recommendations import make_recommendation_tools
    from .reports import make_report_tools
    from .risk import make_risk_tools
    from .tasks import make_task_tools
    from .tracking import make_tracking_tools
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
    if mainline_port is not None:
        for spec in make_mainline_tools(mainline_port):
            registry.register(spec)
    if task_port is not None:
        for spec in make_task_tools(task_port):
            registry.register(spec)
    # 第二批 L1 决策工具：每个端口一个分支，未注入即不注册（不留「在册但一调
    # 就炸」的条目）。用户维度在端口的闭包里绑定，工具参数里没有 user_id。
    if outcome_port is not None:
        for spec in make_outcome_tools(outcome_port):
            registry.register(spec)
    if report_port is not None:
        for spec in make_report_tools(report_port):
            registry.register(spec)
    if recommendation_port is not None:
        for spec in make_recommendation_tools(recommendation_port):
            registry.register(spec)
    if tracking_port is not None:
        for spec in make_tracking_tools(tracking_port):
            registry.register(spec)
    if arena_port is not None:
        for spec in make_arena_tools(arena_port):
            registry.register(spec)
    if risk_port is not None:
        for spec in make_risk_tools(risk_port):
            registry.register(spec)
    if plan_port is not None:
        for spec in make_plan_tools(plan_port):
            registry.register(spec)
    if quality_port is not None:
        for spec in make_quality_tools(quality_port):
            registry.register(spec)
    if paper_port is not None:
        for spec in make_paper_tools(paper_port):
            registry.register(spec)
    if ledger_port is not None:
        for spec in make_ledger_tools(ledger_port):
            registry.register(spec)
    if consensus_port is not None:
        for spec in make_consensus_tools(consensus_port):
            registry.register(spec)
    # L2 证据工具：按注入的依赖逐个装配，缺一个就少一个条目（不注册半成品）。
    if any((evidence_fetchers, watchlist_port, announcements_port, freshness_port)):
        from .evidence import make_evidence_tools

        for spec in make_evidence_tools(
            fetchers=evidence_fetchers,
            watchlist_port=watchlist_port,
            announcements_port=announcements_port,
            freshness_port=freshness_port,
        ):
            registry.register(spec)
    return registry
