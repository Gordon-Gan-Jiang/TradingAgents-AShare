"""A 股板块数据工具（市场主线洞察 M1）。

这些工具通过 route_to_vendor 走 cn_board_data 供应商链（默认 cn_akshare），
供 LLM Agent 直接调用；规则层（mainline_scoring.py）则直接使用 provider 的
fetch_* 函数拿结构化 DataFrame，不走文本工具。
"""
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
def get_board_spot(
    sector_type: Annotated[str, "板块类型：industry=行业板块, concept=概念板块"],
) -> str:
    """获取 A 股板块涨幅榜（行业/概念），返回板块名称、当日涨跌幅、换手率、上涨/下跌家数、领涨股。"""
    return _delegate("get_board_spot")(sector_type)


@tool
def get_board_rank(
    sector_type: Annotated[str, "板块类型：industry=行业板块, concept=概念板块"],
    period: Annotated[str, "资金流周期：1d=今日, 3d=3日, 5d=5日, 10d=10日"],
) -> str:
    """获取板块资金流排行（行业/概念），返回净流入排名，用于判断资金聚焦方向。"""
    return _delegate("get_board_rank")(sector_type, period)


@tool
def get_board_hist(
    board_name: Annotated[str, "板块名称，如 小金属 / CPO概念"],
    sector_type: Annotated[str, "板块类型：industry=行业板块, concept=概念板块"],
) -> str:
    """获取板块历史行情（近60日涨跌幅），用于判断主线持续性与趋势斜率。"""
    return _delegate("get_board_hist")(board_name, sector_type)


@tool
def get_board_cons(
    board_name: Annotated[str, "板块名称，如 小金属 / CPO概念"],
    sector_type: Annotated[str, "板块类型：industry=行业板块, concept=概念板块"],
) -> str:
    """获取板块成分股列表（按当日涨幅排序），用于主线内选股。"""
    return _delegate("get_board_cons")(board_name, sector_type)
