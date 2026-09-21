"""架构边界护栏。

## 为什么这些测试必须存在

设计文档里的分层约定如果不变成会失败的测试，三个月内就会腐化：
新人（或未来的自己）在信号层里加一次网络请求、把新路由写进 `api/main.py`、
或者再定义一份工具，都不会有任何即时反馈。

## 为什么用 AST 而不是字符串匹配

字符串扫描会误判——本包的模块文档字符串里就写着「不 import requests /
akshare / openai」，子串匹配会把文档当成违规。用 `ast` 解析真实的
import 节点，才是在检查代码而不是检查注释。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SIGNALS_DIR = REPO_ROOT / "tradingagents" / "signals"
GROUNDING_DIR = REPO_ROOT / "tradingagents" / "grounding"
ENGINE_DIR = REPO_ROOT / "tradingagents"
API_DIR = REPO_ROOT / "api"

# 信号层禁止出现的一切：网络、行情 SDK、LLM SDK、Agent 框架
BANNED_MODULES = {
    "requests",
    "httpx",
    "urllib",
    "urllib3",
    "akshare",
    "baostock",
    "yfinance",
    "openai",
    "anthropic",
    "google",
    "langchain_core",
    "langchain_openai",
    "langchain_anthropic",
    "langchain_google_genai",
    "langgraph",
}

# 环境时钟：信号层的时间只能由 as_of 显式传入，否则回放无法复现
BANNED_CALLS = {
    ("datetime", "now"),
    ("datetime", "today"),
    ("datetime", "utcnow"),
    ("time", "time"),
    ("time", "monotonic"),
}


def _py_files(root: pathlib.Path):
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _imported_modules(tree: ast.AST) -> set[str]:
    """收集真实 import 语句里的顶层模块名。"""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # 相对导入属于包内，不在禁止之列
                continue
            if node.module:
                found.add(node.module.split(".")[0])
    return found


def _call_names(tree: ast.AST) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            value = node.func.value
            if isinstance(value, ast.Name):
                found.add((value.id, node.func.attr))
    return found


# ── C3：信号层必须纯净 ────────────────────────────────────────────────────


@pytest.mark.parametrize("path", _py_files(SIGNALS_DIR), ids=lambda p: f"signals/{p.name}")
def test_signals_has_no_io_or_llm_imports(path: pathlib.Path):
    """信号层内禁止网络、行情 SDK、LLM SDK 与 Agent 框架。

    这条是洗盘判定可回测的**机械保障**：纯函数不是靠自觉，而是构建时断言。
    任何人在信号层偷偷加一次网络请求或 LLM 调用，这里立刻失败。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations = _imported_modules(tree) & BANNED_MODULES
    assert not violations, f"{path.name} 违反信号层纯函数约束，导入了: {sorted(violations)}"


@pytest.mark.parametrize("path", _py_files(SIGNALS_DIR), ids=lambda p: f"signals/{p.name}")
def test_signals_has_no_ambient_clock(path: pathlib.Path):
    """禁止读环境时钟：时间只能由 as_of 传入。

    否则回放历史某一天时会混入「现在」，同一份输入在不同时刻给出不同输出，
    校准闭环失去意义。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations = _call_names(tree) & BANNED_CALLS
    assert not violations, f"{path.name} 读取了环境时钟: {sorted(violations)}"


def test_signals_does_not_import_tool_or_application_layer():
    """信号层不知道 ToolSpec 存在，也不碰应用层。

    工具契约（ToolResult / Coverage）与领域契约（ProbeResult）的转换发生在
    `tradingagents/tools/signals.py`；把转换写进信号层会让「加一种传输」
    变成要改领域代码。
    """
    for path in _py_files(SIGNALS_DIR):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("tradingagents.tools"), path.name
                assert not node.module.startswith("api"), path.name
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("tradingagents.tools"), path.name
                    assert not alias.name.startswith("api."), path.name


# ── C2：引擎包不得反向依赖应用层 ──────────────────────────────────────────


@pytest.mark.parametrize("path", _py_files(ENGINE_DIR), ids=lambda p: p.name)
def test_tradingagents_never_imports_api(path: pathlib.Path):
    """`tradingagents/` 是纯引擎包，`api/` 是本仓库的应用层。

    该边界目前是干净的（实测零反向依赖），但它只是一种「巧合」——
    一旦有人为了省事在 graph 里 import 一个 service，引擎就无法独立测试、
    也无法作为库被复用。用测试把它固定下来。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "api":
            pytest.fail(f"{path.relative_to(REPO_ROOT)} 反向依赖了 api 层")
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "api":
                    pytest.fail(f"{path.relative_to(REPO_ROOT)} 反向依赖了 api 层")


# ── C4：新路由不得写进入口模块 ────────────────────────────────────────────


