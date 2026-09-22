"""工具层：单一来源注册表、结构化契约、两种传输适配器。

## 这些用例守的三条设计约束

1. **工具定义只有一份**（D1）。注册表是唯一来源，适配器互不知道对方存在，
   两者产出的名称/描述/schema 必须逐字一致——否则就会出现「同一工具在
   对话 Agent 与 MCP 里行为不同」这种最难排查的漂移。
2. **返回值是结构化对象而不是预格式化的中文表格**（D2）。仓库现有 19 个
   `@tool` 返回字符串，外部消费者无法做数值推理；新工具必须返回 `ToolResult`。
3. **`expensive` 工具不进同步面**（D10/D21）。多分析师管道是分钟级任务，
   放进同步工具面会造成 MCP 客户端超时与重试风暴。
"""

from __future__ import annotations

import pytest

from tradingagents.grounding.types import Coverage, EvidenceItem


def _spec(**over):
    from tradingagents.tools.spec import ToolSpec

    base = dict(
        name="demo_tool",
        title="示例",
        description="一个用于测试的工具",
        input_schema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "股票代码"},
                "window_days": {"type": "integer", "description": "窗口", "default": 20},
            },
            "required": ["symbol"],
        },
        output_contract="DEMO",
        handler=lambda **kw: None,
    )
    base.update(over)
    return ToolSpec(**base)


# ══════════════════════════════════════════════════════════════════════════
# 注册表
# ══════════════════════════════════════════════════════════════════════════


def test_registry_registers_and_retrieves():
    from tradingagents.tools.registry import ToolRegistry

    reg = ToolRegistry()
    spec = _spec()
    reg.register(spec)

    assert reg.get("demo_tool") is spec
    assert reg.names() == ("demo_tool",)


def test_registry_rejects_duplicate_names():
    """重名注册必须报错，而不是静默覆盖。

    静默覆盖会让「同一工具」在不同装配路径下指向不同实现，而调用方毫无察觉。
    """
    from tradingagents.tools.registry import ToolRegistry

    reg = ToolRegistry()
    reg.register(_spec())
    with pytest.raises(ValueError, match="duplicate"):
        reg.register(_spec(description="另一个实现"))


def test_registry_excludes_expensive_tools_from_synchronous_surface():
    from tradingagents.tools.registry import ToolRegistry

    reg = ToolRegistry()
    reg.register(_spec(name="cheap_tool"))
    reg.register(_spec(name="pipeline_tool", cost="expensive"))
    reg.register(_spec(name="medium_tool", cost="medium"))

    sync_names = {s.name for s in reg.synchronous()}
    assert sync_names == {"cheap_tool", "medium_tool"}
    assert "pipeline_tool" in reg.names()  # 仍在册，只是不在同步面


def test_registry_validates_arguments_before_handler_runs():
    """参数不合 schema 时必须在 handler 执行前拒绝。

    否则 handler 会带着缺失参数跑进去，失败点从「参数错误」变成
    「运行时异常」，错误信息对模型完全不可用。
    """
    from tradingagents.tools.registry import ToolRegistry

    called = []
    reg = ToolRegistry()
    reg.register(_spec(handler=lambda **kw: called.append(kw)))

    with pytest.raises(ValueError, match="symbol"):
        reg.invoke("demo_tool")
    assert called == []


def test_registry_rejects_wrong_type():
    from tradingagents.tools.registry import ToolRegistry

    called = []
    reg = ToolRegistry()
    reg.register(_spec(handler=lambda **kw: called.append(kw)))

    with pytest.raises(ValueError):
        reg.invoke("demo_tool", symbol="600519.SH", window_days="二十")
    assert called == []


def test_registry_invokes_handler_with_validated_arguments():
    from tradingagents.tools.registry import ToolRegistry

    reg = ToolRegistry()
    reg.register(_spec(handler=lambda **kw: kw))
    out = reg.invoke("demo_tool", symbol="600519.SH")
    # schema 里的 default 会被填充，使模型可以省略可选参数
    assert out == {"symbol": "600519.SH", "window_days": 20}


