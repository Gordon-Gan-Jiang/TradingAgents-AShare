"""Tests for A-share methodology loader (FinSkills-style injection)."""

from pathlib import Path

import pytest

from tradingagents.methodology.loader import (
    get_ashare_methodology_block,
    get_fundamentals_ashare_methodology_block,
    get_news_events_ashare_methodology_block,
    get_sector_macro_ashare_methodology_block,
    methodology_ashare_fundamentals_enabled,
)


def test_builtin_block_non_empty_when_enabled():
    block = get_fundamentals_ashare_methodology_block(
        {"methodology_ashare_fundamentals": True, "methodology_extra_path": ""}
    )
    assert "A 股" in block or "CAS" in block
    assert len(block) > 200


def test_news_and_macro_kinds_non_empty():
    n = get_news_events_ashare_methodology_block({"methodology_ashare_news_events": True})
    assert "事件" in n or "舆情" in n
    m = get_sector_macro_ashare_methodology_block({"methodology_ashare_sector_macro": True})
    assert "行业" in m or "宏观" in m


def test_get_ashare_methodology_block_dispatch():
    assert get_ashare_methodology_block("fundamentals", {"methodology_ashare_fundamentals": True})
    assert get_ashare_methodology_block("news_events", {"methodology_ashare_news_events": True})
    assert get_ashare_methodology_block("sector_macro", {"methodology_ashare_sector_macro": True})
    assert get_ashare_methodology_block("invalid", {}) == ""


def test_disabled_returns_empty():
    assert (
        get_fundamentals_ashare_methodology_block({"methodology_ashare_fundamentals": False})
        == ""
    )


def test_methodology_ashare_fundamentals_enabled_helper():
    assert methodology_ashare_fundamentals_enabled({"methodology_ashare_fundamentals": True}) is True
    assert methodology_ashare_fundamentals_enabled({"methodology_ashare_fundamentals": False}) is False


def test_finskills_root_resolves_default_directories(tmp_path: Path):
    """模拟本地 FinSkills 目录结构，验证 FINSKILLS_ROOT 回退。"""
    root = tmp_path / "finskills"
    ref = root / "China-market" / "financial-statement-analyzer" / "references"
    ref.mkdir(parents=True)
    (ref / "analysis-methodology.md").write_text("# FS\n财务检查", encoding="utf-8")

    block = get_ashare_methodology_block(
        "fundamentals",
        {
            "methodology_ashare_fundamentals": True,
            "methodology_extra_path": "",
            "finskills_root": str(root),
        },
    )
    assert "财务检查" in block
    assert "外部追加" in block


def test_extra_file_appended(tmp_path: Path):
    extra = tmp_path / "extra.md"
    extra.write_text("## 自定义\n测试片段", encoding="utf-8")
    block = get_fundamentals_ashare_methodology_block(
        {
            "methodology_ashare_fundamentals": True,
            "methodology_extra_path": str(extra),
        }
    )
    assert "测试片段" in block
    assert "外部追加" in block


def test_stock_analysis_team_root_fallback(tmp_path: Path):
    root = tmp_path / "stock-analysis-team"
    ref = root / "references"
    ref.mkdir(parents=True)
    (ref / "analysis-framework.md").write_text("# SAT\n多团队分析法", encoding="utf-8")
    (ref / "risk-scoring-criteria.md").write_text("风险评分细则", encoding="utf-8")

    block = get_ashare_methodology_block(
        "fundamentals",
        {
            "methodology_ashare_fundamentals": True,
            "methodology_extra_path": "",
            "finskills_root": str(tmp_path / "not-exist"),  # 禁用环境变量回退，走 stock-analysis-team 兜底
            "stock_analysis_team_root": str(root),
        },
    )
    assert "多团队分析法" in block or "风险评分细则" in block