def test_new_endpoints_are_not_declared_in_main():
    """本变更新增的端点必须走 APIRouter，只写 api/routers/*.py。

    `api/main.py` 已有 9591 行且零 `APIRouter` 使用。对话/信号端点如果再往
    入口模块里加，就是第三次重复同一个错误，所以在这里硬性拦住。
    """
    main_src = (API_DIR / "main.py").read_text(encoding="utf-8")
    for endpoint in (
        '"/v1/chat"',
        '"/v1/signals/washout"',
        '"/v1/signals/verdicts"',
        '"/v1/signals/calibration"',
        '"/v1/conversations"',
        '"/mcp"',
    ):
        assert f"@app.post({endpoint}" not in main_src, f"{endpoint} 不应声明在 api/main.py"
        assert f"@app.get({endpoint}" not in main_src, f"{endpoint} 不应声明在 api/main.py"


def test_existing_chat_completions_endpoint_remains_in_main():
    """既有的 /v1/chat/completions 保持原位：本变更要兼容它，不搬动它。

    这条是「新路由不进 main」的反向约束——避免有人用「全部重构」的名义
    顺手改动既有入口，扩大回归面。
    """
    main_src = (API_DIR / "main.py").read_text(encoding="utf-8")
    assert '@app.post("/v1/chat/completions")' in main_src


# ── 信号层的确定性与显式注入 ──────────────────────────────────────────────


def test_every_dimension_is_deterministic():
    """逐维度断言：同输入两次评估必须完全一致。"""
    from tests._signal_fixtures import make_chips, make_context, make_daily, make_flow, make_intraday
    from tradingagents.signals import capital, chips, intraday, ma_structure, position, price_action, relative, volume
    from tradingagents.signals.ports import PeerSnapshot

    closes = [10.0] * 5 + [9.8, 9.6, 9.4, 9.3, 9.6]
    volumes = [1000, 1000, 1000, 1000, 1000, 700, 600, 500, 480, 700]
    ctx = make_context(
        daily=make_daily(closes, volumes),
        intraday=make_intraday("2026-01-16", [10.0, 9.8, 9.6, 9.5, 9.45, 9.5], [500, 600, 700, 800, 900, 700]),
        chips=make_chips(profit_ratio=0.3, avg_cost=10.4),
        flow=make_flow(main_net=-4.0e7, super_large_net=-5.0e6),
        peer=PeerSnapshot(board_name="白酒", board_return_pct=-3.0, market_return_pct=-1.5,
                          stock_turnover=2.2, board_median_turnover=1.8),
    )
    for module in (volume, price_action, ma_structure, relative, capital, chips, intraday):
        assert module.compute(ctx) == module.compute(ctx), module.__name__
    assert position.compute(ctx) == position.compute(ctx)


def test_weights_are_injected_not_global():
    """权重必须作为参数注入，使并发评估可以用不同版本互不干扰。

    对照 `dataflows/config.py` 的 `global _config`：模块级可变单例在并发下
    会串味，测试之间也会互相污染（仓库已有 test_db_isolation / test_config_fallback
    这类隔离问题的先例）。新链路不复制这个模式。
    """
    import inspect

    from tradingagents.signals import washout

    sig = inspect.signature(washout.evaluate)
    assert "weights" in sig.parameters
    assert isinstance(sig.parameters["weights"].default.version, str)

    ctx_sig = inspect.signature(washout.evaluate_context)
    assert "weights" in ctx_sig.parameters

    # 不同权重集必须给出不同结果（证明它真的被用上了，而不是装饰性参数）
    from tests._signal_fixtures import make_context, make_daily
    from tradingagents.signals.weights import DEFAULT_WEIGHTS, WashoutWeights, replace

    closes = [10.0] * 5 + [9.8, 9.6, 9.4, 9.3, 9.6]
    volumes = [1000, 1000, 1000, 1000, 1000, 700, 600, 500, 480, 700]
    ctx = make_context(daily=make_daily(closes, volumes))
    low = washout.evaluate_context(ctx, replace(DEFAULT_WEIGHTS, margin_abstain=0.99))
    high = washout.evaluate_context(ctx, DEFAULT_WEIGHTS)
    assert low.state == "insufficient_evidence"
    assert high.state != "insufficient_evidence" or high.margin < DEFAULT_WEIGHTS.margin_abstain


# ── 证据锚定层：与信号层同级约束 ──────────────────────────────────────────


@pytest.mark.parametrize("path", _py_files(GROUNDING_DIR), ids=lambda p: f"grounding/{p.name}")
def test_grounding_has_no_io_or_llm_imports(path: pathlib.Path):
    """校验器不得调用模型、不得联网。

    「用 LLM 校验 LLM」会引入第二个不可复现的判定源，且校验逻辑本身无法
    单测。这条约束把「机械校验」四个字变成构建时断言。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations = _imported_modules(tree) & BANNED_MODULES
    assert not violations, f"{path.name} 违反纯函数约束，导入了: {sorted(violations)}"


@pytest.mark.parametrize("path", _py_files(GROUNDING_DIR), ids=lambda p: f"grounding/{p.name}")
def test_grounding_has_no_ambient_clock(path: pathlib.Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations = _call_names(tree) & BANNED_CALLS
    assert not violations, f"{path.name} 读取了环境时钟: {sorted(violations)}"
