"""C1 周期引擎单测：身份匹配、档案轨迹、周期特征、阶段判定、退潮预警、动作建议。"""

from __future__ import annotations

import pytest

from tradingagents.dataflows.mainline_cycle import (
    FERMENT,
    INSUFFICIENT,
    RETREAT,
    RISE,
    SPLIT,
    append_track,
    build_verify_conditions,
    compute_cycle_features,
    judge_cycle_position,
    last_active_days,
    match_by_boards,
    match_mainline_key,
    retreat_alerts,
    suggest_action,
)


def _snap(date, strength, heat=70, rs20=None, fund=None, rsi=None, zt=None, persistent=False):
    return {
        "date": date, "strength": strength, "heat": heat, "rs20": rs20,
        "net_inflow_5d": fund, "inflow_persistent_10": persistent,
        "rsi14": rsi, "chg_1d": 2.0, "zt_count": zt,
    }


def _track(days: list[tuple]):
    """[(date, strength, fund, rsi), ...] → track"""
    out = []
    for d, s, fund, rsi in days:
        out.append(_snap(d, s, fund=fund, rsi=rsi))
    return out


# ── 身份匹配 ────────────────────────────────────────────────────


def test_match_mainline_key_exact():
    assert match_mainline_key("AI算力", [], ["AI算力", "低空经济"]) == "AI算力"


def test_match_mainline_key_contains():
    assert match_mainline_key("算力", [], ["AI算力"]) == "AI算力"
    assert match_mainline_key("AI算力", [], ["算力"]) == "算力"


def test_match_mainline_key_no_match():
    assert match_mainline_key("低空经济", [], ["AI算力"]) is None
    assert match_mainline_key("芯片", [], ["AI算力"]) is None  # 无包含关系
    assert match_mainline_key("A", [], ["AI算力"]) is None     # 单字不参与包含匹配


def test_match_by_boards_intersection():
    known = [{"key": "AI算力", "boards": ["CPO概念", "光模块"]}]
    assert match_by_boards(["光模块", "液冷"], known) == "AI算力"
    assert match_by_boards(["低空经济"], known) is None


# ── 档案轨迹 ────────────────────────────────────────────────────


def test_append_track_dedup_and_order():
    t = append_track([], _snap("2026-08-30", 70))
    t = append_track(t, _snap("2026-08-28", 60))
    t = append_track(t, _snap("2026-08-30", 75))  # 同日覆盖
    assert [s["date"] for s in t] == ["2026-08-28", "2026-08-30"]
    assert t[-1]["strength"] == 75


def test_last_active_days():
    t = append_track([], _snap("2026-08-28", 70))
    assert last_active_days(t, "2026-08-31") == 3
    assert last_active_days([], "2026-08-31") is None


# ── 周期特征 ────────────────────────────────────────────────────


def test_compute_features_basic():
    t = _track([
        ("2026-08-25", 55, 1.0, 55),
        ("2026-08-26", 62, 2.0, 60),
        ("2026-08-27", 68, 3.0, 65),
        ("2026-08-28", 72, 4.0, 70),
        ("2026-08-31", 78, 5.0, 72),
    ])
    f = compute_cycle_features(t)
    assert f["cycle_days"] == 5
    assert f["sufficient"] is True
    assert f["strength_slope1"] is not None and f["strength_slope1"] > 0
    assert f["fund_positive"] is True
    assert f["strength_peak"] == 78
    assert f["peak_gap"] == 0.0
    assert 0 < f["progress"] <= 100


def test_compute_features_missing_fields():
    t = [_snap("2026-08-25", None), _snap("2026-08-26", 62)]
    f = compute_cycle_features(t)
    assert "strength" in f["missing_fields"]
    assert f["sufficient"] is False


# ── 阶段判定 ────────────────────────────────────────────────────


def test_judge_ferment_early():
    t = _track([("2026-08-25", 45, 0.5, 50), ("2026-08-26", 52, 1.0, 55), ("2026-08-27", 58, 1.5, 60)])
    stage, reason = judge_cycle_position(t)
    assert stage == FERMENT
    assert "上升初期" in reason


def test_judge_rise():
    t = _track([
        ("2026-08-24", 60, 1.0, 58), ("2026-08-25", 65, 2.0, 60),
        ("2026-08-26", 70, 3.0, 62), ("2026-08-27", 75, 4.0, 65), ("2026-08-28", 80, 5.0, 68),
    ])
    stage, _ = judge_cycle_position(t)
    assert stage == RISE


