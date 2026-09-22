"""既有 19 个分析师工具的注册与委托（design D1/D2、tasks 4.4/4.5/4.7/4.11）。

## 迁移的目标与边界

* **目标**：能力定义只有一份。既有 19 个 `@tool` 直接调 `route_to_vendor`，
  新工具层也调同一处——否则「同一工具在分析图与对话/MCP 里行为不同」这类
  漂移必然发生，且极难排查。
* **边界**：**不改变既有分析图的行为**。工具名与返回的字符串必须逐字一致，
  否则 7 分析师管道会静默走样。

## 本轮变化：19 → 工具面 11 + 已退役 8

「能力定义只有一份」这条性质现在**只对 11 个成立**（指"同一份声明同时供工具面
与分析图使用"）。另外 8 个已从**工具面**退役，但**分析图仍然在用**，所以它们的
声明以 `RETIRED_DATA_TOOL_DECLS` 的形式保留，只喂 `data_tool_impl`。

这正是本文件要盯住的边界：**退役 ≠ 让分析图不能取数**。判据是两件事分开断言：

1. 8 个退役工具**不在** `DATA_TOOL_DECLS`（→ 不注册进 ToolSpec 工具面）；
2. 它们的 `@tool` **仍然存在、仍然委托**（→ 分析图逐字不变）。

退役理由见 `tradingagents/tools/data.py` 与 `routing.RETIRED_CAPABILITY_HINTS`：
它们返回预格式化散文、`evidence=()`，取到的数字无法被校验器引用——"没有证据就
没有回答"。41 轮真实对话里规划层也没有一次选中过它们。

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
    "get_realtime_quotes",
    "get_fundamentals",
    "get_balance_sheet",
    "get_income_statement",
    "get_global_news",
    "get_board_spot",
    "get_board_rank",
    "get_board_hist",
    "get_board_fund_flow",
    "get_zt_pool",
    "get_hot_stocks_xq",
)

# **已从工具面退役的 8 个**（本轮）。名单是显式的、不是"顺手指出来的"：
# 读到这里的人应当能立刻看出它们是有意摘除的，而不是被谁不小心删掉了。
#
# 三条判据都成立才允许出现在这里（缺一条都应当重新注册，而不是退役）：
#   1. 返回预格式化散文、`evidence=()` → 数字不可引用；
#   2. 有结构化等价落点（资金流→`capital_flow_read`、新闻→`data_symbol_news`…），
#      或该问句类型已明确不再承诺覆盖；
#   3. 分析图仍能取到数（`@tool` 保留）。
RETIRED_NAMES = (
    "get_stock_data",
    "get_indicators",
    "get_cashflow",
    "get_news",
    "get_insider_transactions",
    "get_board_cons",
    "get_individual_fund_flow",
    "get_lhb_detail",
)

# 分析图里的 `@tool` 全集：11 个在工具面 + 8 个已退役。
ALL_NAME = LEGACY_NAMES + RETIRED_NAMES


# ══════════════════════════════════════════════════════════════════════════
# 声明表是唯一来源（对 11 个在册工具）
# ══════════════════════════════════════════════════════════════════════════


def test_every_declared_tool_is_declared_exactly_once():
    from tradingagents.tools.data import DATA_TOOL_DECLS

    names = [d.name for d in DATA_TOOL_DECLS]
    assert sorted(names) == sorted(LEGACY_NAMES)
    assert len(names) == len(set(names)), "声明表存在重复条目"


def test_retired_tools_are_not_declared_on_the_tool_surface():
    """8 个退役工具**必须不在** `DATA_TOOL_DECLS` 里——这是"摘掉"的可执行形式。

    失败信息点名工具：只说"断言失败"的话，下一个人得自己 diff 才能知道是谁漂了。
    """
    from tradingagents.tools.data import DATA_TOOL_DECLS

    surface = {d.name for d in DATA_TOOL_DECLS}
    still_there = sorted(set(RETIRED_NAMES) & surface)
    assert not still_there, (
        f"这些工具已退役却仍在工具面上：{still_there}。"
        "它们返回散文、evidence=()，数字无法被引用；重新注册前先解决证据问题。"
    )


def test_retired_tools_keep_their_langchain_tool_for_the_analyst_pipeline():
    """退役**只**针对工具面：分析图的 `@tool` 必须还在，且仍能正确委托。

    这条是边界断言（boundary #1）。`agents/utils/*_tools.py` 是**另一个消费者**，
    它通过 `data_tool_impl(name) → _DECL_BY_NAME` 取参数顺序；声明若被彻底删掉，
    分析图会在调用时抛 `KeyError`——"从工具面摘掉"不等于"让分析图不能取数"。
    """
    from tradingagents.agents.utils import agent_utils
    from tradingagents.tools import data as data_module

    calls: list[tuple] = []
    saved = data_module.call_vendor
    data_module.call_vendor = lambda name, *args, **kw: (
        calls.append((name, args)) or f"<rendered:{name}>"
    )
    try:
        for name in RETIRED_NAMES:
            tool = getattr(agent_utils, name)
            assert tool.name == name, f"{name} 的 @tool 不见了（分析图会坏）"

            decl = next(d for d in data_module.RETIRED_DATA_TOOL_DECLS if d.name == name)
            kwargs = _valid_kwargs(decl)
            rendered = tool.func(**kwargs) if hasattr(tool, "func") else tool.invoke(kwargs)
            assert rendered == f"<rendered:{name}>", f"{name} 的委托返回变了"
    finally:
        data_module.call_vendor = saved

    assert [name for name, _args in calls] == list(RETIRED_NAMES), (
        f"委托次数或顺序不对：{[n for n, _ in calls]}"
    )


def _valid_kwargs(decl) -> dict:
    """按声明表的 schema 造一组合法参数（类型与默认值都从 schema 推导）。"""
    kwargs = {}
    for arg in decl.args:
        spec = decl.schema["properties"][arg]
        if spec.get("type") == "integer":
            kwargs[arg] = spec.get("default", 5)
        elif spec.get("type") == "array":
            kwargs[arg] = ["600519.SH"]
        else:
            kwargs[arg] = spec.get("default", "600519.SH")
    return kwargs


def test_declarations_carry_schema_and_arg_order():
    from tradingagents.tools.data import DATA_TOOL_DECLS

    for decl in DATA_TOOL_DECLS:
        assert decl.schema["type"] == "object"
        # 参数顺序必须与 vendor 调用顺序一致——这是委托正确性的前提
        assert list(decl.schema["properties"]) == list(decl.args), decl.name
        # 无参工具（如 get_board_fund_flow）是合法的：args 与 properties 同时为空
        assert bool(decl.args) == bool(decl.schema["properties"]), decl.name


def test_retired_declarations_keep_schema_and_arg_order():
    """退役声明的参数顺序同样要正确——分析图的委托仍按它传位置参数。"""
    from tradingagents.tools.data import RETIRED_DATA_TOOL_DECLS

    assert len(RETIRED_DATA_TOOL_DECLS) == len(RETIRED_NAMES)
    for decl in RETIRED_DATA_TOOL_DECLS:
        assert list(decl.schema["properties"]) == list(decl.args), decl.name
        assert bool(decl.args) == bool(decl.schema["properties"]), decl.name


def test_required_args_match_declarations_without_defaults():
    from tradingagents.tools.data import ALL_DATA_TOOL_DECLS

    for decl in ALL_DATA_TOOL_DECLS:
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


@pytest.mark.parametrize("name", ALL_NAME)
def test_legacy_tool_delegates_to_the_single_implementation(name, monkeypatch):
    """每个既有 `@tool` 必须委托到 `call_vendor`，而不是自己拼调用。

    这条断言是「工具定义单一来源」的可执行形式：任何人在 legacy 模块里
    手写一次 `route_to_vendor`，这里立刻失败。

    参数化覆盖 **19 个**（含 8 个退役的）：退役只影响工具面，分析图的委托路径
    必须逐字不变。
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

    # 用声明表构造一组合法参数（退役的 8 个在 RETIRED_DATA_TOOL_DECLS 里）
    decl = next(d for d in data_module.ALL_DATA_TOOL_DECLS if d.name == name)
    kwargs = _valid_kwargs(decl)

    rendered = tool.func(**kwargs) if hasattr(tool, "func") else tool.invoke(kwargs)
    assert calls and calls[0][0] == name
    assert rendered == f"<rendered:{name}>"


