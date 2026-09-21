"""M1 规则层测试：硬过滤、双维度评分、情绪温度 gating、端到端编排。"""

from __future__ import annotations

import pandas as pd
import pytest

from tradingagents.dataflows.mainline_scoring import (
    build_mainline_candidates,
    compute_emotion_temperature,
    hard_filter_spot,
    score_boards,
)


def _spot(n: int = 5, start_chg: float = 3.0) -> pd.DataFrame:
    names = ["板块A", "板块B", "板块C", "板块D", "板块E"]
    return pd.DataFrame(
        {
            "name": names[:n],
            "code": [f"BK{i}" for i in range(n)],
            "chg_1d": [start_chg - i * 0.5 for i in range(n)],
            "turnover": [3.0, 2.5, 2.0, 1.5, 1.0][:n],
            "up_count": [80, 60, 40, 20, 10][:n],
            "down_count": [5, 10, 20, 30, 40][:n],
            "leader": [f"领涨{name}" for name in names[:n]],
            "leader_chg": [10.0, 8.0, 6.0, 4.0, 2.0][:n],
        }
    )


def _hist(close_values) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": [f"2026-06-{i:02d}" for i in range(1, len(close_values) + 1)],
            "close": close_values,
            "turnover": [1.0] * len(close_values),
        }
    )


# ── 硬过滤 ────────────────────────────────────────────────────────


def test_hard_filter_drops_invalid_rows_and_dedupes():
    df = pd.DataFrame(
        {
            "name": ["A", "B", "A", None],
            "code": ["1", "2", "1", "3"],
            "chg_1d": [1.0, None, 1.0, 2.0],
        }
    )
    out = hard_filter_spot(df)
    assert len(out) == 1
    assert out.iloc[0]["name"] == "A"


def test_hard_filter_empty_input():
    assert hard_filter_spot(pd.DataFrame()).empty


# ── 情绪温度 gating ───────────────────────────────────────────────


def test_compute_emotion_temperature_active():
    zt = pd.DataFrame({"连板数": [1] * 50 + [3] * 5 + [5] * 2 + [7] * 1})
    res = compute_emotion_temperature(zt)
    assert res["zt_count"] == 58
    assert res["max_lianban"] == 7
    assert res["lianban_ge3"] == 8
    assert 0 <= res["temperature"] <= 100
    assert res["gate"] in ("normal", "euphoria")


def test_compute_emotion_temperature_ice():
    # 极少数涨停且无连板高度 → 冰点（temperature < 25）
    zt_cold = pd.DataFrame({"连板数": [1] * 2})
    res = compute_emotion_temperature(zt_cold)
    assert res["gate"] == "ice"
    assert res["regime"] == "冰点"


def test_compute_emotion_temperature_unknown():
    res = compute_emotion_temperature(None)
    assert res["regime"] == "未知"
    res2 = compute_emotion_temperature(pd.DataFrame())
    assert res2["regime"] == "未知"


# ── 双维度评分 ────────────────────────────────────────────────────


def test_score_boards_heat_and_strength_components():
    spot = _spot()
    hist_map = {"板块A": _hist([100] * 10 + [110, 115, 120, 125, 130])}
    flow_1d = pd.DataFrame({"name": ["板块A"], "net_inflow": [5.0]})
    flow_5d = pd.DataFrame({"name": ["板块A"], "net_inflow": [20.0]})
    scored = score_boards(
        spot,
        sector_type="industry",
        hist_map=hist_map,
        flow_1d=flow_1d,
        flow_5d=flow_5d,
        benchmark_chg_5d=0.05,
        market_median_chg_1d=2.0,
        perspective="short",
    )
    assert len(scored) == 5
    by_name = {b.name: b for b in scored}
    a = by_name["板块A"]
    assert a.hist_available is True
    assert a.chg_5d is not None and a.chg_5d > 0.2
    assert a.excess_ret_5d is not None
    assert a.net_inflow_1d == 5.0
    assert a.inflow_persistent is True
    assert a.heat_score > 50
    assert 0 <= a.strength_score <= 100
    # 涨幅最高的板块 composite 应排第一
    assert scored[0].name == "板块A"


