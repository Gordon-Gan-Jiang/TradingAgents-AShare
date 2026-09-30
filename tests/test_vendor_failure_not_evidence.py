"""取数失败不得被当成数据（见 docs/report-judgment-accuracy-audit.md §3.1）。

## 三个真实缺陷

1. **同一个判断被写了两遍，且各自漏一半。**
   - `tradingagents/tools/data.py:_VENDOR_FAILURE_MARKERS` 只认中文与少数英文；
   - `api/services/data_source_probe_service.py:_EMPTY_MARKERS` 认识
     `no data found`，但工具层不认识。
   于是 `No data found for symbol '600519.SH' between A and B`
   （`cn_akshare_provider.py:953` **真实返回**的文案）在工具层被当成**有效数据**。
   这类"两处各自维护同一判断"正是漏网的成因——所以要合到**一处**。

2. **路由层把「返回失败文案」记成 `status=hit` 并因此中断回退链。**
   `interface.py:163-166` 只要 provider 没抛异常就记 hit 且立即 `return`，
   于是一个"客气地返回错误文本"的源会把后面**本来可用**的源一起挡掉
   （`tests/test_data_stability_hardening.py` 的文件头把这条记为事故 4）。

3. **失败文案被当内容塞进分析师提示词。**
   `data_collector._safe` 把异常压成裸错误文本放进 `results`，分析师节点
   `pool.get("news", "无数据")` 原样插进提示词；而提示词要求 anchor 的 value
   「必须逐字拷贝自上文工具数据」，于是**照抄失败文案反而是合规的**——
   实测 59422 条证据锚点里 1222 条（2.1%）是失败文案。
"""

from __future__ import annotations

import pytest

#: 取自 provider 源码的**真实**返回文案（`grep -n "return f\"No data" providers/`）。
REAL_EMPTY_NOTICES = [
    "No data found for symbol '600519.SH' between 2026-01-01 and 2026-03-01",
    "No data found for 600519.SH for indicator rsi",
    "No news found for 600519.SH",
    "No news found for 600519.SH between 2026-01-01 and 2026-03-01",
    "Error: No data returned for rsi",
    "No data available for the specified date range.\n",
    "N/A：该日期暂无数据（可能未收盘、数据延迟或非交易日）",
    "N/A：该交易日暂无数据（可能停牌或数据延迟）",
]

#: provider 把异常吞成一句失败文案再正常 return 的痕迹（`cn_akshare` 有 20+ 处）。
REAL_FAILURE_NOTICES = [
    "板块历史行情获取失败（无替代源）：ConnectionError: Response ended prematurely",
    "筹码分布获取失败：KeyError: 'chip'",
    "No available vendor for method 'get_stock_data'.",
]


def _looks_like_vendor_failure(rendered):
    from tradingagents.tools.data import _looks_like_vendor_failure as fn

    return fn(rendered)


# ══════════════════════════════════════════════════════════════════════════
# 1. 真实文案必须被识别
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("text", REAL_EMPTY_NOTICES)
def test_real_empty_notices_are_not_data(text):
    assert _looks_like_vendor_failure(text) is True, (
        f"{text!r} 是 provider 真实的「没数据」文案，不能被当成有效数据"
    )


@pytest.mark.parametrize("text", REAL_FAILURE_NOTICES)
def test_real_failure_notices_are_not_data(text):
    assert _looks_like_vendor_failure(text) is True, f"{text!r} 是失败文案"


def test_normal_payloads_are_still_data():
    """误判防护：正常数据必须继续被判为数据。"""
    from tradingagents.dataflows.providers.cn_akshare_provider import (  # noqa: F401
        CnAkshareProvider,
    )

    normal = [
        "Date,Open,High,Low,Close,Volume\n2026-09-25,1.0,2.0,0.5,1.5,100",
        "| 板块 | 涨幅 |\n| --- | --- |\n| 小金属 | 3.2% |",
        "贵州茅台发布半年报，营业收入同比增长 11%。",  # 正文里出现「数据」二字不算失败
    ]
    for text in normal:
        assert _looks_like_vendor_failure(text) is False, f"正常数据被误判为失败：{text!r}"


