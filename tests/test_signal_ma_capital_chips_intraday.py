"""均线结构、资金构成、筹码分布、分时承接四个维度。

分时承接是「洗盘最直接的证据」——判据是**下跌过程中有没有人接**，
而不是「跌得少」。这也是为什么它被列为高置信的硬前置：缺分时数据时
只能给日线近似，置信度必须封顶。
"""

from __future__ import annotations

import pytest

from tests._signal_fixtures import make_chips, make_context, make_daily, make_flow, make_intraday


def _flat(n=20, price=10.0, vol=1000.0):
    return make_daily([price] * n, [vol] * n)


# ══════════════════════════════════════════════════════════════════════════
# 均线结构
# ══════════════════════════════════════════════════════════════════════════


def test_ma_structure_holding_ma20_scores_washout():
    """价格守住 20 日线 → 结构未坏，符合「中继洗盘」而非趋势破坏。"""
    from tradingagents.signals import ma_structure

    closes = [10.0] * 19 + [10.3]
    c = ma_structure.compute(make_context(daily=make_daily(closes, [1000.0] * 20)))

    assert c.available is True
    assert c.raw["above_ma20"] is True
    assert c.raw["below_ma20_days"] == 0
    assert c.side == "washout"


def test_ma_structure_persistent_break_is_trend_break_not_distribution():
    """有效跌破 20 日线且多日未收回 → **趋势破坏**，不是派发。

    二者在交易含义上不同：派发是主力主动出货（可能仍有余波），趋势破坏是
    结构已坏（任何持有理由都需重审）。合并会让行动建议失真。

    夹具说明：必须有**多于 20 根** bar。MA20 在序列长度不足 20 时无定义，
    只用 20 根 bar 的话「连续 N 日低于 MA20」里 N 最多只能到 1，无法表达
    「多日未收回」这个语义——这时测试会退化成验证一个不存在的行为。
    """
    from tradingagents.signals import ma_structure

    closes = [10.0] * 20 + [9.7, 9.5, 9.3, 9.2, 9.15, 9.1, 9.08, 9.05]
    c = ma_structure.compute(make_context(daily=make_daily(closes, [1000.0] * len(closes))))

    assert c.raw["above_ma20"] is False
    assert c.raw["below_ma20_days"] >= 3
    assert c.side == "trend_break"
    assert c.weighted_magnitude > 0


def test_ma_structure_brief_break_is_neutral_not_trend_break():
    """刚破位 1-2 日 → 中性：洗盘的典型形态本就是「破了再收回」。"""
    from tradingagents.signals import ma_structure

    closes = [10.0] * 14 + [9.9, 9.85, 9.8, 9.75, 9.7, 9.68]
    c = ma_structure.compute(make_context(daily=make_daily(closes, [1000.0] * 20)))

    assert c.raw["above_ma20"] is False
    assert c.raw["below_ma20_days"] < 3
    assert c.side is None


def test_ma_structure_reclaiming_ma5_after_break_boosts_washout():
    """破位后收回 5 日线 → 承接确认，洗盘侧强度提高。"""
    from tradingagents.signals import ma_structure

    closes = [10.0] * 14 + [9.7, 9.6, 9.55, 9.5, 9.9, 10.05]
    c = ma_structure.compute(make_context(daily=make_daily(closes, [1000.0] * 20)))

    assert c.raw["reclaimed_ma5"] is True
    assert c.side == "washout"


def test_ma_structure_reports_all_observables_and_is_not_core():
    """均线结构不是四个核心维度之一：缺它可降置信，但不该直接弃权。"""
    from tradingagents.signals import ma_structure

    c = ma_structure.compute(make_context(daily=_flat()))
    for key in ("ma5", "ma10", "ma20", "above_ma20", "below_ma20_days", "reclaimed_ma5", "ma5_gt_ma10"):
        assert key in c.raw
    assert c.core is False


# ══════════════════════════════════════════════════════════════════════════
# 资金构成
# ══════════════════════════════════════════════════════════════════════════


