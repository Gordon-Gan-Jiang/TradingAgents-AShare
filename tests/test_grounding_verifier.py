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

from tradingagents.grounding.fallback import verify_text
from tradingagents.grounding.types import Claim, EvidenceItem, EvidenceSet, StructuredAnswer
from tradingagents.grounding.verifier import REASON_UNSOURCED_DATE, verify


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


# ══════════════════════════════════════════════════════════════════════════
# 引用证据的**文本**时不得被判成无源（假阳性会让正确回答降级）
# ══════════════════════════════════════════════════════════════════════════
#
# 实测来源（2026-09-22，真实降级，`intraday_bars_read`）：
#     校验器明细：日期断言 '当日' 与所引证据的 as_of ['2026-09-22'] 不一致
#     失败断言：'截至 2026-09-22，收盘价为 1253.8 元，相对当日开盘涨跌幅为 0.131773 pct。'
# 而该轮的证据**字段名本身就是**「相对当日开盘涨跌幅」「当日最高」「当日最低」。
# 模型照抄自己所引证据的字段名，却被判「无源的日期断言」——因为正确引用而被罚。
# 数值分支早就修过同一种假阳性（见 verifier.py 的「候选1」事故记录），
# 日期分支漏了。


def _ev_with_ri_field():
    """复刻 `intraday_bars_read` 真实证据：字段名里带「当日」。"""
    return EvidenceSet(
        items=(
            EvidenceItem(id="E1", field="收盘价", value=1253.8, unit="yuan",
                         source_key="intraday_bars", as_of="2026-09-22"),
            EvidenceItem(id="E2", field="相对当日开盘涨跌幅", value=0.131773, unit="pct",
                         source_key="intraday_bars", as_of="2026-09-22"),
        )
    )


def test_quoting_a_field_name_containing_a_date_word_is_accepted():
    """日期词出现在所引证据的字段名里 → 是引用，不是日期断言。"""
    from tradingagents.grounding.verifier import verify

    ans = StructuredAnswer(
        summary="分时承接",
        claims=(
            Claim(text="截至 2026-09-22，收盘价为 1253.8 元，相对当日开盘涨跌幅为 0.131773 pct。",
                  refs=("E1", "E2")),
        ),
    )
    r = verify(ans, _ev_with_ri_field())

    assert r.ok is True, f"照抄证据字段名不应降级：{r.reason} / {r.detail}"


def test_a_date_word_not_in_the_cited_evidence_is_still_rejected():
    """对照组：证据文本里没有该日期词时，相对日期词照旧必须与 as_of 一致。

    放宽必须以「证据文本里真的有这个词」为界，否则等于放弃日期校验。
    """
    from tradingagents.grounding.verifier import verify

    ans = StructuredAnswer(
        summary="分时承接",
        claims=(Claim(text="今天收盘价为 1253.8 元", refs=("E1",)),),
    )
    r = verify(ans, _ev_with_ri_field(), reference_date="2026-09-23")

    assert r.ok is False
    assert r.reason == "unsourced_date"


def test_field_name_numbers_are_sourced_in_the_weak_text_path():
    """弱校验路径（`verify_text`）此前只比对数值标量，缺了「候选1」那次修复。

    文本证据（候选行）没有标量，字段名里的数字只存在于字段名中——只比对标量
    必然把**正确引用**判成编造。
    """
    from tradingagents.grounding.fallback import verify_text

    ev = EvidenceSet(
        items=(
            EvidenceItem(id="E1", field="候选1", value="智光电气 得分 4247.71", unit="",
                         source_key="market_screen", as_of="2026-09-22"),
        )
    )
    r = verify_text("候选1为智光电气，得分 4247.71。", ev)

    assert r.ok is True, f"引用文本证据的字段名不应降级：{r.reason} / {r.detail}"


def test_weak_text_path_accepts_a_numbered_list():
    """编号列表的序号不是数值断言（真实降级：`watchlist_read` 因序号 1 被判无源）。"""
    from tradingagents.grounding.fallback import verify_text

    ev = EvidenceSet(
        items=(
            EvidenceItem(id="E1", field="自选股数量", value=20.0, unit="count",
                         source_key="watchlist", as_of="2026-09-22"),
        )
    )
    r = verify_text("1. 我的自选股共 20 只。", ev)

    assert r.ok is True, f"列表序号不应被判无源：{r.reason} / {r.detail}"


# ══════════════════════════════════════════════════════════════════════════
# 方向检查必须锚定**资金流字段**，不能拿无关证据判方向
# ══════════════════════════════════════════════════════════════════════════


def _flow_ev(flow_field="主力净流出", flow_value=-3.2e7, flow_unit="yuan"):
    return EvidenceSet(
        items=(
            EvidenceItem(id="E1", field=flow_field, value=flow_value, unit=flow_unit,
                         source_key="individual_fund_flow", as_of="2026-03-03"),
            EvidenceItem(id="E2", field="收盘价", value=1253.8, unit="yuan",
                         source_key="stock_data", as_of="2026-03-03"),
            EvidenceItem(id="E3", field="沪指点位", value=3962.0, unit="",
                         source_key="index", as_of="2026-03-03"),
        )
    )


