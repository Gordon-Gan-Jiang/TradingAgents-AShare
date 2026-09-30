"""新鲜度不得「读不出即 fresh」（见 docs/report-judgment-accuracy-audit.md §4.15–§4.18）。

## 共同形状

这四处代码在**信息缺失**时一律给出**最乐观**的答案：

| 位置 | 缺失的东西 | 旧答案 |
|---|---|---|
| `validator.py`（category 分发末尾） | 不认识的 `category` | `fresh` |
| `validator.py` `_eval_event_driven` | 事件时间戳解析不出来 | `fresh` + 一句 lag_note |
| `aggregator.py` `_aggregate_overall` | 池子里全是 `unknown` | `fresh` |
| `report_service.py` | `freshness_summary` 缺 `overall_status` | `fresh` |

而 `CONFIDENCE_CAPS = {fresh:100, warning:70, stale:50, error:40}` **根本没有 `unknown` 键**，
于是 `CONFIDENCE_CAPS.get(overall, 100)` 对 `unknown` 返回 **100**——
"没验证过"反而拿到了"完全可信"的上限。

更隐蔽的是 `refreshness` 池里的 `unknown` 在 `_STATUS_RANK` 里是 **0**（最低优先级），
所以它们既不会拉低聚合结果，也不会触发任何确认度上限。**"不知道"因此比"新鲜"还安全。**

判据（本文件钉住）：
1. 无时间戳 / 未知 category / 缺 `overall_status` → `unknown`（或 `error`），**不得** `fresh`；
2. 落到 `unknown` 时**必须**施加置信度上限（`cap < 100`）；
3. `report_age_days` 须**如实**给出，不得恒为 0。
"""

from __future__ import annotations

import pytest


# ══════════════════════════════════════════════════════════════════════════
# 0. 上限表必须覆盖 unknown —— 否则"没验证"拿到 100
# ══════════════════════════════════════════════════════════════════════════


def test_confidence_caps_covers_unknown():
    from tradingagents.dataflows.freshness.types import CONFIDENCE_CAPS

    assert "unknown" in CONFIDENCE_CAPS, (
        "CONFIDENCE_CAPS 缺少 unknown 键，`.get(overall, 100)` 会让"
        "「未验证」拿到完全可信的上限（100）"
    )
    assert CONFIDENCE_CAPS["unknown"] < 100, "unknown 必须真的封顶，不能等于 100"


def test_clamp_confidence_actually_caps_unknown():
    from tradingagents.dataflows.freshness.aggregator import clamp_confidence
    from tradingagents.dataflows.freshness.types import CONFIDENCE_CAPS

    assert clamp_confidence(95, "unknown") == CONFIDENCE_CAPS["unknown"]
    assert clamp_confidence(95, "unknown") < 95, "未验证的新鲜度必须压低置信度"


# ══════════════════════════════════════════════════════════════════════════
# 1. 未知 category 不得判 fresh
# ══════════════════════════════════════════════════════════════════════════


def test_unknown_category_is_unknown_not_fresh(monkeypatch):
    """注册在案、但 `category` 不认识时，旧实现直接返回 `fresh`。"""
    from tradingagents.dataflows.freshness import validator

    monkeypatch.setattr(
        validator,
        "get_contract_entry",
        lambda key: {"category": "某种新类别", "criticality": "important"},
    )
    ctx = validator.build_eval_context("2026-03-20", symbol="600519.SH")

    meta = validator.evaluate_source("stock_data", "随便一段文本", ctx)

    assert meta.status != "fresh", (
        f"未知 category 不得判 fresh（等于把未验证当最新）：{meta.status}"
    )
    assert meta.status in {"unknown", "error"}, f"应为 unknown/error：{meta.status}"


# ══════════════════════════════════════════════════════════════════════════
# 2. 事件时间戳解析失败不得判 fresh
# ══════════════════════════════════════════════════════════════════════════


