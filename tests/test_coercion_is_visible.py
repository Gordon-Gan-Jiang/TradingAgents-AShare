"""良性兜底必须可见（见 docs/report-judgment-accuracy-audit.md §4.6、§4.7）。

## §4.6 兜底把「不知道」说成「中等」

`RiskItemSchema._coerce_level` 只认 `high/medium/low` 三个英文小写词，**其余一律
返回 `medium`**：

    level="高"  → "medium"       （中文「高」被降级成中等）
    level="严重" → "medium"
    level 缺失   → "medium"

`_coerce_status` 同理：`status="优" → "neutral"`。
落库实测后果：**风险 73% 是 medium、指标 97.4% 是 neutral**——这不是"市场以中等
风险为主"，而是兜底把真实取值抹平了。更糟的是"真·中等风险"与"没识别出来"
**逐字节相同**，看报告的人无从分辨。

## §4.7 半可解析的抽取结果落库成「无风险」

`risks` / `key_metrics` 默认 `default_factory=list`：只要 `StructuredReport(**parsed)`
能构造成功（哪怕 dict 里根本没有 `risks` 键），就得到 `[]`——与"分析确认没有风险"
**逐字节相同**。纯散文会诚实失败成 NULL，反倒是半可解析的残片更危险。

判据：
* 无法识别时必须落到**显式的 unknown**，不得冒充 medium/neutral；
* 兜底必须**留痕**（日志可见）；
* 「字段缺失」必须与「确认没有」可区分（None vs []）。
"""

from __future__ import annotations

import logging

import pytest


# ══════════════════════════════════════════════════════════════════════════
# §4.6 风险等级
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "raw,expected",
    [
        # 规范值原样保留
        ("high", "high"),
        ("medium", "medium"),
        ("low", "low"),
        ("HIGH", "high"),
        ("High", "high"),
        (" high ", "high"),
        # 中文：这才是落地报告里真正出现的写法
        ("高", "high"),
        ("高风险", "high"),
        ("较高", "high"),
        ("严重", "high"),
        ("重大", "high"),
        ("中", "medium"),
        ("中等", "medium"),
        ("中风险", "medium"),
        ("一般", "medium"),
        ("低", "low"),
        ("较低", "low"),
        ("低风险", "low"),
        # 英文同义
        ("moderate", "medium"),
        ("critical", "high"),
        ("severe", "high"),
        ("minor", "low"),
    ],
)
def test_level_synonyms_are_mapped_not_flattened(raw, expected):
    from api.services.report_service import RiskItemSchema

    item = RiskItemSchema(name="测试风险", level=raw)

    assert item.level == expected, (
        f"level={raw!r} 应映射为 {expected!r}，实际 {item.level!r}"
        "（旧实现把中文「高」等一律抹成 medium，导致 73% 落库为 medium）"
    )


@pytest.mark.parametrize("raw", ["莫名其妙", "", "  ", None, 42, "很高很高"])
def test_unrecognised_level_becomes_unknown_not_medium(raw):
    from api.services.report_service import RiskItemSchema

    item = RiskItemSchema(name="测试风险", level=raw)

    assert item.level == "unknown", (
        f"无法识别的 level={raw!r} 必须落到显式 unknown，"
        f"不能冒充 medium（那与真实的中等风险不可区分）：实际 {item.level!r}"
    )


def test_missing_level_is_unknown_not_medium():
    """键缺失同样不是「中等」——字段默认值也曾是 medium。"""
    from api.services.report_service import RiskItemSchema

    assert RiskItemSchema(name="只有名字").level == "unknown"


def test_level_fallback_leaves_a_trace(caplog):
    """兜底必须留痕：否则"共识别了多少条"永远无法统计。"""
    from api.services.report_service import RiskItemSchema

    with caplog.at_level(logging.WARNING):
        RiskItemSchema(name="测试风险", level="莫名其妙")

    assert any(
        "level" in r.message or "level" in str(r.args) for r in caplog.records
    ), f"兜底必须打日志留痕，实际记录：{[r.message for r in caplog.records]}"


