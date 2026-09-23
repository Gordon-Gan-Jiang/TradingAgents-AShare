"""L2 数据工具必须产出**可引用的证据**。

## 为什么这是最后一环

事实型问句（"600519 现在多少钱"）此前即使取到数也答不出来：19 个数据工具返回
vendor 预格式化字符串、`evidence=()`，零 LLM 校验器于是正确地拒绝任何具体数字，
回答只能退化为"本轮无可用证据"。实测 trace：

    get_realtime_quotes  ok=True  evidence_count=0  → "无可用证据（price/… 未覆盖）"

而 vendor 的输出里**本来就有结构化 JSON**：

    {"600519.SH": {"price": 1252.57, "change_pct": -0.3619, "quote_time": "…", "source": "sina"}}

本文件锁住三件事：

1. **JSON 型输出必须转成证据**（一个数值一条），且数值保持 float —— 校验器按
   数值比对，字符串"约1252元"是没法被引用的。
2. **表格/散文型输出不硬编证据**：宁可为空（诚实），也不要把标签与数字错配。
   错误证据比没有证据糟得多——校验器的全部意义就是不让错的东西出去。
3. **失败文案不产出证据**：vendor 说"数据暂不可用"时，不能因为里面有个数字就
   生成证据。
"""

from __future__ import annotations

import json


def _tool(name: str, *, monkeypatch, rendered: str):
    """构造一个把 vendor 输出固定为 `rendered` 的数据工具。"""
    from tradingagents.tools import data as data_mod

    monkeypatch.setattr(
        data_mod, "data_tool_impl", lambda _name: (lambda **kw: rendered)
    )
    specs = {spec.name: spec for spec in data_mod.make_data_tools()}
    return specs[name]


REALTIME_JSON = json.dumps(
    {
        "600519.SH": {
            "price": 1252.57,
            "open": 1259.0,
            "high": 1259.95,
            "low": 1250.8,
            "previous_close": 1257.12,
            "change": -4.55,
            "change_pct": -0.3619,
            "volume": 2501689.0,
            "amount": 3135910045.0,
            "quote_time": "2026-09-21 15:34:59",
            "source": "sina",
        }
    },
    ensure_ascii=False,
)


def test_realtime_quote_emits_numeric_evidence(monkeypatch):
    spec = _tool("get_realtime_quotes", monkeypatch=monkeypatch, rendered=REALTIME_JSON)
    result = spec.handler(symbols=["600519.SH"])

    assert result.evidence, "报价 JSON 必须变成可引用证据，否则回答只能答'无可用证据'"
    values = {item.field: item.value for item in result.evidence}
    assert any(v == 1252.57 for v in values.values()), f"现价要进证据：{values}"
    # 数值必须是数字，不能是字符串——校验器按量级比对
    numeric = [item for item in result.evidence if isinstance(item.value, (int, float))]
    assert len(numeric) >= 5, f"数值字段应逐条进证据，实际 {len(numeric)} 条"
    assert all(item.as_of for item in result.evidence), "证据必须有基准日"


def test_quote_time_and_source_are_evidence_so_freshness_is_citable(monkeypatch):
    """数据时点与来源必须可引用：用户要能知道这个价格是什么时候的。"""
    spec = _tool("get_realtime_quotes", monkeypatch=monkeypatch, rendered=REALTIME_JSON)
    result = spec.handler(symbols=["600519.SH"])

    joined = " ".join(f"{item.field}={item.value}" for item in result.evidence)
    assert "2026-09-21 15:34:59" in joined, "数据时点必须进证据"
    assert "sina" in joined, "数据来源必须进证据"


def test_percent_fields_use_percent_unit(monkeypatch):
    """涨跌幅这类字段的 unit 必须是 pct——否则比率/百分比换算会被校验器判错。"""
    spec = _tool("get_realtime_quotes", monkeypatch=monkeypatch, rendered=REALTIME_JSON)
    result = spec.handler(symbols=["600519.SH"])

    # 证据字段用**中文标签**（给模型和用户读），因此按标签与单位断言
    pct = [item for item in result.evidence if item.unit == "pct"]
    assert pct, f"涨跌幅这类字段的 unit 应为百分比，实际 {[i.unit for i in result.evidence]}"
    assert any("涨跌幅" in item.field for item in pct), "涨跌幅应在证据里"


