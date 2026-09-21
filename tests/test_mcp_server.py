"""MCP 出口（传输中立核心）。

## 这些用例守的是 MCP 出口的三条硬约束

1. **只暴露同步只读工具**：`pipeline_*` 是分钟级任务，进同步工具面会造成
   客户端超时与重试风暴；写操作 V1 不开放。
2. **未鉴权不得执行**：任何一处忘记加鉴权依赖的挂载都会变成匿名读路径，
   所以核心层必须有兜底断言。
3. **命名即注册表名**：不做 sanitize 映射——映射表就是「双份定义」换了
   一种形式，正是要排除的漂移源。
"""

from __future__ import annotations

import re

import pytest

from tests._signal_fixtures import make_daily
from tradingagents.signals.ports import PeerSnapshot, SignalPorts


def _ports_factory():
    daily = make_daily([10.0] * 5 + [9.8, 9.6, 9.4, 9.3, 9.6],
                       [1000, 1000, 1000, 1000, 1000, 700, 600, 500, 480, 700])

    class _P:
        def bars(self, symbol, end, lookback):
            return daily

    class _Peer:
        def snapshot(self, symbol, day):
            return PeerSnapshot(board_name="白酒", board_return_pct=-3.0,
                                market_return_pct=-1.5, stock_turnover=2.2,
                                board_median_turnover=1.8)

    def factory(symbol: str, as_of: str) -> SignalPorts:
        return SignalPorts(daily=_P(), peer=_Peer())

    return factory


def _core(**kw):
    from api.mcp_server import build_core

    return build_core(ports_factory=_ports_factory(), **kw)


def _registry_with_expensive():
    from tradingagents.tools import build_registry
    from tradingagents.tools.spec import ToolSpec

    registry = build_registry(ports_factory=_ports_factory())
    registry.register(
        ToolSpec(
            name="analyze_pipeline",
            title="深度分析管道",
            description="分钟级多智能体研判",
            input_schema={"type": "object", "properties": {"symbol": {"type": "string"}}},
            output_contract="PIPELINE",
            handler=lambda **kw: None,
            cost="expensive",
        )
    )
    return registry


# ══════════════════════════════════════════════════════════════════════════
# 白名单
# ══════════════════════════════════════════════════════════════════════════


def test_expensive_pipeline_tool_is_not_exposed():
    from api.mcp_server import McpServerCore

    core = McpServerCore(_registry_with_expensive())
    names = {t["name"] for t in core.list_tools()}

    assert "signal_washout_probe" in names
    assert "signal_washout_probe_batch" in names
    assert "analyze_pipeline" not in names


def test_mutating_tools_are_not_exposed_even_if_registered():
    """写操作 V1 一律不开放：外部 Agent 改用户状态的爆炸半径过大。"""
    from api.mcp_server import McpServerCore
    from tradingagents.tools import build_registry
    from tradingagents.tools.spec import ToolSpec

    registry = build_registry(ports_factory=_ports_factory())
    for name in ("watchlist_add", "trade_ledger_append"):
        registry.register(
            ToolSpec(
                name=name, title=name, description=name,
                input_schema={"type": "object", "properties": {}},
                output_contract=name.upper(), handler=lambda **kw: None, cost="cheap",
            )
        )

    core = McpServerCore(registry)
    names = {t["name"] for t in core.list_tools()}
    assert "watchlist_add" not in names
    assert "trade_ledger_append" not in names


def test_explicit_allowlist_can_narrow_exposure():
    from api.mcp_server import McpServerCore

    core = McpServerCore(_registry_with_expensive(), exposes=["signal_washout_probe"])
    assert {t["name"] for t in core.list_tools()} == {"signal_washout_probe"}


def test_exposed_tool_names_are_client_safe():
    """MCP 客户端对工具名字符集有约束；注册表名必须直接可用。"""
    for tool in _core().list_tools():
        assert re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", tool["name"]), tool["name"]


def test_tool_descriptors_match_registry_definitions():
    """MCP 描述与注册表同源：任何一方改动描述都会在这里暴露。"""
    from tradingagents.tools import build_registry

    registry = build_registry(ports_factory=_ports_factory())
    from api.mcp_server import McpServerCore

    core = McpServerCore(registry)
    by_name = {t["name"]: t for t in core.list_tools()}
    assert by_name["signal_washout_probe"]["description"] == registry.get("signal_washout_probe").description
    assert by_name["signal_washout_probe"]["inputSchema"] == dict(
        registry.get("signal_washout_probe").input_schema
    )


