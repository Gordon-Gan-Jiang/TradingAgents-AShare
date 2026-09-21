"""文本兜底校验与 LLM 回答模型适配器。

## 为什么需要兜底路径

结构化输出（`claims[]` + `refs[]`）是首选，但**thinking 模型与部分厂商对
JSON schema 的遵循度并不稳定**。如果结构化解析失败就等于功能不可用，那
「有依据的回答」会被模型选择绑架。

因此两级处理：
* 解析成功 → 走机械校验器（引用级、可归因、可靠）
* 解析失败 → 降级为**文本数值后置校验** + 标记 fallback

文本校验明确比引用校验弱：它只能确认「数字在本轮证据里找得到」，无法确认
「引对了哪条证据」（形式合规、实质错配这一类错误它抓不到）。这个差距不掩盖，
而是通过 `fallback_rate` 指标暴露出来——它是「需要调整 schema 或换模型」的
信号，**不是可接受的稳态**。
"""

from __future__ import annotations

import pytest

from tradingagents.grounding.types import Claim, EvidenceItem, EvidenceSet, StructuredAnswer


def _ev():
    return EvidenceSet(
        items=(
            EvidenceItem(id="E1", field="volume_contraction.rebound_volume_recovery", value=1.228,
                         unit="ratio", source_key="stock_data", as_of="2026-01-16"),
            EvidenceItem(id="E2", field="capital_composition.main_net_inflow", value=-3.2e7,
                         unit="yuan", source_key="individual_fund_flow", as_of="2026-01-16"),
            EvidenceItem(id="E3", field="position.cum_gain", value=-0.04,
                         unit="ratio", source_key="stock_data", as_of="2026-01-16"),
        )
    )


# ══════════════════════════════════════════════════════════════════════════
# 文本兜底校验
# ══════════════════════════════════════════════════════════════════════════


def test_text_fallback_accepts_numbers_present_in_evidence():
    from tradingagents.grounding.fallback import verify_text

    r = verify_text("主力资金净流出 3200 万，反抽量能恢复至 1.228", _ev())
    assert r.ok is True


def test_text_fallback_rejects_fabricated_number():
    from tradingagents.grounding.fallback import verify_text

    r = verify_text("主力资金净流出 9800 万", _ev())
    assert r.ok is False
    assert "unsourced_number" in (r.reason or "")


def test_text_fallback_rejects_fabricated_date():
    from tradingagents.grounding.fallback import verify_text

    r = verify_text("2026-03-05 主力资金净流出 3200 万", _ev())
    assert r.ok is False
    assert "unsourced_date" in (r.reason or "")


def test_text_fallback_is_explicitly_weaker_and_says_so():
    """引用错配这类错误文本校验抓不到——必须留下可辨认的痕迹。

    若不标记「文本校验」，事后统计会把强弱两条路径的准确率混为一谈，
    于是「为什么加了校验还会错」永远查不清。
    """
    from tradingagents.grounding.fallback import verify_text

    r = verify_text("主力资金净流出 3200 万", _ev())
    assert r.ok is True
    assert r.weak is True


def test_text_fallback_flags_directive_language():
    from tradingagents.grounding.fallback import verify_text

    r = verify_text("建议立即清仓全部仓位", _ev())
    assert r.ok is False
    assert "directive" in (r.reason or "")


# ══════════════════════════════════════════════════════════════════════════
# LLM 回答模型
# ══════════════════════════════════════════════════════════════════════════


def _model(responses, **kw):
    from tradingagents.agent.model import LlmAnswerModel

    queue = list(responses)

    def completer(prompt: str) -> str:
        return queue.pop(0) if queue else ""

    return LlmAnswerModel(completer, **kw)


def _verdict():
    from tradingagents.grounding.types import TurnVerdict

    return TurnVerdict(
        id="V1", probe="washout", symbol="600519.SH", as_of="2026-01-16",
        state="washout", confidence=61, unavailable=("chip_distribution",),
    )


