#!/usr/bin/env python
"""跑黄金集，产出**基线报告**（Phase 0 的交付物 0.4）。

用法：

    .venv/bin/python scripts/golden_report.py                 # 打印 + 写 docs/golden-baseline.md
    .venv/bin/python scripts/golden_report.py --json out.json # 只写 JSON（供 CI 比对）
    .venv/bin/python scripts/golden_report.py --compare old.json

为什么要有 compare：Phase 1 之后必须能回答"更好了吗"。没有可机器比对的
上一版分数，"更好了"就只是一句感觉。门槛写死在 `--fail-under` 里供 CI 使用。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.golden.harness import load_golden, probe, score, warm_name_map  # noqa: E402

REPORT_PATH = ROOT / "docs" / "golden-baseline.md"


def _fmt(value: float) -> str:
    return f"{value * 100:.1f}%"


def run(*, verbose: bool = False) -> dict:
    # 预热名称表：否则分数反映的是缓存状态而非实现（踩过）
    name_map_size = warm_name_map()
    items = load_golden()
    probes = {item.id: probe(item.text) for item in items}
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

    payload = {
        "name_map_size": name_map_size,
        "metrics": metrics.as_dict(),
        "by_intent": {k: dict(v) for k, v in sorted(by_intent.items())},
        "probes": {k: p.__dict__ for k, p in probes.items()},
    }
    if verbose:
        for item in items:
            probe_ = probes[item.id]
            flag = "OK " if probe_.servable else "400"
            print(f"  [{flag}] {item.id} {item.text[:34]:36} → {probe_.intent or '-'}")
    return payload


def render(payload: dict) -> str:
    m = payload["metrics"]
    lines = [
        "# 黄金集基线报告（Phase 0）",
        "",
        "> 由 `scripts/golden_report.py` 生成，**不要手工编辑**。",
        "> 这份报告是后续所有改动的对照基线：没有它就说不清\"更好了\"。",
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
    parser = argparse.ArgumentParser(description="黄金集基线报告")
    parser.add_argument("--json", type=Path, default=None, help="把原始结果写到该路径")
    parser.add_argument("--compare", type=Path, default=None, help="与历史 JSON 对比")
    parser.add_argument("--fail-under", type=float, default=None, help="规划准确率门槛（CI 用）")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--no-write", action="store_true", help="不写 docs 报告")
    args = parser.parse_args()

    payload = run(verbose=args.verbose)
    metrics = payload["metrics"]

    print("黄金集基线")
    print(f"  覆盖率        {_fmt(metrics['coverage'])}")
    print(f"  规划准确率    {_fmt(metrics['planning_accuracy'])}")
    print(f"  工具召回率    {_fmt(metrics['tool_recall'])}")
    print(f"  工具精确率    {_fmt(metrics['tool_precision'])}")
    print(f"  澄清正确率    {_fmt(metrics['clarify_accuracy'])}")
    print(f"  样本数        {metrics['total']}")

    if not args.no_write:
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(render(payload), encoding="utf-8")
        print(f"报告已写入 {REPORT_PATH.relative_to(ROOT)}")

    if args.json:
        args.json.write_text(
            json.dumps({k: v for k, v in payload.items() if k != "probes"}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"JSON 已写入 {args.json}")

    if args.compare and args.compare.exists():
        old = json.loads(args.compare.read_text(encoding="utf-8"))["metrics"]
        print("\n与", args.compare, "对比：")
        for key in ("coverage", "planning_accuracy", "tool_recall", "tool_precision", "clarify_accuracy"):
            delta = metrics[key] - old.get(key, 0.0)
            arrow = "↑" if delta > 1e-9 else ("↓" if delta < -1e-9 else "=")
            print(f"  {key:20} {_fmt(old.get(key, 0.0))} → {_fmt(metrics[key])}  {arrow}{abs(delta) * 100:.1f}pt")

    if args.fail_under is not None and metrics["planning_accuracy"] < args.fail_under:
        print(
            f"\n[FAIL] 规划准确率 {_fmt(metrics['planning_accuracy'])} < 门槛 {_fmt(args.fail_under)}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
