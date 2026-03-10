from datetime import datetime, timedelta

from langchain_core.messages import HumanMessage, SystemMessage

from tradingagents.agents.utils.agent_utils import get_indicators, get_stock_data
from tradingagents.dataflows.config import get_config
from tradingagents.prompts import get_prompt


MARKET_INDICATORS = [
    ("close_50_sma", 30),
    ("close_200_sma", 30),
    ("close_10_ema", 30),
    ("rsi", 30),
    ("macd", 30),
    ("boll", 30),
    ("boll_ub", 30),
    ("boll_lb", 30),
    ("atr", 30),
    ("vwma", 30),
]


def create_market_analyst(llm):
    def _invoke_tool(tool, payload):
        try:
            return tool.invoke(payload)
        except Exception as exc:
            return f"{tool.name} 调用失败：{type(exc).__name__}: {exc}"

    def market_analyst_node(state):
        current_date = state["trade_date"]
        ticker = state["company_of_interest"]

        config = get_config()
        system_message = get_prompt("market_system_message", config=config)

        end_dt = datetime.strptime(current_date, "%Y-%m-%d")
        start_dt = end_dt - timedelta(days=30)
        stock_data = _invoke_tool(
            get_stock_data,
            {
                "symbol": ticker,
                "start_date": start_dt.strftime("%Y-%m-%d"),
                "end_date": current_date,
            },
        )

        indicator_blocks = []
        for indicator, look_back_days in MARKET_INDICATORS:
            indicator_blocks.append(
                f"【{indicator}】\n"
                + _invoke_tool(
                    get_indicators,
                    {
                        "symbol": ticker,
                        "indicator": indicator,
                        "curr_date": current_date,
                        "look_back_days": look_back_days,
                    },
                )
            )

        messages = [
            SystemMessage(
                content=(
                    system_message
                    + "\n\n你已经拿到行情与技术指标结果。现在只基于这些结果写报告，"
                    "不要继续请求工具，不要输出 <longcat_tool_call>、XML、JSON 或伪函数调用。"
                    "请全程使用中文。"
                )
            ),
            HumanMessage(
                content=(
                    f"以下是 {ticker} 在 {current_date} 的行情与技术指标资料。"
                    "请严格基于这些资料输出市场技术分析报告。\n\n"
                    f"【get_stock_data】\n{stock_data}\n\n"
                    + "\n\n".join(indicator_blocks)
                )
            ),
        ]

        result = llm.invoke(messages)
        return {
            "market_report": result.content,
        }

    return market_analyst_node
