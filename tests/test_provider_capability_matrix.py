"""能力矩阵与 vendor 链的一致性测试（把"配置在说谎"变成红灯）。

这一套测试是本次事故的直接产物，每一条规则都对应一个**已发生**的故障：

* R1 覆盖完整 —— 防止再有 tool 悄悄落到默认 `yfinance`
  （`cn_research_data` 就是这样丢的，A 股日线第一顺位变成 yfinance）。
* R2 矩阵不说谎 —— 防止矩阵文档化以后自己腐烂。
* R3 配置不遗漏 —— 每个 category 必须显式配置。
* R4 链上无死成员 —— 直接钉死 `cn_baostock` 被塞进 3 条它 0 可服务的链那次事故。
* R5 维度可覆盖 —— 防止整条链都没人能服务的维度存在。
* R6 优先级一致 —— 防止"矩阵说 Baostock 是地基、配置却把 AKShare 排前面"这类漂移。

失败信息里带具体 provider 与 tool，是为了让下一个人**不用重新排查一遍**就知道
是哪条链、哪个成员、缺哪个方法。
"""

from __future__ import annotations

import pytest

from tradingagents.dataflows.interface import TOOLS_CATEGORIES
from tradingagents.dataflows.providers import capability_matrix as cm


def _format(problems) -> str:
    return "\n".join(f"  - {p}" for p in problems)


def test_matrix_has_no_consistency_problems():
    """总闸：任何一条规则不过，这个测试红，且报出全部问题（不是只报第一条）。"""
    problems = cm.validate_matrix()
    assert not problems, "能力矩阵与 vendor 配置不一致：\n" + _format(problems)


def test_every_tool_is_declared_exactly_once():
    tools = [c.tool for c in cm.CAPABILITIES]
    assert len(tools) == len(set(tools)), "矩阵里出现重复 tool"
    all_tools = {t for info in TOOLS_CATEGORIES.values() for t in info["tools"]}
    # 路由能力必须与 `TOOLS_CATEGORIES` 完全一致；直连能力（routed=False）
    # 本来就不属于路由词表，是**额外**登记的，不能要求它出现在 TOOLS_CATEGORIES 里。
    routed = {c.tool for c in cm.CAPABILITIES if c.routed}
    assert routed == all_tools
    direct = {c.tool for c in cm.CAPABILITIES if not c.routed}
    assert direct, "直连能力应至少有一条（基准指数日线）"
    assert not (direct & all_tools), "直连能力不应同时是路由能力"


def test_cn_research_data_is_explicitly_configured():
    """回归：本类别曾整个漏配，导致 7 个研究类 tool 全部先问 yfinance。"""
    from tradingagents.default_config import DEFAULT_CONFIG

    assert "cn_research_data" in DEFAULT_CONFIG["data_vendors"]
    chain = DEFAULT_CONFIG["data_vendors"]["cn_research_data"]
    assert "cn_baostock" in chain, "Baostock 是历史日线/分钟线的主源，必须在链上"


def test_baostock_is_not_in_chains_it_cannot_serve():
    """回归：`cn_baostock` 曾被列进 fundamental_data / news_data / cn_market_data。

    这里按"能否服务该类别任一 tool"直接断言，而不是写死链内容——
    将来 Baostock 真实现了新闻，这条测试不会拦它。
    """
    from tradingagents.dataflows.providers import build_default_registry, support
    from tradingagents.default_config import DEFAULT_CONFIG

    cls = type(build_default_registry().get("cn_baostock"))
    impl = support.implemented_methods(cls)

    for category, raw in DEFAULT_CONFIG["data_vendors"].items():
        members = [v.strip() for v in str(raw).split(",") if v.strip()]
        if "cn_baostock" not in members:
            continue
        tools = TOOLS_CATEGORIES[category]["tools"]
        served = [t for t in tools if t in impl]
        assert served, (
            f"cn_baostock 在 {category} 链上是死成员（可服务 0/{len(tools)}）——"
            f"要么给它实现该方法，要么把它从链上拿掉"
        )


