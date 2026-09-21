"""Tests for ``api/services/t1_stats.py``.

The numeric assertions double as a regression guard on the audit's own math: the
ceiling table, the sample-size floor, and the cost break-even figures quoted in
``docs/short-horizon-alpha-probe.md`` are reproduced here, so a silent change to
these formulas cannot pass unnoticed.
"""

import math

import pytest

from api.services.t1_stats import (
    Stats,
    bootstrap_ci_by_date,
    concentration,
    cost_breakeven_ic,
    day_clustered_stats,
    daily_mean_and_count,
    dedupe_by_window,
    excess_hit_rate,
    leave_one_out_benchmark,
    leave_one_out_excess,
    hit_rate,
    implied_accuracy_from_ic,
    norm_ppf,
    ranks,
    required_n_for_accuracy,
    spearman,
    summarize_accuracy,
    window_key,
)


# --- norm_ppf -----------------------------------------------------------------


def test_norm_ppf_known_values():
    assert norm_ppf(0.975) == pytest.approx(1.959963985, abs=1e-6)
    assert norm_ppf(0.8) == pytest.approx(0.8416212336, abs=1e-6)
    assert norm_ppf(0.5) == pytest.approx(0.0, abs=1e-9)
    assert norm_ppf(0.025) == pytest.approx(-1.959963985, abs=1e-6)


def test_norm_ppf_symmetry():
    for p in (0.001, 0.01, 0.1, 0.3):
        assert norm_ppf(p) == pytest.approx(-norm_ppf(1 - p), abs=1e-6)


def test_norm_ppf_rejects_out_of_range():
    with pytest.raises(ValueError):
        norm_ppf(0.0)
    with pytest.raises(ValueError):
        norm_ppf(1.0)
    with pytest.raises(ValueError):
        norm_ppf(-0.1)


# --- ceiling / power / cost ---------------------------------------------------


@pytest.mark.parametrize(
    "ic,expected",
    [
        (0.05, 0.5159),
        (0.10, 0.5319),
        (0.20, 0.5641),
        (0.30, 0.5970),
    ],
)
def test_implied_accuracy_reproduces_audit_table(ic, expected):
    assert round(implied_accuracy_from_ic(ic), 4) == expected


def test_implied_accuracy_zero_ic_is_coin_flip():
    assert implied_accuracy_from_ic(0.0) == pytest.approx(0.5)


def test_implied_accuracy_negative_ic_is_below_half():
    assert implied_accuracy_from_ic(-0.10) < 0.5


def test_implied_accuracy_clamps_extreme_ic():
    assert implied_accuracy_from_ic(5.0) == pytest.approx(1.0)
    assert implied_accuracy_from_ic(-5.0) == pytest.approx(0.0)


def test_implied_accuracy_nan_returns_half():
    assert implied_accuracy_from_ic(float("nan")) == 0.5


def test_required_n_matches_audit_sample_size_floor():
    """55% vs 50% needs ~784 independent samples; 53.35% needs ~1747."""
    assert required_n_for_accuracy(0.55) == 783
    assert required_n_for_accuracy(0.5335) == 1747


def test_required_n_monotonic_in_edge():
    near = required_n_for_accuracy(0.51)
    far = required_n_for_accuracy(0.60)
    assert near > far > 0


def test_required_n_zero_when_no_edge():
    assert required_n_for_accuracy(0.50) == 0
    assert required_n_for_accuracy(0.49) == 0


def test_required_n_validates_probabilities():
    with pytest.raises(ValueError):
        required_n_for_accuracy(1.5)


@pytest.mark.parametrize(
    "sigma,expected",
    [
        (0.0307, 0.081),
        (0.0563, 0.044),
        (0.0747, 0.033),
        (0.1084, 0.023),
    ],
)
def test_cost_breakeven_reproduces_table_f(sigma, expected):
    assert round(cost_breakeven_ic(0.0025, sigma), 3) == expected


def test_cost_breakeven_infinite_for_zero_sigma():
    assert cost_breakeven_ic(0.0025, 0.0) == float("inf")
    assert cost_breakeven_ic(0.0025, float("nan")) == float("inf")


