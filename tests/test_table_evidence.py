"""表格 → 证据解析器（Phase 2）：护栏 G1–G5 的逐条钉死。

## 为什么这个文件的否证测试是**核心交付物**、不是可选项

`_evidence_items_from_json` 的 docstring 原本写着「**表格不做解析**——从散文里抠
'标签=数字'极易错配，而错配的证据比没有证据糟得多（校验器的全部意义就是不让错的
东西出去）」。

那份顾虑是**对的**，而且它指出的风险有一个很坏的形状：**错配的证据会被校验器
接受**。因为数字确实出现在所引证据里，`unsourced_number` 不会触发——校验器拦不住
"把 A 指标的值安到 B 指标上"。所以这个失败模式比弃权更隐蔽，也更危险。

Phase 2 之所以敢推翻它，是因为实测这两类表是**严格 Markdown 表格**：

    get_fundamentals     22 行 × 10 列，列数不一致的行 **0**
    get_balance_sheet    14 行 × 18 列，列数不一致的行 **0**

有分隔行、列数逐行一致 ⇒ "错位"在机械上可防。**前提是护栏真的生效**，于是：

> 本文件里每一条"必须放弃"的测试，都是在证明护栏没被绕过。

具体地，G1（列数不一致就整张表放弃）是与"错配"正面相对的那一条——它一旦失效，
上面那个隐蔽的正确性漏洞就回来了。G2（`nan` 不是事实）防止把"没有这个数"变成
一个"0 元的数"，那会被模型当成真的读出来。
"""

from __future__ import annotations

import pytest

from tradingagents.tools.data import (
    _evidence_items_from_table,
    _table_blocks,
)

# ── 真实 vendor 文案（`get_balance_sheet(600519.SH, quarterly)` 实抓）────────
# 刻意保留真实形态：前导空格、`---------:` 分隔行、`nan` 单元格、`5.35188e+10`
# 科学计数法、首列报告期。用真文案做 fixture 才可能发现"真数据里才有的"陷阱。
REAL_BALANCE_SHEET = """\
|      报告日 |   流动资产 |        货币资金 |   结算备付金 |        拆出资金 |       交易性金融资产 |    买入返售金融资产 |   衍生金融资产 |
|---------:|-------:|------------:|--------:|------------:|--------------:|------------:|---------:|
| 20260630 |    nan | 5.35188e+10 |     nan | 1.41084e+11 | nan           | 4.06491e+08 |      nan |
| 20260331 |    nan | 4.87867e+10 |     nan | 1.55995e+11 | nan           | 4.0469e+08  |      nan |
"""

# 真实 `get_fundamentals` 形态：表头是报告期，第二列是指标名，第一列是分组。
REAL_FUNDAMENTALS = """\
| 选项   | 指标          |      20260630 |      20260331 |
|:-----|:------------|--------------:|--------------:|
| 常用指标 | 归母净利润       |   4.45169e+10 |   2.72425e+10 |
| 常用指标 | 营业总收入       |   9.22781e+10 |   5.47029e+10 |
| 常用指标 | 商誉          | nan           | nan           |
"""


def _fields(items) -> dict[str, float]:
    return {item.field: item.value for item in items}


# ══════════════════════════════════════════════════════════════════════════
# G1 —— 规整性：列数不一致 → **整张表放弃**
# ══════════════════════════════════════════════════════════════════════════


def test_g1_ragged_table_is_abandoned_entirely():
    """某一行少一列 → 整张表放弃（不是"跳过那一行"）。

    这是与"错配"正面相对的一条。如果只跳过坏行、继续解析其余行，那么一处列数
    不符就会让**后续所有行**的列与表头错位一格——于是 `货币资金` 的值被安到
    `流动资产` 头上，而校验器会**接受**它（数字确实在证据里）。
    """
    ragged = (
        "| 报告日 | 货币资金 | 拆出资金 |\n"
        "|---|---|---|\n"
        "| 20260630 | 5.35188e+10 | 1.41084e+11 |\n"
        "| 20260331 | 4.87867e+10 |\n"  # ← 少一列
    )
    assert _evidence_items_from_table(ragged, source_key="fundamental_data") == (), (
        "列数不一致时整张表必须放弃——部分解析会把值安到错的指标上，而校验器拦不住"
    )