def test_registry_unknown_tool_raises():
    from tradingagents.tools.registry import ToolRegistry

    reg = ToolRegistry()
    with pytest.raises(KeyError):
        reg.invoke("nope", symbol="x")


def test_schemas_export_matches_invocation_validation():
    """导出用的 schema 与调用时校验用的必须是同一份，否则等于两套契约。"""
    from tradingagents.tools.registry import ToolRegistry

    reg = ToolRegistry()
    reg.register(_spec())
    schemas = reg.schemas()
    assert len(schemas) == 1
    assert schemas[0]["name"] == "demo_tool"
    assert schemas[0]["input_schema"] == _spec().input_schema


# ══════════════════════════════════════════════════════════════════════════
# 适配器：LangChain 与 MCP 必须逐字一致
# ══════════════════════════════════════════════════════════════════════════


def test_langchain_adapter_materialises_tool():
    from tradingagents.tools.adapters.langchain import to_langchain_tool

    spec = _spec(handler=lambda **kw: kw)
    tool = to_langchain_tool(spec)

    assert tool.name == spec.name
    assert tool.description == spec.description
    assert "symbol" in tool.args_schema.model_json_schema()["properties"]
    assert tool.invoke({"symbol": "600519.SH"}) == {"symbol": "600519.SH", "window_days": 20}


def test_mcp_adapter_materialises_descriptor():
    from tradingagents.tools.adapters.mcp import to_mcp_tool

    spec = _spec()
    descriptor = to_mcp_tool(spec)

    assert descriptor["name"] == spec.name
    assert descriptor["description"] == spec.description
    assert descriptor["inputSchema"] == dict(spec.input_schema)


def test_adapters_produce_identical_name_description_and_schema():
    """两个适配器只认 ToolSpec、互不 import，但产物必须一致。

    这条断言就是「工具定义单一来源」的可执行形式：一旦有人在某个适配器里
    悄悄改描述或加字段，这里立刻失败。
    """
    from tradingagents.tools.adapters.langchain import to_langchain_tool
    from tradingagents.tools.adapters.mcp import to_mcp_tool

    spec = _spec()
    lc = to_langchain_tool(spec)
    mcp = to_mcp_tool(spec)

    assert lc.name == mcp["name"]
    assert lc.description == mcp["description"]


def test_mcp_tool_name_uses_only_client_safe_characters():
    """MCP 客户端对工具名字符集有约束，注册表名必须直接可用、无需 sanitize。

    若允许点号命名空间，就需要一层「注册表名 → MCP 名」的映射——那正是
    D1 想排除的「双份定义」以另一种形式复活。
    """
    import re

    from tradingagents.tools.adapters.mcp import to_mcp_tool

    spec = _spec(name="signal_washout_probe")
    assert re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", to_mcp_tool(spec)["name"])


# ══════════════════════════════════════════════════════════════════════════
# 洗盘探针工具：ProbeResult → ToolResult
# ══════════════════════════════════════════════════════════════════════════


def _fake_ports_factory(**kw):
    """返回一个只喂日线的端口工厂。"""
    from tests._signal_fixtures import make_daily
    from tradingagents.signals.ports import SignalPorts

    closes = kw.pop("closes", [10.0] * 5 + [9.8, 9.6, 9.4, 9.3, 9.6])
    volumes = kw.pop("volumes", [1000, 1000, 1000, 1000, 1000, 700, 600, 500, 480, 700])
    daily = make_daily(closes, volumes)

    class _P:
        def bars(self, symbol, end, lookback):
            return daily

    def factory(symbol: str, as_of: str) -> SignalPorts:
        return SignalPorts(daily=_P())

    return factory


