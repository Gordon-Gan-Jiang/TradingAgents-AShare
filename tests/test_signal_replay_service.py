"""离线回放编排：防前视、单点失败隔离、权限边界。

最关键的一条是 `test_probe_never_sees_data_after_as_of`：用同一份行情同时
喂判定与兑现，是实现里最容易不小心把未来数据漏进判定的地方。该用例构造
「若看到未来数据则判定必然不同」的序列来钉住它。
"""

from __future__ import annotations

import pandas as pd
import pytest


def _frame(rows):
    return pd.DataFrame(
        [{"date": d, "open": c, "high": h, "low": c, "close": c, "volume": v}
         for d, h, c, v in rows]
    )


def _ports_factory(loader):
    """带板块基准的端口工厂。

    只给日线端口时相对强度不可得（它是核心维度）→ 每次判定都会弃权 →
    回放样本为零。这个后果不报错，因此必须在测试里显式装配完整端口。
    """
    from tradingagents.signals.ports import PeerSnapshot, SignalPorts

    class _Daily:
        def bars(self, symbol, end, lookback):
            return loader(symbol, end, lookback)

    class _Peer:
        def snapshot(self, symbol, day):
            return PeerSnapshot(
                board_name="白酒", board_return_pct=-3.0, market_return_pct=-1.5,
                stock_turnover=2.2, board_median_turnover=1.8,
            )

    def factory(symbol, as_of):
        return SignalPorts(daily=_Daily(), peer=_Peer())

    return factory


def _loader(rows_by_symbol=None, default_rows=None):
    data = rows_by_symbol or {}
    fallback = default_rows or []

    def load(symbol, start, end):
        rows = data.get(symbol, fallback)
        return _frame(rows)

    return load


def _rising_with_future_spike():
    """as_of 之前是缩量下杀 + 反抽；as_of 之后是暴涨。

    若判定看到了 as_of 之后的数据，累计涨幅会显著变大、位置调制因子随之变小，
    判定结果就会不同——因此这个序列能捕捉前视泄漏。
    """
    rows = []
    # 2026-01-05 ~ 01-16：10 个交易日
    closes = [10.0] * 5 + [9.8, 9.6, 9.4, 9.3, 9.6]
    volumes = [1000, 1000, 1000, 1000, 1000, 700, 600, 500, 480, 700]
    for i, (c, v) in enumerate(zip(closes, volumes)):
        rows.append((f"2026-01-{5 + i:02d}", c * 1.005, c, v))
    # as_of 之后：暴涨（只应进入兑现窗口）
    for i, c in enumerate([12.0, 13.0, 14.0, 15.0, 16.0]):
        rows.append((f"2026-01-{19 + i:02d}", c * 1.005, c, 2000))
    return rows


def test_replay_produces_a_sample_per_point():
    from api.services.signal_replay_service import replay

    loader = _loader(default_rows=_rising_with_future_spike())
    report = replay(
        points=[("600519.SH", "2026-01-16")],
        loader=loader,
        ports_factory=_ports_factory(loader),
    )
    assert len(report.samples) == 1
    assert report.samples[0].symbol == "600519.SH"
    assert report.samples[0].as_of == "2026-01-16"
    assert report.samples[0].contributions  # 逐维度贡献是校准的输入


def test_probe_never_sees_data_after_as_of():
    """防前视：as_of 之后的暴涨不得影响判定。

    做法是把它与一份「未来数据被截掉」的行情对比——两者判定必须一致。
    """
    from api.services.signal_replay_service import replay_one

    full = _loader(default_rows=_rising_with_future_spike())
    truncated_rows = [r for r in _rising_with_future_spike() if r[0] <= "2026-01-16"]
    truncated = _loader(default_rows=truncated_rows)

    a = replay_one(symbol="600519.SH", as_of="2026-01-16", loader=full,
                   ports_factory=_ports_factory(full))
    b = replay_one(symbol="600519.SH", as_of="2026-01-16", loader=truncated,
                   ports_factory=_ports_factory(truncated))

    assert a is not None and b is not None
    assert a.state == b.state
    assert a.margin == pytest.approx(b.margin)
    assert a.confidence == b.confidence


def test_outcome_uses_the_window_after_as_of():
    """兑现窗口必须取 as_of 之后的数据：上面的暴涨应使 washout 兑现。"""
    from api.services.signal_replay_service import replay_one

    loader = _loader(default_rows=_rising_with_future_spike())
    sample = replay_one(
        symbol="600519.SH", as_of="2026-01-16", loader=loader,
        ports_factory=_ports_factory(loader), horizon_days=5,
    )
    assert sample is not None
    assert sample.evaluable is True
    assert sample.threshold_hit is True


