"""机械校验器与渲染器：把「不能乱说」从愿望变成构造保证。

## 这些用例守的是什么

现有平台已经有过一次「要求了但没检查」的先例：`analyst_structured.py` 的
提示词写着 evidence anchor 的 value「必须逐字拷贝自上文工具数据（禁止编造
数字）」，但代码只做字符串拼接，**全仓库零校验器**。

因此下面每条断言都对应一个具体的、模型真的会犯的错误：
* 引用一个不存在的证据编号
* 不加引用直接给数字
* 数字对但**引错了证据**（形式上合规、实质错配）
* 编造日期
* 擅自改动探针给出的判定或置信度

最后一条尤其重要：`verdict_ref` 必须指向本轮探针结果，且渲染出的状态与
置信度必须与探针逐字段一致——「方向权在确定性层、表达权在 LLM」这条设计
原则的可执行形式就在这里。
"""

from __future__ import annotations

import pytest

from tradingagents.grounding.types import Claim, EvidenceItem, EvidenceSet, StructuredAnswer


def _ev():
    return EvidenceSet(
        items=(
            EvidenceItem(id="E1", field="volume_contraction.downside_volume_ratio_5d", value=0.632,
                         unit="ratio", source_key="stock_data", as_of="2026-03-03"),
            EvidenceItem(id="E2", field="capital_composition.main_net_inflow", value=-3.2e7,
                         unit="yuan", source_key="individual_fund_flow", as_of="2026-03-03"),
            EvidenceItem(id="E3", field="relative_strength.excess_vs_board_pp", value=2.69,
                         unit="pct", source_key="board_spot", as_of="2026-03-03"),
            EvidenceItem(id="E4", field="intraday_absorption.day_recovery_ratio", value=0.727,
                         unit="ratio", source_key="intraday_bars", as_of="2026-03-03"),
            EvidenceItem(id="E5", field="chip_distribution.profit_ratio", value=0.30,
                         unit="ratio", source_key="chip_distribution", as_of="2026-03-03"),
        )
    )


VERDICTS = frozenset({"V1"})


# ══════════════════════════════════════════════════════════════════════════
# 引用存在性与必备性
# ══════════════════════════════════════════════════════════════════════════


def test_claim_without_citation_is_rejected():
    """没有引用的断言无法被验证 → 必须拒绝，而不是照常输出。"""
    from tradingagents.grounding.verifier import verify

    ans = StructuredAnswer(summary="看起来是洗盘", claims=(Claim(text="主力资金净流出 3200 万", refs=()),))
    r = verify(ans, _ev(), verdict_ids=VERDICTS)

    assert r.ok is False
    assert "citation" in (r.reason or "")


def test_unknown_reference_is_rejected():
    from tradingagents.grounding.verifier import verify

    ans = StructuredAnswer(summary="x", claims=(Claim(text="缩量至 63%", refs=("E9",)),))
    r = verify(ans, _ev(), verdict_ids=VERDICTS)

    assert r.ok is False
    assert "unknown_ref" in (r.reason or "")


# ══════════════════════════════════════════════════════════════════════════
# 数值必须匹配「该 claim 所引证据」
# ══════════════════════════════════════════════════════════════════════════


def test_grounded_claim_passes():
    from tradingagents.grounding.verifier import verify

    ans = StructuredAnswer(
        summary="缩量下杀但存在承接",
        claims=(
            Claim(text="下跌日量能仅为前 5 日均量的 63.2%", refs=("E1",)),
            Claim(text="主力资金净流出 3200 万", refs=("E2",)),
            Claim(text="个股跑赢板块 2.69 个百分点", refs=("E3",)),
        ),
    )
    r = verify(ans, _ev(), verdict_ids=VERDICTS)
    assert r.ok is True, r.detail


def test_fabricated_number_is_rejected():
    """证据里没有这个数字 → 拒绝。这是「不能乱说」最直接的一条。"""
    from tradingagents.grounding.verifier import verify

    ans = StructuredAnswer(summary="x", claims=(Claim(text="主力资金净流出 9800 万", refs=("E2",)),))
    r = verify(ans, _ev(), verdict_ids=VERDICTS)

    assert r.ok is False
    assert "unsourced_number" in (r.reason or "")


def test_number_matching_a_different_evidence_item_is_rejected():
    """数字真实存在，但引用了**另一条**证据 → 仍然拒绝。

    这是最容易漏掉的一条：如果校验只检查「这个数字在本轮任意证据里找得到」，
    模型引 E1 说 E2 的值就能通过。形式上每条断言都有引用，实质上证据链是错的
    ——比没有引用更难被发现。
    """
    from tradingagents.grounding.verifier import verify

    # 0.727 属于 E4（分时承接），不属于 E1（量能）
    ans = StructuredAnswer(summary="x", claims=(Claim(text="下跌日量能仅为 5 日均量的 72.7%", refs=("E1",)),))
    r = verify(ans, _ev(), verdict_ids=VERDICTS)

    assert r.ok is False
    assert "unsourced_number" in (r.reason or "")