# ══════════════════════════════════════════════════════════════════════════
# 2. 防漂移：工具层与探测层必须用**同一**判断
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("text", REAL_EMPTY_NOTICES + REAL_FAILURE_NOTICES)
def test_probe_layer_and_tool_layer_agree(text):
    """两层对同一文案必须给出一致结论（这正是本次漏网的成因）。"""
    from api.services.data_source_probe_service import _looks_empty_text

    tool_says = _looks_like_vendor_failure(text)
    probe_says = _looks_empty_text(text)
    assert tool_says or probe_says, f"两层都没识别出 {text!r}"
    assert tool_says is True, (
        f"{text!r} 被探测层识别、却被工具层漏掉——两层判断必须同源"
    )


def test_single_canonical_predicate_exists():
    """判断必须来自引擎层的**唯一**权威实现，供各层共用。"""
    from tradingagents.dataflows import vendor_text

    assert vendor_text.looks_like_empty_text("No data found for symbol 'X'")
    assert vendor_text.looks_like_failure_text("板块历史行情获取失败（无替代源）：X")
    assert not vendor_text.looks_like_empty_text("2026-09-25,1.0,2.0")
    assert vendor_text.classify_vendor_text("No news found for 600519.SH") == "empty"
    assert vendor_text.classify_vendor_text("获取失败：X") == "failure"
    assert vendor_text.classify_vendor_text("2026-09-25,1.0,2.0") == "data"


# ══════════════════════════════════════════════════════════════════════════
# 3. 路由层：返回失败文案不得记成 hit，也不得中断回退链
# ══════════════════════════════════════════════════════════════════════════


def _install_chain(monkeypatch, providers: dict):
    """把 vendor 链固定成 providers 的**插入顺序**。"""
    from tradingagents.dataflows import interface as iface

    order = list(providers)

    class _Registry:
        def list_names(self):
            return order

        def get(self, name):
            return providers[name]

    monkeypatch.setattr(iface, "_registry", _Registry())
    monkeypatch.setattr(iface, "get_vendor", lambda category, method=None: order[0])
    return iface


class _Prov:
    def __init__(self, name, value=None, exc=None):
        self.name = name
        self._value = value
        self._exc = exc

    def get_symbol_news(self, symbol, start, end):
        if self._exc is not None:
            raise self._exc
        return self._value


def test_returned_empty_notice_does_not_halt_the_chain(monkeypatch):
    """第一个源返回「No news found」时，必须继续问下一个源。

    这是本修复的要点：**「客气地返回错误文本」不该比「抛异常」更有杀伤力**。
    """
    iface = _install_chain(
        monkeypatch,
        {
            "first": _Prov("first", "No news found for 600519.SH"),
            "second": _Prov("second", "真实新闻正文"),
        },
    )
    out = iface.route_to_vendor("get_symbol_news", "600519.SH", "2026-03-01", "2026-03-04")
    assert out == "真实新闻正文", "失败文案不该挡住后面可用的源"


def test_returned_failure_text_does_not_halt_the_chain(monkeypatch):
    iface = _install_chain(
        monkeypatch,
        {
            "first": _Prov("first", "新闻获取失败（无替代源）：ConnectionError: boom"),
            "second": _Prov("second", "真实新闻正文"),
        },
    )
    out = iface.route_to_vendor("get_symbol_news", "600519.SH", "2026-03-01", "2026-03-04")
    assert out == "真实新闻正文"


def test_returned_notice_is_not_traced_as_hit(monkeypatch, capsys):
    iface = _install_chain(
        monkeypatch,
        {
            "first": _Prov("first", "No data found for symbol '600519.SH'"),
            "second": _Prov("second", "真实数据"),
        },
    )
    monkeypatch.setenv("TA_TRACE", "1")
    iface.route_to_vendor("get_symbol_news", "600519.SH", "2026-03-01", "2026-03-04")
    traces = capsys.readouterr().out
    assert "vendor=first status=hit" not in traces, (
        "返回失败文案的源不能被记为 hit —— 那会让运维以为该源正常"
    )