def test_capital_unavailable_without_flow_data():
    from tradingagents.signals import capital

    c = capital.compute(make_context(daily=_flat()))
    assert c.available is False
    assert c.contribution == 0.0
    assert c.unavailable_reason


def test_capital_institutional_outflow_is_distribution():
    """主力净流出且**超大单主导** → 机构主动出货，派发侧。"""
    from tradingagents.signals import capital

    ctx = make_context(
        daily=_flat(),
        flow=make_flow(main_net=-8.0e7, super_large_net=-6.0e7, large_net=-2.0e7),
    )
    c = capital.compute(ctx)

    assert c.raw["main_net_inflow"] == pytest.approx(-8.0e7)
    assert c.raw["super_large_share_of_outflow"] == pytest.approx(0.75)
    assert c.side == "distribution"


def test_capital_retail_driven_outflow_without_institutional_dominance_is_washout():
    """净流出但超大单占比低 → 更像散户恐慌盘，主力并未主动出货 → 洗盘侧。

    这正是提示词里那条「主力净流出 + 急跌 + 缩量 = 洗盘信号」的可计算化：
    光看「净流出」会把两种完全不同的资金结构混为一谈，必须看**构成**。
    """
    from tradingagents.signals import capital

    ctx = make_context(
        daily=_flat(),
        flow=make_flow(main_net=-8.0e7, super_large_net=-1.0e7, large_net=-3.0e7),
    )
    c = capital.compute(ctx)

    assert c.raw["super_large_share_of_outflow"] == pytest.approx(0.125)
    assert c.side == "washout"


def test_capital_inflow_on_decline_is_absorption():
    """下跌过程中主力净流入 = 明确的承接。"""
    from tradingagents.signals import capital

    ctx = make_context(daily=_flat(), flow=make_flow(main_net=5.0e7, super_large_net=4.0e7))
    c = capital.compute(ctx)

    assert c.side == "washout"


def test_capital_immaterial_flow_is_neutral():
    from tradingagents.signals import capital

    ctx = make_context(daily=_flat(), flow=make_flow(main_net=-1.0e6, super_large_net=-0.5e6))
    c = capital.compute(ctx)
    assert c.side is None
    assert c.contribution == 0.0


# ══════════════════════════════════════════════════════════════════════════
# 筹码分布
# ══════════════════════════════════════════════════════════════════════════


def test_chips_unavailable_without_data():
    from tradingagents.signals import chips

    c = chips.compute(make_context(daily=_flat()))
    assert c.available is False
    assert c.raw["profit_ratio"] is None
    assert c.unavailable_reason


def test_chips_low_profit_ratio_means_pressure_released():
    """获利盘比例低 → 多数持仓浮亏，抛压释放较充分 → 洗盘侧。"""
    from tradingagents.signals import chips

    ctx = make_context(daily=_flat(price=10.0), chips=make_chips(profit_ratio=0.25, avg_cost=10.8))
    c = chips.compute(ctx)

    assert c.available is True
    assert c.raw["profit_ratio"] == pytest.approx(0.25)
    assert c.raw["price_vs_avg_cost"] < 0
    assert c.side == "washout"


def test_chips_crowded_profit_is_distribution():
    """获利盘高度拥挤（>85%）→ 上方随时可能兑现 → 派发侧。"""
    from tradingagents.signals import chips

    ctx = make_context(daily=_flat(price=10.0), chips=make_chips(profit_ratio=0.92, avg_cost=8.0))
    c = chips.compute(ctx)

    assert c.side == "distribution"


def test_chips_mid_range_is_neutral():
    from tradingagents.signals import chips

    ctx = make_context(daily=_flat(price=10.0), chips=make_chips(profit_ratio=0.65, avg_cost=9.5))
    c = chips.compute(ctx)
    assert c.side is None