def test_evidence_ids_are_unique_and_sequential(monkeypatch):
    spec = _tool("get_realtime_quotes", monkeypatch=monkeypatch, rendered=REALTIME_JSON)
    result = spec.handler(symbols=["600519.SH"])

    ids = [item.id for item in result.evidence]
    assert len(ids) == len(set(ids)), "证据编号必须唯一，否则引用会指向两条事实"
    assert all(i.startswith("E") for i in ids)


def test_well_formed_table_now_yields_evidence_by_verbatim_transfer(monkeypatch):
    """**规整**的 Markdown 表格现在产出证据——这是 Phase 2 推翻的旧决定。

    原文（`data.py` 的 `_evidence_items_from_json` docstring）是「表格不做解析」，
    理由是"从**散文**里抠'标签=数字'极易错配，而错配的证据比没有证据糟得多"。

    Phase 2 把适用范围收窄为：**有分隔行且所有行列表数一致的 Markdown 表格**
    可以解析（护栏 G1–G5，任一条不满足就整张表放弃）。这份 fixture 恰好就是
    规整表格（首列是日期、含分隔行、三列逐行一致），所以产出证据是**正确**行为：
    `开盘=9.60` 与该行该列逐字一致，没有错位。

    错位才是要防的事，所以本测试只钉"搬运是否忠实"，护栏的否证测试在下面。
    """
    table = "| 日期 | 开盘 | 收盘 |\n|---|---|---|\n| 2026-09-19 | 9.60 | 9.80 |\n"
    spec = _tool("get_fundamentals", monkeypatch=monkeypatch, rendered=table)
    result = spec.handler(ticker="600519.SH", curr_date="2026-09-19")

    assert result.data.get("rendered") == table, "原始文本仍要保留（分析图依赖逐字一致）"
    got = {item.field: item.value for item in result.evidence}
    assert got == {"开盘（2026-09-19）": 9.6, "收盘（2026-09-19）": 9.8}, (
        f"表格搬运必须逐字对应，实得 {got}"
    )
    # G4：时点取行里的日期，不是查询日
    assert {item.as_of for item in result.evidence} == {"2026-09-19"}


def test_vendor_failure_text_yields_no_evidence(monkeypatch):
    """失败文案里即使有数字也不能变成证据。"""
    spec = _tool("get_zt_pool", monkeypatch=monkeypatch, rendered="2026-09-19 涨停板情绪池数据暂不可用。")
    result = spec.handler(date="2026-09-19")

    assert result.evidence == ()
    assert result.degraded is True, "vendor 明确说不可用时应标 degraded"


def test_json_rows_become_one_evidence_per_row(monkeypatch):
    """行式 JSON（榜单类）每行一条证据，便于逐一引用。"""
    rows = json.dumps(
        [
            {"代码": "002169.SZ", "名称": "智光电气", "涨跌幅": 9.16, "换手率": 3.2},
            {"代码": "300476.SZ", "名称": "胜宏科技", "涨跌幅": 9.23, "换手率": 4.1},
        ],
        ensure_ascii=False,
    )
    spec = _tool("get_zt_pool", monkeypatch=monkeypatch, rendered=rows)
    result = spec.handler(date="2026-09-19")

    assert result.evidence, "行式 JSON 应产出证据"
    joined = " ".join(str(item.value) for item in result.evidence)
    assert "002169.SZ" in joined and "300476.SZ" in joined


def test_broken_json_falls_back_to_no_evidence(monkeypatch):
    spec = _tool("get_realtime_quotes", monkeypatch=monkeypatch, rendered='{"600519.SH": {"price": 12')
    result = spec.handler(symbols=["600519.SH"])
    assert result.evidence == ()
    assert result.data.get("rendered"), "原始文本仍要保留"


def test_evidence_count_is_bounded(monkeypatch):
    """证据条数要有上限：一次榜单几十行会把提示词撑爆。"""
    rows = json.dumps(
        [{"代码": f"{600000 + i}", "涨跌幅": 1.0 * i} for i in range(200)], ensure_ascii=False
    )
    spec = _tool("get_zt_pool", monkeypatch=monkeypatch, rendered=rows)
    result = spec.handler(date="2026-09-19")
    assert len(result.evidence) <= 40, f"证据条数应受限，实际 {len(result.evidence)}"


# ══════════════════════════════════════════════════════════════════════════
# 「拿不到」不得被表述成「没有」——包括限流与代码自己的错误前缀
# ══════════════════════════════════════════════════════════════════════════


