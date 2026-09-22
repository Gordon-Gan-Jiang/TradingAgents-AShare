"""声明由**代码**写，不靠模型复述。

## 这个 bug 的形态（线上实测）

模型对 600519 的回答被连续拒绝两次，整轮降级：

    两次生成均未能给出可溯源的回答（coverage_unavailable_not_disclosed）
    已获取的证据字段：profit_ratio、price_vs_avg_cost、intraday_recovery_ratio、…
    为避免给出无依据的结论，本次不作方向性判断。

**未覆盖维度是编排层取数后确定知道的事实**（它就是这么算出来的），却要求模型
逐字复述。那等于把"陈述已知事实"变成一次随机通过的语言考试——模型没照抄，
用户就拿到最差的结果：既没有答案，也不知道缺什么。

## 修法（与免责声明同理）

`declared_unavailable` 由代码补齐后再交给校验器；渲染器本来就会把它写成
「数据未覆盖」一行，所以用户该看到的声明一条不少。同理，"模型估计"口径也由
**代码渲染**进正文，而不是指望模型转述。

## 本文件锁住

1. 模型不声明任何未覆盖项时，回答**不得**因此降级，且声明仍要出现在正文里。
2. 校验器对**模型自己漏掉**的检查仍然有效（不能因为注入而失去把关）。
3. 估计口径必须由代码写进正文（模型不写也照样出现）。
"""

from __future__ import annotations

import pytest


def _answer(**kw):
    from tradingagents.grounding.types import StructuredAnswer

    base = dict(summary="缩量下杀后出现承接。", claims=(), verdict_ref=None, declared_unavailable=())
    base.update(kw)
    return StructuredAnswer(**base)


def test_missing_declarations_are_filled_by_code():
    from tradingagents.agent.loop import _declare_computed_coverage

    filled = _declare_computed_coverage(_answer(), ("position", "capital_composition"))
    assert set(filled.declared_unavailable) == {"position", "capital_composition"}


def test_model_declared_items_are_kept_and_deduplicated():
    from tradingagents.agent.loop import _declare_computed_coverage

    filled = _declare_computed_coverage(
        _answer(declared_unavailable=("position",)), ("position", "chip_distribution")
    )
    assert list(filled.declared_unavailable) == ["position", "chip_distribution"], (
        "模型自己声明的项要保留，且不与代码补齐的重复"
    )


def test_no_coverage_means_no_change():
    from tradingagents.agent.loop import _declare_computed_coverage

    original = _answer(declared_unavailable=("position",))
    assert _declare_computed_coverage(original, ()) is original, "没有未覆盖项时不该改动回答对象"


def test_estimate_disclosure_is_code_rendered():
    """估计口径由代码写：模型不写也照样出现在正文里。"""
    from tradingagents.agent.loop import _estimated_disclosure

    line = _estimated_disclosure({"estimated_dimensions": ["chip_distribution"]})
    assert "模型估计" in line and "chip_distribution" in line

    assert _estimated_disclosure({}) == "", "没有估计维度时不得凭空加口径声明"
    assert _estimated_disclosure({"estimated_dimensions": []}) == ""


def test_verifier_still_catches_a_model_that_hides_coverage():
    """注入的是代码已算出的事实，不是取消把关：校验器本身仍然会拦。"""
    from tradingagents.grounding.types import Claim, EvidenceItem, EvidenceSet
    from tradingagents.grounding.verifier import verify

    evidence = EvidenceSet(
        items=(EvidenceItem(id="E1", field="close", value=9.6, unit="元",
                            source_key="stock_data", as_of="2026-09-19"),)
    )
    check = verify(
        _answer(claims=(Claim(text="收盘 9.6 元", refs=("E1",)),)),
        evidence,
        coverage_unavailable=("position",),
    )
    assert not check.ok, "模型自己没声明时，校验器仍应判定未披露"
    assert check.reason == "coverage_unavailable_not_disclosed"


