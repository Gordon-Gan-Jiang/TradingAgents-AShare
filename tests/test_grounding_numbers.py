"""数值归一与匹配：机械校验器的地基。

## 为什么单位归一是必需的而不是「nice to have」

A 股语境里同一个金额有无数种写法：`3200 万`、`0.32 亿`、`32000000`、`3,200万`。
如果校验器按字面比较，模型写「0.32 亿」而证据里是「32000000」就会被误判为编造，
于是校验器要么被迫放宽到形同虚设，要么产生大量假阳性重写——两种结果都会
让「不能乱说」这条设计目标落空。

## 为什么需要相对容差

模型把 `3204.7 万` 写成 `3200 万` 是合理的语言行为（不是编造），2% 相对容差
覆盖这类四舍五入。但容差不能大到让 `0.54` 与 `0.60` 互相匹配——那会让校验器
失去意义。2% 是经过权衡的值：能容纳四舍五入，不足以容纳「记错一位数」。
"""

from __future__ import annotations

import pytest


# ── 数值抽取 ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected_value", "expected_percent"),
    [
        ("主力净流出 3200 万", 3200e4, False),  # 单位与数字之间有空格，也须识别
        ("净流出 3200万", 3200e4, False),
        ("净流出 0.32亿", 0.32e8, False),
        ("净流出 32000000", 32000000.0, False),
        ("净流出 3,200万", 3200e4, False),
        ("缩量至 54%", 54.0, True),
        ("缩量至 0.54", 0.54, False),
        ("主力净流出 -3200万", -3200e4, False),
        ("板块跌 2.1 个百分点", 2.1, False),
        ("成交 1.2 万亿元", 1.2e12, False),
        ("获利盘 58.3%", 58.3, True),
    ],
)
def test_extract_numbers_handles_chinese_units(text, expected_value, expected_percent):
    from tradingagents.grounding.numbers import extract_numbers

    tokens = extract_numbers(text)
    assert tokens, f"未能从 {text!r} 中抽出数值"
    token = tokens[0]
    assert token.value == pytest.approx(expected_value, rel=1e-9)
    assert token.is_percent is expected_percent


def test_extract_numbers_ignores_dates_and_identifiers():
    """日期与股票代码不能被当成数值断言，否则会产生大量假阳性。"""
    from tradingagents.grounding.numbers import extract_numbers

    tokens = extract_numbers("2026-03-03 600519.SH 在 2026年3月3日 的走势")
    assert tokens == ()


def test_extract_numbers_handles_multiple():
    from tradingagents.grounding.numbers import extract_numbers

    tokens = extract_numbers("净流出 3200万，缩量至 54%，板块跌 2.1%")
    assert len(tokens) == 3
    assert [t.is_percent for t in tokens] == [False, True, True]


# ── 单位归一后的匹配 ──────────────────────────────────────────────────────


def test_equivalent_unit_expressions_match():
    """`3200万` / `0.32亿` / `32000000` 必须被视为同一个值。

    这是校验器可用性的前提：若它们不互相匹配，任何正常的自然语言表述都会
    被判定为编造，校验器就只能被关掉。
    """
    from tradingagents.grounding.numbers import matches_any

    assert matches_any(3200e4, [32000000.0])
    assert matches_any(0.32e8, [32000000.0])
    assert matches_any(32000000.0, [32000000.0])


def test_rounding_within_tolerance_matches():
    from tradingagents.grounding.numbers import matches_any

    assert matches_any(3200e4, [3204.7e4], rel_tol=0.02)
    assert matches_any(0.54, [0.538], rel_tol=0.02)


def test_off_by_a_digit_does_not_match():
    """容差必须小到能拦住「记错一位数」——否则校验形同虚设。"""
    from tradingagents.grounding.numbers import matches_any

    assert not matches_any(3200e4, [2300e4], rel_tol=0.02)
    assert not matches_any(0.54, [0.60], rel_tol=0.02)


def test_percentage_ratio_equivalence():
    """`54%` 与比率 `0.54` 是同一个事实的两种写法，必须互相匹配。"""
    from tradingagents.grounding.numbers import matches_any

    assert matches_any(54.0, [0.54], is_percent=True)
    assert matches_any(0.54, [54.0], is_percent=False)


def test_percentage_does_not_match_unrelated_ratio():
    from tradingagents.grounding.numbers import matches_any

    assert not matches_any(54.0, [0.71], is_percent=True)


def test_zero_and_small_values_use_absolute_epsilon():
    """接近 0 的值不能用相对容差判断（相对容差在 0 附近会退化成永远成立）。"""
    from tradingagents.grounding.numbers import matches_any

    assert matches_any(0.0, [0.0])
    assert not matches_any(0.0, [0.01])
    assert not matches_any(1e-9, [0.5])


# ── 证据标量抽取 ──────────────────────────────────────────────────────────


def test_scalars_of_extracts_numeric_evidence_values():
    from tradingagents.grounding.numbers import scalar_values

    from tradingagents.grounding.types import EvidenceItem, EvidenceSet

    ev = EvidenceSet(
        items=(
            EvidenceItem(id="E1", field="main_net_inflow", value=-3.2e7, unit="yuan",
                         source_key="individual_fund_flow", as_of="2026-03-03"),
            EvidenceItem(id="E2", field="turnover_class", value="broad_contraction", unit="text",
                         source_key="board_spot", as_of="2026-03-03"),
            EvidenceItem(id="E3", field="profit_ratio", value=0.583, unit="ratio",
                         source_key="chip_distribution", as_of="2026-03-03"),
        )
    )
    values = scalar_values(ev.items)
    assert -3.2e7 in values
    assert 0.583 in values
    # 文本类证据不产生标量（它不能被「数值断言」引用错配）
    assert "broad_contraction" not in values
