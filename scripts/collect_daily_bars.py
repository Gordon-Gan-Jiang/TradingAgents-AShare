"""盘后日线采集：把 provider 链取到的日线落到本地库（`daily_bars`）。

## 为什么是盘后、为什么是定时

日线取数走 provider 降级链（东财 → 新浪 → 腾讯），而东财系接口存在 **IP 级封禁**
（实测 `RemoteDisconnected`）。判定逐次实时取数，等于持续把自己推向封禁；一旦三家
全挂，判定就没有日线可用。日线是不可变的历史（昨天的收盘价今天不会变），因此把它
按"每只每天一次"采集下来，读取侧就能"先库后网"（见 `api/services/daily_bar_store.py`）。

## 用法

    python scripts/collect_daily_bars.py --symbols 600519.SH,300750.SZ
    python scripts/collect_daily_bars.py --symbols-file symbols.txt --days 250
    python scripts/collect_daily_bars.py --limit 50 --sleep 0.5

* `--symbols`  逗号分隔（裸代码会自动补市场后缀）
* `--symbols-file`  每行一个代码，`#` 开头与空行忽略
* `--limit N`  只采前 N 只（先跑通再全量）
* `--days N`   回补多少根日线（默认 250）
* `--sleep S`  每只之间的间隔（默认 0.3s）——**必须限流**，不限流等于自己制造封禁

结束时打印成功数 / 失败数 / 总写入行数，并逐条列出失败标的；有任何失败就以非 0
退出码结束，避免"定时任务永远绿灯而数据其实没采到"。
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date as _date
from datetime import timedelta
from pathlib import Path
from typing import Callable, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # 允许 `python scripts/xxx.py` 直接运行
    sys.path.insert(0, str(ROOT))

logger = logging.getLogger("collect_daily_bars")

# 自然日窗口相对"期望交易日根数"的放大倍数：250 个交易日横跨约 350+ 个自然日，
# 再算上春节/国庆长假与停牌，2 倍是"宁可多取一点"的一侧。
_CALENDAR_PADDING = 2

# 取到数据后写进库的来源标签。日线走的是 provider 链，而 `_fetch_hist_df` 不回报
# 是哪一个源成功的；这里如实记 "provider"，而不是猜一个 "sina"/"tencent"——
# 猜错会让"哪条源在退化"这种诊断变成假线索。
DEFAULT_SOURCE = "provider"


# ── 纯函数：计划采什么（可在无网络环境下单测） ────────────────────────────


def parse_symbols(raw: str | None) -> list[str]:
    """解析 `--symbols`：逗号分隔、去重、按输入顺序保留、收敛成规范键。"""
    from api.services.daily_bar_store import canonical_symbol

    if not raw:
        return []
    out: list[str] = []
    for token in str(raw).split(","):
        token = token.strip()
        if not token:
            continue
        symbol = canonical_symbol(token)
        if symbol not in out:
            out.append(symbol)
    return out


def read_symbols_file(path: str) -> list[str]:
    """读标的文件：每行一个，空行与 `#` 注释忽略。"""
    from api.services.daily_bar_store import canonical_symbol

    out: list[str] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        token = line.split("#", 1)[0].strip()
        if not token:
            continue
        symbol = canonical_symbol(token)
        if symbol not in out:
            out.append(symbol)
    return out


def plan_symbols(
    *,
    symbols: str | None,
    symbols_file: str | None,
    limit: int | None = None,
) -> list[str]:
    """决定这次采哪些标的。

    没有标的时**报错**而不是返回空列表：定时任务里"成功采集 0 只"和"数据齐全"
    在退出码上看不出区别，那是最容易长期潜伏的静默失败。
    """
    planned: list[str] = []
    for symbol in parse_symbols(symbols) + (read_symbols_file(symbols_file) if symbols_file else []):
        if symbol not in planned:
            planned.append(symbol)
    if not planned:
        raise ValueError("没有要采集的标的：请用 --symbols 或 --symbols-file 指定")
    if limit is not None and limit > 0:
        planned = planned[:limit]
    return planned


def collection_window(*, end: str | None = None, days: int = 250) -> tuple[str, str]:
    """把"要多少根日线"换算成 provider 能接受的 `(start, end)` 自然日区间。

    自然日必须放大：250 根交易日横跨约一年，直接按 250 个自然日去取会少掉三分之一。
    """
    if end:
        end_day = end
    else:
        from tradingagents.dataflows.trade_calendar import cn_today_str

        end_day = cn_today_str()
    span = max(int(days), 1) * _CALENDAR_PADDING
    start_day = (_date.fromisoformat(end_day) - timedelta(days=span)).isoformat()
    return start_day, end_day


# ── 采集 ──────────────────────────────────────────────────────────────────


def default_fetcher() -> Callable[[str, str, str], object]:
    """默认取数：复用 `cn_research_data` 的日线入口（经 vendor 路由）。

    刻意不直连 akshare：只有经 `route_to_vendor` 才能共享既有的三级降级链与失败
    冷却；另写一套取数会让两条路随时间漂移，而漂移的那一侧没有测试覆盖。
    """
    from tradingagents.dataflows.providers.cn_research_data import fetch_daily_bars_via_router

    return fetch_daily_bars_via_router


def collect_symbol(
    symbol: str,
    *,
    days: int,
    end: str | None,
    fetch: Callable[[str, str, str], object],
) -> int:
    """采一只标的并入库，返回写入行数。

    返回 0 行也**不算失败**：新股上市不足、长期停牌都会给出空结果，这是事实而不是
    错误；真正的错误（取数抛异常）由调用方记入失败清单。
    """
    from api.services.daily_bar_store import persist_frame

    start, end_day = collection_window(end=end, days=days)
    frame = fetch(symbol, start, end_day)
    if frame is None or len(frame) == 0:
        return 0

    # 只保留最近 days 根：区间是按自然日放大的，落库按"根数"回收到请求口径
    tail = frame.tail(int(days)) if len(frame) > int(days) else frame
    return persist_frame(symbol, tail, source=DEFAULT_SOURCE)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="盘后采集 A 股日线到本地库（daily_bars）")
    parser.add_argument("--symbols", default=None, help="逗号分隔的标的，如 600519.SH,300750.SZ")
    parser.add_argument("--symbols-file", default=None, help="标的文件，每行一个代码")
    parser.add_argument("--limit", type=int, default=None, help="只采前 N 只")
    parser.add_argument("--days", type=int, default=250, help="回补的日线根数（默认 250）")
    parser.add_argument("--sleep", type=float, default=0.3, help="每只之间的间隔秒数（默认 0.3，限流用）")
    parser.add_argument("--end", default=None, help="截止日期 YYYY-MM-DD（默认今天，用于回补历史）")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    try:
        symbols = plan_symbols(symbols=args.symbols, symbols_file=args.symbols_file, limit=args.limit)
    except (ValueError, OSError) as exc:
        # 参数错误要给人看得懂的一句话，而不是一段 traceback——定时任务里
        # traceback 只会被当成"又一个偶发失败"，真正的原因（没给标的）反而看不清
        print(f"参数错误：{exc}", file=sys.stderr)
        return 2

    # 定时任务常常先于服务重启跑：此时 `daily_bars` 可能还不存在，显式建表比抛
    # OperationalError 更符合"盘后自动采集"的用法（init_db 本身是幂等的）。
    from api.database import init_db

    init_db()

    fetch = default_fetcher()

    succeeded = 0
    total_written = 0
    failures: list[tuple[str, str]] = []

    print(f"开始采集 {len(symbols)} 只标的，回补 {args.days} 根日线，间隔 {args.sleep}s")
    for index, symbol in enumerate(symbols, start=1):
        try:
            written = collect_symbol(symbol, days=args.days, end=args.end, fetch=fetch)
            succeeded += 1
            total_written += written
            print(f"[{index}/{len(symbols)}] {symbol} 写入 {written} 行")
        except Exception as exc:  # noqa: BLE001 — 单只失败不该中断整批
            detail = f"{type(exc).__name__}: {exc}"
            failures.append((symbol, detail))
            print(f"[{index}/{len(symbols)}] {symbol} 失败：{detail}")
        # 限流：每只之间必须间隔，不限流等于自己制造 IP 封禁
        if index < len(symbols) and args.sleep > 0:
            time.sleep(args.sleep)

    print("—" * 48)
    print(f"成功 {succeeded} 只 / 失败 {len(failures)} 只 / 总写入 {total_written} 行")
    if failures:
        print("失败清单：")
        for symbol, detail in failures:
            print(f"  - {symbol}: {detail}")

    # 有失败就非 0 退出：否则"定时任务绿灯 + 数据没采到"可以长期没人发现
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
