"""L2 证据工具（只给事实、不做判断）。

## 这个文件锁住什么

六个 L2 工具：`chip_distribution_read` / `intraday_bars_read` /
`capital_flow_read` / `watchlist_read` / `announcements_read` /
`data_freshness_check`。每个工具至少覆盖四件事：

1. **证据可引用且单位正确**：数值是 `float` + 量纲（`ratio` / `pct` / `yuan` /
   `count` / `days`）之一，日期进 `as_of`。单位错了校验器的量级比对就成了摆设。
2. **取不到时声明未覆盖**：`Coverage.unavailable` 里给**可读名**，绝不返回空帧
   冒充「取到了」——空数据被读成「没有异常」是本平台最不能接受的失败形态。
3. **失败降级、不外传原文**：`degraded=True` + 可读原因，上游异常消息体
   （可能带 IP、token、URL）不进对外结果，也不抛错给模型。
4. **未注入不注册 / 注入后在册**：没有取数或端口的工具不出现在注册表里，
   不留「在册但一调就炸」的条目。

另外两条专项：
* **估算必须标注**：筹码来源以 `model_estimate` 结尾时，证据里必须逐字出现
  「模型估计」与「非交易所原始数据」。把估计说成事实是最不容忍的失败。
* **clamp 不用 `x or default`**：`limit=0` 必须收窄到 1，而不是被 falsy 换成默认
  值（本仓库已踩过四次）。

## 不联网

全部取数用假函数/假端口注入（或 `monkeypatch` 替换），断言纯粹基于注入的事实。
"""

from __future__ import annotations

import pytest

from tradingagents.tools.evidence import (
    ESTIMATE_NOTE,
    UNAVAILABLE_ANNOUNCEMENTS,
    UNAVAILABLE_CHIP,
    UNAVAILABLE_FLOW,
    UNAVAILABLE_INTRADAY,
    UNAVAILABLE_WATCHLIST,
    MarketEvidenceFetchers,
)
from tradingagents.tools.spec import PLAN_LAYER_EVIDENCE


def _market_tools(**fetchers):
    from tradingagents.tools.evidence import make_evidence_tools

    specs = make_evidence_tools(fetchers=MarketEvidenceFetchers(**fetchers))
    return {spec.name: spec for spec in specs}


def _port_tools(**kwargs):
    from tradingagents.tools.evidence import make_evidence_tools

    specs = make_evidence_tools(**kwargs)
    return {spec.name: spec for spec in specs}


def _joined(result) -> str:
    return " ".join(f"{item.field}={item.value}{item.unit}" for item in result.evidence)


def _by_field(result, field: str):
    for item in result.evidence:
        if item.field == field:
            return item
    return None


def _bare_registry(**kw):
    from tradingagents.tools import build_registry

    return build_registry(ports_factory=lambda *a: object(), news_fetcher=lambda *a: [], **kw)


# ══ 1. chip_distribution_read：筹码分布（含「这是估计」的口径） ═════════════


CHIP_ROW = {
    "date": "2026-09-18",
    "profit_ratio": 0.583,
    "avg_cost": 12.5,
    "concentration_90": 0.21,
    "concentration_70": 0.14,
}


def _chip(fetcher):
    return _market_tools(chip=fetcher)["chip_distribution_read"]


def test_chip_evidence_is_citable_with_correct_units():
    spec = _chip(lambda symbol, day: [dict(CHIP_ROW, source="eastmoney_cyq")])
    result = spec.handler(symbol="600519.SH", as_of="2026-09-21")

    profit = _by_field(result, "获利比例")
    assert profit is not None and profit.unit == "ratio" and profit.value == pytest.approx(0.583)
    cost = _by_field(result, "平均成本")
    assert cost is not None and cost.unit == "yuan" and cost.value == pytest.approx(12.5)
    assert _by_field(result, "90%筹码集中度").unit == "ratio"
    # 日期进 as_of：回答里写「9 月 18 日的筹码」时校验器靠它核对
    assert profit.as_of == "2026-09-18"
    assert [item.id for item in result.evidence] == [f"E{i}" for i in range(1, len(result.evidence) + 1)]
    assert result.coverage.unavailable == ()