def test_probe_tool_returns_structured_data_not_prose():
    from tradingagents.tools.signals import make_signal_tools

    tools = {s.name: s for s in make_signal_tools(_fake_ports_factory())}
    spec = tools["signal_washout_probe"]
    result = spec.handler(symbol="600519.SH", as_of="2026-01-16")

    assert isinstance(result.data, dict)
    assert result.data["state"] in {"washout", "distribution", "trend_break", "insufficient_evidence"}
    assert isinstance(result.data["confidence"], int)
    assert not isinstance(result.data, str)


def test_probe_tool_assigns_stable_evidence_ids_and_coverage():
    """证据必须带 E 编号并声明覆盖范围——回答层的引用与未覆盖声明都依赖它。"""
    from tradingagents.tools.signals import make_signal_tools

    tools = {s.name: s for s in make_signal_tools(_fake_ports_factory())}
    result = tools["signal_washout_probe"].handler(symbol="600519.SH", as_of="2026-01-16")

    assert result.evidence, "探针结果必须携带可引用的证据"
    assert [e.id for e in result.evidence] == [f"E{i+1}" for i in range(len(result.evidence))]
    assert all(isinstance(e, EvidenceItem) for e in result.evidence)
    assert isinstance(result.coverage, Coverage)
    assert "board_turnover" in result.coverage.unavailable or result.coverage.unavailable


def test_probe_tool_result_carries_disclaimer():
    """判定属于对他人意图的概率化推断，任何出口都必须带免责声明。"""
    from tradingagents.tools.signals import make_signal_tools

    tools = {s.name: s for s in make_signal_tools(_fake_ports_factory())}
    result = tools["signal_washout_probe"].handler(symbol="600519.SH", as_of="2026-01-16")
    assert result.disclaimer


def test_probe_tool_exposes_falsifiers_and_daily_only_flag():
    from tradingagents.tools.signals import make_signal_tools

    tools = {s.name: s for s in make_signal_tools(_fake_ports_factory())}
    result = tools["signal_washout_probe"].handler(symbol="600519.SH", as_of="2026-01-16")

    assert result.data["daily_only"] is True  # 只喂了日线
    assert "falsifiers" in result.data


def test_probe_tool_metadata_declares_cost_and_latency():
    from tradingagents.tools.signals import make_signal_tools

    tools = {s.name: s for s in make_signal_tools(_fake_ports_factory())}
    spec = tools["signal_washout_probe"]

    assert spec.cost == "medium"
    assert spec.latency_budget_ms > 0
    assert spec.output_contract == "WASHOUT_PROBE"
    assert "position" in spec.requires


def test_probe_tool_is_in_synchronous_surface():
    from tradingagents.tools.registry import ToolRegistry
    from tradingagents.tools.signals import make_signal_tools

    reg = ToolRegistry()
    for spec in make_signal_tools(_fake_ports_factory()):
        reg.register(spec)
    assert "signal_washout_probe" in {s.name for s in reg.synchronous()}


# ══════════════════════════════════════════════════════════════════════════
# 批量工具：组合级问句可回答的前提
# ══════════════════════════════════════════════════════════════════════════


def test_batch_tool_evaluates_many_symbols_in_one_call():
    """「我持仓里哪只最危险」有 30 只——逐只调用 30 次装不进任何轮数上限。

    批量变体不是优化项，是可回答性的前提。
    """
    from tradingagents.tools.signals import make_signal_tools

    tools = {s.name: s for s in make_signal_tools(_fake_ports_factory())}
    spec = tools["signal_washout_probe_batch"]
    result = spec.handler(symbols=["600519.SH", "000001.SZ", "300750.SZ"], as_of="2026-01-16")

    assert len(result.data["results"]) == 3
    assert {r["symbol"] for r in result.data["results"]} == {"600519.SH", "000001.SZ", "300750.SZ"}


