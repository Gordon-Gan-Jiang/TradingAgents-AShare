"""新闻工具：因果类问句的事实来源（design D23）。

## 为什么必须结构化

「为什么今天跌」若只有量价资金数据，模型**必然会编原因**。而既有
`get_news` 返回的是预格式化的中文文本，无法被数值/日期校验器引用。

因此这里产出的每一条新闻都带 `published_at`，作为可引用的证据项：
回答层若要写「3 月 3 日公告了减持」，校验器就能核对这个日期是否真在证据里。

## 拿不到新闻时必须显式声明

「没有新闻」与「新闻没拉到」是两件完全不同的事。前者可以说「无相关消息」，
后者必须说「新闻维度未覆盖」——把它们合并会凭空制造一个结论。
"""

from __future__ import annotations

import pytest


def _item(title="公司发布减持公告", published_at="2026-03-03", source="证券时报"):
    from tradingagents.tools.news import NewsItem

    return NewsItem(title=title, published_at=published_at, source=source, url="https://example.com/n/1")


def _tools(items=None, *, raises=None):
    from tradingagents.tools.news import make_news_tools

    def fetcher(symbol, start, end):
        if raises is not None:
            raise raises
        return items if items is not None else []

    return {s.name: s for s in make_news_tools(fetcher)}


# ══════════════════════════════════════════════════════════════════════════
# 结构化结果
# ══════════════════════════════════════════════════════════════════════════


def test_news_items_become_citable_evidence():
    spec = _tools([_item(), _item(title="机构调研纪要", published_at="2026-03-02")])["data_symbol_news"]
    result = spec.handler(symbol="600519.SH", as_of="2026-03-04", lookback_days=5)

    assert result.data["count"] == 2
    assert len(result.evidence) == 2
    for item in result.evidence:
        assert item.unit == "text"
        assert item.source_key == "symbol_news"
        # 发布日期必须成为证据的 as_of，回答层才能核对日期断言
        assert item.as_of.startswith("2026-03-")
    assert result.coverage.available == ("symbol_news",)
    assert result.coverage.unavailable == ()


def test_news_tool_reports_unavailable_when_nothing_is_fetched():
    """「没拉到」不是「没有新闻」：必须标为未覆盖，不能凭空说「无相关消息」。"""
    spec = _tools([])["data_symbol_news"]
    result = spec.handler(symbol="600519.SH", as_of="2026-03-04", lookback_days=5)

    assert result.data["count"] == 0
    assert "symbol_news" in result.coverage.unavailable
    assert result.coverage.available == ()
    assert result.degraded is True
    assert result.degraded_reason


def test_fetcher_failure_degrades_instead_of_raising():
    """取数失败不得中断整轮对话；由覆盖声明把「未覆盖」传下去。"""
    spec = _tools(raises=ConnectionError("network down"))["data_symbol_news"]
    result = spec.handler(symbol="600519.SH", as_of="2026-03-04", lookback_days=5)

    assert "symbol_news" in result.coverage.unavailable
    assert result.degraded is True


def test_news_outside_the_lookback_window_is_excluded():
    """超出回看窗口的新闻不得混入证据——否则回答会引用与本次判断无关的旧闻。"""
    spec = _tools([
        _item(published_at="2026-03-03"),
        _item(title="上个月的旧闻", published_at="2026-02-01"),
    ])["data_symbol_news"]
    result = spec.handler(symbol="600519.SH", as_of="2026-03-04", lookback_days=3)

    assert result.data["count"] == 1
    assert "2026-03-03" in {e.as_of for e in result.evidence}


def test_news_metadata_declares_cost_and_no_judgement():
    spec = _tools([_item()])["data_symbol_news"]
    assert spec.cost == "cheap"
    assert spec.latency_budget_ms > 0
    assert spec.output_contract == "SYMBOL_NEWS"
    # 事实类工具不带研究免责声明：它不断言任何判断
    result = spec.handler(symbol="600519.SH", as_of="2026-03-04", lookback_days=5)
    assert result.disclaimer is None


def test_news_tool_requires_symbol_and_as_of():
    from tradingagents.tools.news import make_news_tools

    spec = {s.name: s for s in make_news_tools(lambda *a: [])}["data_symbol_news"]
    assert spec.input_schema["required"] == ["symbol", "as_of"]


# ══════════════════════════════════════════════════════════════════════════
# 注册与暴露
# ══════════════════════════════════════════════════════════════════════════


def test_news_tool_is_registered_when_a_fetcher_is_injected():
    from tradingagents.tools import build_registry

    registry = build_registry(news_fetcher=lambda *a: [])
    assert "data_symbol_news" in registry.names()


def test_news_tool_absent_without_a_fetcher():
    """未注入取数器时不装配——避免出现「在册但一调就炸」的半成品工具。"""
    from tradingagents.tools import build_registry

    assert "data_symbol_news" not in build_registry().names()


def test_news_tool_is_exposed_over_mcp():
    """外部 Agent 最需要的正是事实类工具（新闻/公告），因此它必须在同步面上。"""
    from api.mcp_server import McpServerCore
    from tradingagents.tools import build_registry

    core = McpServerCore(build_registry(news_fetcher=lambda *a: []))
    assert "data_symbol_news" in {t["name"] for t in core.list_tools()}


# ══════════════════════════════════════════════════════════════════════════
# 因果类问句的工具计划
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "text",
    ["600519 为什么今天跌", "600519 今天大跌什么原因", "600519 怎么回事", "600519 有何利空"],
)
def test_causal_questions_plan_the_news_tool(text):
    """因果类问句除了探针还要取新闻，否则模型只能从价格行为倒推原因。"""
    from tradingagents.agent.loop import _plan_tools, TurnRequest

    planned = _plan_tools(
        TurnRequest(text=text, user_id="u1", tier="medium", symbol="600519.SH", as_of="2026-03-04")
    )
    names = [c.tool for c in planned]
    assert "signal_washout_probe" in names
    assert "data_symbol_news" in names


def test_non_causal_questions_do_not_plan_the_news_tool():
    """非因果问句不取新闻：省一次调用，也避免把无关新闻塞进证据集。"""
    from tradingagents.agent.loop import _plan_tools, TurnRequest

    planned = _plan_tools(
        TurnRequest(text="600519 是不是在洗盘", user_id="u1", tier="medium",
                    symbol="600519.SH", as_of="2026-03-04")
    )
    assert "data_symbol_news" not in [c.tool for c in planned]


def test_news_plan_is_skipped_in_light_tier():
    """轻档不加**新闻取数**——「现在多少钱」不需要新闻。

    断言针对新闻工具本身：原先写成 `planned == []`，把"轻档不调任何工具"
    一并固化（那是 bug：轻档该调一个便宜的数据工具，见
    `tests/test_factual_tool_planning.py`）。
    """
    from tradingagents.agent.loop import _plan_tools, TurnRequest

    planned = _plan_tools(
        TurnRequest(text="600519 为什么跌", user_id="u1", tier="light",
                    symbol="600519.SH", as_of="2026-03-04")
    )
    assert "data_symbol_news" not in [call.tool for call in planned]
