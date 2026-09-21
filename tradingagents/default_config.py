import os


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env(name: str, default: str) -> str:
    """读取环境变量；空字符串视为未设置，回退默认值（修复 .env 空值覆盖默认模型/key 的问题）。"""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip()


DEFAULT_CONFIG = {
    "project_dir": os.path.abspath(os.path.join(os.path.dirname(__file__), ".")),
    "results_dir": os.getenv("TA_RESULTS_DIR", "./results"),
    "data_cache_dir": os.path.join(
        os.path.abspath(os.path.join(os.path.dirname(__file__), ".")),
        "dataflows/data_cache",
    ),
    # LLM settings
    "llm_provider": _env("TA_LLM_PROVIDER", "openai"),
    "deep_think_llm": _env("TA_LLM_DEEP", "gpt-4o"),
    "quick_think_llm": _env("TA_LLM_QUICK", "gpt-4o-mini"),
    "llm_temperature": _env_float("TA_LLM_TEMPERATURE", 0.0),
    "backend_url": _env("TA_BASE_URL", "https://api.openai.com/v1"),
    "api_key": _env("TA_API_KEY", ""),
    
    # Provider-specific thinking configuration
    "google_thinking_level": None,      # "high", "minimal", etc.
    "openai_reasoning_effort": None,    # "medium", "high", "low"
    
    # Debate and discussion settings
    # Each extra bull/bear round is another pair of LLM calls in which the argument
    # can drift, adding run-to-run variance for no measured gain in directional
    # accuracy (identical inputs already disagreed 48.6% of the time at
    # temperature 0). One exchange each is enough to surface the disagreement that
    # the research manager is meant to arbitrate; raise via TA_MAX_DEBATE if a
    # specific study justifies it.
    "max_debate_rounds": int(os.getenv("TA_MAX_DEBATE") or "1"),
    "max_risk_discuss_rounds": int(os.getenv("TA_MAX_RISK") or "1"),
    # Decision Critic (post risk judge): user/UI can disable; TA_DECISION_CRITIC=0 forces off in API merge
    "decision_critic_enabled": True,
    "decision_critic_revision_threshold": _env_float("TA_DECISION_CRITIC_REVISION_THRESHOLD", 40.0),
    "max_recur_limit": 100,
    
    # Prompt language control: zh, en, or auto
    "prompt_language": os.getenv("TA_LANGUAGE", "zh"),
    "prompt_language_by_provider": {},
    
    # Provider routing trace logs
    "provider_trace": os.getenv("TA_TRACE", "1").lower() in ("1", "true", "yes", "on"),
    
    # Data vendor configuration
    "data_vendors": {
        "core_stock_apis": "cn_akshare,cn_baostock,yfinance",
        "technical_indicators": "cn_akshare,cn_baostock,yfinance",
        "fundamental_data": "cn_akshare,cn_baostock,yfinance",
        "news_data": "cn_akshare,cn_baostock,yfinance",
        "realtime_data": "cn_akshare",
        # CN market microstructure data (fund flow, billboards, sentiment)
        # Prefer direct Eastmoney HTTP (free) to reduce AkShare wrapper breakages.
        "cn_market_data": "cn_sina_moneyflow,cn_eastmoney_http,cn_akshare,cn_baostock",
        # CN sector/board data (mainline analysis) — AkShare is the only full source for M1.
        "cn_board_data": "cn_akshare",
    },
    # Per-tool vendor override (highest priority). Useful for stabilizing fragile endpoints.
    "tool_vendors": {
        # Prefer direct Eastmoney HTTP for fund flow to reduce AkShare wrapper breakages/anti-bot issues.
        "get_individual_fund_flow": "cn_sina_moneyflow,cn_eastmoney_http,cn_akshare",
        # Sina board ranking avoids Eastmoney push2 connection resets seen in some networks.
        "get_board_fund_flow": "cn_sina_moneyflow,cn_akshare",
    },
    # A 股基本面专项方法论（内置 ashare_fundamentals.md + 可选 TA_METHODOLOGY_EXTRA）
    "methodology_ashare_fundamentals": (
        os.getenv("TA_METHODOLOGY_ASHARE_FUNDAMENTALS", "1").strip().lower()
        not in ("0", "false", "no", "off")
    ),
    "methodology_extra_path": os.getenv("TA_METHODOLOGY_EXTRA", "").strip(),
    # 新闻/事件、行业/宏观 方法论（内置 md + 独立 EXTRA 路径）
    "methodology_ashare_news_events": (
        os.getenv("TA_METHODOLOGY_ASHARE_NEWS", "1").strip().lower()
        not in ("0", "false", "no", "off")
    ),
    "methodology_ashare_sector_macro": (
        os.getenv("TA_METHODOLOGY_ASHARE_MACRO", "1").strip().lower()
        not in ("0", "false", "no", "off")
    ),
    "methodology_extra_path_news": os.getenv("TA_METHODOLOGY_EXTRA_NEWS", "").strip(),
    "methodology_extra_path_macro": os.getenv("TA_METHODOLOGY_EXTRA_MACRO", "").strip(),
    # 市场主线方法论（mainline_analyst / selector 注入）
    "methodology_ashare_mainline": (
        os.getenv("TA_METHODOLOGY_ASHARE_MAINLINE", "1").strip().lower()
        not in ("0", "false", "no", "off")
    ),
    "methodology_extra_path_mainline": os.getenv("TA_METHODOLOGY_EXTRA_MAINLINE", "").strip(),
    # DataCollector 是否注入 macro_ashare_brief（AkShare 宏观摘要）
    "methodology_data_enrichment": (
        os.getenv("TA_METHODOLOGY_DATA_ENRICH", "1").strip().lower()
        not in ("0", "false", "no", "off")
    ),
    # 本地 FinSkills 克隆根目录（未设 TA_METHODOLOGY_EXTRA_* 时自动加载 China-market/*/references）
    "finskills_root": (
        os.getenv("FINSKILLS_ROOT", os.getenv("TA_FINSKILLS_ROOT", "")).strip()
    ),
    "methodology_stock_analysis_team": (
        os.getenv("TA_METHODOLOGY_STOCK_TEAM", "1").strip().lower()
        not in ("0", "false", "no", "off")
    ),
    "stock_analysis_team_root": (
        os.getenv("STOCK_ANALYSIS_TEAM_ROOT", os.getenv("TA_STOCK_ANALYSIS_TEAM_ROOT", "")).strip()
    ),
}
