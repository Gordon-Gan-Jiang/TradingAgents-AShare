"""轻档（事实型）问句必须真的取数。

## 这个 bug 的形态

黄金集基线（Phase 0）显示 `factual_lookup` 8 条里 **7 条缺必需工具**。实测：

    「600519 现在多少钱」        → 计划工具: []   回答：本轮无可用证据，无法给出当前价格的有依据判断
    「600519 的主力资金净流入多少」 → 计划工具: []   回答：本轮无可用证据……

根因是**文档与实现不一致**：`routing.py` 的档位表写着轻档 =「**1 个数据工具** + 1 次汇总」，
但 `_plan_tools` 对轻档直接 `return []`（注释本意是"轻档**不调用探针**"——探针 5× 成本
确实不该付，但实现连**便宜的数据工具**也没调）。结果：最高频的问题类型永远答"无可用证据"。

## 本文件锁住什么

1. 轻档**必须**计划恰好一个便宜的数据工具（按问句语义选，规则优先、零 LLM）。
2. 无标的的轻档问句只能选**市场级**工具（涨停/龙虎榜/板块/全局新闻），不能凭空
   给一个需要代码的工具——那会必然失败。
3. 中档仍计划探针（不能被这次改动波及）。
4. 黄金集里 `factual_lookup` 的工具召回必须达到 100%——**用度量证明修好了**，
   而不是"我觉得修好了"。
"""

from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    "text,symbol,expected",
    [
        ("600519 现在多少钱", "600519.SH", "get_realtime_quotes"),
        ("300750 今天换手率多少", "300750.SZ", "get_realtime_quotes"),
        # 期望值随"没有证据就没有回答"这条硬约束更新：旧 get_individual_fund_flow
        # 返回预格式化文本、evidence=()，取到数也无法被引用；capital_flow_read
        # 给数值（元）+ 弃权声明。
        ("600519 的主力资金净流入多少", "600519.SH", "capital_flow_read"),
        ("贵州茅台市盈率多少", "600519.SH", "get_fundamentals"),
        ("600519 资产负债率是多少", "600519.SH", "get_balance_sheet"),
        ("600519 最近有什么新闻", "600519.SH", "data_symbol_news"),
        ("今天的涨停板有多少只", None, "get_zt_pool"),
        ("今天龙虎榜有哪些", None, "get_lhb_detail"),
    ],
)
def test_factual_question_maps_to_a_cheap_tool(text, symbol, expected):
    from tradingagents.agent.routing import plan_factual_tool

    assert plan_factual_tool(text, has_symbol=bool(symbol)) == expected


def test_symbolless_question_never_gets_a_symbol_requiring_tool():
    """无标的时不能选需要代码的工具——那是一次注定失败的调用。"""
    from tradingagents.agent.routing import SYMBOL_REQUIRED_TOOLS, plan_factual_tool

    tool = plan_factual_tool("市盈率多少", has_symbol=False)
    assert tool not in SYMBOL_REQUIRED_TOOLS, f"无标的却选了 {tool}"


@pytest.mark.parametrize(
    "text,symbol,expected",
    [
        # 本轮新增的四条真数据源此前**没有任何规则入口**：筹码/分时/公告/指数
        # 的问句只能落回 `get_board_spot`，用户问筹码拿到板块快照。
        ("600519 最近筹码集中度怎么样", "600519.SH", "chip_distribution_read"),
        ("600519 今天分时承接怎么样", "600519.SH", "intraday_bars_read"),
        ("600519 最近有什么公告", "600519.SH", "announcements_read"),
        # 公告要盖过新闻（同一段里有"公告"）：公告是**权威披露**，新闻不是。
        ("000001.SZ 最近有什么新闻", "000001.SZ", "data_symbol_news"),
        ("今天上证指数多少点", None, "index_quote_read"),
        ("我的自选股有哪些", None, "watchlist_read"),
        ("你的数据是新的吗", None, "data_freshness_check"),
    ],
)
def test_new_evidence_tools_are_reachable_by_rules(text, symbol, expected):
    """新增的 L2 证据工具必须有**规则可达**的入口。

    规划层只在"规则解析不出标的"时才跑（成本决定）。而带代码的问句一定能解析出
    标的——于是这类问句**永远走不到规划层**，只能靠这张提示词表选工具。
    表里没有的条目 = 那个工具对这类问句根本不可选，"封了但用不上"。
    """
    from tradingagents.agent.routing import plan_factual_tool

    assert plan_factual_tool(text, has_symbol=bool(symbol)) == expected


