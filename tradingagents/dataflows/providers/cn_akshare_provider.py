import re
import time
import threading
import contextvars
from datetime import datetime, timedelta
from typing import Callable, Optional

import numpy as np
import pandas as pd
from stockstats import wrap

from .base import BaseMarketDataProvider
from .errors import SourceEmpty, SourceUnavailable, SourceUnsupported
from ..trade_calendar import cn_market_phase, cn_no_data_reason, cn_today_str, is_cn_trading_day


# ── akshare 并发控制 ──
# 总并发上限 5（防反爬 + akshare 全局状态安全）
# 定时任务最多占 3 个槽位，保证前端至少有 2 个槽位可用
#
# 关键设计：僵尸线程回收
# _run_job 超时后不会 cancel 内部线程（避免 cancel 卡在 to_thread），
# 导致僵尸线程可能永远持有 semaphore permit。_AkshareLock 通过追踪每个
# permit 的持有时间，在超过 STALE_TIMEOUT 后自动回收，防止锁被耗尽。

_is_scheduled_task: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "is_scheduled_task", default=False,
)


def set_scheduled_task_context(value: bool = True) -> contextvars.Token:
    """标记当前上下文为定时任务（会通过 asyncio.to_thread 自动传播到工作线程）"""
    return _is_scheduled_task.set(value)


import logging as _logging

_lock_logger = _logging.getLogger(__name__)
logger = _logging.getLogger(__name__)


class _AkshareLock:
    """akshare 并发锁：前端优先 + 僵尸线程自动回收。

    - 总并发上限 ``total``（防反爬）
    - 定时任务额外受 ``scheduled_max`` 限制，为前端保留带宽
    - 持锁超过 ``stale_timeout`` 秒的线程视为僵尸，permit 被自动回收
    - 僵尸线程最终退出 ``with`` 块时不会 double-release（已被回收）
    """

    ACQUIRE_TIMEOUT = 20   # 等待 slot 的最大秒数（原 60s：等锁过久会占满 FastAPI 线程池 → 前端 fetch 超时）
    STALE_TIMEOUT = 90    # 单次 akshare 调用超过 90s 视为僵尸回收（原 120s：更快释放被网络挂起的锁槽）

    def __init__(self, total: int = 5, scheduled_max: int = 3):
        self._total = threading.Semaphore(total)
        self._scheduled = threading.Semaphore(scheduled_max)
        self._holders: dict[int, tuple[float, bool]] = {}   # tid -> (mono_time, is_scheduled)
        self._mu = threading.Lock()

    # ── 僵尸回收 ──

    def _reclaim_stale(self) -> int:
        """回收超时持有者的 permit，返回回收数量。"""
        now = time.monotonic()
        reclaimed = 0
        with self._mu:
            stale = [
                (tid, is_sched)
                for tid, (t, is_sched) in self._holders.items()
                if now - t > self.STALE_TIMEOUT
            ]
            for tid, is_sched in stale:
                del self._holders[tid]
                self._total.release()
                if is_sched:
                    self._scheduled.release()
                reclaimed += 1
        if reclaimed:
            _lock_logger.warning("[AkshareLock] reclaimed %d stale permits from zombie threads", reclaimed)
        return reclaimed

    # ── context manager ──

    def _acquire_or_reclaim(self, sem: threading.Semaphore, label: str) -> None:
        """尝试获取 semaphore，超时后回收僵尸再重试一次。"""
        if sem.acquire(timeout=self.ACQUIRE_TIMEOUT):
            return
        self._reclaim_stale()
        if sem.acquire(timeout=10):
            return
        raise TimeoutError(f"akshare {label} slot acquire timeout after reclaim")

    def __enter__(self):
        is_scheduled = _is_scheduled_task.get(False)
        try:
            if is_scheduled:
                self._acquire_or_reclaim(self._scheduled, "scheduled")
                try:
                    self._acquire_or_reclaim(self._total, "total")
                except BaseException:
                    self._scheduled.release()
                    raise
            else:
                self._acquire_or_reclaim(self._total, "total")
        except TimeoutError:
            _lock_logger.error("[AkshareLock] acquire timeout (is_scheduled=%s)", is_scheduled)
            raise
        with self._mu:
            self._holders[threading.get_ident()] = (time.monotonic(), is_scheduled)
        return self

    def __exit__(self, *exc_info):
        tid = threading.get_ident()
        with self._mu:
            info = self._holders.pop(tid, None)
        if info is not None:
            _, is_scheduled = info
            self._total.release()
            if is_scheduled:
                self._scheduled.release()
        # info is None → permit 已被 _reclaim_stale 回收，不 double-release


AKSHARE_CALL_LOCK = _AkshareLock(total=5, scheduled_max=3)

#: `ak.stock_news_em` 的**结构性**上限：源码为 `pageSize: 10` 且**没有分页循环**，
#: 即该端点单次最多只会返回最近 10 条。它无法按日期区间回溯翻页。
#:
#: 这个数字必须显式写在这里，因为调用方请求的是 **90 天**窗口
#: （`graph/data_collector.py` 的 `LONG_DAYS`）。源上限 < 请求窗口时，
#: "过滤后为空"**不能**被表述为"窗口内没有新闻"。
_NEWS_SOURCE_ITEM_LIMIT = 10

#: 「窗口覆盖不足」的统一标记。
#:
#: 必须是**可被识别**的文案：`dataflows/vendor_text.py` 的标记表里登记了它，
#: 因此 `_looks_like_vendor_failure` 会把它认成"拿不到"，工具层据此降级、
#: 不产出证据。否则一句"无法判断"会被下游当成有效数据，
#: 甚至被抄成证据锚点（审计 §3.1）。
_NEWS_WINDOW_INSUFFICIENT = "【窗口覆盖不足】"


def _is_transient_network_error(exc: BaseException) -> bool:
    name = type(exc).__name__
    if name in {
        "ConnectionError",
        "Timeout",
        "ReadTimeout",
        "ConnectTimeout",
        "ChunkedEncodingError",
        "RemoteDisconnected",
        "ProtocolError",
    }:
        return True
    msg = str(exc)
    return any(
        token in msg
        for token in (
            "Connection aborted",
            "Remote end closed",
            "timed out",
            "Connection reset",
        )
    )


_LHB_CODE_COLUMNS = ("代码", "SECURITY_CODE", "secu_code", "symbol")


def _is_em_parse_crash(exc: BaseException) -> bool:
    """akshare 是否因上游**返回形态不符**而崩（而不是网络/能力问题）。

    只有这一种错误才值得回退直连去核实窗口：`stock_lhb_detail_em` 的解析体是
    ``data_json["result"]["pages"]``，东财对没有行的窗口返回
    ``HTTP 200 + result: null``，于是 ``None["pages"]`` 抛
    ``TypeError: 'NoneType' object is not subscriptable``——
    **非异动日（完全正常）被上游当成了解析错误**。

    网络异常、缺函数等**不在此列**：它们该如实报错，不该被回退掩盖。
    """
    if isinstance(exc, TypeError) and "not subscriptable" in str(exc):
        return True
    if isinstance(exc, KeyError) and "result" in str(exc):
        return True
    return False


def _recover_lhb_via_direct(
    symbol: str, code: str, day: str, date: str
) -> Optional[str]:
    """akshare 解析崩溃后，用直连端点核实该窗口，返回应给用户的那句话。

    返回 `None` 表示「直连也说不清」——调用方据此保留原始的失败文案（不掩盖故障）。

    * 直连拿到空窗口（东财 `code=9201 / result:null` = 这段窗口没有龙虎榜行）
      ⇒ 中性「无龙虎榜数据（非异动日属正常）」，**不再是**一红色的「数据异常」；
    * 直连拿到行 ⇒ 数据是被上游解析器丢的，还给用户；
    * 直连自己失败 / 未知业务码 ⇒ `None`（如实报失败）。
    """
    try:
        from .cn_eastmoney_direct import fetch_em_lhb_detail_direct

        df = fetch_em_lhb_detail_direct(day, day)
        if df is None or df.empty:
            return f"{symbol} 在 {date} 无龙虎榜数据（非异动日属正常）。"
        hit = _filter_lhb_rows(df, code)
        if hit is None or hit.empty:
            return f"{symbol} 在 {date} 无龙虎榜数据（非异动日属正常）。"
        return f"{symbol} 龙虎榜明细（{date}）：\n{hit.head(20).to_string(index=False)}"
    except Exception as recovery_exc:  # noqa: BLE001 — 回退失败不掩盖原始故障
        logger.warning(
            "lhb direct recovery failed for %s @ %s: %s: %s",
            symbol, date, type(recovery_exc).__name__, recovery_exc,
        )
        return None


def _filter_lhb_rows(df: pd.DataFrame, code: str) -> pd.DataFrame:
    """从全市场龙虎榜明细里挑出本标的的行。

    akshare 返回的是**全市场**当日明细（中文列名，代码列叫「代码」）。调用方必须自己
    过滤：旧实现把整个 frame 的 `head(20)` 直接挂到 "<symbol> 龙虎榜明细" 标题下，
    于是报告里出现的是别的公司的数据。这里也兼容英文列名，防止上游改名后静默漏掉。
    代码统一按 6 位补零比较，避免 "1" 与 "000001" 不相等。
    """
    if df is None or getattr(df, "empty", True):
        return df
    target = str(code).strip().upper().split(".")[0].zfill(6)
    for col in _LHB_CODE_COLUMNS:
        if col in df.columns:
            codes = (
                df[col].astype(str).str.strip().str.upper().str.split(".").str[0].str.zfill(6)
            )
            return df[codes == target]
    # 列名不认识时**不要**退化成返回全表：那正是这个 bug 的形态。
    raise SourceUnsupported(
        f"龙虎榜明细缺少代码列（现有列：{list(df.columns)[:8]}），无法确认归属"
    )


