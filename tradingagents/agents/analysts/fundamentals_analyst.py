from langchain_core.messages import HumanMessage, SystemMessage

from tradingagents.agents.utils.agent_utils import (
    get_balance_sheet,
    get_cashflow,
    get_fundamentals,
    get_income_statement,
)
from tradingagents.dataflows.config import get_config
from tradingagents.prompts import get_prompt


def create_fundamentals_analyst(llm):
    def _invoke_tool(tool, payload):
        try:
            return tool.invoke(payload)
        except Exception as exc:
            return f"{tool.name} 调用失败：{type(exc).__name__}: {exc}"

    def fundamentals_analyst_node(state):
        current_date = state["trade_date"]
        ticker = state["company_of_interest"]

        config = get_config()
        system_message = get_prompt("fundamentals_system_message", config=config)

        tool_outputs = {
            "fundamentals": _invoke_tool(
                get_fundamentals,
                {"ticker": ticker, "curr_date": current_date},
            ),
            "balance_sheet": _invoke_tool(
                get_balance_sheet,
                {"ticker": ticker, "freq": "quarterly", "curr_date": current_date},
            ),
            "cashflow": _invoke_tool(
                get_cashflow,
                {"ticker": ticker, "freq": "quarterly", "curr_date": current_date},
            ),
            "income_statement": _invoke_tool(
                get_income_statement,
                {"ticker": ticker, "freq": "quarterly", "curr_date": current_date},
            ),
        }

        messages = [
            SystemMessage(
                content=(
                    system_message
                    + "\n\n你已经拿到基本面与财务报表结果。现在只基于这些结果写报告，"
                    "不要继续请求工具，不要输出 <longcat_tool_call>、XML、JSON 或伪函数调用。"
                    "请全程使用中文。"
                )
            ),
            HumanMessage(
                content=(
                    f"以下是为 {ticker} 在 {current_date} 主动获取的基本面资料。"
                    "请严格基于这些资料输出基本面分析；只有当工具结果明确失败或为空时，才允许说明数据不足。\n\n"
                    f"【get_fundamentals】\n{tool_outputs['fundamentals']}\n\n"
                    f"【get_balance_sheet】\n{tool_outputs['balance_sheet']}\n\n"
                    f"【get_cashflow】\n{tool_outputs['cashflow']}\n\n"
                    f"【get_income_statement】\n{tool_outputs['income_statement']}\n"
                )
            ),
        ]

        result = llm.invoke(messages)
        return {
            "fundamentals_report": result.content,
        }

    return fundamentals_analyst_node