def test_symbol_requiring_new_tools_are_declared_as_such():
    """需要代码的新工具必须登记，否则无标的问句会选中一次必然失败的调用。"""
    from tradingagents.agent.routing import SYMBOL_REQUIRED_TOOLS

    for name in ("chip_distribution_read", "intraday_bars_read", "announcements_read"):
        assert name in SYMBOL_REQUIRED_TOOLS, f"{name} 需要代码却没登记"


@pytest.mark.parametrize(
    "text",
    [
        "600519 最近筹码集中度怎么样",
        "600519 今天分时承接怎么样",
        "600519 最近有什么公告",
        "今天上证指数多少点",
    ],
)
def test_new_evidence_questions_route_to_the_light_tier(text):
    """这些问句必须是**轻档**：中档会去跑判定探针，答的是另一个问题。

    档位标记表决定了走哪条路：中档 → 洗盘探针（5× 成本、答非所问），
    轻档 → 按语义选一个便宜的证据工具。实测「最近筹码集中度怎么样」落进中档，
    于是用户问筹码集中度，拿到的是洗盘判定。
    """
    from tradingagents.agent.routing import TIER_LIGHT, RuleTierClassifier

    class _Resolver:
        def resolve(self, _text, symbol=None):
            return type(
                "R", (), {"symbol": "600519.SH", "symbols": (), "candidates": ()}
            )()

    assert RuleTierClassifier(_Resolver()).classify(text).tier == TIER_LIGHT


def test_index_question_without_symbol_is_market_level():
    """「今天上证指数多少点」不需要标的，不该被追问代码。"""
    from tradingagents.agent.routing import looks_like_market_level_factual

    assert looks_like_market_level_factual("今天上证指数多少点") is True


def test_unknown_factual_question_falls_back_to_broadest_cheap_fact():
    from tradingagents.agent.routing import plan_factual_tool

    assert plan_factual_tool("600519 怎么样", has_symbol=True) == "get_realtime_quotes"




def _registry():
    """真实注册表：计划参数必须**按工具 schema 生成**，用真表才能验证这一点。"""
    from tradingagents.tools import build_registry

    return build_registry(
        ports_factory=lambda *a: object(), news_fetcher=lambda *a: [], market_screener=object()
    )

# ── 编排：轻档计划一个数据工具，中档仍走探针 ──────────────────────────────


def test_light_tier_plans_exactly_one_data_tool():
    from tradingagents.agent.loop import TurnRequest, _plan_tools

    planned = _plan_tools(
        TurnRequest(text="600519 现在多少钱", user_id="u1", symbol="600519.SH", tier="light",
                    as_of="2026-09-21"),
        registry=_registry(),
    )
    assert [call.tool for call in planned] == ["get_realtime_quotes"]
    # 参数名必须来自工具自己的 schema——实测这个工具要的是 `symbols`（列表）
    assert planned[0].arguments["symbols"] == ["600519.SH"]


def test_light_tier_does_not_call_the_expensive_probe():
    """轻档不该付探针的 5× 成本——这是原注释的本意，必须保持。"""
    from tradingagents.agent.loop import TurnRequest, _plan_tools

    planned = _plan_tools(
        TurnRequest(text="600519 现在多少钱", user_id="u1", symbol="600519.SH", tier="light",
                    as_of="2026-09-21"),
        registry=_registry(),
    )
    assert "signal_washout_probe" not in [call.tool for call in planned]