# --- rank statistics ----------------------------------------------------------


def test_ranks_no_ties():
    assert ranks([3.0, 1.0, 2.0]) == [3.0, 1.0, 2.0]


def test_ranks_with_ties_share_mean_rank():
    # 1,1 -> ranks 1 and 2 -> both 1.5 ; 3 -> 3
    assert ranks([1.0, 1.0, 3.0]) == [1.5, 1.5, 3.0]


def test_spearman_perfect_monotonic():
    x = [1, 2, 3, 4, 5]
    y = [10, 20, 30, 40, 50]
    assert spearman(x, y) == pytest.approx(1.0)


def test_spearman_perfect_inverse():
    x = [1, 2, 3, 4, 5]
    y = [50, 40, 30, 20, 10]
    assert spearman(x, y) == pytest.approx(-1.0)


def test_spearman_rank_based_not_linear():
    """A monotonic but non-linear relation is still a perfect rank correlation."""
    assert spearman([1, 2, 3, 4], [1, 4, 9, 16]) == pytest.approx(1.0)


def test_spearman_known_value():
    # ranks 1,2,3,4 vs 2,1,4,3 -> Pearson on ranks = 0.6
    assert spearman([1, 2, 3, 4], [2, 1, 4, 3]) == pytest.approx(0.6)


def test_spearman_undefined_cases():
    assert spearman([1, 2], [1, 2]) is None
    assert spearman([1, 1, 1, 1], [1, 2, 3, 4]) is None


def test_spearman_skips_none_and_nan():
    assert spearman([1, None, 3, None, 5], [2, 9, 6, 9, 10]) == pytest.approx(1.0)


# --- window_key / dedupe ------------------------------------------------------


def test_window_key_normalizes_symbol_and_truncates_date():
    assert window_key("600519.sh", "2026-01-05T00:00:00", "3") == (
        "600519.SH",
        "2026-01-05",
        3,
    )


def test_dedupe_collapses_identical_windows():
    rows = [
        {"symbol": "600519.SH", "signal_date": "2026-01-05", "window_days": 1,
         "direction_bucket": "BULLISH", "label_correct": True},
        {"symbol": "600519.SH", "signal_date": "2026-01-05", "window_days": 1,
         "direction_bucket": "BULLISH", "label_correct": True},
        {"symbol": "300750.SZ", "signal_date": "2026-01-05", "window_days": 1,
         "direction_bucket": "BEARISH", "label_correct": False},
    ]
    out = dedupe_by_window(rows)
    assert len(out) == 2
    first = out[0]
    assert first["n_raw"] == 2
    assert first["conflict"] is False
    assert first["direction_bucket"] == "BULLISH"


def test_dedupe_flags_conflicting_window():
    """A window the system called both ways is not one prediction."""
    rows = [
        {"symbol": "600519.SH", "signal_date": "2026-01-05", "window_days": 1,
         "direction_bucket": "BULLISH", "label_correct": True},
        {"symbol": "600519.SH", "signal_date": "2026-01-05", "window_days": 1,
         "direction_bucket": "BEARISH", "label_correct": False},
    ]
    out = dedupe_by_window(rows)
    assert len(out) == 1
    assert out[0]["conflict"] is True
    assert out[0]["direction_bucket"] is None


def test_dedupe_separates_different_horizons():
    """Same symbol and date but different horizon = different window."""
    rows = [
        {"symbol": "600519.SH", "signal_date": "2026-01-05", "window_days": 1,
         "direction_bucket": "BULLISH", "label_correct": True},
        {"symbol": "600519.SH", "signal_date": "2026-01-05", "window_days": 5,
         "direction_bucket": "BULLISH", "label_correct": True},
    ]
    assert len(dedupe_by_window(rows)) == 2


def test_dedupe_preserves_input_order():
    rows = [
        {"symbol": "B", "signal_date": "2026-01-05", "window_days": 1, "direction_bucket": "BULLISH"},
        {"symbol": "A", "signal_date": "2026-01-05", "window_days": 1, "direction_bucket": "BULLISH"},
    ]
    out = dedupe_by_window(rows)
    assert [row["symbol"] for row in out] == ["B", "A"]


