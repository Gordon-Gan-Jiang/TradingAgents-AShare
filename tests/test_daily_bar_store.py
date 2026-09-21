"""本地日线库：幂等写入、先库后网、以及采集脚本的纯函数。

## 这份测试要挡住的三类事故

1. **重复采集堆重复行**：盘后定时采集每天都会重跑，且经常回补同一段历史。
   若 `(symbol, date)` 不是唯一键，"采两次"就变成"库里两份日线"，而所有按行数
   取尾部的窗口（信号层取 tail(window)）都会悄悄吃到重复 K 线——数值不报错，
   结论变错。
2. **本地库永远查不中**：读取侧用 `600519.SH`、采集侧若原样存裸代码 `600519`，
   两张写法不收敛，于是"先库后网"退化成"每次都打外网"，且**没有任何报错**。
   本文件用 `canonical_symbol` 把这个收敛变成断言。
3. **把盘中未收盘的那根当成收盘价**：`_fetch_hist_df` 在盘中会追加一根实时行
   （见 `_maybe_append_realtime_row`）。若把它落库，之后所有请求都会把 10:00
   的价格当成"今日收盘"，静默地把判定建立在错误的收盘价上。落库必须丢弃
   未定稿的当日行。

## 不联网

本文件不打外网：取数一律用假的 DataFrame 或断言"取数函数根本没被调用"。
采集脚本只测纯函数（解析 `--symbols`、规划标的、代码规范化）。
"""

from __future__ import annotations

import itertools
from datetime import datetime

import pandas as pd
import pytest

from tradingagents.dataflows.trade_calendar import CN_TZ
from tradingagents.graph.data_collector import _OHLCV_COLS

# 临时测试库是整个进程共享的，写死标的代码会让用例之间互相污染
_COUNTER = itertools.count(1)


def _sym(code: str = "6") -> str:
    """每个用例拿一个独占的标的代码（同进程内唯一）。"""
    return f"{code[0]}{next(_COUNTER):05d}.SH"


def _bars(start: str, count: int, *, close0: float = 10.0) -> list[dict]:
    """连续自然日的一组日线，便于断言排序与截断。"""
    base = pd.Timestamp(start)
    return [
        {
            "date": (base + pd.Timedelta(days=i)).strftime("%Y-%m-%d"),
            "open": close0 + i,
            "high": close0 + i + 0.2,
            "low": close0 + i - 0.2,
            "close": close0 + i,
            "volume": 1000.0 + i,
            "amount": 1.0e6 + i,
            "turnover": 1.5,
        }
        for i in range(count)
    ]


def _frame(rows: list[dict]) -> pd.DataFrame:
    """网络帧的形状：小写 OHLCV（`to_signal_bars` 的契约）。"""
    return pd.DataFrame(rows)[list(_OHLCV_COLS)]


# ── 表结构：幂等的地基 ────────────────────────────────────────────────────


def test_table_declares_unique_symbol_date():
    """`(symbol, date)` 必须唯一 —— 幂等 upsert 靠它兜底，而不是靠调用方自觉。"""
    from api.database import DailyBarDB

    uniques = [
        tuple(sorted(c.name for c in constraint.columns))
        for constraint in DailyBarDB.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    ]
    assert ("date", "symbol") in uniques, f"daily_bars 缺少 (symbol, date) 唯一约束：{uniques}"


def test_table_columns_match_the_signal_contract():
    """列名必须能被信号层直接消费（小写 date/close），否则本地数据也用不上。"""
    from api.database import DailyBarDB

    columns = {c.name for c in DailyBarDB.__table__.columns}
    assert {"symbol", "date", "open", "high", "low", "close", "volume"} <= columns
    assert {"amount", "turnover", "source", "ingested_at"} <= columns


# ── upsert / load ─────────────────────────────────────────────────────────


def test_upsert_is_idempotent_and_overwrites_values():
    """同一 symbol+date 写两次：行数不变，值被更新（回补修正历史时才有效）。"""
    from api.services.daily_bar_store import count, load_bars, upsert_bars

    symbol = _sym()
    rows = _bars("2026-09-01", 5)

    assert upsert_bars(symbol, rows, source="provider") == 5
    assert count(symbol) == 5

    # 第二次：同日期、不同值（模拟复权/数据源修正）
    revised = [dict(row, close=99.0) for row in rows]
    assert upsert_bars(symbol, revised, source="provider") == 5

    assert count(symbol) == 5, "重复采集堆出了重复行 —— 唯一约束没有生效"
    stored = load_bars(symbol, end="2026-12-31", lookback=10)
    assert [r["close"] for r in stored] == [99.0] * 5


