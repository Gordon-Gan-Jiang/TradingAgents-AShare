"""M6 回测测试：run_backtest 在合成面板上的前瞻超额收益验证。"""

from __future__ import annotations

import os
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from tradingagents.dataflows.mainline_backtest import run_backtest, run_mainline_backtest


def _synthetic_panel(n_up=5, n_flat=5, n_down=10, days=80, up_drift=0.01, down_drift=0.01):
    dates = pd.bdate_range("2026-01-01", periods=days).strftime("%Y-%m-%d")
    t = np.arange(days)
    cols: dict[str, np.ndarray] = {}
    for i in range(n_up):
        cols[f"UP{i}"] = 100 * (1 + up_drift) ** t * (1 + 0.05 * i)
    for i in range(n_flat):
        cols[f"FLAT{i}"] = np.full(days, 100 + i)
    for i in range(n_down):
        cols[f"DN{i}"] = 100 * (1 - down_drift) ** t
    panel = pd.DataFrame(cols, index=dates)
    bench = pd.Series(3000.0, index=dates)  # 基准走平
    return panel, bench


def test_run_backtest_detects_edge():
    panel, bench = _synthetic_panel()
    result = run_backtest(panel, bench, top_n=5, horizon=5, warmup=10)
    s = result["summary"]
    assert s["n_days"] > 10
    assert s["top"]["mean"] > 0.01            # 上行板块前瞻超额为正
    assert s["bottom"]["mean"] < -0.01        # 下行板块前瞻超额为负
    assert s["edge_top_minus_bottom"] > 0.02  # 规则层有显著边际
    assert s["top"]["hit_rate"] > 0.6


def test_run_backtest_no_edge_on_random_panel():
    rng = np.random.default_rng(42)
    dates = pd.bdate_range("2026-01-01", periods=80).strftime("%Y-%m-%d")
    panel = pd.DataFrame(
        {f"B{i}": 100 * np.cumprod(1 + rng.normal(0, 0.01, 80)) for i in range(20)},
        index=dates,
    )
    bench = pd.Series(3000.0, index=dates)
    result = run_backtest(panel, bench, top_n=5, horizon=5, warmup=10)
    s = result["summary"]
    assert abs(s["edge_top_minus_bottom"]) < 0.05  # 随机市场无系统性边际


def test_run_backtest_too_short_panel_raises():
    panel, bench = _synthetic_panel(days=10)
    with pytest.raises(ValueError):
        run_backtest(panel, bench, top_n=5, horizon=5, warmup=10)


def test_run_mainline_backtest_end_to_end(tmp_path, monkeypatch):
    panel, bench = _synthetic_panel()
    monkeypatch.setenv("TA_RESULTS_DIR", str(tmp_path))
    with patch(
        "tradingagents.dataflows.mainline_backtest.fetch_industry_panel", return_value=panel
    ), patch(
        "tradingagents.dataflows.mainline_backtest.fetch_benchmark_close", return_value=bench
    ):
        result = run_mainline_backtest(end_date="2026-05-15", days=60, top_n=5, horizon=5, cache=False)
    assert result["summary"]["edge_top_minus_bottom"] > 0
    assert "output_path" in result
    assert os.path.exists(result["output_path"])