def test_rate_limited_vendor_text_is_not_treated_as_data():
    """实测（2026-09-22）`get_global_news` 的真实返回：

        'Error fetching global news: Too Many Requests. Rate limited. Try after a while.'

    上面这段既不是新闻、也不是"没有新闻"，而是**限流**。修复前它一个标记都不命中，
    于是工具把它当有效数据交给模型、同时声明「该源可用」——比缺数据更糟：
    模型可能把这段英文错误文本当成新闻内容来解读。
    """
    from tradingagents.tools.data import _looks_like_vendor_failure

    assert _looks_like_vendor_failure(
        "Error fetching global news: Too Many Requests. Rate limited. Try after a while."
    )
    assert _looks_like_vendor_failure("Error fetching news for AAPL: 429 Client Error")
    assert _looks_like_vendor_failure("")


def test_ordinary_market_text_is_still_data():
    """标记不得宽到把正常返回误判成失败——误判会把有数据的工具变成"白跑"。

    这类 bug 的方向性代价不对称：漏识别只是"少报一次失败"，
    误识别则让**能用的数据被丢掉**，并把一次成功查询表述成数据源故障。
    """
    from tradingagents.tools.data import _looks_like_vendor_failure

    for text in (
        "贵州茅台(600519) 2026-09-22 收盘 1253.8 元，涨跌幅 0.35%",
        "1. 002475.SZ 立讯精密 加入时间 2026-01-05",
        "公司公告：拟以自有资金回购股份，金额不超过 5 亿元。",
        "板块资金流：半导体净流入 12.3 亿元，位居第一",
        "数据日期 2026-09-22｜金额单位 元（负数表示净流出）",
        "【研报】算力需求旺盛，光模块厂商订单饱满。",
    ):
        assert not _looks_like_vendor_failure(text), f"正常数据被误判为失败：{text!r}"


# ══════════════════════════════════════════════════════════════════════════
# 「结构合法但内容为空」的返回同样是「拿不到」
# ══════════════════════════════════════════════════════════════════════════
#
# `_looks_like_vendor_failure` 早就挡住了空字符串（注释写着"空返回同样是拿不到"），
# 但**空 JSON** 穿了过去：`'{}'` 是长度 2 的非空字符串。
#
# 实测 `get_realtime_quotes(symbols='600519.SH')` 就返回 `'{}'`，于是：
#     degraded=False                    （不是失败）
#     available=('core_stock_apis',)    （声称该源**可用**）
#     unavailable=()                    （声称**没有**未覆盖项）
#     证据 0 条
# 用户看到"数据源可用、没有未覆盖项、但没有数据"。600519.SH 是有效代码，
# `{}` 只可能意味着"这次没取到"。


def test_empty_json_payload_is_a_failure_not_data():
    """空 dict / 空 list 必须判为失败——否则会声称数据源可用。"""
    from tradingagents.tools.data import _looks_like_vendor_failure

    for rendered in ("{}", "[]", "  {}  ", "{ }", "[]\n", "{} "):
        assert _looks_like_vendor_failure(rendered) is True, f"{rendered!r} 是空载荷，不能当数据"


def test_non_empty_json_is_still_data():
    """判据必须收窄：**非空** JSON 不是失败（不能因为"是 JSON"就一律拒）。"""
    from tradingagents.tools.data import _looks_like_vendor_failure

    for rendered in ('{"quotes":{}}', '[1]', '{"a":1}', '{"items":[]}'):
        assert _looks_like_vendor_failure(rendered) is False, f"{rendered!r} 有内容，不该判失败"


def test_empty_payload_does_not_claim_the_source_is_available():
    """空载荷下 coverage 必须说"该源取不到"，而不是"可用且没有未覆盖项"。"""
    from tradingagents.tools.data import DATA_TOOL_DECLS, _coverage_for

    decl = next(d for d in DATA_TOOL_DECLS if d.name == "get_realtime_quotes")
    coverage = _coverage_for(decl, rendered="{}", has_evidence=False)

    assert coverage.available == (), "空载荷不得声称数据源可用"
    # `unavailable` 的元素是"类别名（说明）"整串，所以这里要查**子串**，
    # 不能用 tuple 的 `in`（那是精确匹配，会永远为假）。
    assert any(decl.category in item for item in coverage.unavailable), (
        "必须点名未覆盖的是哪个源"
    )


