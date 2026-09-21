"""信号层的数据端口。

## 为什么用 Protocol 而不是继承 BaseMarketDataProvider

仓库现有的 `BaseMarketDataProvider` 是一个 11 个 abstractmethod 的胖接口：
新增一个取数能力就要改 ABC、改全部 provider、并给 stub 补 NotImplementedError
占位。这里的每个端口只声明它自己需要的一两个方法，实现可以是任意现存对象
（甚至一个 lambda 包装），测试里直接给内存假实现，**零继承负担**。

## 为什么端口都是可选的

缺分时、缺筹码、缺板块快照是**常态**（分时历史深度有限、筹码接口不稳定）。
把它们设成必填会迫使调用方伪造空实现，从而把「缺失」伪装成「有数据但为空」——
那正是判定质量虚高的来源。可选端口让缺失成为一等状态，由维度显式报告
`available=False` 并封顶置信度。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import pandas as pd


@dataclass(frozen=True)
class PeerSnapshot:
    """同板块/大盘的相对基准快照。

    双基准（见 design D16）：行业基准判断「是否被错杀」，主线基准判断
    「主线是否退潮」。两者背离本身就是信号，因此分开保存而不是合并成
    一个「相对强度」数字。
    """

    board_name: str | None = None
    board_median_turnover: float | None = None  # 单位：百分比，如 2.1 表示 2.1%
    board_return_pct: float | None = None
    market_return_pct: float | None = None
    stock_turnover: float | None = None  # 单位：百分比
    mainline_board_name: str | None = None
    mainline_return_pct: float | None = None


@runtime_checkable
class DailyBarsPort(Protocol):
    """日线端口。end 为截止日（含），lookback 为请求的交易日数。"""

    def bars(self, symbol: str, end: str, lookback: int) -> pd.DataFrame: ...


@runtime_checkable
class IntradayBarsPort(Protocol):
    """分时端口。返回该交易日的分钟级 bars；不可得时返回 None 或空表。"""

    def bars(self, symbol: str, day: str, freq: str) -> pd.DataFrame | None: ...


@runtime_checkable
class ChipPort(Protocol):
    """筹码分布端口（获利盘比例 / 平均成本 / 集中度）。"""

    def chips(self, symbol: str, day: str) -> pd.DataFrame | None: ...


@runtime_checkable
class CapitalFlowPort(Protocol):
    """资金流端口（主力 / 超大单 / 大单净流入，单位：元）。"""

    def flow(self, symbol: str, end: str, lookback: int) -> pd.DataFrame | None: ...


@runtime_checkable
class PeerPort(Protocol):
    """板块与大盘基准端口。"""

    def snapshot(self, symbol: str, day: str) -> PeerSnapshot | None: ...


@dataclass(frozen=True)
class SignalPorts:
    """显式聚合的数据端口集合。

    刻意不做成全局单例：`dataflows/config.py` 的 `global _config` 在并发请求
    下会串味（一个用户的配置可能影响另一个请求的取数路径），而且测试之间
    会互相污染。显式传参让「这一次评估用哪些数据源」完全由调用方决定。
    """

    daily: DailyBarsPort
    intraday: IntradayBarsPort | None = None
    chips: ChipPort | None = None
    capital: CapitalFlowPort | None = None
    peer: PeerPort | None = None
