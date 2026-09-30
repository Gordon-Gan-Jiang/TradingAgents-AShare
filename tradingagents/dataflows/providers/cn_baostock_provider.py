"""A 股数据源：BaoStock（官方协议）——平台的**历史地基**。

## 定位：为什么把它当"地基"而不是"备胎"

AKShare 是聚合爬虫：接口多、覆盖广，但每个接口都依赖上游网页/接口的当前形态，
且**按 IP 封禁**（本项目实测东财那条路整片不通）。BaoStock 走官方 TCP 协议、
免费、可批量拉取，不封 IP——所以"历史日线/分钟线/季频财务"这类**可离线、可回补、
需要长期稳定**的维度交给它；实时快照、板块、资金流、龙虎榜、涨停池、筹码、新闻
这些它在结构上就没有的能力，仍归 AKShare 系。

分工裁决写在 `providers/capability_matrix.py`，并由一致性测试强制。

## 这次修复的两个真实故障（务必保留注释）

1. **上游主机名迁移**：baostock 0.8.x 拨 `www.baostock.com:10030`，而上游已迁到
   `public-api.baostock.com:10030`（旧主机 443 通、10030 是黑洞）。旧版本登录会挂死
   **75 秒**后报 `10002007 网络接收错误`。依赖下界因此钉在 `baostock>=0.9.4`。

2. **错误语义被吞掉**：旧实现把登录失败写成
   `NotImplementedError("baostock login failed: ...")`，与"没实现该方法"完全同形。
   于是一次**网络/上游事故**在系统里表现为"这个 provider 没实现该功能"，
   永远不会有人去查。现在连不上抛 `SourceUnavailable`，不支持抛 `SourceUnsupported`
   （见 `providers/errors.py`）。

## 超时

baostock 的 `socketutil` 直接 `sock.connect(...)` 而**不设 timeout**，所以没有超时
就是"挂死 75 秒"。这里在调用期间设置进程级 socket 默认超时并在结束时**恢复原值**
（探测与分析都跑在线程里，污染全局默认超时会波及其它 HTTP 调用），同时用进程内锁
把"改全局超时"的窗口串行化，避免并发时互相踩。
"""

from __future__ import annotations

import io
import re
import socket
import threading
import time
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timedelta
from typing import Any, Iterable, Iterator, Sequence

import pandas as pd
from stockstats import wrap

from .bars import to_signal_bars
from .base import BaseMarketDataProvider
from .errors import SourceEmpty, SourceUnavailable, SourceUnsupported
from ..trade_calendar import cn_no_data_reason

#: "改全局 socket 默认超时"的窗口必须串行——它是进程级状态，不是线程级。
_BAOSTOCK_CALL_LOCK = threading.Lock()

# ── 登录冷却（baostock 版的"熔断"）─────────────────────────────────────────
#
# baostock 走**自己的 socket 协议**，不经过 `_http_utils` 的出口闸门，
# 所以它的"快速失败"必须在这里自己做。
#
# 实测（2026-09-29）登录失败时：`error_code=10002007 网络接收错误。`，
# 每次耗时 **10027~12040ms**；同时 TCP `public-api.baostock.com:10030` 却是通的
# ——"端口通、取不到数"，连接级健康检查会给出假阳性。
#
# 致命之处在于链序：`get_daily_bars_df` 属于 `cn_research_data`，配置链是
# `cn_baostock,cn_akshare`——baostock **排第一**。于是每次调用都要先白等 10 秒
# 才轮到能用的 akshare（实测 11318ms 里约 10000ms 纯浪费）。
#
# 连续失败达阈值即冷却，窗口内直接毫秒级失败并让链继续走下一个源。
# 注意：冷却**不会降低可用性**——登录既然在失败，不冷却也一样拿不到数据，
# 只是要在 10 秒后才承认。唯一代价是"一次瞬时抖动后，本来下一次能好"的场景
# 会多等一个窗口，所以阈值取 2 而不是 1，窗口取 120s 而不是 300s。
_LOGIN_FAIL_TO_COOLDOWN = 2
_LOGIN_COOLDOWN_S = 120.0
_login_down_until: float = 0.0
_login_fail_streak: int = 0
_LOGIN_STATE_LOCK = threading.Lock()


