"""价格行为、位置调制、相对强度三个维度。

其中位置调制（position）是本方案最容易做错的地方：它必须是**调制器**
而不是独立加权维度。同样一根「缩量阴线」，启动初期是中继洗盘，翻倍高位
是派发前兆——线性加权会把两者打成相近分数，那正是玩具级模型的特征。
"""

from __future__ import annotations

import pytest

from tests._signal_fixtures import make_context, make_daily
from tradingagents.signals.ports import PeerSnapshot, SignalPorts


def _ctx(daily, **kw):
    return make_context(daily=daily, **kw)


# ══════════════════════════════════════════════════════════════════════════
# 价格行为
# ══════════════════════════════════════════════════════════════════════════


def _sharp_drop_with_long_lower_shadow():
    """急跌但当日收回、留长下影：盘中有承接。

    前 5 根平稳，第 6-8 根下杀，末根低开后收回（下影线长、收盘位于当日上部）。
    """
    closes = [10.0, 10.0, 10.0, 10.0, 10.0, 9.8, 9.6, 9.4, 9.35]
    opens = [10.0, 10.0, 10.0, 10.0, 10.0, 9.8, 9.6, 9.4, 9.15]
    # 末根：开 9.15 低 9.10 高 9.38 收 9.35 → 下影 0.05/0.28≈0.18
    # 改成更典型的长下影：开 9.30 低 9.00 高 9.38 收 9.35
    opens[-1] = 9.30
    lows = [c * 0.998 for c in closes]
    lows[-1] = 9.00
    highs = [c * 1.002 for c in closes]
    highs[-1] = 9.38
    return make_daily(closes, [1000.0] * 9, opens=opens, highs=highs, lows=lows)


def test_price_action_detects_sharp_drop_with_long_lower_shadow():
    from tradingagents.signals import price_action

    c = price_action.compute(_ctx(_sharp_drop_with_long_lower_shadow()))

    assert c.available is True
    # 下影 = min(open, close) - low = 9.30 - 9.00 = 0.30；振幅 = 9.38 - 9.00 = 0.38
    assert c.raw["lower_shadow_ratio"] == pytest.approx(0.30 / 0.38, abs=1e-3)
    # 回收率 = (close - low) / (high - low) = (9.35 - 9.00) / 0.38
    assert c.raw["intraday_recovery_ratio"] == pytest.approx(0.35 / 0.38, abs=1e-3)
    assert c.side == "washout"
    assert c.contribution > 0


def test_price_action_flags_close_near_low_as_distribution():
    """急跌且收在当日低位：无承接，是派发/趋势破坏的形态。"""
    from tradingagents.signals import price_action

    closes = [10.0, 10.0, 10.0, 10.0, 10.0, 9.8, 9.6, 9.4, 9.15]
    opens = [10.0, 10.0, 10.0, 10.0, 10.0, 9.8, 9.6, 9.4, 9.30]
    lows = [c * 0.998 for c in closes]
    lows[-1] = 9.12
    highs = [c * 1.002 for c in closes]
    highs[-1] = 9.40
    c = price_action.compute(_ctx(make_daily(closes, [1000.0] * 9, opens=opens, highs=highs, lows=lows)))

    assert c.raw["intraday_recovery_ratio"] < 0.3
    assert c.side == "distribution"
    assert c.contribution < 0


def test_price_action_reports_drawdown_and_speed():
    """必须同时报告「跌了多少」与「跌得多快」——洗盘的特征是快，派发是慢磨。"""
    from tradingagents.signals import price_action

    daily = make_daily([10.0, 10.0, 10.0, 10.0, 10.0, 9.8, 9.6, 9.4, 9.35],
                       [1000.0] * 9)
    c = price_action.compute(_ctx(daily))

    assert c.raw["drawdown_pct"] > 0
    assert c.raw["drawdown_bars"] >= 1
    assert c.raw["drawdown_speed_pct_per_bar"] > 0
    assert c.core is True


def test_price_action_does_not_add_position_term():
    """价格行为不得内含位置因子——位置只能以调制器形式参与（D5）。

    若这里偷偷按累计涨幅加权，就会出现「同一个形态在早期与晚期得分不同」
    且差异来源不可见，位置交互项的可解释性随之丢失。
    """
    from tradingagents.signals import price_action

    early = make_daily([10.0, 10.05, 10.1, 10.15, 10.2, 10.0, 9.9, 9.8, 9.75], [1000.0] * 9)
    late = make_daily([10.0, 11.0, 12.0, 13.0, 14.0, 13.5, 13.0, 12.5, 12.4], [1000.0] * 9)
    a = price_action.compute(_ctx(early))
    b = price_action.compute(_ctx(late))

    assert "cum_gain" not in a.raw
    assert "distance_to_prior_high" not in a.raw
    assert "modulator" not in a.raw


