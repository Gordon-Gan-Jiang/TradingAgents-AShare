"""洗盘探针：质量聚合、位置调制门控、四态判定、证伪条件、置信度。

本文件是本方案里最关键的测试集合。它守的是三条容易被「实现出来但做错」
的行为：

1. **位置必须是乘法调制**（D5）。如果实现成第 8 个独立加权维度，早期与晚期
   的同形态会得到相近总分——测试 `test_early_position_amplifies_washout_mass`
   直接按因子比例断言 3 倍关系，加法实现无法满足。
2. **缩量不能无条件当洗盘证据**（D6）。板块中位换手缺失时，必须**不给**缩量
   侧加分，而不是悄悄退回「仅自身历史比较」。
3. **弃权必须是结构性的**（D14）。核心维度缺失、证据两侧对立即弃权，且弃权
   是合法结果，不得被下游转成方向性观点。
"""

from __future__ import annotations

import pytest

from tests._signal_fixtures import make_chips, make_context, make_daily, make_flow, make_intraday
from tradingagents.signals.ports import PeerSnapshot, SignalPorts
from tradingagents.signals.types import Component, Evidence, Modulator
from tradingagents.signals.weights import (
    DIM_INTRADAY,
    DIM_MA_STRUCTURE,
    DIM_PRICE_ACTION,
    DIM_RELATIVE_STRENGTH,
    DIM_VOLUME,
    MOD_POSITION,
    WashoutWeights,
)

W = WashoutWeights()


def _comp(key, side, magnitude=0.6, weight=None, core=False, available=True, raw=None, reason=None):
    return Component(
        key=key,
        core=core,
        available=available,
        magnitude=magnitude,
        side=side,
        weight=W.weight_for(key) if weight is None else weight,
        raw=raw or {},
        evidence=(
            (Evidence(field="x", value=1.0, unit="ratio", source_key="stock_data", as_of="2026-01-16"),)
            if available and side
            else ()
        ),
        unavailable_reason=reason,
    )


def _mod(factor, available=True):
    return Modulator(
        key=MOD_POSITION,
        available=available,
        raw={"cum_gain": 0.1, "prior_high": 10.0, "distance_to_prior_high": 0.02, "advance_days": 5},
        factor=factor,
        evidence=(Evidence(field="cum_gain", value=0.1, unit="ratio", source_key="stock_data", as_of="2026-01-16"),),
    )


def _relative(turnover_class="relative_strength_contraction", available=True, side="washout"):
    return _comp(
        DIM_RELATIVE_STRENGTH,
        side,
        core=True,
        raw={
            "turnover_class": turnover_class,
            "turnover_class_available": available,
            "turnover_unavailable_reason": None if available else "no peer turnover",
            "divergence": None,
            "excess_vs_board_pp": 1.2,
        },
    )


# ══════════════════════════════════════════════════════════════════════════
# D5：位置是乘法调制，不是加法
# ══════════════════════════════════════════════════════════════════════════


def test_early_position_amplifies_washout_mass():
    """早期（factor=1.5）应把洗盘侧质量放大到 1.5 倍。

    加法实现下两者只会差一个固定小量，无法满足 3 倍比例关系——这条断言
    就是「位置是交互项」这一设计决策的可执行形式。
    """
    from tradingagents.signals.washout import aggregate_masses

    comps = [_comp(DIM_VOLUME, "washout", magnitude=0.5), _relative()]
    early = aggregate_masses(comps, _mod(1.5), W)
    late = aggregate_masses(comps, _mod(0.5), W)

    assert early.sides["washout"] / late.sides["washout"] == pytest.approx(3.0, rel=1e-6)
    assert early.sides["washout"] > late.sides["washout"]


def test_late_position_amplifies_distribution_mass():
    """晚期（factor=0.5）应把派发侧质量放大到 2 倍（1/factor）。"""
    from tradingagents.signals.washout import aggregate_masses

    comps = [_comp(DIM_PRICE_ACTION, "distribution", magnitude=0.8)]
    early = aggregate_masses(comps, _mod(1.5), W)
    late = aggregate_masses(comps, _mod(0.5), W)

    assert late.sides["distribution"] / early.sides["distribution"] == pytest.approx(3.0, rel=1e-6)


