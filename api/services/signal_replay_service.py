"""离线回放编排：对历史 `(symbol, as_of)` 逐点重跑探针并回填兑现结果。

## 防前视是这一层的核心职责

回放的价值完全取决于「判定只用 as_of 之前的数据」。`build_context` 已经会在
as_of 处截断（即使 provider 多返回了未来数据），本层再保证**兑现窗口单独取**：
用同一份行情同时喂判定与兑现，很容易在实现里不小心把未来数据漏进判定。

因此：判定用 `[:as_of]`，兑现用 `(as_of, as_of+horizon]`，两段显式切开。

## 产出只用于「否决」

返回的报告里不含任何权重建议，只含曲线、平台区、维度判别力与闸门状态。
`check_activation_gate` 会明确拒绝用离线样本激活权重——这条切分由代码执行，
不靠约定。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

import pandas as pd

from tradingagents.signals import DEFAULT_WEIGHTS, WashoutWeights, evaluate
from tradingagents.signals.outcome import (
    DEFAULT_OUTCOME_PARAMS,
    OutcomeParams,
    add_trading_days,
    evaluate_outcome,
)
from tradingagents.signals.ports import SignalPorts
from tradingagents.signals.replay import (
    DEFAULT_TRAIN_RATIO,
    ActivationGate,
    CurvePoint,
    PlateauResult,
    ReplaySample,
    calibration_curve,
    check_activation_gate,
    dimension_power,
    find_plateau,
    non_discriminative_dimensions,
    offline_authority,
    time_split,
)

logger = logging.getLogger(__name__)

BarsLoader = Callable[[str, str, str], pd.DataFrame]

DEFAULT_THRESHOLDS = (0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40)


@dataclass
class ReplayReport:
    samples: list[ReplaySample] = field(default_factory=list)
    curve: list[CurvePoint] = field(default_factory=list)
    plateau: PlateauResult | None = None
    dimension_power: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    rejected_dimensions: tuple[str, ...] = ()
    train_size: int = 0
    holdout_size: int = 0
    holdout_hit_rate: float | None = None
    train_hit_rate: float | None = None
    activation: ActivationGate | None = None
    authority: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "samples": len(self.samples),
            "train_size": self.train_size,
            "holdout_size": self.holdout_size,
            "train_hit_rate": self.train_hit_rate,
            "holdout_hit_rate": self.holdout_hit_rate,
            "curve": [p.__dict__ for p in self.curve],
            "plateau": self.plateau.__dict__ if self.plateau else None,
            "dimension_power": dict(self.dimension_power),
            "rejected_dimensions": list(self.rejected_dimensions),
            "activation": self.activation.__dict__ if self.activation else None,
            "authority": dict(self.authority),
        }


class _LoaderPort:
    """把 bars loader 适配成信号层日线端口。

    刻意**不**做任何截断：截断由 `build_context` 负责（它才是唯一的防前视闸门，
    且已被单测覆盖）。在这里重复实现一次截断等于增加一个可能不一致的第二处。
    """

    def __init__(self, loader: BarsLoader, span_days: int) -> None:
        self._loader = loader
        self._span = span_days

    def bars(self, symbol: str, end: str, lookback: int) -> pd.DataFrame:
        start = add_trading_days(end, -max(lookback * 2, self._span))
        frame = self._loader(symbol, start, end)
        if frame is None:
            raise RuntimeError(f"no bars for {symbol}")
        return frame


def _hit_rate(samples: Sequence[ReplaySample]) -> float | None:
    rows = [s for s in samples if s.evaluable]
    if not rows:
        return None
    return sum(1 for s in rows if s.threshold_hit) / len(rows)


def replay_one(
    *,
    symbol: str,
    as_of: str,
    loader: BarsLoader,
    window_days: int = 20,
    horizon_days: int = 5,
    weights: WashoutWeights = DEFAULT_WEIGHTS,
    params: OutcomeParams = DEFAULT_OUTCOME_PARAMS,
    span_days: int = 60,
    ports_factory: Callable[[str, str], SignalPorts] | None = None,
) -> ReplaySample | None:
    """对一个历史基准日重跑判定并回填兑现结果。

    `ports_factory` 必须注入**完整端口**（含 peer）。只给日线端口时相对强度
    不可得，而它是四个核心维度之一 → 每次判定都会弃权 → 回放样本为零。
    这个后果不会报错，只是「校准一直没有样本」，所以这里明确支持注入并在
    文档里点明。`SignalPorts(daily=...)` 仅作为单测与降级路径的默认值。
    """
    ports = ports_factory(symbol, as_of) if ports_factory is not None else SignalPorts(
        daily=_LoaderPort(loader, span_days)
    )
    try:
        result = evaluate(
            ports=ports, symbol=symbol, as_of=as_of, window_days=window_days, weights=weights
        )
    except Exception as exc:  # noqa: BLE001 — 单点失败不中断整批回放
        logger.info("replay evaluation failed for %s@%s: %s", symbol, as_of, exc)
        return None

    # ── 兑现窗口单独取：判定已只用 as_of 之前的数据，这里取之后的 ──────────
    target = add_trading_days(as_of, horizon_days)
    entry_high: float | None = None
    entry_close: float | None = None
    window = pd.DataFrame(columns=["date", "high", "close"])
    try:
        frame = loader(symbol, as_of, target)
        if frame is not None and len(frame):
            frame = frame.copy()
            frame["date"] = frame["date"].astype(str).str[:10]
            frame = frame.sort_values("date")
            entry_rows = frame[frame["date"] <= as_of]
            if not entry_rows.empty:
                entry = entry_rows.iloc[-1]
                entry_high = float(entry["high"])
                entry_close = float(entry["close"])
            window = frame[frame["date"] > as_of].reset_index(drop=True)
    except Exception as exc:  # noqa: BLE001
        logger.info("replay outcome fetch failed for %s@%s: %s", symbol, as_of, exc)

    if entry_high is None or entry_close is None:
        outcome = None
    else:
        outcome = evaluate_outcome(
            state=result.state,
            entry_high=entry_high,
            entry_close=entry_close,
            bars=window,
            horizon_days=horizon_days,
            params=params,
        )

    contributions = {
        c.key: c.contribution for c in result.components if c.available
    }
    return ReplaySample(
        symbol=symbol,
        as_of=as_of,
        state=result.state,
        margin=float(result.margin),
        confidence=int(result.confidence),
        evaluable=bool(outcome.evaluable) if outcome else False,
        threshold_hit=bool(outcome.threshold_hit) if outcome else False,
        contributions=contributions,
    )


def replay(
    *,
    points: Sequence[tuple[str, str]],
    loader: BarsLoader,
    window_days: int = 20,
    horizon_days: int = 5,
    weights: WashoutWeights = DEFAULT_WEIGHTS,
    thresholds: Sequence[float] = DEFAULT_THRESHOLDS,
    train_ratio: float = DEFAULT_TRAIN_RATIO,
    min_plateau_sample: int = 10,
    activation: ActivationGate | None = None,
    ports_factory: Callable[[str, str], SignalPorts] | None = None,
) -> ReplayReport:
    """对一批 `(symbol, as_of)` 执行回放并产出标定报告。

    样本量门槛由调用方通过 `activation` 传入（通常来自数据库的真实前瞻样本数），
    因此本函数**不会**自行宣称可以激活权重——它只能否决维度。
    """
    samples: list[ReplaySample] = []
    for symbol, as_of in points:
        sample = replay_one(
            symbol=symbol,
            as_of=as_of,
            loader=loader,
            window_days=window_days,
            horizon_days=horizon_days,
            weights=weights,
            ports_factory=ports_factory,
        )
        if sample is not None:
            samples.append(sample)

    train, holdout = time_split(samples, train_ratio=train_ratio)
    curve = calibration_curve(train, thresholds=thresholds)
    plateau = find_plateau(curve, min_sample=min_plateau_sample)
    power = dimension_power(train)

    gate = activation or check_activation_gate(
        offline_samples=len(samples), forward_samples=0, per_dimension={}
    )

    return ReplayReport(
        samples=samples,
        curve=curve,
        plateau=plateau,
        dimension_power=power,
        rejected_dimensions=non_discriminative_dimensions(power),
        train_size=len(train),
        holdout_size=len(holdout),
        train_hit_rate=_hit_rate(train),
        holdout_hit_rate=_hit_rate(holdout),
        activation=gate,
        authority=offline_authority(),
    )


def build_replay_points(
    *,
    symbols: Sequence[str],
    dates: Sequence[str],
) -> list[tuple[str, str]]:
    """笛卡尔积展开回放点。样本量目标 ≥500（跨 ≥100 只股票 × 三种市场状态）。"""
    return [(symbol, day) for symbol in symbols for day in dates]


def default_ports_factory(*, span_days: int = 60):
    """默认端口工厂：装配真实数据源（含板块基准）。

    单独提供是为了让调用方不必自己拼端口——拼错了（例如漏掉 peer）的后果是
    「回放全部弃权」，不报错、只是没有样本。
    """
    from api.services.daily_bar_store import wrap_daily_port
    from api.services.signal_ports import (
        ProviderCapitalFlowPort,
        ProviderChipPort,
        ProviderDailyBarsPort,
        ProviderIntradayPort,
        ProviderPeerPort,
        build_signal_ports,
    )
    from api.services import signal_service
    from tradingagents.dataflows.providers.registry import build_default_registry

    provider = build_default_registry().get("cn_akshare")

    def factory(symbol: str, as_of: str) -> SignalPorts:
        if provider is None:
            raise RuntimeError("no market data provider is registered")
        return build_signal_ports(
            # 回放对几百个 (symbol, as_of) 逐点重跑，是实时取数代价最大、也最容易触发
            # 封禁的地方：同一标的的历史日线只该从网上取一次，之后走本地库。
            daily=wrap_daily_port(ProviderDailyBarsPort(provider=provider)),
            intraday=ProviderIntradayPort(fetcher=signal_service._intraday_fetcher),
            chips=ProviderChipPort(fetcher=signal_service._chips_fetcher),
            capital=ProviderCapitalFlowPort(fetcher=signal_service._capital_fetcher),
            peer=ProviderPeerPort(
                loader=signal_service._peer_loader,
                turnover_lookup=signal_service._stock_turnover_lookup,
            ),
        )

    return factory


def default_loader() -> BarsLoader:
    """默认取数：复用 provider 的结构化缝隙（不解析格式化字符串）。"""
    from tradingagents.dataflows.providers.registry import build_default_registry

    provider = build_default_registry().get("cn_akshare")

    def loader(symbol: str, start: str, end: str) -> pd.DataFrame:
        if provider is None:
            raise RuntimeError("no market data provider is registered")
        return provider.get_daily_bars_df(symbol, start, end)

    return loader