def fetch_industry_board_fund_flow_df(ak_module) -> pd.DataFrame:
    """Fetch industry board fund-flow rankings with akshare API version fallbacks."""
    api_calls: list[tuple[str, Callable[[], pd.DataFrame]]] = []
    legacy = getattr(ak_module, "stock_board_industry_fund_flow_em", None)
    if legacy is not None:
        api_calls.append(("stock_board_industry_fund_flow_em", lambda: legacy(symbol="今日")))
    rank = getattr(ak_module, "stock_sector_fund_flow_rank", None)
    if rank is not None:
        api_calls.append(
            (
                "stock_sector_fund_flow_rank",
                lambda: rank(indicator="今日", sector_type="行业资金流"),
            )
        )
    if not api_calls:
        raise AttributeError(
            "akshare has no supported industry board fund-flow API "
            "(stock_board_industry_fund_flow_em / stock_sector_fund_flow_rank)"
        )

    last_exc: Exception | None = None
    with AKSHARE_CALL_LOCK:
        for _api_name, call in api_calls:
            for attempt in range(3):
                try:
                    return call()
                except Exception as exc:
                    last_exc = exc
                    if attempt < 2 and _is_transient_network_error(exc):
                        time.sleep(0.6 * (2**attempt))
                        continue
                    break
    raise last_exc  # type: ignore[misc]


def fetch_board_fund_flow_df(*, ak_module=None) -> pd.DataFrame:
    """Fetch industry board fund-flow, preferring Sina when Eastmoney is unreachable."""
    try:
        from .cn_sina_moneyflow_provider import fetch_sina_industry_board_fund_flow_df

        return fetch_sina_industry_board_fund_flow_df()
    except Exception:
        pass
    module = ak_module if ak_module is not None else __import__("akshare", fromlist=["ak"]).ak
    return fetch_industry_board_fund_flow_df(module)


def format_board_fund_flow_ranking(df: pd.DataFrame, *, snapshot_date: str | None = None) -> str:
    """Format board fund-flow dataframe with a freshness-parseable anchor date."""
    if df is None or df.empty:
        return "今日板块资金流向数据暂不可用。"
    sort_col = next(
        (c for c in df.columns if "主力" in c and "净" in c and "额" in c and "今日" in c),
        next((c for c in df.columns if "主力" in c and "净" in c and "额" in c), None),
    )
    df_sorted = df.sort_values(sort_col, ascending=False).reset_index(drop=True) if sort_col else df.reset_index(drop=True)
    df_sorted.insert(0, "排名", range(1, len(df_sorted) + 1))
    total = len(df_sorted)
    anchor = snapshot_date or cn_today_str()
    result = df_sorted.head(10).to_string(index=False)
    return f"板块资金流向排名（数据截止 {anchor}，共{total}个板块，前10名）：\n{result}"


# ── 板块数据（市场主线洞察 M1）：涨幅榜 / 历史 / 成分股 / 资金流 ──────────
# 所有 fetch 函数统一做两件事：
#   1. 用 AKSHARE_CALL_LOCK 串行化 + 对瞬时网络错误重试（与既有模式一致）；
#   2. 把 akshare 各版本不稳定的中文列名归一化为稳定的英文列名，
#      供规则层（mainline_scoring.py）与格式化函数消费，屏蔽上游 schema 变化。

_BOARD_SPOT_API = {
    "industry": "stock_board_industry_spot_em",
    "concept": "stock_board_concept_spot_em",
}
_BOARD_HIST_API = {
    "industry": "stock_board_industry_hist_em",
    "concept": "stock_board_concept_hist_em",
}
_BOARD_CONS_API = {
    "industry": "stock_board_industry_cons_em",
    "concept": "stock_board_concept_cons_em",
}
_BOARD_FUND_FLOW_SECTOR_TYPE = {
    "industry": "行业资金流",
    "concept": "概念资金流",
}


def _pick_col(df: pd.DataFrame, candidates: tuple[str, ...]) -> str | None:
    """Return the first existing column among candidates, or None."""
    for c in candidates:
        if c in df.columns:
            return c
    return None


def _normalize_board_spot_df(raw_df: pd.DataFrame) -> pd.DataFrame:
    """Normalize board spot DataFrame to stable columns.

    兼容多源列名：
    - 东方财富 spot_em：板块名称/板块代码/涨跌幅/换手率/上涨家数/下跌家数/领涨股票...
    - 同花顺 summary_ths：板块/涨跌幅/净流入/上涨家数/下跌家数/领涨股/领涨股-涨跌幅
    - 新浪 sector_spot：板块/涨跌幅/平均价格/股票名称(领涨股)/个股-涨跌幅...

    Output columns: name, code, latest, chg_1d, total_mv, turnover,
    up_count, down_count, leader, leader_chg, net_inflow（缺失列填 NaN）。
    """
    if raw_df is None or raw_df.empty:
        return pd.DataFrame()
    mapping = {
        "name": ("板块名称", "行业", "概念", "名称", "板块"),
        "code": ("板块代码", "行业代码", "概念代码", "代码", "label"),
        "latest": ("最新价", "均价", "平均价格"),
        "chg_1d": ("涨跌幅",),
        "total_mv": ("总市值",),
        "turnover": ("换手率",),
        "up_count": ("上涨家数",),
        "down_count": ("下跌家数",),
        "leader": ("领涨股票", "领涨股", "股票名称"),
        "leader_chg": ("领涨股票-涨跌幅", "领涨股-涨跌幅", "个股-涨跌幅"),
        "net_inflow": ("净流入", "今日主力净流入-净额"),
    }
    out = pd.DataFrame(index=range(len(raw_df)))
    for target, candidates in mapping.items():
        col = _pick_col(raw_df, candidates)
        if col is not None:
            out[target] = pd.to_numeric(raw_df[col], errors="coerce") if target not in ("name", "code", "leader") else raw_df[col].astype(str)
        else:
            out[target] = np.nan if target not in ("name", "code", "leader") else ""
    for num_col in ("latest", "chg_1d", "total_mv", "turnover", "up_count", "down_count", "leader_chg", "net_inflow"):
        if num_col in out.columns:
            out[num_col] = pd.to_numeric(out[num_col], errors="coerce")
    return out.reset_index(drop=True)


def _normalize_board_hist_df(raw_df: pd.DataFrame) -> pd.DataFrame:
    """Normalize board history DataFrame (板块历史行情) to stable columns.

    兼容多源列名：
    - 东方财富 hist_em：日期/开盘/收盘/最高/最低/涨跌幅/成交量/成交额/换手率
    - 同花顺 index_ths：日期/开盘价/收盘价/最高价/最低价/涨跌幅/成交量/成交额/换手率

    Output columns: date, open, close, high, low, pct_chg, volume, amount, turnover.
    """
    if raw_df is None or raw_df.empty:
        return pd.DataFrame()
    mapping = {
        "date": ("日期", "date"),
        "open": ("开盘", "开盘价"),
        "close": ("收盘", "收盘价", "close"),
        "high": ("最高", "最高价"),
        "low": ("最低", "最低价"),
        "pct_chg": ("涨跌幅",),
        "volume": ("成交量",),
        "amount": ("成交额",),
        "turnover": ("换手率",),
    }
    out = pd.DataFrame(index=range(len(raw_df)))
    for target, candidates in mapping.items():
        col = _pick_col(raw_df, candidates)
        if col is not None:
            out[target] = raw_df[col]
        else:
            out[target] = np.nan
    for num_col in ("open", "close", "high", "low", "pct_chg", "volume", "amount", "turnover"):
        if num_col in out.columns:
            out[num_col] = pd.to_numeric(out[num_col], errors="coerce")
    return out.reset_index(drop=True)


def _normalize_board_cons_df(raw_df: pd.DataFrame) -> pd.DataFrame:
    """Normalize EM board constituent DataFrame (板块成分股) to stable columns.

    Output columns: code, name, latest, chg_1d, turnover, total_mv, float_mv,
    pe, pb, chg_60d, chg_ytd, amount.
    """
    if raw_df is None or raw_df.empty:
        return pd.DataFrame()
    mapping = {
        "code": ("代码",),
        "name": ("名称",),
        "latest": ("最新价",),
        "chg_1d": ("涨跌幅",),
        "turnover": ("换手率",),
        "total_mv": ("总市值",),
        "float_mv": ("流通市值",),
        "pe": ("市盈率-动态", "市盈率"),
        "pb": ("市净率",),
        "chg_60d": ("60日涨跌幅",),
        "chg_ytd": ("年初至今涨跌幅",),
        "amount": ("成交额",),
    }
    out = pd.DataFrame()
    for target, candidates in mapping.items():
        col = _pick_col(raw_df, candidates)
        if col is not None:
            out[target] = raw_df[col]
    for num_col in ("latest", "chg_1d", "turnover", "total_mv", "float_mv", "pe", "pb", "chg_60d", "chg_ytd", "amount"):
        if num_col in out.columns:
            out[num_col] = pd.to_numeric(out[num_col], errors="coerce")
    return out.reset_index(drop=True)


def _normalize_fund_flow_rank_df(raw_df: pd.DataFrame) -> pd.DataFrame:
    """Normalize EM sector fund-flow rank DataFrame (行业/概念资金流排行) to stable columns.

    Output columns: name, price_idx, chg_1d, inflow, outflow, net_inflow,
    company_count, leader, leader_chg.
    """
    if raw_df is None or raw_df.empty:
        return pd.DataFrame()
    mapping = {
        "name": ("行业", "概念", "名称"),
        "price_idx": ("行业指数", "概念指数"),
        "chg_1d": ("行业-涨跌幅", "概念-涨跌幅", "涨跌幅"),
        "inflow": ("流入资金", "主力净流入-流入"),
        "outflow": ("流出资金", "主力净流入-流出"),
        "net_inflow": ("净额", "主力净流入-净额"),
        "company_count": ("公司家数",),
        "leader": ("领涨股", "领涨股票"),
        "leader_chg": ("领涨股-涨跌幅", "领涨股票-涨跌幅"),
    }
    out = pd.DataFrame()
    for target, candidates in mapping.items():
        col = _pick_col(raw_df, candidates)
        if col is not None:
            out[target] = raw_df[col]
    for num_col in ("price_idx", "chg_1d", "inflow", "outflow", "net_inflow", "company_count", "leader_chg"):
        if num_col in out.columns:
            out[num_col] = pd.to_numeric(out[num_col], errors="coerce")
    return out.reset_index(drop=True)