def test_medium_tier_still_plans_the_probe():
    from tradingagents.agent.loop import TurnRequest, _plan_tools

    planned = _plan_tools(
        TurnRequest(text="600519 是不是在洗盘", user_id="u1", symbol="600519.SH", tier="medium"),
        registry=_registry(),
    )
    assert [call.tool for call in planned] == ["signal_washout_probe"]


def test_heavy_tier_still_plans_nothing_here():
    """重档由既有 LangGraph 管道负责，本循环不执行工具（语义不能变）。"""
    from tradingagents.agent.loop import TurnRequest, _plan_tools

    planned = _plan_tools(
        TurnRequest(text="全面分析一下 600519", user_id="u1", symbol="600519.SH", tier="heavy"),
        registry=_registry(),
    )
    assert planned == []


def test_market_level_light_question_plans_symbolless_tool():
    from tradingagents.agent.loop import TurnRequest, _plan_tools

    planned = _plan_tools(
        TurnRequest(text="今天的涨停板有多少只", user_id="u1", tier="light", as_of="2026-09-21"),
        registry=_registry(),
    )
    assert [call.tool for call in planned] == ["get_zt_pool"]
    # 这个工具要的是 `date`，且**不该**带标的参数
    assert "symbol" not in planned[0].arguments and planned[0].arguments["date"] == "2026-09-21"


# ── 用度量证明修好了 ──────────────────────────────────────────────────────


def test_golden_factual_lookup_tool_recall_is_complete():
    """黄金集事实型用例的工具召回必须 100%。

    这一条把"修复"与"度量"绑在一起：黄金集是 Phase 0 建立的尺子，
    如果它没变好，那就是没修好。
    """
    from tests.golden.harness import load_golden, probe

    items = [item for item in load_golden() if item.intent == "factual_lookup"]
    assert items, "黄金集里应有 factual_lookup 用例"
    misses = []
    for item in items:
        observed = set(probe(item.text).tools)
        missing = set(item.required_tools) - observed
        if missing:
            misses.append((item.id, item.text, sorted(missing), sorted(observed)))
    assert not misses, f"仍有事实型用例没计划到必需工具：{misses}"


def test_portfolio_question_is_never_answered_with_market_data():
    """「我账户现在仓位多少」是组合问题，不能拿板块行情去答。

    这条是度量抓回来的回归：市场级事实通路放进来之后，该问句被路由到
    `get_board_spot`（板块行情）并当成"已服务"——比诚实追问糟得多。
    """
    from api.services import chat_service
    from tradingagents.agent.routing import looks_like_market_level_factual

    assert looks_like_market_level_factual("我账户现在仓位多少") is False
    assert looks_like_market_level_factual("我持仓里哪只最危险") is False
    assert looks_like_market_level_factual("自选股有要止损的吗") is False

    body = chat_service.ChatRequest(
        messages=[{"role": "user", "content": "我账户现在仓位多少"}]
    )
    assert chat_service.build_turn_request(body, user_id="u1") is None, (
        "组合问题在缺组合工具时应诚实追问，而不是用行情凑一个答案"
    )


def test_planned_arguments_are_accepted_by_the_registry():
    """计划出来的参数必须能真的被注册表接受——这是"未知参数"那个 bug 的回归测试。

    曾经按 `symbol`/`as_of` 硬猜参数名，每次调用都被注册表以
    `unknown arguments` 拒掉，而回答只显示"无可用证据"——原因在 trace 里才看得到。
    """
    from tradingagents.agent.loop import TurnRequest, _plan_tools

    registry = _registry()
    cases = [
        ("600519 现在多少钱", "600519.SH"),
        ("600519 的主力资金净流入多少", "600519.SH"),
        ("贵州茅台市盈率多少", "600519.SH"),
        ("600519 资产负债率是多少", "600519.SH"),
        ("000001.SZ 最近有什么新闻", "000001.SZ"),
        ("今天的涨停板有多少只", None),
    ]
    for text, symbol in cases:
        planned = _plan_tools(
            TurnRequest(text=text, user_id="u1", symbol=symbol, tier="light", as_of="2026-09-21"),
            registry=registry,
        )
        assert planned, f"{text!r} 没计划到任何工具"
        call = planned[0]
        spec = registry.get(call.tool)
        properties = set((spec.input_schema.get("properties") or {}).keys())
        unknown = set(call.arguments) - properties
        assert not unknown, f"{call.tool} 收到未知参数 {sorted(unknown)}（合法参数：{sorted(properties)}）"
        required = set(spec.input_schema.get("required") or ())
        assert required <= set(call.arguments), f"{call.tool} 缺少必填参数 {sorted(required - set(call.arguments))}"