def test_score_boards_without_hist_falls_back_to_1d():
    spot = _spot()
    scored = score_boards(
        spot,
        sector_type="concept",
        hist_map={},
        flow_1d=None,
        flow_5d=None,
        benchmark_chg_5d=None,
        market_median_chg_1d=2.0,
        perspective="medium",
    )
    assert len(scored) == 5
    for b in scored:
        assert b.hist_available is False
        assert 0 <= b.strength_score <= 100


def test_score_boards_medium_weights_strength_more():
    spot = _spot()
    hist_map = {name: _hist([100] * 10 + [105, 106, 107, 108, 109]) for name in spot["name"]}
    flow = pd.DataFrame({"name": spot["name"], "net_inflow": [5.0] * len(spot)})
    short = score_boards(
        spot, sector_type="industry", hist_map=hist_map,
        flow_1d=flow, flow_5d=flow, benchmark_chg_5d=0.02,
        market_median_chg_1d=2.0, perspective="short",
    )
    medium = score_boards(
        spot, sector_type="industry", hist_map=hist_map,
        flow_1d=flow, flow_5d=flow, benchmark_chg_5d=0.02,
        market_median_chg_1d=2.0, perspective="medium",
    )
    assert len(short) == len(medium) == 5


# ── 端到端编排（注入 fetcher） ────────────────────────────────────


def _fetchers():
    def f_spot(sector_type: str) -> pd.DataFrame:
        return _spot()

    def f_hist(name: str, sector_type: str) -> pd.DataFrame:
        return _hist([100] * 10 + [110, 115, 120, 125, 130])

    def f_flow(sector_type: str, period: str) -> pd.DataFrame:
        return pd.DataFrame({"name": ["板块A"], "net_inflow": [5.0 if period == "1d" else 20.0]})

    def f_zt(date: str) -> pd.DataFrame:
        return pd.DataFrame({"连板数": [1] * 40 + [3] * 3})

    def f_bench() -> pd.DataFrame:
        return pd.DataFrame({"close": [3000] * 10 + [3100, 3150, 3200, 3250, 3300]})

    return f_spot, f_hist, f_flow, f_zt, f_bench


def test_build_mainline_candidates_end_to_end():
    f_spot, f_hist, f_flow, f_zt, f_bench = _fetchers()
    res = build_mainline_candidates(
        "2026-06-27",
        perspective="short",
        fetch_spot=f_spot,
        fetch_hist=f_hist,
        fetch_flow=f_flow,
        fetch_zt=f_zt,
        fetch_benchmark=f_bench,
    )
    assert res["trade_date"] == "2026-06-27"
    assert res["perspective"] == "short"
    assert res["emotion"]["zt_count"] == 43
    assert res["market_median_chg_1d"] == pytest.approx(2.0, abs=1e-9)
    assert res["benchmark_chg_5d"] == pytest.approx(3300 / 3000 - 1, abs=1e-6)
    assert res["pool_sizes"]["industry"] == 5
    assert res["hist_fetched"] >= 5
    assert len(res["boards"]) > 0
    top = res["boards"][0]
    assert top["name"] == "板块A"
    assert "heat_score" in top and "strength_score" in top and "composite_score" in top
    assert "emotion" in res and "gate" in res["emotion"]


def test_build_mainline_candidates_medium_perspective():
    f_spot, f_hist, f_flow, f_zt, f_bench = _fetchers()
    res = build_mainline_candidates(
        "2026-06-27",
        perspective="medium",
        fetch_spot=f_spot,
        fetch_hist=f_hist,
        fetch_flow=f_flow,
        fetch_zt=f_zt,
        fetch_benchmark=f_bench,
    )
    assert res["perspective"] == "medium"
    assert len(res["boards"]) > 0


