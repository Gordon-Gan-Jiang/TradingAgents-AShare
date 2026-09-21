"""SignalContext：把端口取到的数据归一化成维度可以直接消费的不可变快照。

## 为什么必须在这里截断

回放校准（design D15）会对历史 `(symbol, as_of)` 逐点重跑探针。如果维度
能看到 `as_of` 之后的行，回放结果就包含未来信息，整个校准闭环失去意义。
因此**即使 provider 无视 `end` 参数多返回数据**，这里也必须自己截断——
把「防前视」放在数据入口，而不是指望每个 provider 都守规矩。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import pandas as pd

from .ports import SignalPorts

logger = logging.getLogger(__name__)

# 量能维度需要「近 5 日均量 + 当根」，所以 6 根是最小可用窗口。
# 低于此值不报错为「中性」，而是显式失败——中性会伪装成「无异常信号」，
# 而实际是「没有足够数据判断」。
MIN_BARS = 6

_DAILY_COLUMNS = ("date", "open", "high", "low", "close", "volume")


class InsufficientHistory(RuntimeError):
    """历史数据不足，无法计算需要窗口的维度。"""


@dataclass(frozen=True)
class SignalContext:
    """一次评估所需的全部输入快照。不可变，因此可安全地在维度间共享。"""

    symbol: str
    as_of: str
    window_days: int
    daily: pd.DataFrame
    intraday: pd.DataFrame | None = None
    chips: pd.DataFrame | None = None
    flow: pd.DataFrame | None = None
    peer: Any | None = None

    @property
    def has_intraday(self) -> bool:
        return self.intraday is not None and not self.intraday.empty

    @property
    def has_chips(self) -> bool:
        return self.chips is not None and not self.chips.empty

    @property
    def has_flow(self) -> bool:
        return self.flow is not None and not self.flow.empty

    @property
    def has_peer(self) -> bool:
        return self.peer is not None


def _normalize_daily(raw: pd.DataFrame, as_of: str, window_days: int) -> pd.DataFrame:
    """归一化日线：补列、按日期升序、截断到 as_of、取尾部 window 根。"""
    if raw is None or raw.empty:
        raise InsufficientHistory("daily bars returned empty")

    df = raw.copy()
    missing = [c for c in ("date", "close") if c not in df.columns]
    if missing:
        raise InsufficientHistory(f"daily bars missing required columns: {missing}")

    df["date"] = df["date"].astype(str).str[:10]
    df = df[df["date"] <= as_of]
    if df.empty:
        raise InsufficientHistory(f"no daily bars on or before {as_of}")

    df = df.sort_values("date").drop_duplicates(subset=["date"], keep="last").reset_index(drop=True)
    for col in _DAILY_COLUMNS:
        if col not in df.columns:
            df[col] = pd.NA
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    df = df.tail(window_days).reset_index(drop=True)

    if len(df) < MIN_BARS:
        raise InsufficientHistory(
            f"need >= {MIN_BARS} daily bars, got {len(df)} (as_of={as_of}, window={window_days})"
        )
    return df


def _optional(fetch, label: str):
    """可选数据源：任何失败都降级为 None，并留下日志。

    刻意不抛异常：缺分时/筹码是常态，让它中断整次评估等于把「数据不全」
    变成「功能不可用」。缺失由维度显式报告，置信度由探针封顶。
    """
    try:
        out = fetch()
    except Exception as exc:  # noqa: BLE001 — 可选源失败不应中断评估
        logger.info("signals: optional source %s unavailable: %s: %s", label, type(exc).__name__, exc)
        return None
    if out is None:
        return None
    if isinstance(out, pd.DataFrame) and out.empty:
        return None
    return out


def build_context(
    *,
    ports: SignalPorts,
    symbol: str,
    as_of: str,
    window_days: int = 20,
    intraday_freq: str = "5",
) -> SignalContext:
    """从端口装配上下文。这是信号层**唯一**接触端口的地方。"""
    daily_raw = ports.daily.bars(symbol, as_of, window_days)
    daily = _normalize_daily(daily_raw, as_of, window_days)

    intraday = None
    if ports.intraday is not None:
        intraday = _optional(lambda: ports.intraday.bars(symbol, as_of, intraday_freq), "intraday")

    chips = None
    if ports.chips is not None:
        chips = _optional(lambda: ports.chips.chips(symbol, as_of), "chips")

    flow = None
    if ports.capital is not None:
        flow = _optional(lambda: ports.capital.flow(symbol, as_of, window_days), "capital_flow")

    peer = None
    if ports.peer is not None:
        peer = _optional(lambda: ports.peer.snapshot(symbol, as_of), "peer")

    return SignalContext(
        symbol=symbol,
        as_of=as_of,
        window_days=window_days,
        daily=daily,
        intraday=intraday,
        chips=chips,
        flow=flow,
        peer=peer,
    )