def test_chips_missing_profit_ratio_is_unavailable_not_neutral():
    """只有成本没有获利比例 → 不可得，不能当「无异常」放过去。"""
    from tradingagents.signals import chips

    ctx = make_context(daily=_flat(), chips=make_chips(profit_ratio=None, avg_cost=10.0))
    c = chips.compute(ctx)
    assert c.available is False
    assert c.unavailable_reason


# ══════════════════════════════════════════════════════════════════════════
# 分时承接
# ══════════════════════════════════════════════════════════════════════════


def test_intraday_unavailable_without_bars_and_flags_daily_only():
    """缺分时 → 不可得。这是高置信标签的硬前置（design D8）。"""
    from tradingagents.signals import intraday

    c = intraday.compute(make_context(daily=_flat()))
    assert c.available is False
    assert c.contribution == 0.0
    assert "intraday" in (c.unavailable_reason or "")


def test_intraday_recovery_with_tail_strength_is_washout():
    """日内砸坑后尾盘拉回 → 明确的盘中有承接。"""
    from tradingagents.signals import intraday

    # 10:00 砸到 9.6，尾盘拉回 10.05，日内高点 10.1
    prices = [10.0, 9.9, 9.8, 9.7, 9.6, 9.65, 9.7, 9.8, 9.9, 10.05]
    volumes = [500, 600, 700, 800, 900, 700, 600, 600, 700, 900]
    ctx = make_context(daily=_flat(), intraday=make_intraday("2026-01-16", prices, volumes))
    c = intraday.compute(ctx)

    assert c.available is True
    assert c.raw["day_recovery_ratio"] > 0.8
    assert c.raw["tail_return_pct"] > 0
    assert c.side == "washout"
    assert c.contribution > 0


def test_intraday_close_near_low_with_weak_tail_is_distribution():
    """尾盘继续走弱、收在日内低位 → 无承接。"""
    from tradingagents.signals import intraday

    prices = [10.0, 9.95, 9.9, 9.85, 9.8, 9.75, 9.7, 9.65, 9.6, 9.55]
    volumes = [500] * 10
    ctx = make_context(daily=_flat(), intraday=make_intraday("2026-01-16", prices, volumes))
    c = intraday.compute(ctx)

    assert c.raw["day_recovery_ratio"] <= 0.3
    assert c.raw["tail_return_pct"] < 0
    assert c.side == "distribution"


def test_intraday_reports_vwap_and_volume_distribution():
    """必须报告均价（VWAP）与成交量分布——回答层要能引用它们。"""
    from tradingagents.signals import intraday

    prices = [10.0, 9.9, 9.8, 9.7, 9.6, 9.65, 9.7, 9.8, 9.9, 10.05]
    volumes = [500, 600, 700, 800, 900, 700, 600, 600, 700, 900]
    ctx = make_context(daily=_flat(), intraday=make_intraday("2026-01-16", prices, volumes))
    c = intraday.compute(ctx)

    for key in ("day_high", "day_low", "day_recovery_ratio", "tail_return_pct", "vwap", "close_vs_vwap_pct"):
        assert key in c.raw
    assert c.raw["day_high"] == pytest.approx(10.05)
    assert c.raw["day_low"] == pytest.approx(9.6)
    assert c.raw["vwap"] == pytest.approx(
        sum(p * v for p, v in zip(prices, volumes)) / sum(volumes), abs=1e-6
    )


def test_intraday_evidence_uses_intraday_source_key():
    """证据来源必须标明是分时数据，否则回答层无法解释「承接」的依据。"""
    from tradingagents.signals import intraday

    prices = [10.0, 9.9, 9.8, 9.7, 9.6, 9.65, 9.7, 9.8, 9.9, 10.05]
    volumes = [500, 600, 700, 800, 900, 700, 600, 600, 700, 900]
    ctx = make_context(daily=_flat(), intraday=make_intraday("2026-01-16", prices, volumes))
    c = intraday.compute(ctx)

    assert c.evidence
    assert all(e.source_key == "intraday_bars" for e in c.evidence)
