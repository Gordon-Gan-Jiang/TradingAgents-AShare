"""L2 证据工具的数据源补齐：**个股公告** 与 **指数行情**（全离线）。

## 这个文件锁住什么

L2 证据层此前有两个洞：`announcements_read` 有工具、有端口，但**没有任何取数
实现**（组合根刻意不传 `announcements_port`，于是它不在册）；指数行情连工具都
没有。根因不是工具写不出来，而是**没有数据源**——本文件锁住的是「数据源补上
之后」的两条链路：

    IO 取数（可回退、失败返回 None）→ 纯函数归一 → 帧 → 工具 → 可引用证据

1. **多源回退且可用源优先**：公告走东财（带公告类型）→ 巨潮；指数走新浪直连
   （带快照时间戳）→ 新浪 akshare → 东财 akshare。任一源抛错都只是「换下一个」。
2. **取不到返回 `None`，不返回空帧**：空帧会让上层把「没拉到」读成「没有公告」
   /「指数没动」，那是本平台最不能接受的失败形态。
3. **归一后列名是内部契约**：`published_at/type/title/source/url` 与
   `code/name/price/change_pct/...`；上游列名（`announcementTitle`、`涨跌幅`…）
   不得泄漏到工具层。
4. **证据可引用且单位正确**：公告每条证据 = 披露日期｜类型｜标题｜来源，披露日期
   进 `as_of`；指数每个标的给 float 点位与 `pct` 涨跌幅。
5. **未注入不注册 / 注入后在册**：`index_fetcher` 不注入时工具不在注册表里。

## 不联网

所有 IO 层（`_*_payload` / `_*_frame` / `_*_text`）在测试里被替换成假函数；
纯函数用**实测抓下来的真实载荷**喂进去，离线断言归一结果。
"""

from __future__ import annotations

import pytest

from tradingagents.dataflows.providers import cn_announcement_index_data as cna
from tradingagents.tools.evidence import (
    UNAVAILABLE_ANNOUNCEMENTS,
    UNAVAILABLE_INDEX,
    make_evidence_tools,
)
from tradingagents.tools.spec import PLAN_LAYER_EVIDENCE

# ══ 实测载荷（抓自真实接口，见交付说明的实测表格） ══════════════════════════


# 东财 np-anotice-stock/api/security/ann 的单条（原样）
EM_ANNOUNCEMENT_ITEM = {
    "art_code": "AN202608141827994407",
    "codes": [
        {
            "ann_type": "A,SHA",
            "inner_code": "46077278941243",
            "market_code": "1",
            "short_name": "贵州茅台",
            "stock_code": "600519",
        }
    ],
    "columns": [{"column_code": "001002008", "column_name": "其他"}],
    "display_time": "2026-08-14 20:41:29:380",
    "notice_date": "2026-08-15 00:00:00",
    "title": "贵州茅台:贵州茅台关于召开2026年半年度业绩说明会的公告",
}

# 巨潮 hisAnnouncement/query 的单条（原样）
CNINFO_ANNOUNCEMENT_ITEM = {
    "secCode": "600519",
    "secName": "贵州茅台",
    "orgId": "gssh0600519",
    "announcementId": "1225475868",
    "announcementTitle": "贵州茅台2026年半年度报告",
    "announcementTime": 1786723200000,
    "adjunctUrl": "finalpage/2026-08-15/1225475868.PDF",
    "adjunctType": "PDF",
    "announcementTypeName": None,
}

# 新浪 hq.sinajs.cn/list=... 的**原文**（实测抓取，含一个查不到的代码）
SINA_HQ_TEXT = (
    'var hq_str_sh000001="上证指数,3963.8073,3949.9068,3964.0279,3966.3445,'
    "3955.6341,0,0,169996065,348387201347,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,"
    '2026-09-22,09:58:14,00,";\n'
    'var hq_str_sh999999="";\n'
    'var hq_str_sz399001="深证成指,13858.620,13730.021,13893.823,13914.206,'
    "13850.520,0.000,0.000,22469866688,393259385551.813,0,0.000,0,0.000,0,0.000,0,0.000,"
    "0,0.000,0,0.000,0,0.000,0,0.000,0,0.000,0,0.000,"
    '2026-09-22,09:58:15,00";\n'
)

