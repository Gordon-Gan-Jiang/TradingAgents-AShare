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
from typing import Iterable

from .numbers import (
    date_assertion_ok,
    evidence_text,
    extract_dates,
    extract_numbers,
    flow_direction_conflict,
    matches_any,
    textual_values,
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


# 只有**字段名本身表示资金流方向**的证据才有资格用来判方向。
#
# 中英并存是必须的：证据字段既可能是中文（`evidence.py` 里
# `("主力净流入", facts["main_net_inflow"], "yuan")`），也可能是英文
# （原始资金流帧的 `capital_composition.main_net_inflow`）。只认中文会让
# 英文命名路径的方向护栏**静默失效**——`test_flow_direction_conflict_is_rejected`
# 用的正是英文 fixture，第一次实现时就漏了这个。
_FLOW_FIELD_WORDS = ("流入", "流出", "inflow", "outflow")


def _flow_conflict_over(claim_text: str, items: Iterable[EvidenceItem]):
    """在**方向语义相关**的证据里找方向冲突，返回 `(说明, 命中的证据)`。

    ## 为什么必须把方向判定锚定到字段名（实测假阳性，2026-09-22）

    方向检查此前对**每一个**匹配到的数值都做一遍，于是价格、成交量这类与资金流
    无关的指标也被拿去判方向。确定性复现：

        断言：'主力净流出 3200万，收盘价 1253.8 元'
        证据：E1 主力净流出 = -32000000 yuan（方向与文本**一致**）
              E2 收盘价     =  1253.8     yuan
        结果：ok=False，reason=flow_direction_conflict
              detail=text says outflow but evidence value is positive（证据 E2=1253.8 yuan）

    这条断言**一个错都没有**——数字都有源、方向也一致——却被判冲突，理由还是
    拿「收盘价」去判资金流。也就是说：**任何同时提到资金流向词与任一正值指标的
    断言都会被误杀**，而「主力净流出 X 万，股价 Y 元」正是投研文本的常见句式。
    这与本会话修过的其他几例同属一类：校验器把**无关的证据**当成了断言的依据。

    ## 为什么还要看值是不是数字

    文本证据（口径说明、公告摘录）的 value 是字符串，`float()` 会抛错；
    只对真正带数值的证据判方向。
    """
    for item in items:
        field = str(getattr(item, "field", "") or "")
        if not any(word in field for word in _FLOW_FIELD_WORDS):
            continue
        value = getattr(item, "value", None)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        conflict = flow_direction_conflict(claim_text, float(value))
        if conflict:
            return conflict, item
    return None


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
    # 口径由 `numbers.textual_values` 定义，弱校验路径（`fallback.verify_text`）
    # 用的是**同一个** helper——此前两边各写一份，结果强路径修了、弱路径漏了。
    textual_numbers = textual_values(cited)

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
        # 数值确认有源**之后**才判方向，且锚定**字段名**（不是单位）：
        # 放在这里而不是数值分支内部，是为了让「非数值单位」的资金流证据
        # 也走得到护栏——它由文本口径匹配，进不了上面那个 `is_numeric` 循环。
        # 判定口径见 `_flow_conflict_over` 的假阳性记录。
        conflicted = _flow_conflict_over(claim_text, cited)
        if conflicted is not None:
            conflict, flow_item = conflicted
            return CheckResult.rejected(
                REASON_DIRECTION_CONFLICT,
                failing_claim=claim_text,
                detail=(
                    f"{conflict}（证据 {flow_item.id}={flow_item.value} "
                    f"{flow_item.unit}，字段「{flow_item.field}」）"
                ),
                refs_available=evidence.ids(),
            )

    # ── 日期 ────────────────────────────────────────────────────────────
    cited_as_of = evidence.as_of_of(refs)
    # 与上面数值分支**同一个道理**（见「事故记录」）：日期词出现在所引证据的
    # 字段名或文本正文里时，模型是在**引用**，不是在断言一个日期。
    #
    # 实测（2026-09-22，真实降级，`intraday_bars_read`）：证据字段名本身就叫
    # 「相对当日开盘涨跌幅」「当日最高」「当日最低」，模型照抄字段名写出
    # 「截至 2026-09-22，收盘价为 1253.8 元，相对当日开盘涨跌幅为 0.131773 pct」，
    # 校验器把其中的「当日」当成日期断言判无源 → 整轮降级。
    # 模型因为**正确引用证据**而被罚——与「候选1」那次是同一种假阳性，
    # 数值分支修了，日期分支漏了。
    #
    # 取值来源与数值分支对齐：字段名总是算，文本证据的正文算；
    # 数值证据的 value 是标量，不带日期，其日期在 `as_of` 里（照旧走 as_of 规则）。
    cited_text = evidence_text(cited)
    for assertion in extract_dates(claim_text):
        if assertion.raw and assertion.raw in cited_text:
            continue
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