def reset_login_cooldown() -> None:
    """清空登录冷却（测试用，避免用例之间互相连累）。"""
    global _login_down_until, _login_fail_streak
    with _LOGIN_STATE_LOCK:
        _login_down_until = 0.0
        _login_fail_streak = 0


def _login_cooldown_remaining() -> float:
    with _LOGIN_STATE_LOCK:
        return max(0.0, _login_down_until - time.monotonic())


def _note_login_failure() -> None:
    """登录失败：连续到阈值就进入冷却。"""
    global _login_down_until, _login_fail_streak
    with _LOGIN_STATE_LOCK:
        _login_fail_streak += 1
        if _login_fail_streak >= _LOGIN_FAIL_TO_COOLDOWN:
            _login_down_until = time.monotonic() + _LOGIN_COOLDOWN_S


def _note_login_success() -> None:
    global _login_down_until, _login_fail_streak
    with _LOGIN_STATE_LOCK:
        _login_down_until = 0.0
        _login_fail_streak = 0

_HISTORY_FIELDS = "date,open,high,low,close,volume"
_MINUTE_FIELDS = "date,time,open,high,low,close,volume"
#: 指数的字段集与个股**不同**：指数没有 turn/tradestatus/peTTM 这些字段，
#: 混用会让 baostock 直接报错（"证券代码未找到"之类的误导性提示）。
_INDEX_FIELDS = "date,open,high,low,close,volume"

#: 日线用前复权。**不要改**：改 adjustflag 会静默改变价格含义（历史价会整体平移）。
_ADJUST_FORWARD = "2"
#: 分钟线在 baostock 侧不支持复权，只能取 3（不复权）。
_ADJUST_NONE = "3"

#: 季频财务往回找多少个季度（应对"最新季度还没披露"）。
_QUARTER_WALK_BACK = 6


