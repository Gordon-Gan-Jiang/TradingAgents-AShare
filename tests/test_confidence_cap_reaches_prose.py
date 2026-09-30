"""置信度上限必须同时作用于**正文**，不能只作用于落库的那一列（审计 §4.17，§6 第 11 行）。

## 症状（真实数据坐实）

所有 `freshness_status='error'` 的报告，落库列 `reports.confidence` 都是封顶后的 `40`，
但正文 VERDICT 里的数字没有被改写。真实样例（`reports` 表）：

| id | 列 confidence | 正文 final_trade_decision |
|---|---|---|
| `3f843e49…` | **40** | `confidence: 70` |
| `a1064140…` | **40** | `confidence: 40`（偶然一致） |
| `25ee2668…` | **40** | `confidence: 59` |

于是同一个"置信度"在同一份报告里有**两个不同的数**：读列的人看到 40，
读正文（或从正文重算）的人看到 59/70 —— 上限形同虚设。

## 口径

报告级置信度的来源是**决策级文档**：`_resolve_confidence(final_trade_decision,
trader_investment_plan)`（`api/services/report_service.py:389`）。因此封顶后只有这两份
文档里的数字需要与列保持一致。

**各分析师报告（`market_report` / `news_report` / …）的 confidence 是分析师对自己那一节的
自评，是另一个量，不得一起压**——把它们压到报告级上限会摧毁信息。
"""

from __future__ import annotations

import re

from tradingagents.dataflows.freshness.integrate import attach_freshness_to_result


def _error_pool() -> dict:
    """空池会被展开成全源 error（上限 40）。"""
    return {}


def _doc(block_direction: str, confidence: int) -> str:
    return (
        "## 决策\n\n"
        f"<!-- VERDICT: {{\"direction\": \"{block_direction}\", \"confidence\": {confidence}}} -->\n\n"
        "综上，保持观察。\n"
    )


def _verdict_number(text: str) -> int | None:
    m = re.search(r'"confidence"\s*:\s*(\d+)', text)
    if m:
        return int(m.group(1))
    m = re.search(r"(?:置信度|confidence)[:：]\s*(\d+)%", text, re.IGNORECASE)
    return int(m.group(1)) if m else None


# ══════════════════════════════════════════════════════════════════════════
# 1. 决策级文档的正文必须与封顶后的列一致
# ══════════════════════════════════════════════════════════════════════════


def test_verdict_prose_is_capped_alongside_the_column():
    """**核心**：列被压到 40，正文就不能还是 88。"""
    result = {
        "confidence": 88,
        "final_trade_decision": _doc("看多", 88),
        "trader_investment_plan": _doc("看多", 88),
    }

    out = attach_freshness_to_result(result, _error_pool(), "2026-03-19", "600519.SH")

    assert out["confidence"] == 40, "前置：列已被封顶"
    for key in ("final_trade_decision", "trader_investment_plan"):
        got = _verdict_number(out[key])
        assert got == 40, (
            f"{key} 正文 VERDICT 的置信度必须与落库列一致（40），实际 {got}；"
            f"否则同一份报告有两个不同的置信度"
        )


def test_plain_confidence_statement_is_capped_too():
    """没有 VERDICT 块、只写 `置信度: 88%` 的形态也要压。"""
    result = {
        "confidence": 88,
        "final_trade_decision": "结论：看多。置信度: 88%\n",
    }

    out = attach_freshness_to_result(result, _error_pool(), "2026-03-19", "600519.SH")

    assert out["confidence"] == 40
    assert "88%" not in out["final_trade_decision"], (
        f"正文仍保留未封顶的 88%：{out['final_trade_decision']!r}"
    )
    assert _verdict_number(out["final_trade_decision"]) == 40


# ══════════════════════════════════════════════════════════════════════════
# 2. 反过度修正：不得误伤
# ══════════════════════════════════════════════════════════════════════════


def test_analyst_self_assessed_confidence_is_not_touched():
    """分析师对自己那一节的自评是**另一个量**，不得被报告级上限压掉。"""
    result = {
        "confidence": 88,
        "final_trade_decision": _doc("看多", 88),
        "market_report": _doc("偏多", 58),
        "news_report": _doc("中性", 45),
    }

    out = attach_freshness_to_result(result, _error_pool(), "2026-03-19", "600519.SH")

    assert _verdict_number(out["market_report"]) == 58, "分析师自评置信度不得被改写"
    assert _verdict_number(out["news_report"]) == 45, "分析师自评置信度不得被改写"


def test_no_clamp_needed_leaves_prose_byte_identical():
    """上限不生效时（列本来就低于上限）正文必须**一字不改**。"""
    result = {
        "confidence": 30,
        "final_trade_decision": _doc("看多", 30),
        "trader_investment_plan": _doc("看多", 30),
    }
    before = (result["final_trade_decision"], result["trader_investment_plan"])

    out = attach_freshness_to_result(result, _error_pool(), "2026-03-19", "600519.SH")

    assert out["confidence"] == 30
    assert (out["final_trade_decision"], out["trader_investment_plan"]) == before


def test_missing_documents_do_not_crash():
    """没有决策文档时不得抛异常（历史上 result 形状并不统一）。"""
    out = attach_freshness_to_result(
        {"confidence": 88}, _error_pool(), "2026-03-19", "600519.SH"
    )
    assert out["confidence"] == 40


def test_confidence_absent_stays_absent_and_prose_untouched():
    """confidence 为 None（未声明）时不得凭空写入数字。"""
    text = _doc("看多", 88)
    result = {"final_trade_decision": text}

    out = attach_freshness_to_result(result, _error_pool(), "2026-03-19", "600519.SH")

    assert out.get("confidence") is None
    assert out["final_trade_decision"] == text, "未声明置信度时正文不得被改写"