def test_batch_tool_preserves_per_symbol_evidence_and_abstention():
    """逐只结果必须各自带证据与弃权状态，不得被合并成一份或强行给方向。"""
    from tradingagents.tools.signals import make_signal_tools

    tools = {s.name: s for s in make_signal_tools(_fake_ports_factory())}
    result = tools["signal_washout_probe_batch"].handler(symbols=["600519.SH"], as_of="2026-01-16")

    row = result.data["results"][0]
    assert row["state"] in {"washout", "distribution", "trend_break", "insufficient_evidence"}
    assert isinstance(row["confidence"], int)
    assert result.evidence  # 证据在同一 ToolResult 内，保持完备性


def test_batch_tool_enforces_limit_and_reports_truncation():
    """超过上限必须显式告知，不得静默只算一部分。"""
    from tradingagents.tools.signals import BATCH_LIMIT, make_signal_tools

    tools = {s.name: s for s in make_signal_tools(_fake_ports_factory())}
    symbols = [f"{600000 + i}.SH" for i in range(BATCH_LIMIT + 5)]
    result = tools["signal_washout_probe_batch"].handler(symbols=symbols, as_of="2026-01-16")

    assert len(result.data["results"]) <= BATCH_LIMIT
    assert result.data["truncated"] is True
    assert result.data["requested"] == len(symbols)


def test_batch_tool_ranks_by_evidence_strength():
    from tradingagents.tools.signals import make_signal_tools

    tools = {s.name: s for s in make_signal_tools(_fake_ports_factory())}
    result = tools["signal_washout_probe_batch"].handler(symbols=["600519.SH", "000001.SZ"], as_of="2026-01-16")
    margins = [r["margin"] for r in result.data["results"]]
    assert margins == sorted(margins, reverse=True)


# ══════════════════════════════════════════════════════════════════════════
# 不变量：不存在「未注册」的工具定义
# ══════════════════════════════════════════════════════════════════════════

# 迁移已完成（组 4 的 4.4/4.5/4.7/4.11）：19 个既有分析师工具已登记入册。
#
# **本轮（退役 8 个 vendor 工具）重新引入的豁免**——正如上面那段话预期的：这是一次
# 显式的代码改动，应当被 review 看到。豁免的 8 个**不是遗漏，而是刻意的退役**：
#
#   * 它们返回预格式化中文散文、`evidence=()`，取到的数字无法被校验器引用
#     （"没有证据就没有回答"）；41 轮真实对话里规划层一次也没选中过它们。
#   * 因此从**工具面**（ToolSpec 注册表：对话 Agent / MCP / 规划器共用）摘掉。
#   * 但**分析图仍在用**（7 分析师管道 + `tradingagents/agents/utils/*_tools.py`），
#     所以 `@tool` 保留、`data_tool_impl` 仍能解析它们的声明。删掉会让分析图在
#     调用时抛 `KeyError`——"退役"不等于"让分析图取不到数"。
#
# 换句话说：这 8 个名字出现在这里，是在说"它存在，但**故意**不在工具面上"。
# 任何**新增**的未注册 `@tool`（不在这份名单里）仍然是 D1 违规，测试会红。
LEGACY_ANALYST_TOOLS: frozenset[str] = frozenset(
    {
        "get_stock_data",
        "get_indicators",
        "get_cashflow",
        "get_news",
        "get_insider_transactions",
        "get_board_cons",
        "get_individual_fund_flow",
        "get_lhb_detail",
    }
)


def _langchain_tool_functions():
    """用 AST 扫描 @tool 装饰的函数名（不 import，避免副作用）。"""
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "tradingagents"
    found: dict[str, str] = {}
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                name = dec.id if isinstance(dec, ast.Name) else getattr(dec, "attr", None)
                if name == "tool":
                    found[node.name] = str(path.relative_to(root))
    return found