def test_dedupe_handles_missing_direction():
    rows = [
        {"symbol": "A", "signal_date": "2026-01-01", "window_days": 1, "direction_bucket": None},
        {"symbol": "A", "signal_date": "2026-01-01", "window_days": 1, "direction_bucket": None},
    ]
    out = dedupe_by_window(rows)
    assert out[0]["direction_bucket"] is None
    assert out[0]["conflict"] is False


# --- day_clustered_stats ------------------------------------------------------


def test_day_clustered_mean_weights_dates_equally():
    """A 3-report day must not outvote a 1-report day."""
    pairs = [("d1", 1.0), ("d1", 1.0), ("d1", 1.0), ("d2", 0.0)]
    stats = day_clustered_stats(pairs)
    assert stats.mean == pytest.approx(0.5)  # not 0.75
    assert stats.n_obs == 4
    assert stats.n_days == 2
    assert stats.se == pytest.approx(0.5)
    assert stats.t == pytest.approx(1.0)


def test_pooled_rate_differs_from_clustered_mean():
    """Documents exactly the bug that pooled aggregation hid."""
    pairs = [("d1", 1.0)] * 100 + [("d2", 0.0)]
    pooled = sum(value for _, value in pairs) / len(pairs)
    clustered = day_clustered_stats(pairs).mean
    assert pooled > 0.99
    assert clustered == pytest.approx(0.5)


def test_day_clustered_single_date_has_no_t():
    stats = day_clustered_stats([("d1", 1.0), ("d1", 0.0)])
    assert stats.mean == pytest.approx(0.5)
    assert stats.t is None
    assert stats.se is None
    assert "single date" in stats.note


def test_day_clustered_empty():
    stats = day_clustered_stats([])
    assert stats.mean is None
    assert stats.n_obs == 0
    assert stats.note == "no observations"


def test_day_clustered_skips_none_and_nan():
    stats = day_clustered_stats([("d1", 1.0), ("d1", None), ("d2", float("nan"))])
    assert stats.n_obs == 1
    assert stats.n_days == 1


def test_day_clustered_ci_brackets_mean():
    pairs = [(f"d{i}", float(i % 2)) for i in range(40)]
    stats = day_clustered_stats(pairs)
    assert stats.ci_low < stats.mean < stats.ci_high


def test_stats_as_dict_rounds_and_omits_missing():
    stats = day_clustered_stats([("d1", 1.0), ("d2", 0.0)])
    payload = stats.as_dict()
    assert payload["n_days"] == 2
    assert payload["effective_n"] == 2
    assert isinstance(payload["mean"], float)


def test_stats_as_dict_keys_fit_metric_column():
    """Metric names are stored in a String(32) column."""
    stats = day_clustered_stats([("d1", 1.0), ("d2", 0.0)])
    for key in stats.as_dict():
        assert len(key) <= 32, key


def test_stats_defaults_are_all_none():
    stats = Stats()
    assert stats.mean is None and stats.t is None and stats.effective_n == 0


# --- bootstrap ----------------------------------------------------------------


def test_bootstrap_is_deterministic():
    pairs = [(f"d{i}", float(i % 3)) for i in range(30)]
    first = bootstrap_ci_by_date(pairs, n_boot=500, seed=7)
    second = bootstrap_ci_by_date(pairs, n_boot=500, seed=7)
    assert first == second


def test_bootstrap_low_below_high():
    pairs = [(f"d{i}", float(i % 2)) for i in range(40)]
    low, high = bootstrap_ci_by_date(pairs, n_boot=500)
    assert low is not None and high is not None
    assert low <= high


def test_bootstrap_single_date_returns_none():
    assert bootstrap_ci_by_date([("d1", 1.0), ("d1", 0.0)]) == (None, None)


def test_bootstrap_empty_returns_none():
    assert bootstrap_ci_by_date([]) == (None, None)


# --- concentration ------------------------------------------------------------