def test_market_level_factual_turn_is_not_treated_as_missing_symbol():
    """市场级事实回合不得被澄清分支拦下。

    实测踩到：「今天涨停板有多少只」被构造成了回合（tier=light、无标的），
    但 `_prequel` 的"没标的就追问"把它拦回去问"请告诉我代码"——平台明明有
    涨停池工具。修法是给这类回合一个显式标记（`market_level`），而不是让
    澄清分支靠猜。
    """
    from api.services import chat_service
    from tradingagents.agent.events import ListSink
    from tradingagents.agent.loop import run_turn
    from tradingagents.tools import build_registry

    body = chat_service.ChatRequest(
        messages=[{"role": "user", "content": "今天的涨停板有多少只"}]
    )
    request = chat_service.build_turn_request(body, user_id="u1")
    assert request is not None and request.market_level is True

    class _Clock:
        def now(self):
            return 0.0

    sink = ListSink()
    result = run_turn(
        request=request,
        registry=build_registry(ports_factory=lambda *a: object(), news_fetcher=lambda *a: []),
        model=None,
        sink=sink,
        clock=_Clock(),
    )
    assert "请告诉我具体的股票代码" not in result.answer, "市场级问句不该被追问标的"


@pytest.mark.parametrize(
    "text,symbol,expected",
    [
        # L1 只读工具里的两个"报告类"落点：单测早就封好了，但**没有任何规则入口**
        # ——带代码/名称的问句一定能解析出标的，于是永远走不到规划层，只能靠这张表。
        # 实测：「600519 最近这份报告的多视角共识怎么样」落到中档去跑洗盘探针，
        # 「帮我找一份贵州茅台的研报结论」同理（黄金集 h03 记的就是这个缺口）。
        ("600519 最近这份报告的多视角共识怎么样", "600519.SH", "report_consensus_read"),
        ("600519 的多视角共识如何", "600519.SH", "report_consensus_read"),
        ("帮我找一份贵州茅台的研报结论", "600519.SH", "report_conclusion_lookup"),
        ("之前那份报告结论是什么", "600519.SH", "report_conclusion_lookup"),
    ],
)
def test_report_tools_are_reachable_by_rules(text, symbol, expected):
    from tradingagents.agent.routing import plan_factual_tool

    assert plan_factual_tool(text, has_symbol=bool(symbol)) == expected


def test_report_consensus_needs_a_symbol():
    """共识是**某一个标的**的报告共识；无标的时不能选它（必然失败的调用）。"""
    from tradingagents.agent.routing import SYMBOL_REQUIRED_TOOLS, plan_factual_tool

    assert "report_consensus_read" in SYMBOL_REQUIRED_TOOLS
    assert plan_factual_tool("多视角共识怎么样", has_symbol=False) != "report_consensus_read"


@pytest.mark.parametrize(
    "text",
    ["600519 最近这份报告的多视角共识怎么样", "帮我找一份贵州茅台的研报结论"],
)
def test_report_questions_are_light_tier(text):
    """报告类问句是**读既有结论**，不是跑一遍研判。

    中档会去调洗盘探针——用户问"上一份报告怎么说"，平台答"洗盘还是派发"。
    重档判定（`_requests_report_generation`）在前，所以"出个研报"仍走重档。
    """
    from tradingagents.agent.routing import TIER_LIGHT, RuleTierClassifier

    class _Resolver:
        def resolve(self, _text, symbol=None):
            return type("R", (), {"symbol": "600519.SH", "symbols": (), "candidates": ()})()

    assert RuleTierClassifier(_Resolver()).classify(text).tier == TIER_LIGHT
