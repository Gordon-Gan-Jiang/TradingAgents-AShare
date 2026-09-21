"""新闻工具：因果类问句的事实来源（design D23）。

## 为什么必须结构化，而不是复用 `get_news`

既有 `get_news`（`agents/utils/news_data_tools.py`）返回预格式化的中文文本，
适合喂提示词，但**无法被回答层的机械校验器引用**：校验器只能核对「这个数字/
日期是否出现在证据里」，而一段散文没有可引用的结构化日期。

于是「为什么今天跌」会退化成模型从价格行为倒推原因——那正是 D23 要堵的漏洞。
这里产出的每条新闻都带 `published_at`，成为可引用、可核对的证据项。

## 「没拉到」不等于「没有新闻」

两者合并会凭空制造结论：前者必须说「新闻维度未覆盖」，后者才可以说
「暂无相关消息」。因此取数失败时 `Coverage.unavailable` 会包含 `news`，
由回答层显式声明。

## 事实类工具不带研究免责声明

免责声明约束的是**判断**（洗盘/派发是对他人意图的推断）。新闻工具只陈述
事实，附加免责声明反而会让用户以为新闻列表也被降级处理过。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from tradingagents.grounding.types import Coverage, EvidenceItem

from .spec import ToolResult, ToolSpec

# 取数端口：注入实现，测试直接给内存列表
NewsFetcher = Callable[[str, str, str], Sequence["NewsItem"]]

# 必须与 freshness 契约的源键一致（contract.yaml 的 symbol_news）。
# 早期写成 "news" 会命中旧管道的同名条目，导致 freshness 归属到另一个源。
SOURCE_KEY = "symbol_news"


@dataclass(frozen=True)
class NewsItem:
    """一条新闻/公告。`published_at` 决定它能否成为可引用的日期证据。"""

    title: str
    published_at: str
    source: str = ""
    url: str = ""
    summary: str = ""


def _in_window(item: NewsItem, *, start: str, end: str) -> bool:
    published = (item.published_at or "")[:10]
    if not published:
        return False
    return start <= published <= end


def make_news_tools(fetcher: NewsFetcher) -> tuple[ToolSpec, ...]:
    """构造新闻类工具。取数器由应用层注入（与信号端口同一套注入模式）。"""

    def symbol_news(symbol: str, as_of: str, lookback_days: int = 3) -> ToolResult:
        from tradingagents.signals.outcome import add_trading_days

        # 回看窗口用交易日：自然日会在长假前后把窗口拉得过宽或过窄
        negative = -abs(int(lookback_days))
        try:
            start = add_trading_days(as_of, negative)
        except Exception:  # noqa: BLE001 — 日历不可用时退化为不设下界
            start = ""

        try:
            raw_items = list(fetcher(symbol, start or as_of, as_of) or [])
            failure = None
        except Exception as exc:  # noqa: BLE001 — 取数失败降级，不中断对话
            raw_items = []
            failure = f"{type(exc).__name__}: {exc}"

        items = [i for i in raw_items if not start or _in_window(i, start=start, end=as_of)]

        from .freshness import evaluate_tool_sources

        if not items:
            freshness = evaluate_tool_sources(
                source_keys=(SOURCE_KEY,), as_of=as_of, unavailable=(SOURCE_KEY,)
            )
        else:
            # 有新闻但只验证了「最新一条的发布日期 ≤ as_of」；这不足以断言该源新鲜，
            # 因此用 cutoffs 交给契约判定（最新发布日期早于 as_of 会标 stale）。
            latest = max((i.published_at or "")[:10] for i in items)
            freshness = evaluate_tool_sources(
                source_keys=(SOURCE_KEY,), as_of=as_of, cutoffs={SOURCE_KEY: latest}
            )

        if not items:
            # 「没拉到」与「确实没有」在这里被区分开：二者都置为未覆盖，
            # 但原因不同，回答层据此决定措辞
            reason = (
                f"新闻取数失败（{failure}），该维度未覆盖"
                if failure
                else f"回看窗口 {start or '不限'} ~ {as_of} 内未取到新闻，该维度未覆盖"
            )
            return ToolResult(
                data={"symbol": symbol, "as_of": as_of, "count": 0, "items": []},
                evidence=(),
                coverage=Coverage(available=(), unavailable=(SOURCE_KEY,)),
                degraded=True,
                degraded_reason=reason,
                as_of=as_of,
                freshness=freshness,
            )

        evidence = tuple(
            EvidenceItem(
                id=f"E{i + 1}",
                field="news_title",
                value=item.title,
                unit="text",
                source_key=SOURCE_KEY,
                # 发布日期即该条证据的 as_of：回答层写「3 月 3 日公告了减持」时，
                # 校验器就是靠这个字段核对日期断言的
                as_of=(item.published_at or "")[:10],
            )
            for i, item in enumerate(items)
        )

        return ToolResult(
            data={
                "symbol": symbol,
                "as_of": as_of,
                "count": len(items),
                "items": [
                    {
                        "title": item.title,
                        "published_at": (item.published_at or "")[:10],
                        "source": item.source,
                        "url": item.url,
                        "summary": item.summary,
                    }
                    for item in items
                ],
            },
            evidence=evidence,
            coverage=Coverage(available=(SOURCE_KEY,), unavailable=()),
            as_of=as_of,
            freshness=freshness,
        )

    spec = ToolSpec(
        name="data_symbol_news",
        title="个股新闻与公告",
        description=(
            "获取指定 A 股在回看窗口内的新闻与公告标题，返回结构化条目（含发布日期与来源）。"
            "用于回答「为什么今天跌」这类因果问题：原因必须有事实来源，不得从价格行为倒推。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "A 股代码，如 600519.SH"},
                "as_of": {"type": "string", "description": "基准日 YYYY-MM-DD"},
                "lookback_days": {
                    "type": "integer",
                    "description": "回看交易日数",
                    "default": 3,
                },
            },
            "required": ["symbol", "as_of"],
        },
        output_contract="SYMBOL_NEWS",
        handler=symbol_news,
        cost="cheap",
        latency_budget_ms=8000,
        freshness_sources=(SOURCE_KEY,),
        requires=(),
    )
    return (spec,)