def test_all_notices_still_return_a_string_not_a_new_raise(monkeypatch):
    """**回归护栏**：全部源都只返回文案时，仍要返回字符串。

    `tools/data.py:950` 的调用点没有 try/except，它依赖「拿不到时拿到一句失败文案」
    这一形状来产出降级说明。若这里改成抛异常，工具面会从「降级」变成「报错」——
    那是把修复变成新 bug。
    """
    iface = _install_chain(
        monkeypatch,
        {
            "first": _Prov("first", "No news found for 600519.SH"),
            "second": _Prov("second", "No news found for 600519.SH"),
        },
    )
    out = iface.route_to_vendor("get_symbol_news", "600519.SH", "2026-03-01", "2026-03-04")
    assert isinstance(out, str)
    assert _looks_like_vendor_failure(out) is True


def test_non_string_empty_result_is_still_returned(monkeypatch):
    """**回归护栏**：`[]` 是合法数据，不得因为「看起来空」而被判失败。

    `tests/test_vendor_routing.py` 已依赖该行为，这里再钉一次。
    """
    iface = _install_chain(monkeypatch, {"only": _Prov("only", [])})
    assert iface.route_to_vendor("get_symbol_news", "600519.SH", "2026-03-01", "2026-03-04") == []


# ══════════════════════════════════════════════════════════════════════════
# 4. 采集层：失败必须自带标记，不能被当成内容
# ══════════════════════════════════════════════════════════════════════════


def test_safe_marks_fetch_failure_explicitly():
    """`_safe` 兜住的异常必须以**显眼标记**返回，而不是一段裸错误文本。

    分析师节点会把这个值直接插进提示词，裸错误文本看起来就是数据。
    """
    from tradingagents.graph.data_collector import DATA_FETCH_FAILURE_NOTICE, _safe

    class _Boom:
        name = "get_news"

        def invoke(self, payload):
            raise ConnectionError("Response ended prematurely")

    out = _safe(_Boom(), {})
    assert isinstance(out, str)
    assert DATA_FETCH_FAILURE_NOTICE in out, "失败值必须自带可识别的标记"
    assert _looks_like_vendor_failure(out) is True, "标记本身要能被失败判断识别"
    assert "Response ended prematurely" in out, "诊断信息仍要保留（可见即可查）"


def test_safe_returns_successful_result_unchanged():
    from tradingagents.graph.data_collector import _safe

    class _Ok:
        name = "get_news"

        def invoke(self, payload):
            return "正常新闻"

    assert _safe(_Ok(), {}) == "正常新闻"


# ══════════════════════════════════════════════════════════════════════════
# 5. 失败锚点不得进入 Research Manager 的简报
# ══════════════════════════════════════════════════════════════════════════


def test_failure_anchor_is_dropped_and_counted():
    """可确定性地强制执行「失败串不得进入 evidence_anchors」。

    提示词要求 anchor 的 value「逐字拷贝自上文工具数据」，所以只要失败文案出现在
    上文，模型照抄就是「合规」的。因此必须在我们自己的渲染出口把这类锚点剔掉，
    并且**说出剔了几条**——静默丢弃等于把数据质量问题藏起来。
    """
    from tradingagents.agents.utils.analyst_structured import (
        build_structured_brief_for_research_manager,
    )

    traces = [
        {
            "agent": "news_analyst",
            "verdict": "中性",
            "structured": {
                "agent_role": "news_analyst",
                "direction": "中性",
                "confidence": 50,
                "evidence_anchors": [
                    {
                        "field": "news",
                        "value": "get_news 调用失败：ConnectionError: Response ended prematurely",
                        "source": "get_news",
                    },
                    {"field": "pe", "value": "28.4", "source": "get_fundamentals"},
                ],
                "data_gaps": [],
            },
        }
    ]
    brief = build_structured_brief_for_research_manager(traces)
    assert "ConnectionError" not in brief, "失败文案不得作为证据锚点出现在简报里"
    assert "pe=28.4" in brief, "正常锚点必须保留"
    assert "剔除" in brief or "失败锚点" in brief, "剔除了几条必须说出来，不能静默"


def test_normal_anchors_are_untouched():
    from tradingagents.agents.utils.analyst_structured import (
        build_structured_brief_for_research_manager,
    )

    traces = [
        {
            "agent": "market_analyst",
            "verdict": "看多",
            "structured": {
                "direction": "看多",
                "confidence": 70,
                "evidence_anchors": [
                    {"field": "close", "value": "1650.0", "source": "get_stock_data"},
                ],
            },
        }
    ]
    brief = build_structured_brief_for_research_manager(traces)
    assert "close=1650.0" in brief
    assert "剔除" not in brief
