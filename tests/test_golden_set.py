"""黄金集与评分 harness 的自检。

## 这个文件不测"当前实现好不好"

当前实现在黄金集上一定很低分（实测 14 句自然说法 13 句被 400）——那是**基线**，
由 `scripts/golden_report.py` 产出报告，不是测试断言。若把"实现达标"写成断言，
CI 会长期红着，然后被人习惯性忽略，度量就白做了。

这里只锁两件事：

1. **数据集本身是合法的**：schema、枚举、工具名真实存在、id 唯一。
   数据集烂掉会让所有后续对比失去意义，而且这种烂是静默的。
2. **评分口径不会漂**：`score()` 是纯函数，用人工构造的观测值验证它对
   "漏工具""多调工具""该追问没追问"的判断是否如声明。
"""

from __future__ import annotations

import pytest

from tests.golden.harness import (
    INTENTS,
    GoldenItem,
    Probe,
    load_golden,
    score,
)

GOLDEN = load_golden()


def test_golden_set_is_non_trivial():
    assert len(GOLDEN) >= 40, "黄金集太小，跑分没有统计意义"
    ids = [item.id for item in GOLDEN]
    assert len(ids) == len(set(ids)), "id 必须唯一"
    for item in GOLDEN:
        assert item.text.strip(), f"{item.id} 没有问句文本"


def test_every_intent_is_known():
    for item in GOLDEN:
        assert item.intent in INTENTS, f"{item.id} 的意图 {item.intent!r} 不在枚举内"


def test_golden_covers_the_measured_failure_classes():
    """上一轮实测失败的类别必须在集子里——否则基线会好看但与现实脱节。"""
    by_intent = {item.intent for item in GOLDEN}
    for intent in ("market_view", "portfolio_review", "concept_qa", "multi_symbol_compare"):
        assert intent in by_intent, f"缺少 {intent} 类别（实测中该类整体失败）"
    texts = " ".join(item.text for item in GOLDEN)
    for phrase in ("今天大盘怎么样", "最近哪个板块最强", "我持仓里哪只最危险", "什么是洗盘"):
        assert phrase in texts, f"实测失败问句 {phrase!r} 未纳入黄金集"


def test_required_tools_exist_in_the_registry():
    """数据集里写的工具必须真实存在，否则"漏工具"这类失败无法归因。

    用 `warm_registry()`（= 规划器看到的**同一份**工具面）。此前这里手工用哑依赖
    装配，只得到 26 个工具，于是"数据集写错"与"跑分看不到这个工具"分不开——
    本轮排查正是被这一点误导过。
    """
    known = _registry_names()
    assert "signal_washout_probe" in known
    for item in GOLDEN:
        for tool in item.required_tools + item.optional_tools:
            assert tool in known, f"{item.id} 引用的工具 {tool!r} 不在工具面里"


def test_declared_capabilities_are_not_silently_expected_as_tools():
    """`required_capabilities` 只装**工具面里确实没有**的东西。

    这个字段的原始语义是"待建能力"：能力建好、封成工具之后就必须迁到
    `required_tools`，否则那条期望**永远不被评分**（`score()` 只读
    required/optional_tools）。实测正是如此：18 个新 L1 工具里有 9 个被写成了
    capability，于是"规划器选对了新工具"这件事在跑分里根本不计分，而它偶尔多调的
    一个工具却全额扣分——精确率被单向压到 52%。

    这条断言以前只能靠人工比对；现在工具面在册可查，就可以写成硬约束。
    """
    known = _registry_names()
    for item in GOLDEN:
        for capability in item.required_capabilities:
            assert capability not in known, (
                f"{item.id} 的能力 {capability!r} 已经是真实工具了，"
                "必须迁到 required_tools，否则这条期望不会被评分"
            )


def test_every_declared_capability_is_a_known_non_tool():
    """capability 名字必须落在**明确的非工具清单**里，不能是随手写的词。

    否则数据集会悄悄积累"谁都满足不了"的期望：它不参与评分，看起来无害，
    实际上是把一条产品预期从账上抹掉了。
    """
    from tests.golden.harness import NON_TOOL_CAPABILITIES

    for item in GOLDEN:
        for capability in item.required_capabilities:
            assert capability in NON_TOOL_CAPABILITIES, (
                f"{item.id} 的 capability {capability!r} 既不是工具也没登记为"
                "「刻意不做成工具的能力」"
            )