def test_judge_split_crowded():
    t = _track([
        ("2026-08-24", 75, 4.0, 76), ("2026-08-25", 76, 4.0, 78),
        ("2026-08-26", 76, 4.0, 80), ("2026-08-27", 75, 3.0, 82), ("2026-08-28", 74, 3.0, 83),
    ])
    stage, reason = judge_cycle_position(t)
    assert stage == SPLIT
    assert "拥挤" in reason or "高位" in reason


def test_judge_split_high_flat():
    t = _track([
        ("2026-08-24", 78, 4.0, 70), ("2026-08-25", 78, 4.0, 70),
        ("2026-08-26", 77, 3.0, 72), ("2026-08-27", 76, 3.0, 72), ("2026-08-28", 75, 2.5, 72),
    ])
    stage, _ = judge_cycle_position(t)
    assert stage == SPLIT  # 高位走平


def test_judge_retreat_momentum_decay_plus_fund():
    # 强度从峰值 80 连续降 + 资金转弱 → 退潮
    t = _track([
        ("2026-08-21", 80, 5.0, 78), ("2026-08-22", 78, 4.0, 78),
        ("2026-08-25", 74, 2.0, 76), ("2026-08-26", 68, -1.0, 74), ("2026-08-27", 60, -3.0, 72),
    ])
    stage, reason = judge_cycle_position(t)
    assert stage == RETREAT
    assert "衰减" in reason


def test_judge_not_retreat_on_healthy_pullback():
    # 强度小回调但资金仍强、斜率仍正 → 不应退潮
    t = _track([
        ("2026-08-24", 70, 4.0, 66), ("2026-08-25", 74, 5.0, 68),
        ("2026-08-26", 72, 5.0, 70), ("2026-08-27", 73, 5.0, 70), ("2026-08-28", 76, 6.0, 70),
    ])
    stage, _ = judge_cycle_position(t)
    assert stage in (RISE, FERMENT)  # 健康回调不判退潮


def test_judge_insufficient_data():
    t = _track([("2026-08-25", 60, 1.0, 55), ("2026-08-26", 62, 1.0, 55)])
    stage, reason = judge_cycle_position(t)
    assert stage == INSUFFICIENT
    assert "不足" in reason


# ── 退潮预警 ────────────────────────────────────────────────────


def test_retreat_alerts_single_signal():
    t = _track([
        ("2026-08-21", 80, 5.0, 78), ("2026-08-22", 78, 4.0, 78),
        ("2026-08-25", 74, 2.0, 76), ("2026-08-26", 68, -1.0, 74), ("2026-08-27", 60, -3.0, 72),
    ])
    f = compute_cycle_features(t)
    alerts = retreat_alerts(f)
    assert len(alerts) >= 2  # 衰减加速 + 资金转弱


def test_retreat_alerts_crowding():
    t = _track([("2026-08-25", 70, 3.0, 80), ("2026-08-26", 72, 3.0, 82), ("2026-08-27", 71, 3.0, 84)])
    f = compute_cycle_features(t)
    alerts = retreat_alerts(f)
    assert any("拥挤" in a for a in alerts)


# ── 动作与仓位 ──────────────────────────────────────────────────


def test_suggest_action_ferment_light_position():
    act = suggest_action(FERMENT)
    assert act["action"] == "布局"
    assert act["position"] == 0.10


def test_suggest_action_retreat_zero_position():
    act = suggest_action(RETREAT)
    assert act["action"] == "规避"
    assert act["position"] == 0.0


def test_suggest_action_emotion_regime():
    # 亢奋 → 仓位减半
    act = suggest_action(RISE, emotion_temperature=90)
    assert act["position"] == pytest.approx(0.075, abs=1e-3)
    # 冰点 → 不布局新主线
    act2 = suggest_action(FERMENT, emotion_temperature=20)
    assert act2["action"] == "观察"
    assert act2["position"] == 0.0


# ── 验证条件 ────────────────────────────────────────────────────


def test_build_verify_conditions():
    t = _track([
        ("2026-08-24", 70, 4.0, 66), ("2026-08-25", 74, 5.0, 68),
        ("2026-08-26", 72, 5.0, 70), ("2026-08-27", 73, 5.0, 70), ("2026-08-28", 76, 6.0, 72),
    ])
    f = compute_cycle_features(t)
    conds = build_verify_conditions(RISE, f)
    assert any("斜率" in c for c in conds)
    assert len(conds) >= 1