def test_legacy_tool_arg_order_is_preserved(monkeypatch):
    """参数顺序必须按声明表传递——顺序错了会静默取到错误的数据。

    用 `get_indicators` 作样本：它**已从工具面退役**，但分析图仍按这份声明传
    位置参数，所以顺序约束对它同样成立（这正是退役声明必须保留的原因）。
    """
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

# 样本工具从 `get_stock_data` 换成 `get_fundamentals`：后者是**在册**的散文型
# 工具，同样带日期参数（`curr_date` → `as_of`），因此下面这些断言的语义不变。
# 继续用退役工具做样本会让测试依赖一个不在工具面上的东西。
_SPECIMEN = "get_fundamentals"
_SPECIMEN_ARGS = {"ticker": "600519.SH", "curr_date": "2026-03-04"}


def _spec(name, monkeypatch, rendered="表格内容"):
    from tradingagents.tools import data as data_module

    monkeypatch.setattr(data_module, "call_vendor", lambda *a, **k: rendered)
    tools = {s.name: s for s in data_module.make_data_tools()}
    return tools[name]


def test_data_tool_returns_tool_result_with_rendered_text(monkeypatch):
    spec = _spec(_SPECIMEN, monkeypatch, rendered="| date | close |")
    result = spec.handler(**_SPECIMEN_ARGS)

    assert result.data["rendered"] == "| date | close |"
    assert isinstance(result.data, dict)
    assert result.data["vendor_args"]["ticker"] == "600519.SH"