# ══════════════════════════════════════════════════════════════════════════
# 鉴权与隔离
# ══════════════════════════════════════════════════════════════════════════


def test_invocation_without_user_is_refused():
    """兜底断言：没有解析出用户就不执行。

    缺了这条，任何一处忘记加鉴权依赖的挂载都会变成匿名读路径。
    """
    core = _core()
    with pytest.raises(PermissionError, match="authenticated"):
        core.invoke(name="signal_washout_probe", arguments={"symbol": "600519.SH", "as_of": "2026-01-16"}, user_id=None)


def test_invoking_unexposed_tool_is_refused_explicitly():
    from api.mcp_server import EXPOSURE_DENIED, McpServerCore

    core = McpServerCore(_registry_with_expensive())
    with pytest.raises(PermissionError, match=EXPOSURE_DENIED):
        core.invoke(name="analyze_pipeline", arguments={"symbol": "600519.SH"}, user_id="u1")


def test_invocation_returns_structured_result_with_disclaimer():
    core = _core()
    result = core.invoke(
        name="signal_washout_probe",
        arguments={"symbol": "600519.SH", "as_of": "2026-01-16"},
        user_id="u1",
    )
    assert result.data["state"] in {"washout", "distribution", "trend_break", "insufficient_evidence"}
    assert result.disclaimer
    assert result.evidence


def test_invocation_carries_freshness_and_coverage():
    core = _core()
    result = core.invoke(
        name="signal_washout_probe",
        arguments={"symbol": "600519.SH", "as_of": "2026-01-16"},
        user_id="u1",
    )
    # 未覆盖维度必须结构化返回，供外部 Agent 判断可信度
    assert result.coverage.unavailable
    assert result.degraded is True


# ══════════════════════════════════════════════════════════════════════════
# 审计
# ══════════════════════════════════════════════════════════════════════════


def test_invocations_are_audited_without_credentials():
    core = _core()
    core.invoke(
        name="signal_washout_probe",
        arguments={"symbol": "600519.SH", "as_of": "2026-01-16"},
        user_id="u1",
    )
    entries = core.audit.entries()
    assert len(entries) == 1
    assert entries[0].tool == "signal_washout_probe"
    assert entries[0].user_id == "u1"
    assert entries[0].ok is True
    # 审计条目只含工具名/用户/时长/结果，不含任何凭证字段
    assert not hasattr(entries[0], "token")
    assert "token" not in entries[0].arguments


def test_failed_invocations_are_audited():
    from api.mcp_server import McpServerCore

    core = McpServerCore(_registry_with_expensive())
    with pytest.raises(PermissionError):
        core.invoke(name="analyze_pipeline", arguments={}, user_id="u1")
    assert core.audit.entries()[0].ok is False
    assert core.audit.entries()[0].error


def test_usage_aggregates_per_tool():
    core = _core()
    for _ in range(3):
        core.invoke(
            name="signal_washout_probe",
            arguments={"symbol": "600519.SH", "as_of": "2026-01-16"},
            user_id="u1",
        )
    usage = core.audit.usage()
    assert usage["signal_washout_probe"]["calls"] == 3
    assert "p50_ms" in usage["signal_washout_probe"]


# ══════════════════════════════════════════════════════════════════════════
# 服务描述与挂载说明
# ══════════════════════════════════════════════════════════════════════════


def test_describe_reports_server_identity_and_tools():
    core = _core()
    described = core.describe()
    assert described["server"]
    assert described["version"]
    assert described["tools"]


def test_mount_instructions_document_the_lifespan_trap():
    """挂载陷阱必须以可读文本固化下来。

    FastMCP 的 lifespan 与 `app.mount` 组合有已知问题；写在注释里没人会看，
    做成函数后健康检查端点可以把它暴露出来，接入方在踩坑前就能看到。
    """
    from api.mcp_server import mount_instructions

    text = mount_instructions()
    assert "lifespan" in text
    assert "mcp" in text