def test_unit_equivalent_expression_passes():
    """`0.32 亿` 与证据里的 `-32000000` 是同一个量级表达，不得误判为编造。

    注意符号：金额的**方向**由措辞（「净流出」）承载，校验的是量级。
    因此这里同时接受 0.32 亿 与 -0.32 亿。
    """
    from tradingagents.grounding.verifier import verify

    ok = StructuredAnswer(summary="x", claims=(Claim(text="主力资金净流出 0.32 亿", refs=("E2",)),))
    assert verify(ok, _ev(), verdict_ids=VERDICTS).ok is True

    ok2 = StructuredAnswer(summary="x", claims=(Claim(text="主力资金净流出 32000000 元", refs=("E2",)),))
    assert verify(ok2, _ev(), verdict_ids=VERDICTS).ok is True


def test_percentage_and_ratio_forms_both_accepted():
    from tradingagents.grounding.verifier import verify

    a = StructuredAnswer(summary="x", claims=(Claim(text="获利盘比例 30%", refs=("E5",)),))
    b = StructuredAnswer(summary="x", claims=(Claim(text="获利盘比例 0.30", refs=("E5",)),))
    assert verify(a, _ev(), verdict_ids=VERDICTS).ok is True
    assert verify(b, _ev(), verdict_ids=VERDICTS).ok is True


def test_claim_without_numbers_only_needs_references():
    """定性陈述只需引用存在即可——校验器不判断措辞，只判断可溯源性。"""
    from tradingagents.grounding.verifier import verify

    ans = StructuredAnswer(summary="x", claims=(Claim(text="量能萎缩且盘中出现承接", refs=("E1", "E4")),))
    assert verify(ans, _ev(), verdict_ids=VERDICTS).ok is True


def test_flow_direction_conflict_is_rejected():
    """数字全对但方向相反 —— 这是最危险的一类错误。

    量级校验会放过「净流入 3200 万」对「-32000000」，因为符号由措辞承载。
    因此对资金流做一次保守的方向一致性检查：只在文本明确出现单一方向词时判定，
    含两个方向词（如「由流出转为流入」）则不判断，避免假阳性。
    """
    from tradingagents.grounding.verifier import verify

    bad = StructuredAnswer(summary="x", claims=(Claim(text="主力资金净流入 3200 万", refs=("E2",)),))
    r = verify(bad, _ev(), verdict_ids=VERDICTS)
    assert r.ok is False
    assert "direction" in (r.reason or "")

    # 方向词并列时不判断（避免把「由流出转为流入」误判）
    ambiguous = StructuredAnswer(summary="x", claims=(Claim(text="主力资金由流出转为流入 3200 万", refs=("E2",)),))
    assert verify(ambiguous, _ev(), verdict_ids=VERDICTS).ok is True


def test_structural_numbers_are_not_treated_as_claims():
    """`5 日均量`、`3 个交易日` 里的数字是窗口参数，不是数据断言。

    若不排除，任何提到窗口的正常表述都会被判成编造——校验器会因为假阳性
    过多而被关掉，等于没有校验。
    """
    from tradingagents.grounding.verifier import verify

    ans = StructuredAnswer(
        summary="x",
        claims=(
            Claim(text="下跌日量能仅为前 5 日均量的 63.2%", refs=("E1",)),
            Claim(text="3 个交易日内若未站回 5 日线则假设失效", refs=("E1",)),
        ),
    )
    assert verify(ans, _ev(), verdict_ids=VERDICTS).ok is True


# ══════════════════════════════════════════════════════════════════════════
# 日期断言
# ══════════════════════════════════════════════════════════════════════════


def test_date_matching_cited_evidence_passes():
    from tradingagents.grounding.verifier import verify

    ans = StructuredAnswer(summary="x", claims=(Claim(text="2026-03-03 主力净流出 3200 万", refs=("E2",)),))
    assert verify(ans, _ev(), verdict_ids=VERDICTS).ok is True


def test_fabricated_date_is_rejected():
    """引用证据的 as_of 是 2026-03-03，写成 3 月 5 日就是编造。"""
    from tradingagents.grounding.verifier import verify

    ans = StructuredAnswer(summary="x", claims=(Claim(text="2026-03-05 主力净流出 3200 万", refs=("E2",)),))
    r = verify(ans, _ev(), verdict_ids=VERDICTS)

    assert r.ok is False
    assert "unsourced_date" in (r.reason or "")


def test_chinese_date_form_is_normalized():
    from tradingagents.grounding.verifier import verify

    ans = StructuredAnswer(summary="x", claims=(Claim(text="3月3日主力净流出 3200 万", refs=("E2",)),))
    assert verify(ans, _ev(), verdict_ids=VERDICTS).ok is True