def test_daily_only_ports_yield_only_abstentions():
    """只装配日线端口 → 相对强度不可得（核心维度）→ 全部弃权。

    这个后果**不会报错**，只表现为「校准一直没有样本」。把这条行为钉住，
    是为了让「回放跑通了但样本为零」能被立刻识别，而不是被当成数据问题查很久。
    """
    from api.services.signal_replay_service import replay_one

    sample = replay_one(
        symbol="600519.SH", as_of="2026-01-16",
        loader=_loader(default_rows=_rising_with_future_spike()),
    )
    assert sample is not None
    assert sample.state == "insufficient_evidence"


def test_single_point_failure_does_not_abort_the_batch():
    """单只标的取数失败不得中断整批回放——否则一只停牌股会挡住全部校准样本。"""
    from api.services.signal_replay_service import replay

    good = _rising_with_future_spike()

    def loader(symbol, start, end):
        if symbol == "BAD.SH":
            raise ConnectionError("boom")
        return _frame(good)

    report = replay(
        points=[("600519.SH", "2026-01-16"), ("BAD.SH", "2026-01-16")],
        loader=loader,
        ports_factory=_ports_factory(loader),
    )
    assert len(report.samples) == 1


def test_report_refuses_to_activate_weights_from_offline_samples():
    """权限边界：离线回放只能否决维度，不能激活权重。"""
    from api.services.signal_replay_service import replay

    loader = _loader(default_rows=_rising_with_future_spike())
    report = replay(
        points=[("600519.SH", "2026-01-16")],
        loader=loader,
        ports_factory=_ports_factory(loader),
    )
    assert report.activation is not None
    assert report.activation.allowed is False
    assert report.authority["may_activate_weights"] is False
    assert report.authority["may_reject_dimensions"] is True


def test_report_includes_curve_plateau_and_dimension_power():
    from api.services.signal_replay_service import replay

    # 多个基准日才能形成曲线与切分
    points = [("600519.SH", f"2026-01-{d:02d}") for d in range(5, 17)]
    loader = _loader(default_rows=_rising_with_future_spike())
    report = replay(
        points=points,
        loader=loader,
        ports_factory=_ports_factory(loader),
        thresholds=[0.10, 0.20, 0.30],
        min_plateau_sample=1,
    )
    assert report.samples
    assert len(report.curve) == 3
    assert report.plateau is not None
    assert report.dimension_power
    assert report.train_size + report.holdout_size == len(report.samples)


def test_replay_points_expand_to_cartesian_product():
    from api.services.signal_replay_service import build_replay_points

    points = build_replay_points(
        symbols=["600519.SH", "000001.SZ"], dates=["2026-01-16", "2026-01-19"]
    )
    assert len(points) == 4
    assert ("600519.SH", "2026-01-19") in points


def test_report_is_json_serialisable():
    """报告要能被端点直接返回与落库，因此必须可序列化。"""
    import json

    from api.services.signal_replay_service import replay

    loader = _loader(default_rows=_rising_with_future_spike())
    report = replay(
        points=[("600519.SH", "2026-01-16")],
        loader=loader,
        ports_factory=_ports_factory(loader),
    )
    json.dumps(report.as_dict(), ensure_ascii=False)


def test_persisted_snapshot_contains_the_modulator_factor():
    """落库的权重快照必须含调制因子，否则存档无法重放。

    只存 raw 观测的后果不报错：重算时因子退化为 1.0，得分与原始判定不符，
    而校准统计会因此建立在一个与历史判定不同的模型上。
    """
    from api.database import SignalVerdictDB, get_db_ctx
    from api.services import signal_service
    from tests._signal_fixtures import make_daily
    from tradingagents.signals.context import build_context
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

    ctx = build_context(
        ports=SignalPorts(daily=_Daily(), peer=_Peer()),
        symbol="600519.SH", as_of="2026-01-16", window_days=20,
    )
    result = evaluate_context(ctx)
    verdict_id = signal_service.save_verdict(result, user_id="snapshot-user")

    with get_db_ctx() as db:
        row = db.query(SignalVerdictDB).filter(SignalVerdictDB.id == verdict_id).one()

    assert row.modulator.get("factor") == pytest.approx(result.modulators[0].factor)
    assert row.modulator.get("key") == "position"
    assert row.modulator.get("raw")

    # 用存档重算，必须与原始得分一致
    from tradingagents.signals.replay import replay_score

    replayed = replay_score(
        components=row.contributions["components"],
        modulator_factor=float(row.modulator["factor"]),
    )
    assert replayed["washout"] == pytest.approx(result.score_washout, rel=1e-9)
