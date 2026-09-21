#!/usr/bin/env python
"""识别并（可选）清理被测试夹具写进生产库的研报行。

## 背景

本仓库此前没有 `tests/conftest.py`，而 `api/database.py` 的 `DATABASE_URL` 默认是
`sqlite:///./tradingagents.db`，测试通过 `get_db_ctx()` 直连该库写入数据。于是测试
夹具行与真实研判行混在同一个 `reports` 表里，任何按 `direction` 聚合的统计都被污染。

`tests/conftest.py` 已阻断**未来**的污染，但**已经落库**的那些行还在。

## 判定规则（保守，宁可漏杀不可错杀）

R1 「非规范方向」：`direction` 取值不在规范词表内（例如 `bullish` / `bearish`）。
   真实写入路径会规范化（`report_service._extract_verdict` → `extract_direction_result`），
   所以这些值**只可能**来自直接 INSERT（测试）或 B2 之前的旧代码。
   → 单靠 R1 不足以判死刑：旧代码写入的是**真实**用户数据。

R2 「同刻密集写入」：同一 `created_at`（精确到微秒）上有 ≥ `--burst` 行。
   测试夹具在循环里批量插入，微秒级同刻是强烈特征；真实分析每次都要跑完
   LLM 流程，不可能多份报告共享同一微秒。

只有 **R1 ∧ R2** 才判为污染行。这是刻意的保守取舍：真实旧数据可能满足 R1，
   但几乎不可能同时满足 R2。

## 用法

    # 只看，不删（默认）
    .venv/bin/python scripts/clean_test_polluted_reports.py

    # 显示将被删除的样本行
    .venv/bin/python scripts/clean_test_polluted_reports.py --samples 20

    # 真正执行删除（先自动备份数据库）
    .venv/bin/python scripts/clean_test_polluted_reports.py --apply

`--apply` 会先把整库复制到 `tradingagents.db.bak-<时间戳>`，再在事务里删除。
没有备份就不会动手。
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "tradingagents.db"

# 与 `tradingagents/agents/utils/direction.py` 的规范方向词表一致。
CANONICAL_DIRECTIONS = {
    "看多",
    "偏多",
    "中性",
    "偏空",
    "看空",
    # 允许的等价写法（历史/英文），仍算"规范"
    "bullish_legacy_placeholder",  # 占位：真实词表不含任何英文，见下方说明
}
# 说明：规范化后只应出现中文五档。把英文算作非规范正是本脚本的目的，所以这里
# 刻意不把它们列进白名单。
CANONICAL_DIRECTIONS.discard("bullish_legacy_placeholder")


def _candidates(conn: sqlite3.Connection, burst: int) -> list[tuple]:
    """返回 (id, symbol, direction, created_at, burst_size)。

    注意：不要用关联子查询数 burst —— `reports` 装着整份研报正文（库 ~600MB），
    对每一行做一次全表扫描会慢到跑不完。用一次 GROUP BY 建计数表再 join。
    """
    conn.execute("DROP TABLE IF EXISTS temp._burst")
    conn.execute(
        "CREATE TEMP TABLE _burst AS "
        "SELECT created_at, COUNT(*) AS n FROM reports "
        "WHERE created_at IS NOT NULL GROUP BY created_at"
    )
    conn.execute("CREATE INDEX temp.ix_burst ON _burst(created_at)")
    rows = conn.execute(
        """
        SELECT r.id, r.symbol, r.direction, r.created_at, b.n
        FROM reports r JOIN _burst b ON b.created_at = r.created_at
        WHERE r.created_at IS NOT NULL AND b.n >= ?
        """,
        (burst,),
    ).fetchall()
    out = []
    for rid, symbol, direction, created_at, burst_size in rows:
        d = (direction or "").strip()
        # R1：方向非规范（空串也算——真实写入不会留空）
        if d in CANONICAL_DIRECTIONS:
            continue
        out.append((rid, symbol, d or "<NULL>", created_at, burst_size))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"数据库路径（默认 {DEFAULT_DB}）")
    ap.add_argument("--burst", type=int, default=5, help="同微秒行数阈值，达到才算批量夹具写入（默认 5）")
    ap.add_argument("--samples", type=int, default=10, help="打印多少条样本行")
    ap.add_argument("--apply", action="store_true", help="真正执行删除（会先自动备份）")
    args = ap.parse_args()

    if not args.db.exists():
        print(f"找不到数据库：{args.db}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(args.db)
    try:
        total = conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
        non_canonical = conn.execute(
            "SELECT COUNT(*) FROM reports WHERE direction IS NULL OR TRIM(direction) NOT IN "
            "('看多','偏多','中性','偏空','看空')"
        ).fetchone()[0]
        cands = _candidates(conn, args.burst)

        print(f"数据库：{args.db}")
        print(f"reports 总行数                        : {total}")
        print(f"  其中方向非规范（仅 R1，含真实旧数据）: {non_canonical}")
        print(f"  同时满足 R1 ∧ R2（判为夹具污染）    : {len(cands)}")

        if not cands:
            print("\n没有判定为污染的行。")
            return 0

        by_day: dict[str, int] = {}
        for _rid, _sym, _d, created_at, _b in cands:
            day = str(created_at)[:10]
            by_day[day] = by_day.get(day, 0) + 1
        print("\n按日期分布：")
        for day, n in sorted(by_day.items()):
            print(f"  {day}  {n}")

        print(f"\n样本（最多 {args.samples} 条）：")
        for rid, symbol, direction, created_at, burst in cands[: args.samples]:
            print(f"  {rid[:12]}… {symbol:12} direction={direction:10} burst={burst:3}  {created_at}")

        if not args.apply:
            print("\n（演练模式，未删除任何数据。加 --apply 才执行。）")
            return 0

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = args.db.with_name(f"{args.db.name}.bak-{stamp}")
        shutil.copy2(args.db, backup)
        print(f"\n已备份到：{backup}")

        ids = [(rid,) for rid, *_ in cands]
        cur = conn.cursor()
        cur.execute("BEGIN")
        try:
            cur.executemany("DELETE FROM reports WHERE id = ?", ids)
            deleted = cur.rowcount
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        remaining = conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
        print(f"已删除 {deleted} 行；reports 现在 {remaining} 行。")
        print("如需回滚：把备份文件覆盖回原路径即可。")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