def test_g1_table_without_separator_row_is_not_a_table():
    """没有分隔行就不是 Markdown 表格，不得当表格解析。"""
    no_sep = "| 报告日 | 货币资金 |\n| 20260630 | 5.35188e+10 |\n"
    assert _evidence_items_from_table(no_sep, source_key="fundamental_data") == ()


def test_g1_regular_table_is_accepted():
    """反向：规整表格必须被接受，否则护栏就成了"一律不解析"的借口。"""
    items = _evidence_items_from_table(REAL_BALANCE_SHEET, source_key="fundamental_data")
    assert items, "规整表格不产出证据说明护栏过严"
    assert len({item.id for item in items}) == len(items), "证据编号必须唯一"

    got = _fields(items)
    # 逐字对应：该行该列的值不得挪位
    assert got["货币资金（2026-06-30）"] == 5.35188e10
    assert got["拆出资金（2026-06-30）"] == 1.41084e11
    assert got["买入返售金融资产（2026-06-30）"] == 4.06491e08


def test_g1_label_column_does_not_shift_values_onto_wrong_metrics():
    """列错位会**静默**产生错误证据——钉死每个值都落在正确的指标名下。

    刻意让相邻两列的值相差极大：一旦错位一格，断言必然失败。
    """
    items = _evidence_items_from_table(REAL_BALANCE_SHEET, source_key="fundamental_data")
    got = _fields(items)
    for field, value in got.items():
        if field.startswith("货币资金"):
            assert value == 5.35188e10 or value == 4.87867e10, f"{field} 的值不像货币资金: {value}"
        if field.startswith("拆出资金"):
            assert value in (1.41084e11, 1.55995e11), f"{field} 的值不像拆出资金: {value}"
        if field.startswith("买入返售金融资产"):
            assert value in (4.06491e08, 4.0469e08), f"{field} 的值不像买入返售: {value}"


# ══════════════════════════════════════════════════════════════════════════
# G2 —— `nan` / 空单元格不是事实
# ══════════════════════════════════════════════════════════════════════════


def test_g2_nan_cells_produce_no_evidence():
    """`nan` 不是 0，也不是事实。

    真实资产负债表里 `流动资产` 一期是 `nan`、另一期有值。若把 `nan` 变成 0
    或让解析器抛异常，前者会让模型读出一个"0 元的流动资产"，后者会让整张表丢光。
    """
    items = _evidence_items_from_table(REAL_BALANCE_SHEET, source_key="fundamental_data")
    got = _fields(items)

    assert not any(f.startswith("流动资产") for f in got), (
        "`流动资产` 两期都是 nan，不该产出证据"
    )
    assert not any(f.startswith("衍 生") or f.startswith("衍生") for f in got)
    assert all(value == value for value in got.values()), "不得产出 NaN 值"  # NaN != NaN


@pytest.mark.parametrize("blank", ["", " ", "nan", "NaN", "null", "N/A", "-", "--"])
def test_g2_blank_and_placeholder_cells_are_never_facts(blank):
    """空单元格与各种占位符一律不产出证据（大小写不敏感）。"""
    table = (
        "| 报告日 | 指标A | 指标B |\n"
        "|---|---|---|\n"
        f"| 20260630 | {blank} | 1.5 |\n"
    )
    got = _fields(_evidence_items_from_table(table, source_key="x"))
    assert "指标A（2026-06-30）" not in got, f"{blank!r} 被当成了事实"
    assert got.get("指标B（2026-06-30）") == 1.5, "有值的单元格仍要产出"


# ══════════════════════════════════════════════════════════════════════════
# G3 —— 逐字搬运，不换算、不推断
# ══════════════════════════════════════════════════════════════════════════


def test_g3_values_are_copied_verbatim_including_scientific_notation():
    """科学计数法按原值搬运（不能自己换算成"亿"，那会引入我方的四舍五入）。"""
    items = _evidence_items_from_table(REAL_BALANCE_SHEET, source_key="fundamental_data")
    got = _fields(items)
    # 与文本里的字面量逐位一致
    assert got["货币资金（2026-06-30）"] == float("5.35188e+10")
    assert got["拆出资金（2026-03-31）"] == float("1.55995e+11")