# ══════════════════════════════════════════════════════════════════════════
# Phase 1.3：数据源故障必须让**用户**看见，而不是只丢一个内部类别名
# ══════════════════════════════════════════════════════════════════════════
#
# 原本失败时只声明 `(decl.category,)`，用户看到的是「未覆盖：news_data」——
# 一个内部标识符。真正的诊断（"被限流了"）写在 `ToolResult.degraded_reason` 里，
# 而**全仓库没有任何代码读它**，算完就丢。
#
# 后果不是"少一条信息"，而是：真实的数据源故障被表述成"没有数据"，
# 用户无法判断该刷新重试还是该换个问法。这与 `_coverage_for` 自己的注释
# （"vendor 失败时不得声称该源可用"）是同一条原则。


def test_vendor_failure_declares_the_diagnosis_not_just_the_category():
    """故障声明里必须带人话诊断（沿用 `trade_ledger（没有已录入的交易流水）` 范式）。"""
    from tradingagents.tools.data import DATA_TOOL_DECLS, _coverage_for

    decl = next(d for d in DATA_TOOL_DECLS if d.name == "get_global_news")
    rendered = "Error fetching global news: Too Many Requests. Rate limited. Try after a while."
    coverage = _coverage_for(decl, rendered=rendered, has_evidence=False)

    assert coverage.available == (), "故障时不得声称源可用"
    declared = coverage.unavailable[0]
    assert decl.category in declared, "类别名要保留在前（既有断言用它匹配）"
    assert "数据源故障" in declared, "必须明说是数据源故障"
    assert "Rate limited" in declared, "必须带上真实诊断，否则用户分不清故障与无数据"


def test_vendor_failure_note_says_empty_payload_in_plain_words():
    """`{}` 不是一句诊断，不能原样摆给用户看。"""
    from tradingagents.tools.data import _vendor_failure_note

    assert _vendor_failure_note("{}") == "返回空数据"
    assert _vendor_failure_note("[]") == "返回空数据"
    # 真错误文案要原样透出（它是唯一能解释原因的线索）
    real = "板块涨幅榜获取失败：ConnectionError: (39, 39)"
    assert _vendor_failure_note(real) == real


def test_healthy_data_declares_no_fault():
    """反向：正常数据不得被安上"数据源故障"。"""
    from tradingagents.tools.data import DATA_TOOL_DECLS, _coverage_for

    decl = next(d for d in DATA_TOOL_DECLS if d.name == "get_zt_pool")
    coverage = _coverage_for(
        decl,
        rendered="2026-09-21 涨停家数：103\n连板分布：\n1    82\n",
        has_evidence=True,
    )

    assert coverage.available == (decl.category,)
    assert coverage.unavailable == (), "正常数据不得声明故障"


# ══════════════════════════════════════════════════════════════════════════
# 「拿到了、但没有可引用数值」也必须声明——静默空的主生成点
# ══════════════════════════════════════════════════════════════════════════
#
# 前两节处理的是"**没拿到**"（空载荷、故障文案）。这一节处理第三种情况，
# 也是生产里最常见的一种：
#
#     vendor 正常返回了一段散文 → 不是失败 → 但也抽不出可引用数值
#
# 而 `_coverage_for` 此前**只看"是不是失败文案"**，对"有没有产出证据"完全无感，
# 于是这一类返回被表述成：
#
#     available=('fundamental_data',)   ← 通路健康，这一条**是对的**
#     unavailable=()                    ← 问题在这里：什么都不说
#     evidence=()                       ← 却一条证据都没有
#
# 用户看到的是一个**没有任何解释的空结果**，而平台其实收到了数据、只是那些数据
# 无法被引用。这与上一节 `{}` 的 bug 是同一条原则（"拿不到 ≠ 没有"），只是更难
# 发现：载荷非空，所以连"空载荷"那道闸门都穿不过去。
#
# **注意 `available` 不是要改的那一项**：它的语义是"取数通路健康"，不是"产出了
# 可引用数据"。我最初把这里误判成"声称该源可用"是错的，还据此想把
# `available ∩ unavailable = ∅` 立成不变量——那会抹掉本仓库刻意保留的
# 「你没录入」与「数据源坏了」之分
# （见 `test_trade_ledger_no_trades_keeps_the_source_available` 的 docstring）。
# 真正缺的只有 `unavailable` 那一句话。
#
# 实测（trace 库 753 个回合，2026-09-21～23）这些工具在**成功**返回下的零证据
# 占比：get_global_news 18/23、get_zt_pool 15/22、get_balance_sheet 11/17、
# get_income_statement 11/17、get_board_fund_flow 11/18、get_fundamentals 11/39。
#
# 为什么是"声明"而不是"硬编证据"：本文件开篇第 2 条原则写着"表格/散文型输出不
# 硬编证据：宁可为空（诚实），也不要把标签与数字错配"。这条原则不变——变的是
# **那份诚实的空必须可见**，而不是留下一片沉默。

