"""信号层测试夹具（非测试模块，pytest 不收集）。

设计取舍：信号层要求「纯函数、零 IO、零时钟」，因此测试数据必须由测试
自己构造，不能依赖 provider。这里提供最小可用的构造器，让每个维度的
边界条件都能被精确表达 —— 例如「缩量下跌但反抽同样缩量」这种关键
判据，必须能用 5 个数字写出来，而不是去线上捞一段行情。
"""

from __future__ import annotations

import pandas as pd

__all__ = [
    "trading_days",
    "make_daily",
    "make_intraday",
    "make_chips",
    "make_flow",
    "make_context",
]


def trading_days(n: int, start: str = "2026-01-05") -> list[str]:
    """生成 n 个工作日（跳过周末），使 as_of 与日期序列保持真实感。"""
    days: list[str] = []
    cur = pd.Timestamp(start)
    while len(days) < n:
        if cur.weekday() < 5:
            days.append(cur.strftime("%Y-%m-%d"))
        cur += pd.Timedelta(days=1)
    return days


def make_daily(
    closes: list[float],
    volumes: list[float],
    *,
    start: str = "2026-01-05",
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    opens: list[float] | None = None,
    amounts: list[float] | None = None,
) -> pd.DataFrame:
    """构造归一化日线（列名与 dataflows 的 _OHLCV_COLS 一致：date/open/high/low/close/volume）。

    未显式给出 open/high/low 时按收盘价推导一个自洽的包络，保证
    「high >= max(open, close) >= min(open, close) >= low」这类不变量成立，
    否则维度的影线/实体计算会得到无意义结果。
    """
    if len(closes) != len(volumes):
        raise ValueError("closes 与 volumes 长度必须一致")

    n = len(closes)
    dates = trading_days(n, start)
    opens = opens if opens is not None else [closes[0]] + list(closes[:-1])
    if highs is None:
        highs = [max(o, c) * 1.005 for o, c in zip(opens, closes)]
    if lows is None:
        lows = [min(o, c) * 0.995 for o, c in zip(opens, closes)]

    data: dict[str, list[float]] = {
        "open": list(opens),
        "high": list(highs),
        "low": list(lows),
        "close": list(closes),
        "volume": list(volumes),
    }
    if amounts is not None:
        data["amount"] = list(amounts)

    df = pd.DataFrame({"date": dates, **data})
    return df


def make_intraday(
    day: str,
    prices: list[float],
    volumes: list[float],
    *,
    freq: str = "5",
) -> pd.DataFrame:
    """构造分时数据。列名 date/open/high/low/close/volume，与日线保持同一形状。"""
    if len(prices) != len(volumes):
        raise ValueError("prices 与 volumes 长度必须一致")
    n = len(prices)
    # 09:35 起每 5 分钟一根，仅用于生成可读的时间戳（维度不依赖具体时刻）
    stamps = [
        (pd.Timestamp(f"{day} 09:30") + pd.Timedelta(minutes=5 * (i + 1))).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        for i in range(n)
    ]
    opens = [prices[0]] + list(prices[:-1])
    return pd.DataFrame(
        {
            "date": stamps,
            "open": opens,
            "high": [max(o, c) for o, c in zip(opens, prices)],
            "low": [min(o, c) for o, c in zip(opens, prices)],
            "close": list(prices),
            "volume": list(volumes),
            "freq": [freq] * n,
        }
    )


def make_chips(
    *,
    profit_ratio: float | None,
    avg_cost: float | None,
    concentration_90: float | None = None,
    concentration_70: float | None = None,
    as_of: str = "2026-01-05",
) -> pd.DataFrame:
    """构造筹码分布（对应 akshare stock_cyq_em 的字段语义，单位已归一为 0-1 比率）。"""
    return pd.DataFrame(
        [
            {
                "date": as_of,
                "profit_ratio": profit_ratio,
                "avg_cost": avg_cost,
                "concentration_90": concentration_90,
                "concentration_70": concentration_70,
            }
        ]
    )


def make_flow(
    *,
    main_net: float,
    super_large_net: float | None = None,
    large_net: float | None = None,
    as_of: str = "2026-01-05",
) -> pd.DataFrame:
    """构造资金流。金额单位统一为「元」，符号表示净流入/流出。"""
    return pd.DataFrame(
        [
            {
                "date": as_of,
                "main_net_inflow": main_net,
                "super_large_net_inflow": super_large_net,
                "large_net_inflow": large_net,
            }
        ]
    )


def make_context(
    *,
    symbol: str = "600519.SH",
    daily: pd.DataFrame | None = None,
    intraday: pd.DataFrame | None = None,
    chips: pd.DataFrame | None = None,
    flow: pd.DataFrame | None = None,
    peer=None,
    window_days: int = 20,
):
    """组装 SignalContext。信号层维度只认这个对象，不认 provider。"""
    from tradingagents.signals.context import SignalContext

    if daily is None:
        daily = make_daily([10.0] * 10, [1000.0] * 10)
    as_of = str(daily["date"].iloc[-1])[:10]
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
