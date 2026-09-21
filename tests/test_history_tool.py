"""L1 决策工具：判定历史读取（含兑现）。

## 这个测试锁住什么

1. **结论来自平台、不可改写**：证据是逐条历史判定（含 T+3/T+5 兑现），
   模型只能引用，不能重新生成一个结论。
2. **没有记录时如实声明**：返回 `unavailable`，而不是空列表当成功——
   把"还没有判定记录"说成"查过了、没问题"是最坏的失败形态。
3. **取数失败降级而非抛错**：抛错会让循环记一条工具失败并继续，模型面对
   空证据自由发挥。
4. **用户维度不经过模型**：端口在闭包里绑定 user_id，工具参数里没有 user_id，
   否则等于给模型越权的可能。
5. 工具在 L1（决策层），且注册表的层清单能列到它。
"""

from __future__ import annotations

import pytest


class _FakePort:
    def __init__(self, rows=None, *, error: Exception | None = None):
        self.rows = rows if rows is not None else []
        self.error = error
        self.calls: list[dict] = []

    def recent_verdicts(self, *, limit, symbol=None):
        self.calls.append({"limit": limit, "symbol": symbol})
        if self.error is not None:
            raise self.error
        return self.rows


def _tool(port):
    from tradingagents.tools.history import make_history_tools

    return {spec.name: spec for spec in make_history_tools(port)}["verdict_history_read"]


ROWS = [
    {
        "symbol": "600519.SH", "as_of": "2026-09-15", "state": "distribution",
        "confidence": 85, "outcome": {"t3_return_pct": -3.2, "t5_return_pct": -5.1},
    },
    {
        "symbol": "300750.SZ", "as_of": "2026-09-10", "state": "washout",
        "confidence": 70, "outcome": {},
    },
]


def test_history_becomes_citable_evidence():
    spec = _tool(_FakePort(ROWS))
    result = spec.handler(limit=10, symbol=None, as_of="2026-09-21")

    assert len(result.evidence) == 2
    first = str(result.evidence[0].value)
    assert "600519.SH" in first and "distribution" in first
    assert "-3.2" in first or "-3.20" in first, "兑现数据必须进证据，否则「兑现了吗」无从回答"


def test_no_records_is_declared_not_silently_empty():
    spec = _tool(_FakePort([]))
    result = spec.handler(limit=10, symbol=None, as_of="2026-09-21")

    assert result.evidence == ()
    assert "verdict_history" in result.coverage.available
    assert result.coverage.unavailable, "没有记录必须声明，不能当成「没问题」"


def test_read_failure_degrades_without_raising():
    spec = _tool(_FakePort(error=RuntimeError("db locked")))
    result = spec.handler(limit=5, symbol=None, as_of="2026-09-21")

    assert result.degraded is True
    assert result.coverage.unavailable == ("verdict_history",)
    assert "db locked" not in str(result.data), "上游原文不进对外结果"


def test_limit_is_clamped():
    port = _FakePort(ROWS)
    spec = _tool(port)
    spec.handler(limit=999, symbol=None)
    assert port.calls[0]["limit"] == 50

    spec.handler(limit=0, symbol=None)
    assert port.calls[1]["limit"] == 1


def test_symbol_filter_is_passed_through():
    port = _FakePort(ROWS)
    spec = _tool(port)
    spec.handler(limit=3, symbol="600519.SH")
    assert port.calls[0]["symbol"] == "600519.SH"


def test_no_user_id_in_the_schema():
    """用户维度由端口闭包绑定——参数里出现 user_id 等于给模型越权的可能。"""
    spec = _tool(_FakePort(ROWS))
    assert "user_id" not in (spec.input_schema.get("properties") or {})


def test_tool_is_l1_and_registered_when_injected():
    from tradingagents.tools import build_registry
    from tradingagents.tools.spec import PLAN_LAYER_DECISION

    spec = _tool(_FakePort(ROWS))
    assert spec.plan_layer == PLAN_LAYER_DECISION

    assert "verdict_history_read" not in build_registry(
        ports_factory=lambda *a: object(), news_fetcher=lambda *a: []
    ).names(), "没有端口就不该注册（不留「在册但一调就炸」的条目）"

    registry = build_registry(
        ports_factory=lambda *a: object(), news_fetcher=lambda *a: [],
        history_port=_FakePort(ROWS),
    )
    assert "verdict_history_read" in registry.names()
    assert "verdict_history_read" in registry.names_by_layer(PLAN_LAYER_DECISION)