def test_upsert_skips_rows_without_a_date():
    """没有日期的行不可能幂等（唯一键缺一半），必须丢弃而不是塞个空串进去。"""
    from api.services.daily_bar_store import count, upsert_bars

    symbol = _sym()
    written = upsert_bars(
        symbol,
        [{"date": "", "close": 1.0}, {"close": 2.0}, {"date": "2026-09-01", "close": 3.0}],
        source="provider",
    )

    assert written == 1
    assert count(symbol) == 1


def test_load_bars_returns_ascending_and_truncated():
    """`end` 截断 + `lookback` 取最近 N 条 + 返回升序（信号层按时间递增消费）。"""
    from api.services.daily_bar_store import load_bars, upsert_bars

    symbol = _sym()
    upsert_bars(symbol, _bars("2026-09-01", 30), source="provider")

    rows = load_bars(symbol, end="2026-09-10", lookback=5)

    assert [r["date"] for r in rows] == [
        "2026-09-06",
        "2026-09-07",
        "2026-09-08",
        "2026-09-09",
        "2026-09-10",
    ], "必须是 end 之前最近 5 条，且按日期升序"
    assert rows[-1]["close"] == pytest.approx(10.0 + 9)


def test_load_bars_returns_empty_for_unknown_symbol():
    from api.services.daily_bar_store import load_bars

    assert load_bars(_sym(), end="2026-12-31", lookback=10) == []


def test_symbols_do_not_bleed_into_each_other():
    """多标的互不串扰：串扰的后果是给 A 股喂 B 股的 K 线，数值上完全看不出来。"""
    from api.services.daily_bar_store import count, latest_date, load_bars, upsert_bars

    a, b = _sym(), _sym()
    upsert_bars(a, _bars("2026-09-01", 4, close0=10.0), source="provider")
    upsert_bars(b, _bars("2026-08-01", 2, close0=50.0), source="provider")

    assert count(a) == 4 and count(b) == 2
    assert latest_date(a) == "2026-09-04"
    assert latest_date(b) == "2026-08-02"
    assert all(r["close"] < 20 for r in load_bars(a, end="2026-12-31", lookback=10))


def test_latest_date_and_count_are_none_or_zero_when_absent():
    from api.services.daily_bar_store import count, latest_date

    symbol = _sym()
    assert latest_date(symbol) is None
    assert count(symbol) == 0


def test_load_bars_exposes_source_for_traceability():
    """`source` 必须能取出来：数据源退化（东财被封→新浪→腾讯）时要能事后追溯。"""
    from api.services.daily_bar_store import load_bars, upsert_bars

    symbol = _sym()
    upsert_bars(symbol, _bars("2026-09-01", 2), source="provider")

    assert {r["source"] for r in load_bars(symbol, end="2026-12-31", lookback=10)} == {"provider"}


# ── 代码规范化：两侧键必须收敛 ────────────────────────────────────────────


def test_canonical_symbol_converges_all_spellings():
    """裸代码 / 大小写 / 带交易所前缀都必须收敛到同一个键。"""
    from api.services.daily_bar_store import canonical_symbol

    assert canonical_symbol("600519") == "600519.SH"
    assert canonical_symbol("600519.SH") == "600519.SH"
    assert canonical_symbol("sh600519") == "600519.SH"
    assert canonical_symbol(" 300750.sz ") == "300750.SZ"
    assert canonical_symbol("000001") == "000001.SZ"
    assert canonical_symbol("430047") == "430047.BJ"
    assert canonical_symbol("688981.SH") == "688981.SH"


def test_canonical_symbol_follows_the_research_side_market_rule():
    """市场后缀口径必须与 `cn_research_data._market_of` 一致。

    两处口径不一致的后果不是报错，而是同标的在库里存成两个键：读取永远查不中，
    采集永远在写一个没人读的行。
    """
    from api.services.daily_bar_store import canonical_symbol
    from tradingagents.dataflows.providers.cn_research_data import _market_of

    # 9 开头（B 股）刻意不比对：provider 侧把 9 归沪市，research 侧归深市，本函数跟随沪市口径。
    for code in ("600519", "601318", "688981", "300750", "002594", "000001", "430047", "830799"):
        assert canonical_symbol(code).endswith(f".{_market_of(code).upper()}"), code


def test_upsert_and_load_agree_on_bare_codes():
    """写入用裸代码、读取用带后缀形式时，必须命中同一行。"""
    from api.services.daily_bar_store import count, load_bars, upsert_bars

    symbol = _sym()
    upsert_bars(symbol.split(".")[0], _bars("2026-09-01", 3), source="provider")

    assert count(symbol) == 3
    assert len(load_bars(symbol, end="2026-12-31", lookback=10)) == 3