def test_chip_model_estimate_must_say_it_is_an_estimate():
    """来源是自算估计时必须写明「模型估计，非交易所原始数据」。

    这是本平台最不容忍的失败：把估计说成交易所事实。
    """
    frame = [dict(CHIP_ROW, source="cyq_model_estimate")]
    spec = _chip(lambda symbol, day: frame)
    result = spec.handler(symbol="600519.SH", as_of="2026-09-21")

    joined = _joined(result)
    assert "模型估计" in joined and "非交易所原始数据" in joined
    assert result.data["estimated"] is True
    # 编排层按这个键用**代码**渲染口径行，不指望模型复述
    assert result.data["estimated_dimensions"]
    assert ESTIMATE_NOTE in str(_by_field(result, "数据口径").value)


def test_chip_estimate_marker_survives_a_real_dataframe():
    """真实路径给的是 DataFrame，`frame.get("source")` 会返回 Series。

    照抄那个写法会让 `str(Series).endswith("model_estimate")` 永远为假——
    标记静默失效，估计值就又被当成数据了。这里用真帧钉住这条路径。
    """
    pd = pytest.importorskip("pandas")
    frame = pd.DataFrame([dict(CHIP_ROW, source="cyq_model_estimate")])
    spec = _chip(lambda symbol, day: frame)
    result = spec.handler(symbol="600519.SH", as_of="2026-09-21")

    assert result.data["estimated"] is True
    assert "模型估计" in _joined(result)


def test_chip_exchange_source_is_not_labelled_an_estimate():
    spec = _chip(lambda symbol, day: [dict(CHIP_ROW, source="eastmoney_cyq")])
    result = spec.handler(symbol="600519.SH", as_of="2026-09-21")

    assert result.data["estimated"] is False
    assert "模型估计" not in _joined(result), "交易所口径不得被标成估计"


def test_chip_percent_scale_is_normalised_to_ratio():
    """58.3 这种百分数口径必须换算：否则「获利盘 58.3%」会被当成 58.3 倍。"""
    spec = _chip(lambda symbol, day: [dict(CHIP_ROW, profit_ratio=58.3)])
    result = spec.handler(symbol="600519.SH", as_of="2026-09-21")

    assert _by_field(result, "获利比例").value == pytest.approx(0.583)


def test_chip_missing_is_declared_not_an_empty_success():
    spec = _chip(lambda symbol, day: None)
    result = spec.handler(symbol="600519.SH", as_of="2026-09-21")

    assert result.evidence == (), "没有事实就不该有证据"
    assert result.coverage.unavailable == (UNAVAILABLE_CHIP,)
    assert "筹码" in UNAVAILABLE_CHIP, "未覆盖维度必须给可读名"
    assert result.coverage.available == ()
    assert result.degraded is True
    assert result.freshness["chip_distribution"]["status"] == "error"


def test_chip_failure_degrades_without_leaking_upstream_text():
    def boom(symbol, day):
        raise RuntimeError("eastmoney blocked: ip=10.0.0.7 token=sk-secret")

    result = _chip(boom).handler(symbol="600519.SH", as_of="2026-09-21")

    assert result.degraded is True
    leaked = f"{result.data} {result.degraded_reason}"
    assert "sk-secret" not in leaked and "10.0.0.7" not in leaked
    assert "RuntimeError" in (result.degraded_reason or ""), "只留类型名，便于归因"
    assert result.coverage.unavailable == (UNAVAILABLE_CHIP,)


def test_chip_is_l2_and_registered_only_when_a_fetcher_is_injected():
    spec = _chip(lambda symbol, day: [dict(CHIP_ROW)])
    assert spec.plan_layer == PLAN_LAYER_EVIDENCE

    assert "chip_distribution_read" not in _bare_registry().names(), "没有取数器就不该注册"
    registry = _bare_registry(evidence_fetchers=MarketEvidenceFetchers(chip=lambda s, d: None))
    assert "chip_distribution_read" in registry.names()
    assert "chip_distribution_read" in registry.names_by_layer(PLAN_LAYER_EVIDENCE)


# ══ 2. intraday_bars_read：某日分时承接 ═════════════════════════════════════


def _bars(entries):
    return [
        {"date": f"2026-09-18 {time}", "open": o, "high": h, "low": low, "close": c, "volume": v}
        for time, o, h, low, c, v in entries
    ]


