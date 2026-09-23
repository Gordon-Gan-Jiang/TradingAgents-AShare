"""规划层的灰度开关、独立模型配置、以及接入回合构造。

## 为什么这些必须有测试

规划层是**行为变更**（它改变了"什么问句能进来"）。三条不变量必须锁住：

1. **默认关闭**：开关不开时行为与今天逐字一致——不改变默认行为是灰度的前提，
   否则"上线了但没测过的路径"会悄悄接管。
2. **关闭时一次 LLM 都不调**：开关的意义就是省钱与可控，读了开关却不生效是最
   典型的失败（本仓库已经踩过"声明而不执行"多次）。
3. **规划失败降级**：规划器抛错不能中断回合，要退回规则路径（含追问）。

另外，规划器与回答模型的配置必须**分开**：规划每轮都跑、要便宜快；回答要质量。
"""

from __future__ import annotations

import pytest

from tradingagents.agent.planner_config import (
    PLANNER_ENABLED_ENV,
    PLANNER_MODEL_ENV,
    PLANNER_PROFILE_ENV,
    describe_planner,
    llm_planner_enabled,
    planner_target,
)


def _clear(monkeypatch):
    for name in (PLANNER_ENABLED_ENV, PLANNER_MODEL_ENV, PLANNER_PROFILE_ENV):
        monkeypatch.delenv(name, raising=False)


def test_planner_is_off_by_default(monkeypatch):
    _clear(monkeypatch)
    assert llm_planner_enabled() is False, "默认必须是关闭：灰度期间不改默认行为"


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", "enabled"])
def test_truthy_values_enable_it(monkeypatch, value):
    _clear(monkeypatch)
    monkeypatch.setenv(PLANNER_ENABLED_ENV, value)
    assert llm_planner_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "", "  ", "off", "no"])
def test_other_values_keep_it_off(monkeypatch, value):
    _clear(monkeypatch)
    monkeypatch.setenv(PLANNER_ENABLED_ENV, value)
    assert llm_planner_enabled() is False


def test_planner_target_defaults_to_following_the_answer_model(monkeypatch):
    _clear(monkeypatch)
    assert planner_target() == (None, None)
    assert describe_planner()["follows_answer_model"] is True