# ── 帧 ↔ 行 转换 ──────────────────────────────────────────────────────────


def test_upsert_frame_accepts_titlecase_provider_output():
    """provider 的 `_fetch_hist_df` 输出大写列（提示词表格形态），也要能入库。"""
    from api.services.daily_bar_store import count, upsert_frame

    symbol = _sym()
    frame = pd.DataFrame(
        {
            "Date": ["2026-09-01", "2026-09-02"],
            "Open": [10.0, 10.1],
            "High": [10.2, 10.3],
            "Low": [9.9, 10.0],
            "Close": [10.1, 10.2],
            "Volume": [1000, 1100],
        }
    )

    assert upsert_frame(symbol, frame, source="provider") == 2
    assert count(symbol) == 2


def test_upsert_frame_ignores_empty_frame():
    from api.services.daily_bar_store import count, upsert_frame

    symbol = _sym()
    assert upsert_frame(symbol, pd.DataFrame(), source="provider") == 0
    assert upsert_frame(symbol, None, source="provider") == 0
    assert count(symbol) == 0


# ── 先库后网 ──────────────────────────────────────────────────────────────


class _RecordingFetcher:
    """记录被调用的取数器：本文件断言"够用时它一次都不该被调用"。"""

    def __init__(self, frame: pd.DataFrame | None = None, error: Exception | None = None) -> None:
        self.calls: list[tuple] = []
        self._frame = frame
        self._error = error

    def bars(self, symbol: str, end: str, lookback: int) -> pd.DataFrame:
        self.calls.append((symbol, end, lookback))
        if self._error is not None:
            raise self._error
        assert self._frame is not None
        return self._frame


def _settled_clock() -> datetime:
    """盘后 21:00（北京时间）——当日日线已定稿。"""
    return datetime(2026, 9, 21, 21, 0, tzinfo=CN_TZ)


def test_local_first_port_serves_from_db_without_touching_network():
    """库里够用 → 直接返回库数据，取数器**一次都不被调用**。"""
    from api.services.daily_bar_store import LocalFirstDailyBarsPort, upsert_bars

    symbol = _sym()
    upsert_bars(symbol, _bars("2026-08-01", 40), source="provider")

    inner = _RecordingFetcher(error=AssertionError("库里够用却打了外网"))
    port = LocalFirstDailyBarsPort(inner=inner, clock=_settled_clock)

    frame = port.bars(symbol, "2026-09-09", 20)

    assert inner.calls == [], "库内数据够用，不应触网"
    assert list(frame.columns) == list(_OHLCV_COLS), "本地数据必须说信号层的列契约"
    assert len(frame) == 20
    assert frame["date"].iloc[-1] == "2026-09-09"
    assert frame["date"].is_monotonic_increasing


def test_local_first_port_fetches_and_persists_when_db_is_short():
    """库里不够 → 走网络，并把取回的结果顺手入库。"""
    from api.services.daily_bar_store import LocalFirstDailyBarsPort, count

    symbol = _sym()
    fresh = _bars("2026-08-01", 25)
    inner = _RecordingFetcher(frame=_frame(fresh))
    port = LocalFirstDailyBarsPort(inner=inner, clock=_settled_clock)

    frame = port.bars(symbol, "2026-08-25", 20)

    assert inner.calls == [(symbol, "2026-08-25", 20)], "库内不足时必须补网"
    assert frame.equals(_frame(fresh)), "补网时返回的仍是网络结果（行为与改造前一致）"
    assert count(symbol) == 25, "取回的数据没有入库，下一次仍要打外网"


def test_local_first_port_prefers_network_when_db_is_stale():
    """库里有 30 条但最新只到 08-01，请求到 09-09 → 仍不够用，必须补网。"""
    from api.services.daily_bar_store import LocalFirstDailyBarsPort, upsert_bars

    symbol = _sym()
    upsert_bars(symbol, _bars("2026-07-01", 30), source="provider")

    inner = _RecordingFetcher(frame=_frame(_bars("2026-08-10", 30)))
    port = LocalFirstDailyBarsPort(inner=inner, clock=_settled_clock)

    port.bars(symbol, "2026-09-09", 20)

    assert len(inner.calls) == 1, "条数够但截止日太旧，仍属于「不够用」"