INTRADAY_BARS = _bars(
    [
        ("09:35", 10.0, 10.1, 9.9, 10.0, 100),
        ("09:40", 10.0, 10.0, 9.7, 9.8, 200),
        ("14:35", 9.8, 9.9, 9.6, 9.7, 300),
        ("15:00", 9.7, 9.8, 9.6, 9.75, 400),
    ]
)


def _intraday(fetcher):
    return _market_tools(intraday=fetcher)["intraday_bars_read"]


def test_intraday_evidence_has_units_and_the_trading_day_as_of():
    spec = _intraday(lambda symbol, day, freq: INTRADAY_BARS)
    result = spec.handler(symbol="600519.SH", as_of="2026-09-18")

    close = _by_field(result, "收盘价")
    assert close.value == pytest.approx(9.75) and close.unit == "yuan"
    change = _by_field(result, "相对当日开盘涨跌幅")
    assert change.unit == "pct" and change.value == pytest.approx(-2.5)
    tail = _by_field(result, "尾盘量占比")
    assert tail.unit == "ratio" and tail.value == pytest.approx(700 / 1000)
    assert _by_field(result, "分时根数").value == pytest.approx(4)
    assert _by_field(result, "分时根数").unit == "count"
    assert close.as_of == "2026-09-18"
    assert result.coverage.unavailable == ()


def test_intraday_rows_are_sorted_so_close_is_the_last_bar():
    """分时帧在部分源里不保证顺序；顺序错了收盘价就会是盘中某一根。"""
    reversed_bars = list(reversed(INTRADAY_BARS))
    spec = _intraday(lambda symbol, day, freq: reversed_bars)
    result = spec.handler(symbol="600519.SH", as_of="2026-09-18")

    assert _by_field(result, "收盘价").value == pytest.approx(9.75)


def test_intraday_tail_fallback_is_declared_when_timestamps_are_missing():
    """时间戳不可用时「尾盘」只是最后几根的代理，必须声明，不能当精确切分。"""
    untimed = [{k: v for k, v in bar.items() if k != "date"} for bar in INTRADAY_BARS]
    spec = _intraday(lambda symbol, day, freq: untimed)
    result = spec.handler(symbol="600519.SH", as_of="2026-09-18")

    assert result.coverage.unavailable == ("尾盘分段（时间戳缺失）",)
    assert _by_field(result, "末段量占比") is not None
    assert result.data["tail_precise"] is False
    assert result.degraded is True


def test_intraday_missing_day_is_declared_uncovered():
    spec = _intraday(lambda symbol, day, freq: None)
    result = spec.handler(symbol="600519.SH", as_of="2026-09-20")

    assert result.evidence == ()
    assert result.coverage.unavailable == (UNAVAILABLE_INTRADAY,)
    assert "分时" in UNAVAILABLE_INTRADAY
    assert result.degraded is True
    assert "非交易日" in (result.degraded_reason or "")


def test_intraday_failure_degrades_without_upstream_text():
    def boom(symbol, day, freq):
        raise TimeoutError("sina timeout after 30s at http://10.1.2.3:8080/api?token=x")

    result = _intraday(boom).handler(symbol="600519.SH", as_of="2026-09-18")

    assert result.degraded is True
    leaked = f"{result.data} {result.degraded_reason}"
    assert "10.1.2.3" not in leaked and "token=x" not in leaked
    assert result.coverage.unavailable == (UNAVAILABLE_INTRADAY,)


def test_intraday_freq_is_passed_to_the_fetcher():
    seen: list[str] = []

    def fetcher(symbol, day, freq):
        seen.append(freq)
        return INTRADAY_BARS

    _intraday(fetcher).handler(symbol="600519.SH", as_of="2026-09-18", freq="15")
    assert seen == ["15"]


def test_intraday_is_l2_and_registered_only_when_injected():
    spec = _intraday(lambda symbol, day, freq: INTRADAY_BARS)
    assert spec.plan_layer == PLAN_LAYER_EVIDENCE
    assert "intraday_bars_read" not in _bare_registry().names()
    registry = _bare_registry(evidence_fetchers=MarketEvidenceFetchers(intraday=lambda s, d, f: None))
    assert "intraday_bars_read" in registry.names_by_layer(PLAN_LAYER_EVIDENCE)


