"""既有 19 个分析师工具的注册与委托（design D1/D2、tasks 4.4/4.5/4.7/4.11）。

## 迁移的目标与边界

* **目标**：能力定义只有一份。既有 19 个 `@tool` 直接调 `route_to_vendor`，
  新工具层也调同一处——否则「同一工具在分析图与对话/MCP 里行为不同」这类
  漂移必然发生，且极难排查。
* **边界**：**不改变既有分析图的行为**。工具名与返回的字符串必须逐字一致，
  否则 7 分析师管道会静默走样。

## 为什么 `data["structured"] is False`

这些 vendor 返回**预格式化的中文文本**，无法拆出可引用的标量。诚实做法是显式
标注「不可逐值引用」，让回答层在需要引用数字时改用结构化工具，而不是假装
这段文本可以被校验器核对。
"""

from __future__ import annotations

import pytest

from tradingagents.grounding.types import EvidenceItem


# 与 tests/test_tool_registry.py 的豁免名单一致；迁移后该名单应清空
LEGACY_NAMES = (
    "get_stock_data",
    "get_realtime_quotes",
    "get_fundamentals",
    "get_balance_sheet",
    "get_cashflow",
    "get_income_statement",
    "get_news",
    "get_global_news",
    "get_insider_transactions",
    "get_board_spot",
    "get_board_rank",
    "get_board_hist",
    "get_board_cons",
    "get_board_fund_flow",
    "get_individual_fund_flow",
    "get_lhb_detail",
    "get_zt_pool",
    "get_hot_stocks_xq",
    "get_indicators",
)


# ══════════════════════════════════════════════════════════════════════════
# 声明表是唯一来源
# ══════════════════════════════════════════════════════════════════════════


def test_every_legacy_tool_is_declared_exactly_once():
    from tradingagents.tools.data import DATA_TOOL_DECLS

    names = [d.name for d in DATA_TOOL_DECLS]
    assert sorted(names) == sorted(LEGACY_NAMES)
    assert len(names) == len(set(names)), "声明表存在重复条目"


def test_declarations_carry_schema_and_arg_order():
    from tradingagents.tools.data import DATA_TOOL_DECLS

    for decl in DATA_TOOL_DECLS:
        assert decl.schema["type"] == "object"
        # 参数顺序必须与 vendor 调用顺序一致——这是委托正确性的前提
        assert list(decl.schema["properties"]) == list(decl.args), decl.name
        # 无参工具（如 get_board_fund_flow）是合法的：args 与 properties 同时为空
        assert bool(decl.args) == bool(decl.schema["properties"]), decl.name


def test_required_args_match_declarations_without_defaults():
    from tradingagents.tools.data import DATA_TOOL_DECLS

    for decl in DATA_TOOL_DECLS:
        required = set(decl.schema.get("required", []))
        optional = {
            name
            for name, spec in decl.schema["properties"].items()
            if "default" in spec
        }
        # 无参工具走另一条断言路径，这里只校验有参工具
        if not decl.args:
            assert not required
            continue
        assert required | optional == set(decl.args), decl.name
        assert not (required & optional), decl.name


# ══════════════════════════════════════════════════════════════════════════
# 既有 @tool 委托到唯一实现
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("name", LEGACY_NAMES)
def test_legacy_tool_delegates_to_the_single_implementation(name, monkeypatch):
    """每个既有 `@tool` 必须委托到 `call_vendor`，而不是自己拼调用。

    这条断言是「工具定义单一来源」的可执行形式：任何人在 legacy 模块里
    手写一次 `route_to_vendor`，这里立刻失败。
    """
    from tradingagents.agents.utils import agent_utils
    from tradingagents.tools import data as data_module

    calls: list[tuple] = []

    def fake_call_vendor(tool_name, *args, **kwargs):
        calls.append((tool_name, args))
        return f"<rendered:{tool_name}>"

    monkeypatch.setattr(data_module, "call_vendor", fake_call_vendor)

    tool = getattr(agent_utils, name)
    assert tool.name == name

    # 用声明表构造一组合法参数
    decl = next(d for d in data_module.DATA_TOOL_DECLS if d.name == name)
    kwargs = {}
    for arg in decl.args:
        spec = decl.schema["properties"][arg]
        if spec.get("type") == "integer":
            kwargs[arg] = spec.get("default", 5)
        elif spec.get("type") == "array":
            kwargs[arg] = ["600519.SH"]
        else:
            kwargs[arg] = spec.get("default", "600519.SH")

    rendered = tool.func(**kwargs) if hasattr(tool, "func") else tool.invoke(kwargs)
    assert calls and calls[0][0] == name
    assert rendered == f"<rendered:{name}>"


def test_legacy_tool_arg_order_is_preserved(monkeypatch):
    """参数顺序必须按声明表传递——顺序错了会静默取到错误的数据。"""
    from tradingagents.tools import data as data_module

    seen: list[tuple] = []

    def fake_call_vendor(tool_name, *args, **kwargs):
        seen.append((tool_name, args))
        return "ok"

    monkeypatch.setattr(data_module, "call_vendor", fake_call_vendor)

    from tradingagents.agents.utils.technical_indicators_tools import get_indicators

    get_indicators.func(symbol="600519.SH", indicator="rsi", curr_date="2026-03-04", look_back_days=30)
    assert seen[0] == ("get_indicators", ("600519.SH", "rsi", "2026-03-04", 30))


