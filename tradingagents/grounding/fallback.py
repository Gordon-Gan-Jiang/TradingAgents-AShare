"""文本兜底校验：结构化解析失败时的弱校验路径。

## 明确的能力边界

它只能回答「这段话里的数字**是否在本轮证据里存在**」，**不能**回答
「这个数字引用的是不是对的那条证据」。因此像「引 E1 说 E2 的值」这种
形式合规、实质错配的错误，文本校验抓不到——机械校验器能抓到。

这不是实现偷懒，而是文本形态本身丢失了归因信息。把它显式标记为 `weak`
（`CheckResult.weak=True`），是为了让事后统计不会把强弱两条路径的准确率
混为一谈：否则「为什么加了校验还会错」会永远查不清。
"""

from __future__ import annotations

from tradingagents.grounding.numbers import (
    date_assertion_ok,
    extract_dates,
    extract_numbers,
    matches_any,
    scalar_values,
)
from tradingagents.grounding.types import CheckResult, EvidenceSet

REASON_UNSOURCED_NUMBER = "unsourced_number"
REASON_UNSOURCED_DATE = "unsourced_date"
REASON_DIRECTIVE = "directive_language"

# 指令式买卖措辞：研究结论允许是条件化观察，不允许是祈使句
_DIRECTIVE_PHRASES = (
    "立即清仓",
    "马上买入",
    "立刻卖出",
    "全仓买入",
    "无条件卖出",
    "抓紧买入",
    "建议买入",
    "建议卖出",
)


def verify_text(
    text: str,
    evidence: EvidenceSet,
    *,
    reference_date: str | None = None,
    previous_trading_day: str | None = None,
) -> CheckResult:
    """弱校验：数值与日期的存在性 + 指令式措辞拦截。"""
    available = scalar_values(evidence.items)

    for phrase in _DIRECTIVE_PHRASES:
        if phrase in text:
            return CheckResult.rejected(
                REASON_DIRECTIVE,
                failing_claim=phrase,
                detail=f"回答包含指令式买卖措辞 {phrase!r}，研究结论不得给出祈使式操作指令",
                refs_available=evidence.ids(),
            )

    for token in extract_numbers(text):
        if not matches_any(token.value, available, is_percent=token.is_percent):
            return CheckResult.rejected(
                REASON_UNSOURCED_NUMBER,
                failing_claim=token.raw,
                detail=f"文本中的数值 {token.raw} 无法在本轮证据中找到来源",
                refs_available=evidence.ids(),
            )

    cited_as_of = frozenset(item.as_of for item in evidence.items)
    for assertion in extract_dates(text):
        if not date_assertion_ok(
            assertion,
            cited_as_of,
            reference_date=reference_date,
            previous_trading_day=previous_trading_day,
        ):
            return CheckResult.rejected(
                REASON_UNSOURCED_DATE,
                failing_claim=assertion.raw,
                detail=f"日期断言 {assertion.raw!r} 不在本轮证据的 as_of {sorted(cited_as_of)} 内",
                refs_available=evidence.ids(),
            )

    result = CheckResult.accepted(refs_available=evidence.ids())
    # 标记为弱校验：调用方与统计必须能区分这两条路径
    return CheckResult(
        ok=result.ok,
        reason=result.reason,
        failing_claim=result.failing_claim,
        detail=result.detail,
        refs_available=result.refs_available,
        weak=True,
    )