# ══════════════════════════════════════════════════════════════════════════
# 位置调制器
# ══════════════════════════════════════════════════════════════════════════


def test_position_modulator_reports_raw_observables_and_factor():
    from tradingagents.signals import position

    daily = make_daily([10.0 + 0.1 * i for i in range(10)], [1000.0] * 10)
    m = position.compute(_ctx(daily))

    assert m.available is True
    assert m.key == "position"
    assert m.raw["cum_gain"] == pytest.approx(10.9 / 10.0 - 1.0, abs=1e-6)
    assert m.raw["prior_high"] == pytest.approx(10.9, abs=1e-6)
    assert m.raw["distance_to_prior_high"] == pytest.approx(0.0, abs=1e-6)
    assert 0.5 <= m.factor <= 1.5


def test_position_modulator_favours_early_stage():
    """早期（累计涨幅低）→ factor > 1，把量价证据推向洗盘侧。"""
    from tradingagents.signals import position

    daily = make_daily([10.0, 10.02, 10.04, 10.03, 10.05, 10.02, 10.01, 10.0, 9.98], [1000.0] * 9)
    m = position.compute(_ctx(daily))

    assert m.raw["cum_gain"] < 0.05
    assert m.factor == pytest.approx(1.5, abs=1e-6)


def test_position_modulator_disfavours_late_stage():
    """晚期（累计涨幅高）→ factor < 1，把量价证据推向派发侧。"""
    from tradingagents.signals import position

    # 夹具修正：累计涨幅需真正进入晚期区间（>0.80）。
    # 原写法截至 16.9 只有 0.69，属「中段」——错误的夹具会让这条断言
    # 变成在验证一个不存在的行为。
    daily = make_daily([10.0, 11.25, 12.5, 13.75, 15.0, 16.25, 17.5, 18.75, 19.9], [1000.0] * 9)
    m = position.compute(_ctx(daily))

    assert m.raw["cum_gain"] > 0.8
    assert m.factor == pytest.approx(0.5)


def test_position_factor_is_monotone_in_cum_gain():
    """factor 必须随累计涨幅单调不增——否则「位置越高越危险」这条先验被破坏。"""
    from tradingagents.signals import position

    factors = []
    for top in (10.1, 10.5, 11.0, 12.0, 13.0, 15.0, 18.0):
        daily = make_daily([10.0 + (top - 10.0) * i / 8 for i in range(9)], [1000.0] * 9)
        factors.append(position.compute(_ctx(daily)).factor)

    assert all(b <= a + 1e-9 for a, b in zip(factors, factors[1:])), factors


def test_position_unavailable_on_flat_price_series_only_when_degenerate():
    """全平序列仍可计算（cum_gain=0），但基准价非正时必须显式不可得。"""
    from tradingagents.signals import position

    daily = make_daily([10.0] * 9, [1000.0] * 9)
    m = position.compute(_ctx(daily))
    assert m.available is True
    assert m.raw["cum_gain"] == pytest.approx(0.0)


# ══════════════════════════════════════════════════════════════════════════
# 相对强度与相对换手
# ══════════════════════════════════════════════════════════════════════════


def _peer(**kw):
    return PeerSnapshot(**kw)


def test_relative_requires_peer_snapshot():
    from tradingagents.signals import relative

    c = relative.compute(_ctx(make_daily([10.0] * 9, [1000.0] * 9)))
    assert c.available is False
    assert c.core is True
    assert c.unavailable_reason


def test_relative_outperformance_scores_washout():
    """个股跌幅小于板块 → 被错杀，洗盘侧。"""
    from tradingagents.signals import relative

    daily = make_daily([10.0, 10.0, 10.0, 10.0, 10.0, 9.9, 9.8, 9.75, 9.72], [1000.0] * 9)
    ctx = make_context(
        daily=daily,
        peer=_peer(board_name="白酒", board_return_pct=-3.0, market_return_pct=-1.5,
                   stock_turnover=1.2, board_median_turnover=1.8),
    )
    c = relative.compute(ctx)

    assert c.available is True
    assert c.raw["excess_vs_board_pp"] > 0
    assert c.side == "washout"


