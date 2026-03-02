from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

from dotenv import load_dotenv
import os

# Load environment variables from .env file
load_dotenv()

# Create a custom config
config = DEFAULT_CONFIG.copy()
config["llm_provider"] = os.getenv("LLM_PROVIDER", config["llm_provider"])
config["backend_url"] = os.getenv("OPENAI_BASE_URL", config["backend_url"])
config["quick_think_llm"] = os.getenv("QUICK_THINK_LLM", "gpt-5-mini")
config["deep_think_llm"] = os.getenv("DEEP_THINK_LLM", "gpt-5-mini")
config["max_debate_rounds"] = int(os.getenv("MAX_DEBATE_ROUNDS", "1"))
config["max_risk_discuss_rounds"] = int(os.getenv("MAX_RISK_DISCUSS_ROUNDS", "1"))

# Configure data vendors (prefer cn_akshare for A-share, fallback to yfinance)
config["data_vendors"] = {
    "core_stock_apis": "cn_akshare,cn_baostock,yfinance",
    "technical_indicators": "cn_akshare,cn_baostock,yfinance",
    "fundamental_data": "cn_akshare,cn_baostock,yfinance",
    "news_data": "cn_akshare,cn_baostock,yfinance",
}

# Initialize with custom config
ta = TradingAgentsGraph(debug=True, config=config)

# forward propagate
_, decision = ta.propagate("600519.SH", "2026-03-02")
print(decision)

# Memorize mistakes and reflect
# ta.reflect_and_remember(1000) # parameter is the position returns
