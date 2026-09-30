#!/usr/bin/env python
"""CLI：手动跑一轮数据源探测。

用途（与页面上的「立即探测」是同一条代码路径，只是不经过 HTTP）：

* 部署后/升级依赖后**第一时间**确认各源是否真的可用
* 排查线上问题时，先拿到一份带版本指纹的现场快照
* CI 里做冒烟检查（`--no-persist` 避免污染探测历史）

示例：
    # 全量探测并落库（页面可见）
    python scripts/probe_data_sources.py

    # 只看 Baostock 的日线，不落库，机器可读
    python scripts/probe_data_sources.py --source cn_baostock --tool get_daily_bars_df \\
        --no-persist --json

    # 只列出会探哪些目标，不发任何请求（CI 里校验矩阵接线）
    python scripts/probe_data_sources.py --list

退出码：0 = 无失败；1 = 有 failed（便于 CI/告警直接使用）；2 = 没有匹配目标。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.database import SessionLocal, init_db  # noqa: E402
from api.services import data_source_probe_service as probe  # noqa: E402

_STATUS_ICON = {
    probe.STATUS_OK: "OK  ",
    probe.STATUS_DEGRADED: "DEGR",
    #: 超时单独一个标记：与 FAIL 同为红色系但必须看得出来是"慢"不是"挂"
    probe.STATUS_TIMEOUT: "SLOW",
    probe.STATUS_FAILED: "FAIL",
    probe.STATUS_UNSUPPORTED: "N/A ",
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="数据源探测（真实最小请求）")
    parser.add_argument("--source", action="append", help="只看这些数据源（可重复）")
    parser.add_argument("--tool", action="append", help="只看这些维度/tool（可重复）")
    parser.add_argument(
        "--deadline", type=float, default=probe.DEFAULT_DEADLINE_SECONDS,
        help=f"单个目标硬截止秒数（默认 {probe.DEFAULT_DEADLINE_SECONDS:g}）",
    )
    parser.add_argument(
        "--no-persist", action="store_true", help="不写入数据库（不影响页面历史）"
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument(
        "--list",
        dest="list_targets",
        action="store_true",
        help="只列出将探测的目标，不发请求、不落库（离线可用）",
    )
    return parser


def _select_targets(args) -> list[tuple[str, str]]:
    targets = probe.probe_targets()
    if args.source:
        wanted = set(args.source)
        targets = [t for t in targets if t[0] in wanted]
    if args.tool:
        wanted_tools = set(args.tool)
        targets = [t for t in targets if t[1] in wanted_tools]
    return targets


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    targets = _select_targets(args)

    if not targets:
        print("没有匹配的探测目标（检查 --source/--tool）", file=sys.stderr)
        return 2

    if args.list_targets:
        # 离线模式：**不建连接、不建表、不发请求**。
        # 存在的意义是让"矩阵接线对不对"能在 CI 里被验证，而不必真的打网络。
        if args.json:
            print(json.dumps([{"source": s, "tool": t} for s, t in targets], ensure_ascii=False, indent=2))
        else:
            print(f"将探测 {len(targets)} 个目标（未发请求）：")
            for source, tool in targets:
                print(f"  {source:<20} {tool}")
        return 0

    ctx = probe.ProbeContext.build()
    # 必须显式建表：`create_all` 只在 `init_db()` 里调用（应用侧由 lifespan 触发），
    # 而本脚本可能跑在应用从未启动过的机器上——否则会探完一圈才发现
    # "no such table: data_source_probe_results"，白跑一次真实网络请求。
    init_db()
    db = SessionLocal()
    try:
        summary = probe.run_probe(
            db,
            trigger=probe.TRIGGER_MANUAL,
            ctx=ctx,
            deadline_seconds=args.deadline,
            persist=not args.no_persist,
            targets=targets,
        )
    finally:
        db.close()

    if args.json:
        print(
            json.dumps(
                {
                    "id": summary.id,
                    "status": summary.status,
                    "trading_day": summary.trading_day,
                    "duration_ms": summary.duration_ms,
                    "total_count": summary.total_count,
                    "ok_count": summary.ok_count,
                    "degraded_count": summary.degraded_count,
                    "timeout_count": summary.timeout_count,
                    "failed_count": summary.failed_count,
                    "unsupported_count": summary.unsupported_count,
                    "results": [
                        {
                            "source": o.source,
                            "tool": o.tool,
                            "status": o.status,
                            "latency_ms": o.latency_ms,
                            "row_count": o.row_count,
                            "row_unit": o.row_unit,
                            "version": o.version,
                            "error_kind": o.error_kind,
                            "error_message": o.error_message,
                        }
                        for o in summary.outcomes
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(
            f"探测基准日 {ctx.day}（{'交易日' if summary.trading_day else '非交易日'}）"
            f" 用时 {summary.duration_ms}ms"
        )
        print("-" * 100)
        for o in sorted(summary.outcomes, key=lambda o: (o.source, o.tool)):
            icon = _STATUS_ICON.get(o.status, o.status)
            detail = ""
            if o.status in (probe.STATUS_FAILED, probe.STATUS_DEGRADED, probe.STATUS_TIMEOUT):
                detail = f"  <- {o.error_kind}: {(o.error_message or '')[:80]}"
            version = f"v{o.version}" if o.version else ""
            if o.row_count is None:
                rows = ""
            elif getattr(o, "row_unit", None) == probe.ROW_UNIT_LINES:
                rows = f"≈{o.row_count}行"   # 文本载体：非空行数，不是记录数
            else:
                rows = f"{o.row_count}行"
            print(
                f"{icon} {o.source:<20} {o.tool:<26} {o.latency_ms:>6}ms "
                f"{rows:>8} {version:<12}{detail}"
            )
        print("-" * 100)
        print(
            f"合计 {summary.total_count}：可用 {summary.ok_count} / 降级 "
            f"{summary.degraded_count} / 超时 {summary.timeout_count} / 失败 "
            f"{summary.failed_count} / 不支持 {summary.unsupported_count} "
            f"-> {summary.status}"
        )

    return 1 if summary.failed_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