# 新浪 stock_zh_index_spot_sina 的列与两行（实测）
_SINA_SPOT_COLUMNS = [
    "代码",
    "名称",
    "最新价",
    "涨跌额",
    "涨跌幅",
    "昨收",
    "今开",
    "最高",
    "最低",
    "成交量",
    "成交额",
]
SINA_SPOT_ROWS = [
    ["sh000001", "上证指数", 3962.2666, 12.360, 0.313, 3949.9068, 3963.8073, 3964.6326, 3955.6341, 135668810, 278220767809],
    ["sz399006", "创业板指", 3459.4560, 59.866, 1.761, 3399.5900, 3446.3290, 3472.4710, 3440.8980, 5031644905, 149914332473],
]

# 东财 stock_zh_index_spot_em("沪深重要指数") 的列与两行（实测）
_EM_SPOT_COLUMNS = [
    "序号",
    "代码",
    "名称",
    "最新价",
    "涨跌幅",
    "涨跌额",
    "成交量",
    "成交额",
    "振幅",
    "最高",
    "最低",
    "今开",
    "昨收",
    "量比",
]
EM_SPOT_ROWS = [
    [1, "000001", "上证指数", 3963.55, 0.35, 13.64, 171833903, 3.525087e11, 0.27, 3966.34, 3955.63, 3963.81, 3949.91, 3.04],
    [4, "399006", "创业板指", 3461.22, 1.81, 61.63, 66144993, 1.899674e11, 0.93, 3472.47, 3440.90, 3446.33, 3399.59, 3.42],
]


def _frame(columns, rows):
    pd = pytest.importorskip("pandas")
    return pd.DataFrame(rows, columns=columns)


# ══ IO 层替换：默认**全部源不可用**，每个用例只打开它关心的那条 ═════════════

_ANNOUNCEMENT_SEAMS = ("_announcement_payload_via_em", "_announcement_payload_via_cninfo")
_INDEX_SEAMS = ("_index_text_via_sina_hq", "_index_frame_via_sina_akshare", "_index_frame_via_em_akshare")


@pytest.fixture(autouse=True)
def _all_sources_off(monkeypatch):
    """默认所有低层源都「拿不到」。

    这样每个用例只需声明它关心的那条源，回退顺序的断言就不会被别的源掩盖。
    """
    for name in _ANNOUNCEMENT_SEAMS + _INDEX_SEAMS:
        monkeypatch.setattr(cna, name, lambda *a, **kw: None)
    yield


def _em_payload(items):
    return {"data": {"total_hits": len(items), "list": list(items)}}


def _cninfo_payload(items):
    return {"totalAnnouncement": len(items), "announcements": list(items)}


# ══ 1. 公告：纯函数归一 ═══════════════════════════════════════════════════


def test_em_announcements_normalize_into_internal_contract():
    rows = cna.announcements_from_em(_em_payload([EM_ANNOUNCEMENT_ITEM]))

    assert len(rows) == 1
    row = rows[0]
    assert row["published_at"] == "2026-08-15"
    assert row["title"] == "贵州茅台关于召开2026年半年度业绩说明会的公告"  # 简称前缀被剥掉
    assert row["type"] == "其他"
    assert row["url"].endswith("AN202608141827994407.html")
    assert "source" not in row  # 来源由 fetcher 按实际命中的源贴上，归一函数不猜


def test_cninfo_announcements_convert_epoch_ms_to_shanghai_date():
    """巨潮给的是**毫秒时间戳**，且北京时间 00:00 = UTC 前一天 16:00。

    按 UTC 直接取日期会整批差一天——公告日期是回答里最常被引用的断言，
    差一天就是编造日期。
    """
    rows = cna.announcements_from_cninfo(_cninfo_payload([CNINFO_ANNOUNCEMENT_ITEM]))

    assert rows[0]["published_at"] == "2026-08-15"
    assert rows[0]["title"] == "贵州茅台2026年半年度报告"
    assert rows[0]["url"].startswith("http://static.cninfo.com.cn/finalpage/")
    # 巨潮的类别名可能是 null（未按类别查询时）——不得凭空编一个类型
    assert rows[0]["type"] in ("", None)


