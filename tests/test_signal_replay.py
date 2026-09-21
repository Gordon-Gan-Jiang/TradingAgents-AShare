"""离线回放与阈值标定（design D15）。

## 这些用例守的是「校准不被自欺」

离线回放有同源偏差：同一份代码既选样本又调参。因此规格把权限切开了——

* **离线只用于否决**无判别力的维度
* **激活权重必须由前瞻样本支持**

如果这条切分只写在文档里，几周后就会有人用回放样本去调权重，然后得到一个
在那段行情上"表现优异"的模型。所以它必须是会失败的断言。

另两条同样重要：
* **按时间切分**（随机切分会因金融数据自相关而泄漏）
* **没有平台区就不许调参**（那说明维度集本身无判别力，调阈值只是把噪声挪位置）
"""

from __future__ import annotations

import pytest


def _sample(**kw):
    from tradingagents.signals.replay import ReplaySample

    base = dict(
        symbol="600519.SH",
        as_of="2026-01-16",
        state="washout",
        margin=0.5,
        confidence=85,
        evaluable=True,
        threshold_hit=True,
        contributions={"volume_contraction": 0.9, "relative_strength": 1.1},
    )
    base.update(kw)
    return ReplaySample(**base)


# ══════════════════════════════════════════════════════════════════════════
# 时间切分
# ══════════════════════════════════════════════════════════════════════════


def test_time_split_is_chronological_not_random():
    """必须按时间切分：金融数据有自相关，随机切分会让同波行情同时出现在
    训练与测试集里，测出来的准确率是泄漏出来的。"""
    from tradingagents.signals.replay import time_split

    samples = [
        _sample(as_of=f"2026-01-{d:02d}", threshold_hit=(d % 2 == 0)) for d in range(1, 11)
    ]
    train, holdout = time_split(samples, train_ratio=0.7)

    assert len(train) == 7
    assert len(holdout) == 3
    # 切分点按时间：train 的全部日期都早于 holdout 的最早日期
    assert max(s.as_of for s in train) <= min(s.as_of for s in holdout)
    # 且切分不是简单取前 N 个：输入乱序时仍按时间排序
    shuffled = list(reversed(samples))
    train2, holdout2 = time_split(shuffled, train_ratio=0.7)
    assert [s.as_of for s in train2] == [s.as_of for s in train]


def test_time_split_handles_ties_on_the_boundary_date():
    """同一天的多个样本必须整组落在同一侧——否则同一天的判定被拆到两侧。"""
    from tradingagents.signals.replay import time_split

    samples = [_sample(as_of="2026-01-01", symbol=f"{i}.SH") for i in range(3)]
    samples += [_sample(as_of="2026-01-02", symbol=f"x{i}.SH") for i in range(3)]
    train, holdout = time_split(samples, train_ratio=0.5)
    assert {s.as_of for s in train} & {s.as_of for s in holdout} == set()


def test_empty_input_is_handled():
    from tradingagents.signals.replay import time_split

    assert time_split([]) == ([], [])


# ══════════════════════════════════════════════════════════════════════════
# 标定曲线
# ══════════════════════════════════════════════════════════════════════════


def _samples_for_curve():
    """构造：margin 越高命中率越高，且高 margin 样本更少。"""
    out = []
    for i in range(40):
        margin = 0.05 + i * 0.02
        hit = margin > 0.25
        out.append(_sample(as_of=f"2026-01-{(i % 28) + 1:02d}", margin=margin, threshold_hit=hit))
    return out


def test_calibration_curve_reports_hit_and_abstention_rates():
    from tradingagents.signals.replay import calibration_curve

    curve = calibration_curve(_samples_for_curve(), thresholds=[0.15, 0.25, 0.35])
    assert len(curve) == 3
    for point in curve:
        assert 0.0 <= point.abstention_rate <= 1.0
        assert point.sample >= 0
        if point.sample:
            assert 0.0 <= point.hit_rate <= 1.0
    # 阈值越高 → 弃权越多
    assert curve[0].abstention_rate <= curve[-1].abstention_rate


