"""Regression tests for disclosure period parsing from financial report markdown."""

from tradingagents.dataflows.freshness.parser import (
    parse_disclosure_period,
    yyyymmdd_to_disclosure_period,
)


def test_yyyymmdd_to_disclosure_period_quarter_ends():
    assert yyyymmdd_to_disclosure_period("20260331") == "2026-Q1"
    assert yyyymmdd_to_disclosure_period("20251231") == "2025-Q4"
    assert yyyymmdd_to_disclosure_period("20250930") == "2025-Q3"


def test_parse_disclosure_period_picks_latest_yyyymmdd_column_not_false_q_regex():
    """20251231 must not be misread as 2025-Q1 when 20260331 is present."""
    md = """## Balance Sheet (600519.SH)

| 报告日 | 20260331 | 20251231 | 20250331 |
| --- | --- | --- | --- |
| 货币资金 | 100 | 90 | 80 |
"""
    assert parse_disclosure_period(md) == "2026-Q1"


def test_parse_disclosure_period_fundamentals_abstract_columns():
    md = """### Financial Abstract (latest available columns)

| 指标 | 20260331 | 20251231 | 20250331 |
| 归母净利润 | 1 | 2 | 3 |
"""
    assert parse_disclosure_period(md) == "2026-Q1"


def test_parse_disclosure_period_still_supports_explicit_q_label():
    assert parse_disclosure_period("公司 2025-Q4 年报摘要") == "2025-Q4"
