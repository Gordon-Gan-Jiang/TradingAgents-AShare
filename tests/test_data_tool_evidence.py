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


def test_table_text_output_does_not_invent_evidence(monkeypatch):
    """表格/散文型输出**不**硬编证据：错配的标签-数字比没有证据更糟。

    样本从 `get_stock_data` 换成 `get_fundamentals`（在册的散文型工具，同样带
    日期参数）；判据不变——非结构化文本不得凭空生成证据。
    """
    table = "| 日期 | 开盘 | 收盘 |\n|---|---|---|\n| 2026-09-19 | 9.60 | 9.80 |\n"
    spec = _tool("get_fundamentals", monkeypatch=monkeypatch, rendered=table)
    result = spec.handler(ticker="600519.SH", curr_date="2026-09-19")

    assert result.evidence == (), "非结构化文本不得凭空生成证据"
    assert result.data.get("rendered") == table, "原始文本仍要保留（分析图依赖逐字一致）"


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