def test_curve_separates_non_evaluable_samples():
    """不可评估样本（停牌）不得进入命中率分母。"""
    from tradingagents.signals.replay import calibration_curve

    samples = [
        _sample(margin=0.5, evaluable=True, threshold_hit=True),
        _sample(margin=0.5, evaluable=False, threshold_hit=False),
    ]
    point = calibration_curve(samples, thresholds=[0.15])[0]
    assert point.sample == 1
    assert point.not_evaluable == 1
    assert point.hit_rate == pytest.approx(1.0)


# ══════════════════════════════════════════════════════════════════════════
# 平台区检测
# ══════════════════════════════════════════════════════════════════════════


def test_plateau_is_found_in_a_stable_region():
    """阈值取**平台区拐点**（±20% 扰动下命中率不变），不取最大值。

    取最大值会把阈值钉在一个由个别样本造成的尖峰上；平台区意味着这个区间内
    结论不敏感，因此可以放心选取。
    """
    from tradingagents.signals.replay import calibration_curve, find_plateau

    curve = calibration_curve(_samples_for_curve(), thresholds=[0.15, 0.20, 0.25, 0.30, 0.35])
    plateau = find_plateau(curve, tolerance=0.2)
    assert plateau.found is True
    assert plateau.threshold is not None
    assert plateau.stable_low is not None and plateau.stable_high is not None
    assert plateau.stable_low <= plateau.threshold <= plateau.stable_high


def test_no_plateau_reports_that_the_dimension_set_lacks_power():
    """曲线始终陡变 → 说明维度集无判别力，**不得进入调参**。

    这一条最容易被忽略：没有平台区时继续调阈值，只是把噪声挪个位置。
    """
    from tradingagents.signals.replay import CurvePoint, find_plateau

    curve = [
        CurvePoint(threshold=0.10, sample=30, hit_rate=0.10, abstention_rate=0.05, not_evaluable=0),
        CurvePoint(threshold=0.20, sample=20, hit_rate=0.90, abstention_rate=0.30, not_evaluable=0),
        CurvePoint(threshold=0.30, sample=10, hit_rate=0.20, abstention_rate=0.60, not_evaluable=0),
        CurvePoint(threshold=0.40, sample=5, hit_rate=0.95, abstention_rate=0.85, not_evaluable=0),
    ]
    plateau = find_plateau(curve, tolerance=0.05)
    assert plateau.found is False
    assert plateau.threshold is None
    assert "power" in plateau.reason or "判别力" in plateau.reason


def test_plateau_rejects_the_highest_hit_rate_spike():
    """尖峰（样本极少、命中率极高）不得被选为阈值。"""
    from tradingagents.signals.replay import CurvePoint, find_plateau

    curve = [
        CurvePoint(threshold=0.15, sample=50, hit_rate=0.55, abstention_rate=0.10, not_evaluable=0),
        CurvePoint(threshold=0.25, sample=45, hit_rate=0.56, abstention_rate=0.25, not_evaluable=0),
        CurvePoint(threshold=0.35, sample=40, hit_rate=0.55, abstention_rate=0.40, not_evaluable=0),
        CurvePoint(threshold=0.90, sample=2, hit_rate=1.00, abstention_rate=0.95, not_evaluable=0),
    ]
    plateau = find_plateau(curve, tolerance=0.2, min_sample=10)
    assert plateau.found is True
    assert plateau.threshold != 0.90


# ══════════════════════════════════════════════════════════════════════════
# 维度判别力
# ══════════════════════════════════════════════════════════════════════════


