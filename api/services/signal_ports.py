"""把既有数据源适配成信号层的数据端口。

## 为什么需要这一层而不是让信号层直接调 provider

信号层是纯函数（由架构边界测试强制），它只认 `Protocol` 端口。这一层是
**唯一的转换点**，因此：

* 换数据源（akshare → 券商 API）只改这里
* 测试信号层不需要网络
* 「哪些维度拿不到」这件事有明确归属——端口返回 None/空，维度显式报告
  `available=False`，而不是抛异常中断整个评估

## 两个刻意的设计

1. **端口全部是「尽力而为」**：任何取数失败都返回 None，并留下日志。
   缺分时/筹码是常态，让它中断评估等于把「数据不全」变成「功能不可用」。
2. **换手率口径必须与板块可比**：D6 要求用**板块中位换手**消解「缩量」歧义，
   因此这里取板块成分股换手率的中位数，而不是板块指数自身的换手率——
   后者与个股换手不在同一量纲上，比较没有意义。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Sequence

import pandas as pd

from tradingagents.signals.ports import PeerSnapshot, SignalPorts

logger = logging.getLogger(__name__)

# 日线回看窗口：取 window_days 的若干倍自然日，覆盖停牌与长假
_CALENDAR_PADDING = 3
_MIN_CALENDAR_DAYS = 30


def _to_date_range(as_of: str, window_days: int) -> tuple[str, str]:
    import datetime as _dt

    end = _dt.date.fromisoformat(as_of)
    span = max(window_days * _CALENDAR_PADDING, _MIN_CALENDAR_DAYS)
    start = end - _dt.timedelta(days=span)
    return start.isoformat(), end.isoformat()


@dataclass
class ProviderDailyBarsPort:
    """日线端口。`provider` 需提供 `get_daily_bars_df(symbol, start, end) -> DataFrame`。"""

    provider: Any

    def bars(self, symbol: str, end: str, lookback: int) -> pd.DataFrame:
        start, end_date = _to_date_range(end, max(lookback, 6))
        frame = self.provider.get_daily_bars_df(symbol, start, end_date)
        if frame is None or len(frame) == 0:
            raise RuntimeError(f"daily bars empty for {symbol} up to {end}")
        return frame


@dataclass
class ProviderIntradayPort:
    """分时端口。取不到返回 None（不抛异常）——缺分时是常态。"""

    fetcher: Callable[[str, str, str], pd.DataFrame | None]

    def bars(self, symbol: str, day: str, freq: str) -> pd.DataFrame | None:
        try:
            return self.fetcher(symbol, day, freq)
        except Exception as exc:  # noqa: BLE001
            logger.info("intraday unavailable for %s: %s", symbol, exc)
            return None


@dataclass
class ProviderChipPort:
    """筹码分布端口。"""

    fetcher: Callable[[str, str], pd.DataFrame | None]

    def chips(self, symbol: str, day: str) -> pd.DataFrame | None:
        try:
            return self.fetcher(symbol, day)
        except Exception as exc:  # noqa: BLE001
            logger.info("chip distribution unavailable for %s: %s", symbol, exc)
            return None


@dataclass
class ProviderCapitalFlowPort:
    """资金流端口。"""

    fetcher: Callable[[str, str], pd.DataFrame | None]

    def flow(self, symbol: str, end: str, lookback: int) -> pd.DataFrame | None:
        try:
            return self.fetcher(symbol, end)
        except Exception as exc:  # noqa: BLE001
            logger.info("capital flow unavailable for %s: %s", symbol, exc)
            return None


# 板块行情表里可能的列名（不同数据源口径不同，逐个尝试而不是硬编码一个）
_BOARD_TURNOVER_COLUMNS = ("换手率", "turnover", "turnover_rate")
_BOARD_CHANGE_COLUMNS = ("涨跌幅", "change_pct", "pct_change")
_BOARD_NAME_COLUMNS = ("板块名称", "名称", "board_name", "name")
_CONS_TURNOVER_COLUMNS = ("换手率", "turnover", "turnover_rate")
_CONS_INDUSTRY_COLUMNS = ("行业", "所属行业", "industry", "板块名称")


def _pick_column(frame: pd.DataFrame, candidates: Sequence[str]) -> str | None:
    for name in candidates:
        if name in frame.columns:
            return name
    return None


def _median(series: pd.Series) -> float | None:
    values = pd.to_numeric(series, errors="coerce").dropna()
    if values.empty:
        return None
    return float(values.median())


def build_peer_snapshot(
    *,
    symbol: str,
    board_name: str | None,
    board_frame: pd.DataFrame | None,
    cons_frame: pd.DataFrame | None,
    stock_turnover: float | None,
    mainline_board_name: str | None = None,
    mainline_frame: pd.DataFrame | None = None,
) -> PeerSnapshot | None:
    """由板块行情与成分股表构造相对基准。

    拆成纯函数是为了让「口径」可以被单测：板块中位换手取**成分股**换手率的
    中位数（与个股同量纲），而不是板块指数自身的换手率。
    """
    board_return: float | None = None
    if board_frame is not None and not board_frame.empty and board_name:
        name_col = _pick_column(board_frame, _BOARD_NAME_COLUMNS)
        change_col = _pick_column(board_frame, _BOARD_CHANGE_COLUMNS)
        if name_col and change_col:
            match = board_frame[board_frame[name_col].astype(str) == str(board_name)]
            if not match.empty:
                value = pd.to_numeric(match.iloc[0][change_col], errors="coerce")
                board_return = None if pd.isna(value) else float(value)

    board_median_turnover: float | None = None
    if cons_frame is not None and not cons_frame.empty:
        turnover_col = _pick_column(cons_frame, _CONS_TURNOVER_COLUMNS)
        if turnover_col:
            board_median_turnover = _median(cons_frame[turnover_col])

    mainline_return: float | None = None
    if mainline_frame is not None and not mainline_frame.empty and mainline_board_name:
        name_col = _pick_column(mainline_frame, _BOARD_NAME_COLUMNS)
        change_col = _pick_column(mainline_frame, _BOARD_CHANGE_COLUMNS)
        if name_col and change_col:
            match = mainline_frame[mainline_frame[name_col].astype(str) == str(mainline_board_name)]
            if not match.empty:
                value = pd.to_numeric(match.iloc[0][change_col], errors="coerce")
                mainline_return = None if pd.isna(value) else float(value)

    if board_return is None and board_median_turnover is None and mainline_return is None:
        return None

    return PeerSnapshot(
        board_name=board_name,
        board_median_turnover=board_median_turnover,
        board_return_pct=board_return,
        market_return_pct=None,
        stock_turnover=stock_turnover,
        mainline_board_name=mainline_board_name,
        mainline_return_pct=mainline_return,
    )


@dataclass
class ProviderPeerPort:
    """板块基准端口。

    取数由注入的 `loader` 完成（返回 (行业名, 板块行情表, 成分股表) 或 None），
    口径计算交给纯函数 `build_peer_snapshot`，因此口径可单测、取数可替换。
    """

    loader: Callable[[str, str], tuple[str | None, pd.DataFrame | None, pd.DataFrame | None] | None]
    mainline_lookup: Callable[[str], str | None] | None = None
    mainline_loader: Callable[[str], pd.DataFrame | None] | None = None
    turnover_lookup: Callable[[str, str], float | None] | None = None

    def snapshot(self, symbol: str, day: str) -> PeerSnapshot | None:
        try:
            loaded = self.loader(symbol, day)
        except Exception as exc:  # noqa: BLE001
            logger.info("peer snapshot unavailable for %s: %s", symbol, exc)
            return None
        if loaded is None:
            return None
        board_name, board_frame, cons_frame = loaded

        stock_turnover = None
        if self.turnover_lookup is not None:
            try:
                stock_turnover = self.turnover_lookup(symbol, day)
            except Exception:  # noqa: BLE001
                stock_turnover = None

        mainline_name = None
        mainline_frame = None
        if self.mainline_lookup is not None:
            try:
                mainline_name = self.mainline_lookup(symbol)
            except Exception:  # noqa: BLE001
                mainline_name = None
        if mainline_name and self.mainline_loader is not None:
            try:
                mainline_frame = self.mainline_loader(mainline_name)
            except Exception:  # noqa: BLE001
                mainline_frame = None

        return build_peer_snapshot(
            symbol=symbol,
            board_name=board_name,
            board_frame=board_frame,
            cons_frame=cons_frame,
            stock_turnover=stock_turnover,
            mainline_board_name=mainline_name,
            mainline_frame=mainline_frame,
        )


def build_signal_ports(
    *,
    daily: ProviderDailyBarsPort,
    intraday: ProviderIntradayPort | None = None,
    chips: ProviderChipPort | None = None,
    capital: ProviderCapitalFlowPort | None = None,
    peer: ProviderPeerPort | None = None,
) -> SignalPorts:
    """装配端口集合。缺哪个就传 None——缺失是一等状态，不是错误。"""
    return SignalPorts(daily=daily, intraday=intraday, chips=chips, capital=capital, peer=peer)