# ══ 3. capital_flow_read：资金流（单位：元） ═════════════════════════════════


def _flow(fetcher):
    return _market_tools(flow=fetcher)["capital_flow_read"]


def test_capital_flow_evidence_is_in_yuan_and_keeps_the_sign():
    frame = [
        {
            "date": "2026-09-18",
            "main_net_inflow": -1.2e8,
            "super_large_net_inflow": -5.0e7,
            "large_net_inflow": -3.0e7,
        }
    ]
    result = _flow(lambda symbol, day: frame).handler(symbol="600519.SH", as_of="2026-09-21")

    main = _by_field(result, "主力净流入")
    assert main.unit == "yuan", "单位必须是元（Tushare 是万元，差 10000 倍）"
    assert main.value == pytest.approx(-1.2e8), "负号必须保留：它承载「净流出」"
    assert _by_field(result, "超大单净流入").value == pytest.approx(-5.0e7)
    assert main.as_of == "2026-09-18"
    assert result.data["unit"] == "yuan"


def test_capital_flow_converts_a_raw_tushare_frame_from_wan_yuan():
    """误传原始 Tushare 帧（net_mf_amount 单位万元）时必须换算成元。"""
    frame = [{"date": "2026-09-18", "net_mf_amount": 12000.0}]  # 12000 万元 = 1.2 亿元
    result = _flow(lambda symbol, day: frame).handler(symbol="600519.SH", as_of="2026-09-21")

    assert _by_field(result, "主力净流入").value == pytest.approx(1.2e8)


def test_capital_flow_missing_is_declared_uncovered():
    result = _flow(lambda symbol, day: None).handler(symbol="600519.SH", as_of="2026-09-21")

    assert result.evidence == ()
    assert result.coverage.unavailable == (UNAVAILABLE_FLOW,)
    assert result.degraded is True
    assert "资金流" in UNAVAILABLE_FLOW


def test_capital_flow_failure_degrades_without_upstream_text():
    def boom(symbol, day):
        raise ConnectionError("eastmoney RemoteDisconnected via proxy 127.0.0.1:7897")

    result = _flow(boom).handler(symbol="600519.SH", as_of="2026-09-21")

    assert result.degraded is True
    leaked = f"{result.data} {result.degraded_reason}"
    assert "7897" not in leaked and "RemoteDisconnected" not in leaked
    assert result.coverage.unavailable == (UNAVAILABLE_FLOW,)


def test_capital_flow_is_l2_and_registered_only_when_injected():
    spec = _flow(lambda symbol, day: None)
    assert spec.plan_layer == PLAN_LAYER_EVIDENCE
    assert "capital_flow_read" not in _bare_registry().names()
    registry = _bare_registry(evidence_fetchers=MarketEvidenceFetchers(flow=lambda s, d: None))
    assert "capital_flow_read" in registry.names_by_layer(PLAN_LAYER_EVIDENCE)


def test_market_fetchers_inject_independently():
    """只注入一个取数器时，另外两个工具不注册（缺依赖=不注册，逐个成立）。"""
    registry = _bare_registry(evidence_fetchers=MarketEvidenceFetchers(chip=lambda s, d: None))
    names = registry.names()
    assert "chip_distribution_read" in names
    assert "intraday_bars_read" not in names and "capital_flow_read" not in names


# ══ 4. watchlist_read：当前用户的自选清单 ══════════════════════════════════


class _FakeWatchlist:
    def __init__(self, rows=None, *, error: Exception | None = None):
        self.rows = rows if rows is not None else []
        self.error = error
        self.calls: list[dict] = []

    def list_items(self, *, limit):
        self.calls.append({"limit": limit})
        if self.error is not None:
            raise self.error
        return self.rows


WATCHLIST_ROWS = [
    {"id": "a1", "symbol": "600519.SH", "name": "贵州茅台", "created_at": "2026-01-02T09:30:00", "has_scheduled": True},
    {"id": "a2", "symbol": "300750.SZ", "created_at": "2026-02-11T10:00:00", "has_scheduled": False},
]


def _watchlist(port):
    return _port_tools(watchlist_port=port)["watchlist_read"]


