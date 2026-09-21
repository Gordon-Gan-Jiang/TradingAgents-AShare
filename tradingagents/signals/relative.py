"""相对强度与相对换手分类。

## 两个基准，两种用途（design D16）

| 基准 | 用途 | 数据 |
|---|---|---|
| 行业板块中位数 | 判断「是否被错杀」 | `board_spot` 已有 industry |
| 所属主线代表板块 | 判断「主线是否退潮」 | 复用 `mainline_candidates` |

**背离本身就是信号**：行业稳而主线退潮 → 个股被主线情绪拖累（偏派发）；
行业跌而主线稳 → 个股被行业错杀（偏洗盘）。

## 明确不做全量概念板块

概念板块有**内生性**问题：个股自身涨跌会推高所属概念板块指数，使「相对强度」
失真；且成员动态调整、蹭概念普遍。概念板块的价值在于判断「主线还在不在」，
那是 `mainline_*` 子系统的职责——职责分离，不塞进个股探针。

## 相对换手不可得时为什么整块不降级为「仅自身比较」

自身缩量无法区分「主力锁仓」与「无人问津」，而这个区分正是 D6 的全部意义。
因此板块中位换手缺失时，`turnover_class` 显式标记不可得，且**下游不得**据此
给出缩量侧的洗盘加分（门控在 `washout.py`），置信度同时下调。
"""

from __future__ import annotations

from .context import SignalContext
from .types import SIDE_DISTRIBUTION, SIDE_WASHOUT, Component, Evidence
from .weights import DEFAULT_WEIGHTS, DIM_RELATIVE_STRENGTH, RelativeParams, WashoutWeights
from .volume import _volume_ratio_5d  # 复用同一套「自身缩量」口径，避免两处定义漂移

SOURCE_KEY = "board_spot"
SOURCE_MARKET = "market_index"


def _turnover_class(
    *,
    own_volume_ratio: float | None,
    stock_turnover: float | None,
    board_median_turnover: float | None,
    params: RelativeParams,
) -> tuple[str | None, bool, str | None]:
    """返回 (分类, 是否可得, 不可得原因)。"""
    if stock_turnover is None or board_median_turnover is None:
        return None, False, "board median turnover unavailable; refusing self-relative fallback"
    if own_volume_ratio is None:
        return None, False, "own volume ratio unavailable"
    if board_median_turnover <= 0:
        return None, False, "board median turnover is non-positive"

    own_contracted = own_volume_ratio <= 0.7
    if not own_contracted:
        return "normal_or_elevated", True, None

    ratio = stock_turnover / board_median_turnover
    if ratio >= params.broad_turnover_ratio:
        return "relative_strength_contraction", True, None
    return "broad_contraction", True, None


def _divergence(peer, params: RelativeParams) -> str | None:
    board = peer.board_return_pct if peer is not None else None
    mainline = peer.mainline_return_pct if peer is not None else None
    if board is None or mainline is None:
        return None
    if board >= params.board_stable_pp and mainline <= params.mainline_receding_pp:
        return "board_stable_mainline_receding"
    if board <= params.board_falling_pp and mainline >= params.mainline_stable_pp:
        return "board_falling_mainline_stable"
    return None


