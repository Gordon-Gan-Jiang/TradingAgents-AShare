"""机械校验器：确定性代码，零 LLM。

## 为什么不能用模型来校验模型

用另一个 LLM 判「有没有编造」会引入第二个不可复现的判定源：同样的回答
两次检查可能得到不同结论，而且校验逻辑本身无法写单测。本模块的全部判断
都是纯函数，可以在毫秒内穷举边界。

## 检查顺序与失败语义

1. 引用存在性 —— 没有引用的断言无法被验证
2. 数值一致性 —— 数字必须匹配**该 claim 所引证据**的标量
3. 日期一致性 —— 日期断言必须落在所引证据的 as_of 内
4. 判定引用 —— `verdict_ref` 必须指向本轮探针结果
5. 覆盖声明 —— 工具声明未覆盖的维度必须在回答里显式出现

第 2 条的关键细节是「所引证据」而不是「本轮全部证据」。只检查后者的话，
模型引 E1 说 E2 的数字也能通过——形式上每条断言都有引用，实质上证据链错配，
这比没有引用更难被发现。
"""

from __future__ import annotations

import re

from .numbers import (
    date_assertion_ok,
    extract_dates,
    extract_numbers,
    flow_direction_conflict,
    matches_any,
)
from .types import CheckResult, Coverage, EvidenceItem, EvidenceSet, StructuredAnswer

REASON_NO_CITATION = "claim_without_citation"
REASON_UNKNOWN_REF = "unknown_ref"
REASON_UNSOURCED_NUMBER = "unsourced_number"
REASON_UNSOURCED_DATE = "unsourced_date"
REASON_DIRECTION_CONFLICT = "flow_direction_conflict"
REASON_VERDICT_REF = "verdict_ref_not_from_turn"
REASON_COVERAGE = "coverage_unavailable_not_disclosed"
# 断言里的**股票代码**必须能溯源：要么是本轮标的，要么出现在被引证据里。
# 实测漏洞：代码在数字提取时被 `_mask` 排除（那是为了不把 600519 当成数值），
# 于是「999999.SZ 得分最高」这种**凭空编造的候选**反而一路通过校验——
# 被排除在数值检查之外，就没有任何检查管它。而"候选不得编造"正是筛选回答的底线。
REASON_UNSOURCED_INSTRUMENT = "unsourced_instrument"

# 6 位代码（可带交易所后缀）。刻意不匹配更长数字串里的片段。
_INSTRUMENT_RE = re.compile(r"(?<!\d)(\d{6})(?:\.(?:SH|SZ|SS))?(?!\d)", re.IGNORECASE)


def _instruments(text: str) -> set[str]:
    """抽取断言中提到的股票代码（归一为 6 位裸代码）。"""
    return {match.group(1) for match in _INSTRUMENT_RE.finditer(text or "")}