def test_event_driven_without_timestamp_is_unknown_not_fresh(monkeypatch):
    from tradingagents.dataflows.freshness import validator

    monkeypatch.setattr(
        validator,
        "get_contract_entry",
        lambda key: {
            "category": "event_driven",
            "criticality": "important",
            "window_hours": 72,
        },
    )
    ctx = validator.build_eval_context("2026-03-20", symbol="600519.SH")

    # 纯散文、没有任何可解析的时间戳
    meta = validator.evaluate_source("stock_news", "公司发布了公告，内容略。", ctx)

    assert meta.status != "fresh", (
        f"解析不出事件时间戳时不得判 fresh：{meta.status}（lag_note={meta.lag_note}）"
    )


# ══════════════════════════════════════════════════════════════════════════
# 3. 全 unknown 的池子不得聚合出 fresh
# ══════════════════════════════════════════════════════════════════════════


def _unknown_pool():
    return {
        "stock_data": {"source_key": "stock_data", "status": "unknown"},
        "stock_news": {"source_key": "stock_news", "status": "unknown"},
    }


def test_all_unknown_pool_does_not_aggregate_to_fresh():
    from tradingagents.dataflows.freshness.aggregator import build_freshness_summary

    summary = build_freshness_summary(_unknown_pool(), "2026-03-20", symbol="600519.SH")

    assert summary["overall_status"] != "fresh", (
        "池子里没有任何一个源被验证过，聚合结果不得是 fresh"
        f"（实际 {summary['overall_status']}）"
    )
    assert summary["confidence_cap"] < 100, (
        f"未验证的池子必须封顶：{summary['confidence_cap']}"
    )


def test_fresh_pool_still_aggregates_to_fresh():
    """**反过度修正**：真的验证过是新鲜时，仍应给 fresh / 100。"""
    from tradingagents.dataflows.freshness.aggregator import build_freshness_summary

    pool = {
        "stock_data": {"source_key": "stock_data", "status": "fresh"},
    }
    summary = build_freshness_summary(pool, "2026-03-20", symbol="600519.SH")

    assert summary["overall_status"] == "fresh"
    assert summary["confidence_cap"] == 100


# ══════════════════════════════════════════════════════════════════════════
# 4. report_age_days 须如实
# ══════════════════════════════════════════════════════════════════════════


def test_report_age_days_is_not_hardcoded_zero():
    """历史日期的分析必须给出真实报告年龄，不能一律 0（「0 天前、无告警」）。"""
    from tradingagents.dataflows.freshness.aggregator import build_freshness_summary

    summary = build_freshness_summary(
        {"stock_data": {"source_key": "stock_data", "status": "fresh"}},
        "2020-01-02",
        symbol="600519.SH",
    )

    age = summary["report_age_days"]
    assert age is not None, "报告年龄不得为 None：调用方无法区分「0 天」与「不知道」"
    assert age > 30, f"2020-01-02 的报告年龄不可能还是 0 天：{age}"


def test_recent_report_age_is_zero_or_small():
    """**反过度修正**：刚生成的分析年龄应当很小。"""
    from datetime import datetime, timezone

    from tradingagents.dataflows.freshness.aggregator import build_freshness_summary

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    summary = build_freshness_summary(
        {"stock_data": {"source_key": "stock_data", "status": "fresh"}},
        today,
        symbol="600519.SH",
    )

    assert 0 <= summary["report_age_days"] <= 1


# ══════════════════════════════════════════════════════════════════════════
# 5. 缺 overall_status 不得当 fresh
# ══════════════════════════════════════════════════════════════════════════


def test_missing_overall_status_is_not_fresh(monkeypatch):
    """`str(summary.get("overall_status") or "fresh")` —— 缺失即 fresh 且跳过上限。"""
    from api.services import report_service

    captured: dict = {}

    def _fake_clamp(confidence, overall_status):
        captured["overall"] = overall_status
        return 40

    monkeypatch.setattr(report_service, "clamp_confidence", _fake_clamp, raising=False)

    getter = getattr(report_service, "_overall_status_or_unknown", None)
    assert getter is not None, (
        "缺少把缺失 overall_status 判为 unknown 的入口："
        "report_service 里 `or \"fresh\"` 会把缺失当成新鲜"
    )
    assert getter({"overall_status": None}) == "unknown"
    assert getter({}) == "unknown"
    assert getter({"overall_status": "stale"}) == "stale"
    assert getter({"overall_status": "FRESH"}) == "fresh"