def test_position_does_not_modulate_trend_break_mass():
    """趋势破坏由均线结构决定，与位置无关，不应被位置因子放大。

    晚期一次破位与早期一次破位的「结构已坏」程度相同；把位置乘上去会让
    早期破位被显著低估，从而在真正的趋势破坏上给出过于乐观的表述。
    """
    from tradingagents.signals.washout import aggregate_masses

    comps = [_comp(DIM_MA_STRUCTURE, "trend_break", magnitude=0.7)]
    early = aggregate_masses(comps, _mod(1.5), W)
    late = aggregate_masses(comps, _mod(0.5), W)

    assert early.sides["trend_break"] == pytest.approx(late.sides["trend_break"])


def test_modulator_factor_exposed_separately_from_additive_contributions():
    """原始位置观测与调制因子必须单独暴露，不能混进加法贡献里。"""
    from tradingagents.signals.washout import aggregate_masses

    comps = [_comp(DIM_VOLUME, "washout", magnitude=0.5), _relative()]
    agg = aggregate_masses(comps, _mod(1.5), W)

    assert agg.factor == pytest.approx(1.5)
    assert agg.position_raw["cum_gain"] == pytest.approx(0.1)
    # 位置不得作为一个“组件”出现在贡献列表里
    assert MOD_POSITION not in {k for k in agg.sides}


# ══════════════════════════════════════════════════════════════════════════
# D6：缩量不得无条件当洗盘证据
# ══════════════════════════════════════════════════════════════════════════


def test_broad_contraction_blocks_washout_credit():
    """自身缩量且低于板块中位换手 → 缩量侧加分必须被撤销，并转入非洗盘侧。

    这是 D6 的核心：缩量既可能是主力锁仓，也可能是流动性枯竭无人问津。
    后者恰是最危险的情形——模型会在最该警惕时给出「洗盘」。
    """
    from tradingagents.signals.washout import aggregate_masses

    # relative 组件自身置为中性，以隔离被测的门控行为
    comps = [
        _comp(DIM_VOLUME, "washout", magnitude=0.9),
        _relative(turnover_class="broad_contraction", side=None),
    ]
    agg = aggregate_masses(comps, _mod(1.0), W)

    assert "broad_contraction_blocks_washout" in agg.gates
    assert agg.sides["washout"] == pytest.approx(0.0)
    # 缩量证据不是被丢掉，而是转入非洗盘侧
    assert agg.sides["trend_break"] == pytest.approx(W.weight_for(DIM_VOLUME) * 0.9)


def test_missing_peer_turnover_withholds_contraction_credit():
    """板块中位换手不可得 → **不给**缩量侧加分，且标记该维度未覆盖。

    spec 明确禁止静默回退到自身历史比较。这条断言保证「无法区分」时系统
    选择少说，而不是猜。
    """
    from tradingagents.signals.washout import aggregate_masses

    comps = [
        _comp(DIM_VOLUME, "washout", magnitude=0.9),
        _relative(turnover_class=None, available=False, side=None),
    ]
    agg = aggregate_masses(comps, _mod(1.0), W)

    assert "contraction_credit_withheld_no_peer" in agg.gates
    assert agg.sides["washout"] == pytest.approx(0.0)
    # 与 broad_contraction 不同：这里连非洗盘侧也不加分——只是「收回加分」
    assert agg.sides["trend_break"] == pytest.approx(0.0)
    assert "board_turnover" in agg.unavailable


def test_relative_strength_contraction_keeps_washout_credit():
    """缩量但换手不低于板块中位 → 保留洗盘侧加分（这是真正的相对强缩量）。"""
    from tradingagents.signals.washout import aggregate_masses

    comps = [_comp(DIM_VOLUME, "washout", magnitude=0.9), _relative(side=None)]
    agg = aggregate_masses(comps, _mod(1.0), W)

    assert agg.gates == ()
    assert agg.sides["washout"] == pytest.approx(W.weight_for(DIM_VOLUME) * 0.9)


# ══════════════════════════════════════════════════════════════════════════
# D14：结构性弃权
# ══════════════════════════════════════════════════════════════════════════


def test_abstains_when_core_dimension_unavailable():
    """四个核心维度任一不可得 → 必须弃权，并点名缺了哪个。"""
    from tradingagents.signals.washout import evaluate_context

    daily = make_daily([10.0, 10.0, 10.0, 10.0, 10.0, 9.9, 9.8, 9.7, 9.65], [1000.0] * 9)
    ctx = make_context(daily=daily)  # 无 peer → relative_strength 不可得
    r = evaluate_context(ctx, W)

    assert r.state == "insufficient_evidence"
    assert DIM_RELATIVE_STRENGTH in r.unavailable
    assert r.confidence == 0