def test_relative_day_word_requires_resolved_date_in_evidence():
    """「昨天」必须以调用方解析出的前一交易日为准，且该日期要真在证据里。"""
    from tradingagents.grounding.verifier import verify

    ans = StructuredAnswer(summary="x", claims=(Claim(text="昨天主力净流出 3200 万", refs=("E2",)),))
    ok = verify(ans, _ev(), verdict_ids=VERDICTS, reference_date="2026-03-04", previous_trading_day="2026-03-03")
    assert ok.ok is True

    bad = verify(ans, _ev(), verdict_ids=VERDICTS, reference_date="2026-03-04", previous_trading_day="2026-03-02")
    assert bad.ok is False


def test_relative_day_word_without_resolution_is_rejected():
    """无法解析相对日期时不得默认放行——那是把不确定性当成正确。"""
    from tradingagents.grounding.verifier import verify

    ans = StructuredAnswer(summary="x", claims=(Claim(text="昨天主力净流出 3200 万", refs=("E2",)),))
    r = verify(ans, _ev(), verdict_ids=VERDICTS)
    assert r.ok is False


# ══════════════════════════════════════════════════════════════════════════
# 判定与置信度不可被改写（D4）
# ══════════════════════════════════════════════════════════════════════════


def test_verdict_ref_must_point_to_a_turn_verdict():
    from tradingagents.grounding.verifier import verify

    ans = StructuredAnswer(
        summary="x",
        claims=(Claim(text="量能萎缩", refs=("E1",)),),
        verdict_ref="V9",
    )
    r = verify(ans, _ev(), verdict_ids=VERDICTS)
    assert r.ok is False
    assert "verdict_ref" in (r.reason or "")


def test_missing_verdict_ref_is_allowed_for_data_only_answers():
    """纯数据问答（「现在多少钱」）没有判定，不必强行引用。"""
    from tradingagents.grounding.verifier import verify

    ans = StructuredAnswer(summary="x", claims=(Claim(text="获利盘 30%", refs=("E5",)),))
    assert verify(ans, _ev(), verdict_ids=VERDICTS).ok is True


# ══════════════════════════════════════════════════════════════════════════
# 未覆盖维度必须显式声明
# ══════════════════════════════════════════════════════════════════════════


def test_unavailable_dimensions_must_be_surfaced():
    from tradingagents.grounding.verifier import verify

    ans = StructuredAnswer(
        summary="x",
        claims=(Claim(text="量能萎缩", refs=("E1",)),),
        declared_unavailable=(),
    )
    r = verify(ans, _ev(), verdict_ids=VERDICTS, coverage_unavailable=("northbound",))
    assert r.ok is False
    assert "coverage" in (r.reason or "")


def test_unavailable_disclosed_passes():
    from tradingagents.grounding.verifier import verify

    ans = StructuredAnswer(
        summary="x",
        claims=(Claim(text="量能萎缩", refs=("E1",)),),
        declared_unavailable=("northbound",),
    )
    assert verify(ans, _ev(), verdict_ids=VERDICTS, coverage_unavailable=("northbound",)).ok is True


# ══════════════════════════════════════════════════════════════════════════
# 渲染器
# ══════════════════════════════════════════════════════════════════════════


def test_renderer_emits_prose_with_traceable_citations():
    from tradingagents.grounding.renderer import render

    ans = StructuredAnswer(
        summary="缩量下杀但盘中有承接",
        claims=(
            Claim(text="下跌日量能仅为前 5 日均量的 63.2%", refs=("E1",)),
            Claim(text="主力资金净流出 3200 万", refs=("E2",)),
        ),
        declared_unavailable=("northbound",),
    )
    text = render(ans, _ev())

    assert "缩量下杀但盘中有承接" in text
    assert "63.2%" in text
    assert "E1" in text and "E2" in text  # 可追溯
    assert "northbound" in text or "未覆盖" in text


def test_renderer_never_alters_numbers():
    """渲染器不得「美化」数字——改写会破坏与证据的一致性。"""
    from tradingagents.grounding.renderer import render

    ans = StructuredAnswer(summary="x", claims=(Claim(text="净流出 32000000 元", refs=("E2",)),))
    text = render(ans, _ev())
    assert "32000000" in text


# ══════════════════════════════════════════════════════════════════════════
# 有界修复与诚实降级
# ══════════════════════════════════════════════════════════════════════════


def test_check_result_carries_actionable_detail_for_repair():
    """失败结果必须带足够信息让上层发起一次修复，而不是简单丢弃。"""
    from tradingagents.grounding.verifier import verify

    ans = StructuredAnswer(summary="x", claims=(Claim(text="主力净流出 9800 万", refs=("E2",)),))
    r = verify(ans, _ev(), verdict_ids=VERDICTS)

    assert r.ok is False
    assert r.failing_claim == "主力净流出 9800 万"
    assert r.detail
    assert r.refs_available == ("E1", "E2", "E3", "E4", "E5")