def test_prose_vendor_is_explicitly_marked_non_citable(monkeypatch):
    """预格式化文本无法逐值引用 → 必须显式标注，而不是假装可校验。"""
    spec = _spec(_SPECIMEN, monkeypatch)
    result = spec.handler(**_SPECIMEN_ARGS)

    assert result.data["structured"] is False
    assert "citable" in result.data["note"] or "引用" in result.data["note"]
    # 没有结构化标量 → 不得伪造证据项
    assert result.evidence == ()


def test_data_tool_declares_coverage(monkeypatch):
    spec = _spec(_SPECIMEN, monkeypatch)
    result = spec.handler(**_SPECIMEN_ARGS)
    assert result.coverage.available
    assert result.coverage.unavailable == ()


def test_data_tool_records_as_of_from_its_args(monkeypatch):
    """as_of 必须来自参数（end_date / curr_date / date），不能读环境时钟——
    否则回放同一历史日期会得到不同的时间基准。"""
    spec = _spec(_SPECIMEN, monkeypatch)
    result = spec.handler(**_SPECIMEN_ARGS)
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
    spec = _spec(_SPECIMEN, monkeypatch)
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
        registry.invoke(_SPECIMEN)

    # 类型错误必须在 handler 之前被拦下（`get_global_news` 的 look_back_days 是整数）
    with pytest.raises(ValueError, match="must be integer"):
        registry.invoke(
            "get_global_news", curr_date="2026-03-04", look_back_days="三十",
        )

    # 拼写错误必须被拒——**不能**静默丢弃后退回默认值。
    # `get_fundamentals` 的声明参数是 `ticker` / `curr_date`，所以 `currdate`
    # 与 `ticker_x` 都是明显的拼错（前缀关系 / 编辑距离小）。
    # 静默丢掉它们的后果是"问的是 A、执行的是默认的 B"，且毫无痕迹。
    for mistyped in ({"currdate": "2026-03-04"}, {"ticker_x": "600519.SH"}):
        with pytest.raises(ValueError, match="unknown arguments"):
            registry.invoke(_SPECIMEN, ticker="600519.SH", **mistyped)

    # 但**与任何声明参数都不同源**的外来键只是被丢弃，不让整次调用失败。
    # 实测（2026-09-22，问题「贵州茅台市盈率多少」）规划器给出的参数是
    # ticker + curr_date（齐全且正确）+ 一个多带出来的 symbol；原有硬拒让整次
    # 调用抛 ValueError → 0 条证据 → 用户看到「本轮无可用证据」。
    # 模型因为"多给了一个信息"而受罚，而且这是必然重演的行为。
    result = registry.invoke(
        _SPECIMEN, ticker="600519.SH", curr_date="2026-03-04", symbol="600519.SH"
    )
    assert result is not None, "外来键不该让整次调用失败"
    assert registry.last_dropped_arguments == ("symbol",), "丢弃必须在 trace 里可见"


