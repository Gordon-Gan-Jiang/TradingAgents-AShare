"""L1 决策工具：持仓清单读取。

## 本文件锁住什么

1. **只读事实**：每只持仓一条可引用证据（代码/数量/可用/成本/市值/占比），
   风险判断交给确定性信号层——"读了什么"与"判了什么"必须能分开追溯。
2. **「解析不到持仓」≠「空仓」**：前者必须声明未覆盖；否则模型会按空仓给建议，
   那是本平台明令禁止的失败形态（`position` 一直在覆盖声明里就是这个原因）。
3. **user_id 不经模型**：端口闭包绑定，参数里没有 user_id（否则等于给它越权的可能）。
4. 取数失败降级不抛错；未注入端口则不注册。
"""

from __future__ import annotations

import pytest


class _FakePort:
    def __init__(self, rows=None, *, error: Exception | None = None):
        self.rows = rows if rows is not None else []
        self.error = error
        self.calls: list[int] = []

    def list_holdings(self, *, limit):
        self.calls.append(limit)
        if self.error is not None:
            raise self.error
        return self.rows


ROWS = [
    {"symbol": "600519.SH", "security_name": "贵州茅台", "current_position": 100.0,
     "available_position": 100.0, "average_cost": 1180.5, "market_value": 125257.0,
     "current_position_pct": 32.5},
    {"symbol": "300750.SZ", "security_name": "宁德时代", "current_position": 200.0,
     "available_position": 0.0, "average_cost": 210.0, "market_value": 48000.0,
     "current_position_pct": 12.4},
]


def _tool(port):
    from tradingagents.tools.portfolio import make_portfolio_tools

    return {spec.name: spec for spec in make_portfolio_tools(port)}["portfolio_holdings_read"]


def test_holdings_become_citable_evidence():
    spec = _tool(_FakePort(ROWS))
    result = spec.handler(limit=20, as_of="2026-09-21")

    assert len(result.evidence) == 2
    first = str(result.evidence[0].value)
    assert "600519.SH" in first and "贵州茅台" in first
    assert "1180.5" in first or "1180.50" in first, "成本必须进证据（回答「浮亏多少」要用）"
    assert "可" in first, "可用数量必须进证据（可卖数量与持仓数量不同）"


def test_unresolved_holdings_are_declared_not_treated_as_empty():
    """「解析不到持仓」必须与「空仓」区分——否则模型会按空仓给建议。"""
    spec = _tool(_FakePort([]))
    result = spec.handler(limit=20, as_of="2026-09-21")

    assert result.evidence == ()
    assert result.coverage.unavailable, "必须声明未获取到持仓"
    assert "未获取到持仓" in " ".join(result.coverage.unavailable)


def test_read_failure_degrades_without_raising():
    spec = _tool(_FakePort(error=RuntimeError("db locked")))
    result = spec.handler(limit=5, as_of="2026-09-21")

    assert result.degraded is True
    assert result.coverage.unavailable == ("portfolio_holdings",)
    assert "db locked" not in str(result.data)


def test_limit_is_clamped_without_the_falsy_zero_trap():
    port = _FakePort(ROWS)
    spec = _tool(port)
    spec.handler(limit=0)
    assert port.calls[0] == 1, "limit=0 应夹到 1，而不是被 falsy 换成缺省值"
    spec.handler(limit=999)
    assert port.calls[1] == 30


def test_no_user_id_in_the_schema():
    spec = _tool(_FakePort(ROWS))
    assert "user_id" not in (spec.input_schema.get("properties") or {})


def test_tool_is_l1_and_only_registered_with_a_port():
    from tradingagents.tools import build_registry
    from tradingagents.tools.spec import PLAN_LAYER_DECISION

    spec = _tool(_FakePort(ROWS))
    assert spec.plan_layer == PLAN_LAYER_DECISION

    without = build_registry(ports_factory=lambda *a: object(), news_fetcher=lambda *a: [])
    assert "portfolio_holdings_read" not in without.names()

    with_port = build_registry(
        ports_factory=lambda *a: object(), news_fetcher=lambda *a: [],
        holdings_port=_FakePort(ROWS),
    )
    assert "portfolio_holdings_read" in with_port.names_by_layer(PLAN_LAYER_DECISION)