def test_g3_thousands_separators_are_display_only():
    """千分位是显示格式，去掉它不改变数值语义。"""
    table = "| 报告日 | 营业总收入 |\n|---|---|\n| 20260630 | 92,278,100,000 |\n"
    got = _fields(_evidence_items_from_table(table, source_key="x"))
    assert got["营业总收入（2026-06-30）"] == 92278100000.0


# ══════════════════════════════════════════════════════════════════════════
# G4 —— `as_of` 取**报告期**，不是查询日
# ══════════════════════════════════════════════════════════════════════════


def test_g4_as_of_is_the_report_period_not_the_query_date():
    """证据时点必须是报告期——用查询日会让"这是哪期的数"无从回答，也会骗过日期校验。

    查询日是 2026-09-22，而最新报告期是 2026-06-30：两者必须不同，否则这条测试
    什么也没证明。
    """
    items = _evidence_items_from_table(
        REAL_BALANCE_SHEET, source_key="fundamental_data", as_of="2026-09-22"
    )
    assert {item.as_of for item in items} == {"2026-06-30", "2026-03-31"}, (
        f"as_of 应是报告期，实得 {sorted({i.as_of for i in items})}"
    )
    assert "2026-09-22" not in {item.as_of for item in items}, "不得用查询日充当数据时点"


def test_g4_a_row_with_an_unparseable_period_makes_the_whole_table_not_row_oriented():
    """首列必须**每一行**都是能直接落地的报告期，否则整张表不按"首列是报告期"处理。

    这是刻意选的**严格**口径。放宽成"跳过坏行、保留好行"看似多留了一条证据，
    但它建立在"首列就是报告期轴"这个假设上——而首列只要有一行不是报告期，
    这个假设就不再可靠，此时按首列切分就是在赌。赌错的代价是错位证据，
    而错位证据**校验器拦不住**（见本文件顶部说明）。

    实际损失为零：真实 `get_balance_sheet` / `get_income_statement` 的首列
    **每一行**都是 `20260630` 这种形态，严格口径不会少拿一条证据。
    """
    mixed = (
        "| 报告日 | 货币资金 |\n"
        "|---|---|\n"
        "| 2026-Q2 | 5.35188e+10 |\n"  # 季度写法：需要推断月末，故不认
        "| 20260331 | 4.87867e+10 |\n"
    )
    assert _evidence_items_from_table(mixed, source_key="x") == (), (
        "首列混入非报告期时，整张表不该被当作朝向 A 解析"
    )

    # 反向：全部行都是合法报告期时，必须正常产出
    clean = (
        "| 报告日 | 货币资金 |\n"
        "|---|---|\n"
        "| 20260630 | 5.35188e+10 |\n"
        "| 20260331 | 4.87867e+10 |\n"
    )
    items = _evidence_items_from_table(clean, source_key="x")
    assert {item.as_of for item in items} == {"2026-06-30", "2026-03-31"}


def test_g4_both_orientations_derive_as_of_from_their_own_period_axis():
    """两种朝向的报告期来源不同：一种在首列，一种在表头。都必须取对。"""
    row_oriented = _evidence_items_from_table(REAL_BALANCE_SHEET, source_key="x")
    col_oriented = _evidence_items_from_table(REAL_FUNDAMENTALS, source_key="x")

    assert {i.as_of for i in row_oriented} == {"2026-06-30", "2026-03-31"}
    assert {i.as_of for i in col_oriented} == {"2026-06-30", "2026-03-31"}


# ══════════════════════════════════════════════════════════════════════════
# 朝向识别（两种真实形态）
# ══════════════════════════════════════════════════════════════════════════


