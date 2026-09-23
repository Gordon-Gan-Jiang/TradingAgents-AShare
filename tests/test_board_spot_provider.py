"""W2：板块快照接入新浪源——东财被封时的兜底，以及失败时的诚实声明。

## 背景（实测，2026-09-23）

东财那条路对本机不通（走系统代理 `ProxyError`、强制直连 `RemoteDisconnected`），而
`cn_board_data` 只配了东财一个源，于是 `get_board_spot` / `get_board_rank` /
`get_board_hist` 一起变成「数据源故障」。对比之下 `cn_market_data` 因为配了
`cn_sina_moneyflow` 优先，`get_board_fund_flow` 一直是有数的。

新浪的行业板块快照实测可用（`newSinaHy.php`，49 个板块）。本文件守住三件事：

1. **归一化到东财的列契约**：新浪的字段顺序与命名都不同，解析后必须落到
   `_normalize_board_spot_df` 的列契约（`name` / `chg_1d` / `leader` …），
   否则下游（规则层 `mainline_scoring`、格式化函数）会各自去认一套列名。
2. **链路顺序**：新浪优先、东财兜底；新浪能服务时**不许**再打东财（东财被封时
   每一次多余的调用都要先等重试耗完）。
3. **拿不到 ≠ 没有**：两源皆失败时必须有 `Coverage.unavailable` 的**原因**，
   而不是一段空文本或一个"成功但没有数据"的形状。

全部用 mock，**不打网络**（新浪这条真链路的实测输出在 `/tmp` 探测脚本里，
不提交进仓库）。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from tradingagents.dataflows.providers import cn_sina_moneyflow_provider as sina_module
from tradingagents.dataflows.providers.cn_akshare_provider import CnAkshareProvider
from tradingagents.dataflows.providers.cn_sina_moneyflow_provider import (
    CnSinaMoneyflowProvider,
    fetch_sina_industry_board_spot_df,
    format_sina_board_spot_ranking,
)


# ── 样本：与新浪真实返回同形 ─────────────────────────────────────────────
#
# `newSinaHy.php` 返回的是一句 JS 赋值（不是 JSON），值是以逗号拼接的 13 字段串：
# label,板块,公司家数,平均价格,涨跌额,涨跌幅,总成交量,总成交额,股票代码,
# 个股-涨跌幅,个股-当前价,个股-涨跌额,股票名称
# ——两个样本取自实测返回，另加一行改造成"涨幅最大"以便断言排序。
def _sina_hy_payload() -> str:
    rows = {
        "new_blhy": (
            "new_blhy,玻璃行业,19,17.846842105263,0.011052631578947,"
            "0.061968838526912,674027363,14406811668,sh600293,10.119,3.700,0.340,三峡新材"
        ),
        "new_cbzz": (
            "new_cbzz,船舶制造,8,14.802857142857,-0.49285714285714,"
            "-3.2221910899412,247562371,4733791813,sz002608,-0.411,7.270,-0.070,亚光科技"
        ),
        "new_dzxx": (
            "new_dzxx,电子信息,120,25.500000000000,0.29999999999999,"
            "1.1900000000000,900000000,12000000000,sz300059,10.020,20.000,1.200,东方财富"
        ),
    }
    return "var S_Finance_bankuai_sinaindustry = " + json.dumps(rows, ensure_ascii=False) + ";"


def _patch_sina_payload(monkeypatch, payload: str | None = None) -> MagicMock:
    """把新浪 HTTP 换成样本，并返回 mock 以便断言"有没有打网络"。"""
    getter = MagicMock(return_value=_sina_hy_payload() if payload is None else payload)
    monkeypatch.setattr(sina_module, "http_get_text", getter)
    return getter


def _em_spot_raw() -> pd.DataFrame:
    """东财 spot 样本（回退路径用）。"""
    return pd.DataFrame(
        {
            "板块名称": ["小金属"],
            "板块代码": ["BK1027"],
            "最新价": [100.0],
            "涨跌幅": [3.2],
            "换手率": [2.5],
            "上涨家数": [80],
            "下跌家数": [5],
            "领涨股票": ["北方稀土"],
            "领涨股票-涨跌幅": [10.0],
        }
    )


# ── 1. 解析与归一化 ─────────────────────────────────────────────────────


def test_fetch_sina_industry_board_spot_df_normalizes_to_em_column_contract(monkeypatch):
    """新浪 13 字段串 → 东财列契约（下游只认这一套列名）。

    断言的是**列契约**本身：`_normalize_board_spot_df` 定义的那几个目标列必须都在，
    因为规则层（`mainline_scoring`）与格式化函数都按这些名字取值。
    """
    _patch_sina_payload(monkeypatch)

    df = fetch_sina_industry_board_spot_df()

    assert len(df) == 3
    for column in ("name", "code", "latest", "chg_1d", "leader", "leader_chg"):
        assert column in df.columns, f"缺少东财列契约里的 {column}"
    row = df.set_index("name").loc["玻璃行业"]
    assert row["code"] == "new_blhy"
    assert row["chg_1d"] == pytest.approx(0.061968838526912)
    assert row["latest"] == pytest.approx(17.846842105263)
    assert row["leader"] == "三峡新材"
    assert row["leader_chg"] == pytest.approx(10.119)


def test_fetch_sina_industry_board_spot_df_hits_the_sina_industry_endpoint(monkeypatch):
    getter = _patch_sina_payload(monkeypatch)

    fetch_sina_industry_board_spot_df()

    url = getter.call_args.args[0]
    assert "newSinaHy.php" in url
    assert getter.call_count == 1


def test_fetch_sina_industry_board_spot_df_rejects_unparsable_payload(monkeypatch):
    """拿不到就是拿不到：解析不出任何板块必须抛错（由路由去兜底），不许返回空表。"""
    _patch_sina_payload(monkeypatch, payload="<html>502 Bad Gateway</html>")

    with pytest.raises(Exception):
        fetch_sina_industry_board_spot_df()


# ── 2. 渲染：能被同一套证据抽取器吃下 ────────────────────────────────────


def test_sina_board_spot_render_is_eaten_by_the_shared_evidence_extractor(monkeypatch):
    """渲染文本必须能被 `evidence_from_rendered` 抽出可引用证据。

    为什么这条是硬要求：抽取器（`tools/data.py::_evidence_items_from_ranked_whitespace`）
    的判据是「表头第一个字段是 `排名`、行数一致、排名连续、**名称之后每个字段都是数字**」。
    东财的 spot 表格里夹着 `leader`（股票名）这类非数字中间列，所以按 Phase 3.2 的
    裁定东财路径**产不出证据**（见 `tests/test_table_evidence.py`）。新浪这条路
    因此只把**数值列**放进表格，领涨股走一行说明性文本——否则「拿到数据却 0 条证据」
    就成了一种新的、没有必要的沉默。
    """
    from tradingagents.tools.data import evidence_from_rendered

    _patch_sina_payload(monkeypatch)
    df = fetch_sina_industry_board_spot_df()

    text = format_sina_board_spot_ranking(df, sector_type="industry", top_n=10)

    assert "行业板块涨幅榜" in text
    assert "数据截止" in text
    assert "玻璃行业" in text
    # 领涨股没有丢：它在表格之外的说明行里（不参与数值证据）
    assert "三峡新材" in text
    # 排名必须连续，抽取器靠它自证列对齐（定宽表格会右对齐，所以按字段取）
    assert any(line.split()[:1] == ["1"] for line in text.splitlines())

    items = evidence_from_rendered(text, source_key="board_data")
    assert items, "新浪板块快照渲染文本没能产出任何证据"
    fields = [item.field for item in items]
    # 排序按涨跌幅降序 → 电子信息在最前
    assert fields[0].startswith("电子信息")
    assert any("涨跌幅" in field for field in fields)
    first = items[0]
    assert first.value == pytest.approx(1.19)
    assert first.unit == "", "百分数还是小数取决于源，猜单位比没有单位更糟"


def test_sina_board_spot_render_declares_the_fields_the_source_does_not_cover(monkeypatch):
    """源不提供的字段要**说出来**（未覆盖 ≠ 当日为 0）。

    新浪行业快照没有换手率/上涨家数/下跌家数。工具 docstring 承诺了这几个字段，
    如果渲染里不声不响地少几列，读的人只会以为"这些板块今天没有这些数"。
    """
    _patch_sina_payload(monkeypatch)
    df = fetch_sina_industry_board_spot_df()

    text = format_sina_board_spot_ranking(df, sector_type="industry")

    assert "换手率" in text and "上涨家数" in text
    assert "未覆盖" in text
    # 这句"未覆盖"不能把自己弄成一条故障 —— 否则整个工具会被判为数据源故障
    from tradingagents.tools.data import _looks_like_vendor_failure

    assert _looks_like_vendor_failure(text) is False


def test_sina_provider_get_board_spot_returns_rendered_text(monkeypatch):
    _patch_sina_payload(monkeypatch)

    text = CnSinaMoneyflowProvider().get_board_spot("industry")

    assert "行业板块涨幅榜" in text and "玻璃行业" in text


def test_sina_provider_get_board_spot_raises_so_the_router_can_fall_back(monkeypatch):
    """失败必须**抛**出去，而不是自己兜成一段话。

    供应商自己吞掉异常、返回一句"获取失败"，路由层会把它当成**成功**（`route_to_vendor`
    只在异常时才试下一个源），于是东财那条兜底永远轮不到。这是 `get_board_fund_flow`
    已有的写法（新浪的 `get_board_fund_flow` 同样不 catch），这里保持一致。
    """
    monkeypatch.setattr(
        sina_module, "http_get_text", MagicMock(side_effect=ConnectionError("sina down"))
    )

    with pytest.raises(ConnectionError):
        CnSinaMoneyflowProvider().get_board_spot("industry")


# ── 3. 链路优先级：新浪优先、东财兜底 ───────────────────────────────────


def test_board_data_chain_is_sina_then_eastmoney():
    """配置与路由链顺序：`cn_sina_moneyflow` 在前、`cn_akshare` 在后。"""
    from tradingagents.dataflows.interface import _resolve_vendor_chain
    from tradingagents.default_config import DEFAULT_CONFIG

    configured = DEFAULT_CONFIG["data_vendors"]["cn_board_data"]
    assert configured.split(",")[:2] == ["cn_sina_moneyflow", "cn_akshare"]

    chain = _resolve_vendor_chain("get_board_spot", configured)
    assert chain[:2] == ["cn_sina_moneyflow", "cn_akshare"]


def test_route_to_vendor_board_spot_prefers_sina_and_never_calls_eastmoney(monkeypatch):
    """新浪能服务时，东财**一次都不许**被调用。

    东财被封时每次调用都要先等完重试与退避（实测 3 次尝试），而结果注定是
    `RemoteDisconnected`；既然新浪已经拿到数据，多打一次只是白等。
    """
    from tradingagents.dataflows.interface import route_to_vendor

    _patch_sina_payload(monkeypatch)
    ak_calls = MagicMock(side_effect=AssertionError("新浪成功后不应调用东财"))
    monkeypatch.setattr(CnAkshareProvider, "_ak", lambda self: SimpleNamespace(stock_board_industry_spot_em=ak_calls))

    text = route_to_vendor("get_board_spot", "industry")

    assert "玻璃行业" in text
    assert ak_calls.call_count == 0


def test_route_to_vendor_board_spot_falls_back_to_eastmoney_when_sina_fails(monkeypatch):
    from tradingagents.dataflows.interface import route_to_vendor

    monkeypatch.setattr(
        sina_module, "http_get_text", MagicMock(side_effect=ConnectionError("sina down"))
    )
    em_call = MagicMock(return_value=_em_spot_raw())
    monkeypatch.setattr(
        CnAkshareProvider,
        "_ak",
        lambda self: SimpleNamespace(stock_board_industry_spot_em=em_call),
    )

    text = route_to_vendor("get_board_spot", "industry")

    assert em_call.call_count == 1
    assert "行业板块涨幅榜" in text and "小金属" in text


def test_route_to_vendor_board_spot_falls_back_to_eastmoney_for_concept(monkeypatch):
    """新浪不覆盖 concept（分类体系不同源，不可互换）→ 明确拒绝，由东财来服务。

    这就是「未覆盖」的正确形态：新浪**声明**自己不覆盖，而不是悄悄按默认值
    （industry）取一份数据回去交差。
    """
    from tradingagents.dataflows.interface import route_to_vendor

    getter = _patch_sina_payload(monkeypatch)
    em_call = MagicMock(return_value=_em_spot_raw())
    monkeypatch.setattr(
        CnAkshareProvider,
        "_ak",
        lambda self: SimpleNamespace(stock_board_concept_spot_em=em_call),
    )

    text = route_to_vendor("get_board_spot", "concept")

    assert em_call.call_count == 1
    assert "概念板块涨幅榜" in text
    assert getter.call_count == 0, "concept 未被覆盖，不该去打新浪按 industry 取数"


# ── 4. 未支持的 sector_type ─────────────────────────────────────────────


@pytest.mark.parametrize("sector_type", ["地域", "bogus", "concept_board"])
def test_sina_spot_rejects_unknown_sector_types_with_the_eastmoney_contract(
    monkeypatch, sector_type
):
    """东财列契约之外的取值 → `ValueError`（与 `fetch_board_spot_df` 同一个契约）。

    关键在**不许静默取默认值**：这一类取值如果被随手当成 `industry`，
    调用方会以为拿到的是自己要的那个板块集合。
    """
    getter = _patch_sina_payload(monkeypatch)

    with pytest.raises(ValueError) as excinfo:
        CnSinaMoneyflowProvider().get_board_spot(sector_type)

    message = str(excinfo.value)
    assert sector_type in message
    assert "industry" in message and "concept" in message
    assert getter.call_count == 0, "未支持的取值不得去打网络（更不得按默认值取数）"


def test_sina_spot_declares_the_known_but_uncovered_sector_type(monkeypatch):
    """`concept` 是**已知但未覆盖**：这是另一类信号，不是"认不出的取值"。

    未覆盖的取值必须显式声明，并且要点名谁覆盖它（东财），好让路由继续往下走，
    同时让看日志的人知道这个缺口是有意的、有主的选择，而不是漏了。
    """
    getter = _patch_sina_payload(monkeypatch)

    with pytest.raises(NotImplementedError) as excinfo:
        CnSinaMoneyflowProvider().get_board_spot("concept")

    message = str(excinfo.value)
    assert "未覆盖" in message, f"没有声明未覆盖：{message}"
    assert "concept" in message
    assert getter.call_count == 0, "未覆盖的取值不得去打网络（更不得按默认值取数）"


# ── 5. 两源皆失败：必须有不可用声明与原因 ───────────────────────────────


def _broken_eastmoney(monkeypatch):
    """东财：调用即失败。

    用 `ConnectionError` 作为替身：实测本机东财那条路的两种失败形状
    （走系统代理 `ProxyError`、强制直连 `RemoteDisconnected`）都属于
    `requests` 的 `ConnectionError` 家族，而 provider 的失败文案会带上异常类名，
    所以这里必须用一个**同族**异常，否则测的就不是真实文案的长度与形状。
    """
    monkeypatch.setattr(
        "tradingagents.dataflows.providers.cn_akshare_provider.time.sleep", lambda *_: None
    )
    monkeypatch.setattr(
        CnAkshareProvider,
        "_ak",
        lambda self: SimpleNamespace(
            stock_board_industry_spot_em=MagicMock(
                side_effect=ConnectionError("('Connection aborted.', RemoteDisconnected(...))")
            )
        ),
    )


def test_route_to_vendor_board_spot_returns_diagnosis_not_empty_when_every_source_fails(
    monkeypatch,
):
    """两源皆失败：返回的是**一句带原因的诊断**，不是空结果（也绝不静默成功）。"""
    from tradingagents.dataflows.interface import route_to_vendor

    monkeypatch.setattr(
        sina_module, "http_get_text", MagicMock(side_effect=ConnectionError("sina down"))
    )
    _broken_eastmoney(monkeypatch)

    text = route_to_vendor("get_board_spot", "industry")

    assert text.strip(), "两源皆失败却返回了空结果"
    assert "获取失败" in text
    assert "ConnectionError" in text


def test_board_spot_tool_declares_unavailable_with_reason_when_every_source_fails(monkeypatch):
    """工具面：`Coverage.unavailable` 非空且**含原因**、`degraded` 为真、证据为空。

    这是「拿不到 ≠ 没有」在工具输出上的判据。反过来那个被明令禁止的形状是
    `degraded=False` + `available=(category,)` + 0 条证据 —— "源可用、无未覆盖项、
    但没有数据"。`_coverage_for` 的注释（`tools/data.py`）把这条写成了不变量。
    """
    import tradingagents.tools.data as data_module

    from tradingagents.dataflows.interface import route_to_vendor

    monkeypatch.setattr(
        sina_module, "http_get_text", MagicMock(side_effect=ConnectionError("sina down"))
    )
    _broken_eastmoney(monkeypatch)
    rendered = route_to_vendor("get_board_spot", "industry")

    monkeypatch.setattr(data_module, "call_vendor", lambda *args, **kwargs: rendered)
    spec = {s.name: s for s in data_module.make_data_tools()}["get_board_spot"]

    result = spec.handler()

    assert result.evidence == ()
    assert result.coverage.available == (), "取不到数据却声称源可用"
    assert result.coverage.unavailable, "取不到数据却没有声明未覆盖"
    declared = result.coverage.unavailable[0]
    assert "board_data" in declared
    assert "板块涨幅榜获取失败" in declared
    assert "ConnectionError" in declared, f"声明里没有原因：{declared}"
    assert result.degraded is True


# ── 6. get_board_rank / get_board_hist：未覆盖与降级 ────────────────────


def test_sina_provider_declares_board_rank_uncovered(monkeypatch):
    """新浪的板块资金流只有「当日」一个截面，没有 3/5/10 日维度。

    而 `get_board_rank` 的契约带 `period`；同一个截面挂到第二个工具名下会让这个
    工具**随背后源不同而含义不同**（东财：行业+概念 × 4 个周期；新浪：仅行业、仅当日）。
    按「宁可少支持一个工具，也不许编数据」：显式声明未覆盖，让路由继续找东财。
    """
    getter = _patch_sina_payload(monkeypatch)

    with pytest.raises(NotImplementedError) as excinfo:
        CnSinaMoneyflowProvider().get_board_rank("industry", "1d")

    assert "未覆盖" in str(excinfo.value)
    assert "get_board_fund_flow" in str(excinfo.value), "应指出同期同数据该走哪个工具"
    assert getter.call_count == 0


def test_route_to_vendor_board_rank_falls_back_to_eastmoney(monkeypatch):
    """新浪声明未覆盖 → 路由继续尝试东财（而不是把整个工具判成不可用）。"""
    from tradingagents.dataflows.interface import route_to_vendor

    _patch_sina_payload(monkeypatch)
    flow_raw = pd.DataFrame(
        {
            "行业": ["小金属"],
            "行业指数": [1000.0],
            "行业-涨跌幅": [2.0],
            "流入资金": [10.0],
            "流出资金": [6.0],
            "净额": [4.0],
            "公司家数": [30],
            "领涨股": ["北方稀土"],
            "领涨股-涨跌幅": [10.0],
        }
    )
    rank_call = MagicMock(return_value=flow_raw)
    monkeypatch.setattr(
        CnAkshareProvider,
        "_ak",
        lambda self: SimpleNamespace(stock_sector_fund_flow_rank=rank_call),
    )

    text = route_to_vendor("get_board_rank", "industry", "5d")

    assert rank_call.call_count == 1
    assert "行业板块5日资金净流入" in text


def test_sina_provider_declares_board_hist_uncovered():
    """新浪没有板块日线历史源 —— 不许拿日线行情或别的板块冒充。"""
    with pytest.raises(NotImplementedError) as excinfo:
        CnSinaMoneyflowProvider().get_board_hist("玻璃行业", "industry")

    assert "未覆盖" in str(excinfo.value)


def test_board_hist_failure_is_declared_with_a_reason_and_no_alternative_source(monkeypatch):
    """东财板块历史取不到时：一句**人话**的不可用声明（含原因 + 无替代源）。

    板块历史只有东财一个源，所以这里的诚实要点有两条：说出**具体原因**
    （ConnectionError…），并说出**为什么没有兜底**（本平台没有第二个源）。
    """
    import tradingagents.tools.data as data_module

    from tradingagents.dataflows.interface import route_to_vendor

    _broken_eastmoney(monkeypatch)
    monkeypatch.setattr(
        CnAkshareProvider,
        "_ak",
        lambda self: SimpleNamespace(
            stock_board_industry_hist_em=MagicMock(side_effect=ConnectionError("em blocked"))
        ),
    )
    rendered = route_to_vendor("get_board_hist", "小金属", "industry")

    assert "获取失败" in rendered
    assert "无替代源" in rendered

    monkeypatch.setattr(data_module, "call_vendor", lambda *args, **kwargs: rendered)
    spec = {s.name: s for s in data_module.make_data_tools()}["get_board_hist"]

    result = spec.handler()

    assert result.evidence == ()
    assert result.coverage.unavailable, "板块历史取不到却没有声明未覆盖"
    declared = result.coverage.unavailable[0]
    assert "板块历史行情获取失败" in declared
    assert "ConnectionError" in declared, f"声明里没有原因：{declared}"
    assert result.degraded is True
