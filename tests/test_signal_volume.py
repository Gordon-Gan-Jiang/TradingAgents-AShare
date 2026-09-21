"""量能维度：缩量/放量、反抽量能恢复率、以及 D6 的「缩量歧义」处理。

最关键的两条：
1. **缩量本身不能算洗盘证据**。缩量既可能是主力锁仓震仓，也可能是流动性
   枯竭无人问津——后者恰恰是最危险的「真跌」。区分办法是看反抽时量能是否
   回来：缩量下跌 + 反抽同样无量 → 不给洗盘分。
2. **仍在下杀途中（尚未反抽）只能给一半证据**。此时无法验证承接，判定必须
   由证伪条件兜底（「次日反抽无量则假设失效」）。
"""

from __future__ import annotations

import pytest

from tests._signal_fixtures import make_context, make_daily


def _ctx(closes, volumes, **kw):
    return make_context(daily=make_daily(closes, volumes, **kw))


# ── 可用性 ────────────────────────────────────────────────────────────────


def test_unavailable_when_volume_column_is_all_nan():
    """量能全缺时必须显式报不可得，而不是当作「无异常」的中性 0。"""
    from tradingagents.signals import volume

    daily = make_daily([10.0] * 10, [1000.0] * 10)
    daily["volume"] = None
    c = volume.compute(_ctx_with(daily))

    assert c.available is False
    assert c.contribution == 0.0
    assert c.unavailable_reason


def _ctx_with(daily):
    from tradingagents.signals.context import build_context
    from tradingagents.signals.ports import SignalPorts

    class _P:
        def bars(self, symbol, end, lookback):
            return daily

    return build_context(ports=SignalPorts(daily=_P()), symbol="600519.SH", as_of="2026-12-31", window_days=20)


# ── 缩量下跌 + 反抽放量 → 洗盘侧 ──────────────────────────────────────────


def test_contraction_with_rebound_recovery_scores_washout():
    """缩量下杀后反抽量能恢复 = 有人在接，这是洗盘最直接的量能证据。"""
    from tradingagents.signals import volume

    closes = [10.0, 10.2, 10.4, 10.6, 10.8, 10.5, 10.3, 10.2, 10.1, 10.3]
    volumes = [1000, 1000, 1000, 1000, 1000, 700, 600, 500, 480, 700]
    c = volume.compute(_ctx(closes, volumes))

    assert c.available is True
    assert c.raw["downside_volume_ratio_5d"] == pytest.approx(480 / 760, abs=1e-4)
    assert c.raw["rebound_volume_recovery"] == pytest.approx(700 / 570, abs=1e-4)
    assert c.side == "washout"
    # 0.5 基础分 + 超出 1.0 的恢复幅度（上限 0.5）
    assert c.magnitude == pytest.approx(0.5 + min(700 / 570 - 1.0, 0.5), abs=1e-4)


def test_contraction_still_declining_gets_half_credit_only():
    """仍在下杀途中（末根为阴线、尚无反抽）：只能给一半证据。

    此时「有没有人接」尚未被观测到，给满分等于把假设当成事实。
    """
    from tradingagents.signals import volume

    closes = [10.0, 10.2, 10.4, 10.6, 10.8, 10.5, 10.3, 10.2, 10.1]
    volumes = [1000, 1000, 1000, 1000, 1000, 700, 600, 500, 480]
    c = volume.compute(_ctx(closes, volumes))

    assert c.raw["rebound_volume_recovery"] is None
    assert c.side == "washout"
    assert c.magnitude == pytest.approx(0.5)


# ── D6：缩量的歧义必须被消解 ───────────────────────────────────────────────


def test_contraction_with_weak_rebound_scores_nothing():
    """缩量下跌 + 反抽同样无量 → **不给洗盘分**。

    这是本维度最重要的一条：两种情况在「缩量」这个单一观测量上完全一致，
    只能靠反抽量能区分。不给分（而不是给负分）是因为量能证据本身不支持
    「主力在主动出货」这个更强的论断——派发需要放量。
    """
    from tradingagents.signals import volume

    closes = [10.0, 10.2, 10.4, 10.6, 10.8, 10.5, 10.3, 10.2, 10.1, 10.15]
    volumes = [1000, 1000, 1000, 1000, 1000, 700, 600, 500, 480, 350]
    c = volume.compute(_ctx(closes, volumes))

    assert c.raw["rebound_volume_recovery"] == pytest.approx(350 / 570, abs=1e-4)
    assert c.side is None
    assert c.contribution == 0.0
    # 但原始观测必须保留 —— 回答层要能说「反抽未放量」
    assert c.raw["rebound_volume_recovery"] is not None


def test_expansion_on_decline_scores_distribution():
    """放量下跌 = 派发侧证据（此形态下缩量才可能是洗盘）。"""
    from tradingagents.signals import volume

    closes = [10.0, 10.2, 10.4, 10.6, 10.8, 10.5, 10.2, 10.0, 9.9]
    volumes = [1000, 1000, 1000, 1000, 1000, 1500, 1700, 1900, 2100]
    c = volume.compute(_ctx(closes, volumes))

    assert c.raw["downside_volume_ratio_5d"] == pytest.approx(2100 / 1420, abs=1e-4)
    assert c.side == "distribution"
    assert c.contribution < 0


def test_neutral_when_volume_within_normal_band():
    """量能在正常区间 → 中性，不产生任何方向的证据。"""
    from tradingagents.signals import volume

    closes = [10.0 + 0.1 * i for i in range(9)] + [10.7]
    volumes = [1000] * 10
    c = volume.compute(_ctx(closes, volumes))

    assert c.available is True
    assert c.side is None
    assert c.contribution == 0.0


# ── 证据与观测完整性 ───────────────────────────────────────────────────────


def test_every_contribution_is_anchored_to_evidence():
    """贡献非零则必须带证据锚点（值 + 来源 + as_of）。"""
    from tradingagents.signals import volume

    closes = [10.0, 10.2, 10.4, 10.6, 10.8, 10.5, 10.3, 10.2, 10.1, 10.3]
    volumes = [1000, 1000, 1000, 1000, 1000, 700, 600, 500, 480, 700]
    c = volume.compute(_ctx(closes, volumes))

    assert c.contribution != 0
    assert c.evidence, "非零贡献必须携带证据"
    for e in c.evidence:
        assert e.source_key == "stock_data"
        assert e.as_of == "2026-01-16"
        assert e.unit in {"ratio", "pct", "yuan", "count", "days", "date", "bool", "text"}


def test_raw_reports_all_documented_observables():
    """原始观测必须齐全——回答层要引用它们，校准层要按它们统计判别力。"""
    from tradingagents.signals import volume

    closes = [10.0, 10.2, 10.4, 10.6, 10.8, 10.5, 10.3, 10.2, 10.1, 10.3]
    volumes = [1000, 1000, 1000, 1000, 1000, 700, 600, 500, 480, 700]
    c = volume.compute(_ctx(closes, volumes))

    for key in (
        "volume_ratio_5d",
        "downside_volume_ratio_5d",
        "rebound_volume_recovery",
        "downside_volume_share",
        "tail_down_bars",
    ):
        assert key in c.raw, f"缺少观测量 {key}"


def test_core_flag_allows_abstention_gating():
    """量能是四个核心维度之一：不可得时探针必须弃权，因此需要 core 标记。"""
    from tradingagents.signals import volume

    c = volume.compute(_ctx([10.0] * 10, [1000.0] * 10))
    assert c.core is True