def test_concentration_reports_top_share():
    rows = [{"symbol": "A"}] * 6 + [{"symbol": "B"}] * 3 + [{"symbol": "C"}]
    result = concentration(rows)
    assert result["unique_symbols"] == 3
    assert result["top_symbol_share"] == pytest.approx(0.6)
    assert result["top5_symbol_share"] == pytest.approx(1.0)


def test_concentration_empty():
    result = concentration([])
    assert result["unique_symbols"] == 0
    assert result["top_symbol_share"] is None


# --- hit rate -----------------------------------------------------------------


def test_hit_rate_basic():
    rows = [{"label_correct": True}, {"label_correct": True}, {"label_correct": False}]
    result = hit_rate(rows)
    assert result["n"] == 3
    assert result["hits"] == 2
    assert result["rate"] == pytest.approx(2 / 3)


def test_hit_rate_skips_unlabelled():
    rows = [{"label_correct": True}, {"label_correct": None}, {}]
    assert hit_rate(rows)["n"] == 1


def test_hit_rate_empty():
    assert hit_rate([]) == {"n": 0, "hits": 0, "rate": None}


def test_excess_hit_rate_subtracts_benchmark():
    rows = [
        {"signal_date": "d1", "forward_return": 0.03},
        {"signal_date": "d1", "forward_return": 0.04},
    ]
    stats = excess_hit_rate(rows, {"d1": 0.035})
    assert stats.mean == pytest.approx(0.0)


def test_excess_hit_rate_ignores_dates_without_benchmark():
    rows = [
        {"signal_date": "d1", "forward_return": 0.03},
        {"signal_date": "d9", "forward_return": 0.09},
    ]
    stats = excess_hit_rate(rows, {"d1": 0.01})
    assert stats.n_obs == 1


# --- summarize_accuracy -------------------------------------------------------


def _window(symbol, date, bucket, correct, conflict=False):
    return {
        "symbol": symbol,
        "signal_date": date,
        "direction_bucket": bucket,
        "label_correct": correct,
        "conflict": conflict,
    }


def test_summarize_accuracy_separates_directions():
    rows = [
        _window("A", "d1", "BULLISH", True),
        _window("B", "d1", "BULLISH", False),
        _window("C", "d2", "BEARISH", True),
    ]
    result = summarize_accuracy(rows, boostrap_samples=200)
    assert result["effective_n"] == 3
    assert result["bullish_n"] == 2
    assert result["bearish_n"] == 1
    assert result["bullish_rate"] == pytest.approx(0.5)
    assert result["bearish_rate"] == pytest.approx(1.0)


def test_summarize_accuracy_excludes_conflicts():
    rows = [
        _window("A", "d1", "BULLISH", True),
        _window("B", "d2", None, True, conflict=True),
    ]
    result = summarize_accuracy(rows, boostrap_samples=200)
    assert result["effective_n"] == 1
    assert result["conflict_n"] == 1
    assert result["conflict_rate"] == pytest.approx(0.5)


def test_summarize_accuracy_excludes_neutral_windows():
    rows = [
        _window("A", "d1", "BULLISH", True),
        _window("B", "d1", "NEUTRAL", True),
    ]
    result = summarize_accuracy(rows, boostrap_samples=200)
    assert result["effective_n"] == 1


def test_summarize_accuracy_reports_interval_and_days():
    rows = [_window(f"S{i}", f"d{i}", "BULLISH", i % 2 == 0) for i in range(20)]
    result = summarize_accuracy(rows, boostrap_samples=300)
    assert result["bullish_n_days"] == 20
    assert result["bullish_ci_low"] is not None
    assert result["bullish_ci_low"] <= result["bullish_ci_high"]


def test_summarize_accuracy_empty_is_safe():
    result = summarize_accuracy([])
    assert result["effective_n"] == 0
    assert result["conflict_rate"] is None
    assert result["bullish_rate"] is None


def test_summarize_accuracy_keys_fit_metric_column():
    rows = [_window("A", "d1", "BULLISH", True), _window("B", "d2", "BEARISH", False)]
    for key in summarize_accuracy(rows, boostrap_samples=100):
        assert len(key) <= 32, key