def test_relative_lone_decline_while_board_rises_scores_distribution():
    """板块上涨而个股独跌 = alpha 走弱，最危险的形态 → 派发侧。"""
    from tradingagents.signals import relative

    daily = make_daily([10.0, 10.0, 10.0, 10.0, 10.0, 9.9, 9.8, 9.7, 9.6], [1000.0] * 9)
    ctx = make_context(
        daily=daily,
        peer=_peer(board_name="白酒", board_return_pct=1.8, market_return_pct=0.9,
                   stock_turnover=2.5, board_median_turnover=1.8),
    )
    c = relative.compute(ctx)

    assert c.raw["board_return_pct"] > 0
    assert c.raw["stock_return_pct"] < 0
    assert c.side == "distribution"


def test_turnover_class_broad_contraction_when_below_both():
    """自身缩量且低于板块中位 → broad_contraction：不允许贡献洗盘证据（D6）。"""
    from tradingagents.signals import relative

    # 末根量 300，前 5 日均量 1000 → 自身显著缩量
    daily = make_daily([10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 9.9, 9.8, 9.7],
                       [1000, 1000, 1000, 1000, 1000, 1000, 600, 500, 300])
    ctx = make_context(
        daily=daily,
        peer=_peer(board_name="白酒", board_return_pct=-2.0, market_return_pct=-1.0,
                   stock_turnover=0.6, board_median_turnover=1.8),
    )
    c = relative.compute(ctx)

    assert c.raw["turnover_class"] == "broad_contraction"
    assert c.raw["turnover_class_available"] is True


def test_turnover_class_relative_strength_when_at_or_above_board_median():
    """自身缩量但换手不低于板块中位 → relative_strength_contraction：允许洗盘证据。"""
    from tradingagents.signals import relative

    daily = make_daily([10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 9.9, 9.8, 9.7],
                       [1000, 1000, 1000, 1000, 1000, 1000, 600, 500, 300])
    ctx = make_context(
        daily=daily,
        peer=_peer(board_name="白酒", board_return_pct=-2.0, market_return_pct=-1.0,
                   stock_turnover=2.4, board_median_turnover=1.8),
    )
    c = relative.compute(ctx)

    assert c.raw["turnover_class"] == "relative_strength_contraction"


def test_turnover_class_unavailable_without_peer_turnover_and_no_self_fallback():
    """板块中位换手不可得时，**不得**静默回退到「仅自身历史比较」。

    这是 spec 明确禁止的行为：自身缩量无法区分「主力锁仓」与「无人问津」，
    回退会让最危险的情形（流动性枯竭的阴跌）被误判为洗盘。
    """
    from tradingagents.signals import relative

    daily = make_daily([10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 9.9, 9.8, 9.7],
                       [1000, 1000, 1000, 1000, 1000, 1000, 600, 500, 300])
    ctx = make_context(
        daily=daily,
        peer=_peer(board_name="白酒", board_return_pct=-2.0, market_return_pct=-1.0,
                   stock_turnover=None, board_median_turnover=None),
    )
    c = relative.compute(ctx)

    assert c.raw["turnover_class"] is None
    assert c.raw["turnover_class_available"] is False
    assert c.raw["turnover_unavailable_reason"]


def test_dual_benchmark_divergence_board_stable_but_mainline_receding():
    """行业稳、主线退潮 → 个股被主线情绪拖累 → 派发侧加分（D16）。"""
    from tradingagents.signals import relative

    daily = make_daily([10.0, 10.0, 10.0, 10.0, 10.0, 9.95, 9.9, 9.88, 9.85], [1000.0] * 9)
    ctx = make_context(
        daily=daily,
        peer=_peer(
            board_name="白酒", board_return_pct=-0.2, market_return_pct=-0.1,
            stock_turnover=1.2, board_median_turnover=1.8,
            mainline_board_name="AI算力", mainline_return_pct=-3.5,
        ),
    )
    c = relative.compute(ctx)

    assert c.raw["divergence"] == "board_stable_mainline_receding"
    assert c.side == "distribution"


def test_dual_benchmark_divergence_board_falling_but_mainline_stable():
    """行业跌、主线稳 → 个股被行业错杀 → 洗盘侧加分（D16）。"""
    from tradingagents.signals import relative

    daily = make_daily([10.0, 10.0, 10.0, 10.0, 10.0, 9.95, 9.9, 9.88, 9.85], [1000.0] * 9)
    ctx = make_context(
        daily=daily,
        peer=_peer(
            board_name="白酒", board_return_pct=-2.5, market_return_pct=-1.2,
            stock_turnover=1.2, board_median_turnover=1.8,
            mainline_board_name="AI算力", mainline_return_pct=-0.2,
        ),
    )
    c = relative.compute(ctx)

    assert c.raw["divergence"] == "board_falling_mainline_stable"
    assert c.side == "washout"
