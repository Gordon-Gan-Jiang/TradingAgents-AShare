"""多标的问句：不能只认第一个代码。

## 事故

「600519 和 300750 哪个好」解析出 `('600519.SH', ())` —— 第二个标的被**静默丢掉**。
这不是 400（那至少用户知道失败了），而是**只分析一半、却以「我听懂了」的姿态作答**。

根因：`_match_explicit_code` 命中第一个代码就 `return`，而 `TurnRequest.symbols`
（批量判定走的那条路）在对话入口**从未被填充过**——`signal_washout_probe_batch`
明明支持多标的，却从 `/v1/chat` 进不去。

## 本文件锁住的边界

* 多个显式代码 → 走批量探针，逐只给结论（而不是问用户"你要哪一只"——
  用户问的是"哪个好"，那是**比较**需求，追问会曲解意图）。
* 单个代码、名称、筛选路径**行为不变**（各自已有测试）。
* 重复代码要合并；名称歧义仍走"请选择"，不因本次改动被吞掉。
"""

from __future__ import annotations

import pytest


def _patch_map(monkeypatch, mapping=None):
    import api.main as main

    stable = dict(mapping or {"贵州茅台": "600519.SH", "宁德时代": "300750.SZ", "平安银行": "000001.SZ"})
    monkeypatch.setattr(main, "_get_stock_name_map_cached_only", lambda: stable)


def _body(text: str, **kw):
    from api.services.chat_service import ChatRequest

    return ChatRequest(messages=[{"role": "user", "content": text}], **kw)


# ══════════════════════════════════════════════════════════════════════════
# 1. 抽取（纯函数）
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "text,expected",
    [
        ("600519 和 300750 哪个好", ("600519.SH", "300750.SZ")),
        ("600519.SH 与 300750.SZ 对比一下", ("600519.SH", "300750.SZ")),
        ("帮我比较 600519、000001.SZ、300750", ("600519.SH", "000001.SZ", "300750.SZ")),
        ("600519 和 600519.SH 一样吗", ("600519.SH",)),  # 去重
        ("600519 是不是在洗盘", ("600519.SH",)),  # 单个照旧
        ("贵州茅台是不是在洗盘", ()),  # 名称不是代码
        ("帮我看看还能不能拿", ()),
        ("", ()),
    ],
)
def test_extract_explicit_codes(text, expected):
    from api.services.chat_service import extract_explicit_codes

    assert extract_explicit_codes(text) == expected


def test_extract_keeps_first_for_the_single_symbol_path(monkeypatch):
    """`_match_explicit_code` 的语义（取第一个）保持不变。"""
    from api.services.chat_service import _match_explicit_code

    assert _match_explicit_code("600519 和 300750 哪个好") == "600519.SH"
    assert _match_explicit_code("贵州茅台是不是在洗盘") is None


def test_codes_are_not_captured_from_unrelated_digits():
    from api.services.chat_service import extract_explicit_codes

    # 6 位数字才算代码；「10 只」「2026」这类不该被当标的
    assert extract_explicit_codes("帮我找10只票") == ()
    assert extract_explicit_codes("2026 年怎么样") == ()


# ══════════════════════════════════════════════════════════════════════════
# 2. 回合构造
# ══════════════════════════════════════════════════════════════════════════


def test_two_codes_become_a_multi_symbol_turn(monkeypatch):
    from api.services import chat_service

    _patch_map(monkeypatch)
    request = chat_service.build_turn_request(_body("600519 和 300750 哪个好"), user_id="u1")

    assert request is not None
    assert request.symbols == ("600519.SH", "300750.SZ"), (
        f"两个标的都要带上，实际 {request.symbols!r}——只带一个是静默丢标的"
    )
    assert request.symbol is None, "多标的回合不该同时塞一个 symbol，否则下游会只看它"
    assert request.screen is False
    assert request.tier in ("medium", "heavy"), "逐只判定需要工具预算"


def test_explicit_symbol_argument_still_wins(monkeypatch):
    """显式传 symbol 时按单标走——那是调用方的明确指定。"""
    from api.services import chat_service

    _patch_map(monkeypatch)
    request = chat_service.build_turn_request(
        _body("600519 和 300750 哪个好", symbol="600519.SH"), user_id="u1"
    )
    assert request is not None
    assert request.symbol == "600519.SH"
    assert request.symbols == ()


def test_single_code_path_unchanged(monkeypatch):
    from api.services import chat_service

    _patch_map(monkeypatch)
    request = chat_service.build_turn_request(_body("600519 是不是在洗盘"), user_id="u1")
    assert request is not None
    assert request.symbol == "600519.SH"
    assert request.symbols == ()


def test_name_ambiguity_still_asks(monkeypatch):
    """名称歧义仍要问用户选哪只——不能因为"多标的"逻辑把它吞成一堆。"""
    from api.services import chat_service

    # 两个名字 → 两个候选，谁也没写代码：这是「你要问哪一只」，不是「比较两只」
    _patch_map(monkeypatch)
    request = chat_service.build_turn_request(_body("茅台和宁德时代哪个好"), user_id="u1")
    assert request is not None
    assert request.symbol is None
    assert request.symbols == (), "写的是名字不是代码，不该走多标的批量（那是比较需求）"
    assert set(request.candidates) == {"600519.SH", "300750.SZ"}, "应交给对话层追问选哪一只"