# ── 权重版本（L1）─────────────────────────────────────────────────────────


class _FakeWeights:
    def __init__(self, active=None, versions=None, *, error=None):
        self._active = active
        self._versions = versions or []
        self._error = error

    def active_version(self):
        if self._error:
            raise self._error
        return self._active

    def list_versions(self):
        return self._versions


def _weight_tool(port):
    from tradingagents.tools.history import make_weight_tools

    return {spec.name: spec for spec in make_weight_tools(port)}["signal_weight_read"]


def test_active_weight_version_is_evidence():
    spec = _weight_tool(
        _FakeWeights(active="v3", versions=[{"version": "v3", "activated_at": "2026-09-01",
                                             "source": "calibration"}])
    )
    result = spec.handler(as_of="2026-09-21")
    joined = " ".join(f"{i.field}={i.value}" for i in result.evidence)
    assert "v3" in joined


def test_no_active_version_is_declared_as_prior_weights():
    """「没有激活版本」本身是事实：此时用的是内置先验，必须说出来而不是留空。"""
    spec = _weight_tool(_FakeWeights(active=None, versions=[]))
    result = spec.handler(as_of="2026-09-21")

    assert result.evidence, "不能空手而归——「没有激活版本」也要成为一条可引用事实"
    assert "先验" in str(result.evidence[0].value)
    assert result.coverage.unavailable == (), (
        "「正在用先验权重」是可回答的事实，不是未覆盖——声明成未覆盖会多出一条噪音"
    )


def test_weight_read_failure_degrades():
    spec = _weight_tool(_FakeWeights(error=RuntimeError("db down")))
    result = spec.handler(as_of="2026-09-21")
    assert result.degraded is True
    assert result.coverage.unavailable == ("signal_weights",)
    assert "db down" not in str(result.data)


def test_weight_tool_is_l1_and_registered():
    from tradingagents.tools import build_registry
    from tradingagents.tools.spec import PLAN_LAYER_DECISION

    spec = _weight_tool(_FakeWeights(active="v1"))
    assert spec.plan_layer == PLAN_LAYER_DECISION

    registry = build_registry(
        ports_factory=lambda *a: object(), news_fetcher=lambda *a: [],
        weight_port=_FakeWeights(active="v1"),
    )
    assert "signal_weight_read" in registry.names_by_layer(PLAN_LAYER_DECISION)


def test_history_port_reads_the_real_service_key():
    """端口必须读 `list_verdicts` 的**实际**返回键。

    事故记录：`_history_port` 曾写 `payload.get("verdicts")`，而该服务返回
    `{"items": [...], "count": n}`——键不存在 → 永远空列表 →
    `verdict_history_read` 一直报「尚未落库的判定记录」，而用户其实有判定。
    这是本平台最不能接受的失败形态：**「读不到」被说成「没有」**，且完全静默。

    这条测试用**真实服务签名**（键名 items）打桩，因此键名再写错就会红。
    """
    import api.services.chat_service as cs
    from api.services import signal_service

    calls: list[dict] = []

    def _fake_list_verdicts(*, user_id, symbol=None, limit=50):
        calls.append({"user_id": user_id, "limit": limit})
        return {"items": [{"symbol": "600519.SH", "as_of": "2026-09-15",
                           "state": "distribution", "confidence": 85}], "count": 1}

    original = signal_service.list_verdicts
    signal_service.list_verdicts = _fake_list_verdicts  # type: ignore[assignment]
    try:
        # 直接构造工具用的端口（build_registry 内部闭包同一实现）
        registry = cs.build_registry(user_id="u1")
        spec = registry.get("verdict_history_read")
        result = spec.handler(limit=5, as_of="2026-09-21")
    finally:
        signal_service.list_verdicts = original  # type: ignore[assignment]

    assert calls, "端口没有调用服务"
    assert result.evidence, (
        "端口读错键名会让工具永远报「没有判定」——这正是要防的静默失败"
    )
    assert "600519.SH" in str(result.evidence[0].value)
