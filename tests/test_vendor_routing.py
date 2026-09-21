"""新数据源接入 vendor 路由（tasks 1.3）。

## 为什么必须走 `route_to_vendor`

既有 19 个工具都经 `route_to_vendor` 调用，从而共享**三级降级链**
（akshare → 东财直连 → 新浪，按配置与可用性自动回退）与失败冷却。本轮新增的
分时/筹码/资金流/新闻若直连 akshare，就享受不到任何降级：akshare 一抖，
整个维度不可得，而东财直连明明可用。

## 边界

取数实现属于 **provider 层**（`tradingagents/dataflows/providers/`），不属于
应用层——引擎包不得 import `api/`（由架构边界测试强制）。因此本轮的搬运是
把 akshare 适配从 `api/services/` 移入 provider 层，应用层只做装配。
"""

from __future__ import annotations

import pandas as pd
import pytest


# ══════════════════════════════════════════════════════════════════════════
# 路由注册
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "method",
    ["get_intraday_bars", "get_chip_distribution", "get_symbol_news", "get_daily_bars_df"],
)
def test_new_sources_are_routable(method):
    """新方法必须在 vendor 类别表里，否则 `route_to_vendor` 直接抛错。"""
    from tradingagents.dataflows.interface import get_category_for_method

    assert get_category_for_method(method)


def test_new_methods_share_a_category_so_one_config_covers_them():
    """同一类数据放在同一类别下，使用户配置一处即可调整全部。"""
    from tradingagents.dataflows.interface import get_category_for_method

    categories = {
        get_category_for_method(m)
        for m in ("get_intraday_bars", "get_chip_distribution", "get_symbol_news", "get_daily_bars_df")
    }
    assert len(categories) == 1


# ══════════════════════════════════════════════════════════════════════════
# 降级链真的生效
# ══════════════════════════════════════════════════════════════════════════


def test_router_falls_back_to_the_next_provider(monkeypatch):
    """首个 provider 抛 NotImplementedError 时必须回退到下一个。

    这就是走 `route_to_vendor` 的全部意义：单个数据源不可用不等于该维度不可得。
    """
    from tradingagents.dataflows import interface as iface

    calls: list[str] = []

    class _First:
        name = "first"

        def get_chip_distribution(self, symbol, day):
            calls.append("first")
            raise NotImplementedError("not supported by this provider")

    class _Second:
        name = "second"

        def get_chip_distribution(self, symbol, day):
            calls.append("second")
            return "筹码数据"

    class _Registry:
        def list_names(self):
            return ["first", "second"]

        def get(self, name):
            return {"first": _First(), "second": _Second()}[name]

    monkeypatch.setattr(iface, "_registry", _Registry())
    monkeypatch.setattr(iface, "get_vendor", lambda category, method=None: "first")

    out = iface.route_to_vendor("get_chip_distribution", "600519.SH", "2026-03-04")
    assert out == "筹码数据"
    assert calls == ["first", "second"]


def test_router_reports_a_provider_that_lacks_the_method(monkeypatch):
    """provider 没有该方法时跳过它——不能因为一个 stub provider 就整体失败。"""
    from tradingagents.dataflows import interface as iface

    class _Stub:
        name = "stub"

    class _Real:
        name = "real"

        def get_symbol_news(self, symbol, start, end):
            return []

    class _Registry:
        def list_names(self):
            return ["stub", "real"]

        def get(self, name):
            return {"stub": _Stub(), "real": _Real()}[name]

    monkeypatch.setattr(iface, "_registry", _Registry())
    monkeypatch.setattr(iface, "get_vendor", lambda category, method=None: "stub")

    assert iface.route_to_vendor("get_symbol_news", "600519.SH", "2026-03-01", "2026-03-04") == []


# ══════════════════════════════════════════════════════════════════════════
# provider 层提供实现
# ══════════════════════════════════════════════════════════════════════════


def test_akshare_provider_exposes_the_new_methods():
    from tradingagents.dataflows.providers.cn_akshare_provider import CnAkshareProvider

    provider = CnAkshareProvider()
    for method in ("get_intraday_bars", "get_chip_distribution", "get_symbol_news"):
        assert callable(getattr(provider, method, None)), method


def test_provider_fetching_functions_are_importable_from_the_engine_layer():
    """取数实现必须在引擎层，而不是应用层——否则引擎无法独立复用这些能力。"""
    from tradingagents.dataflows.providers import cn_research_data

    for fn in (
        "fetch_intraday_frame",
        "fetch_chip_frame",
        "fetch_flow_frame",
        "fetch_peer_frames",
        "fetch_stock_turnover",
        "fetch_symbol_news",
    ):
        assert callable(getattr(cn_research_data, fn, None)), fn


def test_engine_layer_does_not_import_the_application_layer():
    """搬运后仍必须守住分层：引擎包不得反向依赖 api/。"""
    import ast
    import pathlib

    path = pathlib.Path("tradingagents/dataflows/providers/cn_research_data.py")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith("api"), node.module
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("api"), alias.name


# ══════════════════════════════════════════════════════════════════════════
# 应用层经路由取数
# ══════════════════════════════════════════════════════════════════════════


def test_news_service_goes_through_the_vendor_router(monkeypatch):
    """新闻取数必须经路由，从而共享降级链与失败冷却。"""
    from tradingagents.dataflows import interface as iface
    from tradingagents.dataflows.providers import cn_research_data

    seen: list[tuple] = []

    def fake_route(method, *args, **kwargs):
        seen.append((method,) + args)
        return []

    monkeypatch.setattr(iface, "route_to_vendor", fake_route)
    result = cn_research_data.fetch_symbol_news_via_router("600519.SH", "2026-03-01", "2026-03-04")
    assert seen and seen[0][0] == "get_symbol_news"
    assert result == []