# ══════════════════════════════════════════════════════════════════════════
# 注册与不变量
# ══════════════════════════════════════════════════════════════════════════


def test_declared_tools_are_registered_by_default():
    """默认装配即包含在册的 11 个数据工具——它们不依赖注入的端口工厂。"""
    from tradingagents.tools import build_registry

    names = set(build_registry().names())
    missing = [n for n in LEGACY_NAMES if n not in names]
    assert not missing, f"未注册: {missing}"


def test_retired_tools_are_not_registered_by_default():
    """反向断言：退役的 8 个**不得**出现在工具面（默认装配下也不许）。"""
    from tradingagents.tools import build_registry

    names = set(build_registry().names())
    present = sorted(set(RETIRED_NAMES) & names)
    assert not present, (
        f"这些工具已退役却仍在工具面上：{present}。"
        "它们返回散文、evidence=()，数字无法被引用；退役是刻意的，不是遗漏。"
    )


def test_langchain_tools_are_exempt_only_when_retired():
    """豁免**只**允许给这 8 个退役工具；其余 `@tool` 都必须在注册表中。

    豁免名单是本轮之前的临时状态；留着它等于让「工具定义单一来源」这条约束
    形同虚设——没人知道名单里的条目什么时候会变成幽灵。

    本轮豁免的是**退役**这 8 个：它们的存在有明确、可执行的理由（分析图仍在用），
    而不是"暂时先放着"。名单外出现任何未注册的 `@tool` 都是 bug。
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
    unregistered = {
        n: w
        for n, w in found.items()
        if registry.get(n) is None and n not in RETIRED_NAMES
    }
    assert not unregistered, (
        f"未注册的 @tool（且不在退役名单里）: {unregistered}"
    )

    # 退役名单必须真的对应着存在的 `@tool`：名单本身不许留幽灵
    ghosts = sorted(set(RETIRED_NAMES) - set(found))
    assert not ghosts, f"退役名单里的 @tool 根本不存在: {ghosts}"


def test_data_tools_are_usable_from_the_analysis_graph_path(monkeypatch):
    """分析图路径的行为必须不变：同一个名字、同一段字符串。

    用**已退役**的 `get_stock_data` 作样本是刻意的：工具面摘掉它之后，分析图
    仍必须能取到同样一段字符串。
    """
    from tradingagents.agents.utils import agent_utils
    from tradingagents.tools import data as data_module

    monkeypatch.setattr(data_module, "call_vendor", lambda *a, **k: "SAME-STRING")
    out = agent_utils.get_stock_data.func(
        symbol="600519.SH", start_date="2026-01-01", end_date="2026-03-04"
    )
    assert out == "SAME-STRING"
