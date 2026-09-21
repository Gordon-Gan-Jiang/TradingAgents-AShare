"""数值与日期的抽取、单位归一与容差匹配。

## 这个模块解决的唯一问题

「模型说的数字，是不是证据里真的有的那个数字」——看起来简单，但在 A 股
语境里同一个事实有无数种写法：`3200 万`、`0.32 亿`、`32000000`、`3,200万`、
`54%` 与 `0.54`。按字面比较会把正常表述判成编造（假阳性），放宽到模糊比较
又会放过「记错一位数」（假阴性）。

这里的解法是**先归一、再按量级容差比较**，并且把容差控制在 2%：
足以容纳四舍五入，不足以容纳数量级错误。

## 明确的三条边界（避免过度自信）

1. **方向词不做机械校验**（除资金流的流入/流出对比外）：`净流出 3200 万`
   在证据为 `-32000000` 时通过，因为符号由措辞承载。资金流方向做了保守的
   一致性检查（只在文本明确出现单一方向词时才判冲突）。
2. **结构性数字被排除**：`5 日均量`、`20 日线`、`3 个交易日` 里的数字是参数，
   不是数据断言；把它们纳入校验会产生大量假阳性。判据是数字后紧跟
   日/天/周/月/年/个交易日等周期词。
3. **日期与代码不被当作数值**：`2026-03-03`、`600519.SH` 先被遮蔽。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence

# 相对容差：容纳四舍五入，但不容纳数量级错误（0.54 vs 0.60 判为不匹配）
DEFAULT_REL_TOL = 0.02

_MULTIPLIERS = {
    "千": 1e3,
    "万": 1e4,
    "亿": 1e8,
    "万亿": 1e12,
}

# 需要先遮蔽的模式：日期、股票代码。否则它们会被抽成数值断言。
_MASK_PATTERNS = (
    re.compile(r"\d{4}-\d{1,2}-\d{1,2}"),
    re.compile(r"\d{4}年\d{1,2}月\d{1,2}日"),
    re.compile(r"\d{1,2}月\d{1,2}日"),
    re.compile(r"\d{4}年\d{1,2}月"),
    re.compile(r"\d{6}\.(?:SH|SZ|BJ)", re.IGNORECASE),
    # 裸 6 位代码（A 股代码以 0/3/6/8 开头）。这是一个启发式：
    # 金额通常带 万/亿 单位或位数更多，所以冲突很少；代价可接受。
    re.compile(r"(?<![\d.])[0368]\d{5}(?![\d.])"),
)

# 周期词：紧跟数字时说明它是窗口参数而非数据
_PERIOD_LOOKAHEAD = r"(?![\d]|\s*(?:日|天|周|月|年|个交易日|分钟|小时))"

_NUMBER_RE = re.compile(
    r"(?<![\d.,])"
    r"(-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d+(?:\.\d+)?)"
    r"(\s*万亿|\s*亿|\s*万|\s*千)?"
    r"(\s*%)?"
    + _PERIOD_LOOKAHEAD
)

_DATE_ISO_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_DATE_CN_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")
_DATE_MD_RE = re.compile(r"(?<!\d)(\d{1,2})月(\d{1,2})日")

_RELATIVE_TODAY = ("今天", "今日", "当日", "本日")
_RELATIVE_PREV = ("昨天", "昨日", "前一交易日", "上一交易日", "前一日")

_FLOW_OUT = ("流出", "净流出")
_FLOW_IN = ("流入", "净流入")


@dataclass(frozen=True)
class NumberToken:
    """一个数值断言。`value` 已按中文量级归一为绝对量。"""

    raw: str
    value: float
    is_percent: bool


@dataclass(frozen=True)
class DateAssertion:
    """一个日期断言。`normalized` 为 None 表示需要按相对词解析。"""

    raw: str
    normalized: str | None
    relative: str | None = None


def _mask(text: str) -> str:
    masked = text
    for pattern in _MASK_PATTERNS:
        masked = pattern.sub(lambda m: "\u0000" * len(m.group(0)), masked)
    return masked


def extract_numbers(text: str) -> tuple[NumberToken, ...]:
    """抽出数值断言（已排除日期、代码与窗口参数）。"""
    masked = _mask(text)
    tokens: list[NumberToken] = []
    for match in _NUMBER_RE.finditer(masked):
        raw = match.group(0).strip()
        literal, multiplier, percent = match.group(1), match.group(2), match.group(3)
        try:
            value = float(literal.replace(",", ""))
        except ValueError:  # pragma: no cover — 正则已保证可解析
            continue
        if multiplier:
            value *= _MULTIPLIERS[multiplier.strip()]
        tokens.append(NumberToken(raw=raw, value=value, is_percent=bool(percent)))
    return tuple(tokens)


def extract_dates(text: str) -> tuple[DateAssertion, ...]:
    """抽出日期断言（绝对日期 + 相对日词）。

    注意：必须在**原文**上搜索，不能先遮蔽。日期遮蔽只用于 `extract_numbers`
    ——那里的目的是避免把 `2026-03-03` 拆成 2026 / 03 / 03 三个数值断言。
    早期版本在这里复用了遮蔽后的文本，结果日期断言永远为空，编造日期也就
    永远不会被发现（一个静默失效的校验分支）。
    """
    found: list[DateAssertion] = []

    for match in _DATE_ISO_RE.finditer(text):
        y, m, d = match.groups()
        found.append(DateAssertion(raw=match.group(0), normalized=f"{int(y):04d}-{int(m):02d}-{int(d):02d}"))
    for match in _DATE_CN_RE.finditer(text):
        y, m, d = match.groups()
        found.append(DateAssertion(raw=match.group(0), normalized=f"{int(y):04d}-{int(m):02d}-{int(d):02d}"))
    for match in _DATE_MD_RE.finditer(text):
        m, d = match.groups()
        found.append(DateAssertion(raw=match.group(0), normalized=f"??-{int(m):02d}-{int(d):02d}"))

    for word in _RELATIVE_TODAY:
        if word in text:
            found.append(DateAssertion(raw=word, normalized=None, relative="today"))
    for word in _RELATIVE_PREV:
        if word in text:
            found.append(DateAssertion(raw=word, normalized=None, relative="previous"))

    return tuple(found)


def _close(a: float, b: float, rel_tol: float) -> bool:
    if a == b:
        return True
    scale = max(abs(a), abs(b))
    if scale < 1e-9:
        return abs(a - b) <= 1e-9
    return abs(a - b) / scale <= rel_tol


def candidate_values(
    value: float,
    *,
    is_percent: bool,
    allow_scale_variants: bool = True,
) -> tuple[float, ...]:
    """把一个数值断言展开成等价的候选量级。

    * `54%`  ↔ `0.54`（百分比与比率）
    * `0.54` ↔ `54`（反向）

    `allow_scale_variants=False` 用于带量纲（元/股/天）的证据：对金额做
    ×100/÷100 的等价展开会让「5400 万」与「54 万」互相匹配，那正好放过了
    「记错一位数」这类错误。
    """
    variants = [value]
    if is_percent:
        variants.append(value / 100.0)
    elif allow_scale_variants:
        variants.extend((value / 100.0, value * 100.0))
    return tuple(variants)


def matches_any(
    value: float,
    candidates: Sequence[float],
    *,
    is_percent: bool = False,
    rel_tol: float = DEFAULT_REL_TOL,
    allow_scale_variants: bool = True,
) -> bool:
    """数值断言的量级是否落在候选集合内（按量级比较，符号由措辞承载）。"""
    variants = candidate_values(value, is_percent=is_percent, allow_scale_variants=allow_scale_variants)
    for candidate in candidates:
        for variant in variants:
            if _close(abs(variant), abs(candidate), rel_tol):
                return True
    return False


def scalar_values(items: Iterable[object]) -> tuple[float, ...]:
    """取证据项中的数值标量（文本类证据不参与数值校验）。"""
    out: list[float] = []
    for item in items:
        if getattr(item, "is_numeric", False):
            out.append(float(item.value))
    return tuple(out)


def flow_direction_conflict(text: str, evidence_value: float) -> str | None:
    """资金流方向的一致性检查（保守：只在文本明确出现单一方向词时判定）。

    「净流出 3200 万」与证据 `-32000000` 一致；若写成「净流入 3200 万」，
    量级校验会放过它——而这是最危险的一类错误：数字全对、方向相反。
    含两个方向词（如「由流出转为流入」）时不做判断，避免假阳性。
    """
    has_out = any(w in text for w in _FLOW_OUT)
    has_in = any(w in text for w in _FLOW_IN)
    if has_out and has_in:
        return None
    if has_out and evidence_value > 0:
        return "text says outflow but evidence value is positive"
    if has_in and evidence_value < 0:
        return "text says inflow but evidence value is negative"
    return None


def date_assertion_ok(
    assertion: DateAssertion,
    cited_as_of: frozenset[str],
    *,
    reference_date: str | None = None,
    previous_trading_day: str | None = None,
) -> bool:
    """日期断言必须落在**被引用证据**的 as_of 集合内。"""
    if assertion.relative == "today":
        return reference_date is not None and reference_date in cited_as_of
    if assertion.relative == "previous":
        return previous_trading_day is not None and previous_trading_day in cited_as_of
    if assertion.normalized is None:
        return False
    if assertion.normalized.startswith("??-"):
        suffix = assertion.normalized[3:]
        return any(item_as_of[5:] == suffix for item_as_of in cited_as_of)
    return assertion.normalized in cited_as_of
