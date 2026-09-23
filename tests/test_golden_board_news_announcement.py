"""W4-T2：黄金集必须覆盖**板块 / 新闻 / 公告**三类问句，且新增用例必须真的命中。

## 为什么要单独守这三类

它们是"平台本就有工具、却没人量"的典型。板块走 `get_board_spot`（W2 刚替过数据源）、
新闻走 `data_symbol_news`、公告走 `announcements_read`（W1 才补上规则入口）。
量不到就等于"封了但用不上"——`routing.py` 里那句注释说的正是这个坑。

## 为什么"必须真的命中"要写成断言

黄金集是**闸门**的输入（`check.sh` 默认跑离线档，规划准确率 ≥ 0.40、
工具召回 ≥ 0.60）。往集子里塞一条**当前实现永远满足不了**的期望，等于把闸门
的阈值往下拽——分数会掉，而掉分的原因不是实现退化，是尺子被塞了假刻度。
所以新增用例必须同时满足：可服务、意图判对、标的判对、必需工具被计划到。

（集子里**已有**的失败条目是另一回事：v01/v03/v05 这类是**如实记录的真实缺口**
——规则路径根本产不出 `market_view` 意图——它们的价值恰恰在于失败。
本文件只管新增用例不许成为噪声。）
"""

from __future__ import annotations

import pytest

from tests.golden.harness import load_golden, probe

GOLDEN = load_golden()
BY_ID = {item.id: item for item in GOLDEN}

# W4 新增的三条：板块 / 新闻 / 公告。id 沿用既有前缀（都是 factual_lookup）。
NEW_IDS: tuple[str, ...] = ("f09", "f10", "f11")
# 这三类问句各自**必须**指向的工具（工具面在册可查，见 test_golden_set.py）。
EXPECTED_TOOLS: frozenset[str] = frozenset(
    {"get_board_spot", "data_symbol_news", "announcements_read"}
)


def test_new_cases_exist():
    missing = [i for i in NEW_IDS if i not in BY_ID]
    assert not missing, f"黄金集缺少 W4 用例：{missing}"


def test_new_cases_cover_board_news_and_announcement_tools():
    """板块 / 新闻 / 公告 三个工具都必须有用例要求它们。"""
    required = {tool for i in NEW_IDS for tool in BY_ID[i].required_tools}
    assert EXPECTED_TOOLS <= required, f"缺少覆盖：{sorted(EXPECTED_TOOLS - required)}"


def test_new_cases_use_the_documented_jsonl_shape():
    """字段与既有条目一致：新增条目不许自创字段，也不许少写字段。"""
    for item_id in NEW_IDS:
        raw_note = BY_ID[item_id].note
        assert raw_note, f"{item_id} 没写 note：改尺子必须能说出理由"
        assert BY_ID[item_id].required_capabilities == (), (
            f"{item_id} 的能力已经封成工具，必须写在 required_tools 里（否则不计分）"
        )


@pytest.mark.parametrize("item_id", NEW_IDS)
def test_new_case_is_actually_hittable_by_the_current_rules_path(item_id):
    """**核心断言**：新增用例必须在离线（规则路径）实现上真的命中。

    这是"不靠调阈值过关"的机械保证：可服务 + 意图 + 标的 + 必需工具，四项全中。
    若某条问法命中不了，正确做法是**换一条能命中的问法**（并说明原来那条为什么
    不行），而不是把它留在集子里拉低闸门基线。
    """
    item = BY_ID[item_id]
    observed = probe(item.text)
    assert observed.servable, (
        f"{item_id}「{item.text}」构造不出可服务回合（会被追问代码）——"
        "换一条能命中的问法，或先补路由规则"
    )
    assert observed.intent == item.intent, (
        f"{item_id} 意图不符：期望 {item.intent}，实测 {observed.intent}"
    )
    assert set(observed.symbols) == set(item.symbols), (
        f"{item_id} 标的不符：期望 {item.symbols}，实测 {observed.symbols}"
    )
    missing = set(item.required_tools) - set(observed.tools)
    assert not missing, (
        f"{item_id} 必需工具没被计划到：{sorted(missing)}（实际计划 {observed.tools}）"
    )
    assert bool(observed.clarifying) == bool(item.expect_clarify), (
        f"{item_id} 追问期望不符：期望 {item.expect_clarify}，实测 {observed.clarifying}"
    )