def test_column_oriented_table_uses_metric_column_not_group_column():
    """表头是报告期时，指标名取 `指标` 列，**不能**取 `选项` 分组列。

    `get_fundamentals` 的第一列是 `常用指标`（分组），第二列才是 `归母净利润`
    （指标）。取错列会产出三条**同名**证据「常用指标（2026-06-30）」——
    模型引用时无法区分是净利润还是营业收入。
    """
    got = _fields(_evidence_items_from_table(REAL_FUNDAMENTALS, source_key="x"))

    assert "归母净利润（2026-06-30）" in got
    assert "营业总收入（2026-06-30）" in got
    assert not any("常用指标" in field for field in got), (
        "指标名取到了分组列，同名证据会让引用失去意义"
    )
    assert got["归母净利润（2026-06-30）"] == float("4.45169e+10")
    # `商誉` 两期都是 nan → 不产出
    assert not any(f.startswith("商誉") for f in got)


# ══════════════════════════════════════════════════════════════════════════
# G5 —— 条数上限与"期优先"的截断位置
# ══════════════════════════════════════════════════════════════════════════


def test_g5_evidence_count_is_capped():
    """条数必须有硬上限（提示词容量与校验成本）。"""
    from tradingagents.tools.data import _MAX_EVIDENCE_ITEMS

    header = "| 报告日 | " + " | ".join(f"指标{i}" for i in range(60)) + " |"
    sep = "|---" * 61 + "|"
    body = "| 20260630 | " + " | ".join("1.5" for _ in range(60)) + " |"
    items = _evidence_items_from_table(
        "\n".join([header, sep, body]), source_key="x"
    )
    assert len(items) == _MAX_EVIDENCE_ITEMS


def test_g5_newest_period_is_filled_first():
    """**期优先**：最新一期铺满所有指标，截断发生在最老的期限上。

    若按"指标优先"，`get_fundamentals` 的前 5 个指标 × 8 期就吃光 40 条额度，
    用户问「营业成本多少」时那个指标**根本没有证据**——明明表里有。
    """
    from tradingagents.tools.data import _MAX_EVIDENCE_ITEMS

    periods = ["20260630", "20260331", "20251231", "20250930"]
    header = "| 报告日 | " + " | ".join(f"指标{i}" for i in range(20)) + " |"
    sep = "|---" * 21 + "|"
    rows = [
        f"| {p} | " + " | ".join("1.5" for _ in range(20)) + " |" for p in periods
    ]
    items = _evidence_items_from_table(
        "\n".join([header, sep, *rows]), source_key="x"
    )
    assert len(items) == _MAX_EVIDENCE_ITEMS

    newest = [i for i in items if i.as_of == "2026-06-30"]
    assert len(newest) == 20, "最新一期必须铺满全部指标（期优先）"
    assert items[0].as_of == "2026-06-30", "最新一期应排在最前"


# ══════════════════════════════════════════════════════════════════════════
# 非表格输入：必须保持空证据（诚实优先那条原则仍在）
# ══════════════════════════════════════════════════════════════════════════


def test_prose_and_markdown_headers_never_produce_evidence():
    """散文、标题、列表一律不产出证据——"从散文里抠标签=数字"仍是被拒绝的做法。"""
    for prose in (
        "## Fundamentals for 600519.SH\n\n最新披露期: 2026-Q2\n",
        "板块资金流向排名（数据截止 2026-09-22，共48个板块，前10名）：\n 排名   名称\n",
        "2026-09-21 涨停家数：103\n连板分布：\n连板数\n1    82\n",
        "Error fetching global news: Too Many Requests. Rate limited.\n",
        "",
    ):
        assert _evidence_items_from_table(prose, source_key="x") == (), (
            f"散文不该产出证据：{prose[:30]!r}"
        )


def test_table_blocks_rejects_ragged_and_separatorless_tables():
    """`_table_blocks` 是 G1 的实现处，直接钉它的两个否定条件。"""
    assert _table_blocks("| a | b |\n| 1 | 2 |\n") == [], "无分隔行"
    assert _table_blocks("| a | b |\n|---|---|\n| 1 | 2 | 3 |\n") == [], "列数不一致"
    ok = _table_blocks("| a | b |\n|---|---|\n| 1 | 2 |\n")
    assert len(ok) == 1, "规整表格应被接受"
    assert ok[0] == [["a", "b"], ["---", "---"], ["1", "2"]]