def test_dimension_power_reports_sample_and_hit_rate():
    from tradingagents.signals.replay import dimension_power

    samples = [
        _sample(threshold_hit=True, contributions={"volume_contraction": 0.9}),
        _sample(threshold_hit=True, contributions={"volume_contraction": 0.8}),
        _sample(threshold_hit=False, contributions={"volume_contraction": -0.2}),
        _sample(threshold_hit=False, contributions={"volume_contraction": -0.1}),
    ]
    # 显式降低门槛以覆盖判别力逻辑本身；生产默认闸门由下一条用例钉住
    report = dimension_power(samples, min_sample=4)
    volume = report["volume_contraction"]
    assert volume["sample"] == 4
    assert volume["hit_rate"] == pytest.approx(0.5)
    # 贡献为正时命中，为负时未命中 → 判别力为正
    assert volume["discriminative"] is True


def test_dimension_power_flags_non_discriminative_dimension():
    """贡献与结果无关的维度必须被标为无判别力——这是「否决」的输入。"""
    from tradingagents.signals.replay import dimension_power

    samples = [
        _sample(threshold_hit=True, contributions={"ma_structure": 0.5}),
        _sample(threshold_hit=True, contributions={"ma_structure": -0.5}),
        _sample(threshold_hit=False, contributions={"ma_structure": 0.5}),
        _sample(threshold_hit=False, contributions={"ma_structure": -0.5}),
    ]
    assert dimension_power(samples, min_sample=4)["ma_structure"]["discriminative"] is False


def test_dimension_power_default_gate_refuses_to_conclude_on_small_samples():
    """默认门槛下 4 个样本必须给出「样本不足」而不是 False。

    给 False 会误杀一个只是样本少的有效维度——那正是样本饥饿的表现。
    """
    from tradingagents.signals.replay import dimension_power

    samples = [
        _sample(threshold_hit=True, contributions={"volume_contraction": 0.9}),
        _sample(threshold_hit=False, contributions={"volume_contraction": 0.8}),
    ]
    stats = dimension_power(samples)
    assert stats["volume_contraction"]["discriminative"] is None
    assert "insufficient" in stats["volume_contraction"]["reason"]


def test_dimension_power_reports_insufficient_sample():
    from tradingagents.signals.replay import dimension_power

    samples = [_sample(threshold_hit=True, contributions={"chip_distribution": 0.5})]
    chip = dimension_power(samples)["chip_distribution"]
    assert chip["sample"] == 1
    assert chip["discriminative"] is None  # 不是 False：样本不足时不给结论
    assert "insufficient" in chip["reason"]


# ══════════════════════════════════════════════════════════════════════════
# 离线 / 前瞻 权限分离
# ══════════════════════════════════════════════════════════════════════════


def test_offline_replay_may_only_reject_dimensions():
    from tradingagents.signals.replay import offline_authority

    authority = offline_authority()
    assert authority["may_reject_dimensions"] is True
    assert authority["may_activate_weights"] is False
    assert authority["reason"]


def test_weight_activation_requires_forward_samples():
    from tradingagents.signals.replay import check_activation_gate

    offline_only = check_activation_gate(offline_samples=500, forward_samples=0, per_dimension={})
    assert offline_only.allowed is False
    assert "forward" in offline_only.reason or "前瞻" in offline_only.reason

    forward_ok = check_activation_gate(
        offline_samples=500,
        forward_samples=120,
        per_dimension={"volume_contraction": 40},
    )
    assert forward_ok.allowed is True


def test_activation_gate_enforces_per_dimension_minimum():
    """每个待改维度各自需要 ≥30 个可用样本——防的正是样本饥饿。"""
    from tradingagents.signals.replay import check_activation_gate

    gate = check_activation_gate(
        offline_samples=1000,
        forward_samples=200,
        per_dimension={"volume_contraction": 40, "chip_distribution": 5},
    )
    assert gate.allowed is False
    assert "chip_distribution" in gate.blocking_dimensions


def test_activation_gate_reports_sample_gap():
    from tradingagents.signals.replay import check_activation_gate

    gate = check_activation_gate(offline_samples=10, forward_samples=40, per_dimension={})
    assert gate.allowed is False
    assert gate.needed_forward == 100
    assert gate.have_forward == 40


