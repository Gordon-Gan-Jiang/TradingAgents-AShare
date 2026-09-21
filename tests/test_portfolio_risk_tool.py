"""L1 决策工具：组合体检（风险排序）。

## 这个测试锁住什么

1. **排序口径不由模型定**：证据顺序必须与服务层给出的顺序**逐条一致**——
   工具若重排（比如按代码排序），"哪只最危险"就会随实现漂移。
2. **约束必须可引用**：T+1 / 整手 / 涨跌停这些"不可执行"的理由不能丢，
   否则回答会把"今天卖不掉"说成"不用卖"。
3. **三种"空"必须分开**：没有持仓行（未导入）与有持仓但评估不出来（行情不可用）
   是两件事，声明文案必须不同；取数抛错则是第三种（degraded）。
4. 上限收窄含 `limit=0`（不得被 `or` 换成默认值）；schema 里没有 user_id；
   层为 L1；未注入端口不注册、注入后在册。
"""

from __future__ import annotations

import pytest


class _FakeRiskPort:
    def __init__(self, payload=None, *, error: Exception | None = None):
        self.payload = payload
        self.error = error
        self.calls: list[int] = []

    def risk_review(self, *, limit: int):
        self.calls.append(limit)
        if self.error is not None:
            raise self.error
        return self.payload


DECISIONS = [
    {
        "symbol": "600519.SH", "name": "贵州茅台", "action": "减仓", "priority": "high",
        "reasons": ["浮亏超过纪律阈值", "跌破计划止损位"], "triggers": ["跌破止损"],
        "suggested_shares": 30, "suggested_pct": 30.0, "price": 1700.0,
        "previous_close": 1720.0, "pnl_pct": -8.16, "position": 100.0,
        "available_position": 0.0, "average_cost": 1850.0,
        "constraints": ["T+1 可用 0 股", "不足一手"], "plan_available": True,
        "plan_direction": "long", "anchor_gate": "ANCHOR_OK", "is_actionable_sell": False,
    },
    {
        "symbol": "300750.SZ", "name": "宁德时代", "action": "观察", "priority": "low",
        "reasons": [], "triggers": [], "suggested_shares": 0, "suggested_pct": 0.0,
        "price": 210.0, "previous_close": 211.0, "pnl_pct": -2.3, "position": 200.0,
        "available_position": 200.0, "average_cost": 215.0, "constraints": [],
        "plan_available": False, "plan_direction": None, "anchor_gate": None,
        "is_actionable_sell": False,
    },
]

SUMMARY = {
    "total": 2, "清仓": 0, "减仓": 1, "持有": 0, "观察": 1,
    "high_priority": 1, "with_plan": 1, "without_plan": 1,
}


def _payload(items=None, *, position_rows=2):
    return {
        "items": DECISIONS if items is None else items,
        "summary": SUMMARY,
        "total": len(DECISIONS if items is None else items),
        "position_rows": position_rows,
    }


def _tool(port):
    from tradingagents.tools.risk import make_risk_tools

    return {spec.name: spec for spec in make_risk_tools(port)}["portfolio_risk_review"]


def test_risk_review_becomes_citable_evidence():
    spec = _tool(_FakeRiskPort(_payload()))
    result = spec.handler(limit=10, as_of="2026-09-21")

    first = str(result.evidence[0].value)
    assert "600519.SH" in first and "贵州茅台" in first, "标的必须可引用"
    assert "减仓" in first and "high" in first, "处置动作与优先级必须可引用"
    assert "-8.16" in first, "浮亏必须可引用（同级排序的依据就是它）"
    assert "T+1 可用 0 股" in first, "A 股约束必须可引用，否则会把「卖不掉」说成「不用卖」"
    joined = " ".join(str(item.value) for item in result.evidence)
    assert "共 2 只" in joined, "组合级汇总必须可引用"
    assert "排序口径" in " ".join(item.field for item in result.evidence), (
        "排序口径必须可引用，否则「哪只最危险」无法复核"
    )


