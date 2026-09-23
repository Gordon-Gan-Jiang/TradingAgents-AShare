#!/usr/bin/env python
"""工具面**只读**体检：每个工具调一次，问"它会不会静默地什么都不给"。

## 为什么需要它

工具"调用成功"不等于"取到了能引用的数据"。实测（2026-09-23，42 个同步工具全量
调用）里存在三种完全不同的形态，而它们在日志里长得差不多：

* **静默空**：`evidence=()`、`unavailable=()`、`degraded=False`。工具没报错、
  也没声明任何未覆盖维度，但**一张可引用的证据都没有**——用户看到的是
  "本轮无可用证据"，而没有任何一层说得出"为什么"。这是唯一要告警的坏形状。
* **已声明不可用**：`evidence=()` 但 `unavailable` 有声明（"没有已录入的交易流水"）。
  回答层据此能如实说"这个维度没覆盖"，**不是问题**。
* **故障**：`degraded=True`。真取不到（源超时/挂了），已在 `degraded_reason` 里说明。

## Coverage 的两个字段说的是两件事（**不要按"矛盾"判**）

`available` 说的是**取数通路健康**，`unavailable` 说的是**该维度的数据**。
同一个维度同时出现在两边是**刻意设计**（区分「你没录入」与「数据源坏了」），
实测 13 个工具是这个形状。`/tmp/coverage_probe.py` 那份一次性探针按
"available ∩ unavailable ≠ ∅" 判"自相矛盾"，于是把这个刻意设计全标红了——
W3 的提交 `9441594` 专门更正过这一点。本脚本只把它当**信息**报（`both_sides`），
不参与退出码。

判维度相等要取「维度名（说明）」里的**维度名**再精确比较：`signal_washout_probe`
的 available 里有 `position`、unavailable 里有 `capital_composition`，而
`'position' in 'capital_composition'` 为 **True**——子串匹配会凭空造出一个
"两边都有 position"的假象。见 `dimension_name()` / `dimensions_in_both()`。

## 只读

* 库/数据源**只读**：只调 handler，不改任何东西；
* **默认不调 L3 异步工具**：它们的"调一次"= 提交一个真实任务（写库 + 烧模型调用）。
  实测（同一轮）探针不设防地调了 `submit_backtest_job` / `submit_daily_review_job` /
  `submit_deep_analysis_job` / `submit_model_arena_job`，还顺带初始化了一次 LLM 客户端。
  只读体检不该有这种副作用，所以默认跳过并如实标注"未调用（L3 异步）"，
  要调必须显式 `--include-async`。

## 用法

    .venv/bin/python scripts/tool_healthcheck.py                    # 全量同步工具
    .venv/bin/python scripts/tool_healthcheck.py --as-of 2026-09-23 # 钉住日期，结果可复现
    .venv/bin/python scripts/tool_healthcheck.py --json /tmp/health.json
    .venv/bin/python scripts/tool_healthcheck.py --strict           # 故障/异常也算失败

退出码：**静默空个数 > 0 → 1**（这条可以进 CI）。`--strict` 时故障与异常也算失败。

⚠️ 体检结论依赖 `--as-of` 与数据源可达性：网络类工具**允许失败**（那是"故障"，
不是"静默空"）。所以默认只在静默空时失败——把"今天网断了"变成 CI 红是另一种噪声。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tradingagents.tools.spec import ToolResult  # noqa: E402

# ── 结论枚举（中文直写：报告是给人和 CI 日志看的）──────────────────────────
VERDICT_OK = "ok"
VERDICT_SILENT_EMPTY = "静默空"
VERDICT_DECLARED_UNAVAILABLE = "已声明不可用"
VERDICT_DEGRADED = "故障"
VERDICT_EXCEPTION = "异常"
# 未调用**不是**结论，是一个显式状态：只读体检不提交任务（见模块 docstring）。
VERDICT_SKIPPED_ASYNC = "未调用（L3 异步）"

# 默认标的：体检只需要"一个合法标的"，用最主流的那个，便于人工复现。
DEFAULT_SYMBOL = "600519.SH"
# 日期类参数名（各工具并不统一，必须按名字给对应的值）。
_DATE_KEYS: tuple[str, ...] = (
    "as_of", "date", "curr_date", "trade_date", "start_date", "end_date",
)
_INSTRUMENT_KEYS: tuple[str, ...] = ("symbol", "ticker", "code", "symbols")
# 板块类参数：给股票代码是**无效输入**（工具会返回"没这个板块"），
# 那会把一个参数问题误报成静默空。用一个真实存在的板块名。
_BOARD_KEYS: tuple[str, ...] = ("board_name", "sector", "industry", "board")
DEFAULT_BOARD = "半导体"


def dimension_name(declaration: str) -> str:
    """从 `维度名（说明）` 里取**维度名**。

    维度声明是"维度名 + 可选说明"的格式（如
    `trade_ledger（没有已录入的交易流水）`）。比较维度是否相同必须先脱掉说明：
    说明文字里会出现别的维度名，直接比整串会误判。
    """
    text = str(declaration or "").strip()
    for opener in ("（", "("):
        head, sep, _tail = text.partition(opener)
        if sep:
            text = head
            break
    return text.strip()


def dimensions_in_both(
    available: Iterable[str], unavailable: Iterable[str]
) -> tuple[str, ...]:
    """同时出现在 `available` 与 `unavailable` 里的**维度名**（精确相等）。

    **不能用子串匹配**：`'position' in 'capital_composition'` 为真，会把
    `signal_washout_probe` 这种"通路健康 + 别的维度没数据"误判成"同一个维度两边都有"。

    返回值是**信息**，不是失败：同一个维度两边都有 = "工具是好的，只是你没有数据"
    （实测 13 个工具如此，刻意设计）。
    """
    left = {dimension_name(x) for x in available if dimension_name(x)}
    right = {dimension_name(x) for x in unavailable if dimension_name(x)}
    return tuple(sorted(left & right))


def _sample_value(
    key: str, prop: Mapping[str, Any], *, as_of: str, symbol: str
) -> Any:
    """给一个参数挑一个最小的合法值（按声明类型 + 参数名语义）。"""
    kind = prop.get("type")
    if kind == "array":
        return [symbol]
    if kind == "integer":
        return 20
    if kind == "number":
        return 1.0
    if kind == "boolean":
        return False
    if key in _BOARD_KEYS:
        # 板块类参数给股票代码是**无效输入**，会被读成"没这个板块"。
        return DEFAULT_BOARD
    if key in _DATE_KEYS:
        return as_of
    if key in _INSTRUMENT_KEYS:
        return symbol
    # 兜底给标的：未知的字符串参数大多是"某个代码/某个键"，给标的比给空串更可能
    # 触到真实路径（空串往往被当成"没传"而提前返回）。
    return symbol


def derive_minimal_arguments(
    schema: Mapping[str, Any] | None, *, as_of: str, symbol: str = DEFAULT_SYMBOL
) -> dict[str, Any]:
    """从 schema **推导**出一次调用的参数：必需项 + 已声明的默认值。

    必须推导而不是猜：各工具的参数名不统一（`get_realtime_quotes` 要
    `symbols`（列表）、`get_fundamentals` 要 `ticker`、`get_zt_pool` 要 `date`）。
    曾经按 `symbol`/`as_of` 硬猜，结果每次调用都被注册表以 `unknown arguments` 拒掉，
    而回答只说"无可用证据"——原因看不出来（`routing.py` 记着这次事故）。

    **声明了 `default` 的参数也要填**，因为生产调用链就是这么做的：
    `registry.invoke()` → `validate()` 对未提供的参数套用 `prop["default"]`
    （`tradingagents/tools/registry.py:239`），之后才调 handler。体检直接调
    `spec.handler`，不自己填就会让工具拿到 `None`——实测第一次全量体检
    （2026-09-23）把 `get_board_spot` / `get_board_rank` / `get_board_hist`
    三个**健康**工具全报成「故障」，理由是
    `Unknown sector_type=None, expected 'industry' or 'concept'`。
    体检报假警和漏报一样有害：它会让人开始忽略这份报告。

    `"default": None` 按生产语义**原样传 None**（如 `get_fundamentals.curr_date`），
    不做"顺手修正"——要量的就是生产那条路径。
    """
    schema = schema or {}
    properties: Mapping[str, Any] = schema.get("properties") or {}
    required = {str(key) for key in (schema.get("required") or ())}
    args: dict[str, Any] = {}
    for key, prop in properties.items():
        prop = prop or {}
        if key in required:
            args[key] = _sample_value(key, prop, as_of=as_of, symbol=symbol)
        elif "default" in prop:
            args[key] = prop["default"]
    return args


def classify(result: ToolResult) -> str:
    """按判据给结论（顺序即优先级，**不可调换**）。

    1. `degraded=True` → 故障（一定是降级，与是否声明了未覆盖维度无关）；
    2. 有证据 → ok（一个工具常覆盖多维度，个别维度没数据不影响它答得出话）；
    3. 无证据但有 `unavailable` 声明 → 已声明不可用（回答层能如实说明）；
    4. 三者皆空 → **静默空**（要告警的坏形状）。
    """
    if result.degraded:
        return VERDICT_DEGRADED
    if result.evidence:
        return VERDICT_OK
    if result.coverage.unavailable:
        return VERDICT_DECLARED_UNAVAILABLE
    return VERDICT_SILENT_EMPTY


def probe_tool(
    spec: Any, *, as_of: str, symbol: str = DEFAULT_SYMBOL
) -> dict[str, Any]:
    """调一次工具，记录形状与结论。**异常在这里转成结论**，不上抛。

    单个工具炸掉不该中断整轮体检——否则一个坏工具就让其余 40 个工具的结论
    全都看不到（`/tmp/coverage_probe.py` 就是这么写的第一版，好在没炸）。
    """
    args = derive_minimal_arguments(getattr(spec, "input_schema", None), as_of=as_of, symbol=symbol)
    started = time.monotonic()
    try:
        result = spec.handler(**args)
    except Exception as exc:  # noqa: BLE001 — 这就是"故障/异常"这条结论本身
        return _row(
            spec, called=True, verdict=VERDICT_EXCEPTION,
            detail=f"{type(exc).__name__}: {exc}"[:200],
            seconds=time.monotonic() - started, arguments=args,
        )

    if not isinstance(result, ToolResult):
        # 旧式 `@tool` 形态（预格式化字符串）。它没有 evidence 概念，所以"没有证据"
        # 是结构性的。折成异常而不是静默算 ok——否则体检会给它发一张合格证。
        return _row(
            spec, called=True, verdict=VERDICT_EXCEPTION,
            detail=f"handler 返回 {type(result).__name__}，不是 ToolResult",
            seconds=time.monotonic() - started, arguments=args,
        )

    available = list(result.coverage.available)
    unavailable = list(result.coverage.unavailable)
    row = _row(
        spec, called=True, verdict=classify(result),
        evidence=len(result.evidence), available=available, unavailable=unavailable,
        degraded=bool(result.degraded), detail=str(result.degraded_reason or "")[:200],
        seconds=time.monotonic() - started, arguments=args,
        data_keys=sorted(str(k) for k in (result.data or {})),
    )
    row["both_sides"] = list(dimensions_in_both(available, unavailable))
    return row


def _row(
    spec: Any,
    *,
    called: bool,
    verdict: str,
    evidence: int = 0,
    available: Sequence[str] = (),
    unavailable: Sequence[str] = (),
    degraded: bool = False,
    detail: str = "",
    seconds: float = 0.0,
    arguments: Mapping[str, Any] | None = None,
    data_keys: Sequence[str] = (),
) -> dict[str, Any]:
    return {
        "tool": str(getattr(spec, "name", "?")),
        # "是否在册"：能走到这里就说明在册（注册表里取出来的），显式记下来是为了
        # 让报告自己说得清这一点，而不是让读者去推。
        "registered": True,
        "layer": str(getattr(spec, "plan_layer", "") or ""),
        "asynchronous": bool(getattr(spec, "asynchronous", False)),
        "called": called,
        "arguments": dict(arguments or {}),
        "evidence": evidence,
        "available": list(available),
        "unavailable": list(unavailable),
        "both_sides": [],
        "degraded": degraded,
        "detail": detail,
        "data_keys": list(data_keys),
        "seconds": round(seconds, 2),
        "verdict": verdict,
    }


def audit(
    registry: Any,
    *,
    as_of: str,
    symbol: str = DEFAULT_SYMBOL,
    include_async: bool = False,
    only: Sequence[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """遍历工具面，得到逐工具结论与汇总计数。

    **每个工具只调一次**：实测有工具单次 9.2 秒（`signal_washout_probe`），
    体检脚本不该给数据源额外加压。

    默认跳过 L3 异步工具（`spec.asynchronous`）：调它们 = 提交真实任务（写库）。
    它们仍进报告，结论写"未调用（L3 异步）"——从报告里**消失**才是静默。
    """
    wanted = set(only) if only else None
    rows: list[dict[str, Any]] = []
    # `registry` 的可迭代顺序已按名字排序（`ToolRegistry.__iter__`），报告才可比对。
    for spec in registry:
        if wanted is not None and spec.name not in wanted:
            continue
        if getattr(spec, "asynchronous", False) and not include_async:
            rows.append(_row(spec, called=False, verdict=VERDICT_SKIPPED_ASYNC,
                             detail="L3 异步工具：调用=提交真实任务，只读体检默认跳过（--include-async 可强制）"))
            continue
        rows.append(probe_tool(spec, as_of=as_of, symbol=symbol))

    summary: dict[str, Any] = {
        "registered_total": len(rows),
        "called": sum(1 for r in rows if r["called"]),
        "skipped_async": sum(1 for r in rows if r["verdict"] == VERDICT_SKIPPED_ASYNC),
        "ok": sum(1 for r in rows if r["verdict"] == VERDICT_OK),
        "silent_empty": sum(1 for r in rows if r["verdict"] == VERDICT_SILENT_EMPTY),
        "declared_unavailable": sum(
            1 for r in rows if r["verdict"] == VERDICT_DECLARED_UNAVAILABLE
        ),
        "degraded": sum(1 for r in rows if r["verdict"] == VERDICT_DEGRADED),
        "exception": sum(1 for r in rows if r["verdict"] == VERDICT_EXCEPTION),
        "both_sides": sum(1 for r in rows if r["both_sides"]),
        "as_of": as_of,
        "symbol": symbol,
        # `--only` 过滤后 `registered_total` 是"本次覆盖的工具数"，不是注册表总数；
        # 报告里要把这件事说出来，否则一个被过滤过的报告会被当成全量结论。
        "only": sorted(wanted) if wanted else [],
    }
    summary["silent_empty_tools"] = [
        r["tool"] for r in rows if r["verdict"] == VERDICT_SILENT_EMPTY
    ]
    summary["degraded_tools"] = [
        r["tool"] for r in rows if r["verdict"] == VERDICT_DEGRADED
    ]
    summary["exception_tools"] = [
        r["tool"] for r in rows if r["verdict"] == VERDICT_EXCEPTION
    ]
    summary["by_layer"] = _by_layer(rows)
    return rows, summary


def _by_layer(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    """按 L1/L2/L3 分层汇总。分层错了会让规划层注入错工具面（见 `ToolSpec` 注释）。"""
    out: dict[str, dict[str, int]] = {}
    for row in rows:
        bucket = out.setdefault(str(row["layer"] or "?"), {"total": 0, "ok": 0, "silent_empty": 0})
        bucket["total"] += 1
        bucket["ok"] += int(row["verdict"] == VERDICT_OK)
        bucket["silent_empty"] += int(row["verdict"] == VERDICT_SILENT_EMPTY)
    return out


def render_report(rows: Sequence[Mapping[str, Any]], summary: Mapping[str, Any]) -> str:
    """人看的报告：逐工具一行，最后汇总 + **点名**静默空的工具。

    点名是必须的：只知道"有 1 个静默空"没法修，还得自己再跑一遍去找。
    """
    lines = [
        f"工具面体检（只读）· as_of={summary.get('as_of')} symbol={summary.get('symbol')}",
        "",
        f"{'工具':32s} {'层':4s} {'证据':>4s} {'degraded':8s} {'avail':>5s} {'unavail':>7s}  结论",
        "─" * 104,
    ]
    for row in rows:
        name = str(row["tool"])
        lines.append(
            f"{name[:32]:32s} {str(row['layer']):4s} {row['evidence']:>4d} "
            f"{str(bool(row['degraded'])):8s} {len(row['available']):>5d} "
            f"{len(row['unavailable']):>7d}  {row['verdict']}"
            + (f"  ← {row['detail']}" if row["detail"] and row["verdict"] in
               (VERDICT_EXCEPTION, VERDICT_DEGRADED) else "")
        )
    lines += [
        "─" * 104,
        "===== 汇总 =====",
        f"覆盖工具（在册）    {summary.get('registered_total')}"
        + ("（`--only` 过滤后）" if summary.get("only") else ""),
        f"实调                {summary.get('called')}",
        f"  未调用（L3 异步） {summary.get('skipped_async')}",
        f"ok                  {summary.get('ok')}",
        f"静默空              {summary.get('silent_empty')}"
        + ("   ← **失败**：没有证据、也没有声明任何未覆盖维度" if summary.get("silent_empty") else ""),
        f"已声明不可用        {summary.get('declared_unavailable')}",
        f"故障（degraded）    {summary.get('degraded')}",
        f"异常                {summary.get('exception')}",
    ]
    for layer, bucket in sorted((summary.get("by_layer") or {}).items()):
        lines.append(
            f"  分层 {layer:3s}           共 {bucket['total']:3d}，ok {bucket['ok']:3d}，"
            f"静默空 {bucket['silent_empty']}"
        )
    # 同一个维度两边同时出现：**刻意设计**，只报信息，不影响退出码。
    lines += [
        "",
        f"同一维度同时出现在 available/unavailable：{summary.get('both_sides')} 个"
        "（刻意设计：通路健康 + 该维度没有数据，**不是**矛盾）",
    ]

    # 明细：非 ok 的工具逐个列出**实际维度名**。只给个数没法复核——"已声明不可用 13 个"
    # 看不出这些声明是否成立，而 `trade_ledger（没有已录入的交易流水）` 一眼就能判断。
    # 静默空的排在最前面：它是要修的那个。
    order = {
        VERDICT_SILENT_EMPTY: 0, VERDICT_EXCEPTION: 1, VERDICT_DEGRADED: 2,
        VERDICT_DECLARED_UNAVAILABLE: 3, VERDICT_SKIPPED_ASYNC: 4, VERDICT_OK: 5,
    }
    problems = sorted(
        (r for r in rows if r["verdict"] != VERDICT_OK), key=lambda r: order.get(r["verdict"], 9)
    )
    if problems:
        lines += ["", "===== 明细（非 ok）====="]
        for row in problems:
            lines.append(f"· {row['tool']} [{row['layer'] or '?'}] {row['verdict']}")
            lines.append(f"    参数     {row['arguments']}")
            if row["verdict"] != VERDICT_SKIPPED_ASYNC:
                lines.append(f"    证据     {row['evidence']} 条；degraded={row['degraded']}")
            lines.append(f"    available {row['available']}")
            lines.append(f"    unavailable {row['unavailable']}")
            if row["both_sides"]:
                lines.append(
                    f"    两边同维度 {row['both_sides']}（刻意设计：通路健康 + 该维度没数据）"
                )
            if row["detail"]:
                lines.append(f"    说明     {row['detail']}")

    for key, label in (
        ("silent_empty_tools", "静默空的工具"),
        ("degraded_tools", "故障的工具"),
        ("exception_tools", "异常的工具"),
    ):
        names = summary.get(key) or []
        if names:
            lines.append(f"{label}（{len(names)}）：{', '.join(names)}")
    return "\n".join(lines)


def _build_registry(user_id: str) -> Any:
    """装配**生产同一份**工具面。

    `build_registry` 里的端口都是惰性闭包，装配本身不触网；`user_id` 绑定
    按用户存的模型档案与持仓端口。
    """
    from api.services.chat_service import build_registry

    return build_registry(user_id)


def main(argv: Sequence[str] | None = None, *, registry: Any = None) -> int:
    parser = argparse.ArgumentParser(
        description="工具面只读体检：静默空 > 0 时以非零退出码结束（可进 CI）"
    )
    parser.add_argument("--as-of", default=date.today().isoformat(),
                        help="体检用的日期（钉住它结论才可复现；默认今天）")
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL, help="体检用的标的")
    parser.add_argument("--user-id", default="healthcheck",
                        help="装配注册表用的用户（模型档案/持仓按用户存）")
    parser.add_argument("--include-async", action="store_true",
                        help="连 L3 异步工具也调（**会提交真实任务**，默认不做）")
    parser.add_argument("--strict", action="store_true",
                        help="故障与异常也算失败（默认容忍：网络类工具允许失败）")
    parser.add_argument("--json", type=Path, default=None, help="把逐工具结果写成 JSON")
    parser.add_argument("--only", default=None,
                        help="只体检这些工具（逗号分隔），排查单个工具时用")
    args = parser.parse_args(list(argv) if argv is not None else None)

    reg = registry if registry is not None else _build_registry(args.user_id)
    only = tuple(x.strip() for x in args.only.split(",") if x.strip()) if args.only else None
    rows, summary = audit(
        reg, as_of=args.as_of, symbol=args.symbol,
        include_async=args.include_async, only=only,
    )
    print(render_report(rows, summary))

    if args.json:
        args.json.write_text(
            json.dumps({"summary": summary, "tools": rows}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"JSON 已写入 {args.json}")

    # 退出码：**静默空**永远算失败；故障/异常默认容忍（网络类工具允许失败，
    # 把"今天网断了"变成 CI 红是另一种噪声），`--strict` 时才计入。
    failures: list[str] = []
    if summary.get("silent_empty"):
        failures.append(f"静默空 {summary['silent_empty']} 个")
    if args.strict:
        if summary.get("degraded"):
            failures.append(f"故障 {summary['degraded']} 个")
        if summary.get("exception"):
            failures.append(f"异常 {summary['exception']} 个")
    if failures:
        print(
            f"\n[FAIL] {'；'.join(failures)} → 退出码 1"
            + ("（--strict）" if args.strict else ""),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