def test_injected_answer_passes_the_same_check():
    from tradingagents.grounding.types import Claim, EvidenceItem, EvidenceSet
    from tradingagents.grounding.verifier import verify
    from tradingagents.agent.loop import _declare_computed_coverage

    evidence = EvidenceSet(
        items=(EvidenceItem(id="E1", field="close", value=9.6, unit="元",
                            source_key="stock_data", as_of="2026-09-19"),)
    )
    answer = _declare_computed_coverage(
        _answer(claims=(Claim(text="收盘 9.6 元", refs=("E1",)),)), ("position",)
    )
    check = verify(answer, evidence, coverage_unavailable=("position",))
    assert check.ok, f"{check.reason}: {check.detail}"


# ── 端到端：模型不声明也不该降级 ──────────────────────────────────────────


def test_turn_is_not_degraded_when_the_model_omits_declarations(monkeypatch):
    from tradingagents.agent.events import ListSink
    from tradingagents.agent.loop import TurnRequest, run_turn
    from tradingagents.grounding.types import Claim
    from tradingagents.tools import build_registry

    from tests.test_acceptance_conversational_signal_agent import _ports_factory

    factory = _ports_factory()

    class _Model:
        def propose(self, *, prompt, evidence, verdict, has_tool_text=False):
            ref = evidence.items[0].id if evidence.items else None
            return _answer(
                summary="缩量下杀后出现承接。",
                claims=(Claim(text="量能出现萎缩", refs=(ref,)),) if ref else (),
            )

        def repair(self, **kw):  # pragma: no cover
            return kw["previous"]

    class _Clock:
        def __init__(self):
            self.t = 0.0

        def now(self):
            self.t += 0.5
            return self.t

    sink = ListSink()
    result = run_turn(
        request=TurnRequest(text="600519 是不是在洗盘", user_id="u1", symbol="600519.SH",
                            tier="medium", as_of="2026-09-21"),
        registry=build_registry(ports_factory=factory, news_fetcher=lambda *a: []),
        model=_Model(), sink=sink, clock=_Clock(), position_context=None,
    )
    assert result.status != "degraded", (
        f"模型没声明未覆盖项不该导致整轮降级（实际 {result.status}）：{result.answer[:120]}"
    )
    assert "数据未覆盖" in result.answer, "声明仍必须出现在回答里（由渲染器写出）"


# ── 顺带补上一个被暴露的漏洞：代码必须有出处 ──────────────────────────────


def _evidence_with(code: str):
    from tradingagents.grounding.types import EvidenceItem, EvidenceSet

    return EvidenceSet(
        items=(
            EvidenceItem(id="E1", field="候选1", value=f"{code} 智光电气｜得分 4247.71",
                         unit="", source_key="realtime_quotes", as_of="2026-09-21"),
        )
    )


def test_fabricated_instrument_is_rejected():
    """编造的候选代码必须被拦——它此前因为"代码不算数字"而完全没人管。"""
    from tradingagents.grounding.types import Claim
    from tradingagents.grounding.verifier import REASON_UNSOURCED_INSTRUMENT, verify

    check = verify(
        _answer(claims=(Claim(text="999999.SZ 天地板科技得分最高", refs=("E1",)),)),
        _evidence_with("002169.SZ"),
        allowed_symbols=(),
    )
    assert not check.ok
    assert check.reason == REASON_UNSOURCED_INSTRUMENT


def test_turn_symbol_is_always_allowed():
    """本轮标的即使没写进证据也允许出现（单标的探针的证据里本来就没有代码）。"""
    from tradingagents.grounding.types import Claim, EvidenceItem, EvidenceSet
    from tradingagents.grounding.verifier import verify

    evidence = EvidenceSet(
        items=(EvidenceItem(id="E1", field="close", value=9.6, unit="元",
                            source_key="stock_data", as_of="2026-09-19"),)
    )
    check = verify(
        _answer(claims=(Claim(text="600519.SH 收盘 9.6 元", refs=("E1",)),)),
        evidence,
        allowed_symbols=("600519.SH",),
    )
    assert check.ok, f"{check.reason}: {check.detail}"


def test_instrument_present_in_cited_evidence_is_allowed():
    """被引证据里有的代码允许出现（例如板块成分股、新闻里提到的其它票）。"""
    from tradingagents.grounding.types import Claim
    from tradingagents.grounding.verifier import verify

    check = verify(
        _answer(claims=(Claim(text="002169.SZ 得分最高", refs=("E1",)),)),
        _evidence_with("002169.SZ"),
        allowed_symbols=(),
    )
    assert check.ok, f"{check.reason}: {check.detail}"