def _check_claim(
    claim_text: str,
    refs: tuple[str, ...],
    evidence: EvidenceSet,
    *,
    reference_date: str | None,
    previous_trading_day: str | None,
) -> CheckResult | None:
    """返回 None 表示通过。"""
    if not refs:
        return CheckResult.rejected(
            REASON_NO_CITATION,
            failing_claim=claim_text,
            detail="断言没有引用任何证据，无法验证其来源",
            refs_available=evidence.ids(),
        )

    cited: tuple[EvidenceItem, ...] = evidence.cited(refs)
    if len(cited) != len(refs):
        missing = [r for r in refs if evidence.get(r) is None]
        return CheckResult.rejected(
            REASON_UNKNOWN_REF,
            failing_claim=claim_text,
            detail=f"引用了不存在的证据编号: {missing}",
            refs_available=evidence.ids(),
        )

    # ── 数值：必须匹配「该 claim 所引证据」 ─────────────────────────────
    #
    # 证据的**内容**不只有数值标量，还包括：
    #   * 字段标签里的数字（「候选1」「MA20」「T+3」）——它是证据的名字，不是断言
    #   * 文本证据的正文（候选行、新闻摘录）——数值出现在正文里即为有出处
    #
    # 事故记录：真实模型照抄字段名「候选1」写「候选1为智光电气，得分 4247.71」，
    # 因为候选行是文本证据（标量集合为空），那个 `1` 被判成凭空捏造，整条回答
    # 被拒两次、降级成"说明缺什么"。误判会命中任何带数字的字段名与所有文本证据。
    label_numbers: list[float] = []
    text_numbers: list[float] = []
    for item in cited:
        label_numbers.extend(
            tok.value for tok in extract_numbers(str(getattr(item, "field", "") or ""))
        )
        if not item.is_numeric:
            text_numbers.extend(tok.value for tok in extract_numbers(str(item.value)))
    textual_numbers = tuple(label_numbers + text_numbers)

    for token in extract_numbers(claim_text):
        matched = False
        # 文本/标签口径：纯数字**不做**量级展开（否则「5020」会被「50.2」匹配，
        # 正好放过记错一位数这类错误）；百分比仍允许与比率互认。
        if textual_numbers and matches_any(
            token.value,
            textual_numbers,
            is_percent=token.is_percent,
            allow_scale_variants=token.is_percent,
        ):
            matched = True
        for item in cited:
            if matched:
                break
            if not item.is_numeric:
                continue
            # 带量纲的证据不做 ×100/÷100 展开：否则「5400 万」会匹配「54 万」
            allow_scale = item.unit in {"ratio", "pct"}
            if not matches_any(
                token.value,
                [float(item.value)],
                is_percent=token.is_percent,
                allow_scale_variants=allow_scale,
            ):
                continue
            conflict = flow_direction_conflict(claim_text, float(item.value))
            if conflict:
                return CheckResult.rejected(
                    REASON_DIRECTION_CONFLICT,
                    failing_claim=claim_text,
                    detail=f"{conflict}（证据 {item.id}={item.value} {item.unit}）",
                    refs_available=evidence.ids(),
                )
            matched = True
            break
        if not matched:
            candidates = [float(i.value) for i in cited if i.is_numeric]
            return CheckResult.rejected(
                REASON_UNSOURCED_NUMBER,
                failing_claim=claim_text,
                detail=(
                    f"数值 {token.raw} 在所引证据 {list(refs)} 中找不到来源"
                    f"（数值证据的标量: {candidates}；"
                    f"文本/字段标签中的数字: {list(textual_numbers)}）"
                ),
                refs_available=evidence.ids(),
            )

    # ── 日期 ────────────────────────────────────────────────────────────
    cited_as_of = evidence.as_of_of(refs)
    for assertion in extract_dates(claim_text):
        if not date_assertion_ok(
            assertion,
            cited_as_of,
            reference_date=reference_date,
            previous_trading_day=previous_trading_day,
        ):
            return CheckResult.rejected(
                REASON_UNSOURCED_DATE,
                failing_claim=claim_text,
                detail=(
                    f"日期断言 {assertion.raw!r} 与所引证据的 as_of {sorted(cited_as_of)} 不一致"
                ),
                refs_available=evidence.ids(),
            )

    return None


def verify(
    answer: StructuredAnswer,
    evidence: EvidenceSet,
    *,
    verdict_ids: frozenset[str] = frozenset(),
    coverage_unavailable: tuple[str, ...] = (),
    allowed_symbols: Sequence[str] = (),
    reference_date: str | None = None,
    previous_trading_day: str | None = None,
) -> CheckResult:
    """校验一个结构化回答。通过返回 ok=True，否则携带可修复的失败信息。"""
    refs_available = evidence.ids()

    for claim in answer.claims:
        failure = _check_claim(
            claim.text,
            claim.refs,
            evidence,
            reference_date=reference_date,
            previous_trading_day=previous_trading_day,
        )
        if failure is not None:
            return failure

    if answer.verdict_ref is not None and answer.verdict_ref not in verdict_ids:
        return CheckResult.rejected(
            REASON_VERDICT_REF,
            failing_claim=answer.verdict_ref,
            detail=f"verdict_ref={answer.verdict_ref!r} 不指向本轮探针结果 {sorted(verdict_ids)}",
            refs_available=refs_available,
        )

    # ── 股票代码必须可溯源 ───────────────────────────────────────────────
    allowed_codes = {str(item).split(".")[0] for item in (allowed_symbols or ())}
    for claim in answer.claims:
        claim_text = str(getattr(claim, "text", "") or "")
        cited_text = " ".join(
            f"{item.field} {item.value}" for item in evidence.cited(claim.refs)
        )
        for code in _instruments(claim_text):
            if code in allowed_codes:
                continue
            if code in _instruments(cited_text):
                continue
            return CheckResult.rejected(
                REASON_UNSOURCED_INSTRUMENT,
                failing_claim=claim_text,
                detail=(
                    f"代码 {code} 既不是本轮标的，也未出现在被引证据中"
                    f"（本轮标的: {sorted(allowed_codes) or '无'}）"
                ),
                refs_available=evidence.ids(),
            )

    missing_coverage = [d for d in coverage_unavailable if d not in answer.declared_unavailable]
    if missing_coverage:
        return CheckResult.rejected(
            REASON_COVERAGE,
            detail=f"以下维度未被覆盖但回答未声明: {missing_coverage}",
            refs_available=refs_available,
        )

    return CheckResult.accepted(refs_available=refs_available)


def coverage_unavailable_of(coverages: tuple[Coverage, ...] | list[Coverage]) -> tuple[str, ...]:
    """汇总若干工具结果的未覆盖维度。"""
    out: list[str] = []
    for cov in coverages:
        for item in cov.unavailable:
            if item not in out:
                out.append(item)
    return tuple(out)