# ══════════════════════════════════════════════════════════════════════════
# 3. 编排：走批量探针，且提示词要求逐只覆盖
# ══════════════════════════════════════════════════════════════════════════


def test_planner_uses_the_batch_probe_for_multi_symbol():
    from tradingagents.agent.loop import TurnRequest, _plan_tools

    planned = _plan_tools(
        TurnRequest(
            text="600519 和 300750 哪个好",
            user_id="u1",
            symbols=("600519.SH", "300750.SZ"),
            tier="medium",
            as_of="2026-09-21",
        )
    )
    assert [c.tool for c in planned] == ["signal_washout_probe_batch"]
    assert planned[0].arguments["symbols"] == ["600519.SH", "300750.SZ"]


def test_single_symbol_still_uses_the_single_probe():
    from tradingagents.agent.loop import TurnRequest, _plan_tools

    planned = _plan_tools(
        TurnRequest(text="600519 是不是在洗盘", user_id="u1", symbol="600519.SH", tier="medium")
    )
    assert [c.tool for c in planned] == ["signal_washout_probe"]


def test_multi_symbol_prompt_demands_per_symbol_coverage():
    """真实风险：模型只谈其中一只就交卷。提示词必须要求逐只 + 比较。"""
    from tradingagents.agent.loop import TurnRequest, _build_prompt
    from tradingagents.agent.middleware import TurnContext
    from tradingagents.grounding.types import EvidenceSet

    request = TurnRequest(
        text="600519 和 300750 哪个好",
        user_id="u1",
        symbols=("600519.SH", "300750.SZ"),
        tier="medium",
    )
    prompt = _build_prompt(
        request, EvidenceSet(items=()), TurnContext(user_id="u1", tier="medium", text=request.text)
    )
    assert "600519.SH" in prompt and "300750.SZ" in prompt, "两只标的都要在提示词里出现"
    assert "逐只" in prompt or "分别" in prompt, "必须要求逐只给结论"


# ══════════════════════════════════════════════════════════════════════════
# 4. HTTP 端到端：真的调了批量探针，且两只都在参数里
# ══════════════════════════════════════════════════════════════════════════


def test_http_multi_symbol_turn_calls_batch_probe(monkeypatch):
    from fastapi.testclient import TestClient

    from api.main import app
    from api.services import chat_service, signal_service
    from tests.test_acceptance_conversational_signal_agent import _ports_factory
    from tests.test_symbol_resolution import _jwt
    from tradingagents.tools import build_registry

    factory = _ports_factory()
    monkeypatch.setattr(signal_service, "build_ports_factory", lambda user_id=None: factory)
    monkeypatch.setattr(chat_service, "build_ports_factory", lambda user_id=None: factory)
    monkeypatch.setattr(
        chat_service, "build_registry",
        lambda user_id=None: build_registry(ports_factory=factory, news_fetcher=lambda *a: []),
    )

    class _Model:
        def propose(self, *, prompt, evidence, verdict, has_tool_text=False):
            from tradingagents.grounding.types import StructuredAnswer

            return StructuredAnswer(
                summary="两只均已判定。", claims=(), verdict_ref=None, declared_unavailable=()
            )

        def repair(self, **kw):  # pragma: no cover
            return kw["previous"]

    monkeypatch.setattr(chat_service, "build_answer_model", lambda **kw: _Model())
    _patch_map(monkeypatch)

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/v1/chat",
        json={"messages": [{"role": "user", "content": "600519 和 300750 哪个好"}]},
        headers={"Authorization": f"Bearer {_jwt()}"},
    )
    assert resp.status_code == 200, resp.text[:300]
    assert "signal_washout_probe_batch" in resp.text, "多标的应走批量探针"
    assert "300750.SZ" in resp.text, "第二个标的必须进入工具参数，而不是被丢掉"


def test_clarification_lists_the_resolved_candidates(monkeypatch):
    """追问必须列出已解析出的候选。

    `build_turn_request` 此前把 `candidates` 丢了，于是解析层算出的名单在边界
    消失，用户只看到泛泛的「请告诉我代码」——算出来了却没用上。
    """
    from api.services import chat_service
    from tradingagents.agent.events import ListSink
    from tradingagents.agent.loop import run_turn
    from tradingagents.tools import build_registry

    _patch_map(monkeypatch)
    request = chat_service.build_turn_request(_body("茅台和宁德时代哪个好"), user_id="u1")
    assert request is not None and request.candidates

    class _Clock:
        def now(self):
            return 0.0

    sink = ListSink()
    result = run_turn(
        request=request, registry=build_registry(), model=None, sink=sink, clock=_Clock()
    )
    assert "600519.SH" in result.answer and "300750.SZ" in result.answer, (
        f"追问应列出候选，实际：{result.answer!r}"
    )