def test_a_correct_flow_claim_that_also_mentions_a_price_is_accepted():
    """**第四例假阳性**（实测，2026-09-22）。

    方向检查此前对**每一个**匹配到的数值都做一遍，于是「收盘价 1253.8」也被拿去
    判资金流方向：文本里有「流出」而 1253.8 是正数 → 判 `flow_direction_conflict`，
    把一条**数字全对、方向也一致**的断言拒掉：

        '主力净流出 3200万，收盘价 1253.8 元'
        证据 E1 主力净流出 = -32000000（方向与文本一致）
             E2 收盘价     = 1253.8
        旧行为：ok=False, flow_direction_conflict（证据 E2=1253.8 yuan）

    「主力净流出 X 万，股价 Y 元」是投研文本的常见句式，所以这条假阳性会稳定命中。
    """
    from tradingagents.grounding.verifier import verify

    ans = StructuredAnswer(
        summary="x",
        claims=(Claim(text="主力净流出 3200万，收盘价 1253.8 元", refs=("E1", "E2")),),
    )
    r = verify(ans, _flow_ev())
    assert r.ok is True, f"正确断言被误杀：{r.reason} / {r.detail}"


def test_a_correct_flow_claim_mentioning_a_dimensionless_index_is_accepted():
    """指数点位（`unit=""`，无量纲）同样不得被拿去判资金流方向。"""
    from tradingagents.grounding.verifier import verify

    ans = StructuredAnswer(
        summary="x",
        claims=(Claim(text="主力净流出 3200万，沪指 3962 点", refs=("E1", "E3")),),
    )
    assert verify(ans, _flow_ev()).ok is True


def test_direction_conflict_is_still_caught_for_english_field_names():
    """英文命名的资金流字段（原始帧字段名）也必须能判方向。

    第一次实现锚定时只认「流入/流出」两个汉字，于是英文 fixture
    （`capital_composition.main_net_inflow`）的方向护栏**静默失效**——
    既有测试 `test_flow_direction_conflict_is_rejected` 当场失败。
    中英并存是真实情况（见 `evidence.py` 的证据构造）。
    """
    from tradingagents.grounding.verifier import verify

    ev = _flow_ev(flow_field="capital_composition.main_net_inflow")
    ans = StructuredAnswer(
        summary="x", claims=(Claim(text="主力资金净流入 3200 万", refs=("E1",)),)
    )
    r = verify(ans, ev)
    assert r.ok is False
    assert r.reason == "flow_direction_conflict"


def test_direction_conflict_is_caught_even_when_the_flow_field_has_no_unit():
    """空单位的资金流证据也要判方向。

    这类证据由**文本口径**匹配（`is_numeric` 为 False），进不了数值分支的循环，
    所以方向检查必须放在"数值确认有源之后"，否则它的护栏是死的——而
    `unit=""` 恰恰是 `evidence.py` 里最常见的单位取值。
    """
    from tradingagents.grounding.verifier import verify

    ev = _flow_ev(flow_unit="")
    ans = StructuredAnswer(
        summary="x", claims=(Claim(text="主力资金净流入 3200 万", refs=("E1",)),)
    )
    r = verify(ans, ev)
    assert r.ok is False
    assert r.reason == "flow_direction_conflict"


# ══════════════════════════════════════════════════════════════════════════
# 相对日词必须有**基准日**才可能通过——生产路径曾经一个都不给
# ══════════════════════════════════════════════════════════════════════════
#
# `date_assertion_ok` 对相对日词的判据是：
#     relative == "today"    → reference_date in cited_as_of
#     relative == "previous" → previous_trading_day in cited_as_of
#
# 生产`_TurnDeps` 此前从不传这两个参数（只有测试传），默认 None ⇒ 两条判断恒为
# False ⇒ **任何**含「今天/今日/当日/本日/昨日/上一交易日」的回答都必然
# `unsourced_date`，日期写得对不对完全不影响结果。生产库实测原话：
#     日期断言 '今天' 不在本轮证据的 as_of [] 内
#     日期断言 '当日' 与所引证据的 as_of ['2026-09-22'] 不一致   ← 给了 as_of 照样失败
#
# 杀伤面大的原因：`当日` 是**证据字段名**的一部分（当日最高/当日最低/当日涨幅/
# 收盘位于当日区间位置），模型照字段名写就会踩中。


def _ev_as_of(as_of: str = "2026-09-22"):
    return EvidenceSet(
        items=(
            EvidenceItem(id="E1", field="收盘价", value=1253.8, unit="yuan",
                         source_key="sina", as_of=as_of),
        )
    )


