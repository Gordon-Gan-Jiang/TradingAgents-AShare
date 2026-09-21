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
def get_board_fund_flow() -> str:
    """获取今日行业板块资金流向排名，用于判断板块轮动信号和个股所在板块的资金吸引力。"""
    return _delegate("get_board_fund_flow")()


@tool
def get_individual_fund_flow(
    symbol: Annotated[str, "股票代码，格式如 600519.SH"],
) -> str:
    """获取个股近5日主力资金净流向，判断机构资金进出方向。symbol 格式如 600519.SH。"""
    return _delegate("get_individual_fund_flow")(symbol)


@tool
def get_lhb_detail(
    symbol: Annotated[str, "股票代码，格式如 600519.SH"],
    date: Annotated[str, "日期，格式 YYYY-MM-DD"],
) -> str:
    """获取个股龙虎榜数据，非异动日无数据属正常。symbol 格式如 600519.SH，date 格式 YYYY-MM-DD。"""
    return _delegate("get_lhb_detail")(symbol, date)


@tool
def get_zt_pool(
    date: Annotated[str, "日期，格式 YYYY-MM-DD"],
) -> str:
    """获取市场涨停板情绪池，反映市场整体情绪温度，date 格式 YYYY-MM-DD。"""
    return _delegate("get_zt_pool")(date)


@tool
def get_hot_stocks_xq() -> str:
    """获取雪球热搜股票列表，反映散户当前关注热点。"""
    return _delegate("get_hot_stocks_xq")()