def test_concept_questions_require_no_tools():
    """概念性问答不该被逼着调工具——那会变成"用数据回答常识"。"""
    for item in GOLDEN:
        if item.intent == "concept_qa":
            assert item.required_tools == ()
            assert item.required_capabilities == ()


def test_scoring_reports_capabilities_nobody_can_satisfy():
    """评分必须**报告**未登记的 capability，而不是静默忽略。

    "静默"是本仓库最贵的失败形态：它让一条永远失败的期望看起来像不存在。
    """
    from tests.golden.harness import Metrics, score

    item = _item(required_capabilities=("nobody_built_this",))
    m = score([item], [Probe(servable=True, intent=item.intent)])
    assert m.unmapped_capabilities == [{"id": "x", "capability": "nobody_built_this"}]


def _registry_names() -> set[str]:
    from tests.golden.harness import warm_registry

    return set(warm_registry().names())


# ── 评分口径 ──────────────────────────────────────────────────────────────


def _item(**kw) -> GoldenItem:
    base = dict(id="x", text="t", intent="single_symbol_judgement")
    base.update(kw)
    return GoldenItem(**base)


def test_perfect_probe_scores_one():
    item = _item(symbols=("600519.SH",), required_tools=("signal_washout_probe",))
    probe = Probe(servable=True, intent=item.intent, symbols=item.symbols, tools=item.required_tools)
    m = score([item], [probe])
    assert m.coverage == 1.0
    assert m.planning_accuracy == 1.0
    assert m.tool_recall == 1.0
    assert m.tool_precision == 1.0
    assert m.failures == []


def test_missing_required_tool_hurts_recall_not_accuracy():
    item = _item(symbols=("600519.SH",), required_tools=("signal_washout_probe",))
    probe = Probe(servable=True, intent=item.intent, symbols=item.symbols, tools=())
    m = score([item], [probe])
    assert m.planning_accuracy == 1.0, "标的与意图都对，规划准确率不该被工具拖累"
    assert m.tool_recall == 0.0
    assert any(f["why"] == "missing required tools" for f in m.failures)


def test_extra_unexpected_tool_hurts_precision():
    item = _item(symbols=("600519.SH",), required_tools=("signal_washout_probe",))
    probe = Probe(
        servable=True, intent=item.intent, symbols=item.symbols,
        tools=("signal_washout_probe", "get_zt_pool"),
    )
    m = score([item], [probe])
    assert m.tool_recall == 1.0
    assert m.tool_precision == 0.5, "多调的工具应计入精确率损失"


def test_optional_tool_does_not_hurt_precision():
    item = _item(
        symbols=("600519.SH",),
        required_tools=("signal_washout_probe",),
        optional_tools=("data_symbol_news",),
    )
    probe = Probe(
        servable=True, intent=item.intent, symbols=item.symbols,
        tools=("signal_washout_probe", "data_symbol_news"),
    )
    m = score([item], [probe])
    assert m.tool_precision == 1.0, "允许的可选工具不该算噪声"


def test_unservable_turn_dents_coverage():
    item = _item(symbols=("600519.SH",), required_tools=("signal_washout_probe",))
    probe = Probe(servable=False, clarifying=True)
    m = score([item], [probe])
    assert m.coverage == 0.0
    assert any(f["why"] == "planning mismatch" for f in m.failures)


def test_clarify_expectation_is_checked_both_ways():
    """该问的没问、不该问的乱问，都要被抓到。"""
    item = _item(expect_clarify=True)
    m = score([item], [Probe(servable=True, intent=item.intent, clarifying=False)])
    assert m.clarify_accuracy == 0.0

    item2 = _item(id="y", expect_clarify=False)
    m2 = score([item2], [Probe(servable=True, intent=item2.intent, clarifying=True)])
    assert m2.clarify_accuracy == 0.0

    m3 = score([item], [Probe(servable=True, intent=item.intent, clarifying=True)])
    assert m3.clarify_accuracy == 1.0


def test_items_without_clarify_expectation_do_not_affect_the_metric():
    item = _item(expect_clarify=None)
    m = score([item], [Probe(servable=True, intent=item.intent, clarifying=True)])
    assert m.clarify_accuracy == 1.0, "未声明期望的条目不该拉低澄清正确率"


def test_empty_metrics_are_not_silently_perfect():
    m = score([], [])
    assert m.total == 0
    assert m.coverage == 0.0