def test_announcement_normalizers_drop_rows_without_date_or_title():
    """没有日期或标题的行成不了证据：留着会生成一条引用不了的「公告N」。"""
    payload = _em_payload(
        [
            EM_ANNOUNCEMENT_ITEM,
            {"title": "只有标题", "codes": [{"stock_code": "600519", "short_name": "贵州茅台"}]},
            {"notice_date": "2026-08-15 00:00:00", "codes": []},
        ]
    )
    assert len(cna.announcements_from_em(payload)) == 1


def test_announcement_normalizers_tolerate_garbage_payload():
    """上游返回形状变了不能抛：抛出去会让整次取数失败，而失败应表现为「未覆盖」。"""
    for payload in (None, {}, {"data": None}, {"data": {"list": None}}, "not a dict"):
        assert cna.announcements_from_em(payload) == []
        assert cna.announcements_from_cninfo(payload) == []


# ══ 2. 公告：fetcher 的回退与 None 语义 ═══════════════════════════════════


def test_fetch_announcements_uses_em_first(monkeypatch):
    monkeypatch.setattr(cna, "_announcement_payload_via_em", lambda *a, **kw: _em_payload([EM_ANNOUNCEMENT_ITEM]))
    monkeypatch.setattr(cna, "_announcement_payload_via_cninfo", lambda *a, **kw: _cninfo_payload([CNINFO_ANNOUNCEMENT_ITEM]))

    frame = cna.fetch_symbol_announcements("600519.SH", 10)

    assert frame is not None and len(frame) == 1
    assert list(frame.columns) == list(cna.ANNOUNCEMENT_COLUMNS)
    assert frame.iloc[0]["source"] == cna.SOURCE_EM  # 命中哪条源就记哪条
    assert frame.iloc[0]["title"].startswith("贵州茅台关于召开")


