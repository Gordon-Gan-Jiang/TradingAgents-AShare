"""A 股专用方法论包：Prompt 注入 + DataCollector 宏观摘要。"""

from .loader import (
    get_ashare_methodology_block,
    get_fundamentals_ashare_methodology_block,
    get_mainline_ashare_methodology_block,
    get_news_events_ashare_methodology_block,
    get_sector_macro_ashare_methodology_block,
    get_stock_team_analysis_framework_block,
    get_stock_team_market_strategy_block,
    get_stock_team_risk_scoring_block,
    get_stock_team_reference_block,
    methodology_ashare_fundamentals_enabled,
)

__all__ = [
    "get_ashare_methodology_block",
    "get_fundamentals_ashare_methodology_block",
    "get_mainline_ashare_methodology_block",
    "get_news_events_ashare_methodology_block",
    "get_sector_macro_ashare_methodology_block",
    "get_stock_team_analysis_framework_block",
    "get_stock_team_market_strategy_block",
    "get_stock_team_risk_scoring_block",
    "get_stock_team_reference_block",
    "methodology_ashare_fundamentals_enabled",
]