# ══════════════════════════════════════════════════════════════════════════
# §4.6 指标优劣
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("good", "good"),
        ("neutral", "neutral"),
        ("bad", "bad"),
        ("GOOD", "good"),
        ("好", "good"),
        ("优", "good"),
        ("优秀", "good"),
        ("良好", "good"),
        ("正面", "good"),
        ("中", "neutral"),
        ("中性", "neutral"),
        ("一般", "neutral"),
        ("持平", "neutral"),
        ("差", "bad"),
        ("较差", "bad"),
        ("负面", "bad"),
        ("劣", "bad"),
        ("poor", "bad"),
        ("excellent", "good"),
        ("average", "neutral"),
    ],
)
def test_status_synonyms_are_mapped_not_flattened(raw, expected):
    from api.services.report_service import KeyMetricSchema

    metric = KeyMetricSchema(name="PE", value="28.5x", status=raw)

    assert metric.status == expected, (
        f"status={raw!r} 应映射为 {expected!r}，实际 {metric.status!r}"
        "（旧实现把「优」等一律抹成 neutral，导致 97.4% 落库为 neutral）"
    )


@pytest.mark.parametrize("raw", ["莫名其妙", "", "  ", None, 7])
def test_unrecognised_status_becomes_unknown_not_neutral(raw):
    from api.services.report_service import KeyMetricSchema

    metric = KeyMetricSchema(name="PE", value="28.5x", status=raw)

    assert metric.status == "unknown", (
        f"无法识别的 status={raw!r} 必须落到显式 unknown，不能冒充 neutral："
        f"实际 {metric.status!r}"
    )


def test_missing_status_is_unknown_not_neutral():
    from api.services.report_service import KeyMetricSchema

    assert KeyMetricSchema(name="PE", value="28.5x").status == "unknown"


def test_status_fallback_leaves_a_trace(caplog):
    from api.services.report_service import KeyMetricSchema

    with caplog.at_level(logging.WARNING):
        KeyMetricSchema(name="PE", value="28.5x", status="莫名其妙")

    assert any(
        "status" in r.message or "status" in str(r.args) for r in caplog.records
    ), f"兜底必须打日志留痕，实际记录：{[r.message for r in caplog.records]}"


# ══════════════════════════════════════════════════════════════════════════
# §4.7 「字段缺失」必须与「确认没有」可区分
# ══════════════════════════════════════════════════════════════════════════


def test_missing_risks_key_is_none_not_empty_list():
    """**核心**：dict 里没有 `risks` 键 → None（没抽到），而不是 []（确认没有风险）。"""
    from api.services.report_service import StructuredReport

    report = StructuredReport(**{"decision": "BUY", "confidence": 70})

    assert report.risks is None, (
        f"缺少 risks 键时必须是 None，实际 {report.risks!r}"
        "（[] 与「分析确认没有风险」逐字节相同，会误导读者）"
    )
    assert report.key_metrics is None


def test_explicit_empty_list_stays_empty_list():
    """**反过度修正**：模型明确给出 `[]` 时，那就是"确认没有"，必须保留为 []。"""
    from api.services.report_service import StructuredReport

    report = StructuredReport(**{"decision": "BUY", "risks": [], "key_metrics": []})

    assert report.risks == []
    assert report.key_metrics == []


def test_half_parsed_fragment_is_distinguishable_from_confirmed_empty():
    """半可解析的残片（只有 decision）与"确认无风险"必须落在不同值上。"""
    from api.services.report_service import StructuredReport

    fragment = StructuredReport(**{"decision": "HOLD"})
    confirmed = StructuredReport(**{"decision": "HOLD", "risks": []})

    assert fragment.risks != confirmed.risks, (
        "残片与确认空必须是可区分的两种状态"
    )


def test_persistence_helper_preserves_none_vs_empty():
    """落库/回传时的转换必须保留这个区别（None → None，[] → []）。"""
    from api.services.report_service import StructuredReport, dump_structured_items

    fragment = StructuredReport(**{"decision": "HOLD"})
    confirmed = StructuredReport(**{"decision": "HOLD", "risks": []})
    populated = StructuredReport(
        **{"decision": "HOLD", "risks": [{"name": "估值高", "level": "高"}]}
    )

    assert dump_structured_items(fragment, "risks") is None
    assert dump_structured_items(confirmed, "risks") == []
    dumped = dump_structured_items(populated, "risks")
    assert dumped and dumped[0]["level"] == "high", (
        f"落库时等级必须已被正确映射：{dumped!r}"
    )
    assert dump_structured_items(None, "risks") is None