def test_planner_target_can_be_configured_separately(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv(PLANNER_MODEL_ENV, "cheap-model")
    monkeypatch.setenv(PLANNER_PROFILE_ENV, "profile-1")
    assert planner_target() == ("profile-1", "cheap-model")
    assert describe_planner()["model"] == "cheap-model"


# ── 接入回合构造 ──────────────────────────────────────────────────────────


def _body(text: str):
    from api.services.chat_service import ChatRequest

    return ChatRequest(messages=[{"role": "user", "content": text}])


def _patch_map(monkeypatch):
    import api.main as main

    monkeypatch.setattr(
        main, "_get_stock_name_map_cached_only",
        lambda: {"贵州茅台": "600519.SH", "宁德时代": "300750.SZ"},
    )


# 规则路径**服务不了**的一句自然语言：下面几条测试需要一个"规则失败"的问句作载体，
# 它必须真的没有不需要标的的落点——否则测的就不是"规划层把路打开"，而是"规则已经
# 能服务了"。
#
# 这个载体换过一次（2026-09-23）：原来是「最近哪个板块最强」，闸门补上板块类措辞后
# 它被规则路径接下了（计划 `get_board_spot`、`build_turn_request` 不再返回 None），
# 三条"退回规则路径"的断言因此同时变红。现在换成「现在市场情绪如何」——它要的是
# P2 的市场主线/情绪工具（`market_mainline_view`，今天不在工具面上），规则路径仍然
# 服务不了。**等它也能被服务时这几条会再次变红，那时请再换一个载体**，别把断言放宽
# 成"或非 None"：那等于把"规划失败必须降级、不能中断回合"这条性质测没了。
_RULES_CANNOT_SERVE = "现在市场情绪如何"


def test_without_planner_behaviour_is_unchanged(monkeypatch):
    """不注入规划器 = 今天的行为：解析不出标的、也没有市况级落点的问句返回 None。"""
    from api.services import chat_service

    _patch_map(monkeypatch)
    assert chat_service.build_turn_request(_body(_RULES_CANNOT_SERVE), user_id="u1") is None


def test_planner_is_only_called_when_rules_fail(monkeypatch):
    """规则已解析出标的时**不得**调用规划器（高频问句不付规划成本）。"""
    from api.services import chat_service
    from tradingagents.agent.routing import Plan

    _patch_map(monkeypatch)
    calls: list[str] = []

    def _planner(text):
        calls.append(text)
        return Plan(intent="market_view", tier="light", market_level=True, confidence=0.8)

    request = chat_service.build_turn_request(
        _body("贵州茅台是不是在洗盘"), user_id="u1", planner=_planner
    )
    assert calls == [], f"规则已能解析标的却调用了规划器：{calls}"
    assert request is not None and request.plan_source == "rules"


def test_planner_opens_the_natural_language_path(monkeypatch):
    """规则失败时才用规划器——这正是"任意自然语言"的入口。"""
    from api.services import chat_service
    from tradingagents.agent.routing import Plan

    _patch_map(monkeypatch)

    def _planner(text):
        return Plan(intent="market_view", tier="light", market_level=True, confidence=0.8)

    request = chat_service.build_turn_request(
        _body(_RULES_CANNOT_SERVE), user_id="u1", planner=_planner
    )
    assert request is not None, "规划层给出的落点应当被采纳"
    assert request.plan_source == "llm"
    assert request.market_level is True
    assert request.tier == "light"


def test_plan_supplies_symbols_for_comparison(monkeypatch):
    from api.services import chat_service
    from tradingagents.agent.routing import Plan

    _patch_map(monkeypatch)
    request = chat_service.build_turn_request(
        _body("这两个哪个好"), user_id="u1",
        planner=lambda text: Plan(
            intent="multi_symbol_compare", tier="medium",
            symbols=("600519.SH", "300750.SZ"), confidence=0.7,
        ),
    )
    assert request is not None
    assert request.symbols == ("600519.SH", "300750.SZ")


def test_planner_failure_degrades_instead_of_breaking_the_turn(monkeypatch):
    """规划器抛错不能中断回合：退回规则路径（此处即 400 追问）。"""
    from api.services import chat_service

    _patch_map(monkeypatch)

    def _boom(text):
        raise TimeoutError("planner slow")

    assert chat_service.build_turn_request(
        _body(_RULES_CANNOT_SERVE), user_id="u1", planner=_boom
    ) is None


def test_planner_returning_none_falls_back(monkeypatch):
    from api.services import chat_service

    _patch_map(monkeypatch)
    assert chat_service.build_turn_request(
        _body(_RULES_CANNOT_SERVE), user_id="u1", planner=lambda text: None
    ) is None


# ── 计划里的步骤必须被编排层采用 ──────────────────────────────────────────


def _registry():
    from tradingagents.tools import build_registry

    return build_registry(
        ports_factory=lambda *a: object(), news_fetcher=lambda *a: [], market_screener=object()
    )


def test_planned_steps_win_over_mechanical_rules():
    """规划层给了步骤就以它为准——否则规划只是个"换档位"的装饰。

    实测教训：规划层上线后覆盖率 60%→100%，但工具精确率 86%→51%、
    召回 75.8%→63.6%。原因就是 `_plan_tools` 仍按档位机械决定工具，
    规划层选好的步骤被丢在一边。
    """
    from tradingagents.agent.loop import TurnRequest, _plan_tools

    request = TurnRequest(
        text="用日线看看 600519 的量能", user_id="u1", symbol="600519.SH", tier="medium",
        as_of="2026-09-21", plan_source="llm", intent="factual_lookup",
        planned_steps=(
            {"tool": "get_realtime_quotes", "arguments": {"symbols": ["600519.SH"]},
             "reason": "planned"},
        ),
    )
    planned = _plan_tools(request, registry=_registry())

    assert [call.tool for call in planned] == ["get_realtime_quotes"], (
        "规划层指定了工具，却仍按档位机械选了探针"
    )
    assert planned[0].arguments == {"symbols": ["600519.SH"]}, "参数要原样采用"


def test_unknown_planned_tool_is_skipped_with_fallback():
    """计划里的工具未在册时跳过该步；若全被跳过则回落到机械规则。"""
    from tradingagents.agent.loop import TurnRequest, _plan_tools

    request = TurnRequest(
        text="600519 是不是在洗盘", user_id="u1", symbol="600519.SH", tier="medium",
        as_of="2026-09-21", plan_source="llm", intent="single_symbol_judgement",
        planned_steps=({"tool": "ghost_tool", "arguments": {}},),
    )
    planned = _plan_tools(request, registry=_registry())

    assert [call.tool for call in planned] == ["signal_washout_probe"], (
        "未在册的步骤被丢后应回落到机械规则，而不是什么都不做"
    )


def test_plan_intent_is_recorded_for_measurement(monkeypatch):
    """意图必须进回合与 trace：度量规划准确率全靠它。"""
    from api.services import chat_service
    from tradingagents.agent.routing import Plan

    _patch_map(monkeypatch)
    request = chat_service.build_turn_request(
        _body("最近哪个板块最强"), user_id="u1",
        planner=lambda text: Plan(intent="market_view", tier="light", market_level=True,
                                  confidence=0.8),
    )
    assert request is not None
    assert request.intent == "market_view"
    assert request.plan_source == "llm"


def test_planned_steps_suppress_the_clarification_prompt():
    """规划层已决定要做什么时不得追问——它的步骤可能本来就不需要标的。

    实测踩到：「我持仓里哪只最危险」被规划器正确解析出计划（含
    portfolio_holdings_read 这类无需标的的工具），却在执行前被追问分支拦下，
    回了"请告诉我具体的股票代码"。追问分支只看 symbol，把规划层的决定
    当成了"用户没说清楚"。
    """
    from tradingagents.agent.events import ListSink
    from tradingagents.agent.loop import TurnRequest, run_turn
    from tradingagents.tools import build_registry

    class _Clock:
        def now(self):
            return 0.0

    request = TurnRequest(
        text="我持仓里哪只最危险", user_id="u1", tier="medium", as_of="2026-09-21",
        plan_source="llm", intent="portfolio_review",
        planned_steps=({"tool": "portfolio_holdings_read", "arguments": {}, "reason": "planned"},),
    )
    sink = ListSink()
    result = run_turn(
        request=request,
        registry=build_registry(
            ports_factory=lambda *a: object(), news_fetcher=lambda *a: [],
            holdings_port=type("P", (), {"list_holdings": lambda self, *, limit: []})(),
        ),
        model=None, sink=sink, clock=_Clock(),
    )
    assert "请告诉我具体的股票代码" not in result.answer, (
        "规划层已给出步骤，不该被追问分支当成「用户没说清楚」"
    )
