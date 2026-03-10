from datetime import datetime, timedelta

from langchain_core.messages import HumanMessage, SystemMessage

from tradingagents.agents.utils.agent_utils import get_global_news, get_news
from tradingagents.dataflows.config import get_config
from tradingagents.prompts import get_prompt


def create_news_analyst(llm):
    def _invoke_tool(tool, payload):
        try:
            return tool.invoke(payload)
        except Exception as exc:
            return f"{tool.name} 调用失败：{type(exc).__name__}: {exc}"

    def news_analyst_node(state):
        current_date = state["trade_date"]
        ticker = state["company_of_interest"]

        config = get_config()
        system_message = get_prompt("news_system_message", config=config)

        end_dt = datetime.strptime(current_date, "%Y-%m-%d")
        start_dt = end_dt - timedelta(days=7)
        stock_news = _invoke_tool(
            get_news,
            {
                "ticker": ticker,
                "start_date": start_dt.strftime("%Y-%m-%d"),
                "end_date": current_date,
            },
        )
        global_news = _invoke_tool(
            get_global_news,
            {
                "curr_date": current_date,
                "look_back_days": 7,
                "limit": 10,
            },
        )

        messages = [
            SystemMessage(
                content=(
                    system_message
                    + "\n\n你已经拿到标的新闻与宏观新闻资料。现在只基于这些结果写报告，"
                    "不要继续请求工具，不要输出 <longcat_tool_call>、XML、JSON 或伪函数调用。"
                    "请全程使用中文。"
                )
            ),
            HumanMessage(
                content=(
                    f"以下是 {ticker} 在 {current_date} 的新闻资料。"
                    "请严格基于这些资料输出新闻分析报告。\n\n"
                    f"【get_news】\n{stock_news}\n\n"
                    f"【get_global_news】\n{global_news}\n"
                )
            ),
        ]

        result = llm.invoke(messages)
        return {
            "news_report": result.content,
        }

    return news_analyst_node