# ══════════════════════════════════════════════════════════════════════════
# 结构化 ToolResult
# ══════════════════════════════════════════════════════════════════════════


def _spec(name, monkeypatch, rendered="表格内容"):
    from tradingagents.tools import data as data_module

    monkeypatch.setattr(data_module, "call_vendor", lambda *a, **k: rendered)
    tools = {s.name: s for s in data_module.make_data_tools()}
    return tools[name]


def test_data_tool_returns_tool_result_with_rendered_text(monkeypatch):
    spec = _spec("get_stock_data", monkeypatch, rendered="| date | close |")
    result = spec.handler(symbol="600519.SH", start_date="2026-01-01", end_date="2026-03-04")

    assert result.data["rendered"] == "| date | close |"
    assert isinstance(result.data, dict)
    assert result.data["vendor_args"]["symbol"] == "600519.SH"


def test_prose_vendor_is_explicitly_marked_non_citable(monkeypatch):
    """预格式化文本无法逐值引用 → 必须显式标注，而不是假装可校验。"""
    spec = _spec("get_stock_data", monkeypatch)
    result = spec.handler(symbol="600519.SH", start_date="2026-01-01", end_date="2026-03-04")

    assert result.data["structured"] is False
    assert "citable" in result.data["note"] or "引用" in result.data["note"]
    # 没有结构化标量 → 不得伪造证据项
    assert result.evidence == ()


def test_data_tool_declares_coverage(monkeypatch):
    spec = _spec("get_stock_data", monkeypatch)
    result = spec.handler(symbol="600519.SH", start_date="2026-01-01", end_date="2026-03-04")
    assert result.coverage.available
    assert result.coverage.unavailable == ()


def test_data_tool_records_as_of_from_its_args(monkeypatch):
    """as_of 必须来自参数（end_date / curr_date / date），不能读环境时钟——
    否则回放同一历史日期会得到不同的时间基准。"""
    spec = _spec("get_stock_data", monkeypatch)
    result = spec.handler(symbol="600519.SH", start_date="2026-01-01", end_date="2026-03-04")
    assert result.as_of == "2026-03-04"

    news = _spec("get_global_news", monkeypatch)
    assert news.handler(curr_date="2026-02-01").as_of == "2026-02-01"


def test_single_arg_data_tool_has_no_as_of(monkeypatch):
    """没有日期参数的工具不得凭空造一个 as_of。"""
    spec = _spec("get_board_fund_flow", monkeypatch)
    result = spec.handler()
    assert result.as_of == ""
    assert result.data["rendered"]


def test_data_tool_metadata_is_cheap_and_declares_vendor(monkeypatch):
    spec = _spec("get_stock_data", monkeypatch)
    assert spec.cost == "cheap"
    assert spec.latency_budget_ms > 0
    assert spec.output_contract == "DATA_PROSE"
    assert spec.input_schema["required"]


def test_data_tool_validates_arguments(monkeypatch):
    from tradingagents.tools import data as data_module
    from tradingagents.tools.registry import ToolRegistry

    monkeypatch.setattr(data_module, "call_vendor", lambda *a, **k: "x")
    registry = ToolRegistry()
    for spec in data_module.make_data_tools():
        registry.register(spec)

    with pytest.raises(ValueError, match="missing required"):
        registry.invoke("get_stock_data", symbol="600519.SH")

    # 类型错误必须在 handler 之前被拦下（用真实存在的参数名）
    with pytest.raises(ValueError, match="must be integer"):
        registry.invoke(
            "get_indicators", symbol="600519.SH", indicator="rsi",
            curr_date="2026-03-04", look_back_days="三十",
        )

    with pytest.raises(ValueError, match="unknown arguments"):
        registry.invoke(
            "get_stock_data", symbol="600519.SH", start_date="a", end_date="b", look_back=5
        )


# ══════════════════════════════════════════════════════════════════════════
# 注册与不变量
# ══════════════════════════════════════════════════════════════════════════


def test_all_legacy_tools_are_registered_by_default():
    """默认装配即包含 19 个数据工具——它们不依赖注入的端口工厂。"""
    from tradingagents.tools import build_registry

    names = set(build_registry().names())
    missing = [n for n in LEGACY_NAMES if n not in names]
    assert not missing, f"未注册: {missing}"


def test_no_langchain_tool_remains_exempt():
    """迁移后不存在豁免条目：所有 `@tool` 都必须在注册表中。

    豁免名单是本轮之前的临时状态；留着它等于让「工具定义单一来源」这条约束
    形同虚设——没人知道名单里的条目什么时候会变成幽灵。
    """
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

    from tradingagents.tools import build_registry

    registry = build_registry()
    unregistered = {n: w for n, w in found.items() if registry.get(n) is None}
    assert not unregistered, f"未注册的 @tool: {unregistered}"


def test_data_tools_are_usable_from_the_analysis_graph_path(monkeypatch):
    """分析图路径的行为必须不变：同一个名字、同一段字符串。"""
    from tradingagents.agents.utils import agent_utils
    from tradingagents.tools import data as data_module

    monkeypatch.setattr(data_module, "call_vendor", lambda *a, **k: "SAME-STRING")
    out = agent_utils.get_stock_data.func(
        symbol="600519.SH", start_date="2026-01-01", end_date="2026-03-04"
    )
    assert out == "SAME-STRING"
