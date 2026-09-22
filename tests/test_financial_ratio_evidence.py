"""财务比率的证据契约：比率要能被回答，也要标对单位。

## 背景：一个"数据明明在，却答不出来"的缺陷

「600519 的毛利率是多少」此前注定降级。原因**不在**模型，也不在数据——
数据一直在，只是问句被指向了错的工具：

| 工具 | 证据数 | 比率类字段 |
|---|---|---|
| `get_income_statement` | 40 | **0 条**（只有营业收入/营业成本等原始组成项） |
| `get_fundamentals` | 40 | 毛利率 89.5552 / ROE 16.75 / 资产负债率 15.1931 / 销售净利率 50.7516 / ROA 20.0558 |

路由把「毛利率」接到了 `get_income_statement`，于是模型拿不到比率，只能自己算；
而「判定权在确定性层」——自算值被校验器判 `unsourced_number`，整轮降级。
用户看到的是"没能给出可溯源的回答"，而答案本来就在隔壁那个工具里。

`get_balance_sheet` 同理：它的 40 条证据**全是资产科目**（货币资金/应收账款/
预付款项…），**不含任何负债率字段**，所以「资产负债率」在它那里结构上无解。

## 第二个缺陷：单位被写死成"元"

两个 Markdown 表格解析路径都硬编码 `unit="yuan"`，于是
`净资产收益率(ROE) = 16.75` 被标成 16.75 **元**。这不是美观问题：

1. 提示词把单位渲染给模型（`… = 89.5552 (yuan, 截至 …)`），模型据此会写成「元」；
2. `EvidenceItem.is_numeric` 要求 `unit ∈ NUMERIC_UNITS`；
3. 校验器按 `unit in {"ratio","pct"}` 决定要不要做 ×100/÷100 换算。

判据按**字段名**，且"率"优先——`净资产收益率` 同时含"率"与"资产"，
它是指标不是金额。
"""

from __future__ import annotations

import pytest

from tradingagents.grounding.types import NUMERIC_UNITS
from tradingagents.tools.data import _unit_for_metric


# ── 单位推断 ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "field,expected",
    [
        # 比率：必须是 pct，否则模型会照着 (yuan) 写成"元"
        ("毛利率", "pct"),
        ("销售净利率", "pct"),
        ("净资产收益率(ROE)", "pct"),
        ("总资产报酬率(ROA)", "pct"),
        ("资产负债率", "pct"),
        # 金额
        ("营业总收入", "yuan"),
        ("营业成本", "yuan"),
        ("归母净利润", "yuan"),
        ("经营现金流量净额", "yuan"),
        ("股东权益合计(净资产)", "yuan"),
        ("基本每股收益", "yuan"),
        ("每股净资产", "yuan"),
    ],
)
def test_metric_units_are_inferred_from_the_field_name(field, expected):
    assert _unit_for_metric(field) == expected


def test_a_rate_containing_a_money_word_is_still_a_rate():
    """`净资产收益率` 同时含"率"与"资产"——判据必须"率"优先。

    只按"含金额词"判会把 ROE 标成 yuan；只按"含率"判则会漏掉金额项。
    所以两组词都要有，且百分比先判。
    """
    assert _unit_for_metric("净资产收益率") == "pct"
    assert _unit_for_metric("总资产报酬率") == "pct"
    # 反过来确认金额词那条仍然生效（否则上面两条可能只是"没匹配上"）
    assert _unit_for_metric("资产总计") == "yuan"


def test_an_unrecognized_metric_gets_no_unit_rather_than_a_guess():
    """认不出来就**留空**。

    留空是本仓库的既有原则：给错单位比不给更糟——它会让校验器的换算与数值
    判断悄悄走错分支。所以宁可不判，也不猜。
    """
    assert _unit_for_metric("某个没见过的指标") == ""


def test_inferred_units_are_in_the_vocabulary_the_verifier_uses():
    """推断出来的单位必须在 `NUMERIC_UNITS` 里，否则 `is_numeric` 是 False。

    这条把两个模块的词汇表**绑在一起**：`data.py` 若哪天改用一个新词
    （比如中文"元"），`is_numeric` 会静默变 False，而校验器的数值分支会直接
    `continue` 掉它——证据看着还在，实际已经退出了数值比对。
    """
    for field in ("毛利率", "营业总收入", "涨停家数"):
        unit = _unit_for_metric(field)
        assert unit in NUMERIC_UNITS, (
            f"{field} 推断出 {unit!r}，它不在 NUMERIC_UNITS 里 → is_numeric 会是 False"
        )


# ── 路由：问句必须落到有数据的那个工具 ────────────────────────────────


@pytest.mark.parametrize(
    "question,expected",
    [
        ("600519 的毛利率是多少", "get_fundamentals"),
        ("600519 的 ROE 是多少", "get_fundamentals"),
        ("600519 的 roa 是多少", "get_fundamentals"),
        ("600519 资产负债率是多少", "get_fundamentals"),
        ("600519 的销售净利率是多少", "get_fundamentals"),
        ("600519 每股收益多少", "get_fundamentals"),
    ],
)
def test_ratio_questions_route_to_the_tool_that_actually_has_them(question, expected):
    """比率问句必须去 `get_fundamentals`——实测只有它含这些字段。"""
    from tradingagents.agent.routing import plan_factual_tool

    assert plan_factual_tool(question, has_symbol=True) == expected


@pytest.mark.parametrize(
    "question,expected",
    [
        # 「报表」要的是报表，「比率」要的是指标——两者互不为子串，不能互相抢走
        ("600519 资产负债表怎么样", "get_balance_sheet"),
        ("600519 利润表怎么样", "get_income_statement"),
    ],
)
def test_statement_questions_still_route_to_statements(question, expected):
    """修比率路由不能顺手把"报表"问句也抢走。

    `资产负债率` 与 `资产负债表` 只差一个字，很容易在加关键词时把后者误伤。
    """
    from tradingagents.agent.routing import plan_factual_tool

    assert plan_factual_tool(question, has_symbol=True) == expected