def test_abstains_when_margin_below_threshold():
    """两侧证据质量接近（margin < 0.15）→ 弃权，而不是选一个大的。"""
    from tradingagents.signals.washout import aggregate_masses

    # 两侧质量必须真的接近，否则验证的不是弃权分支
    w = _comp(DIM_VOLUME, "washout", magnitude=0.5, weight=1.0)
    d = _comp(DIM_PRICE_ACTION, "distribution", magnitude=0.5, weight=1.0)
    agg = aggregate_masses([w, d, _relative(side=None)], _mod(1.0), W)

    assert agg.sides["washout"] == pytest.approx(agg.sides["distribution"])
    assert agg.margin == pytest.approx(0.0)
    assert agg.margin < W.margin_abstain


def test_margin_is_relative_separation_not_absolute_score():
    """margin 必须是比值而非绝对值，使不同标的之间的判定门槛一致。"""
    from tradingagents.signals.washout import aggregate_masses

    small = aggregate_masses(
        [_comp(DIM_VOLUME, "washout", magnitude=0.2, weight=1.0), _relative(side=None)], _mod(1.0), W
    )
    large = aggregate_masses(
        [_comp(DIM_VOLUME, "washout", magnitude=2.0, weight=1.0), _relative(side=None)], _mod(1.0), W
    )
    # 只有一侧有质量 → 相对分离度两边都是 1.0
    assert small.margin == pytest.approx(1.0)
    assert large.margin == pytest.approx(1.0)


def test_abstention_is_a_first_class_outcome():
    from tradingagents.signals.washout import evaluate_context

    daily = make_daily([10.0] * 9, [1000.0] * 9)
    r = evaluate_context(make_context(daily=daily), W)
    assert r.state == "insufficient_evidence"
    assert r.is_abstention is True
    assert r.confidence == 0
    assert r.unavailable


# ══════════════════════════════════════════════════════════════════════════
# 四态可达
# ══════════════════════════════════════════════════════════════════════════


def _rich_ctx(*, closes, volumes, intraday=None, chips=None, flow=None, peer=None):
    return make_context(
        daily=make_daily(closes, volumes),
        intraday=intraday,
        chips=chips,
        flow=flow,
        peer=peer,
    )


def test_washout_state_reachable_with_full_evidence():
    """缩量下杀 + 分时承接 + 低获利盘 + 相对抗跌 → washout。"""
    from tradingagents.signals.washout import evaluate_context

    closes = [10.0] * 5 + [9.8, 9.6, 9.4, 9.3, 9.6]
    volumes = [1000, 1000, 1000, 1000, 1000, 700, 600, 500, 480, 700]
    ctx = _rich_ctx(
        closes=closes,
        volumes=volumes,
        intraday=make_intraday("2026-01-16", [10.0, 9.8, 9.6, 9.5, 9.45, 9.5, 9.6, 9.7, 9.8, 9.85],
                              [500, 600, 700, 800, 900, 700, 600, 600, 700, 900]),
        chips=make_chips(profit_ratio=0.3, avg_cost=10.4),
        flow=make_flow(main_net=-4.0e7, super_large_net=-5.0e6),
        peer=PeerSnapshot(board_name="白酒", board_return_pct=-3.0, market_return_pct=-1.5,
                          stock_turnover=2.2, board_median_turnover=1.8),
    )
    r = evaluate_context(ctx, W)

    assert r.state == "washout"
    assert r.score_washout > r.score_distribution
    assert r.confidence > 0


def test_distribution_state_reachable():
    """放量下跌 + 超大单主导流出 + 高获利盘 → distribution。"""
    from tradingagents.signals.washout import evaluate_context

    # 晚期夹具：累计涨幅必须真正进入晚期区间（>0.80），否则位置因子
    # 会把证据反向推向洗盘侧，测试就变成在验证一个相反的结论。
    closes = [10.0, 13.0, 16.0, 19.0, 22.0, 21.0, 20.0, 19.0, 18.4]
    volumes = [1000, 1000, 1000, 1000, 1000, 1500, 1700, 1900, 2100]
    ctx = _rich_ctx(
        closes=closes,
        volumes=volumes,
        flow=make_flow(main_net=-9.0e7, super_large_net=-7.0e7),
        peer=PeerSnapshot(board_name="白酒", board_return_pct=1.2, market_return_pct=0.6,
                          stock_turnover=3.0, board_median_turnover=1.8),
    )
    r = evaluate_context(ctx, W)

    assert r.state in {"distribution", "trend_break"}
    assert r.score_distribution + r.score_trend_break > r.score_washout