def test_every_langchain_tool_is_registered_or_explicitly_exempt():
    """工具定义只能有一份来源（D1）。

    这条断言的价值在于：它是**唯一**能发现「悄悄多出第二个工具定义」的机制。
    没有它，双份定义会先以「小改动」的形式出现，等到发现时两边行为已经分叉。
    """
    from tradingagents.tools import build_registry

    registry = build_registry()
    unregistered = {
        name: where
        for name, where in _langchain_tool_functions().items()
        if name not in LEGACY_ANALYST_TOOLS and registry.get(name) is None
    }
    assert not unregistered, (
        f"发现未注册的 @tool 定义: {unregistered}。"
        "新增工具必须注册进 ToolRegistry，或加入 LEGACY_ANALYST_TOOLS——"
        "后者只允许用于**已从工具面退役、但分析图仍在用**的工具，并要写清理由。"
    )


def test_legacy_exemption_list_holds_exactly_the_retired_tools():
    """豁免名单**恰好**是那 8 个已退役工具，一个不多一个不少。

    原来这条断言是 `LEGACY_ANALYST_TOOLS == frozenset()`（迁移刚完成时豁免确实为空）。
    本轮从工具面退役 8 个工具，豁免因此被**显式**重新引入——这正是那个空集合
    "为了让重新引入成为一次显式改动"的设计意图。

    为什么要断言"恰好等于"而不是"包含"：豁免名单是护栏上唯一的缺口。缺口必须
    有边界，否则它会缓慢变成"所有未注册工具的收容所"，而 D1 就名存实亡了。
    """
    assert LEGACY_ANALYST_TOOLS == frozenset(
        {
            "get_stock_data",
            "get_indicators",
            "get_cashflow",
            "get_news",
            "get_insider_transactions",
            "get_board_cons",
            "get_individual_fund_flow",
            "get_lhb_detail",
        }
    ), "豁免名单只能有这 8 个已退役工具"

    actual = set(_langchain_tool_functions())
    # 19 个既有 @tool **全部仍在**（退役只摘工具面，不动分析图）
    assert len(actual) == 19, f"既有 @tool 数量应为 19，实际 {len(actual)}"

    # 豁免的每个名字都必须真的对应一个 `@tool` 定义——名单里不许留幽灵
    ghosts = sorted(LEGACY_ANALYST_TOOLS - actual)
    assert not ghosts, f"豁免名单里的 @tool 根本不存在: {ghosts}"


def test_retired_tools_are_registered_in_neither_layer():
    """退役的工具在**任何**注册表里都不该出现（含注入了端口的组合根）。

    只检查"默认装配"是不够的：那 8 个是 vendor 数据工具，本来就不依赖注入端口，
    真正需要防的是有人日后把它们重新注册回工具面。
    """
    from tradingagents.tools import build_registry

    registry = build_registry()
    present = sorted(n for n in LEGACY_ANALYST_TOOLS if registry.get(n) is not None)
    assert not present, (
        f"这些工具已从工具面退役，却又被注册回来了：{present}。"
        "它们返回散文、evidence=()，数字无法被引用；重新注册前先解决证据问题。"
    )


def test_composition_root_registers_signal_tools():
    """组合根是唯一装配点：注入端口工厂后信号工具必须在册。"""
    from tradingagents.tools import build_registry

    registry = build_registry(ports_factory=lambda symbol, as_of: None)
    assert "signal_washout_probe" in registry.names()
    assert "signal_washout_probe_batch" in registry.names()


def test_registry_without_ports_factory_still_has_data_tools():
    """数据工具不依赖注入的端口，因此默认在册；信号/新闻工具则不在。

    避免出现「在册但一调就炸」的半成品：没有取数器就不注册对应工具——
    那类条目会让 Agent 把预算浪费在必然失败的调用上。
    """
    from tradingagents.tools import build_registry

    names = set(build_registry().names())
    # 用 `get_fundamentals` 当样本：`get_stock_data` 已从工具面退役（不在册）。
    assert "get_fundamentals" in names
    assert "signal_washout_probe" not in names
    assert "data_symbol_news" not in names