def compute(ctx: SignalContext, weights: WashoutWeights = DEFAULT_WEIGHTS) -> Component:
    params: RelativeParams = weights.relative
    weight = weights.weight_for(DIM_RELATIVE_STRENGTH)
    peer = ctx.peer
    as_of = ctx.as_of

    empty_raw = {
        "stock_return_pct": None,
        "board_return_pct": None,
        "market_return_pct": None,
        "mainline_return_pct": None,
        "excess_vs_board_pp": None,
        "excess_vs_market_pp": None,
        "turnover_class": None,
        "turnover_class_available": False,
        "turnover_unavailable_reason": None,
        "divergence": None,
    }
    if peer is None:
        return Component(
            key=DIM_RELATIVE_STRENGTH,
            core=True,
            available=False,
            magnitude=0.0,
            side=None,
            weight=weight,
            raw=empty_raw,
            evidence=(),
            unavailable_reason="peer snapshot unavailable",
        )

    closes = [float(c) for c in ctx.daily["close"].tolist()]
    volumes = [float(v) for v in ctx.daily["volume"].tolist()]
    if len(closes) < 2:
        return Component(
            key=DIM_RELATIVE_STRENGTH,
            core=True,
            available=False,
            magnitude=0.0,
            side=None,
            weight=weight,
            raw=empty_raw,
            evidence=(),
            unavailable_reason="need at least 2 bars",
        )

    stock_return_pct = (closes[-1] - closes[-2]) / closes[-2] * 100.0 if closes[-2] > 0 else None
    board_return_pct = peer.board_return_pct
    market_return_pct = peer.market_return_pct
    mainline_return_pct = peer.mainline_return_pct

    own_volume_ratio = _volume_ratio_5d(volumes)
    turnover_class, turnover_available, turnover_reason = _turnover_class(
        own_volume_ratio=own_volume_ratio,
        stock_turnover=peer.stock_turnover,
        board_median_turnover=peer.board_median_turnover,
        params=params,
    )
    divergence = _divergence(peer, params)

    excess_board = (
        stock_return_pct - board_return_pct
        if stock_return_pct is not None and board_return_pct is not None
        else None
    )
    excess_market = (
        stock_return_pct - market_return_pct
        if stock_return_pct is not None and market_return_pct is not None
        else None
    )

    raw = {
        "stock_return_pct": stock_return_pct,
        "board_return_pct": board_return_pct,
        "market_return_pct": market_return_pct,
        "mainline_return_pct": mainline_return_pct,
        "excess_vs_board_pp": excess_board,
        "excess_vs_market_pp": excess_market,
        "turnover_class": turnover_class,
        "turnover_class_available": turnover_available,
        "turnover_unavailable_reason": turnover_reason,
        "divergence": divergence,
    }

    # 若连相对收益都算不出来，本维度确实不可得
    if excess_board is None and divergence is None:
        return Component(
            key=DIM_RELATIVE_STRENGTH,
            core=True,
            available=False,
            magnitude=0.0,
            side=None,
            weight=weight,
            raw=raw,
            evidence=(),
            unavailable_reason="no comparable board/market return",
        )

    side: str | None = None
    magnitude = 0.0
    used: list[str] = []

    # ① 板块上涨而个股独跌 = alpha 走弱，最危险的形态
    if (
        board_return_pct is not None
        and stock_return_pct is not None
        and board_return_pct > 0
        and stock_return_pct < 0
    ):
        side = SIDE_DISTRIBUTION
        magnitude = min(abs(stock_return_pct) / max(params.full_credit_pp, 1e-9), 1.0)
        magnitude = max(magnitude, 0.5)
        used += ["stock_return_pct", "board_return_pct"]
    elif excess_board is not None and abs(excess_board) >= params.outperformance_pp:
        if excess_board > 0:
            side = SIDE_WASHOUT
        else:
            side = SIDE_DISTRIBUTION
        magnitude = min(abs(excess_board) / max(params.full_credit_pp, 1e-9), 1.0)
        used += ["excess_vs_board_pp", "board_return_pct"]

    # ② 双基准背离：背离是强信号，可覆盖较弱的方向判断
    if divergence == "board_stable_mainline_receding":
        side = SIDE_DISTRIBUTION
        magnitude = max(magnitude, params.divergence_magnitude)
        used += ["mainline_return_pct", "board_return_pct"]
        raw["turnover_class"] = turnover_class
    elif divergence == "board_falling_mainline_stable":
        side = SIDE_WASHOUT
        magnitude = max(magnitude, params.divergence_magnitude)
        used += ["mainline_return_pct", "board_return_pct"]

    evidence: list[Evidence] = []
    for field in dict.fromkeys(used):
        if raw.get(field) is None:
            continue
        unit = "pct" if field.endswith("_pct") or field.endswith("_pp") else "ratio"
        src = SOURCE_KEY if "board" in field or "mainline" in field else SOURCE_MARKET
        evidence.append(Evidence(field=field, value=raw[field], unit=unit, source_key=src, as_of=as_of))
    if turnover_class is not None:
        evidence.append(
            Evidence(field="turnover_class", value=turnover_class, unit="text", source_key=SOURCE_KEY, as_of=as_of)
        )

    return Component(
        key=DIM_RELATIVE_STRENGTH,
        core=True,
        available=True,
        magnitude=float(magnitude),
        side=side,
        weight=weight,
        raw=raw,
        evidence=tuple(evidence),
    )