def test_watchlist_items_are_citable_evidence():
    result = _watchlist(_FakeWatchlist(WATCHLIST_ROWS)).handler(limit=20, as_of="2026-09-21")

    count = _by_field(result, "自选股数量")
    assert count.value == pytest.approx(2) and count.unit == "count"
    first = _by_field(result, "自选股1")
    assert "600519.SH" in str(first.value)
    # 加入日期进 as_of：回答里写「1 月 2 日加的」时校验器靠它核对
    assert first.as_of == "2026-01-02"
    assert result.coverage.unavailable == ()


def test_empty_watchlist_is_a_fact_not_a_failed_read():
    """用户问「我的自选有什么」时，「没有」就是答案——不能声明成未覆盖。"""
    result = _watchlist(_FakeWatchlist([])).handler(limit=20, as_of="2026-09-21")

    assert _by_field(result, "自选股数量").value == pytest.approx(0.0)
    assert result.coverage.available == ("watchlist",)
    assert result.coverage.unavailable == (), "空清单是事实，不是未覆盖"
    assert result.data["note"] == "自选清单为空"


def test_watchlist_failure_degrades_without_upstream_text():
    result = _watchlist(_FakeWatchlist(error=RuntimeError("db locked: /tmp/x.db"))).handler(
        limit=5, as_of="2026-09-21"
    )

    assert result.degraded is True
    assert result.coverage.unavailable == (UNAVAILABLE_WATCHLIST,)
    assert "db locked" not in str(result.data)
    assert "自选清单" in UNAVAILABLE_WATCHLIST


def test_watchlist_limit_clamp_does_not_use_or_default():
    """`limit=0` 必须收窄到 1，而不是被 falsy 换成默认值。"""
    port = _FakeWatchlist(WATCHLIST_ROWS)
    spec = _watchlist(port)

    spec.handler(limit=0, as_of="2026-09-21")
    assert port.calls[0]["limit"] == 1

    spec.handler(limit=999, as_of="2026-09-21")
    assert port.calls[1]["limit"] == 50

    spec.handler(limit=7, as_of="2026-09-21")
    assert port.calls[2]["limit"] == 7


def test_watchlist_schema_has_no_user_id():
    """用户维度由端口的闭包绑定——参数里出现 user_id 等于给模型越权的可能。"""
    spec = _watchlist(_FakeWatchlist(WATCHLIST_ROWS))
    assert "user_id" not in (spec.input_schema.get("properties") or {})


def test_watchlist_is_l2_and_registered_only_when_injected():
    spec = _watchlist(_FakeWatchlist(WATCHLIST_ROWS))
    assert spec.plan_layer == PLAN_LAYER_EVIDENCE

    assert "watchlist_read" not in _bare_registry().names()
    registry = _bare_registry(watchlist_port=_FakeWatchlist(WATCHLIST_ROWS))
    assert "watchlist_read" in registry.names_by_layer(PLAN_LAYER_EVIDENCE)


# ══ 5. announcements_read：个股公告 ═════════════════════════════════════════


class _FakeAnnouncements:
    def __init__(self, rows=None, *, error: Exception | None = None):
        self.rows = rows if rows is not None else []
        self.error = error
        self.calls: list[dict] = []

    def list_announcements(self, *, symbol, start, end, limit):
        self.calls.append({"symbol": symbol, "start": start, "end": end, "limit": limit})
        if self.error is not None:
            raise self.error
        return self.rows


ANNOUNCEMENT_ROWS = [
    {"title": "关于股东减持计划的公告", "published_at": "2026-09-18", "type": "股东减持", "source": "巨潮"},
    {"title": "2026 年半年度业绩预告", "published_at": "2026-09-15", "type": "业绩预告"},
]


def _announcements(port):
    return _port_tools(announcements_port=port)["announcements_read"]


def test_announcements_become_dated_citable_evidence():
    result = _announcements(_FakeAnnouncements(ANNOUNCEMENT_ROWS)).handler(
        symbol="600519.SH", as_of="2026-09-21", lookback_days=7, limit=10
    )

    assert _by_field(result, "公告数量").value == pytest.approx(2)
    first = _by_field(result, "公告1")
    assert "股东减持" in str(first.value)
    # 披露日期即该条证据的 as_of：回答里写「9 月 18 日公告了减持」靠它核对
    assert first.as_of == "2026-09-18"
    assert result.coverage.unavailable == ()