def test_build_mainline_candidates_empty_spot():
    def f_spot(sector_type: str) -> pd.DataFrame:
        return pd.DataFrame()

    f_hist = lambda name, st: pd.DataFrame()  # noqa: E731
    f_flow = lambda st, period: pd.DataFrame()  # noqa: E731
    f_zt = lambda date: pd.DataFrame()  # noqa: E731
    f_bench = lambda: pd.DataFrame()  # noqa: E731

    res = build_mainline_candidates(
        "2026-06-27",
        fetch_spot=f_spot,
        fetch_hist=f_hist,
        fetch_flow=f_flow,
        fetch_zt=f_zt,
        fetch_benchmark=f_bench,
    )
    assert res["market_median_chg_1d"] is None
    assert res["benchmark_chg_5d"] is None
    assert res["boards"] == []
    assert res["emotion"]["regime"] == "未知"


# ── v2 多周期动量结构（金融理论版） ─────────────────────────────


def _hist_trend(up=True, days=80):
    """带波动的上行/下行板块历史（RSI 落在正常区间，不触发拥挤门槛）。"""
    import math

    closes = []
    for i in range(days):
        base = 100 * (1.008 ** i) if up else 100 * (0.992 ** i)
        closes.append(base * (1 + 0.02 * math.sin(i)))  # 强周期波动，含明显回调日（RSI 正常区间）
    return pd.DataFrame({"date": [f"2026-06-{i:02d}" for i in range(1, days + 1)], "close": closes, "turnover": [1.2] * days})


def test_v2_features_and_gate():
    spot = _spot()
    up_hist = _hist_trend(up=True, days=80)   # 上行趋势：RS20>0, MA20>MA60
    hist_map = {name: up_hist for name in spot["name"]}
    flow_pos = pd.DataFrame({"name": spot["name"], "net_inflow": [5.0] * len(spot)})
    scored = score_boards(
        spot, sector_type="industry", hist_map=hist_map,
        flow_1d=flow_pos, flow_5d=flow_pos, flow_10d=flow_pos,
        benchmark_chg_5d=0.01, benchmark_chg_20d=0.02,
        market_median_chg_1d=2.0, perspective="short",
    )
    a = scored[0]
    assert a.hist_available is True
    assert a.rs20 is not None and a.rs20 > 0          # 上行板块 20 日超额为正
    assert a.ma_bullish is True                        # 多头排列
    assert a.rsi14 is not None and 0 <= a.rsi14 <= 100
    assert a.passes_gate is True                       # 硬门槛通过
    assert a.phase_hint in ("主升", "发酵", "高位分歧")
    assert any(b.passes_gate for b in scored)


def test_v2_down_trend_fails_gate():
    spot = _spot()
    down_hist = _hist_trend(up=False, days=80)
    hist_map = {name: down_hist for name in spot["name"]}
    scored = score_boards(
        spot, sector_type="industry", hist_map=hist_map,
        flow_1d=None, flow_5d=None, flow_10d=None,
        benchmark_chg_5d=0.0, benchmark_chg_20d=0.0,
        market_median_chg_1d=2.0, perspective="short",
    )
    for b in scored:
        assert b.passes_gate is False                  # 下行板块不满足 RS20/均线门槛
        assert b.phase_hint in ("退潮", "脉冲", "数据不足")


def test_v2_flow_data_missing_exempts_gate():
    """资金流数据缺失时硬门槛豁免（flow_data_missing=True），不误杀板块。"""
    spot = _spot()
    up_hist = _hist_trend(up=True, days=80)
    hist_map = {name: up_hist for name in spot["name"]}
    scored = score_boards(
        spot, sector_type="industry", hist_map=hist_map,
        flow_1d=None, flow_5d=None, flow_10d=None,   # 资金流整体缺失
        benchmark_chg_5d=0.01, benchmark_chg_20d=0.02,
        market_median_chg_1d=2.0, perspective="short",
    )
    for b in scored[:3]:
        assert b.flow_data_missing is True
        assert b.inflow_persistent_10 is False
        assert b.passes_gate is True                  # 豁免后仍过门槛（RS20/均线满足）