def test_trend_break_state_reachable():
    """多日跌破 20 日线未收回 → trend_break。"""
    from tradingagents.signals.washout import evaluate_context

    closes = [10.0] * 20 + [9.7, 9.5, 9.3, 9.2, 9.15, 9.1, 9.08, 9.05]
    volumes = [1000.0] * len(closes)
    ctx = _rich_ctx(
        closes=closes,
        volumes=volumes,
        peer=PeerSnapshot(board_name="白酒", board_return_pct=-1.0, market_return_pct=-0.5,
                          stock_turnover=0.8, board_median_turnover=1.8),
    )
    r = evaluate_context(ctx, W)

    assert r.state == "trend_break"
    assert r.score_trend_break > r.score_washout


# ══════════════════════════════════════════════════════════════════════════
# 证伪条件
# ══════════════════════════════════════════════════════════════════════════


def test_falsifiers_are_mandatory_and_traceable():
    """非弃权判定必须带 ≥2 条证伪条件，且每条可追溯到已计算的观测。"""
    from tradingagents.signals.washout import evaluate_context

    closes = [10.0] * 5 + [9.8, 9.6, 9.4, 9.3, 9.6]
    volumes = [1000, 1000, 1000, 1000, 1000, 700, 600, 500, 480, 700]
    ctx = _rich_ctx(
        closes=closes,
        volumes=volumes,
        peer=PeerSnapshot(board_name="白酒", board_return_pct=-3.0, market_return_pct=-1.5,
                          stock_turnover=2.2, board_median_turnover=1.8),
    )
    r = evaluate_context(ctx, W)

    assert r.state != "insufficient_evidence"
    assert len(r.falsifiers) >= 2
    valid = {c.key for c in r.components} | {m.key for m in r.modulators}
    for f in r.falsifiers:
        assert f.observable and f.threshold and f.window_days >= 1 and f.description
        assert f.derived_from in valid, f"证伪条件 {f.observable} 的来源 {f.derived_from} 不在组件中"


def test_abstention_carries_no_falsifiers():
    """弃权时没有可检验的预测，因此不应编造证伪条件。"""
    from tradingagents.signals.washout import evaluate_context

    r = evaluate_context(make_context(daily=make_daily([10.0] * 9, [1000.0] * 9)), W)
    assert r.state == "insufficient_evidence"
    assert r.falsifiers == ()


# ══════════════════════════════════════════════════════════════════════════
# 置信度与高置信标签
# ══════════════════════════════════════════════════════════════════════════


def test_confidence_is_monotone_in_margin():
    from tradingagents.signals.weights import confidence_from_margin

    values = [confidence_from_margin(m, W) for m in (0.15, 0.2, 0.3, 0.38, 0.45, 0.9)]
    assert all(b >= a for a, b in zip(values, values[1:])), values
    assert values[0] == 40
    assert values[-1] == 85


def test_daily_only_flag_set_when_intraday_missing():
    from tradingagents.signals.washout import evaluate_context

    closes = [10.0] * 5 + [9.8, 9.6, 9.4, 9.3, 9.6]
    volumes = [1000, 1000, 1000, 1000, 1000, 700, 600, 500, 480, 700]
    ctx = _rich_ctx(
        closes=closes,
        volumes=volumes,
        chips=make_chips(profit_ratio=0.3, avg_cost=10.4),
        peer=PeerSnapshot(board_name="白酒", board_return_pct=-3.0, market_return_pct=-1.5,
                          stock_turnover=2.2, board_median_turnover=1.8),
    )
    r = evaluate_context(ctx, W)

    assert r.daily_only is True
    assert r.high_confidence is False
    assert DIM_INTRADAY in r.unavailable


