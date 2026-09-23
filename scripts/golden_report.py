#!/usr/bin/env python
"""跑黄金集，产出**基线报告**（Phase 0 的交付物 0.4）。

用法：

    .venv/bin/python scripts/golden_report.py                 # 规则路径 → docs/golden-baseline.md

    跑**生产口径**（规划层）必须给用户：模型档案是按用户存的，不给就取不到 key，
    会回落到环境默认模型并 401，然后**静默退回规则层**——数字是规则路径的，
    却会被当成规划层的。

    TA_PLANNER_USER_ID=<uuid> .venv/bin/python scripts/golden_report.py --with-planner
                                                              # → docs/golden-with-planner.md
    .venv/bin/python scripts/golden_report.py --json out.json # 只写 JSON（供 CI 比对）
    .venv/bin/python scripts/golden_report.py --compare old.json

为什么要有 compare：Phase 1 之后必须能回答"更好了吗"。没有可机器比对的
上一版分数，"更好了"就只是一句感觉。门槛写死在 `--fail-under` 里供 CI 使用。

## 两个**真实 trace** 度量（W4 新增）

上面那些指标量的都是**规划层**（意图/标的/工具），它们看不到"计划跑完之后到底
产出了几张证据"。于是有一个盲区：规划全对、工具也调了，而每一条 step 的
`evidence_count` 都是 0——用户仍然看到"本轮无可用证据"，报告上一个字都不说。
所以报告里再加上两项，数据来自真实库 `tradingagents.db`（**只读**）：

* **零证据率**：一轮 trace 里所有 step 的 `evidence_count` 之和为 0 的回合占比；
* **参数丢弃率**：step 里出现 `dropped_arguments` 的回合占比。这个字段是 W1
  （`c112775`）新增的 `registry.last_dropped_arguments` 的唯一消费点，因此这条
  指标的**唯一**数据源就是它——去读别的东西，指标就与那个消费点脱钩了。

"改前/改后"对比走既有的 `--compare`（两个比率都进 JSON payload）；也可以把窗口
切在某个时刻，边界用 `--since` / `--until`：

    .venv/bin/python scripts/golden_report.py --json /tmp/before.json   # 改前
    # …改动…
    .venv/bin/python scripts/golden_report.py --compare /tmp/before.json  # 改后
    .venv/bin/python scripts/golden_report.py --since 2026-09-23T14:32    # 只看某时刻之后

⚠️ 边界是 **UTC**：`chat_turn_traces.created_at` 由模型的
`datetime.now(timezone.utc)` 默认值写入。按本地时间（+08:00）填边界会**静默错窗**，
所以脚本会把归一化后的实际边界与窗口内的真实时间范围一起打印出来。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.golden.harness import load_golden, probe, score, warm_name_map  # noqa: E402

REPORT_PATH = ROOT / "docs" / "golden-baseline.md"
# trace 库：`DATABASE_URL` 的默认值（`api/database.py`），相对仓库根。
TRACE_DB = ROOT / "tradingagents.db"

# `dropped_arguments` 这个 step 字段**从哪一刻起才可能存在**：W1 的提交
# `c112775`（作者时间 2026-09-23 22:32:02 +0800 = 14:32:02 UTC）。写死在这里是
# 刻意的——没有它，一个 0.0% 的丢弃率会被读成"没有丢弃"，而真相是**分母里没有
# 任何一条回合可能携带该字段**（实测：库里最后一条 trace 是 14:02:52 UTC，
# 比这一刻早 29 分钟）。
DROPPED_ARGUMENTS_LANDED_AT = "2026-09-23 14:32:02"


def _fmt(value: float) -> str:
    return f"{value * 100:.1f}%"


def _fmt_optional(value: float | None) -> str:
    """比率可能"测不到"。**不许把测不到渲染成 0.0%**（本仓库最贵的失败形态）。"""
    return "—（未测到）" if value is None else _fmt(value)


def _normalize_bound(value: str | None) -> str | None:
    """把时间窗边界归一化成与库内格式可比的形式。

    库里的 `created_at` 是 `YYYY-MM-DD HH:MM:SS.ffffff`（UTC）。两件事必须处理：

    * `T` 分隔（`2026-09-23T14:32`）要换成空格——逐字符比较时 `T`(0x54) >
      `' '`(0x20)，用 `T` 形式去比会**静默多算**一整个时间段的回合；
    * 裸日期补 `00:00:00`。`--until` 是**排他**上界，于是 `--until 2026-09-23`
      正好排除 09-23 全天，`--since 2026-09-23 --until 2026-09-23` 的组合语义
      才与人的直觉一致（两个相邻窗口拼起来 = 全集且不重叠）。
    """
    if value is None:
        return None
    text = str(value).strip().replace("T", " ")
    if not text:
        return None
    if len(text) == 10:
        text += " 00:00:00"
    return text


def _steps_of(raw: object) -> list[dict]:
    """把一行 trace 的 `steps` 解析成 step 列表（非 dict 项一律忽略）。"""
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return []
    if not isinstance(raw, list):
        return []
    return [step for step in raw if isinstance(step, dict)]


def trace_metrics(
    *, db_path: Path | None = None, since: str | None = None, until: str | None = None
) -> dict:
    """从真实 trace 库（**只读**）算出零证据率与参数丢弃率。

    口径（两条都必须逐字如此，否则与"用户看到的最后一公里"对不上）：

    * **零证据率** = 一轮里 `sum(step.evidence_count) == 0` 的回合占比。
      用求和而不是"任意一步为空"：3 步有证据、1 步空的回合仍然答得出来。
    * **参数丢弃率** = 含 `dropped_arguments` 键的 step 的回合占比。
      键存在即计（`loop.py` 只在**非空**时写该键，"键不存在" == "没有丢弃"）。

    返回里还带两个**必须一起读**的量，防止那两个比率被误读：

    * `no_steps_turns`：压根没有工具步骤的回合（追问 / 校验重试轨迹）。它们按定义
      必然零证据。实测（2026-09-23，753 条）零证据率 35.1% 里，只有 32.7pt 是
      "调了工具却零证据"，另外 2.4pt 是这些回合。
    * `field_live_in_window`：窗口里到底有没有一条**可能**携带 `dropped_arguments`
      的回合（见 `DROPPED_ARGUMENTS_LANDED_AT`）。为 False 时那个 0.0% 是
      **结构性为零**，不是"没有丢弃"。

    **只读**用 `mode=ro` 打开：度量动作不该有权限去改被度量的库
    （`scripts/clean_test_polluted_reports.py` 的整个存在就是测试污染生产库的证据）。
    库不在或读不动时返回 `available=False` + 原因，比率给 `None`——不给 0.0%。
    """
    target = Path(db_path) if db_path is not None else TRACE_DB
    low = _normalize_bound(since)
    high = _normalize_bound(until)
    out: dict = {
        "available": False,
        "reason": "",
        "db": str(target),
        "since": low,
        "until": high,
        "turns": 0,
        "zero_evidence_turns": 0,
        "zero_evidence_rate": None,
        "no_steps_turns": 0,
        "steps_but_no_evidence_turns": 0,
        "dropped_arguments_turns": 0,
        "dropped_arguments_rate": None,
        "turns_with_steps": 0,
        "first_turn_at": None,
        "last_turn_at": None,
        "field_live_in_window": None,
        "field_live_since": DROPPED_ARGUMENTS_LANDED_AT,
    }

    if not target.exists():
        out["reason"] = f"找不到 trace 库 {target}（无从度量，≠「零问题」）"
        return out

    where: list[str] = []
    params: list[str] = []
    if low:
        where.append("created_at >= ?")
        params.append(low)
    if high:
        where.append("created_at < ?")
        params.append(high)
    sql = "SELECT created_at, steps FROM chat_turn_traces"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at"

    try:
        con = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
        try:
            rows = con.execute(sql, params).fetchall()
        finally:
            con.close()
    except sqlite3.Error as exc:  # 表不存在 / 不是 sqlite / 权限不足
        out["reason"] = f"读 {target} 失败：{type(exc).__name__}: {exc}"
        return out

    zero = 0
    no_steps = 0
    with_steps = 0
    dropped = 0
    for created_at, raw_steps in rows:
        steps = _steps_of(raw_steps)
        if steps:
            with_steps += 1
        else:
            no_steps += 1
        if sum(int(step.get("evidence_count") or 0) for step in steps) == 0:
            zero += 1
        if any("dropped_arguments" in step for step in steps):
            dropped += 1

    turns = len(rows)
    out.update(
        {
            "available": True,
            "turns": turns,
            "zero_evidence_turns": zero,
            "no_steps_turns": no_steps,
            "steps_but_no_evidence_turns": zero - no_steps,
            "dropped_arguments_turns": dropped,
            "turns_with_steps": with_steps,
            "first_turn_at": str(rows[0][0]) if rows else None,
            "last_turn_at": str(rows[-1][0]) if rows else None,
        }
    )
    if turns == 0:
        # 窗口空 ≠ 指标好。说清是窗口的问题，比率留 None。
        out["reason"] = (
            f"窗口 [{low or '不限'} , {high or '不限'}) 内一条回合都没有，无法度量"
        )
        return out

    out["zero_evidence_rate"] = zero / turns
    out["dropped_arguments_rate"] = dropped / turns
    # 窗口里有没有一条**可能**携带该字段的回合（见 DROPPED_ARGUMENTS_LANDED_AT）。
    out["field_live_in_window"] = out["last_turn_at"] >= DROPPED_ARGUMENTS_LANDED_AT
    if not out["field_live_in_window"]:
        out["reason"] = (
            f"窗口内最晚一条 trace 是 {out['last_turn_at']}（UTC），早于 "
            f"`dropped_arguments` 字段落地时刻 {DROPPED_ARGUMENTS_LANDED_AT}（UTC）——"
            "参数丢弃率 0% 是**结构性为零**，不是'没有丢弃'"
        )
    return out


def run(
    *,
    verbose: bool = False,
    with_planner: bool = False,
    trace_since: str | None = None,
    trace_until: str | None = None,
) -> dict:
    # 预热名称表：否则分数反映的是缓存状态而非实现（踩过）
    name_map_size = warm_name_map()
    items = load_golden()
    planner = None
    planner_model = ""
    if with_planner:
        from api.services import chat_service
        from tradingagents.agent.planner_config import describe_planner

        # 模型档案是**按用户**存的：不给 user_id 就解析不到档案，会回落到
        # 环境里的默认配置（实测打到 api.openai.com 并 401）。跑分必须指定用户。
        import os as _os

        planner = chat_service.build_planner(user_id=_os.getenv("TA_PLANNER_USER_ID") or None)
        _cfg = describe_planner()
        print("规划器配置:", _cfg, "| 构造成功:", planner is not None)
        planner_model = str(_cfg.get("model") or _cfg.get("profile_id") or "（跟随回答模型）")
        if planner is None:
            raise SystemExit(
                "规划层被要求启用但构造失败（十有八九是没给 TA_PLANNER_USER_ID）。\n"
                "此时若继续跑，数字是**规则路径**的却会被写进'规划层'口径，\n"
                "比直接失败更危险——所以这里拒绝出报告。"
            )
    probes = {item.id: probe(item.text, planner=planner) for item in items}
    metrics = score(items, probes)

    by_intent: dict[str, Counter] = {}
    for item in items:
        probe_ = probes[item.id]
        bucket = by_intent.setdefault(item.intent, Counter())
        bucket["total"] += 1
        bucket["servable"] += int(probe_.servable)
        bucket["intent_ok"] += int(probe_.intent == item.intent)
        bucket["tools_ok"] += int(
            set(item.required_tools) <= set(probe_.tools)
        )
        # 规划器选了、执行层却丢掉的步骤。正常情况下恒为 0；不为 0 说明两边的
        # 工具面又不一致了——那会让"规划器多调了什么"这个结论失真。
        bucket["dropped"] += int(bool(set(probe_.planned_steps) - set(probe_.tools)))
        bucket["planned"] += int(bool(probe_.planned_steps))

    payload = {
        "name_map_size": name_map_size,
        "metrics": metrics.as_dict(),
        "by_intent": {k: dict(v) for k, v in sorted(by_intent.items())},
        "probes": {k: p.__dict__ for k, p in probes.items()},
        # **口径必须随报告落盘**。此前不落盘，于是同一份 `golden-baseline.md`
        # 既可能是"规则路径"也可能是"规划层启用"的数字，而标题永远写着
        # "Phase 0"——读者无从分辨，对照就失去意义。
        # 实测踩到：同一文件先被规则路径写（规划准确率 48%），
        # 又被规划层写（88%），两次都没有任何一行说明口径变了。
        "with_planner": bool(with_planner),
        # 规划器是否**真的**介入：`with_planner=True` 但档案解析不到时，
        # 会回落到环境默认模型并 401，然后**静默退回规则层**（见上面 run() 的注释）。
        # 那种情况下数字是规则路径的，却会被当成规划层的——所以要把
        # "模型真的跑起来了"与"只是要求跑"分开记。
        "planner_model": planner_model,
        # 真实 trace 度量（零证据率 / 参数丢弃率）。**进 payload 是为了能比对**：
        # 只打印在终端上的数字没法与上一版对照，等于每次都要人肉记。
        "trace": trace_metrics(since=trace_since, until=trace_until),
    }
    if verbose:
        for item in items:
            probe_ = probes[item.id]
            flag = "OK " if probe_.servable else "400"
            print(f"  [{flag}] {item.id} {item.text[:34]:36} → {probe_.intent or '-'}")
    return payload


def render_trace_section(trace: dict | None) -> list[str]:
    """报告里的"真实 trace 度量"小节。

    三条渲染纪律（每一条都对应一个踩过的坑）：

    1. **测不到就写"无法度量"**，不写 0.0%。库不在、窗口内无回合时给 0.0% 会被
       读成"零丢弃、零空证据"——这正是 `tests/test_quality_gates.py` 记录的 M6。
    2. **两个比率都要带分子/分母**。只有百分比时，"35.1%"到底是 264/753 还是
       7/20 分不出来，而后者毫无统计意义。
    3. **零证据率必须拆开**"压根没调工具"与"调了却没证据"，否则读者会把总数
       理解成后者的规模（实测差 2.4pt）。
    """
    if not trace:
        return ["## 真实 trace 度量（只读 `tradingagents.db`）", "", "（未采集）", ""]

    lines = ["## 真实 trace 度量（只读 `tradingagents.db`）", ""]
    if not trace.get("available") or trace.get("turns", 0) == 0:
        lines += [
            f"**无法度量**：{trace.get('reason') or '未知原因'}",
            "",
            "> 这里刻意**不写 0.0%**：把「没测到」渲染成「没问题」会让这两个指标",
            "> 从第一天起就是假的。",
            "",
        ]
        return lines

    turns = trace["turns"]
    zero_n = trace["zero_evidence_turns"]
    drop_n = trace["dropped_arguments_turns"]
    window = f"{trace.get('first_turn_at')} → {trace.get('last_turn_at')}（UTC）"
    if trace.get("since") or trace.get("until"):
        window += f" · 窗口 [{trace.get('since') or '不限'} , {trace.get('until') or '不限'})"
    lines += [
        "| 指标 | 值 | 口径 |",
        "|---|---|---|",
        f"| 零证据率 | **{_fmt_optional(trace.get('zero_evidence_rate'))}** | "
        f"所有 step 的 `evidence_count` 之和为 0 的回合占比（{zero_n}/{turns}） |",
        f"| 参数丢弃率 | **{_fmt_optional(trace.get('dropped_arguments_rate'))}** | "
        f"step 里出现 `dropped_arguments` 的回合占比（{drop_n}/{turns}） |",
        f"| 回合数 | {turns} | 窗口 {window} |",
        f"| 其中无 step 的回合 | {trace.get('no_steps_turns', 0)} | "
        "没有工具步骤（追问 / 校验重试轨迹），按定义必然零证据 |",
        f"| 其中调了工具却零证据 | {trace.get('steps_but_no_evidence_turns', 0)} | "
        "**这才是**「取了数却没拿到可引用值」的规模 |",
        f"| 可能有工具步骤的回合 | {trace.get('turns_with_steps', 0)} | "
        "参数丢弃率的分母上限（只有真调过工具的回合才可能丢参数） |",
        "",
    ]
    if trace.get("field_live_in_window") is False:
        lines += [
            f"> ⚠ **参数丢弃率 {_fmt_optional(trace.get('dropped_arguments_rate'))} 是结构性为零**：",
            f"> 窗口内最晚一条 trace 是 `{trace.get('last_turn_at')}`（UTC），早于 "
            f"`dropped_arguments` 字段落地时刻 `{trace.get('field_live_since')}`（UTC）。",
            "> 也就是说分母里**没有任何一条回合可能携带该字段**——这个 0 不能读成"
            "「W1 之后没再丢过参数」。要测它，得有字段落地之后产生的新 trace。",
            "",
        ]
    return lines


def render(payload: dict) -> str:
    m = payload["metrics"]
    # 标题必须写出口径。此前固定写 "Phase 0"，于是规则路径与规划层的数字
    # 落在同一个标题下——读者无法分辨，对照失去意义。
    mode = "规划层启用（生产口径）" if payload.get("with_planner") else "规则路径（离线）"
    lines = [
        f"# 黄金集报告 · {mode}",
        "",
        "> 由 `scripts/golden_report.py` 生成，**不要手工编辑**。",
        "> 这份报告是后续所有改动的对照基线：没有它就说不清\"更好了\"。",
        "",
        "| 口径 | 值 |",
        "|---|---|",
        f"| 规划层 | {'启用' if payload.get('with_planner') else '未启用（规则路径）'} |",
        f"| 规划器模型 | {payload.get('planner_model') or '（未启用）'} |",
        f"| 黄金集 | {m['total']} 条 |",
        "",
        "## 总体",
        "",
        "| 指标 | 值 | 含义 |",
        "|---|---|---|",
        f"| 覆盖率 | **{_fmt(m['coverage'])}** | 能构造出可服务回合（而非 400 追问）的比例 |",
        f"| 规划准确率 | **{_fmt(m['planning_accuracy'])}** | 意图与标的集合都判对的比例 |",
        f"| 工具召回率 | **{_fmt(m['tool_recall'])}** | 必需工具被计划到的比例 |",
        f"| 工具精确率 | **{_fmt(m['tool_precision'])}** | 计划到的工具中不属噪声的比例 |",
        f"| 澄清正确率 | **{_fmt(m['clarify_accuracy'])}** | 该追问的追问了、不该追问的没问 |",
        f"| 样本数 | {m['total']} | — |",
        f"| 名称表规模 | {payload.get('name_map_size', 0)} | 冷表会让名称类用例全废，分数不可比 |",
        "",
    ]
    lines += render_trace_section(payload.get("trace"))
    lines += [
        "## 按意图拆解",
        "",
        "| 意图 | 样本 | 可服务 | 意图判对 | 工具满足 |",
        "|---|---|---|---|---|",
    ]
    for intent, row in payload["by_intent"].items():
        lines.append(
            f"| `{intent}` | {row['total']} | {row['servable']} | {row['intent_ok']} | {row['tools_ok']} |"
        )

    failures = m["failures"]
    if failures:
        lines += ["", "## 失败明细（前 25 条）", ""]
        lines.append("| id | 问句 | 原因 | 详情 |")
        lines.append("|---|---|---|---|")
        for row in failures[:25]:
            detail = {
                k: v
                for k, v in row.items()
                if k not in ("id", "text", "why")
            }
            lines.append(
                f"| {row.get('id')} | {str(row.get('text', ''))[:30]} | {row.get('why')} | "
                f"`{json.dumps(detail, ensure_ascii=False)}` |"
            )
    else:
        lines += ["", "（无失败项）"]
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="黄金集报告（标题会标明口径：规则路径 / 规划层）")
    parser.add_argument("--json", type=Path, default=None, help="把原始结果写到该路径")
    parser.add_argument("--compare", type=Path, default=None, help="与历史 JSON 对比")
    parser.add_argument("--fail-under", type=float, default=None, help="规划准确率门槛（CI 用）")
    parser.add_argument(
        "--fail-under-recall", type=float, default=None,
        help="工具召回率门槛（CI 用）。离线（规则路径）实测 66.7%，规划层启用时 96.2%——"
             "两个口径的基线不同，门槛必须跟着当前口径走，否则闸门要么常红要么形同虚设",
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--no-write", action="store_true", help="不写 docs 报告")
    parser.add_argument("--with-planner", action="store_true",
                        help="启用真实 LLM 规划层跑分（需要模型可用；对比 ③ 的增益）")
    # 时间窗：两个真实 trace 度量（零证据率 / 参数丢弃率）的取样范围。
    # **边界是 UTC**——`chat_turn_traces.created_at` 存的是 UTC（见模块 docstring）。
    # `--since` 含、`--until` 不含，所以两个相邻窗口拼起来正好是全集。
    parser.add_argument("--since", default=None, metavar="UTC",
                        help="真实 trace 度量窗口下界（含）。UTC，如 2026-09-23T14:32")
    parser.add_argument("--until", default=None, metavar="UTC",
                        help="真实 trace 度量窗口上界（**不含**）。UTC，如 2026-09-24")
    args = parser.parse_args()

    payload = run(
        verbose=args.verbose,
        with_planner=args.with_planner,
        trace_since=args.since,
        trace_until=args.until,
    )
    metrics = payload["metrics"]

    # 标题必须带口径。此前这里写的是无口径的"黄金集基线"，于是
    # 一份"规划层"的数字和一份"规则路径"的数字在终端上长得一模一样——
    # 与报告标题写死"Phase 0"是同一类问题（P/M6）。
    _mode_label = "规划层启用（生产口径）" if args.with_planner else "规则路径（离线）"
    print(f"黄金集 · {_mode_label}")
    print(f"  覆盖率        {_fmt(metrics['coverage'])}")
    print(f"  规划准确率    {_fmt(metrics['planning_accuracy'])}")
    print(f"  工具召回率    {_fmt(metrics['tool_recall'])}")
    print(f"  工具精确率    {_fmt(metrics['tool_precision'])}")
    print(f"  澄清正确率    {_fmt(metrics['clarify_accuracy'])}")
    print(f"  样本数        {metrics['total']}")

    # 真实 trace 度量。**报分子/分母**：只有百分比时分不清 264/753 与 7/20。
    trace = payload.get("trace") or {}
    print("  真实 trace 度量（只读 tradingagents.db）")
    if trace.get("available") and trace.get("turns"):
        turns = trace["turns"]
        print(
            f"    零证据率      {_fmt_optional(trace['zero_evidence_rate'])}"
            f"（{trace['zero_evidence_turns']}/{turns}；其中无 step 的 "
            f"{trace['no_steps_turns']} 条，调了工具却零证据的 "
            f"{trace['steps_but_no_evidence_turns']} 条）"
        )
        print(
            f"    参数丢弃率    {_fmt_optional(trace['dropped_arguments_rate'])}"
            f"（{trace['dropped_arguments_turns']}/{turns}）"
        )
        print(f"    窗口          {trace['first_turn_at']} → {trace['last_turn_at']}（UTC）")
        if trace.get("field_live_in_window") is False:
            # 0% 会被读成"没有丢弃"，必须当场说明它是**结构性为零**。
            print(f"    ⚠ {trace.get('reason')}")
    else:
        # 库不在 / 窗口内没有回合 → 说"无法度量"，**不写 0.0%**。
        print(f"    ⚠ 无法度量：{trace.get('reason') or '未知原因'}")

    # 未登记的 capability 不参与任何指标，因此必须**单独喊出来**：
    # 静默忽略等于把一条产品预期从账上抹掉（本轮 9 个新 L1 工具就这么被抹掉过）。
    unmapped = metrics.get("unmapped_capabilities") or []
    if unmapped:
        print(f"  ⚠ 无法评分的能力 {len(unmapped)} 条（既不是工具也没登记为刻意不做成工具）")
        for row in unmapped[:10]:
            print(f"      {row['id']} → {row['capability']}")

    if not args.no_write:
        # **两种口径写两个文件**。写死同一个路径的后果实测过：跑一次规划层
        # 就把规则路径的基线覆盖了，而"改前 vs 改后"的比较锚点正是那个文件——
        # 测量动作本身把测量基准毁掉。
        target = (
            ROOT / "docs" / "golden-with-planner.md"
            if args.with_planner
            else REPORT_PATH
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render(payload), encoding="utf-8")
        print(f"报告已写入 {target.relative_to(ROOT)}")

    if args.json:
        # **保留 probes**：它是"多调了哪些工具/精度损失来自哪里"的唯一原始依据。
        # 曾经把它过滤掉，结果要分析精确率时只能重跑 50 次 LLM 调用。
        args.json.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"JSON 已写入 {args.json}")

    if args.compare and args.compare.exists():
        old_payload = json.loads(args.compare.read_text(encoding="utf-8"))
        old = old_payload["metrics"]
        print("\n与", args.compare, "对比：")
        for key in ("coverage", "planning_accuracy", "tool_recall", "tool_precision", "clarify_accuracy"):
            delta = metrics[key] - old.get(key, 0.0)
            arrow = "↑" if delta > 1e-9 else ("↓" if delta < -1e-9 else "=")
            print(f"  {key:20} {_fmt(old.get(key, 0.0))} → {_fmt(metrics[key])}  {arrow}{abs(delta) * 100:.1f}pt")

        # 两个真实 trace 度量也要能"改前 vs 改后"比对。**只在两边都测到时**才比：
        # 拿一个"未测到"（None）当 0.0 去算 delta，会凭空造出一个巨大的"改善"。
        old_trace = old_payload.get("trace") or {}
        for key, label in (
            ("zero_evidence_rate", "零证据率"),
            ("dropped_arguments_rate", "参数丢弃率"),
        ):
            before, after = old_trace.get(key), trace.get(key)
            if before is None or after is None:
                print(f"  {label:20} 无法比对（改前 {_fmt_optional(before)} / 改后 {_fmt_optional(after)}）")
                continue
            delta = after - before
            arrow = "↑" if delta > 1e-9 else ("↓" if delta < -1e-9 else "=")
            print(f"  {label:20} {_fmt(before)} → {_fmt(after)}  {arrow}{abs(delta) * 100:.1f}pt")

    if args.fail_under is not None and metrics["planning_accuracy"] < args.fail_under:
        print(
            f"\n[FAIL] 规划准确率 {_fmt(metrics['planning_accuracy'])} < 门槛 {_fmt(args.fail_under)}",
            file=sys.stderr,
        )
        return 1
    if args.fail_under_recall is not None and metrics["tool_recall"] < args.fail_under_recall:
        print(
            f"\n[FAIL] 工具召回率 {_fmt(metrics['tool_recall'])} < 门槛 "
            f"{_fmt(args.fail_under_recall)}（当前口径："
            f"{'规划层启用' if args.with_planner else '规则路径（离线）'}）",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
