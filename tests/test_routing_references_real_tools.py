"""规则层引用的工具**必须真的在册**——约束要变成会失败的测试。

## 这条约束为什么必须存在

`tradingagents/agent/routing.py` 的三张表（`FACTUAL_TOOL_HINTS` /
`SYMBOL_REQUIRED_TOOLS` / `MARKET_LEVEL_TOOLS`）是**规则路径**的全部依据。规则
路径的特点是"零 LLM、毫秒级"，好处是快，代价是它**不会自己发现表里的工具不存在**：

* `FACTUAL_TOOL_HINTS` 指向一个不在册的工具 → `_plan_tools` 把它当作"未注册"
  丢掉，然后落 `DEFAULT_*` 兜底：用户问龙虎榜，平台答板块行情。
* `SYMBOL_REQUIRED_TOOLS` 漏登记一个需要代码的工具 → 无标的的问句也能选中它，
  然后必然失败——用户看到"查过了但没数据"。
* `MARKET_LEVEL_TOOLS` 指向一个不在册的工具 → 市场级问句没有落点。

这三种都不是崩溃，是**静默走样**。实测形态（本轮退役 8 个 vendor 工具时真实出现）：

    '今天龙虎榜有多少只' → tier=light market_level=True planned=['get_board_spot']

所以这里把"表里的名字必须存在于注册表"变成一条会红的断言。它顶住的是本轮的核心
风险：**退役工具时，注册表改了而规则表没跟着改**。

## 为什么用生产注册表

规则路径在生产里跑的是 `chat_service.build_registry()` 装配出来的那一份（含
所有注入端口：指数取数、公告、证据取数集合……）。只检查 `make_data_tools()`
这样的局部集合会漏掉"端口没注入导致工具根本没注册"这一类缺口——`index_quote_read`
正是这种：它依赖 `index_fetcher`，组合根不装配就不在册。
"""

from __future__ import annotations

import pytest

from api.services import chat_service


@pytest.fixture(scope="module")
def production_names() -> set[str]:
    return set(chat_service.build_registry("routing-guardrail").names())


# ══════════════════════════════════════════════════════════════════════════
# 三张表里的每个名字都要在册
# ══════════════════════════════════════════════════════════════════════════


def test_factual_hints_only_name_registered_tools(production_names):
    """`FACTUAL_TOOL_HINTS` 指向的工具必须都存在。

    失败信息要点出**具体是哪个工具**：只说"断言失败"的话，下一个人得自己 diff
    两张表才能知道是谁漂了。
    """
    from tradingagents.agent.routing import FACTUAL_TOOL_HINTS

    named = {tool for _hints, tool in FACTUAL_TOOL_HINTS}
    missing = sorted(named - production_names)
    assert not missing, (
        f"规则层 FACTUAL_TOOL_HINTS 指向了不在册的工具：{missing}。"
        "这些工具要么被退役（那就把规则入口一并摘掉，并登记进 "
        "RETIRED_CAPABILITY_HINTS 让问句如实弃权），要么是拼错了名字。"
        "留在表里会让执行层丢掉这一步、改落 DEFAULT_* 兜底——用另一个问题的数据"
        "回答这个问题。"
    )


def test_symbol_required_tools_are_all_registered(production_names):
    """`SYMBOL_REQUIRED_TOOLS` 里的名字必须都在册。"""
    from tradingagents.agent.routing import SYMBOL_REQUIRED_TOOLS

    missing = sorted(set(SYMBOL_REQUIRED_TOOLS) - production_names)
    assert not missing, (
        f"SYMBOL_REQUIRED_TOOLS 登记了不在册的工具：{missing}。"
        "退役的工具要从这里删掉；留着会让「需要代码」的语义指向一个幽灵条目。"
    )


def test_market_level_tools_are_all_registered(production_names):
    """`MARKET_LEVEL_TOOLS` 里的名字必须都在册。"""
    from tradingagents.agent.routing import MARKET_LEVEL_TOOLS

    missing = sorted(set(MARKET_LEVEL_TOOLS) - production_names)
    assert not missing, (
        f"MARKET_LEVEL_TOOLS 登记了不在册的工具：{missing}。"
        "无标的的市场级问句会因此没有落点。"
    )


def test_default_tools_are_registered(production_names):
    """两个 `DEFAULT_*` 兜底本身就是工具名，同样必须在册。

    它们比表里的条目更危险：兜底是"没匹配上"时的最终去处，一个不在册的默认工具
    会让所有未匹配的事实问句都变成一次被丢弃的调用。
    """
    from tradingagents.agent.routing import DEFAULT_FACTUAL_TOOL, DEFAULT_MARKET_TOOL

    missing = sorted({DEFAULT_FACTUAL_TOOL, DEFAULT_MARKET_TOOL} - production_names)
    assert not missing, f"DEFAULT_* 兜底指向了不在册的工具：{missing}"