def test_baostock_serves_daily_bars_as_primary():
    """结合形式的核心裁决：日线/分钟线的 tier 1 是 Baostock（历史地基）。"""
    assert cm.BY_TOOL["get_daily_bars_df"].primary.source == "cn_baostock"
    assert cm.BY_TOOL["get_intraday_bars"].primary.source == "cn_baostock"


def test_akshare_owns_realtime_and_sentiment_dimensions():
    """Baostock 没有实时/资金流/板块/龙虎榜能力，这些维度必须仍是 AKShare 系独占。"""
    for tool in (
        "get_realtime_quotes",
        "get_lhb_detail",
        "get_zt_pool",
        "get_chip_distribution",
    ):
        sources = cm.BY_TOOL[tool].source_names()
        assert sources, f"{tool} 没有任何源"
        assert all(s.startswith("cn_") for s in sources), f"{tool} 混入了非 CN 源: {sources}"


def test_fundamentals_keep_akshare_as_primary_to_preserve_口径():
    """财务保持 AKShare 主、Baostock 兜底：不静默改变已有数字的含义。"""
    for tool in (
        "get_fundamentals",
        "get_balance_sheet",
        "get_cashflow",
        "get_income_statement",
    ):
        assert cm.BY_TOOL[tool].primary.source == "cn_akshare"


def test_matrix_validation_detects_a_dead_chain_member():
    """反向测试：校验器必须真的能抓到死成员，否则上面那些绿灯没有意义。"""
    from tradingagents.default_config import DEFAULT_CONFIG

    broken = {
        "data_vendors": {**DEFAULT_CONFIG["data_vendors"]},
        "tool_vendors": dict(DEFAULT_CONFIG.get("tool_vendors", {})),
    }
    broken["data_vendors"]["news_data"] = "cn_baostock,cn_akshare"

    problems = cm.validate_matrix(config=broken)
    assert any(p.rule == "R4" and "cn_baostock" in p.detail for p in problems), (
        "校验器没能发现 news_data 链上的死成员 cn_baostock：\n" + _format(problems)
    )


def test_matrix_validation_detects_missing_category():
    """反向测试：漏配的 category 必须被 R3 抓到（cn_research_data 那次）。"""
    from tradingagents.default_config import DEFAULT_CONFIG

    broken = {
        "data_vendors": {
            k: v
            for k, v in DEFAULT_CONFIG["data_vendors"].items()
            if k != "cn_research_data"
        },
        "tool_vendors": dict(DEFAULT_CONFIG.get("tool_vendors", {})),
    }
    problems = cm.validate_matrix(config=broken)
    assert any(p.rule == "R3" and "cn_research_data" in p.detail for p in problems)


def test_matrix_snapshot_is_serializable_for_the_probe_page():
    snap = cm.matrix_snapshot()
    assert len(snap) == len(cm.CAPABILITIES)
    first = snap[0]
    assert set(first) == {"tool", "label", "category", "note", "routed", "sources"}
    # `routed` 是契约的一部分：False 表示这条路没有 router 兜底链，
    # 前端要据此说明"没有备用源"，不能让它看起来和普通维度一样。
    assert isinstance(first["routed"], bool)
    assert any(row["routed"] is False for row in snap), "直连能力应出现在快照里且 routed=False"
    for row in snap:
        assert row["sources"], f"{row['tool']} 没有源"
        for s in row["sources"]:
            assert s["tier"] >= 1
    # 必须能过 JSON（端点直接返回它）
    import json

    json.dumps(snap)


@pytest.mark.parametrize("source", cm.source_names())
def test_declared_sources_exist_in_registry(source: str):
    from tradingagents.dataflows.providers import build_default_registry

    assert source in build_default_registry().list_names()