def test_relative_date_passes_once_a_reference_date_is_supplied():
    """给上基准日，「截至 2026-09-22…当日涨幅…」必须能通过。

    `当日` 在这里不是日期断言，而是字段描述的一部分；即便按相对日词处理，
    只要基准日等于所引证据的 as_of，它就该通过。
    """
    # 这条断言**完全可溯源**（1253.8 正是 E1 的取值），唯一"可疑"之处是 `当日`。
    # 给了基准日就必须通过——否则一条正确的回答会因为用词习惯被拒。
    text = "当日收盘价 1253.8 元 [E1]。"
    assert verify_text(text, _ev_as_of(), reference_date="2026-09-22").ok is True
    assert verify_text(
        "截至 2026-09-22，当日市场结构性活跃 [E1]。", _ev_as_of(), reference_date="2026-09-22"
    ).ok is True


def test_relative_date_fails_when_no_reference_date_is_supplied():
    """不给基准日时同一句话必然失败——锁住"基准日缺失=结构性必败"这个事实。

    这条不是"期望的失败"，而是把**生产曾经的状态**固定成可观测的行为：
    只要有人再把它改成不给值，这个测试就会红。
    """
    text = "当日收盘价 1253.8 元 [E1]。"
    result = verify_text(text, _ev_as_of(), reference_date=None)
    assert result.ok is False
    assert result.reason == REASON_UNSOURCED_DATE


def test_production_turn_deps_supply_the_reference_dates():
    """装配层必须把基准日传下去——这是"提示词/校验器一致"的最后一环。

    上面两条证明的是语义；这一条防的是**装配漏传**（正是本轮的真实缺陷：
    语义没问题，只是没人给它值）。
    """
    import inspect

    from api.services import chat_service

    source = inspect.getsource(chat_service.run_turn_streaming)
    assert "reference_date=" in source, "生产 _TurnDeps 未传 reference_date，相对日词会恒失败"
    assert "previous_trading_day=" in source, "生产 _TurnDeps 未传 previous_trading_day"


# ══════════════════════════════════════════════════════════════════════════
# 小数部分不是股票代码
# ══════════════════════════════════════════════════════════════════════════
#
# `_INSTRUMENT_RE` 原本是 `(?<!\d)(\d{6})(?!\d)`。实测（2026-09-22）模型写
# `3.203957e+09` 时，`203957` 被读成了股票代码 —— 因为小数点不是数字，
# 前视断言放它过去了。于是**模型写一个 6 位小数的数就被判 `unsourced_instrument`**，
# 而证据里那个数是 3203957000.0，`_instruments` 从它里抽不出任何 6 位串，
# 所以必然对不上。
#
# 这与「把引用机制读成数据」「把字段名里的日期词读成日期断言」是同一类假阳性：
# **校验器把无关的东西读成了断言**。而这类假阳性最难发现，因为它只在模型
# "恰好写了 6 位小数"时出现。


def test_decimal_fraction_is_not_read_as_a_stock_code():
    from tradingagents.grounding.verifier import _instruments

    for text in ("3.203957e+09", "0.123456", "涨跌幅 0.005472", "净额 3.203957e+09 元"):
        assert _instruments(text) == set(), f"{text!r} 的小数部分被读成了股票代码"


def test_real_codes_are_still_recognized_in_every_common_position():
    """反向：真实的 6 位代码在各种写法下都必须仍被识别。

    放宽 lookbehind 的风险是"把真代码也放过"，所以每种常见写法都要钉住。
    """
    from tradingagents.grounding.verifier import _instruments

    for text, expected in (
        ("600519", {"600519"}),
        ("600519.SH", {"600519"}),
        ("000001.SZ", {"000001"}),
        ("代码：600519", {"600519"}),
        ("[600519]", {"600519"}),
        ("（600519）", {"600519"}),
        ("600519 的市盈率", {"600519"}),
    ):
        assert _instruments(text) == expected, f"{text!r} 的真实代码没被识别"


def test_claim_writing_a_six_decimal_number_passes_when_evidence_holds_it():
    """端到端：证据里存的是原始值，模型写 6 位小数不该被判无源代码。"""
    from tradingagents.grounding.types import Claim, EvidenceItem, EvidenceSet, StructuredAnswer
    from tradingagents.grounding.verifier import verify

    evidence = EvidenceSet(
        items=(
            EvidenceItem(
                id="E1",
                field="房地产 今日主力净流入-净额（2026-09-22）",
                value=3203957000.0,
                unit="",
                source_key="fundamental_data",
                as_of="2026-09-22",
            ),
        )
    )
    answer = StructuredAnswer(
        summary="",
        claims=(Claim(text="房地产主力净流入 3.203957e+09 元 [E1]。", refs=("E1",)),),
    )
    result = verify(answer, evidence, reference_date="2026-09-22")
    assert result.ok, f"6 位小数被误判：{result.reason}"