def test_parses_structured_answer_with_claims_and_refs():
    payload = """{
      "summary": "缩量下杀后出现承接",
      "claims": [
        {"text": "反抽量能恢复至 1.228", "refs": ["E1"]},
        {"text": "主力资金净流出 3200 万", "refs": ["E2"]}
      ],
      "verdict_ref": "V1",
      "unavailable": ["chip_distribution"]
    }"""
    m = _model([payload])
    answer = m.propose(prompt="p", evidence=_ev(), verdict=_verdict())

    assert isinstance(answer, StructuredAnswer)
    assert len(answer.claims) == 2
    assert answer.claims[0].refs == ("E1",)
    assert answer.verdict_ref == "V1"
    assert answer.declared_unavailable == ("chip_distribution",)
    assert m.fallback_used == 0


def test_tolerates_code_fence_and_prose_around_json():
    """模型经常把 JSON 包在 ```json 里，或前后加一句解释。

    若因此判为解析失败并降级，fallback_rate 会被这种无害格式噪声推高，
    指标失去意义。
    """
    payload = '好的，以下是结论：\n```json\n{"summary":"承接出现","claims":[{"text":"量能恢复","refs":["E1"]}]}\n```\n希望有帮助。'
    m = _model([payload])
    answer = m.propose(prompt="p", evidence=_ev(), verdict=None)

    assert answer.summary == "承接出现"
    assert answer.claims[0].refs == ("E1",)
    assert m.fallback_used == 0


def test_malformed_json_falls_back_to_text_mode_and_counts_it():
    m = _model(["这不是 JSON，只是一段说明。"])
    answer = m.propose(prompt="p", evidence=_ev(), verdict=None)

    assert m.fallback_used == 1
    assert answer.rendered_note is not None
    # 文本模式下没有逐条引用，因此 claims 为空、正文直接从摘要渲染
    assert answer.summary


def test_claims_without_refs_are_preserved_for_the_verifier_to_reject():
    """模型层**不做**校验。

    若模型层顺手把无引用的 claim 丢掉或补一个引用，校验器就永远看不到
    真实失败率，护栏形同虚设。
    """
    payload = '{"summary":"x","claims":[{"text":"量能恢复","refs":[]}]}'
    m = _model([payload])
    answer = m.propose(prompt="p", evidence=_ev(), verdict=None)

    assert answer.claims[0].refs == ()


def test_prompt_includes_evidence_ids_and_verdict_constraint():
    seen: list[str] = []

    def completer(prompt: str) -> str:
        seen.append(prompt)
        return '{"summary":"x","claims":[]}'

    from tradingagents.agent.model import LlmAnswerModel

    LlmAnswerModel(completer).propose(prompt="用户问题：是不是在洗盘", evidence=_ev(), verdict=_verdict())
    text = seen[0]

    assert "E1" in text and "E2" in text
    assert "washout" in text
    assert "不能改写" in text or "不得改写" in text
    assert "chip_distribution" in text  # 未覆盖维度必须出现在提示里


def test_repair_prompt_carries_the_failure_detail():
    seen: list[str] = []

    def completer(prompt: str) -> str:
        seen.append(prompt)
        return '{"summary":"x","claims":[{"text":"量能恢复","refs":["E1"]}]}'

    from tradingagents.agent.model import LlmAnswerModel
    from tradingagents.grounding.types import CheckResult

    m = LlmAnswerModel(completer)
    m.repair(
        previous=StructuredAnswer(summary="x", claims=(Claim(text="净流出 9800 万", refs=("E2",)),)),
        failure=CheckResult.rejected("unsourced_number", failing_claim="净流出 9800 万", detail="数值 9800万 无来源"),
        evidence=_ev(),
        verdict=None,
    )
    assert "9800" in seen[0]
    assert "unsourced_number" in seen[0] or "无来源" in seen[0]


def test_empty_response_does_not_crash():
    m = _model([""])
    answer = m.propose(prompt="p", evidence=_ev(), verdict=None)
    assert isinstance(answer, StructuredAnswer)
    assert m.fallback_used == 1


def test_model_never_invents_a_verdict_reference():
    """模型没给 verdict_ref 时，不得替它补一个——补出来的引用会让校验器
    以为「判定被正确引用」，而实际上模型并没有引用任何判定。"""
    m = _model(['{"summary":"x","claims":[{"text":"量能恢复","refs":["E1"]}]}'])
    answer = m.propose(prompt="p", evidence=_ev(), verdict=_verdict())
    assert answer.verdict_ref is None