def _call_akshare_retry(func: Callable[[], pd.DataFrame], *, api_name: str) -> pd.DataFrame:
    """Call an akshare function under the global lock with transient-error retry."""
    last_exc: Exception | None = None
    with AKSHARE_CALL_LOCK:
        for attempt in range(3):
            try:
                return func()
            except Exception as exc:
                last_exc = exc
                if attempt < 2 and _is_transient_network_error(exc):
                    time.sleep(0.6 * (2**attempt))
                    continue
                raise
    raise last_exc  # type: ignore[misc]


def fetch_board_spot_df(ak_module, sector_type: str = "industry") -> pd.DataFrame:
    """Fetch EM board spot rankings (industry|concept), normalized to stable columns."""
    api_name = _BOARD_SPOT_API.get(sector_type)
    if api_name is None:
        raise ValueError(f"Unknown sector_type={sector_type!r}, expected 'industry' or 'concept'")
    func = getattr(ak_module, api_name, None)
    if func is None:
        raise SourceUnsupported(f"akshare has no supported API: {api_name}")
    raw = _call_akshare_retry(func, api_name=api_name)
    return _normalize_board_spot_df(raw)


def fetch_board_hist_df(
    ak_module,
    board_name: str,
    sector_type: str = "industry",
    *,
    start_date: str | None = None,
    end_date: str | None = None,
) -> pd.DataFrame:
    """Fetch board history (板块历史行情) by board NAME, normalized.

    akshare 的板块历史接口以板块名（symbol）而非代码寻址；board_name 如 "小金属"。
    """
    api_name = _BOARD_HIST_API.get(sector_type)
    if api_name is None:
        raise ValueError(f"Unknown sector_type={sector_type!r}")
    func = getattr(ak_module, api_name, None)
    if func is None:
        raise SourceUnsupported(f"akshare has no supported API: {api_name}")
    end = (end_date or cn_today_str()).replace("-", "")
    if start_date:
        start = start_date.replace("-", "")
    else:
        # 默认以 end 为锚向前推 180 个自然日（约 120+ 个交易日，覆盖 60 日历史需求）
        end_dt = datetime.strptime(end, "%Y%m%d")
        start = (end_dt - timedelta(days=180)).strftime("%Y%m%d")

    def _call():
        try:
            return func(symbol=board_name, start_date=start, end_date=end, period="日k", adjust="")
        except TypeError:
            # 旧版签名：位置参数 (symbol, start_date, end_date)
            return func(board_name, start, end)

    raw = _call_akshare_retry(_call, api_name=api_name)
    return _normalize_board_hist_df(raw)


def fetch_board_cons_df(ak_module, board_name: str, sector_type: str = "industry") -> pd.DataFrame:
    """Fetch board constituents (板块成分股) by board NAME, normalized."""
    api_name = _BOARD_CONS_API.get(sector_type)
    if api_name is None:
        raise ValueError(f"Unknown sector_type={sector_type!r}")
    func = getattr(ak_module, api_name, None)
    if func is None:
        raise SourceUnsupported(f"akshare has no supported API: {api_name}")

    def _call():
        try:
            return func(symbol=board_name)
        except TypeError:
            return func(board_name)

    raw = _call_akshare_retry(_call, api_name=api_name)
    return _normalize_board_cons_df(raw)


def fetch_sector_fund_flow_rank_df(
    ak_module, sector_type: str = "industry", period: str = "1d"
) -> pd.DataFrame:
    """Fetch EM sector fund-flow rank (行业|概念, 今日|5日), normalized.

    period: '1d' | '3d' | '5d' | '10d'（akshare indicator 参数：今日/3日/5日/10日）。
    """
    api_name = "stock_sector_fund_flow_rank"
    func = getattr(ak_module, api_name, None)
    if func is None:
        raise SourceUnsupported("akshare has no supported API: stock_sector_fund_flow_rank")
    indicator = {"1d": "今日", "3d": "3日", "5d": "5日", "10d": "10日"}.get(period, "今日")
    sector_label = _BOARD_FUND_FLOW_SECTOR_TYPE.get(sector_type)
    if sector_label is None:
        raise ValueError(f"Unknown sector_type={sector_type!r}")

    def _call():
        return func(indicator=indicator, sector_type=sector_label)

    raw = _call_akshare_retry(_call, api_name=api_name)
    return _normalize_fund_flow_rank_df(raw)


# ── 多源兜底（数据稳定性）：东财 push2 不稳时切同花顺/新浪 ──────────


def fetch_ths_industry_summary_df(ak_module) -> pd.DataFrame:
    """Fetch THS industry board summary (同花顺行业板块汇总，90 个板块)，归一化。

    列：板块/涨跌幅/净流入/上涨家数/下跌家数/领涨股/领涨股-涨跌幅 —— 与东财 spot 兼容。
    """
    api_name = "stock_board_industry_summary_ths"
    func = getattr(ak_module, api_name, None)
    if func is None:
        raise SourceUnsupported(f"akshare has no supported API: {api_name}")
    raw = _call_akshare_retry(func, api_name=api_name)
    return _normalize_board_spot_df(raw)


def fetch_sina_sector_spot_df(ak_module) -> pd.DataFrame:
    """Fetch Sina industry sector spot (新浪行业板块，约 49 个板块)，归一化。"""
    api_name = "stock_sector_spot"
    func = getattr(ak_module, api_name, None)
    if func is None:
        raise SourceUnsupported(f"akshare has no supported API: {api_name}")

    def _call():
        return func(indicator="新浪行业")

    raw = _call_akshare_retry(_call, api_name=api_name)
    return _normalize_board_spot_df(raw)


def fetch_board_spot_df_chain(ak_module, sector_type: str = "industry") -> tuple[pd.DataFrame, str]:
    """多源板块涨幅榜链：东财(直连 push2delay 优先 → akshare) → 同花顺(行业) → 新浪(行业)。

    返回 (归一化 df, source)。source ∈ {"em", "ths", "sina"}。
    - industry：em → ths_summary → sina_sector
    - concept：em（直连 push2delay 可用；同花顺/新浪无全量概念涨幅榜，失败抛错由调用方降级）
    注：东财 push2 域名在本网络被重置，akshare spot_em 常失败；直连 push2delay 0.2s 可靠。
    """
    if sector_type not in ("industry", "concept"):
        raise ValueError(f"Unknown sector_type={sector_type!r}")
    # 1) 东财直连（push2delay）
    try:
        from .cn_eastmoney_direct import fetch_em_board_spot_direct

        df = fetch_em_board_spot_direct(sector_type)
        if df is not None and not df.empty:
            return df, "em"
    except Exception:
        pass
    # 2) akshare 东财（push2，可能被网络重置）
    try:
        return fetch_board_spot_df(ak_module, sector_type), "em"
    except Exception:
        if sector_type != "industry":
            raise
    # 3) 同花顺行业汇总
    try:
        return fetch_ths_industry_summary_df(ak_module), "ths"
    except Exception:
        pass
    # 4) 新浪行业
    try:
        return fetch_sina_sector_spot_df(ak_module), "sina"
    except Exception as exc:
        raise RuntimeError("所有板块涨幅榜源均不可用（em/ths/sina）") from exc


def fetch_ths_board_index_df(
    ak_module, board_name: str, sector_type: str = "industry"
) -> pd.DataFrame:
    """Fetch THS board index daily history (同花顺板块指数日K)，归一化。

    用 THS 板块名（与 summary_ths 的板块名一致）取指数日K，用于计算 5/20 日涨幅。
    """
    api_name = {
        "industry": "stock_board_industry_index_ths",
        "concept": "stock_board_concept_index_ths",
    }.get(sector_type)
    if api_name is None:
        raise ValueError(f"Unknown sector_type={sector_type!r}")
    func = getattr(ak_module, api_name, None)
    if func is None:
        raise SourceUnsupported(f"akshare has no supported API: {api_name}")

    def _call():
        try:
            return func(symbol=board_name)
        except TypeError:
            return func(board_name)

    raw = _call_akshare_retry(_call, api_name=api_name)
    return _normalize_board_hist_df(raw)


def fetch_market_breadth_df(ak_module) -> dict:
    """Fetch market breadth (涨跌家数) via Sina full-market snapshot.

    东财全市场快照在本网络不稳定，改用新浪全市场（约 5500 行，单次约 20s，调用方需 TTL 缓存）。
    返回 {"up": n, "down": n, "flat": n, "total": n, "as_of": iso}。
    """
    api_name = "stock_zh_a_spot"
    func = getattr(ak_module, api_name, None)
    if func is None:
        raise SourceUnsupported(f"akshare has no supported API: {api_name}")
    raw = _call_akshare_retry(func, api_name=api_name)
    if raw is None or raw.empty:
        return {"up": 0, "down": 0, "flat": 0, "total": 0, "as_of": None}
    chg_col = next((c for c in ("涨跌幅", "changepercent") if c in raw.columns), None)
    if chg_col is None:
        return {"up": 0, "down": 0, "flat": 0, "total": len(raw), "as_of": None}
    chg = pd.to_numeric(raw[chg_col], errors="coerce").dropna()
    ts_col = next((c for c in ("时间戳", "timestamp") if c in raw.columns), None)
    as_of = str(raw[ts_col].iloc[-1]) if ts_col is not None and len(raw) else None
    return {
        "up": int((chg > 0).sum()),
        "down": int((chg < 0).sum()),
        "flat": int((chg == 0).sum()),
        "total": int(len(chg)),
        "as_of": as_of,
    }


