"""本地日线库：把「每次判定都实时打外网取日线」变成「查库、缺了再补网」。

## 为什么这件事必须做

日线取数走的是 provider 降级链（东财 → 新浪 → 腾讯），而东财系接口存在 **IP 级
封禁**（实测 `RemoteDisconnected`）。判定逐次实时取数，等于持续把自己推向封禁；
一旦三家全挂，判定就没有日线可用。日线又是**不可变的历史**——昨天的收盘价今天
不会变——所以它天然适合本地留存：补网频率从「每次请求」降到「每只每天一次」。

## 先库后网放在 api 侧，而不是 `cn_research_data.py` 里

`tradingagents/` 是纯引擎包，**不得 import `api/`**（`tests/test_architecture_boundaries.py`
用 AST 强制）。而本地库要读数据库、要写 `DailyBarDB`，那是应用层的事。因此本模块
（`api/services/`）持有库，取数仍然走既有 provider 链，二者在**装配处**接起来：
`api/services/signal_service.py` 与 `api/services/signal_replay_service.py` 的端口
工厂把 `provider.get_daily_bars_df` 包进 `LocalFirstDailyBarsPort`。

刻意**不**在 `tradingagents/` 里放一个模块级可变单例（"注册一个库回调"）：那个
模式在并发下会串味，测试之间也会互相污染（仓库已有 `test_config_fallback` /
`test_db_isolation` 这类隔离问题的先例）。依赖从装配处传进去，边界与可测性一起保住。

## 一条不能省的规则：库里只放"已收盘"的日线

`_fetch_hist_df` 盘中会追加一根**实时行**（见 `_maybe_append_realtime_row`）。
若把它落库，之后所有请求都会把 10:00 的价格当成「今日收盘」——这是静默的错误
结论，比取不到更糟。所以落库时会丢弃当期未定稿的当日行；相应地，"够不够用"的
判据要求库内最新日期覆盖到请求的截止日，**不留"差一两天也算够"的容差**：
容差能省下最后一次网络调用，代价是把"少一根 K 线"变成无声的事实。

已知代价（如实记录）：长假期间库内最新日期永远补不到 `end`，于是长假里的请求
仍会补网。我们没有把交易日历引进来判"是否休市"，因为
`trade_calendar._load_cn_trade_dates()` 会调 akshare 拉整张交易日历——那正是
本模块要消灭的"读取路径打外网"。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date as _date
from datetime import datetime, time as _time, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

import pandas as pd

from api.database import DailyBarDB, get_db_ctx
from tradingagents.agent.repository import new_id
from tradingagents.dataflows.trade_calendar import CN_TZ, now_cn

logger = logging.getLogger(__name__)

# 列契约与 `tradingagents/graph/data_collector.py::_OHLCV_COLS` 一致（小写）。
# 本地库若用别的列名，信号层（`signals/context.py::_normalize_daily`）会直接抛
# InsufficientHistory——这正是此前"用大写列喂信号层"那起事故的形态。
_BAR_COLUMNS = ("date", "open", "high", "low", "close", "volume")

# 落库时可以从网络帧里认出来的列（大写/中文都认，因为 provider 的
# `_fetch_hist_df` 输出的是给提示词用的大写表格）
_NUMERIC_ALIASES: Mapping[str, Sequence[str]] = {
    "open": ("open",),
    "high": ("high",),
    "low": ("low",),
    "close": ("close",),
    "volume": ("volume",),
    "amount": ("amount", "成交额"),
    "turnover": ("turnover", "换手率"),
}
_DATE_ALIASES = ("date", "trade_date", "日期")

# A 股代码 → 市场后缀。口径与 `cn_research_data._market_of` 一致（6/5→沪、
# 4/8→北、其余→深），只有 9 开头（B 股）按沪市处理——B 股 900xxx 在沪市。
_MARKET_BY_LEADING = {"5": "SH", "6": "SH", "9": "SH", "4": "BJ", "8": "BJ"}

# 收盘时间：15:00 之后当日日线才定稿。刻意不查交易日历（那会打外网），
# 且非交易日本来就不会产出当日行，这里的判断只用于"要不要落库"。
_MARKET_CLOSE = _time(15, 0)


def canonical_symbol(symbol: str) -> str:
    """把各种写法收敛成 `600519.SH` 这种带市场后缀的规范键。

    为什么这件事值得一个函数：读取侧（对话/信号服务）用 `600519.SH`，采集侧的
    命令行常拿到裸代码 `600519`。两侧键不一致时，本地库**永远查不中**——表现
    不是报错，而是"每次都打外网"，本功能彻底失效且无人察觉。
    """
    text = str(symbol).strip().upper()
    match = re.search(r"(\d{6})", text)
    if not match:
        # 非 A 股代码（指数/美股等）：原样保留，不猜市场
        return text
    code = match.group(1)
    market = _MARKET_BY_LEADING.get(code[0], "SZ")
    return f"{code}.{market}"


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    number = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    return None if pd.isna(number) else float(number)


def _normalize_date(value: Any) -> str | None:
    if value is None:
        return None
    parsed = pd.to_datetime(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(parsed):
        return None
    return parsed.strftime("%Y-%m-%d")


def _row_from_mapping(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """把一行（可能来自大写帧）归一成入库形状；没有日期的行直接丢弃。"""
    lowered = {str(key).strip().lower(): value for key, value in row.items()}
    raw_date = None
    for alias in _DATE_ALIASES:
        if alias in lowered:
            raw_date = lowered[alias]
            break
    day = _normalize_date(raw_date)
    if day is None:
        # 唯一键的一半缺失 → 这行不可能幂等，宁可丢弃也不塞空值
        return None

    out: dict[str, Any] = {"date": day}
    for target, aliases in _NUMERIC_ALIASES.items():
        for alias in aliases:
            if alias in lowered:
                out[target] = _to_float(lowered[alias])
                break
    return out


def _dedupe_by_date(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """同一批里同日期的行只留最后一条（后写的覆盖先写的）。"""
    merged: dict[str, dict[str, Any]] = {}
    for row in rows:
        normalized = _row_from_mapping(row)
        if normalized is not None:
            merged[normalized["date"]] = normalized
    return [merged[day] for day in sorted(merged)]


def upsert_bars(symbol: str, rows: Iterable[Mapping[str, Any]], *, source: str) -> int:
    """幂等写入日线：同一 `(symbol, date)` 覆盖更新，返回写入行数。

    刻意用「先查后写」而不是方言相关的 `ON CONFLICT`：本仓库的库既可能是 SQLite
    也可能是 PostgreSQL，而幂等性由表上的 `(symbol, date)` 唯一约束兜底——即使
    并发下真的发出两条 INSERT，第二条也会被约束挡下，而不是堆出一行重复日线。
    """
    key = canonical_symbol(symbol)
    normalized = _dedupe_by_date(rows)
    if not normalized:
        return 0

    with get_db_ctx() as db:
        days = [row["date"] for row in normalized]
        existing = {
            row.date: row
            for row in db.query(DailyBarDB)
            .filter(DailyBarDB.symbol == key, DailyBarDB.date.in_(days))
            .all()
        }
        for row in normalized:
            current = existing.get(row["date"])
            if current is None:
                db.add(
                    DailyBarDB(
                        id=new_id(),
                        symbol=key,
                        date=row["date"],
                        open=row.get("open"),
                        high=row.get("high"),
                        low=row.get("low"),
                        close=row.get("close"),
                        volume=row.get("volume"),
                        amount=row.get("amount"),
                        turnover=row.get("turnover"),
                        source=source,
                    )
                )
                continue
            # 已存在：覆盖值。复权修正与数据源纠错都要能写进来。
            current.open = row.get("open")
            current.high = row.get("high")
            current.low = row.get("low")
            current.close = row.get("close")
            current.volume = row.get("volume")
            if row.get("amount") is not None:
                current.amount = row.get("amount")
            if row.get("turnover") is not None:
                current.turnover = row.get("turnover")
            current.source = source
            current.ingested_at = datetime.now(timezone.utc)
        db.commit()
    return len(normalized)


def load_bars(symbol: str, *, end: str, lookback: int) -> list[dict[str, Any]]:
    """取 `<= end` 的最近 `lookback` 条，**按日期升序**返回。

    升序是因为消费方（信号层）按时间递增读窗口；在这里排好比让每个调用方各排一次
    更不容易漏。
    """
    key = canonical_symbol(symbol)
    if lookback is not None and lookback <= 0:
        return []

    with get_db_ctx() as db:
        query = db.query(DailyBarDB).filter(DailyBarDB.symbol == key)
        if end:
            query = query.filter(DailyBarDB.date <= end)
        rows = query.order_by(DailyBarDB.date.desc()).limit(int(lookback)).all()

    return [
        {
            "date": row.date,
            "open": row.open,
            "high": row.high,
            "low": row.low,
            "close": row.close,
            "volume": row.volume,
            "amount": row.amount,
            "turnover": row.turnover,
            "source": row.source,
        }
        for row in reversed(rows)
    ]


def latest_date(symbol: str) -> str | None:
    key = canonical_symbol(symbol)
    with get_db_ctx() as db:
        row = (
            db.query(DailyBarDB.date)
            .filter(DailyBarDB.symbol == key)
            .order_by(DailyBarDB.date.desc())
            .first()
        )
    return row[0] if row else None


def count(symbol: str) -> int:
    key = canonical_symbol(symbol)
    with get_db_ctx() as db:
        return int(db.query(DailyBarDB).filter(DailyBarDB.symbol == key).count())


def bars_frame(rows: Iterable[Mapping[str, Any]]) -> pd.DataFrame:
    """把库里的行拼成信号层契约的 DataFrame（小写 OHLCV，升序）。"""
    frame = pd.DataFrame(list(rows))
    if frame.empty:
        return pd.DataFrame(columns=list(_BAR_COLUMNS))
    frame = frame[list(_BAR_COLUMNS)]
    frame["date"] = frame["date"].astype(str).str[:10]
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.sort_values("date").reset_index(drop=True)


def frame_to_rows(frame: pd.DataFrame | None) -> list[dict[str, Any]]:
    """网络帧 → 入库行。刻意容忍大写列：`_fetch_hist_df` 的输出就是大写。"""
    if frame is None or len(frame) == 0:
        return []
    return [dict(record) for record in frame.to_dict(orient="records")]


def upsert_frame(symbol: str, frame: pd.DataFrame | None, *, source: str) -> int:
    """把一帧网络数据顺手入库。"""
    return upsert_bars(symbol, frame_to_rows(frame), source=source)


# ── 先库后网 ──────────────────────────────────────────────────────────────


def _local_now(clock: Callable[[], datetime]) -> datetime:
    """统一成北京时间再比较：时钟若给的是 UTC，"15:00 收盘"会整体错 8 小时。"""
    moment = clock()
    if moment.tzinfo is None:
        return moment.replace(tzinfo=CN_TZ)
    return moment.astimezone(CN_TZ)


def _is_settled(day: str, clock: Callable[[], datetime]) -> bool:
    """这一天（北京时间）的日线是否已定稿。

    15:00 之后才定稿；非交易日本来就不会有当日行，所以这里不必查交易日历
    （查它会打外网，见模块文档）。
    """
    moment = _local_now(clock)
    try:
        parsed = _date.fromisoformat(day)
    except ValueError:
        return False
    if parsed < moment.date():
        return True
    if parsed > moment.date():
        return False
    return moment.time() >= _MARKET_CLOSE


def persist_frame(
    symbol: str,
    frame: pd.DataFrame | None,
    *,
    source: str,
    clock: Callable[[], datetime] = now_cn,
) -> int:
    """把一帧网络数据落库，并丢弃未定稿的当日行。

    丢弃而不是"标记"：盘中那根实时行一旦进库，之后每一次请求都会把它当成今日
    收盘价——静默的错误结论，比取不到更糟。
    """
    rows: list[dict[str, Any]] = []
    for record in frame_to_rows(frame):
        normalized = _row_from_mapping(record)
        if normalized is None:
            continue
        if _is_settled(normalized["date"], clock):
            rows.append(normalized)
    if not rows:
        return 0
    return upsert_bars(symbol, rows, source=source)


def expected_last_settled_date(end: str, clock: Callable[[], datetime]) -> str | None:
    """`end` 之前（含）网络**能给出收盘价**的最近一个交易日（只用周末规则）。

    库内最新日期必须 >= 这个值才算"够用"。刻意不留"差一两天也算够"的容差：
    15:00 收盘后、盘后采集尚未跑的那一刻，库里最新是昨天，而网络已经能给出今天
    的完整日线——容差会让判定少掉今天这一根 K 线，静默地把结论改掉。
    """
    if not end:
        return None
    try:
        requested = _date.fromisoformat(end)
    except ValueError:
        return None
    target = min(requested, _local_now(clock).date())
    while target.weekday() >= 5:  # 周末不产出日线，回退到最近的周五
        target -= timedelta(days=1)
    return target.isoformat()


def load_local_bars_if_enough(
    symbol: str,
    *,
    end: str,
    lookback: int,
    clock: Callable[[], datetime] = now_cn,
) -> list[dict[str, Any]] | None:
    """库内够用就**把行取回来**，否则返回 None。

    返回行而不是布尔值，是为了让调用方一次查询就拿到结果：先判"够不够"再查一次
    "取数据"，中间那一瞬库被锁/被清就会把"够用"变成一次失败——新加的一层绝不能
    把原本能跑通的请求变成失败。
    """
    if int(lookback) <= 0:
        return None  # 窗口无要求时不做本地判断，交给网络口径决定
    expected = expected_last_settled_date(end, clock)
    if expected is None:
        return None
    try:
        rows = load_bars(symbol, end=end, lookback=int(lookback))
    except Exception as exc:  # noqa: BLE001 — 本地库不可用不该让判定失败
        # 表还没建（服务未重启）、库被锁、文件缺失……这些都只该让本层"让路"：
        # 改造前这些请求是能跑通的，新加的一层绝不能把它们变成失败。
        logger.warning("daily bar store: 本地库不可用，改走网络 %s: %s", type(exc).__name__, exc)
        return None
    if len(rows) < int(lookback):
        return None
    if str(rows[-1]["date"]) < expected:
        return None
    return rows


def local_bars_enough(
    symbol: str,
    *,
    end: str,
    lookback: int,
    clock: Callable[[], datetime] = now_cn,
) -> bool:
    """库内数据够不够用：条数够 **且** 新鲜度够（只读判断，供观测与单测使用）。

    只看条数会让"库里 250 条都是三年前的"被当成够用；只看新鲜度会让窗口不足的
    标的（新股、长期停牌）喂给信号层一根瘦窗口。
    """
    return load_local_bars_if_enough(symbol, end=end, lookback=lookback, clock=clock) is not None


def load_or_fetch_daily_bars(
    symbol: str,
    *,
    end: str,
    lookback: int,
    fetch: Callable[..., pd.DataFrame],
    clock: Callable[[], datetime] = now_cn,
    source: str = "provider",
) -> pd.DataFrame:
    """先库后网取日线：库内够用就不打外网，不够才补网并把结果顺手入库。

    `fetch(symbol, end, lookback)` 是网络取数（注入进来，因此本函数可离线单测）。
    补网失败照旧抛出——用一个只有几条的库去凑结果，等于把"取不到"伪装成"数据
    就是这么多"。入库失败则只记日志：采集是顺手做的事，读取才是主路径。
    """
    local = load_local_bars_if_enough(symbol, end=end, lookback=lookback, clock=clock)
    if local is not None:
        return bars_frame(local)

    frame = fetch(symbol, end, lookback)

    try:
        persist_frame(symbol, frame, source=source, clock=clock)
    except Exception as exc:  # noqa: BLE001 — 入库失败不该影响本次读取
        logger.warning("daily bar store: 落库失败 %s: %s: %s", symbol, type(exc).__name__, exc)

    return frame


@dataclass
class LocalFirstDailyBarsPort:
    """日线端口的先库后网实现：包住既有网络端口，对外接口完全一致。

    `inner` 需提供 `bars(symbol, end, lookback) -> DataFrame`（即
    `signal_ports.ProviderDailyBarsPort`）。包住端口而不是 provider，是为了让
    "窗口该取多少自然日"这类口径只留在原来的地方，本层不复制一份。
    """

    inner: Any
    clock: Callable[[], datetime] = now_cn
    source: str = "provider"
    hits: int = field(default=0, init=False)  # 命中本地库的次数（供观测/测试读数）

    def bars(self, symbol: str, end: str, lookback: int) -> pd.DataFrame:
        local = load_local_bars_if_enough(symbol, end=end, lookback=lookback, clock=self.clock)
        if local is not None:
            self.hits += 1
            return bars_frame(local)

        frame = self.inner.bars(symbol, end, lookback)

        try:
            persist_frame(symbol, frame, source=self.source, clock=self.clock)
        except Exception as exc:  # noqa: BLE001 — 入库失败不该影响本次读取
            logger.warning("daily bar store: 落库失败 %s: %s: %s", symbol, type(exc).__name__, exc)

        return frame


def wrap_daily_port(inner: Any) -> LocalFirstDailyBarsPort:
    """装配处用的一行包装（可读性：装配代码不必关心端口内部结构）。"""
    return LocalFirstDailyBarsPort(inner=inner)