def test_fetch_announcements_falls_back_to_cninfo(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("np-anotice-stock 500: ip banned")

    monkeypatch.setattr(cna, "_announcement_payload_via_em", boom)
    monkeypatch.setattr(cna, "_announcement_payload_via_cninfo", lambda *a, **kw: _cninfo_payload([CNINFO_ANNOUNCEMENT_ITEM]))

    frame = cna.fetch_symbol_announcements("600519.SH", 10)

    assert frame is not None and len(frame) == 1
    assert frame.iloc[0]["source"] == cna.SOURCE_CNINFO
    assert frame.iloc[0]["published_at"] == "2026-08-15"


def test_fetch_announcements_returns_none_not_empty_frame(monkeypatch):
    """两条源都拿到空 = **没拉到的**，不是「没有公告」——不能返回空帧。"""
    monkeypatch.setattr(cna, "_announcement_payload_via_em", lambda *a, **kw: _em_payload([]))
    monkeypatch.setattr(cna, "_announcement_payload_via_cninfo", lambda *a, **kw: _cninfo_payload([]))

    assert cna.fetch_symbol_announcements("600519.SH", 10) is None


def test_fetch_announcements_never_raises(monkeypatch):
    """取数层「失败只记日志不抛」：抛给工具等于把上游故障变成对话故障。"""

    def boom(*a, **kw):
        raise ValueError("juchao api key=abc123 rejected")

    monkeypatch.setattr(cna, "_announcement_payload_via_em", boom)
    monkeypatch.setattr(cna, "_announcement_payload_via_cninfo", boom)

    assert cna.fetch_symbol_announcements("600519.SH", 10) is None


def test_fetch_announcements_respects_limit_and_passes_window(monkeypatch):
    seen = {}

    def payload(symbol, start, end, **kw):
        seen.update({"symbol": symbol, "start": start, "end": end})
        return _em_payload([EM_ANNOUNCEMENT_ITEM, dict(EM_ANNOUNCEMENT_ITEM), dict(EM_ANNOUNCEMENT_ITEM)])

    monkeypatch.setattr(cna, "_announcement_payload_via_em", payload)

    frame = cna.fetch_symbol_announcements("600519.SH", 2, start="2026-09-15", end="2026-09-22")

    assert frame is not None and len(frame) == 2  # limit 收窄到 2
    assert seen == {"symbol": "600519", "start": "2026-09-15", "end": "2026-09-22"}  # 前缀被剥掉


# ══ 3. 指数：纯函数归一 ═══════════════════════════════════════════════════


def test_sina_hq_text_parses_into_quotes_with_snapshot_date():
    rows = cna.index_quotes_from_sina_hq(SINA_HQ_TEXT, requested=("sh000001", "sz399001"))

    assert [row["code"] for row in rows] == ["sh000001", "sz399001"]
    first = rows[0]
    assert first["name"] == "上证指数"
    assert first["price"] == pytest.approx(3964.0279)
    assert first["prev_close"] == pytest.approx(3949.9068)
    # 涨跌幅是**百分数口径**（0.36 表示 +0.36%），与证据的 pct 单位一致
    assert first["change_pct"] == pytest.approx(0.358, abs=0.01)
    # 快照日期只有直连这条路才拿得到：它是「这数据什么时候的」的唯一依据
    assert first["quote_date"] == "2026-09-22"
    assert first["source"] == cna.SOURCE_SINA


def test_sina_hq_text_drops_codes_the_source_does_not_return():
    """`sh999999` 这种查不到的代码返回空串——**不能**当成 0 点。"""
    rows = cna.index_quotes_from_sina_hq(SINA_HQ_TEXT, requested=("sh000001", "sh999999"))

    assert [row["code"] for row in rows] == ["sh000001"]


def test_sina_hq_garbage_text_yields_no_quotes():
    for text in (None, "", "not a quote at all", "var hq_str_sh000001=\"\";"):
        assert cna.index_quotes_from_sina_hq(text, requested=("sh000001",)) == []


def test_sina_spot_frame_normalizes_chinese_columns():
    rows = cna.index_quotes_from_sina_spot(_frame(_SINA_SPOT_COLUMNS, SINA_SPOT_ROWS), requested=("sh000001", "sz399006"))

    assert [row["code"] for row in rows] == ["sh000001", "sz399006"]
    assert rows[0]["name"] == "上证指数"
    assert rows[0]["change_pct"] == pytest.approx(0.313)
    assert rows[0]["price"] == pytest.approx(3962.2666)
    # 该源不带时间戳 → 快照日期留空（**不填今天**）
    assert rows[0]["quote_date"] == ""
    assert rows[1]["source"] == cna.SOURCE_SINA


def test_em_spot_frame_matches_bare_codes():
    """东财返回的是**裸代码**（`000001`），请求的是 `sh000001`——按裸码匹配。"""
    rows = cna.index_quotes_from_em_spot(_frame(_EM_SPOT_COLUMNS, EM_SPOT_ROWS), requested=("sh000001", "sz399006"))

    assert [row["code"] for row in rows] == ["sh000001", "sz399006"]
    assert rows[0]["change_pct"] == pytest.approx(0.35)
    assert rows[0]["source"] == cna.SOURCE_EM


def test_index_code_normalization_is_documented_and_stable():
    """裸代码要按**指数**口径补前缀：000001 是上证指数（不是平安银行）。"""
    assert cna.normalize_index_code("000001") == "sh000001"
    assert cna.normalize_index_code("399006") == "sz399006"
    assert cna.normalize_index_code("sh000300") == "sh000300"
    assert cna.normalize_index_code("SZ399001") == "sz399001"
    assert cna.normalize_index_code("600519.SH") == "sh600519"
    assert cna.normalize_index_code("") == ""


def test_default_index_codes_cover_sh_sz_and_chinext():
    assert cna.DEFAULT_INDEX_CODES == ("sh000001", "sz399001", "sz399006")


def test_tool_and_data_layer_agree_on_the_default_indices():
    """两处各写一份默认集必然漂移，漂移的表现是「以为查了创业板，其实没查」。"""
    from tradingagents.tools import evidence as ev

    assert ev.DEFAULT_INDEX_CODES == cna.DEFAULT_INDEX_CODES


# ══ 4. 指数：fetcher 的回退与 None 语义 ═══════════════════════════════════


def test_fetch_index_quotes_uses_sina_hq_first(monkeypatch):
    monkeypatch.setattr(cna, "_index_text_via_sina_hq", lambda codes: SINA_HQ_TEXT)
    monkeypatch.setattr(cna, "_index_frame_via_sina_akshare", lambda: _frame(_SINA_SPOT_COLUMNS, SINA_SPOT_ROWS))

    frame = cna.fetch_index_quotes(("sh000001", "sz399001"))

    assert frame is not None
    assert list(frame.columns) == list(cna.INDEX_QUOTE_COLUMNS)
    assert frame.iloc[0]["quote_date"] == "2026-09-22"  # 直连那条才有的时间戳


def test_fetch_index_quotes_falls_back_through_three_sources(monkeypatch):
    def boom(*a, **kw):
        raise OSError("connection reset by peer")

    monkeypatch.setattr(cna, "_index_text_via_sina_hq", boom)
    monkeypatch.setattr(cna, "_index_frame_via_sina_akshare", boom)
    monkeypatch.setattr(cna, "_index_frame_via_em_akshare", lambda: _frame(_EM_SPOT_COLUMNS, EM_SPOT_ROWS))

    frame = cna.fetch_index_quotes(("sh000001", "sz399006"))

    assert frame is not None
    assert frame.iloc[0]["source"] == cna.SOURCE_EM
    assert frame.iloc[0]["quote_date"] == ""  # 这条源没有时间戳，就如实留空


def test_fetch_index_quotes_returns_none_when_code_unknown_everywhere(monkeypatch):
    monkeypatch.setattr(cna, "_index_text_via_sina_hq", lambda codes: SINA_HQ_TEXT)
    monkeypatch.setattr(cna, "_index_frame_via_sina_akshare", lambda: _frame(_SINA_SPOT_COLUMNS, SINA_SPOT_ROWS))
    monkeypatch.setattr(cna, "_index_frame_via_em_akshare", lambda: _frame(_EM_SPOT_COLUMNS, EM_SPOT_ROWS))

    # 三条源都活着，但没有一条认识这个代码 → None（空帧会被读成「这个指数是 0」）
    assert cna.fetch_index_quotes(("sh999999",)) is None


def test_fetch_index_quotes_never_raises(monkeypatch):
    def boom(*a, **kw):
        raise KeyError("unexpected upstream shape")

    for name in _INDEX_SEAMS:
        monkeypatch.setattr(cna, name, boom)

    assert cna.fetch_index_quotes(("sh000001",)) is None


def test_fetch_index_quotes_without_codes_uses_defaults():
    assert cna.fetch_index_quotes(None) is None  # 默认源全关时是 None，不是空帧
    assert cna.fetch_index_quotes(()) is None  # 空请求 = 没有可覆盖的维度


# ══ 5. 工具层：公告（端口适配在取数帧之上） ═══════════════════════════════


def test_announcements_port_adapts_frame_into_tool_rows(monkeypatch):
    monkeypatch.setattr(
        cna,
        "fetch_symbol_announcements",
        lambda symbol, limit, **kw: _frame(
            cna.ANNOUNCEMENT_COLUMNS,
            [("2026-09-18", "股东减持", "关于股东减持计划的公告", cna.SOURCE_EM, "http://x/1")],
        ),
    )
    rows = cna.SymbolAnnouncementsPort().list_announcements(
        symbol="600519.SH", start="2026-09-14", end="2026-09-21", limit=10
    )

    assert rows == [
        {
            "published_at": "2026-09-18",
            "type": "股东减持",
            "title": "关于股东减持计划的公告",
            "source": cna.SOURCE_EM,
            "url": "http://x/1",
        }
    ]


def test_announcements_port_returns_empty_when_fetcher_finds_nothing(monkeypatch):
    """端口把 None 翻成空列表：工具的「未覆盖」分支靠空列表触发，靠 None 会炸。"""
    monkeypatch.setattr(cna, "fetch_symbol_announcements", lambda *a, **kw: None)

    assert cna.SymbolAnnouncementsPort().list_announcements(
        symbol="600519.SH", start="2026-09-14", end="2026-09-21", limit=10
    ) == []


def test_announcements_tool_through_the_real_port(monkeypatch):
    """端到端（离线）：取数帧 → 端口 → 工具证据。"""
    monkeypatch.setattr(
        cna,
        "fetch_symbol_announcements",
        lambda symbol, limit, **kw: _frame(
            cna.ANNOUNCEMENT_COLUMNS,
            [
                ("2026-09-18", "股东减持", "关于股东减持计划的公告", cna.SOURCE_EM, "http://x/1"),
                ("2026-09-15", "业绩预告", "2026 年半年度业绩预告", cna.SOURCE_EM, "http://x/2"),
            ],
        ),
    )
    spec = _tool("announcements_read", announcements_port=cna.SymbolAnnouncementsPort())
    result = spec.handler(symbol="600519.SH", as_of="2026-09-21", lookback_days=7, limit=10)

    assert result.coverage.unavailable == ()
    assert _field(result, "公告数量").value == pytest.approx(2)
    first = _field(result, "公告1")
    # 披露日期｜类型｜标题｜来源 四段齐全，且披露日期进 as_of
    assert first.as_of == "2026-09-18"
    assert first.value == "2026-09-18｜类型 股东减持｜关于股东减持计划的公告｜来源 东方财富"


def test_announcements_tool_declares_uncovered_when_source_is_empty(monkeypatch):
    monkeypatch.setattr(cna, "fetch_symbol_announcements", lambda *a, **kw: None)
    spec = _tool("announcements_read", announcements_port=cna.SymbolAnnouncementsPort())

    result = spec.handler(symbol="600519.SH", as_of="2026-09-21", lookback_days=7, limit=10)

    assert result.evidence == ()
    assert result.coverage.unavailable == (UNAVAILABLE_ANNOUNCEMENTS,)
    assert result.degraded is True
    assert "abc123" not in str(result.data)


def test_announcements_port_lets_fetch_failure_reach_the_decay_path(monkeypatch):
    """取数层不抛，端口也不吞：真抛了由工具降级（且不外传原文）。"""

    def boom(*a, **kw):
        raise RuntimeError("cninfo token=abc123 rejected")

    monkeypatch.setattr(cna, "fetch_symbol_announcements", boom)
    spec = _tool("announcements_read", announcements_port=cna.SymbolAnnouncementsPort())

    result = spec.handler(symbol="600519.SH", as_of="2026-09-21", lookback_days=7, limit=10)

    assert result.degraded is True
    assert "abc123" not in str(result.data)
    assert result.coverage.unavailable == (UNAVAILABLE_ANNOUNCEMENTS,)


# ══ 6. 工具层：指数行情 ═══════════════════════════════════════════════════


def _index_fetcher(rows=None, *, error: Exception | None = None):
    def fetcher(codes):
        if error is not None:
            raise error
        if rows is None:
            return None
        return _frame(cna.INDEX_QUOTE_COLUMNS, rows)

    return fetcher


def _index_rows():
    return [
        ("sh000001", "上证指数", 3962.2666, 12.36, 0.313, 3949.9068, "2026-09-22", cna.SOURCE_SINA),
        ("sz399001", "深证成指", 13880.265, 150.244, 1.094, 13730.021, "2026-09-22", cna.SOURCE_SINA),
        ("sz399006", "创业板指", 3459.456, 59.866, 1.761, 3399.59, "2026-09-22", cna.SOURCE_SINA),
    ]


def test_index_quote_evidence_is_citable_with_pct_unit():
    spec = _tool("index_quote_read", index_fetcher=_index_fetcher(_index_rows()))
    result = spec.handler(as_of="2026-09-22")

    assert spec.plan_layer == PLAN_LAYER_EVIDENCE
    assert _field(result, "指数数量").value == pytest.approx(3)

    price = _field(result, "上证指数（sh000001）点位")
    assert price is not None and isinstance(price.value, float) and price.value == pytest.approx(3962.2666)
    # 点位没有量纲（指数点），涨跌幅必须是 pct
    assert price.unit == ""

    pct = _field(result, "上证指数（sh000001）涨跌幅")
    assert pct is not None and pct.unit == "pct" and isinstance(pct.value, float)
    assert pct.value == pytest.approx(0.313)
    # 快照日期进 as_of：回答写「9 月 22 日上证涨 0.31%」靠它核对
    assert pct.as_of == "2026-09-22"
    assert [item.id for item in result.evidence] == [f"E{i}" for i in range(1, len(result.evidence) + 1)]
    assert result.coverage.unavailable == ()


def test_index_quote_unknown_snapshot_date_does_not_claim_freshness():
    """源没有时间戳时（akshare 兜底路径）**不得**把基准日当成快照日期。"""
    rows = [("sh000001", "上证指数", 3962.2666, 12.36, 0.313, 3949.9068, "", cna.SOURCE_EM)]
    spec = _tool("index_quote_read", index_fetcher=_index_fetcher(rows))
    result = spec.handler(as_of="2026-09-22")

    item = _field(result, "上证指数（sh000001）涨跌幅")
    assert item.as_of == "2026-09-22"  # 只能用请求基准日承载这条证据
    assert result.freshness["index_quotes"]["status"] == "unknown"  # 但不许声称「新鲜」
    assert "快照日期" in " ".join(str(i.value) for i in result.evidence)


def test_index_quote_names_the_snapshot_date_of_the_source():
    spec = _tool("index_quote_read", index_fetcher=_index_fetcher(_index_rows()))
    result = spec.handler(as_of="2026-09-22")

    assert result.freshness["index_quotes"]["status"] == "fresh"
    assert result.freshness["index_quotes"]["anchor_actual"] == "2026-09-22"
    assert _field(result, "数据时点与来源").value.startswith("快照日期 2026-09-22")


def test_index_quote_records_the_snapshot_date_not_the_reference_date():
    """实时快照只有**今天**的报价：问的是历史某日时，证据的 as_of 必须是快照日期。

    若把 as_of 记成用户问的那天，「9 月 10 日上证涨 0.39%」这条断言会被校验器
    放行——那等于用一个今天的数字编造了一天的历史。
    """
    spec = _tool("index_quote_read", index_fetcher=_index_fetcher(_index_rows()))
    result = spec.handler(as_of="2026-09-10")

    pct = _field(result, "上证指数（sh000001）涨跌幅")
    assert pct.as_of == "2026-09-22"  # 数据的真实日期
    assert "2026-09-10" not in {item.as_of for item in result.evidence}
    # 工具另外用人类可读的一句话说明「只有实时快照，没有那天的收盘」
    assert result.data["quote_date_matches_as_of"] is False
    assert "2026-09-10" in result.data["note"]
    # 平台时效词汇里没有「基准日早于快照日」这一态：切点晚于基准日不算过期，
    # 真正的防线是上面那条 as_of（校验器按被引用证据的 as_of 核对日期断言）。
    assert result.freshness["index_quotes"]["status"] != "error"


def test_index_quote_declares_uncovered_when_nothing_came_back():
    spec = _tool("index_quote_read", index_fetcher=_index_fetcher(None))
    result = spec.handler(as_of="2026-09-22")

    assert result.evidence == ()
    assert result.coverage.unavailable == (UNAVAILABLE_INDEX,)
    assert result.degraded is True
    assert "指数" in UNAVAILABLE_INDEX


def test_index_quote_failure_degrades_without_upstream_text():
    spec = _tool("index_quote_read", index_fetcher=_index_fetcher(error=ValueError("hq.sinajs.cn 403 token=abc123")))
    result = spec.handler(as_of="2026-09-22")

    assert result.degraded is True
    assert "abc123" not in str(result.data)
    assert "hq.sinajs.cn" not in str(result.degraded_reason)
    assert "ValueError" in (result.degraded_reason or "")
    assert result.coverage.unavailable == (UNAVAILABLE_INDEX,)


def test_index_quote_marks_requested_but_missing_codes_as_uncovered():
    """请求了三个、只回来两个：第三个是**未覆盖**，不是「涨跌幅 0」。"""
    spec = _tool("index_quote_read", index_fetcher=_index_fetcher(_index_rows()[:2]))
    result = spec.handler(codes=["sh000001", "sz399001", "sh000688"], as_of="2026-09-22")

    assert any("sh000688" in name for name in result.coverage.unavailable)
    assert result.degraded is True


def test_index_quote_empty_code_list_is_not_replaced_by_defaults():
    """`codes=[]` 是「什么都不查」，不能被 falsy 换成默认三个（本仓库踩过四次）。"""
    calls: list = []

    def fetcher(codes):
        calls.append(tuple(codes))
        return _frame(cna.INDEX_QUOTE_COLUMNS, _index_rows())

    result = _tool("index_quote_read", index_fetcher=fetcher).handler(codes=[], as_of="2026-09-22")

    assert calls == []  # 压根没取数
    assert result.coverage.unavailable == (UNAVAILABLE_INDEX,)
    assert result.degraded is True


def test_index_quote_caps_and_dedupes_requested_codes():
    seen: list = []

    def fetcher(codes):
        seen.append(list(codes))
        return _frame(cna.INDEX_QUOTE_COLUMNS, _index_rows())

    from tradingagents.tools.evidence import MAX_INDEX_CODES

    _tool("index_quote_read", index_fetcher=fetcher).handler(
        codes=["sh000001", "sh000001"] + [f"sh{i:06d}" for i in range(MAX_INDEX_CODES + 5)],
        as_of="2026-09-22",
    )

    assert len(seen[0]) == MAX_INDEX_CODES
    assert seen[0][0] == "sh000001"
    assert len(set(seen[0])) == MAX_INDEX_CODES


def test_index_quote_defaults_to_main_indices_when_codes_omitted():
    seen: list = []

    def fetcher(codes):
        seen.append(list(codes))
        return _frame(cna.INDEX_QUOTE_COLUMNS, _index_rows())

    _tool("index_quote_read", index_fetcher=fetcher).handler(as_of="2026-09-22")

    assert seen[0] == list(cna.DEFAULT_INDEX_CODES)


# ══ 7. 注册表：未注入不注册 / 注入后在册 ══════════════════════════════════


def test_index_quote_is_l2_and_registered_only_when_injected():
    spec = _tool("index_quote_read", index_fetcher=_index_fetcher(_index_rows()))
    assert spec.plan_layer == PLAN_LAYER_EVIDENCE
    assert spec.freshness_sources == ("index_quotes",)

    assert "index_quote_read" not in _registry().names()
    registry = _registry(index_fetcher=_index_fetcher(_index_rows()))
    assert "index_quote_read" in registry.names_by_layer(PLAN_LAYER_EVIDENCE)


def test_both_new_data_sources_land_in_the_evidence_layer(monkeypatch):
    monkeypatch.setattr(
        cna,
        "fetch_symbol_announcements",
        lambda symbol, limit, **kw: _frame(
            cna.ANNOUNCEMENT_COLUMNS,
            [("2026-09-18", "股东减持", "关于股东减持计划的公告", cna.SOURCE_EM, "")],
        ),
    )
    registry = _registry(
        announcements_port=cna.SymbolAnnouncementsPort(),
        index_fetcher=_index_fetcher(_index_rows()),
    )
    names = registry.names_by_layer(PLAN_LAYER_EVIDENCE)

    assert "announcements_read" in names
    assert "index_quote_read" in names


def test_application_layer_factories_are_offline_and_return_the_right_shape():
    """组合根的注入点：工厂本身不触网，返回的对象形状对得上工具端口。"""
    from api.services import chat_service

    port = chat_service.build_announcements_port()
    assert hasattr(port, "list_announcements")

    fetcher = chat_service.build_index_fetcher()
    assert callable(fetcher)


# ── 小工具 ────────────────────────────────────────────────────────────────


def _tool(name, **kwargs):
    return {spec.name: spec for spec in make_evidence_tools(**kwargs)}[name]


def _field(result, field):
    for item in result.evidence:
        if item.field == field:
            return item
    return None


def _registry(**kw):
    from tradingagents.tools import build_registry

    return build_registry(ports_factory=lambda *a: object(), news_fetcher=lambda *a: [], **kw)