def test_local_first_port_refetches_when_requested_lookback_exceeds_store():
    """库里有 10 条、请求 20 条 → 条数不足也要补网。"""
    from api.services.daily_bar_store import LocalFirstDailyBarsPort, upsert_bars

    symbol = _sym()
    upsert_bars(symbol, _bars("2026-09-01", 10), source="provider")

    inner = _RecordingFetcher(frame=_frame(_bars("2026-08-01", 40)))
    port = LocalFirstDailyBarsPort(inner=inner, clock=_settled_clock)

    port.bars(symbol, "2026-09-10", 20)

    assert len(inner.calls) == 1


def test_local_first_port_does_not_persist_unsettled_today_bar():
    """盘中取回的当日行不得入库：否则 10:00 的价会被当成今日收盘，静默污染后续判定。"""
    from api.services.daily_bar_store import LocalFirstDailyBarsPort, latest_date

    symbol = _sym()
    # 网络帧含"今天"这一根（盘中实时行）+ 之前两根
    frame = _frame(_bars("2026-09-19", 3))
    inner = _RecordingFetcher(frame=frame)
    intraday_clock = lambda: datetime(2026, 9, 21, 10, 0, tzinfo=CN_TZ)  # noqa: E731

    port = LocalFirstDailyBarsPort(inner=inner, clock=intraday_clock)
    returned = port.bars(symbol, "2026-09-21", 20)

    assert len(returned) == 3, "盘中这次请求该拿到含实时行的完整结果"
    assert latest_date(symbol) == "2026-09-20", "未定稿的 09-21 不该落库"


def test_local_first_port_persists_today_bar_after_close():
    """收盘后（15:00 之后）当日行已定稿，必须入库，否则盘后判定永远用不上本地库。"""
    from api.services.daily_bar_store import LocalFirstDailyBarsPort, latest_date

    symbol = _sym()
    inner = _RecordingFetcher(frame=_frame(_bars("2026-09-19", 3)))

    port = LocalFirstDailyBarsPort(inner=inner, clock=_settled_clock)
    port.bars(symbol, "2026-09-21", 20)

    assert latest_date(symbol) == "2026-09-21"


def test_local_first_port_survives_a_failing_write():
    """入库失败不能影响返回：采集是"顺手"，读取才是主路径。"""
    from api.services import daily_bar_store
    from api.services.daily_bar_store import LocalFirstDailyBarsPort

    symbol = _sym()
    fresh = _bars("2026-08-01", 25)
    inner = _RecordingFetcher(frame=_frame(fresh))
    port = LocalFirstDailyBarsPort(inner=inner, clock=_settled_clock)

    def _boom(*args, **kwargs):
        raise RuntimeError("disk full")

    original = daily_bar_store.persist_frame
    daily_bar_store.persist_frame = _boom
    try:
        frame = port.bars(symbol, "2026-08-25", 20)
    finally:
        daily_bar_store.persist_frame = original

    assert len(frame) == 25


def test_local_first_returns_network_error_unchanged():
    """网络失败照旧抛出去（与改造前一致）：绝不用"库里只有几条"去凑一个结果。"""
    from api.services.daily_bar_store import LocalFirstDailyBarsPort

    inner = _RecordingFetcher(error=RuntimeError("daily bars empty for X"))
    port = LocalFirstDailyBarsPort(inner=inner, clock=_settled_clock)

    with pytest.raises(RuntimeError, match="daily bars empty"):
        port.bars(_sym(), "2026-09-09", 20)


def test_local_first_falls_back_to_network_when_store_is_broken(monkeypatch):
    """本地库读不了（表还没建/库被锁）时**让路**，不能让原本能跑通的判定失败。

    服务在表建好之前就可能已经在处理请求（`init_db` 只在启动时跑），这条路径
    必须降级成"走网络"，而不是把 OperationalError 抛给用户。
    """
    from api.services import daily_bar_store
    from api.services.daily_bar_store import LocalFirstDailyBarsPort

    def _broken(*args, **kwargs):
        raise RuntimeError("no such table: daily_bars")

    monkeypatch.setattr(daily_bar_store, "load_bars", _broken)

    fresh = _bars("2026-08-01", 25)
    inner = _RecordingFetcher(frame=_frame(fresh))
    port = LocalFirstDailyBarsPort(inner=inner, clock=_settled_clock)

    frame = port.bars(_sym(), "2026-08-25", 20)

    assert len(inner.calls) == 1, "本地库不可用必须降级到网络"
    assert len(frame) == 25