def test_announcements_window_is_derived_from_as_of():
    port = _FakeAnnouncements(ANNOUNCEMENT_ROWS)
    _announcements(port).handler(symbol="600519.SH", as_of="2026-09-21", lookback_days=7, limit=10)

    assert port.calls[0]["start"] == "2026-09-14"
    assert port.calls[0]["end"] == "2026-09-21"
    assert port.calls[0]["symbol"] == "600519.SH"


def test_empty_announcements_are_declared_uncovered_not_no_news():
    """「没拉到」不等于「没有公告」——两者合并会凭空制造一个结论。"""
    result = _announcements(_FakeAnnouncements([])).handler(
        symbol="600519.SH", as_of="2026-09-21", lookback_days=7, limit=10
    )

    assert result.evidence == ()
    assert result.coverage.unavailable == (UNAVAILABLE_ANNOUNCEMENTS,)
    assert result.degraded is True
    assert "不等于" in (result.degraded_reason or "")
    assert "公告" in UNAVAILABLE_ANNOUNCEMENTS


def test_announcements_failure_degrades_without_upstream_text():
    class _Port:
        def list_announcements(self, **kw):
            raise ValueError("juchao api key=abc123 rejected")

    spec = _announcements(_Port())
    result = spec.handler(symbol="600519.SH", as_of="2026-09-21", lookback_days=7, limit=10)

    assert result.degraded is True
    assert "abc123" not in str(result.data)
    assert result.coverage.unavailable == (UNAVAILABLE_ANNOUNCEMENTS,)


def test_announcements_is_l2_and_registered_only_when_injected():
    spec = _announcements(_FakeAnnouncements(ANNOUNCEMENT_ROWS))
    assert spec.plan_layer == PLAN_LAYER_EVIDENCE

    assert "announcements_read" not in _bare_registry().names()
    registry = _bare_registry(announcements_port=_FakeAnnouncements(ANNOUNCEMENT_ROWS))
    assert "announcements_read" in registry.names_by_layer(PLAN_LAYER_EVIDENCE)


# ══ 6. data_freshness_check：这数据什么时候的 ══════════════════════════════


class _FakeFreshness:
    def __init__(self, facts=None, *, error: Exception | None = None):
        self.facts = facts or {}
        self.error = error
        self.calls: list[dict] = []

    def source_facts(self, *, source_keys, symbol=None):
        self.calls.append({"source_keys": tuple(source_keys), "symbol": symbol})
        if self.error is not None:
            raise self.error
        return self.facts


def _freshness(port):
    return _port_tools(freshness_port=port)["data_freshness_check"]


def test_freshness_reports_cutoff_origin_and_lag():
    port = _FakeFreshness({"stock_data": {"cutoff": "2026-09-18", "origin": "本地日线库"}})
    result = _freshness(port).handler(as_of="2026-09-21", sources=["stock_data"], symbol="600519.SH")

    item = _by_field(result, "时效：stock_data")
    assert "2026-09-18" in str(item.value) and "本地日线库" in str(item.value)
    # 日期进 as_of：回答里写「截止 9 月 18 日」时校验器靠它核对
    assert item.as_of == "2026-09-18"
    lag = _by_field(result, "stock_data 滞后天数")
    assert lag.unit == "days" and lag.value == pytest.approx(3.0)
    assert result.data["sources"][0]["status"] == "stale", "早于基准日必须判过期"
    assert result.data["sources"][0]["stale"] is True


def test_freshness_includes_contract_category_and_criticality():
    port = _FakeFreshness({"stock_data": {"cutoff": "2026-09-21", "origin": "新浪日线"}})
    result = _freshness(port).handler(as_of="2026-09-21", sources=["stock_data"])

    value = str(_by_field(result, "时效：stock_data").value)
    assert "calendar_anchored" in value and "critical" in value
    assert result.data["sources"][0]["status"] == "fresh"


def test_unknown_cutoff_is_declared_unknown_not_fresh():
    """截止日未知必须说「未知」——用今天日期兜底就是把未验证伪装成最新。"""
    result = _freshness(_FakeFreshness({})).handler(as_of="2026-09-21", sources=["chip_distribution"])

    row = result.data["sources"][0]
    assert row["status"] == "unknown" and row["cutoff"] is None
    assert "chip_distribution（时效未知）" in result.coverage.unavailable
    assert result.coverage.available == ()
    assert result.degraded is True
    assert "未知" in (result.degraded_reason or "")


