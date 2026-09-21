"""新闻取数：把既有新闻能力适配成结构化条目。

## 为什么需要适配而不是直接用 `get_news`

既有 `get_news` 走 `route_to_vendor` 返回预格式化文本，适合喂提示词，但无法
被回答层的机械校验器引用（没有可核对的结构化日期）。这里的职责是把新闻取成
`NewsItem`（含 `published_at`），使其成为可引用、可核对的证据。

## ⚠️ 未实测

当前开发环境无网络（akshare 返回 ConnectionError），因此本模块的取数**未做
联网实测**。设计约束是「失败即降级」：取不到就抛出，由新闻工具把它转成
`Coverage.unavailable = ("news",)`，回答层据此声明「新闻维度未覆盖」——
不会因为接口字段变化而编造原因。

联网后需实测清单见 tasks.md 组 1。
"""

from __future__ import annotations

import logging
from typing import Sequence

from tradingagents.tools.news import NewsItem

logger = logging.getLogger(__name__)

# akshare 的列名口径（不同版本可能不同，逐个尝试）
_TITLE_COLUMNS = ("新闻标题", "title")
_TIME_COLUMNS = ("发布时间", "时间", "publish_time")
_SOURCE_COLUMNS = ("文章来源", "来源", "source")
_URL_COLUMNS = ("新闻链接", "链接", "url")
_SUMMARY_COLUMNS = ("新闻内容", "内容", "summary")


def _pick(columns: Sequence[str], candidates: Sequence[str]) -> str | None:
    for name in candidates:
        if name in columns:
            return name
    return None


def akshare_news_fetcher(symbol: str, start: str, end: str) -> Sequence[NewsItem]:
    """取个股新闻（经 vendor 路由，共享三级降级链）。

    只返回 `start ~ end` 窗口内的条目；窗口过滤在工具层还会再执行一次，
    这里提前过滤是为了避免把无关旧闻传进证据集。
    """
    from tradingagents.dataflows.providers.cn_research_data import fetch_symbol_news_via_router

    return fetch_symbol_news_via_router(symbol, start, end)