def fetch_zt_industry_heat_df(ak_module, date: str) -> pd.DataFrame:
    """Fetch limit-up pool aggregated by industry (涨停池按所属行业聚合)。

    东财涨停池可用时，按"所属行业"聚合涨停家数/最高连板/连板>=3 家数，
    作为短线题材热度的真实代理（东财概念涨幅榜不可用时的降级信号）。
    Output columns: industry, zt_count, max_lianban, lianban_ge3, leaders.
    """
    api_name = "stock_zt_pool_em"
    func = getattr(ak_module, api_name, None)
    if func is None:
        raise SourceUnsupported(f"akshare has no supported API: {api_name}")
    raw = _call_akshare_retry(
        lambda: func(date=date.replace("-", "")), api_name=api_name
    )
    if raw is None or raw.empty:
        return pd.DataFrame()
    ind_col = next((c for c in ("所属行业", "行业") if c in raw.columns), None)
    lb_col = next((c for c in ("连板数", "连板") if c in raw.columns), None)
    if ind_col is None:
        return pd.DataFrame()
    df = raw.copy()
    df["_industry"] = df[ind_col].astype(str).str.strip()
    df["_lianban"] = pd.to_numeric(df[lb_col], errors="coerce") if lb_col else 0
    agg = (
        df.groupby("_industry")
        .agg(
            zt_count=("_lianban", "size"),
            max_lianban=("_lianban", "max"),
            lianban_ge3=("_lianban", lambda s: int((s >= 3).sum())),
            leaders=("名称", lambda s: "、".join(s.head(3))),
        )
        .reset_index()
        .rename(columns={"_industry": "industry"})
    )
    agg["max_lianban"] = agg["max_lianban"].fillna(0).astype(int)
    return agg.sort_values(["zt_count", "max_lianban"], ascending=False).reset_index(drop=True)


def fetch_zt_pool_rows_df(ak_module, date: str) -> pd.DataFrame:
    """Fetch limit-up pool row-level data (涨停池明细行)，归一化。

    东财涨停池（稳定可用）按代码/名称/连板数/所属行业/封板资金/炸板次数等输出，
    作为主线选股的候选池（东财板块成分股不可用时的降级/补充）。
    Output columns: code, name, chg_1d, latest, turnover, amount, seal_amount,
    lianban, first_seal, last_seal, zha_ban, industry, zt_status.
    """
    api_name = "stock_zt_pool_em"
    func = getattr(ak_module, api_name, None)
    if func is None:
        raise SourceUnsupported(f"akshare has no supported API: {api_name}")
    raw = _call_akshare_retry(
        lambda: func(date=date.replace("-", "")), api_name=api_name
    )
    if raw is None or raw.empty:
        return pd.DataFrame()
    mapping = {
        "code": ("代码",),
        "name": ("名称",),
        "chg_1d": ("涨跌幅",),
        "latest": ("最新价",),
        "turnover": ("换手率",),
        "amount": ("成交额",),
        "seal_amount": ("封板资金",),
        "lianban": ("连板数",),
        "first_seal": ("首次封板时间",),
        "last_seal": ("最后封板时间",),
        "zha_ban": ("炸板次数",),
        "industry": ("所属行业",),
        "zt_status": ("涨停统计",),
    }
    out = pd.DataFrame(index=range(len(raw)))
    for target, candidates in mapping.items():
        col = _pick_col(raw, candidates)
        if col is not None:
            out[target] = raw[col]
        else:
            out[target] = "" if target in ("code", "name", "first_seal", "last_seal", "industry", "zt_status") else np.nan
    for num_col in ("chg_1d", "latest", "turnover", "amount", "seal_amount", "lianban", "zha_ban"):
        if num_col in out.columns:
            out[num_col] = pd.to_numeric(out[num_col], errors="coerce")
    out["code"] = out["code"].astype(str).str.strip()
    out["name"] = out["name"].astype(str).str.strip()
    out["industry"] = out["industry"].astype(str).str.strip()
    return out.reset_index(drop=True)


def _normalize_sina_flow_df(raw_df: pd.DataFrame) -> pd.DataFrame:
    """Normalize Sina industry fund-flow df to the fund-flow-rank schema.

    Sina columns: 名称/今日涨跌幅/今日主力净流入-净额/今日主力净流入-净占比
    Output columns: name, chg_1d, net_inflow（net_inflow 单位=亿元）。
    """
    if raw_df is None or raw_df.empty:
        return pd.DataFrame()
    out = pd.DataFrame(index=range(len(raw_df)))
    name_col = _pick_col(raw_df, ("名称",))
    chg_col = _pick_col(raw_df, ("今日涨跌幅",))
    net_col = _pick_col(raw_df, ("今日主力净流入-净额",))
    out["name"] = raw_df[name_col].astype(str) if name_col else ""
    out["chg_1d"] = pd.to_numeric(raw_df[chg_col], errors="coerce") if chg_col else np.nan
    out["net_inflow"] = pd.to_numeric(raw_df[net_col], errors="coerce") if net_col else np.nan
    return out.reset_index(drop=True)


def format_board_spot_ranking(
    df: pd.DataFrame, *, sector_type: str = "industry", top_n: int = 20, snapshot_date: str | None = None
) -> str:
    """Format board spot ranking (涨幅榜) as a markdown-friendly table."""
    label = "行业" if sector_type == "industry" else "概念"
    if df is None or df.empty:
        return f"{label}板块涨幅榜数据暂不可用。"
    cols = [
        c for c in ("name", "chg_1d", "turnover", "up_count", "down_count", "leader", "leader_chg")
        if c in df.columns
    ]
    sub = df.sort_values("chg_1d", ascending=False).head(top_n)[cols].reset_index(drop=True)
    sub.insert(0, "排名", range(1, len(sub) + 1))
    anchor = snapshot_date or cn_today_str()
    return f"{label}板块涨幅榜 Top{top_n}（数据截止 {anchor}，共{len(df)}个板块）：\n{sub.to_string(index=False)}"


def format_board_rank_ranking(
    df: pd.DataFrame, *, sector_type: str = "industry", period: str = "1d", top_n: int = 20
) -> str:
    """Format sector fund-flow rank (净流入排行) as a markdown-friendly table."""
    label = "行业" if sector_type == "industry" else "概念"
    period_label = {"1d": "今日", "3d": "3日", "5d": "5日", "10d": "10日"}.get(period, "今日")
    if df is None or df.empty:
        return f"{label}板块{period_label}资金流排行数据暂不可用。"
    if "net_inflow" in df.columns:
        df = df.sort_values("net_inflow", ascending=False).reset_index(drop=True)
    cols = [
        c for c in ("name", "chg_1d", "net_inflow", "inflow", "outflow", "leader")
        if c in df.columns
    ]
    sub = df.head(top_n)[cols].reset_index(drop=True)
    sub.insert(0, "排名", range(1, len(sub) + 1))
    return f"{label}板块{period_label}资金净流入 Top{top_n}：\n{sub.to_string(index=False)}"


def format_board_hist_table(
    df: pd.DataFrame, board_name: str, *, sector_type: str = "industry", recent_days: int = 60
) -> str:
    """Format board history (近 N 日涨跌幅) as a markdown-friendly table."""
    label = "行业" if sector_type == "industry" else "概念"
    if df is None or df.empty:
        return f"{label}板块「{board_name}」历史行情数据暂不可用。"
    cols = [c for c in ("date", "close", "pct_chg", "amount", "turnover") if c in df.columns]
    sub = df.tail(recent_days)[cols].reset_index(drop=True)
    return f"{label}板块「{board_name}」近{len(sub)}日行情：\n{sub.to_string(index=False)}"


def format_board_cons_table(
    df: pd.DataFrame, board_name: str, *, sector_type: str = "industry", top_n: int = 30
) -> str:
    """Format board constituents (成分股) as a markdown-friendly table."""
    label = "行业" if sector_type == "industry" else "概念"
    if df is None or df.empty:
        return f"{label}板块「{board_name}」成分股数据暂不可用。"
    cols = [
        c for c in ("code", "name", "chg_1d", "turnover", "total_mv", "pe", "pb")
        if c in df.columns
    ]
    sub = df.sort_values("chg_1d", ascending=False).head(top_n)[cols].reset_index(drop=True)
    sub.insert(0, "排名", range(1, len(sub) + 1))
    return f"{label}板块「{board_name}」成分股 Top{top_n}（按当日涨幅）：\n{sub.to_string(index=False)}"


# 结构化日线的列契约已抽到 `providers/bars.py`：现在 AKShare 与 BaoStock 都要产出
# 同一种帧，契约留在一处才不会各自漂移。这里 re-export 旧名，既有的
# `from ...cn_akshare_provider import to_signal_bars` 与 `_SIGNAL_BAR_COLUMNS`
# 导入路径保持不变（见 tests/test_daily_bars_contract.py）。
from .bars import SIGNAL_BAR_COLUMNS as _SIGNAL_BAR_COLUMNS  # noqa: E402
from .bars import to_signal_bars  # noqa: E402,F401