PROSE_RETURN = (
    "贵州茅台（600519）今日小幅震荡，成交量较前一交易日略有萎缩，"
    "市场情绪整体偏谨慎，机构观点分歧有所加大。"
)


def test_prose_return_is_not_a_failure_but_must_be_declared(monkeypatch):
    """散文不是失败，但"不可引用"必须说出来。"""
    from tradingagents.tools.data import _looks_like_vendor_failure

    assert not _looks_like_vendor_failure(PROSE_RETURN), (
        "散文是正常返回，判成失败会让能用的文本被丢掉"
    )

    tool = _tool("get_fundamentals", monkeypatch=monkeypatch, rendered=PROSE_RETURN)
    result = tool.handler(ticker="600519.SH", curr_date="2026-01-16")

    assert result.evidence == (), "散文不得被硬编成证据（原则 2 不变）"
    assert not result.degraded, "这不是数据源故障，不得标 degraded"
    assert result.coverage.unavailable, (
        "拿到了数据却一条都不可引用，必须声明；否则这个空结果没有任何解释"
    )
    declared = " ".join(result.coverage.unavailable)
    assert "fundamental_data" in declared, "要点名是哪个源"
    assert "散文" in declared or "可引用" in declared, (
        f"说明要说清是「没有可引用数值」，而不是含糊的不可用：{declared!r}"
    )


def test_prose_declaration_keeps_the_source_available():
    """散文不是故障：**通路健康**这一点要保住，声明的是"这个维度不可引用"。

    `available` 的语义是"取数通路健康"，不是"产出了可引用数据"——这条语义在本
    仓库是刻意的，且被 9 条测试守着（`test_trade_ledger_no_trades_keeps_the_source_available`
    的 docstring 说得最清楚：「把'没有记录'讲成'数据源故障'会让用户以为平台坏了」）。
    所以"散文"这一格保留 `available`，只把该说的那句话写进 `unavailable`；
    同一个源同时出现在两个列表里因此不是矛盾：前者说通路，后者说数据。

    我最初把这里写成 `available=()`，那会让"源答了但内容不可引用"与"源根本取不到"
    在 coverage 上变得一模一样——正是这条语义要避免的。
    """
    from tradingagents.tools.data import DATA_TOOL_DECLS, _coverage_for

    decl = next(d for d in DATA_TOOL_DECLS if d.name == "get_global_news")
    coverage = _coverage_for(decl, rendered=PROSE_RETURN, has_evidence=False)

    assert coverage.available == (decl.category,), "散文不是故障，通路是健康的"
    assert coverage.unavailable, "但必须声明这个维度不可引用，否则是静默空"
    declared = coverage.unavailable[0]
    assert decl.category in declared, "要点名是哪个源"
    assert "可引用" in declared, "要说清是「没有可引用数值」，不能与数据源故障混为一谈"
    assert "故障" not in declared, "这不是故障，措辞不得误导用户去重试"


def test_prose_and_failure_are_distinguishable_from_coverage_alone():
    """反向：两种情况的 coverage 必须**能分辨**（这正是 `available` 的用处）。"""
    from tradingagents.tools.data import DATA_TOOL_DECLS, _coverage_for

    decl = next(d for d in DATA_TOOL_DECLS if d.name == "get_global_news")
    prose = _coverage_for(decl, rendered=PROSE_RETURN, has_evidence=False)
    failure = _coverage_for(
        decl,
        rendered="Error fetching global news: Too Many Requests. Rate limited.",
        has_evidence=False,
    )

    assert prose.available and not failure.available, "源答没答，靠 available 分辨"
    assert "故障" in failure.unavailable[0]
    assert "故障" not in prose.unavailable[0]
