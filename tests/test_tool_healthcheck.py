"""W4-T3：只读工具体检脚本（`scripts/tool_healthcheck.py`）的自检。

## 这个脚本要回答的问题

"工具在最坏情况下会不会**看起来查过了、其实什么都没给**"。判据来自
`tradingagents/grounding/types.py` 的 `Coverage` docstring（**先读它再读这里**）：

* `degraded=True` —— 一定是降级（故障）；
* `evidence=()` 且 `unavailable=()` 且 `degraded=False` —— **静默空**，这是要盯的坏形状；
* `evidence=()` 但 `unavailable` 有声明 —— 已声明不可用，**不是**问题。

## 为什么"同一个维度出现在两边"不是矛盾

`available` 说的是**取数通路健康**，`unavailable` 说的是**该维度的数据**。
`available=('trade_ledger',)` + `unavailable=('trade_ledger（没有已录入的交易流水）',)`
是刻意设计（"工具是好的，只是你还没录流水"），实测 13 个工具是这个形状。
所以体检脚本只能把它当**信息**报出来，绝不能判成失败——`/tmp/coverage_probe.py`
那份探针就是按"自相矛盾"判的，于是 13 个刻意设计全被标红（W3 的提交
`9441594` 专门更正了这一点）。

## 为什么必须按**维度名**精确比较

`signal_washout_probe` 的 `available` 里有 `position`、`unavailable` 里有
`capital_composition`——而 `'position' in 'capital_composition'` 为 **True**。
子串匹配会凭空造出一个"两边都有 position"的假象。所以要先取
「维度名（说明）」里的维度名，再逐个 `==`。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import tool_healthcheck as hc  # type: ignore  # noqa: E402

from tradingagents.grounding.types import Coverage  # noqa: E402
from tradingagents.tools.registry import ToolRegistry  # noqa: E402
from tradingagents.tools.spec import (  # noqa: E402
    PLAN_LAYER_DECISION,
    PLAN_LAYER_EVIDENCE,
    PLAN_LAYER_TASK,
    ToolResult,
    ToolSpec,
)


# ── 夹具 ─────────────────────────────────────────────────────────────────


def _spec(name: str, handler, *, layer: str = PLAN_LAYER_EVIDENCE,
          schema: dict | None = None, asynchronous: bool = False) -> ToolSpec:
    return ToolSpec(
        name=name,
        title=name,
        description=f"stub {name}",
        input_schema=schema or {"type": "object", "properties": {}, "required": []},
        output_contract=f"{name} → ToolResult",
        handler=handler,
        plan_layer=layer,
        asynchronous=asynchronous,
        cost="expensive" if asynchronous else "cheap",
    )


def _registry(*specs: ToolSpec) -> ToolRegistry:
    reg = ToolRegistry()
    for spec in specs:
        reg.register(spec)
    return reg


def _ok(*, evidence: int = 2, unavailable: tuple[str, ...] = ()) -> ToolResult:
    from tradingagents.grounding.types import EvidenceItem

    items = tuple(
        EvidenceItem(
            id=f"E{i + 1}", field="close", value=1.0, unit="yuan",
            source_key="k", as_of="2026-09-23",
        )
        for i in range(evidence)
    )
    return ToolResult(data={}, evidence=items, coverage=Coverage(available=("k",), unavailable=unavailable))


# ── 维度名比较：子串误判是本条任务的显式陷阱 ────────────────────────────────


def test_dimension_name_strips_the_explanation():
    """`维度名（说明）` → 取维度名。比较前必须脱掉说明，否则说明文字会参与匹配。"""
    assert hc.dimension_name("trade_ledger（没有已录入的交易流水）") == "trade_ledger"
    assert hc.dimension_name("capital_composition (no data)") == "capital_composition"
    assert hc.dimension_name("  position  ") == "position"


def test_position_is_not_capital_composition():
    """**显式陷阱**：`'position' in 'capital_composition'` 为真，但那不是同一个维度。

    实测（2026-09-23）：`signal_washout_probe` 的 available 含 `position`、
    unavailable 含 `capital_composition`，子串匹配会把这条判成"两边同时出现"。
    """
    both = hc.dimensions_in_both(("position",), ("capital_composition（无逐笔资金构成）",))
    assert both == (), "子串匹配把两个不同的维度判成了同一个"


def test_same_dimension_on_both_sides_is_reported():
    """真正的同维度两侧出现要能报出来（它是刻意设计，不是矛盾）。"""
    both = hc.dimensions_in_both(
        ("trade_ledger",), ("trade_ledger（没有已录入的交易流水）", "watchlist")
    )
    assert both == ("trade_ledger",)


# ── 判据：静默空才是告警 ──────────────────────────────────────────────────


def test_silent_empty_is_the_alarm():
    """`evidence=()` + `unavailable=()` + `degraded=False` = 静默空。"""
    result = ToolResult(data={}, evidence=(), coverage=Coverage(available=()))
    assert hc.classify(result) == hc.VERDICT_SILENT_EMPTY


def test_declared_unavailable_is_not_a_failure():
    """有声明就没问题：回答层能如实说"这个维度没覆盖"，用户不会被误导。"""
    result = ToolResult(
        data={}, evidence=(), coverage=Coverage(available=("trade_ledger",), unavailable=("trade_ledger（没有已录入的交易流水）",))
    )
    assert hc.classify(result) == hc.VERDICT_DECLARED_UNAVAILABLE


def test_same_dimension_on_both_sides_is_not_a_failure():
    """两边同时出现（通路健康 + 该维度没数据）是刻意设计，不得判成失败。"""
    result = ToolResult(
        data={}, evidence=(),
        coverage=Coverage(
            available=("position",), unavailable=("capital_composition（无逐笔资金构成）",)
        ),
    )
    row = hc.probe_tool(_spec("signal_washout_probe", lambda **kw: result), as_of="2026-09-23")
    assert row["verdict"] == hc.VERDICT_DECLARED_UNAVAILABLE
    assert row["both_sides"] == [], "不同维度被子串匹配误判成同一个"


def test_degraded_wins_over_declared_unavailable():
    """`degraded=True` 一定是降级——它比"已声明不可用"更严重，优先级更高。"""
    result = ToolResult(
        data={}, evidence=(), degraded=True,
        degraded_reason="数据源超时",
        coverage=Coverage(unavailable=("k（源超时）",)),
    )
    assert hc.classify(result) == hc.VERDICT_DEGRADED


def test_evidence_present_is_ok_even_with_unavailable_dimensions():
    """有证据就是 ok，哪怕同时声明了未覆盖维度（一个工具常覆盖多个维度）。"""
    result = _ok(evidence=3, unavailable=("capital_composition（无数据）",))
    assert hc.classify(result) == hc.VERDICT_OK


# ── 从 schema 推导最小调用参数 ────────────────────────────────────────────


def test_minimal_arguments_are_derived_from_the_schema():
    """参数必须**从 schema 推导**：各工具参数名并不统一（`symbols` 是列表、
    `ticker` 是字符串、`curr_date` 是日期）——猜名字会让每次调用都被拒。"""
    schema = {
        "type": "object",
        "properties": {
            "symbols": {"type": "array"},
            "as_of": {"type": "string"},
            "limit": {"type": "integer"},
        },
        "required": ["symbols", "as_of"],
    }
    args = hc.derive_minimal_arguments(schema, as_of="2026-09-23", symbol="600519.SH")
    assert args == {"symbols": ["600519.SH"], "as_of": "2026-09-23"}, "只填必需项"

    schema2 = {
        "type": "object",
        "properties": {"ticker": {"type": "string"}},
        "required": ["ticker"],
    }
    assert hc.derive_minimal_arguments(schema2, as_of="2026-09-23", symbol="600519.SH") == {
        "ticker": "600519.SH"
    }


def test_declared_defaults_are_supplied_like_production_does():
    """schema 里声明了 `default` 的参数必须**一并填上**。

    生产调用链会填：`registry.invoke()` → `validate()` 对未提供的参数套用
    `prop["default"]`（`tradingagents/tools/registry.py:239`）后才调 handler。
    体检脚本直接调 `spec.handler`，若不自己填默认值，工具拿到的是 **None**——
    实测第一次全量体检（2026-09-23）就踩了：`get_board_spot` / `get_board_rank` /
    `get_board_hist` 三个**健康**工具全被自己的体检脚本报成「故障」，
    理由是 `Unknown sector_type=None, expected 'industry' or 'concept'`。

    那是测量伪影，不是工具病：把默认值补上后三者都正常。体检脚本报假警的成本
    和漏报一样高——报假警会让人开始忽略这份报告。
    """
    schema = {
        "type": "object",
        "properties": {
            "board_name": {"type": "string"},
            "sector_type": {"type": "string", "default": "industry"},
            "period": {"type": "string", "default": "1d"},
        },
        "required": ["board_name"],
    }
    args = hc.derive_minimal_arguments(schema, as_of="2026-09-23", symbol="600519.SH")
    assert args == {"board_name": "半导体", "sector_type": "industry", "period": "1d"}


def test_default_none_is_passed_through_verbatim():
    """`"default": None`（如 `get_fundamentals.curr_date`）按生产语义**原样传 None**。

    不要"顺手"把它变成别的值或删掉：生产就是把 None 交给 handler，
    体检必须量同一条路径，否则量的不是同一个东西。
    """
    schema = {
        "type": "object",
        "properties": {"ticker": {"type": "string"}, "curr_date": {"type": "string", "default": None}},
        "required": ["ticker"],
    }
    args = hc.derive_minimal_arguments(schema, as_of="2026-09-23", symbol="600519.SH")
    assert args == {"ticker": "600519.SH", "curr_date": None}


def test_probe_tool_passes_defaults_to_the_handler():
    """端到端：handler 必须真的**收到**默认值（上面两条只测了推导）。"""
    seen: dict = {}

    def handler(**kw):
        seen.update(kw)
        return _ok(evidence=1)

    spec = _spec(
        "get_board_spot", handler,
        schema={
            "type": "object",
            "properties": {"sector_type": {"type": "string", "default": "industry"}},
            "required": [],
        },
    )
    hc.probe_tool(spec, as_of="2026-09-23")
    assert seen == {"sector_type": "industry"}, "sector_type 没传给 handler，工具会拿到 None"


def test_probe_tool_reports_shape_and_verdict():
    """一行结果必须带：在册/分层/证据条数/两边的维度/degraded/结论。"""
    spec = _spec(
        "demo", lambda **kw: _ok(evidence=2), layer=PLAN_LAYER_DECISION
    )
    row = hc.probe_tool(spec, as_of="2026-09-23")
    assert row["tool"] == "demo"
    assert row["layer"] == PLAN_LAYER_DECISION
    assert row["registered"] is True
    assert row["called"] is True
    assert row["evidence"] == 2
    assert row["available"] == ["k"]
    assert row["unavailable"] == []
    assert row["degraded"] is False
    assert row["verdict"] == hc.VERDICT_OK


def test_handler_exception_becomes_a_verdict_not_a_crash():
    """单个工具炸掉不能中断整轮体检——否则一个坏工具就再也看不到其余工具的体检结果。

    网络类工具允许失败（判据里写着），但那必须是**一条结论**，不是异常上抛。
    """

    def boom(**kw):
        raise RuntimeError("network down")

    row = hc.probe_tool(_spec("net", boom), as_of="2026-09-23")
    assert row["verdict"] == hc.VERDICT_EXCEPTION
    assert "RuntimeError" in row["detail"]


def test_non_tool_result_return_is_an_exception_verdict():
    """handler 返回的不是 `ToolResult`（旧式 `@tool` 形态）也必须算异常。

    否则 `.evidence` 属性访问会抛 `AttributeError`，把整轮体检炸掉。
    """
    row = hc.probe_tool(_spec("legacy", lambda **kw: "markdown 表格"), as_of="2026-09-23")
    assert row["verdict"] == hc.VERDICT_EXCEPTION
    assert "ToolResult" in row["detail"]


# ── 只读：不提交异步任务 ──────────────────────────────────────────────────


def test_async_tools_are_not_called_by_default():
    """L3 异步工具的"调一次"= **提交一个真实任务**（写库、烧模型调用）。

    一个声称只读的体检脚本不能提交任务。默认跳过并如实标注"未调用"，
    要调必须显式 `--include-async`。
    """
    calls: list[str] = []

    def handler(**kw):
        calls.append("called")
        return _ok()

    reg = _registry(
        _spec("sync_demo", handler),
        _spec("submit_thing", handler, layer=PLAN_LAYER_TASK, asynchronous=True),
    )
    rows, summary = hc.audit(reg, as_of="2026-09-23")
    by_name = {r["tool"]: r for r in rows}
    assert calls == ["called"], "异步工具的 handler 不得被调用"
    assert by_name["submit_thing"]["called"] is False
    assert by_name["submit_thing"]["verdict"] == hc.VERDICT_SKIPPED_ASYNC
    assert by_name["submit_thing"]["layer"] == PLAN_LAYER_TASK
    assert summary["skipped_async"] == 1
    assert summary["silent_empty"] == 0, "跳过不等于静默空"


def test_include_async_calls_them_when_explicitly_asked():
    reg = _registry(_spec("submit_thing", lambda **kw: _ok(), layer=PLAN_LAYER_TASK, asynchronous=True))
    rows, _ = hc.audit(reg, as_of="2026-09-23", include_async=True)
    assert rows[0]["called"] is True
    assert rows[0]["verdict"] == hc.VERDICT_OK


# ── 汇总与退出码 ──────────────────────────────────────────────────────────


def _mixed_registry() -> ToolRegistry:
    return _registry(
        _spec("good", lambda **kw: _ok(evidence=2)),
        _spec(
            "silent",
            lambda **kw: ToolResult(data={}, evidence=(), coverage=Coverage()),
        ),
        _spec(
            "declared",
            lambda **kw: ToolResult(
                data={}, evidence=(),
                coverage=Coverage(available=("d",), unavailable=("d（没录）",)),
            ),
        ),
        _spec(
            "broken",
            lambda **kw: ToolResult(data={}, evidence=(), degraded=True, degraded_reason="超时"),
        ),
        _spec("boom", lambda **kw: (_ for _ in ()).throw(RuntimeError("x"))),
    )


def test_report_lists_the_actual_dimensions_not_just_counts():
    """非 ok 的工具必须在报告里列出**实际维度名**，不能只给个数。

    "已声明不可用 13 个"没法让人判断这些声明是否成立；要点到维度
    （`trade_ledger（没有已录入的交易流水）`），人工才复核得了。
    同理，`available` 也要列出来——"通路健康"与"该维度没数据"是两件事，
    只看 unavailable 会以为工具坏了。
    """
    reg = _registry(
        _spec(
            "declared",
            lambda **kw: ToolResult(
                data={}, evidence=(),
                coverage=Coverage(
                    available=("trade_ledger",),
                    unavailable=("trade_ledger（没有已录入的交易流水）",),
                ),
            ),
        ),
        _spec("good", lambda **kw: _ok(evidence=1)),
    )
    rows, summary = hc.audit(reg, as_of="2026-09-23")
    text = hc.render_report(rows, summary)
    assert "没有已录入的交易流水" in text, "未列出 unavailable 的实际维度"
    assert "trade_ledger" in text, "未列出 available 的实际维度"


def test_summary_counts_every_verdict():
    rows, summary = hc.audit(_mixed_registry(), as_of="2026-09-23")
    assert summary["registered_total"] == 5
    assert summary["called"] == 5
    assert summary["ok"] == 1
    assert summary["silent_empty"] == 1
    assert summary["declared_unavailable"] == 1
    assert summary["degraded"] == 1
    assert summary["exception"] == 1


def test_report_names_the_silent_empty_tools():
    """报告必须**点名**静默空的工具——只知道"有 1 个"没法修。"""
    rows, summary = hc.audit(_mixed_registry(), as_of="2026-09-23")
    text = hc.render_report(rows, summary)
    assert "静默空" in text
    assert "silent" in text, "没有点名静默空的工具"
    assert "good" in text


def test_exit_code_is_nonzero_when_silent_empty_exists():
    """静默空 > 0 → 非零退出码，这样它能进 CI。"""
    assert hc.main(["--as-of", "2026-09-23"], registry=_mixed_registry()) == 1


def test_exit_code_is_zero_without_silent_empty():
    reg = _registry(
        _spec("good", lambda **kw: _ok(evidence=1)),
        _spec("declared", lambda **kw: ToolResult(data={}, evidence=(), coverage=Coverage(unavailable=("d（没录）",)))),
        _spec("broken", lambda **kw: ToolResult(data={}, evidence=(), degraded=True)),
    )
    assert hc.main(["--as-of", "2026-09-23"], registry=reg) == 0


def test_strict_mode_also_fails_on_degraded_and_exception():
    """网络失败默认容忍（判据如此），但 `--strict` 时可以要求零故障零异常。"""
    reg = _registry(_spec("broken", lambda **kw: ToolResult(data={}, evidence=(), degraded=True)))
    assert hc.main(["--as-of", "2026-09-23"], registry=reg) == 0
    assert hc.main(["--as-of", "2026-09-23", "--strict"], registry=reg) == 1


# ── 源码护栏 ──────────────────────────────────────────────────────────────


def test_every_tool_is_probed_exactly_once():
    """每个工具只调一次：体检脚本自己不该给数据源加压（实测有工具单次 9.2 秒）。"""
    counts: dict[str, int] = {}

    def handler(name):
        def _h(**kw):
            counts[name] = counts.get(name, 0) + 1
            return _ok(evidence=1)
        return _h

    reg = _registry(_spec("a", handler("a")), _spec("b", handler("b")))
    hc.audit(reg, as_of="2026-09-23")
    assert counts == {"a": 1, "b": 1}


def test_source_only_imports_the_real_factory():
    """护栏：只能 `from api.services.chat_service import build_registry`。

    `_require_user` 之类的东西**并不存在**，照着猜的 import 会让脚本一跑就
    `ImportError`——而那种失败会被误读成"工具面坏了"。
    """
    src = (ROOT / "scripts" / "tool_healthcheck.py").read_text(encoding="utf-8")
    assert "from api.services.chat_service import build_registry" in src
    assert "_require_user" not in src