# ══════════════════════════════════════════════════════════════════════════
# 权重快照可重放
# ══════════════════════════════════════════════════════════════════════════


def test_replay_score_reproduces_the_original_scores():
    """用存档的权重与观测重算，必须与原始判定逐项一致。

    若存档缺少调制因子（例如只存了 raw 观测），重算就会与原始判定不符——
    这种不一致不报错，只是让校准统计建立在一个与历史判定不同的模型上。
    """
    from dataclasses import asdict

    from tests._signal_fixtures import make_context, make_daily
    from tradingagents.signals.replay import replay_score
    from tradingagents.signals.ports import PeerSnapshot, SignalPorts
    from tradingagents.signals.washout import evaluate_context

    daily = make_daily([10.0] * 5 + [9.8, 9.6, 9.4, 9.3, 9.6],
                       [1000, 1000, 1000, 1000, 1000, 700, 600, 500, 480, 700])

    class _Daily:
        def bars(self, symbol, end, lookback):
            return daily

    class _Peer:
        def snapshot(self, symbol, day):
            return PeerSnapshot(board_name="白酒", board_return_pct=-3.0,
                                market_return_pct=-1.5, stock_turnover=2.2,
                                board_median_turnover=1.8)

    from tradingagents.signals.context import build_context

    ctx = build_context(
        ports=SignalPorts(daily=_Daily(), peer=_Peer()),
        symbol="600519.SH", as_of="2026-01-16", window_days=20,
    )
    result = evaluate_context(ctx)
    assert result.state != "insufficient_evidence"

    # 模拟落库：components 用 asdict，调制器存完整字段
    components = [asdict(c) for c in result.components]
    modulator_factor = result.modulators[0].factor

    replayed = replay_score(components=components, modulator_factor=modulator_factor)
    assert replayed["washout"] == pytest.approx(result.score_washout, rel=1e-9)
    assert replayed["distribution"] == pytest.approx(result.score_distribution, rel=1e-9)
    assert replayed["trend_break"] == pytest.approx(result.score_trend_break, rel=1e-9)


def test_replay_score_reveals_a_missing_modulator_factor():
    """缺调制因子时重算必然不符——这条断言就是「快照必须完整」的守门人。"""
    from dataclasses import asdict

    from tests._signal_fixtures import make_daily
    from tradingagents.signals.context import build_context
    from tradingagents.signals.ports import PeerSnapshot, SignalPorts
    from tradingagents.signals.replay import replay_score
    from tradingagents.signals.washout import evaluate_context

    daily = make_daily([10.0] * 5 + [9.8, 9.6, 9.4, 9.3, 9.6],
                       [1000, 1000, 1000, 1000, 1000, 700, 600, 500, 480, 700])

    class _Daily:
        def bars(self, symbol, end, lookback):
            return daily

    class _Peer:
        def snapshot(self, symbol, day):
            return PeerSnapshot(board_name="白酒", board_return_pct=-3.0,
                                market_return_pct=-1.5, stock_turnover=2.2,
                                board_median_turnover=1.8)

    ctx = build_context(
        ports=SignalPorts(daily=_Daily(), peer=_Peer()),
        symbol="600519.SH", as_of="2026-01-16", window_days=20,
    )
    result = evaluate_context(ctx)
    components = [asdict(c) for c in result.components]

    # 用 1.0 冒充因子（相当于存档里没有因子）
    wrong = replay_score(components=components, modulator_factor=1.0)
    assert wrong["washout"] != pytest.approx(result.score_washout, rel=1e-6)


def test_replay_score_does_not_modulate_trend_break():
    from tradingagents.signals.replay import replay_score

    components = [{"key": "ma_structure", "available": True, "side": "trend_break",
                   "weight": 0.8, "magnitude": 1.0}]
    early = replay_score(components=components, modulator_factor=1.5)
    late = replay_score(components=components, modulator_factor=0.5)
    assert early["trend_break"] == pytest.approx(late["trend_break"])