def test_ceiling_and_required_n_are_consistent():
    """If measured accuracy sits at the ceiling for its IC, n must be large."""
    ic = 0.05
    ceiling = implied_accuracy_from_ic(ic)
    assert required_n_for_accuracy(ceiling) > 500
    assert math.isclose(ceiling, 0.5159, abs_tol=1e-4)


# ---------------------------------------------------------------------------
# P5: leave-one-out benchmark primitives
# ---------------------------------------------------------------------------

class TestLeaveOneOutBenchmark:
    """The benchmark must exclude the row it grades, or it benchmarks itself."""

    def test_exact_leave_one_out_arithmetic(self):
        """LOO excess equals the value minus the mean of the OTHERS."""
        rows = [
            {"signal_date": "d1", "forward_return": 0.01},
            {"signal_date": "d1", "forward_return": 0.03},
        ]
        means, counts = daily_mean_and_count(rows)
        assert means["d1"] == 0.02
        assert counts["d1"] == 2
        # for the 0.01 row the benchmark is the other row alone (0.03)
        assert leave_one_out_excess(rows[0], means, counts) == pytest.approx(-0.02)
        assert leave_one_out_excess(rows[1], means, counts) == pytest.approx(0.02)

    def test_three_rows_use_the_mean_of_the_other_two(self):
        rows = [
            {"signal_date": "d", "forward_return": 1.0},
            {"signal_date": "d", "forward_return": 2.0},
            {"signal_date": "d", "forward_return": 6.0},
        ]
        means, counts = daily_mean_and_count(rows)
        # inclusive mean = 3.0; for the 6.0 row: (3*3 - 6)/2 = 1.5
        assert leave_one_out_excess(rows[2], means, counts) == pytest.approx(6.0 - 1.5)

    def test_single_row_day_has_no_benchmark(self):
        """Returning the row's own value would manufacture a 50% excess hit rate."""
        rows = [{"signal_date": "solo", "forward_return": 0.5}]
        means, counts = daily_mean_and_count(rows)
        assert leave_one_out_excess(rows[0], means, counts) is None
        assert leave_one_out_benchmark(rows) == {}

    def test_benchmark_omits_peerless_dates(self):
        rows = [
            {"signal_date": "a", "forward_return": 1.0},
            {"signal_date": "a", "forward_return": 3.0},
            {"signal_date": "b", "forward_return": 9.0},
        ]
        bench = leave_one_out_benchmark(rows)
        assert bench == {"a": 2.0}
        assert "b" not in bench

    def test_missing_and_nan_values_are_skipped(self):
        rows = [
            {"signal_date": "d", "forward_return": 1.0},
            {"signal_date": "d", "forward_return": None},
            {"signal_date": "d", "forward_return": float("nan")},
            {"signal_date": "d", "forward_return": 3.0},
        ]
        means, counts = daily_mean_and_count(rows)
        assert counts["d"] == 2, "None 与 NaN 不能计入基准"
        assert means["d"] == pytest.approx(2.0)

    def test_unparseable_values_are_skipped_not_crashed(self):
        rows = [
            {"signal_date": "d", "forward_return": 1.0},
            {"signal_date": "d", "forward_return": "n/a"},
            {"signal_date": "d", "forward_return": 3.0},
        ]
        means, counts = daily_mean_and_count(rows)
        assert counts["d"] == 2
        assert means["d"] == pytest.approx(2.0)

    def test_rows_without_a_date_are_ignored(self):
        rows = [
            {"signal_date": None, "forward_return": 1.0},
            {"signal_date": "d", "forward_return": 1.0},
            {"signal_date": "d", "forward_return": 3.0},
        ]
        means, counts = daily_mean_and_count(rows)
        assert set(means) == {"d"}
        assert counts["d"] == 2

    def test_excess_hit_rate_name_is_documented_as_a_return(self):
        """`excess_hit_rate` returns mean excess RETURN, not a hit rate."""
        doc = excess_hit_rate.__doc__ or ""
        assert "mean excess return" in doc.lower()
        assert "leave_one_out_benchmark" in doc
