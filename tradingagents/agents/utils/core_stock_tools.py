from langchain_core.tools import tool
from typing import Annotated
from tradingagents.tools.data import data_tool_impl as _data_tool_impl

def _delegate(name: str):
    """委托到工具层的唯一实现（design D1：能力定义只有一份）。

    本模块只保留 `@tool` 包装与 docstring——它们是分析图的接口面，
    行为必须逐字不变；取数逻辑与参数顺序收敛到 `tools/data.py` 的声明表。
    """
    return _data_tool_impl(name)


@tool
def get_stock_data(
    symbol: Annotated[str, "ticker symbol of the company"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
) -> str:
    """
    Retrieve stock price data (OHLCV) for a given ticker symbol.
    Uses the configured core_stock_apis vendor.
    Args:
        symbol (str): Ticker symbol of the company, e.g. AAPL, TSM
        start_date (str): Start date in yyyy-mm-dd format
        end_date (str): End date in yyyy-mm-dd format
    Returns:
        str: A formatted dataframe containing the stock price data for the specified ticker symbol in the specified date range.
    """
    return _delegate("get_stock_data")(symbol, start_date, end_date)


@tool
def get_realtime_quotes(
    symbols: Annotated[list[str], "List of ticker symbols"],
) -> str:
    """Retrieve realtime quote snapshots for the given symbols."""
    return _delegate("get_realtime_quotes")(symbols)