class CnAkshareProvider(BaseMarketDataProvider):
    """A-share provider backed by AkShare."""

    INDICATOR_DESCRIPTIONS = {
        "close_50_sma": (
            "50 日均线（SMA）：中期趋势指标。"
            "用途：识别趋势方向，并作为动态支撑/阻力参考。"
        ),
        "close_200_sma": (
            "200 日均线（SMA）：长期趋势基准。"
            "用途：确认大级别趋势，并辅助识别金叉/死叉结构。"
        ),
        "close_10_ema": (
            "10 日指数均线（EMA）：短期响应更快。"
            "用途：捕捉短线动量变化与潜在入场时机。"
        ),
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
        return "cn_akshare"

    def _ak(self):
        try:
            import akshare as ak  # type: ignore
        except ImportError as exc:
            raise SourceUnavailable(
                "cn_akshare requires 'akshare'. Install it with: pip install akshare"
            ) from exc
        return ak

    def _locked(self, func, *args, **kwargs):
        with AKSHARE_CALL_LOCK:
            return func(*args, **kwargs)

    def _normalize_symbol(self, symbol: str) -> str:
        s = symbol.strip().lower()
        m = re.search(r"(\d{6})", s)
        if not m:
            raise SourceUnsupported(
                f"cn_akshare only supports A-share 6-digit symbols, got: {symbol}"
            )
        return m.group(1)

    def _sina_symbol(self, symbol: str) -> str:
        code = self._normalize_symbol(symbol)
        if code.startswith(("5", "6", "9")):
            return f"sh{code}"
        return f"sz{code}"

    def _xq_symbol(self, symbol: str) -> str:
        code = self._normalize_symbol(symbol)
        if code.startswith(("5", "6", "9")):
            return f"SH{code}"
        return f"SZ{code}"

    def _is_likely_etf_symbol(self, symbol: str) -> bool:
        code = self._normalize_symbol(symbol)
        # 常见 A 股 ETF 代码段：5xxxxx(沪市) / 15xxxx,16xxxx,18xxxx(深市)
        return code.startswith(("5", "15", "16", "18"))

    def _normalize_hist_df(self, raw_df: pd.DataFrame) -> pd.DataFrame:
        if raw_df is None or raw_df.empty:
            return pd.DataFrame()

        col_map = {
            "日期": "Date",
            "date": "Date",
            "Date": "Date",
            "开盘": "Open",
            "open": "Open",
            "Open": "Open",
            "最高": "High",
            "high": "High",
            "High": "High",
            "最低": "Low",
            "low": "Low",
            "Low": "Low",
            "收盘": "Close",
            "close": "Close",
            "Close": "Close",
            "成交量": "Volume",
            "volume": "Volume",
            "Volume": "Volume",
            # 刻意**不把** amount / 成交额 映射到 Volume：
            # 新浪 stock_zh_a_daily 与腾讯 stock_zh_a_hist_tx 会**同时**返回 volume 与 amount，
            # 两者都映射到 "Volume" 后 rename 产生**重名列**，于是 out["Volume"] 是 DataFrame
            # 而不是 Series，pd.to_numeric 抛
            #   TypeError: arg must be a list, tuple, 1-d array, or Series
            # 该异常又被下面的兜底分支吞掉，最终报成"eastmoney/sina/tencent all failed"——
            # 实测新浪与腾讯各返回 19 行**真实数据**却全被丢弃，这条兜底链从来没生效过。
            # 另外 amount(成交额,元) 与 volume(成交量,手) 语义本就不同，不该互相冒充。
        }
        df = raw_df.rename(columns=col_map).copy()

        # 撞列防护：重名列会让 df[col] 退化成 DataFrame（即上面那个事故的机制）。
        # 只保留第一列并**显式记一条 warning**，绝不把 DataFrame 当 Series 往下传。
        if df.columns.duplicated().any():
            dupes = sorted({str(c) for c in df.columns[df.columns.duplicated()]})
            _lock_logger.warning(
                "hist dataframe 出现重名列（rename 后），保留第一列：%s", dupes
            )
            df = df.loc[:, ~df.columns.duplicated()]

        required = ["Date", "Open", "High", "Low", "Close", "Volume"]
        missing = [c for c in required if c not in df.columns]
        if "Volume" in missing:
            # 少数端点只给成交额、不给成交量：退而用它，也不要让整条链判失败。
            # 语义不同（元 vs 手），但"有量无额"总好过"没有数据"。
            for alt in ("amount", "Amount", "成交额", "turnover", "换手率"):
                if alt in df.columns:
                    df["Volume"] = df[alt]
                    missing = [c for c in missing if c != "Volume"]
                    break
        if missing:
            raise ValueError(f"hist dataframe missing columns: {missing}")

        out = df[required].copy()
        out["Date"] = pd.to_datetime(out["Date"], errors="coerce")
        out = out.dropna(subset=["Date"]).sort_values("Date")

        for c in ["Open", "High", "Low", "Close", "Volume"]:
            out[c] = pd.to_numeric(out[c], errors="coerce")
        out = out.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
        out["Volume"] = out["Volume"].astype(float)

        return out

    def _format_ak_hist(self, df: pd.DataFrame, symbol: str, start: str, end: str) -> str:
        if df is None or df.empty:
            return f"No data found for symbol '{symbol}' between {start} and {end}"
        out = self._normalize_hist_df(df)
        # 同 cn_baostock_provider：日线源不提供分红/拆股，填 NaN（未知）而不是
        # 0.0（那等于断言"从未分红、从未拆股"，审计 §4.14）。cn_akshare 是
        # A 股价格的 tier 1 主路径，这个伪事实会直接进分析师提示词。
        out["Dividends"] = float("nan")
        out["Stock Splits"] = float("nan")
        out["Date"] = pd.to_datetime(out["Date"]).dt.strftime("%Y-%m-%d")

        header = f"# Stock data for {symbol} from {start} to {end}\n"
        header += f"# Total records: {len(out)}\n"
        header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        return header + out.to_csv(index=False)

    @staticmethod
    def _slice_hist_df(df: pd.DataFrame, start_date: str, end_date: str) -> pd.DataFrame:
        if df is None or df.empty:
            return pd.DataFrame()
        start_dt = pd.to_datetime(start_date, errors="coerce")
        end_dt = pd.to_datetime(end_date, errors="coerce")
        if pd.isna(start_dt) or pd.isna(end_dt):
            return df
        out = df.copy()
        out["Date"] = pd.to_datetime(out["Date"], errors="coerce")
        out = out.dropna(subset=["Date"])
        out = out[(out["Date"] >= start_dt) & (out["Date"] <= end_dt)]
        return out.sort_values("Date").reset_index(drop=True)

    @staticmethod
    def _shrink_table(df: pd.DataFrame, max_rows: int = 12, max_cols: int = 16) -> pd.DataFrame:
        if df is None or df.empty:
            return df
        rows = min(max_rows, len(df))
        cols = min(max_cols, len(df.columns))
        return df.head(rows).iloc[:, :cols]

    @staticmethod
    def _latest_disclosure_period_line(df: pd.DataFrame) -> str:
        from tradingagents.dataflows.freshness.parser import yyyymmdd_to_disclosure_period

        if df is None or df.empty:
            return ""
        if "报告日" in df.columns:
            period = yyyymmdd_to_disclosure_period(str(df["报告日"].iloc[0]))
            if period:
                return f"最新披露期: {period}\n\n"
        yyyymmdd_cols = [str(c) for c in df.columns if re.fullmatch(r"20\d{6}", str(c))]
        if yyyymmdd_cols:
            period = yyyymmdd_to_disclosure_period(max(yyyymmdd_cols))
            if period:
                return f"最新披露期: {period}\n\n"
        return ""

    def _fetch_hist_df(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        with AKSHARE_CALL_LOCK:
            ak = self._ak()
            code = self._normalize_symbol(symbol)
            symbol_with_market = self._sina_symbol(symbol)
            start_yyyymmdd = start_date.replace("-", "")
            end_yyyymmdd = end_date.replace("-", "")

            # ETF 优先：Sina 历史接口稳定且不依赖东财
            if self._is_likely_etf_symbol(symbol):
                etf_errors = []
                try:
                    df = ak.fund_etf_hist_sina(symbol=symbol_with_market)
                    out = self._normalize_hist_df(df)
                    out = self._slice_hist_df(out, start_date, end_date)
                    if not out.empty:
                        return self._maybe_append_realtime_row(symbol, out, end_date, assume_locked=True)
                    etf_errors.append("fund_etf_hist_sina: empty after date filter")
                except Exception as exc:
                    etf_errors.append(f"fund_etf_hist_sina: {type(exc).__name__}")

                try:
                    df = ak.fund_etf_hist_em(
                        symbol=code,
                        period="daily",
                        start_date=start_yyyymmdd,
                        end_date=end_yyyymmdd,
                        adjust="qfq",
                    )
                    out = self._normalize_hist_df(df)
                    if not out.empty:
                        return self._maybe_append_realtime_row(symbol, out, end_date, assume_locked=True)
                    etf_errors.append("fund_etf_hist_em: empty dataframe")
                except Exception as exc:
                    etf_errors.append(f"fund_etf_hist_em: {type(exc).__name__}")

            # Source 1: Eastmoney (default)
            em_last_exc = None
            for i in range(2):
                try:
                    df = ak.stock_zh_a_hist(
                        symbol=code,
                        period="daily",
                        start_date=start_yyyymmdd,
                        end_date=end_yyyymmdd,
                        adjust="qfq",
                    )
                    out = self._normalize_hist_df(df)
                    return self._maybe_append_realtime_row(symbol, out, end_date, assume_locked=True)
                except Exception as exc:
                    em_last_exc = exc
                    if i < 1:
                        time.sleep(0.6 * (i + 1))

            # Source 2: Sina
            # 兜底分支**不再静默**：原来这里是 `except Exception: pass`，于是新浪/腾讯
            # 到底为什么失败完全丢失，最终只报一句"eastmoney/sina/tencent all failed"，
            # 让人无从判断是源坏了、被限流了，还是我们自己的解析炸了（实测是后者）。
            fallback_errors: list[str] = []
            try:
                df = ak.stock_zh_a_daily(
                    symbol=symbol_with_market,
                    start_date=start_yyyymmdd,
                    end_date=end_yyyymmdd,
                    adjust="qfq",
                )
                out = self._normalize_hist_df(df)
                return self._maybe_append_realtime_row(symbol, out, end_date, assume_locked=True)
            except Exception as exc:
                fallback_errors.append(f"sina={type(exc).__name__}: {exc}")

            # Source 3: Tencent
            try:
                df = ak.stock_zh_a_hist_tx(
                    symbol=symbol_with_market,
                    start_date=start_yyyymmdd,
                    end_date=end_yyyymmdd,
                    adjust="qfq",
                )
                out = self._normalize_hist_df(df)
                return self._maybe_append_realtime_row(symbol, out, end_date, assume_locked=True)
            except Exception as exc:
                fallback_errors.append(f"tencent={type(exc).__name__}: {exc}")

            raise SourceUnavailable(
                "cn_akshare is temporarily unavailable for price history "
                f"(eastmoney/sina/tencent all failed): em={em_last_exc}; "
                + "; ".join(fallback_errors)
            ) from em_last_exc

    def _fetch_realtime_row_unlocked(self, symbol: str) -> pd.DataFrame:
        ak = self._ak()
        spot = ak.stock_individual_spot_xq(symbol=self._xq_symbol(symbol))
        if spot is None or spot.empty:
            return pd.DataFrame()
        if not {"item", "value"}.issubset(set(spot.columns)):
            return pd.DataFrame()
        kv = dict(zip(spot["item"].astype(str), spot["value"]))

        date_val = pd.to_datetime(kv.get("时间"), errors="coerce")
        if pd.isna(date_val):
            date_val = pd.to_datetime(cn_today_str())
        row = {
            "Date": pd.to_datetime(date_val).normalize(),
            "Open": pd.to_numeric(kv.get("今开"), errors="coerce"),
            "High": pd.to_numeric(kv.get("最高"), errors="coerce"),
            "Low": pd.to_numeric(kv.get("最低"), errors="coerce"),
            "Close": pd.to_numeric(kv.get("现价"), errors="coerce"),
            "Volume": pd.to_numeric(kv.get("成交量"), errors="coerce"),
        }
        rt = pd.DataFrame([row]).dropna(subset=["Open", "High", "Low", "Close", "Volume"])
        return rt

    def _fetch_realtime_row(self, symbol: str) -> pd.DataFrame:
        with AKSHARE_CALL_LOCK:
            return self._fetch_realtime_row_unlocked(symbol)

    def _maybe_append_realtime_row(
        self,
        symbol: str,
        hist_df: pd.DataFrame,
        end_date: str,
        *,
        assume_locked: bool = False,
    ) -> pd.DataFrame:
        if hist_df is None:
            hist_df = pd.DataFrame()
        try:
            end_dt = pd.to_datetime(end_date, errors="coerce")
            if pd.isna(end_dt):
                return hist_df
            today = pd.to_datetime(cn_today_str())
            if end_dt.normalize() < today:
                return hist_df
            if not is_cn_trading_day(today.strftime("%Y-%m-%d")):
                return hist_df

            has_today = False
            if not hist_df.empty:
                has_today = (pd.to_datetime(hist_df["Date"]).dt.normalize() == today).any()
            if has_today:
                return hist_df

            phase = cn_market_phase()
            if phase in ("pre_open", "closed"):
                return hist_df

            if assume_locked:
                rt = self._fetch_realtime_row_unlocked(symbol)
            else:
                rt = self._fetch_realtime_row(symbol)
            if rt.empty:
                return hist_df
            if pd.to_datetime(rt.iloc[0]["Date"]).normalize() != today:
                return hist_df

            merged = pd.concat([hist_df, rt], ignore_index=True)
            merged = merged.sort_values("Date").drop_duplicates(subset=["Date"], keep="last")
            return merged.reset_index(drop=True)
        except Exception:
            return hist_df

    def get_stock_data(self, symbol: str, start_date: str, end_date: str) -> str:
        df = self._fetch_hist_df(symbol, start_date, end_date)
        return self._format_ak_hist(df, symbol, start_date, end_date)

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
        if df is None or df.empty:
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
        ind_df["date"] = pd.to_datetime(ind_df["date"], errors="coerce")
        ind_df = ind_df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)

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
            if key in values_by_date:
                value = values_by_date[key]
                if value == "N/A":
                    value = cn_no_data_reason(key)
            else:
                value = cn_no_data_reason(key)
            lines.append(f"{key}: {value}")
            d -= timedelta(days=1)

        result = (
            f"## {indicator} 指标值（{begin.strftime('%Y-%m-%d')} 至 {curr_date}）：\n\n"
            + "\n".join(lines)
            + "\n\n"
            + self.INDICATOR_DESCRIPTIONS[indicator]
        )
        return result

    # ── 研究类数据（对话式研判依赖）────────────────────────────────────
    # 取数实现集中在 cn_research_data，这里只做端口适配，避免两套实现漂移。

    def get_intraday_bars(self, symbol: str, day: str, freq: str = "5"):
        from . import cn_research_data

        return cn_research_data.fetch_intraday_frame(symbol, day, freq)

    def get_chip_distribution(self, symbol: str, day: str):
        from . import cn_research_data

        return cn_research_data.fetch_chip_frame(symbol, day)

    def get_flow_frame(self, symbol: str, day: str):
        from . import cn_research_data

        return cn_research_data.fetch_flow_frame(symbol, day)

    def get_peer_frames(self, symbol: str, day: str):
        from . import cn_research_data

        return cn_research_data.fetch_peer_frames(symbol, day)

    def get_stock_turnover(self, symbol: str, day: str):
        from . import cn_research_data

        return cn_research_data.fetch_stock_turnover(symbol, day)

    def get_symbol_news(self, symbol: str, start_date: str, end_date: str):
        from . import cn_research_data

        return cn_research_data.fetch_symbol_news(symbol, start_date, end_date)

    def get_daily_bars_df(self, symbol: str, start_date: str, end_date: str):
        """结构化日线（DataFrame）。

        `get_stock_data` 返回的是**预格式化的中文表格字符串**，适合喂提示词，
        但不适合数值计算——确定性信号层需要 DataFrame。这里直接复用既有的
        多源降级取数路径（`_fetch_hist_df`），不复制任何取数逻辑，避免两套
        实现随着时间漂移。
        """
        # 说信号层的语言：小写 OHLCV（`_fetch_hist_df` 的输出是提示词表格形态）
        return to_signal_bars(self._fetch_hist_df(symbol, start_date, end_date))

    def get_fundamentals(self, ticker: str, curr_date: str = None) -> str:
        with AKSHARE_CALL_LOCK:
            ak = self._ak()
            code = self._normalize_symbol(ticker)
            errors = []

            info_df = None
            try:
                info_df = ak.stock_individual_info_em(symbol=code)
            except Exception as exc:
                errors.append(f"stock_individual_info_em: {type(exc).__name__}")

            if info_df is None or info_df.empty:
                try:
                    info_df = ak.stock_individual_basic_info_xq(symbol=self._xq_symbol(ticker))
                    if not info_df.empty and set(info_df.columns) >= {"item", "value"}:
                        info_df = info_df.rename(columns={"item": "item", "value": "value"})
                except Exception as exc:
                    errors.append(f"stock_individual_basic_info_xq: {type(exc).__name__}")

            abstract_df = None
            try:
                abstract_df = ak.stock_financial_abstract(symbol=code)
            except Exception as exc:
                errors.append(f"stock_financial_abstract: {type(exc).__name__}")

            parts = [f"## Fundamentals for {ticker}"]
            if info_df is not None and not info_df.empty:
                for c in info_df.columns:
                    info_df[c] = info_df[c].astype(str).str.slice(0, 220)
                parts.append("### Company Profile")
                parts.append(info_df.head(40).to_markdown(index=False))
            if abstract_df is not None and not abstract_df.empty:
                parts.append("### Financial Abstract (latest available columns)")
                metric_cols = [c for c in abstract_df.columns if c not in ("选项", "指标")]
                top_cols = metric_cols[:8]
                cols = [c for c in ("选项", "指标") if c in abstract_df.columns] + top_cols
                abstract_slice = abstract_df[cols]
                period_line = self._latest_disclosure_period_line(abstract_slice)
                if period_line:
                    parts.append(period_line.rstrip())
                parts.append(self._shrink_table(abstract_slice, max_rows=20, max_cols=10).to_markdown(index=False))

            if len(parts) > 1:
                return "\n\n".join(parts)

            raise SourceUnavailable(
                "cn_akshare is temporarily unavailable for fundamentals: "
                + "; ".join(errors)
            )

    def _financial_report_sina(self, ticker: str, report_name: str) -> str:
        with AKSHARE_CALL_LOCK:
            ak = self._ak()
            symbol = self._sina_symbol(ticker)
            errors = []
            try:
                df = ak.stock_financial_report_sina(stock=symbol, symbol=report_name)
                if df is None or df.empty:
                    raise ValueError("empty dataframe")
                period_line = self._latest_disclosure_period_line(df)
                body = self._shrink_table(df, max_rows=12, max_cols=18).to_markdown(index=False)
                return f"{period_line}{body}"
            except Exception as exc:
                errors.append(f"stock_financial_report_sina: {type(exc).__name__}")

            code = self._normalize_symbol(ticker)
            indicator = "按报告期"
            try:
                # 同花顺摘要表作为备用，口径不完全一致但可作为降级保障
                df = ak.stock_financial_abstract_new_ths(symbol=code, indicator=indicator)
                if df is None or df.empty:
                    raise ValueError("empty dataframe")
                return self._shrink_table(df, max_rows=12, max_cols=18).to_markdown(index=False)
            except Exception as exc:
                errors.append(f"stock_financial_abstract_new_ths: {type(exc).__name__}")

            raise SourceUnavailable(
                f"cn_akshare is temporarily unavailable for {report_name}: {'; '.join(errors)}"
            )

    def get_balance_sheet(
        self, ticker: str, freq: str = "quarterly", curr_date: str = None
    ) -> str:
        table = self._financial_report_sina(ticker, "资产负债表")
        return f"## Balance Sheet ({ticker})\n\n{table}"

    def get_cashflow(
        self, ticker: str, freq: str = "quarterly", curr_date: str = None
    ) -> str:
        table = self._financial_report_sina(ticker, "现金流量表")
        return f"## Cashflow ({ticker})\n\n{table}"

    def get_income_statement(
        self, ticker: str, freq: str = "quarterly", curr_date: str = None
    ) -> str:
        table = self._financial_report_sina(ticker, "利润表")
        return f"## Income Statement ({ticker})\n\n{table}"

    def get_news(self, ticker: str, start_date: str, end_date: str) -> str:
        with AKSHARE_CALL_LOCK:
            ak = self._ak()
            code = self._normalize_symbol(ticker)
            try:
                df = ak.stock_news_em(symbol=code)
                if df is None or df.empty:
                    # 源**一条都没给**：只能说"这个源没给到"，不能说"窗口内没有新闻"。
                    # 两者的差别是这个模块的核心：前者是关于**源**的陈述，后者是关于
                    # **世界**的断言，而后者会让分析师得出"无催化剂/无负面报道"。
                    return (
                        f"{_NEWS_WINDOW_INSUFFICIENT} cn_akshare 未返回任何新闻条目"
                        f"（{ticker}，请求窗口 {start_date} 至 {end_date}）"
                    )

                date_col = "发布时间" if "发布时间" in df.columns else None
                fetched_n = int(len(df))
                oldest_dt = None
                start_dt = None
                if date_col is not None:
                    df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
                    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
                    end_dt = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
                    oldest_dt = df[date_col].min()
                    df = df[(df[date_col] >= start_dt) & (df[date_col] < end_dt)]

                if df.empty:
                    # ── 关键区分：源的覆盖范围**够不够到窗口起点** ──────────────
                    #
                    # `stock_news_em` 只有 `pageSize: 10` 且无分页，即结构上最多返回
                    # **最近 10 条**。窗口过滤后为空有两种截然不同的含义：
                    #
                    #   (a) 源最早的一条**早于**窗口起点 → 源的视野覆盖了窗口，
                    #       所以"窗口内没有新闻"是一个**有证据支撑**的断言，
                    #       但仍须限定在"返回的 N 条里"，不能冒充完整扫描。
                    #   (b) 源最早的一条**晚于**窗口起点 → 源根本看不到那么早，
                    #       此时说"窗口内没有新闻"就是把**能力不足**说成**事实**。
                    #       实测后果：任何历史日期的分析都会让新闻分析师读到
                    #       「该公司在此期间没有新闻」。真相应是**不知道**。
                    covers_window_start = (
                        oldest_dt is not None
                        and start_dt is not None
                        and not pd.isna(oldest_dt)
                        and oldest_dt.to_pydatetime() <= start_dt
                    )
                    if covers_window_start:
                        return (
                            f"在 cn_akshare 返回的最近 {fetched_n} 条新闻中，"
                            f"{start_date} 至 {end_date} 窗口内无新闻"
                            f"（该源单次上限 {_NEWS_SOURCE_ITEM_LIMIT} 条，非全量扫描）。"
                        )
                    oldest_txt = (
                        oldest_dt.strftime("%Y-%m-%d")
                        if oldest_dt is not None and not pd.isna(oldest_dt)
                        else "未知"
                    )
                    return (
                        f"{_NEWS_WINDOW_INSUFFICIENT} cn_akshare 单次仅返回最近 "
                        f"{_NEWS_SOURCE_ITEM_LIMIT} 条（本次 {fetched_n} 条，最早 {oldest_txt}），"
                        f"无法判断 {start_date} 至 {end_date} 是否有新闻"
                        f"（{ticker}）。请勿据此推断该期间无新闻或无催化剂。"
                    )

                rows = []
                for _, row in df.head(20).iterrows():
                    title = str(row.get("新闻标题", row.get("标题", "No title")))
                    src = str(row.get("文章来源", row.get("来源", "Unknown")))
                    summary = str(row.get("新闻内容", row.get("内容", "")))
                    link = str(row.get("新闻链接", row.get("链接", "")))
                    rows.append(f"### {title} (source: {src})")
                    if summary and summary != "nan":
                        rows.append(summary[:400])
                    if link and link != "nan":
                        rows.append(f"Link: {link}")
                    rows.append("")

                # 标题必须披露**源的能力边界**：只有最近 N 条被看过。否则读者会把
                # 「返回了 3 条」理解成「这个窗口里只有 3 条新闻」。
                return (
                    f"## {ticker} 新闻（{start_date} 至 {end_date}；"
                    f"cn_akshare 单次仅返回最近 {_NEWS_SOURCE_ITEM_LIMIT} 条，"
                    f"本次抓取 {fetched_n} 条、窗口内命中 {int(len(df))} 条）：\n\n"
                    + "\n".join(rows)
                )
            except Exception as exc:
                raise SourceUnavailable(
                    f"cn_akshare is temporarily unavailable for news: {exc}"
                ) from exc

    def get_global_news(
        self, curr_date: str, look_back_days: int = 7, limit: int = 50
    ) -> str:
        with AKSHARE_CALL_LOCK:
            ak = self._ak()
            try:
                if hasattr(ak, "news_cctv"):
                    target_dt = datetime.strptime(curr_date, "%Y-%m-%d")
                    used_date = curr_date
                    df = ak.news_cctv(date=curr_date.replace("-", ""))
                    if df is None or df.empty:
                        # Fallback: if today's feed is empty, try recent 3 days.
                        for back in range(1, 4):
                            probe_dt = target_dt - timedelta(days=back)
                            probe_date = probe_dt.strftime("%Y-%m-%d")
                            probe_df = ak.news_cctv(date=probe_date.replace("-", ""))
                            if probe_df is not None and not probe_df.empty:
                                df = probe_df
                                used_date = probe_date
                                break
                    if df is None or df.empty:
                        return f"{curr_date} 未获取到全球市场新闻（已回看最近3天）"
                    rows = []
                    for _, row in df.head(limit).iterrows():
                        title = str(row.get("title", row.get("标题", "No title")))
                        content = str(row.get("content", row.get("内容", "")))
                        rows.append(f"### {title}")
                        if content and content != "nan":
                            rows.append(content[:300])
                        rows.append("")
                    start = (
                        datetime.strptime(curr_date, "%Y-%m-%d") - timedelta(days=look_back_days)
                    ).strftime("%Y-%m-%d")
                    # 标题必须写**实际内容的日期跨度**，不能写请求窗口。
                    #
                    # 旧写法 `## 全球市场新闻（{start} 至 {curr_date}）` 里 start 是
                    # `curr_date - look_back_days`（调用方传 90），而内容其实只有
                    # `used_date` **一天**的新闻联播。宏观分析师会把 1 天的头条
                    # 当成 90 天的宏观扫描——标题凭空放大了 90 倍的覆盖面。
                    coverage = (
                        f"实际内容仅 {used_date} 一天（请求窗口 {start} 至 {curr_date}，"
                        f"该端点按日取数、不支持区间回溯）"
                    )
                    if used_date != curr_date:
                        return (
                            f"## 全球市场新闻（{coverage}；当日为空，已回退至 {used_date}）：\n\n"
                            + "\n".join(rows)
                        )
                    return f"## 全球市场新闻（{coverage}）：\n\n" + "\n".join(rows)
                return "当前 cn_akshare 实现暂不支持全球新闻接口。"
            except Exception as exc:
                raise SourceUnavailable(
                    f"cn_akshare is temporarily unavailable for global news: {exc}"
                ) from exc

    def get_insider_transactions(self, symbol: str) -> str:
        ak = self._ak()
        code = self._normalize_symbol(symbol)
        errors = []
        try:
            # stock_ggcg_em 不支持按个股代码查询，默认全市场数据量较大
            with AKSHARE_CALL_LOCK:
                df = ak.stock_main_stock_holder(stock=code)
            if df is not None and not df.empty:
                return (
                    f"## Insider Transactions for {symbol}\n\n"
                    f"{df.head(20).to_markdown(index=False)}"
                )
            errors.append("stock_main_stock_holder: empty dataframe")
        except Exception as exc:
            errors.append(f"stock_main_stock_holder: {type(exc).__name__}")

        try:
            # 退化为最近相关新闻，至少保证接口有可用输出
            end_date = datetime.now().strftime("%Y-%m-%d")
            start_date = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
            news = self.get_news(symbol, start_date, end_date)
            return (
                f"## Insider Transactions for {symbol}\n\n"
                f"未获取到股东交易明细，降级返回近两周公司相关新闻：\n\n{news}"
            )
        except Exception as exc:
            errors.append(f"news_fallback: {type(exc).__name__}")

        raise SourceUnavailable(
            f"cn_akshare is temporarily unavailable for insider transactions: {'; '.join(errors)}"
        )

    # TTL cache for stock_zh_a_spot_em to avoid hammering Eastmoney under concurrent load
    _spot_cache: "pd.DataFrame | None" = None
    _spot_cache_ts: float = 0.0
    _SPOT_CACHE_TTL: float = 8.0  # seconds

    def get_realtime_quotes(self, symbols: list[str]) -> str:
        """Fetch real-time A-share quotes. Tries Eastmoney first, falls back to Sina."""
        import json
        import time as _time
        import logging

        logger = logging.getLogger(__name__)

        # Build normalized code → original symbol map
        code_to_original: dict[str, str] = {}
        for s in symbols:
            if not s or not s.strip():
                continue
            try:
                code = self._normalize_symbol(s)
            except NotImplementedError:
                continue
            if code and code not in code_to_original:
                code_to_original[code] = s.strip().upper()

        if not code_to_original:
            return json.dumps({})

        # Try Sina first (lightweight, rarely blocked)
        try:
            result = self._fetch_quotes_sina(code_to_original)
            if result and result != "{}":
                return result
        except Exception as exc:
            logger.debug("[realtime-quotes] Sina failed, falling back to Eastmoney: %s", exc)

        # Fallback: Eastmoney via akshare (cached)
        now = _time.time()
        if (
            CnAkshareProvider._spot_cache is not None
            and (now - CnAkshareProvider._spot_cache_ts) < CnAkshareProvider._SPOT_CACHE_TTL
        ):
            df = CnAkshareProvider._spot_cache
        else:
            with AKSHARE_CALL_LOCK:
                ak = self._ak()
                df = ak.stock_zh_a_spot_em()
            CnAkshareProvider._spot_cache = df
            CnAkshareProvider._spot_cache_ts = now

        if df is not None and not df.empty:
            return self._build_quotes_from_em(df, code_to_original)
        return json.dumps({})

    def _build_quotes_from_em(self, df: "pd.DataFrame", code_to_original: dict[str, str]) -> str:
        import json
        normalized = list(code_to_original.keys())
        df = df[df["代码"].isin(normalized)]
        result: dict[str, dict] = {}
        for _, row in df.iterrows():
            code = str(row.get("代码", ""))
            original = code_to_original.get(code)
            if not original:
                continue
            price = self._safe_float(row.get("最新价"))
            prev_close = self._safe_float(row.get("昨收"))
            change = round(price - prev_close, 4) if price is not None and prev_close else None
            change_pct = round(change / prev_close * 100, 4) if change is not None and prev_close else None
            result[original] = {
                "price": price,
                "open": self._safe_float(row.get("今开")),
                "high": self._safe_float(row.get("最高")),
                "low": self._safe_float(row.get("最低")),
                "previous_close": prev_close,
                "change": change,
                "change_pct": change_pct,
                "volume": self._safe_float(row.get("成交量")),
                "amount": self._safe_float(row.get("成交额")),
                "source": "eastmoney",
            }
        return json.dumps(result, ensure_ascii=False)

    def _fetch_quotes_sina(self, code_to_original: dict[str, str]) -> str:
        """Fetch quotes from Sina Finance hq.sinajs.cn as fallback."""
        import json
        import requests as _requests

        sina_codes = []
        sina_to_original: dict[str, str] = {}
        for code, original in code_to_original.items():
            prefix = "sh" if code.startswith(("5", "6", "9")) else "bj" if code.startswith(("4", "8")) else "sz"
            sina_code = f"{prefix}{code}"
            sina_codes.append(sina_code)
            sina_to_original[sina_code] = original

        if not sina_codes:
            return json.dumps({})

        try:
            resp = _requests.get(
                "https://hq.sinajs.cn/list=" + ",".join(sina_codes),
                headers={"Referer": "https://finance.sina.com.cn/", "User-Agent": "Mozilla/5.0"},
                timeout=5,
            )
            resp.encoding = "gbk"
        except Exception:
            return json.dumps({})

        result: dict[str, dict] = {}
        for line in resp.text.splitlines():
            line = line.strip()
            if not line or '="' not in line:
                continue
            try:
                var_part, data_part = line.split('="', 1)
                sina_code = var_part.split("_")[-1]
                fields = data_part.rstrip('";').split(",")
                if len(fields) < 10:
                    continue
                original = sina_to_original.get(sina_code)
                if not original:
                    continue
                price = self._safe_float(fields[3])
                prev_close = self._safe_float(fields[2])
                change = round(price - prev_close, 4) if price is not None and prev_close else None
                change_pct = round(change / prev_close * 100, 4) if change is not None and prev_close else None
                # Sina fields[30]=date, fields[31]=time
                quote_time = None
                if len(fields) > 31 and fields[30] and fields[31]:
                    quote_time = f"{fields[30]} {fields[31]}"
                result[original] = {
                    "price": price,
                    "open": self._safe_float(fields[1]),
                    "high": self._safe_float(fields[4]),
                    "low": self._safe_float(fields[5]),
                    "previous_close": prev_close,
                    "change": change,
                    "change_pct": change_pct,
                    "volume": self._safe_float(fields[8]),
                    "amount": self._safe_float(fields[9]),
                    "quote_time": quote_time,
                    "source": "sina",
                }
            except (ValueError, IndexError):
                continue
        return json.dumps(result, ensure_ascii=False)

    @staticmethod
    def _safe_float(val) -> float | None:
        if val is None:
            return None
        try:
            f = float(val)
            return f if not pd.isna(f) else None
        except (ValueError, TypeError):
            return None

    def get_board_fund_flow(self) -> str:
        """获取行业板块资金流向排名。"""
        try:
            df = fetch_board_fund_flow_df(ak_module=self._ak())
            return format_board_fund_flow_ranking(df)
        except Exception as exc:
            return f"板块资金流向数据获取失败：{type(exc).__name__}: {exc}"

    def get_board_spot(self, sector_type: str = "industry") -> str:
        """获取板块涨幅榜（industry=行业板块 / concept=概念板块）。"""
        try:
            df = fetch_board_spot_df(self._ak(), sector_type)
            return format_board_spot_ranking(df, sector_type=sector_type)
        except Exception as exc:
            return f"板块涨幅榜获取失败：{type(exc).__name__}: {exc}"

    def get_board_rank(self, sector_type: str = "industry", period: str = "1d") -> str:
        """获取板块资金流排行（industry|concept，period: 1d|3d|5d|10d）。"""
        try:
            df = fetch_sector_fund_flow_rank_df(self._ak(), sector_type, period)
            return format_board_rank_ranking(df, sector_type=sector_type, period=period)
        except Exception as exc:
            return f"板块资金流排行获取失败：{type(exc).__name__}: {exc}"

    def get_board_hist(self, board_name: str, sector_type: str = "industry") -> str:
        """获取板块历史行情（近60日），用于判断主线持续性。board_name 为板块名。"""
        try:
            df = fetch_board_hist_df(self._ak(), board_name, sector_type)
            return format_board_hist_table(df, board_name, sector_type=sector_type)
        except Exception as exc:
            # 「无替代源」是给**"为什么没有兜底"**的答案：板块日线历史在本平台只有
            # 东财一个源（新浪不提供板块历史，同花顺板块指数没有接进这个工具），
            # 所以用户读到的应当是"只能等东财恢复"，而不是一句光秃秃的错误、
            # 让人以为是路由漏了下一个源。若将来接入同花顺板块指数
            # （`fetch_ths_board_index_df`），这句话要同步改。
            return f"板块历史行情获取失败（无替代源）：{type(exc).__name__}: {exc}"

    def get_board_cons(self, board_name: str, sector_type: str = "industry") -> str:
        """获取板块成分股列表，用于主线内选股。board_name 为板块名。"""
        try:
            df = fetch_board_cons_df(self._ak(), board_name, sector_type)
            return format_board_cons_table(df, board_name, sector_type=sector_type)
        except Exception as exc:
            return f"板块成分股获取失败：{type(exc).__name__}: {exc}"

    def get_individual_fund_flow(self, symbol: str) -> str:
        """获取个股近期主力资金净流向。"""
        try:
            ak = self._ak()
            code = self._normalize_symbol(symbol)
            # 沪市：以 5、6、9 开头；其余为深市
            market = "sh" if code[:1] in ("5", "6", "9") else "sz"
            for attempt in range(3):
                try:
                    with AKSHARE_CALL_LOCK:
                        df = ak.stock_individual_fund_flow(stock=code, market=market)
                    break
                except Exception as exc:
                    if attempt < 2:
                        time.sleep(0.6 * (2**attempt))
                    else:
                        raise
            if df is None or df.empty:
                return f"{symbol} 近期主力资金流向数据暂不可用。"
            df_recent = df.tail(5)
            return f"{symbol} 近5日主力资金净流向：\n{df_recent.to_string(index=False)}"
        except Exception as exc:
            return f"个股资金流向数据获取失败：{type(exc).__name__}: {exc}"

    def get_lhb_detail(self, symbol: str, date: str) -> str:
        """获取龙虎榜数据，非异动日返回空提示（属正常）。

        akshare 的 `stock_lhb_detail_em` 是**按日期区间取全市场明细**的接口，没有
        symbol 形参：`stock_lhb_detail_em(start_date, end_date)`。所以这里必须请求
        「当天」这个单日窗口，再按代码过滤出本标的。

        历史 bug（曾让每次深度分析都报 ChunkedEncodingError）：旧实现按「签名随版本
        变化」的假设逐个试调用，第一项 `func(code, date, date)` 因参数个数不符抛
        TypeError，于是落到第二项 `func(code, date)` —— akshare 接受并把 code 绑到
        `start_date`，再切片拼成 "6005-19-.SH"。该字符串排在所有 ISO 日期之前，
        filter 因而命中几乎全部历史：一次调用爬了全市场 402 页 × 5000 行，多在中途
        断连（ChunkedEncodingError / ProxyError）；即便侥幸成功，返回的也是**别的
        股票**的行，却挂在 "<symbol> 龙虎榜明细" 标题下。

        现在只有一条正确调用路径；签名若变化由
        `tests/test_lhb_detail_fetch.py::test_real_akshare_lhb_signature_is_a_pure_date_window`
        直接报红，而不是靠运行时瞎试。
        """
        try:
            ak = self._ak()
            code = self._normalize_symbol(symbol)
            # akshare 要 YYYYMMDD（内部会切成 YYYY-MM-DD），必须去掉连字符。
            day = str(date).replace("-", "").strip()
            if len(day) != 8 or not day.isdigit():
                return f"龙虎榜数据获取失败：日期无法识别（{date}）"
            with AKSHARE_CALL_LOCK:
                func = getattr(ak, "stock_lhb_detail_em", None)
                if func is None:
                    raise SourceUnsupported("akshare.stock_lhb_detail_em not found")
                # 单日窗口：start_date == end_date，这是让这次调用保持廉价的关键。
                df = func(start_date=day, end_date=day)
            if df is None or getattr(df, "empty", True):
                return f"{symbol} 在 {date} 无龙虎榜数据（非异动日属正常）。"
            hit = _filter_lhb_rows(df, code)
            if hit is None or hit.empty:
                return f"{symbol} 在 {date} 无龙虎榜数据（非异动日属正常）。"
            return f"{symbol} 龙虎榜明细（{date}）：\n{hit.head(20).to_string(index=False)}"
        except Exception as exc:
            # 上游解析崩溃（`result: null` ⇒ None["pages"]）时，回退直连**核实**这个
            # 窗口究竟是真的空、还是有数据被上游解析器丢了。不改主路径：akshare
            # 正常时仍由它供数（单日窗口的调用形状有测试钉着）。
            if _is_em_parse_crash(exc):
                recovered = _recover_lhb_via_direct(symbol, code, day, date)
                if recovered is not None:
                    return recovered
            return f"龙虎榜数据获取失败：{type(exc).__name__}: {exc}"

    def get_zt_pool(self, date: str) -> str:
        """获取涨停板情绪池，反映市场整体情绪温度。"""
        try:
            ak = self._ak()
            with AKSHARE_CALL_LOCK:
                df = ak.stock_zt_pool_em(date=date.replace("-", ""))
            if df is None or df.empty:
                return f"{date} 涨停板情绪池数据暂不可用。"
            count = len(df)
            result = f"{date} 涨停家数：{count}\n"
            if "连板数" in df.columns:
                lianban = df["连板数"].value_counts().sort_index()
                result += f"连板分布：\n{lianban.head(10).to_string()}"
            return result
        except Exception as exc:
            return f"涨停板情绪池数据获取失败：{type(exc).__name__}: {exc}"

    def get_hot_stocks_xq(self) -> str:
        """获取雪球热搜股票，反映散户关注度。"""
        try:
            ak = self._ak()
            with AKSHARE_CALL_LOCK:
                df = ak.stock_hot_follow_xq(symbol="最热门")
            if df is None or df.empty:
                return "雪球热搜数据暂不可用。"
            return f"雪球热搜前20：\n{df.head(20).to_string(index=False)}"
        except Exception as exc:
            return f"雪球热搜数据获取失败：{type(exc).__name__}: {exc}"
