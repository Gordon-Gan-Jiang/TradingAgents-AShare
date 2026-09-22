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


def run(*, verbose: bool = False, with_planner: bool = False) -> dict:
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
    }
    if verbose:
        for item in items:
            probe_ = probes[item.id]
            flag = "OK " if probe_.servable else "400"
            print(f"  [{flag}] {item.id} {item.text[:34]:36} → {probe_.intent or '-'}")
    return payload


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
    parser.add_argument(
        "--fail-under-recall", type=float, default=None,
        help="工具召回率门槛（CI 用）。离线（规则路径）实测 66.7%，规划层启用时 96.2%——"
             "两个口径的基线不同，门槛必须跟着当前口径走，否则闸门要么常红要么形同虚设",
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--no-write", action="store_true", help="不写 docs 报告")
    parser.add_argument("--with-planner", action="store_true",
                        help="启用真实 LLM 规划层跑分（需要模型可用；对比 ③ 的增益）")
    args = parser.parse_args()

    payload = run(verbose=args.verbose, with_planner=args.with_planner)
    metrics = payload["metrics"]

    print("黄金集基线")
    print(f"  覆盖率        {_fmt(metrics['coverage'])}")
    print(f"  规划准确率    {_fmt(metrics['planning_accuracy'])}")
    print(f"  工具召回率    {_fmt(metrics['tool_recall'])}")
    print(f"  工具精确率    {_fmt(metrics['tool_precision'])}")
    print(f"  澄清正确率    {_fmt(metrics['clarify_accuracy'])}")
    print(f"  样本数        {metrics['total']}")

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