def test_risk_review_preserves_service_ordering():
    """工具不得重排：服务层的「优先级 → 浮亏」排序就是结论本身。"""
    reversed_items = list(reversed(DECISIONS))
    spec = _tool(_FakeRiskPort(_payload(reversed_items)))
    result = spec.handler(limit=10, as_of="2026-09-21")

    first = str(result.evidence[0].value)
    assert "300750.SZ" in first, "证据顺序必须与端口给出的顺序一致（不得自行排序）"


def test_risk_review_no_positions_is_declared_as_unknown_not_flat():
    """没有持仓行 = 「未获取到持仓」，不能读成「你没有风险」。"""
    spec = _tool(_FakeRiskPort(_payload([], position_rows=0)))
    result = spec.handler(limit=10, as_of="2026-09-21")

    assert result.evidence == ()
    assert "portfolio_risk" in result.coverage.available
    unavailable = " ".join(result.coverage.unavailable)
    assert "未获取到持仓" in unavailable, "必须声明「不知道」，而不是空仓"


def test_risk_review_positions_but_unevaluable_is_a_different_abstention():
    """有持仓却评估不出来（行情不可用）与「没有持仓」必须给不同的声明。"""
    spec = _tool(_FakeRiskPort(_payload([], position_rows=3)))
    result = spec.handler(limit=10, as_of="2026-09-21")

    unavailable = " ".join(result.coverage.unavailable)
    assert "无法评估" in unavailable, "这是取数/评估失败，不是「没有持仓」"
    assert "未获取到持仓" not in unavailable


def test_risk_review_unparsable_payload_degrades_not_empty():
    spec = _tool(_FakeRiskPort("not-a-mapping"))
    result = spec.handler(limit=10, as_of="2026-09-21")

    assert result.degraded is True
    assert result.coverage.unavailable == ("portfolio_risk",)
    assert "not-a-mapping" not in str(result.data)


def test_risk_review_read_failure_degrades_without_raising():
    spec = _tool(_FakeRiskPort(error=RuntimeError("exit engine exploded")))
    result = spec.handler(limit=10, as_of="2026-09-21")

    assert result.degraded is True
    assert result.coverage.unavailable == ("portfolio_risk",)
    assert "exit engine exploded" not in str(result.data)
    assert "exit engine exploded" not in str(result.degraded_reason)


def test_risk_review_limit_is_clamped_including_zero():
    port = _FakeRiskPort(_payload())
    spec = _tool(port)
    spec.handler(limit=999)
    assert port.calls[0] == 30

    spec.handler(limit=0)
    assert port.calls[1] == 1, "limit=0 不得被 `or` 换成默认值"


def test_risk_review_states_missing_plan_anchor():
    """没有交易计划 = 纪律锚点缺失，必须说出来（否则减仓建议没有可复核依据）。"""
    spec = _tool(_FakeRiskPort(_payload()))
    result = spec.handler(limit=10, as_of="2026-09-21")

    second = str(result.evidence[1].value)
    assert "无交易计划" in second


def test_risk_review_schema_has_no_user_id():
    spec = _tool(_FakeRiskPort(_payload()))
    assert "user_id" not in (spec.input_schema.get("properties") or {})


def test_risk_review_is_l1_and_only_registered_with_a_port():
    from tradingagents.tools import build_registry
    from tradingagents.tools.spec import PLAN_LAYER_DECISION

    spec = _tool(_FakeRiskPort(_payload()))
    assert spec.plan_layer == PLAN_LAYER_DECISION
    assert 5000 <= spec.latency_budget_ms <= 15000
    assert spec.freshness_sources == ("portfolio_risk",)

    without = build_registry(ports_factory=lambda *a: object(), news_fetcher=lambda *a: [])
    assert "portfolio_risk_review" not in without.names()

    with_port = build_registry(
        ports_factory=lambda *a: object(), news_fetcher=lambda *a: [],
        risk_port=_FakeRiskPort(_payload()),
    )
    assert "portfolio_risk_review" in with_port.names()
    assert "portfolio_risk_review" in with_port.names_by_layer(PLAN_LAYER_DECISION)
