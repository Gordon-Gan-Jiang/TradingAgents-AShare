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
    #
    # 这段配置有**可执行的校验**：`providers/capability_matrix.py` 声明每个维度的
    # 分层源，`tests/test_provider_capability_matrix.py` 强制两件事——
    #   R4 链上不许有"死成员"（列在这条链上却对该类别的 tool 一个都实现不了）
    #   R6 有效链里第一个能服务该 tool 的 provider 必须等于矩阵声明的 tier 1
    # 这两条规则来自一次真实事故：`cn_baostock` 曾被列进 3 条它 0 可服务的链，
    # 于是"链上三源覆盖"是假象，而它的真实故障（上游主机名迁移）被
    # NotImplementedError 吞掉。改这里的链之前，请先改矩阵，让测试告诉你是否自洽。
    "data_vendors": {
        "core_stock_apis": "cn_akshare,cn_baostock,yfinance",
        "technical_indicators": "cn_akshare,cn_baostock,yfinance",
        # Baostock 在这里是**兜底**而非主源：AKShare 的同花顺/新浪口径字段更丰富，
        # 且既有分析提示词与历史报告都按该口径写；Baostock 提供的是季频财务**指标**
        # （roeAvg / currentRatio / CFOToNP 之类），同名方法含义并不相同。
        # 把它排在第二，既拿到"上游改版时还有稳定退路"，又不静默改变已有数字的含义。
        "fundamental_data": "cn_akshare,cn_baostock,yfinance",
        # cn_baostock 曾在这条链上，但它没有新闻能力（可服务 0/3）——链上死成员
        # 让"三源覆盖"变成假象，且取数失败会被记成"这个 provider 没实现"。
        "news_data": "cn_akshare,yfinance",
        "realtime_data": "cn_akshare",
        # 研究类数据（日线/分时/筹码/资金流/板块基准/换手/个股新闻）。
        # 这一类曾经**整类漏配**：`get_vendor` 落到默认值 "yfinance"，再被
        # `_resolve_vendor_chain` 追加全部注册 provider——实际效果是
        # **A 股日线的第一顺位变成 yfinance**，一个没人有意做过的决定。
        # 现在显式配置：Baostock 是历史日线/分钟线的地基（官方协议、可批量、不封 IP），
        # 其余维度 Baostock 结构上没有，交给 AKShare 系。
        "cn_research_data": "cn_baostock,cn_akshare",
        # CN market microstructure data (fund flow, billboards, sentiment)
        # Prefer direct Eastmoney HTTP (free) to reduce AkShare wrapper breakages.
        # cn_baostock 已移除此链：它对该类别的 5 个 tool 可服务数为 0/5。
        "cn_market_data": "cn_sina_moneyflow,cn_eastmoney_http,cn_akshare",
        # CN sector/board data (mainline analysis).
        # 实测（2026-09-23）东财那条路在本机不通（走系统代理 ProxyError、强制直连
        # RemoteDisconnected），而单源的代价不是"慢一点"，而是**整个板块维度一起
        # 变成数据源故障**（get_board_spot / get_board_rank / get_board_hist 同时没数）。
        # 新浪行业快照实测可用（49 个板块），与 cn_market_data 的既有裁决一致：
        # 新浪优先、东财兜底。新浪只覆盖 get_board_spot 的 industry；
        # 其余（concept、多周期排行、板块历史）由 provider 显式声明未覆盖后交给东财。
        "cn_board_data": "cn_sina_moneyflow,cn_akshare",
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