def test_symbol_to_market_fallback_targets_are_registered_and_market_level(
    production_names,
):
    """`SYMBOL_TOOL_MARKET_FALLBACK` 的两端都必须是**在册的真名**，且落点必须是市场级。

    这张表决定"命中需标的工具、问句却没有标的"时去哪个工具——实测修前的行为是
    「最近有什么新闻」→ `get_board_spot`（答非所问）。这张表是那个缺陷的修法，
    因此它自己也要被同一套约束盯住：

    * 键必须是 `SYMBOL_REQUIRED_TOOLS` 里的工具（否则这条兜底永远不会被触发，
      下一个人会以为它生效了）；
    * 值必须在 `MARKET_LEVEL_TOOLS` 里（否则无标的时又被指向一个需代码的工具，
      回到"必然失败的调用"）；
    * 两端都要在册（不在册 = 静默落回 `DEFAULT_*` 兜底，正是这次修掉的形态）。
    """
    from tradingagents.agent.routing import (
        MARKET_LEVEL_TOOLS,
        SYMBOL_REQUIRED_TOOLS,
        SYMBOL_TOOL_MARKET_FALLBACK,
    )

    assert SYMBOL_TOOL_MARKET_FALLBACK, "映射表不该是空的（新闻是已确认的一条）"

    for source, target in SYMBOL_TOOL_MARKET_FALLBACK.items():
        assert source in SYMBOL_REQUIRED_TOOLS, (
            f"{source!r} 不是需标的工具，这条兜底永远不会被触发"
        )
        assert target in MARKET_LEVEL_TOOLS, (
            f"{source!r} 的市况级对应 {target!r} 不在 MARKET_LEVEL_TOOLS 里——"
            "无标的时会被指向一个非市场级工具"
        )
        assert source != target
    missing = sorted(set(SYMBOL_TOOL_MARKET_FALLBACK.values()) - production_names)
    assert not missing, f"市况级对应工具不在册：{missing}"


# ══════════════════════════════════════════════════════════════════════════
# 反向：退役能力必须真的"没有落点"
# ══════════════════════════════════════════════════════════════════════════


def test_retired_capability_questions_do_not_get_an_unrelated_tool():
    """退役能力的问句必须**没有落点**，而不是被兜底到另一个工具。

    这是上面那条约束的"另一半"：光把退役工具的名字从表里删掉还不够——删掉之后
    规则层若返回 `None`，执行层的 `DEFAULT_*` 兜底会顶上，于是用户问龙虎榜、
    平台答板块行情。实测过这个形态，所以这里锁住。

    正确形态是空串（`NO_FACTUAL_TOOL`）→ 空计划 → 回答层如实报"数据未覆盖"。
    "没有证据就没有回答"要的就是这个，而不是一个跑题的答案。
    """
    from tradingagents.agent.routing import (
        MARKET_LEVEL_TOOLS,
        NO_FACTUAL_TOOL,
        RETIRED_CAPABILITY_HINTS,
        plan_factual_tool,
    )

    assert RETIRED_CAPABILITY_HINTS, "退役能力标记表不该是空的"

    # 三类退役能力各取一个代表问句：{现金流, 技术指标, 龙虎榜}
    probes = (
        "600519 的现金流是多少",
        "600519 的 MACD 是多少",
        "今天龙虎榜有多少只",
    )
    for text in probes:
        matched = next(
            (h for h in RETIRED_CAPABILITY_HINTS if h in text or h in text.lower()), None
        )
        assert matched, f"{text!r} 应该命中退役能力标记"
        for has_symbol in (True, False):
            chosen = plan_factual_tool(text, has_symbol=has_symbol)
            assert chosen == NO_FACTUAL_TOOL, (
                f"{text!r}（has_symbol={has_symbol}）本平台已无对应工具，"
                f"却被指向 {chosen!r}——这是拿另一个问题的数据回答这个问题"
            )
            assert chosen not in MARKET_LEVEL_TOOLS

    # 退役能力**不**等于"市场级可答"：它只是不该被替换成别的工具。
    assert NO_FACTUAL_TOOL not in MARKET_LEVEL_TOOLS, (
        "空串不是工具名，绝不能被当成市场级落点登记"
    )


# ══════════════════════════════════════════════════════════════════════════
# 反向：喂给模型的提示里也不许出现不存在的工具名
# ══════════════════════════════════════════════════════════════════════════


def test_vendor_note_only_names_registered_tools(production_names):
    """`VENDOR_NOTE` 是**喂给模型**的文本，里面点到的工具必须真的在册。

    这条约束是本轮修掉的一个真实缺陷的可执行形式：`VENDOR_NOTE` 曾写着
    「请改用结构化 data_* 工具（kline / news / washout 等）」，而 `kline` 在注册表里
    **根本不存在**。把不存在的名字写进提示词，模型会照着编一个工具名出来，然后收到
    `unknown arguments`——一次必然失败的调用，而且失败原因离真正的错处（提示词）很远。

    它与上面三张表的约束是同一类风险的不同出口：**表格/提示词都是"名字的消费者"，
    名字的供给方只有注册表**。退役工具时，所有消费者都要跟着改；这条断言负责盯住
    `VENDOR_NOTE`，免得它又漂回去。
    """
    import re

    from tradingagents.tools.data import VENDOR_NOTE

    named = set(re.findall(r"`([a-z_][a-z0-9_]*)`", VENDOR_NOTE))
    assert named, "VENDOR_NOTE 里没有再点出任何工具名——若确实改成不点名了，请删掉这条断言"
    missing = sorted(named - production_names)
    assert not missing, (
        f"VENDOR_NOTE 点名了不在册的工具：{missing}。"
        "提示词里只允许出现真名：不存在的名字会让模型编出一个工具调用并必然失败。"
    )