def test_failed_source_is_reported_as_error_not_unknown():
    port = _FakeFreshness({"intraday_bars": {"unavailable": True, "origin": "东财"}})
    result = _freshness(port).handler(as_of="2026-09-21", sources=["intraday_bars"])

    assert result.data["sources"][0]["status"] == "error"
    assert "intraday_bars（本轮取数失败）" in result.coverage.unavailable


def test_unregistered_source_is_declared():
    result = _freshness(_FakeFreshness({})).handler(as_of="2026-09-21", sources=["not_a_real_source"])

    assert result.data["unregistered"] == ["not_a_real_source"]
    assert "not_a_real_source（未登记在时效契约）" in result.coverage.unavailable


def test_freshness_truncates_beyond_the_cap():
    from tradingagents.tools.evidence import MAX_FRESHNESS_SOURCES

    many = [f"source_{i}" for i in range(MAX_FRESHNESS_SOURCES + 3)]
    port = _FakeFreshness({})
    result = _freshness(port).handler(as_of="2026-09-21", sources=many)

    assert result.data["requested"] == len(many)
    assert result.data["checked"] == MAX_FRESHNESS_SOURCES
    assert result.data["truncated"] is True
    assert len(port.calls[0]["source_keys"]) == MAX_FRESHNESS_SOURCES


def test_freshness_defaults_only_when_sources_is_none():
    """`sources=[]` 是「什么都不查」，不能被 falsy 换成默认集。"""
    from tradingagents.tools.evidence import DEFAULT_FRESHNESS_SOURCES

    port = _FakeFreshness({})
    spec = _freshness(port)

    spec.handler(as_of="2026-09-21", sources=None)
    assert port.calls[0]["source_keys"] == tuple(DEFAULT_FRESHNESS_SOURCES)

    nothing = spec.handler(as_of="2026-09-21", sources=[])
    assert nothing.coverage.unavailable == ("data_freshness（时效查询）",)
    assert nothing.data["checked"] == 0
    assert len(port.calls) == 1, "显式空列表不该再问探针"


def test_freshness_probe_failure_degrades_without_upstream_text():
    port = _FakeFreshness(error=RuntimeError("db locked at /Users/x/tradingagents.db"))
    result = _freshness(port).handler(as_of="2026-09-21", sources=["stock_data"])

    assert result.degraded is True
    assert result.coverage.unavailable == ("data_freshness（时效查询）",)
    assert "db locked" not in str(result.data)
    assert "RuntimeError" in (result.degraded_reason or "")


def test_freshness_is_l2_and_registered_only_when_injected():
    spec = _freshness(_FakeFreshness({}))
    assert spec.plan_layer == PLAN_LAYER_EVIDENCE
    assert spec.freshness_sources == ()

    assert "data_freshness_check" not in _bare_registry().names()
    registry = _bare_registry(freshness_port=_FakeFreshness({}))
    assert "data_freshness_check" in registry.names_by_layer(PLAN_LAYER_EVIDENCE)


# ══ 全量装配：六个注入点都在时，六个工具都在册且都在 L2 ══════════════════


def test_all_six_tools_land_in_the_evidence_layer():
    registry = _bare_registry(
        evidence_fetchers=MarketEvidenceFetchers(
            chip=lambda s, d: None, intraday=lambda s, d, f: None, flow=lambda s, d: None
        ),
        watchlist_port=_FakeWatchlist(WATCHLIST_ROWS),
        announcements_port=_FakeAnnouncements(ANNOUNCEMENT_ROWS),
        freshness_port=_FakeFreshness({}),
    )
    names = set(registry.names_by_layer(PLAN_LAYER_EVIDENCE))
    expected = {
        "chip_distribution_read",
        "intraday_bars_read",
        "capital_flow_read",
        "watchlist_read",
        "announcements_read",
        "data_freshness_check",
    }
    assert expected <= names, f"缺工具：{sorted(expected - names)}"
    for name in expected:
        assert registry.get(name).plan_layer == PLAN_LAYER_EVIDENCE