class CnBaoStockProvider(BaseMarketDataProvider):
    """A-share provider backed by BaoStock."""

    #: 单次 baostock 调用的 socket 超时（秒）。默认 10s：正常登录 0.1~0.2s，
    #: 取数 <1s；给到 10s 已足够宽松，同时把"挂死 75 秒"这种事故压到可接受范围。
    SOCKET_TIMEOUT_SECONDS = 10.0

    INDICATOR_DESCRIPTIONS = {
        "close_50_sma": "50 日均线（SMA）：中期趋势指标。",
        "close_200_sma": "200 日均线（SMA）：长期趋势基准。",
        "close_10_ema": "10 日指数均线（EMA）：短期响应更快。",
        "macd": "MACD：趋势与动量综合指标。",
        "macds": "MACD 信号线（Signal）。",
        "macdh": "MACD 柱状图（Histogram）。",
        "rsi": "RSI：衡量超买/超卖的动量指标。",
        "boll": "布林中轨（20 日均线）。",
        "boll_ub": "布林上轨。",
        "boll_lb": "布林下轨。",
        "atr": "ATR：真实波动幅度均值，用于波动与风控。",
        "vwma": "VWMA：成交量加权均线。",
        "mfi": "MFI：资金流量指标。",
    }

    @property
    def name(self) -> str:
        return "cn_baostock"

    # ------------------------------------------------------------------ 基础

    def _bs(self):
        """取 baostock 模块。缺依赖是**环境问题**，不是"不支持该能力"。"""
        try:
            import baostock as bs  # type: ignore
        except ImportError as exc:  # pragma: no cover - 依赖已钉在 pyproject
            raise SourceUnavailable(
                "cn_baostock requires 'baostock'. Install it with: pip install baostock"
            ) from exc
        return bs

    def _normalize_symbol(self, symbol: str) -> str:
        """`600519.SH` / `600519` / `sh.600519` → `sh.600519`。

        非 6 位数字代码（如 `AAPL`）在 baostock 里**结构上**不存在 →
        `SourceUnsupported`，好让链继续问下一个源（yfinance 才有美股）。
        """
        raw = str(symbol).strip().lower()
        m = re.search(r"(\d{6})", raw)
        if not m:
            raise SourceUnsupported(
                f"cn_baostock only supports A-share 6-digit symbols, got: {symbol}"
            )
        code = m.group(1)
        # 显式给了 sh./sz. 前缀就尊重它（指数 sh.000001 与深市个股 000001 同号但不同标的）
        if raw.startswith("sh.") or raw.startswith("sz."):
            return f"{raw[:2]}.{code}"
        if code.startswith(("5", "6", "9")):
            return f"sh.{code}"
        return f"sz.{code}"

    @contextmanager
    def _socket_deadline(self) -> Iterator[None]:
        """在窗口内给 socket 设默认超时，结束后恢复——并串行化这个窗口。"""
        with _BAOSTOCK_CALL_LOCK:
            previous = socket.getdefaulttimeout()
            socket.setdefaulttimeout(self.SOCKET_TIMEOUT_SECONDS)
            try:
                yield
            finally:
                socket.setdefaulttimeout(previous)

    @contextmanager
    def _session(self) -> Iterator[Any]:
        """登录 → 交出 SDK → 登出。任何连接层失败都表达为 `SourceUnavailable`。"""
        bs = self._bs()
        remaining = _login_cooldown_remaining()
        if remaining > 0:
            # 冷却期内**连登录请求都不发**：这就是把"每次白等 10 秒"变成毫秒级
            # 失败、让链立刻走到下一个源的关键。
            raise SourceUnavailable(
                f"baostock login is in cooldown ({int(remaining)}s left) after repeated "
                f"login failures; skipping to the next source"
            )
        with self._socket_deadline():
            try:
                with redirect_stdout(io.StringIO()):
                    lg = bs.login()
            except Exception as exc:  # socket 层：BrokenPipe / timeout / OSError
                _note_login_failure()
                raise SourceUnavailable(
                    f"baostock login failed: {type(exc).__name__}: {exc}"
                ) from exc

            if getattr(lg, "error_code", "1") != "0":
                _note_login_failure()
                raise SourceUnavailable(
                    f"baostock login failed: error_code={getattr(lg, 'error_code', '?')} "
                    f"msg={getattr(lg, 'error_msg', '?')}"
                )
            _note_login_success()
            try:
                yield bs
            finally:
                try:
                    with redirect_stdout(io.StringIO()):
                        bs.logout()
                except Exception:  # 登出失败不该掩盖真正的异常
                    pass

    # ------------------------------------------------------------- 查询与取帧

    @staticmethod
    def _rows_from(result: Any) -> tuple[list[str], list[list[str]]]:
        fields = list(getattr(result, "fields", []) or [])
        rows: list[list[str]] = []
        while result.next():
            rows.append(list(result.get_row_data()))
        return fields, rows

    def _query(self, bs: Any, api: str, *args, **kwargs) -> Any:
        """调一个 baostock 查询接口，把 error_code != 0 统一成 `SourceUnavailable`。"""
        result = getattr(bs, api)(*args, **kwargs)
        code = getattr(result, "error_code", "0")
        if code != "0":
            raise SourceUnavailable(
                f"baostock {api} failed: error_code={code} "
                f"msg={getattr(result, 'error_msg', '?')}"
            )
        return result

    def _fetch_df(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        *,
        frequency: str = "d",
        fields: str = _HISTORY_FIELDS,
    ) -> pd.DataFrame:
        """取 K 线，返回**小写列名**的原始帧（未做提示词格式化）。"""
        code = self._normalize_symbol(symbol)
        adjustflag = _ADJUST_FORWARD if frequency == "d" else _ADJUST_NONE
        with self._session() as bs:
            result = self._query(
                bs,
                "query_history_k_data_plus",
                code,
                fields,
                start_date=start_date,
                end_date=end_date,
                frequency=frequency,
                adjustflag=adjustflag,
            )
            names, rows = self._rows_from(result)
        if not rows:
            return pd.DataFrame(columns=[f.lower() for f in names] or list(fields.split(",")))
        frame = pd.DataFrame(rows, columns=names)
        frame.columns = [str(c).strip().lower() for c in frame.columns]
        return frame

    def _fetch_hist_df(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        """**大写** OHLCV 帧（`Date`/`Open`/…）——给提示词表格用。

        信号层要小写帧，请用 `get_daily_bars_df`。两条路径的消费者不同，
        不要在这里互相迁就（历史上的 `InsufficientHistory` 就是这么来的）。
        """
        df = self._fetch_df(symbol, start_date, end_date)
        if df.empty:
            return pd.DataFrame(columns=["Date", "Open", "High", "Low", "Close", "Volume"])

        rename_map = {
            "date": "Date",
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
        }
        df = df.rename(columns=rename_map)
        for c in ("Open", "High", "Low", "Close", "Volume"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        df = df.dropna(subset=["Date", "Open", "High", "Low", "Close", "Volume"])
        df = df.sort_values("Date").reset_index(drop=True)
        return df

    # ------------------------------------------------------------ 行情（对外）

    def get_stock_data(self, symbol: str, start_date: str, end_date: str) -> str:
        df = self._fetch_hist_df(symbol, start_date, end_date)
        if df.empty:
            return f"No data found for symbol '{symbol}' between {start_date} and {end_date}"
        out = df.copy()
        # 分红/拆股：来源（baostock 日线）**不提供**这两项，所以填 NaN（未知），
        # 不能填 0.0。`0.0` 是一个断言——"这只票从未分红、从未拆股"——而这段
        # 文本会**逐字**进入分析师提示词表格，等于向模型声明了一个我们无权
        # 声明的事实（审计 §4.14）。保留列名是为了不破坏下游对 yfinance 式
        # schema 的假设，值则如实表示"取不到"。
        out["Dividends"] = float("nan")
        out["Stock Splits"] = float("nan")
        out["Date"] = out["Date"].dt.strftime("%Y-%m-%d")
        header = f"# Stock data for {symbol} from {start_date} to {end_date}\n"
        header += f"# Total records: {len(out)}\n"
        header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        return header + out.to_csv(index=False)

    def get_daily_bars_df(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        """结构化日线（**信号层契约**：小写 OHLCV，date 为 YYYY-MM-DD）。

        无数据时抛 `SourceEmpty`（而不是返回空帧）：让链继续问下一个源，
        并且让"今天确实没数据"与"这个源坏了"在日志里可区分。
        """
        frame = self._fetch_hist_df(symbol, start_date, end_date)
        if frame.empty:
            raise SourceEmpty(
                f"cn_baostock returned no daily bars for {symbol} "
                f"between {start_date} and {end_date}"
            )
        return to_signal_bars(frame)

    # ---------------------------------------------------------- 指数（对外）

    def _normalize_index_symbol(self, code: str) -> str:
        """指数代码 → baostock 的 `sh.000300` 形式。

        **指数的代码口径与个股不同，必须单独规范化**：`000300` 作为个股是深市
        的普通股票位，作为指数却是沪深300；`000001` 作为个股是平安银行、
        作为指数是上证指数。所以这里**不能**复用 `_normalize_symbol`
        （它按数字段判市场，会把 `000300` 判成 `sz.000300`，取到一只不相干的股票，
        而且不会报错——静默取错标的比取不到更糟）。

        前缀约定复用平台既有的 `normalize_index_code`（`sh000300` 形式），
        这里只把它转成 baostock 需要的带点形式，避免出现第二套约定。
        """
        from .cn_announcement_index_data import normalize_index_code

        normalized = normalize_index_code(code)
        if not normalized or len(normalized) < 8:
            raise SourceUnsupported(
                f"cn_baostock cannot parse index code: {code!r}"
            )
        # 前缀后面的分隔符形式不固定：`normalize_index_code` 对已带前缀的输入
        # **原样返回**（`sh.000905` 保持带点，`sh000905` 不带点），所以这里统一
        # 先剥掉分隔符再补一个点，避免出现 `sh..000905` 这种非法代码。
        prefix, digits = normalized[:2], normalized[2:].lstrip(".")
        if not digits.isdigit():
            raise SourceUnsupported(f"cn_baostock cannot parse index code: {code!r}")
        return f"{prefix}.{digits}"

    def get_index_daily_bars_df(
        self, code: str, start_date: str, end_date: str
    ) -> pd.DataFrame:
        """指数日线（**信号层契约**：小写 OHLCV，date 为 YYYY-MM-DD）。

        用途是**基准指数**（主线上涨评分要拿沪深300算相对强弱）。这条路此前只有
        AKShare 一家，而东财整片不通——基准一取不到，主线评分就整块失效，
        所以补一个走官方 TCP 的可用源。

        无数据时抛 `SourceEmpty`，理由同 `get_daily_bars_df`。
        """
        normalized = self._normalize_index_symbol(code)
        frame = self._fetch_df(normalized, start_date, end_date, fields=_INDEX_FIELDS)
        if frame.empty:
            raise SourceEmpty(
                f"cn_baostock returned no index bars for {code} "
                f"between {start_date} and {end_date}"
            )
        return to_signal_bars(frame)

    def get_intraday_bars(
        self, symbol: str, day: str, freq: str = "5"
    ) -> pd.DataFrame:
        """历史分钟线。`freq` 取值 5/15/30/60（baostock 只支持这几档）。"""
        freq = str(freq).strip()
        if freq not in {"5", "15", "30", "60"}:
            raise SourceUnsupported(
                f"cn_baostock supports minute frequencies 5/15/30/60, got: {freq}"
            )
        frame = self._fetch_df(
            symbol, day, day, frequency=freq, fields=_MINUTE_FIELDS
        )
        if frame.empty:
            raise SourceEmpty(
                f"cn_baostock returned no {freq}m bars for {symbol} on {day}"
            )
        out = frame.copy()
        if "date" in out.columns:
            out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.strftime("%Y-%m-%d")
        for column in ("open", "high", "low", "close", "volume"):
            if column in out.columns:
                out[column] = pd.to_numeric(out[column], errors="coerce")
        return out.reset_index(drop=True)

    def get_indicators(
        self, symbol: str, indicator: str, curr_date: str, look_back_days: int
    ) -> str:
        if indicator not in self.INDICATOR_DESCRIPTIONS:
            raise ValueError(
                f"Indicator {indicator} is not supported. "
                f"Please choose from: {list(self.INDICATOR_DESCRIPTIONS.keys())}"
            )

        curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
        start_dt = curr_dt - timedelta(days=max(look_back_days, 260))
        df = self._fetch_hist_df(symbol, start_dt.strftime("%Y-%m-%d"), curr_date)
        if df.empty:
            return f"No data found for {symbol} for indicator {indicator}"

        ind_df = df.rename(
            columns={
                "Date": "date",
                "Open": "open",
                "High": "high",
                "Low": "low",
                "Close": "close",
                "Volume": "volume",
            }
        )[["date", "open", "high", "low", "close", "volume"]].copy()

        ss = wrap(ind_df)
        indicator_series = ss[indicator]
        values_by_date = {}
        for idx, dt_val in enumerate(ind_df["date"]):
            date_str = pd.to_datetime(dt_val).strftime("%Y-%m-%d")
            val = indicator_series.iloc[idx]
            values_by_date[date_str] = "N/A" if pd.isna(val) else str(val)

        begin = curr_dt - timedelta(days=look_back_days)
        lines = []
        d = curr_dt
        while d >= begin:
            key = d.strftime("%Y-%m-%d")
            value = values_by_date.get(key)
            if value is None:
                value = cn_no_data_reason(key)
            elif value == "N/A":
                value = cn_no_data_reason(key)
            lines.append(f"{key}: {value}")
            d -= timedelta(days=1)

        return (
            f"## {indicator} 指标值（{begin.strftime('%Y-%m-%d')} 至 {curr_date}）：\n\n"
            + "\n".join(lines)
            + "\n\n"
            + self.INDICATOR_DESCRIPTIONS[indicator]
        )

    # ---------------------------------------------------------------- 财务

    #: 季频财务指标接口 → 中文维度名。
    #: 注意口径：baostock 提供的是**季频财务指标/比率**，不是完整三表。
    #: 因此它在能力矩阵里是财务维度的 tier 2（AKShare 主），避免静默改变
    #: 已有分析口径——两边同名方法的**含义并不相同**。
    _RATIO_APIS: dict[str, tuple[str, str]] = {
        "get_fundamentals": ("query_profit_data", "盈利能力（季频指标）"),
        "get_balance_sheet": ("query_balance_data", "偿债与资本结构（季频指标）"),
        "get_cashflow": ("query_cash_flow_data", "现金流质量（季频指标）"),
        "get_income_statement": ("query_growth_data", "成长性（季频指标）"),
    }

    @staticmethod
    def _quarter_candidates(curr_date: str) -> list[tuple[int, int]]:
        """从 curr_date 所在季度往回列的候选 (year, quarter)。

        先试当前季度是刻意的：披露后最新季报就在当前季度；没披露则自然往回走。
        """
        d = datetime.strptime(curr_date[:10], "%Y-%m-%d")
        year, quarter = d.year, (d.month - 1) // 3 + 1
        out: list[tuple[int, int]] = []
        for _ in range(_QUARTER_WALK_BACK):
            out.append((year, quarter))
            quarter -= 1
            if quarter == 0:
                quarter, year = 4, year - 1
        return out

    def _ratio_table(
        self, bs: Any, api: str, code: str, year: int, quarter: int
    ) -> pd.DataFrame:
        result = self._query(bs, api, code, year=year, quarter=quarter)
        names, rows = self._rows_from(result)
        if not rows:
            return pd.DataFrame()
        frame = pd.DataFrame(rows, columns=names)
        return frame

    def _fundamentals_text(
        self, ticker: str, curr_date: str, *, apis: Sequence[tuple[str, str]]
    ) -> str:
        code = self._normalize_symbol(ticker)
        candidates = self._quarter_candidates(curr_date)
        with self._session() as bs:
            chosen: tuple[int, int] | None = None
            tables: list[tuple[str, pd.DataFrame]] = []
            for year, quarter in candidates:
                frames: list[tuple[str, pd.DataFrame]] = []
                for api, label in apis:
                    frame = self._ratio_table(bs, api, code, year, quarter)
                    if not frame.empty:
                        frames.append((label, frame))
                if frames:
                    chosen = (year, quarter)
                    tables = frames
                    break

        if chosen is None:
            raise SourceEmpty(
                f"cn_baostock returned no fundamentals for {ticker} "
                f"(walked back {_QUARTER_WALK_BACK} quarters from {curr_date})"
            )

        year, quarter = chosen
        parts = [
            f"## Fundamentals for {ticker}（baostock 季频财务指标）",
            f"- 数据源：baostock（`{code}`，period={year}Q{quarter}）",
            "- 口径提示：**baostock 提供的是季频财务指标/比率**（如 roeAvg、currentRatio、"
            "CFOToNP），与 AKShare 的完整三表/同花顺摘要口径不同；"
            "跨源对比同一指标前请先确认口径。",
        ]
        for label, frame in tables:
            parts.append(f"### {label}")
            parts.append(frame.to_markdown(index=False))
        return "\n\n".join(parts)

    def get_fundamentals(self, ticker: str, curr_date: str = None) -> str:
        return self._fundamentals_text(
            ticker,
            curr_date or datetime.now().strftime("%Y-%m-%d"),
            apis=(
                self._RATIO_APIS["get_fundamentals"],
                ("query_dupont_data", "杜邦分析（季频指标）"),
                ("query_operation_data", "运营效率（季频指标）"),
            ),
        )

    def get_balance_sheet(
        self, ticker: str, freq: str = "quarterly", curr_date: str = None
    ) -> str:
        return self._fundamentals_text(
            ticker,
            curr_date or datetime.now().strftime("%Y-%m-%d"),
            apis=(self._RATIO_APIS["get_balance_sheet"],),
        )

    def get_cashflow(
        self, ticker: str, freq: str = "quarterly", curr_date: str = None
    ) -> str:
        return self._fundamentals_text(
            ticker,
            curr_date or datetime.now().strftime("%Y-%m-%d"),
            apis=(self._RATIO_APIS["get_cashflow"],),
        )

    def get_income_statement(
        self, ticker: str, freq: str = "quarterly", curr_date: str = None
    ) -> str:
        return self._fundamentals_text(
            ticker,
            curr_date or datetime.now().strftime("%Y-%m-%d"),
            apis=(self._RATIO_APIS["get_income_statement"],),
        )

    # -------------------------------------------------- 结构上不支持的能力

    def get_realtime_quotes(self, symbols: Iterable[str]) -> str:
        raise SourceUnsupported("cn_baostock does not provide realtime quotes.")

    def get_news(self, ticker: str, start_date: str, end_date: str) -> str:
        raise SourceUnsupported("cn_baostock does not provide news.")

    def get_global_news(
        self, curr_date: str, look_back_days: int = 7, limit: int = 50
    ) -> str:
        raise SourceUnsupported("cn_baostock does not provide global news.")

    def get_insider_transactions(self, symbol: str) -> str:
        raise SourceUnsupported("cn_baostock does not provide insider transactions.")