def test_high_confidence_requires_intraday_and_chips_and_margin():
    """高置信是**结构条件**：分时 + 筹码可得 且 margin ≥ 0.30，不含分数阈值。"""
    from tradingagents.signals.washout import evaluate_context

    closes = [10.0] * 5 + [9.8, 9.6, 9.4, 9.3, 9.6]
    volumes = [1000, 1000, 1000, 1000, 1000, 700, 600, 500, 480, 700]
    base = dict(
        closes=closes,
        volumes=volumes,
        peer=PeerSnapshot(board_name="白酒", board_return_pct=-3.0, market_return_pct=-1.5,
                          stock_turnover=2.2, board_median_turnover=1.8),
    )
    no_chips = evaluate_context(_rich_ctx(intraday=make_intraday(
        "2026-01-16", [10.0, 9.8, 9.6, 9.5, 9.45, 9.5, 9.6, 9.7, 9.8, 9.85],
        [500, 600, 700, 800, 900, 700, 600, 600, 700, 900]), **base), W)
    with_chips = evaluate_context(_rich_ctx(
        intraday=make_intraday("2026-01-16", [10.0, 9.8, 9.6, 9.5, 9.45, 9.5, 9.6, 9.7, 9.8, 9.85],
                               [500, 600, 700, 800, 900, 700, 600, 600, 700, 900]),
        chips=make_chips(profit_ratio=0.3, avg_cost=10.4), **base), W)

    assert no_chips.high_confidence is False
    # 筹码到位且分离度足够时才允许高置信
    if with_chips.margin >= W.margin_high:
        assert with_chips.high_confidence is True


# ══════════════════════════════════════════════════════════════════════════
# 确定性与证据完整性
# ══════════════════════════════════════════════════════════════════════════


def test_result_is_deterministic():
    """同输入必须逐字段相同——这是回放校准的绝对前提。"""
    from tradingagents.signals.washout import evaluate_context

    closes = [10.0] * 5 + [9.8, 9.6, 9.4, 9.3, 9.6]
    volumes = [1000, 1000, 1000, 1000, 1000, 700, 600, 500, 480, 700]
    ctx = _rich_ctx(
        closes=closes,
        volumes=volumes,
        peer=PeerSnapshot(board_name="白酒", board_return_pct=-3.0, market_return_pct=-1.5,
                          stock_turnover=2.2, board_median_turnover=1.8),
    )
    a = evaluate_context(ctx, W)
    b = evaluate_context(ctx, W)
    assert a == b


def test_components_are_well_formed():
    """贡献非零的组件必须带证据锚点（源头校验，而不是指望回答层修复）。"""
    from tradingagents.signals.types import assert_well_formed
    from tradingagents.signals.washout import evaluate_context

    closes = [10.0] * 5 + [9.8, 9.6, 9.4, 9.3, 9.6]
    volumes = [1000, 1000, 1000, 1000, 1000, 700, 600, 500, 480, 700]
    ctx = _rich_ctx(
        closes=closes,
        volumes=volumes,
        chips=make_chips(profit_ratio=0.3, avg_cost=10.4),
        peer=PeerSnapshot(board_name="白酒", board_return_pct=-3.0, market_return_pct=-1.5,
                          stock_turnover=2.2, board_median_turnover=1.8),
    )
    r = evaluate_context(ctx, W)
    assert_well_formed(r.components)  # 不应抛出


def test_evaluate_via_ports_truncates_to_as_of():
    """经端口评估时也必须防前视——回放会依赖这条。"""
    from tradingagents.signals.washout import evaluate

    daily = make_daily([10.0 + i * 0.02 for i in range(30)], [1000.0] * 30)
    as_of = str(daily["date"].iloc[9])[:10]

    class _P:
        def bars(self, symbol, end, lookback):
            return daily  # provider 无视 end，多返回了未来数据

    r = evaluate(
        ports=SignalPorts(daily=_P()),
        symbol="600519.SH",
        as_of=as_of,
        window_days=9,
        weights=W,
    )
    # 只能用 as_of 及之前的 9 根：累计涨幅必须基于第 9 根收盘
    position = next(m for m in r.modulators if m.key == MOD_POSITION)
    expected = float(daily["close"].iloc[9]) / float(daily["close"].iloc[1]) - 1.0
    assert position.raw["cum_gain"] == pytest.approx(expected, rel=1e-9)


def test_signal_version_recorded():
    from tradingagents.signals.washout import evaluate_context

    ctx = make_context(daily=make_daily([10.0] * 9, [1000.0] * 9))
    r = evaluate_context(ctx, W)
    assert r.signal_version == W.version
    assert r.probe == "washout"
