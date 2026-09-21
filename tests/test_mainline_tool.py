"""L1 决策工具：市场主线（板块强弱）。

## 本文件锁住什么

1. **结论来自平台、不可改写**：主线是平台的判定产出（定时任务），Agent 只读取与
   解释。模型临场"推一条主线"等于把平台结论与即兴编造混为一谈。
2. **板块强弱要看得到名次与涨跌幅**：这是主线的核心内容，也是"哪个板块最强"的
   直接依据——只给一句概述等于没给依据。
3. **没有报告要显式声明**：不能空手而归，更不能编一个主线。
4. 取数失败降级；未注入端口不注册。
"""

from __future__ import annotations

import pytest


class _FakePort:
    def __init__(self, rows=None, *, error: Exception | None = None):
        self.rows = rows if rows is not None else []
        self.error = error
        self.calls: list[int] = []

    def list_runs(self, *, limit):
        self.calls.append(limit)
        if self.error is not None:
            raise self.error
        return self.rows


ROWS = [
    {
        "trade_date": "2026-09-19", "status": "completed",
        "summary": "资金向算力与半导体集中",
        "boards": [
            {"name": "半导体", "change_pct": 3.2},
            {"name": "算力", "change_pct": 2.7},
        ],
    }
]


def _tool(port):
    from tradingagents.tools.portfolio import make_mainline_tools

    return {spec.name: spec for spec in make_mainline_tools(port)}["market_mainline_view"]


def test_mainline_becomes_citable_evidence_with_board_ranking():
    spec = _tool(_FakePort(ROWS))
    result = spec.handler(limit=3, as_of="2026-09-21")

    assert result.evidence, "主线必须产出证据"
    value = str(result.evidence[0].value)
    assert "半导体" in value and "3.2" in value, "板块名次与涨跌幅必须可引用"
    assert "2026-09-19" in value, "报告日期必须可引用（否则无从判断这是哪天的主线）"


def test_missing_report_is_declared_not_invented():
    spec = _tool(_FakePort([]))
    result = spec.handler(limit=3, as_of="2026-09-21")

    assert result.evidence == ()
    assert result.coverage.unavailable, "没有主线报告必须显式声明，不能编一条"


def test_read_failure_degrades_without_raising():
    spec = _tool(_FakePort(error=RuntimeError("db down")))
    result = spec.handler(limit=3, as_of="2026-09-21")

    assert result.degraded is True
    assert result.coverage.unavailable == ("market_mainline",)
    assert "db down" not in str(result.data)


def test_limit_clamped_without_falsy_zero_trap():
    port = _FakePort(ROWS)
    spec = _tool(port)
    spec.handler(limit=0)
    assert port.calls[0] == 1
    spec.handler(limit=99)
    assert port.calls[1] == 10


def test_tolerates_missing_board_payload():
    """报告里没有板块明细时也要能出证据（只有概述也算事实）。"""
    spec = _tool(_FakePort([{"trade_date": "2026-09-19", "status": "completed",
                            "summary": "普涨"}]))
    result = spec.handler(limit=1, as_of="2026-09-21")
    assert result.evidence and "普涨" in str(result.evidence[0].value)


def test_tool_is_l1_and_only_registered_with_a_port():
    from tradingagents.tools import build_registry
    from tradingagents.tools.spec import PLAN_LAYER_DECISION

    spec = _tool(_FakePort(ROWS))
    assert spec.plan_layer == PLAN_LAYER_DECISION

    without = build_registry(ports_factory=lambda *a: object(), news_fetcher=lambda *a: [])
    assert "market_mainline_view" not in without.names()

    with_port = build_registry(
        ports_factory=lambda *a: object(), news_fetcher=lambda *a: [],
        mainline_port=_FakePort(ROWS),
    )
    assert "market_mainline_view" in with_port.names_by_layer(PLAN_LAYER_DECISION)
