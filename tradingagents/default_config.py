import os

DEFAULT_CONFIG = {
    "project_dir": os.path.abspath(os.path.join(os.path.dirname(__file__), ".")),
    "results_dir": os.getenv("TRADINGAGENTS_RESULTS_DIR", "./results"),
    "data_cache_dir": os.path.join(
        os.path.abspath(os.path.join(os.path.dirname(__file__), ".")),
        "dataflows/data_cache",
    ),
    # LLM settings
    "llm_provider": "openai",
    "deep_think_llm": "gpt-5.2",
    "quick_think_llm": "gpt-5-mini",
    "backend_url": "https://api.openai.com/v1",
    # Provider-specific thinking configuration
    "google_thinking_level": None,      # "high", "minimal", etc.
    "openai_reasoning_effort": None,    # "medium", "high", "low"
    # Debate and discussion settings
    "max_debate_rounds": 1,
    "max_risk_discuss_rounds": 1,
    "max_recur_limit": 100,
    # Prompt language control: zh, en, or auto (by llm_provider mapping below)
    "prompt_language": os.getenv("TRADINGAGENTS_PROMPT_LANGUAGE", "zh"),
    "prompt_language_by_provider": {
        # Example: "opesnai": "zh",
    },
    # Provider routing trace logs (set TRADINGAGENTS_PROVIDER_TRACE=0 to disable)
    "provider_trace": os.getenv("TRADINGAGENTS_PROVIDER_TRACE", "1").lower() in ("1", "true", "yes", "on"),
    # Data vendor configuration
    # Category-level configuration (default for all tools in category)
    "data_vendors": {
        "core_stock_apis": "yfinance",       # Options: yfinance, alpha_vantage, cn_akshare, cn_tushare, cn_stub
        "technical_indicators": "yfinance",  # Options: yfinance, alpha_vantage, cn_akshare, cn_tushare, cn_stub
        "fundamental_data": "yfinance",      # Options: yfinance, alpha_vantage, cn_akshare, cn_tushare, cn_stub
        "news_data": "yfinance",             # Options: yfinance, alpha_vantage, cn_akshare, cn_tushare, cn_stub
    },
    # Tool-level configuration (takes precedence over category-level)
    "tool_vendors": {
        # Example: "get_stock_data": "alpha_vantage",  # Override category default
    },
}