def test_local_first_port_does_not_claim_the_store_for_an_empty_window():
    """`lookback=0` 这种退化入参不能让"够用"判断在空列表上取下标而崩掉。"""
    from api.services.daily_bar_store import LocalFirstDailyBarsPort, upsert_bars

    symbol = _sym()
    upsert_bars(symbol, _bars("2026-09-01", 5), source="provider")

    inner = _RecordingFetcher(frame=_frame(_bars("2026-09-01", 5)))
    port = LocalFirstDailyBarsPort(inner=inner, clock=_settled_clock)

    port.bars(symbol, "2026-09-05", 0)

    assert len(inner.calls) == 1


def test_local_bars_enough_requires_both_count_and_freshness():
    """「够用」= 条数够 **且** 截止日够新；只满足一条时必须判不够。

    两条都写成断言，是因为两种"只满足一条"的误判都会静默改变判定口径：
    条数够但太旧 → 用过期行情判定；足够新但窗口瘦 → 统计量不可靠。
    """
    from api.services.daily_bar_store import local_bars_enough, upsert_bars

    symbol = _sym()
    upsert_bars(symbol, _bars("2026-07-01", 30), source="provider")  # 条数够、但只到 07-30

    assert local_bars_enough(symbol, end="2026-09-09", lookback=20, clock=_settled_clock) is False
    assert local_bars_enough(symbol, end="2026-07-30", lookback=20, clock=_settled_clock) is True
    assert local_bars_enough(symbol, end="2026-07-30", lookback=60, clock=_settled_clock) is False


def test_load_or_fetch_daily_bars_uses_the_local_store_first():
    """底层入口（非端口形态）也要先库后网：range loader 之类可以直接复用。"""
    from api.services.daily_bar_store import load_or_fetch_daily_bars, upsert_bars

    symbol = _sym()
    upsert_bars(symbol, _bars("2026-08-01", 40), source="provider")

    def _fetch(*args, **kwargs):  # pragma: no cover - 被调用即失败
        raise AssertionError("库内够用却打了外网")

    frame = load_or_fetch_daily_bars(
        symbol, end="2026-09-09", lookback=20, fetch=_fetch, clock=_settled_clock
    )
    assert len(frame) == 20


# ── 采集脚本：纯函数 ──────────────────────────────────────────────────────


def test_parse_symbols_splits_dedupes_and_canonicalizes():
    from scripts.collect_daily_bars import parse_symbols

    assert parse_symbols("600519.SH,300750.SZ") == ["600519.SH", "300750.SZ"]
    assert parse_symbols(" 600519 , 300750.SZ ,, 600519 ") == ["600519.SH", "300750.SZ"]
    assert parse_symbols("") == []
    assert parse_symbols(None) == []


def test_read_symbols_file_ignores_blanks_and_comments(tmp_path):
    from scripts.collect_daily_bars import read_symbols_file

    path = tmp_path / "symbols.txt"
    path.write_text("# 关注池\n600519.SH\n\n300750.SZ  \n# 注释行\n", encoding="utf-8")

    assert read_symbols_file(str(path)) == ["600519.SH", "300750.SZ"]


def test_plan_symbols_merges_sources_and_applies_limit():
    from scripts.collect_daily_bars import plan_symbols

    planned = plan_symbols(
        symbols="600519.SH",
        symbols_file=None,
        limit=None,
    )
    assert planned == ["600519.SH"]

    assert plan_symbols(symbols="600519.SH,300750.SZ,000001.SZ", symbols_file=None, limit=2) == [
        "600519.SH",
        "300750.SZ",
    ]


def test_plan_symbols_prefers_file_when_given(tmp_path):
    from scripts.collect_daily_bars import plan_symbols

    path = tmp_path / "s.txt"
    path.write_text("601318.SH\n601398.SH\n", encoding="utf-8")

    assert plan_symbols(symbols=None, symbols_file=str(path), limit=None) == [
        "601318.SH",
        "601398.SH",
    ]


def test_plan_symbols_raises_when_nothing_to_collect():
    """没有标的时必须报错退出，而不是"成功采集 0 只"——后者会让定时任务永远绿灯。"""
    from scripts.collect_daily_bars import plan_symbols

    with pytest.raises(ValueError):
        plan_symbols(symbols="", symbols_file=None, limit=None)


def test_collection_window_covers_requested_trading_days():
    """`--days 250` 要的是 250 根日线，自然日窗口必须放大以覆盖停牌与长假。"""
    from scripts.collect_daily_bars import collection_window

    start, end = collection_window(end="2026-09-21", days=250)

    assert end == "2026-09-21"
    span = (pd.Timestamp(end) - pd.Timestamp(start)).days
    assert span > 250, f"自然日窗口 {span} 天不可能装下 250 根交易日"
