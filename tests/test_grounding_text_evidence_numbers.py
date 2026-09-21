"""文本证据里的数字必须有出处——否则「候选1」这种字段名会误判整条回答。

## 事故

真实模型（deepseek-v4-flash）对筛选结果给出的解读被校验器**两次拒绝**：

    数值 1 在所引证据 ['E1'] 中找不到来源（该证据的标量: []）

原因不是模型写错，而是校验器只看**数值型**证据的标量：候选行是文本证据
（`value` 是「002169.SZ 智光电气｜得分 4247.71｜…」），标量集合为空，于是
模型照抄字段名「候选1」里的那个 `1` 被判成凭空捏造。

同类误判会命中任何字段名带数字的证据：`MA20`、`T+3`、`候选1`……以及**所有**
文本证据（新闻摘录、公告片段）里出现的数字。

## 本文件锁住的分寸

修误判不能放宽尺度：数字仍须能在**被引用证据**里找到。放宽的只是"证据的内容"
这一概念——字段标签与文本正文都算证据自身的内容，而 ×100/÷100 的等价展开
仍然只对百分比开放（否则「5020」会匹配「50.2」，正好放过记错一位数的错误）。
"""

from __future__ import annotations

from tradingagents.grounding.types import Claim, EvidenceItem, EvidenceSet, StructuredAnswer
from tradingagents.grounding.verifier import verify


def _text_evidence() -> EvidenceItem:
    return EvidenceItem(
        id="E1",
        field="候选1",
        value="002169.SZ 智光电气｜得分 4247.71｜涨幅 9.16%｜入选理由：涨幅强势（+9.16%）",
        unit="",
        source_key="realtime_quotes",
        as_of="2026-09-21",
    )


def _answer(claims: tuple[Claim, ...]) -> StructuredAnswer:
    return StructuredAnswer(
        summary="筛选结果解读。", claims=claims, verdict_ref=None, declared_unavailable=()
    )


def _verify(claims: tuple[Claim, ...], items: tuple[EvidenceItem, ...]):
    return verify(_answer(claims), EvidenceSet(items=items))


def test_field_label_digits_are_not_unsourced_numbers():
    """模型照抄字段名「候选1」不该被判成编造。"""
    check = _verify((Claim(text="候选1为智光电气，得分 4247.71。", refs=("E1",)),), (_text_evidence(),))
    assert check.ok, f"{check.reason}: {check.detail}"


def test_numbers_inside_text_evidence_are_sourced():
    """文本证据正文里的数字就是有出处——它是该证据的内容。"""
    check = _verify((Claim(text="该候选涨幅 9.16%。", refs=("E1",)),), (_text_evidence(),))
    assert check.ok, f"{check.reason}: {check.detail}"


def test_numeric_field_label_digits_are_sourced():
    """`MA20`、`T+3` 这类字段标签里的数字同样属于证据自身。"""
    item = EvidenceItem(
        id="E1", field="MA20", value=12.3, unit="元", source_key="stock_data", as_of="2026-09-21"
    )
    check = _verify((Claim(text="MA20 为 12.3 元。", refs=("E1",)),), (item,))
    assert check.ok, f"{check.reason}: {check.detail}"


def test_fabricated_number_is_still_rejected():
    """放宽的是"证据内容"的定义，不是把关尺度。"""
    check = _verify(
        (Claim(text="候选1涨幅 123.45%。", refs=("E1",)),),
        (_text_evidence(),),
    )
    assert not check.ok
    assert check.reason == "unsourced_number"


def test_scale_variants_stay_closed_for_plain_numbers():
    """「5020」不得被「50.2」匹配——那正好放过记错一位数这类错误。"""
    item = EvidenceItem(
        id="E1", field="扫描范围", value="全市场 5020 只", unit="",
        source_key="realtime_quotes", as_of="2026-09-21",
    )
    check = _verify((Claim(text="本轮筛选了 50.2 只。", refs=("E1",)),), (item,))
    assert not check.ok, "纯数字不做量级展开，避免掩盖数量级错误"
    assert check.reason == "unsourced_number"


def test_uncited_evidence_still_cannot_source_a_number():
    """数字只能由**被引用**的证据提供：E2 有 777，但 claim 只引 E1。"""
    e1 = _text_evidence()
    e2 = EvidenceItem(
        id="E2", field="候选2", value="002180.SZ 奔图科技｜得分 777.00", unit="",
        source_key="realtime_quotes", as_of="2026-09-21",
    )
    check = _verify((Claim(text="候选1得分为 777.00。", refs=("E1",)),), (e1, e2))
    assert not check.ok
    assert check.reason == "unsourced_number"
