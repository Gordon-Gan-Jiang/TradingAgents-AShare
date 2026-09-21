"""Statistics for T+1 judgment quality — pure functions, stdlib only.

Why this module exists
----------------------
The published accuracy metrics were **not measuring what they claimed**:

* ``insights_t1_service`` computed ``accuracy_pct = correct / len(day_rows) * 100``
  where ``day_rows`` counted one row per *report*. Several users analysing the same
  symbol on the same day share one price window, so one price move was counted many
  times. Measured: 4601 evaluated rows collapse to ~1583 unique price windows, with
  100 rows on a single key and 31.5% of all rows from just two symbols.
* Errors were treated as independent when they are clustered by date. The same
  signal measured pooled gave ``t = -0.01`` and date-clustered gave ``t = -0.81``;
  for one look-ahead variant the pooled and clustered conclusions even had opposite
  signs.
* No effective sample size, confidence interval, or significance was reported, so a
  48% reading on n=12 looked the same as a 48% reading on n=1200.

Every metric produced here is therefore reported together with ``effective_n`` (the
number of *independent* windows), ``n_days``, and a date-clustered confidence
interval.

The ceiling math
----------------
For a cross-sectional signal with information coefficient ``IC`` the best achievable
directional hit rate is

    P(correct) = 0.5 + asin(IC) / pi

so ``IC = 0.05 -> 51.59%``, ``IC = 0.10 -> 53.19%``, ``IC = 0.20 -> 56.41%``.
A gross hit rate above 50% is therefore *not* evidence of an edge unless the IC
supports it. :func:`implied_accuracy_from_ic` and :func:`required_n_for_accuracy`
exist so that claim can be checked instead of asserted.

Costs
-----
Round-trip cost ``c`` against return volatility ``sigma`` implies a break-even
``IC > c / sigma``; with ``c = 0.0025`` a 3.07% daily volatility needs ``IC > 0.081``,
far above anything measured. :func:`cost_breakeven_ic` reports this.

Dependencies: standard library only. ``scipy`` is deliberately not used — it is not
installed in this project's venv, and the two functions that would need it
(:func:`norm_ppf`, :func:`spearman`) are short, exactly-specified, and unit-tested
against known values instead.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "Stats",
    "norm_ppf",
    "implied_accuracy_from_ic",
    "required_n_for_accuracy",
    "cost_breakeven_ic",
    "readable_alpha_from_ic",
    "spearman",
    "ranks",
    "window_key",
    "dedupe_by_window",
    "day_clustered_stats",
    "bootstrap_ci_by_date",
    "concentration",
    "excess_hit_rate",
    "hit_rate",
    "summarize_accuracy",
]


# --- normal quantile (Acklam's rational approximation) ----------------------

# Coefficients from Peter Acklam's inverse normal CDF approximation,
# relative error < 1.15e-9 over the whole open interval (0, 1).
_A = (
    -3.969683028665376e01,
    2.209460984245205e02,
    -2.759285104469687e02,
    1.383577518672690e02,
    -3.066479806614716e01,
    2.506628277459239e00,
)
_B = (
    -5.447609879822406e01,
    1.615858368580409e02,
    -1.556989798598866e02,
    6.680131188771972e01,
    -1.328068155288572e01,
)
_C = (
    -7.784894002430293e-03,
    -3.223964580411365e-01,
    -2.400758277161838e00,
    -2.549732539343734e00,
    4.374664141464968e00,
    2.938163982698783e00,
)
_D = (
    7.784695709041462e-03,
    3.224671290700398e-01,
    2.445134137142996e00,
    3.754408661907416e00,
)
_P_LOW = 0.02425
_P_HIGH = 1.0 - _P_LOW


def norm_ppf(p: float) -> float:
    """Inverse standard-normal CDF (quantile function).

    >>> round(norm_ppf(0.975), 6)
    1.959964
    >>> round(norm_ppf(0.8), 4)
    0.8416
    """
    if not 0.0 < p < 1.0:
        raise ValueError(f"p must be in (0, 1), got {p!r}")

    if p < _P_LOW:
        q = math.sqrt(-2.0 * math.log(p))
        return (
            ((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5]
        ) / ((((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1.0)

    if p <= _P_HIGH:
        q = p - 0.5
        r = q * q
        return (
            (((((_A[0] * r + _A[1]) * r + _A[2]) * r + _A[3]) * r + _A[4]) * r + _A[5])
            * q
            / (((((_B[0] * r + _B[1]) * r + _B[2]) * r + _B[3]) * r + _B[4]) * r + 1.0)
        )

    q = math.sqrt(-2.0 * math.log(1.0 - p))
    return -(
        ((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5]
    ) / ((((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1.0)


# --- ceiling / power / cost math --------------------------------------------


def implied_accuracy_from_ic(ic: float) -> float:
    """Best achievable directional hit rate for a signal with this IC.

    ``P(correct) = 0.5 + asin(IC) / pi``, clamped to ``[0, 1]``.

    Reference values: IC 0.05 -> 0.5159, IC 0.10 -> 0.5319, IC 0.20 -> 0.5641,
    IC 0.30 -> 0.5970.
    """
    if ic != ic:  # NaN
        return 0.5
    clipped = max(-1.0, min(1.0, float(ic)))
    return 0.5 + math.asin(clipped) / math.pi


def required_n_for_accuracy(
    target: float,
    baseline: float = 0.5,
    alpha: float = 0.05,
    power: float = 0.8,
) -> int:
    """Independent samples needed to distinguish ``target`` from ``baseline``.

    Two-sided one-sample proportion test:

        n = ( z_{1-a/2} * sqrt(p0(1-p0)) + z_{power} * sqrt(p(1-p)) )^2 / (p - p0)^2

    Reference values: 0.55 vs 0.5 -> 783; 0.5335 vs 0.5 -> 1746. These reproduce
    the sample-size floor quoted in the audit.

    Returns 0 when ``target`` is not above ``baseline`` (nothing to detect).
    """
    edge = float(target) - float(baseline)
    if edge <= 0:
        return 0
    if not 0.0 < float(baseline) < 1.0 or not 0.0 < float(target) < 1.0:
        raise ValueError("target and baseline must be probabilities in (0, 1)")

    z_alpha = norm_ppf(1.0 - alpha / 2.0)
    z_power = norm_ppf(power)
    p0 = float(baseline)
    p1 = float(target)

    numerator = (
        z_alpha * math.sqrt(p0 * (1.0 - p0)) + z_power * math.sqrt(p1 * (1.0 - p1))
    ) ** 2
    return int(math.ceil(numerator / (edge ** 2)))


def cost_breakeven_ic(cost: float, sigma: float) -> float:
    """IC required for expected edge to cover round-trip ``cost``.

    ``IC > cost / sigma``. With ``cost = 0.0025``: sigma 3.07% -> 0.081,
    sigma 5.63% -> 0.044, sigma 7.47% -> 0.033, sigma 10.84% -> 0.023.
    """
    if sigma is None or sigma != sigma or sigma <= 0:
        return float("inf")
    return float(cost) / float(sigma)


def readable_alpha_from_ic(ic: float) -> float:
    """Expected excess return per unit of cross-sectional volatility.

    A convenience scaling for reporting: ``IC`` is the correlation between the
    signal and the next-period return, so the long-short spread scales with it.
    """
    return float(ic)


# --- rank statistics (no scipy) ---------------------------------------------


def ranks(values: Sequence[float]) -> List[float]:
    """Average ranks (1-based), with ties sharing their mean rank."""
    indexed = sorted(
        ((value, index) for index, value in enumerate(values)),
        key=lambda item: item[0],
    )
    out = [0.0] * len(values)
    position = 0
    total = len(indexed)
    while position < total:
        end = position
        while end + 1 < total and indexed[end + 1][0] == indexed[position][0]:
            end += 1
        average = (position + end) / 2.0 + 1.0
        for slot in range(position, end + 1):
            out[indexed[slot][1]] = average
        position = end + 1
    return out


def spearman(x: Sequence[float], y: Sequence[float]) -> Optional[float]:
    """Spearman rank correlation via Pearson on ranks.

    Returns ``None`` when the correlation is undefined (fewer than 3 pairs, or
    either side has zero rank variance).
    """
    pairs = [
        (float(a), float(b))
        for a, b in zip(x, y)
        if a is not None and b is not None and a == a and b == b
    ]
    if len(pairs) < 3:
        return None

    rx = ranks([item[0] for item in pairs])
    ry = ranks([item[1] for item in pairs])
    n = len(pairs)
    mean_x = sum(rx) / n
    mean_y = sum(ry) / n

    cov = sum((rx[i] - mean_x) * (ry[i] - mean_y) for i in range(n))
    var_x = sum((value - mean_x) ** 2 for value in rx)
    var_y = sum((value - mean_y) ** 2 for value in ry)
    if var_x <= 0 or var_y <= 0:
        return None
    return cov / math.sqrt(var_x * var_y)


# --- deduplication by price window ------------------------------------------


def window_key(symbol: Any, signal_date: Any, window_days: Any) -> Tuple[str, str, int]:
    """The identity of a *price window*: one symbol observed over one horizon.

    Two reports on the same symbol and date with the same horizon do not provide
    two independent observations — they share the same forward return. This key is
    what makes that explicit.
    """
    return (
        str(symbol or "").strip().upper(),
        str(signal_date or "").strip()[:10],
        int(window_days or 1),
    )


def dedupe_by_window(
    rows: Iterable[Dict[str, Any]],
    *,
    symbol_key: str = "symbol",
    date_key: str = "signal_date",
    window_key_name: str = "window_days",
    direction_key: str = "direction_bucket",
) -> List[Dict[str, Any]]:
    """Collapse rows to one per price window.

    Each output row carries:

    ``window``
        the :func:`window_key` tuple.
    ``direction_bucket``
        the unanimous direction, or ``None`` when the raw rows disagreed.
    ``conflict``
        ``True`` when the raw rows for this window disagreed. Such a window is not
        a single prediction and must be excluded from accuracy, not silently
        resolved by majority or by row order.
    ``n_raw``
        how many raw rows collapsed into it, so the inflation factor is visible.

    Raw value keys are carried through from the first row seen for the window.
    """
    grouped: Dict[Tuple[str, str, int], List[Dict[str, Any]]] = {}
    order: List[Tuple[str, str, int]] = []

    for row in rows:
        key = window_key(row.get(symbol_key), row.get(date_key), row.get(window_key_name))
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(row)

    out: List[Dict[str, Any]] = []
    for key in order:
        members = grouped[key]
        directions = {
            str(item.get(direction_key)).strip().upper()
            for item in members
            if item.get(direction_key) not in (None, "")
        }
        merged = dict(members[0])
        merged["window"] = key
        merged["n_raw"] = len(members)
        if len(directions) == 1:
            merged[direction_key] = directions.pop()
            merged["conflict"] = False
        elif not directions:
            merged[direction_key] = None
            merged["conflict"] = False
        else:
            merged[direction_key] = None
            merged["conflict"] = True
        out.append(merged)
    return out


# --- clustered inference ----------------------------------------------------


@dataclass
class Stats:
    """A point estimate with clustering-aware uncertainty.

    ``n_obs`` counts rows; ``n_days`` counts independent date clusters and is the
    quantity that governs significance. ``effective_n`` is ``n_obs`` after
    deduplication by price window, which is the honest denominator for a hit rate.
    """

    mean: Optional[float] = None
    se: Optional[float] = None
    t: Optional[float] = None
    ci_low: Optional[float] = None
    ci_high: Optional[float] = None
    n_obs: int = 0
    n_days: int = 0
    effective_n: int = 0
    unique_symbols: int = 0
    top_symbol_share: Optional[float] = None
    note: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        """Flat mapping of scalar fields, safe to write as metric values.

        Keys are kept short (<= 32 chars) because metric names are persisted in a
        ``String(32)`` column.
        """
        out: Dict[str, Any] = {
            "n_obs": self.n_obs,
            "n_days": self.n_days,
            "effective_n": self.effective_n,
            "unique_symbols": self.unique_symbols,
        }
        for name in ("mean", "se", "t", "ci_low", "ci_high", "top_symbol_share"):
            value = getattr(self, name)
            if value is not None:
                out[name] = round(float(value), 6)
        if self.note:
            out["note"] = self.note[:200]
        out.update(self.extra)
        return out


def day_clustered_stats(pairs: Iterable[Tuple[Any, float]]) -> Stats:
    """Mean of ``(day, value)`` pairs with date-clustered standard error.

    The estimator is the mean of per-date means — every date gets equal weight, so
    a day with 100 reports cannot outvote a day with one report. The standard error
    uses the across-date dispersion (``ddof=1``), which is the correct denominator
    when observations within a day are correlated.

    Returns a ``Stats`` with ``t = None`` when there are fewer than 2 dates, because
    a single date carries no information about dispersion.
    """
    buckets: Dict[Any, List[float]] = {}
    for day, value in pairs:
        if value is None or value != value:
            continue
        buckets.setdefault(day, []).append(float(value))

    if not buckets:
        return Stats(note="no observations")

    day_means = [sum(values) / len(values) for values in buckets.values()]
    n_days = len(day_means)
    mean = sum(day_means) / n_days
    n_obs = sum(len(values) for values in buckets.values())

    if n_days < 2:
        return Stats(
            mean=mean,
            se=None,
            t=None,
            ci_low=None,
            ci_high=None,
            n_obs=n_obs,
            n_days=n_days,
            effective_n=n_obs,
            note="single date cluster: dispersion undefined",
        )

    variance = sum((value - mean) ** 2 for value in day_means) / (n_days - 1)
    se = math.sqrt(variance / n_days)
    t_stat = mean / se if se > 0 else None
    ci_low = ci_high = None
    if se > 0:
        half = norm_ppf(0.975) * se
        ci_low, ci_high = mean - half, mean + half

    return Stats(
        mean=mean,
        se=se,
        t=t_stat,
        ci_low=ci_low,
        ci_high=ci_high,
        n_obs=n_obs,
        n_days=n_days,
        effective_n=n_obs,
    )


def bootstrap_ci_by_date(
    pairs: Iterable[Tuple[Any, float]],
    *,
    n_boot: int = 2000,
    alpha: float = 0.05,
    seed: int = 20260916,
) -> Tuple[Optional[float], Optional[float]]:
    """Percentile CI from resampling **dates** with replacement.

    Resampling dates rather than rows respects within-day correlation; resampling
    rows would understate the interval by roughly the square root of the average
    rows-per-day. The RNG is seeded so repeated runs report identical numbers —
    an interval that moves between runs would be indistinguishable from noise.
    """
    buckets: Dict[Any, List[float]] = {}
    for day, value in pairs:
        if value is None or value != value:
            continue
        buckets.setdefault(day, []).append(float(value))

    day_means = [sum(values) / len(values) for values in buckets.values()]
    n_days = len(day_means)
    if n_days < 2:
        return None, None

    rng = random.Random(seed)
    estimates: List[float] = []
    for _ in range(int(n_boot)):
        sample = [day_means[rng.randrange(n_days)] for _ in range(n_days)]
        estimates.append(sum(sample) / n_days)

    estimates.sort()
    lower_index = max(0, min(len(estimates) - 1, int(math.floor((alpha / 2.0) * n_boot))))
    upper_index = max(
        0, min(len(estimates) - 1, int(math.ceil((1.0 - alpha / 2.0) * n_boot)) - 1)
    )
    return estimates[lower_index], estimates[upper_index]


def concentration(rows: Iterable[Dict[str, Any]], *, symbol_key: str = "symbol") -> Dict[str, Any]:
    """How concentrated the sample is in a few symbols.

    Reports the share of rows contributed by the single largest symbol and the top
    five. High values mean the effective sample is far smaller than ``n`` suggests:
    the audit found two symbols supplying 31.5% of all rows.
    """
    counts: Dict[str, int] = {}
    total = 0
    for row in rows:
        symbol = str(row.get(symbol_key) or "").strip().upper()
        if not symbol:
            continue
        counts[symbol] = counts.get(symbol, 0) + 1
        total += 1

    if total == 0:
        return {"unique_symbols": 0, "top_symbol_share": None, "top5_symbol_share": None}

    ordered = sorted(counts.values(), reverse=True)
    return {
        "unique_symbols": len(counts),
        "top_symbol_share": round(ordered[0] / total, 6),
        "top5_symbol_share": round(sum(ordered[:5]) / total, 6),
    }


# --- hit rates --------------------------------------------------------------


def hit_rate(rows: Iterable[Dict[str, Any]], *, correct_key: str = "label_correct") -> Dict[str, Any]:
    """Raw hit rate over the rows given (no dedup — use with deduped rows only)."""
    values = [
        row.get(correct_key)
        for row in rows
        if row.get(correct_key) is not None
    ]
    if not values:
        return {"n": 0, "hits": 0, "rate": None}
    hits = sum(1 for value in values if value)
    return {"n": len(values), "hits": hits, "rate": hits / len(values)}


def wilson_interval(
    hits: int,
    n: int,
    *,
    z: float = 1.959963984540054,
) -> Tuple[Optional[float], Optional[float]]:
    """Wilson score interval for a binomial proportion.

    Used for per-day hit rates, where ``n`` is often 1–5. The normal approximation
    (``p ± z·sqrt(p(1-p)/n)``) is unusable there — it produces intervals that run
    outside [0, 1] and claims zero width when ``p`` is exactly 0 or 1, i.e. it
    reports maximum confidence from a single observation. The Wilson interval stays
    inside [0, 1], never collapses to zero width while ``n`` is finite, and is the
    standard choice for small-sample proportions.

    Returns ``(None, None)`` when ``n <= 0``.

    >>> low, high = wilson_interval(1, 1)
    >>> round(low, 4), round(high, 4)
    (0.2065, 1.0)
    >>> low, high = wilson_interval(500, 1000)
    >>> round(low, 4), round(high, 4)
    (0.4691, 0.5309)
    """
    if n <= 0:
        return None, None
    if hits < 0 or hits > n:
        raise ValueError(f"hits must be within [0, n], got hits={hits!r} n={n!r}")

    p = hits / n
    z2 = z * z
    denominator = 1.0 + z2 / n
    centre = (p + z2 / (2.0 * n)) / denominator
    margin = (z / denominator) * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n))
    return max(0.0, centre - margin), min(1.0, centre + margin)


def deduped_hit_rate(
    rows: Iterable[Dict[str, Any]],
    *,
    correct_key: str = "label_correct",
) -> Dict[str, Any]:
    """Hit rate for an **already deduplicated** cohort, with a Wilson interval.

    ``rows`` must come from :func:`dedupe_by_window`, so ``n`` is the number of
    distinct price windows rather than the number of reports. Conflict windows
    (where the raw rows disagreed on direction) are excluded: the system did not
    make a single call there, so there is nothing to score.

    Returns ``hits``/``n``/``rate`` plus ``ci_low``/``ci_high`` and ``conflict_n``.
    """
    scored = [row for row in rows if not row.get("conflict")]
    values = [row.get(correct_key) for row in scored if row.get(correct_key) is not None]
    hits = sum(1 for value in values if value)
    n = len(values)
    low, high = wilson_interval(hits, n)
    return {
        "n": n,
        "hits": hits,
        "rate": (hits / n) if n else None,
        "ci_low": low,
        "ci_high": high,
        "conflict_n": sum(1 for row in rows if row.get("conflict")),
        "dropped_n": len(scored) - n,
    }


def excess_hit_rate(
    rows: Iterable[Dict[str, Any]],
    benchmark_by_date: Dict[Any, float],
    *,
    date_key: str = "signal_date",
    return_key: str = "forward_return",
) -> Stats:
    """Excess returns over the day's benchmark, with date-clustered standard error.

    .. note::
       The name is misleading — this returns the **mean excess return** (a return,
       in the same units as ``return_key``), not a hit rate. It is kept under this
       name because callers and tests already depend on it; use
       :func:`leave_one_out_benchmark` + :func:`summarize_accuracy` when you want an
       actual *excess hit rate*.

    A bullish call on a day the whole market rose 2% is not evidence of skill, yet
    absolute-return grading counts it as a win. Subtracting the cross-sectional
    mean return for the date removes that. The audit found a systematic gap here:
    bullish accuracy -0.41pp and bearish -0.49pp once excess returns were used.
    """
    pairs: List[Tuple[Any, float]] = []
    for row in rows:
        day = row.get(date_key)
        if day not in benchmark_by_date:
            continue
        ret = row.get(return_key)
        if ret is None:
            continue
        pairs.append((day, float(ret) - float(benchmark_by_date[day])))
    return day_clustered_stats(pairs)


def daily_mean_and_count(
    rows: Iterable[Dict[str, Any]],
    *,
    date_key: str = "signal_date",
    value_key: str = "forward_return",
) -> Tuple[Dict[Any, float], Dict[Any, int]]:
    """Inclusive per-date mean and the count behind it.

    The pair is what :func:`leave_one_out_excess` needs to remove a row exactly
    without a second pass over the data.
    """
    sums: Dict[Any, float] = {}
    counts: Dict[Any, int] = {}
    for row in rows:
        day = row.get(date_key)
        value = row.get(value_key)
        if day is None or value is None:
            continue
        try:
            v = float(value)
        except (TypeError, ValueError):
            continue
        if v != v:  # NaN
            continue
        sums[day] = sums.get(day, 0.0) + v
        counts[day] = counts.get(day, 0) + 1
    return {day: sums[day] / counts[day] for day in sums}, counts


def leave_one_out_benchmark(
    rows: Iterable[Dict[str, Any]],
    *,
    date_key: str = "signal_date",
    value_key: str = "forward_return",
) -> Dict[Any, float]:
    """Per-date cross-sectional mean, restricted to dates with >= 2 usable values.

    Used to de-beta a T+1 judgement: the benchmark is "what the other names we
    looked at that day did", so a call only gets credit for beating its peers.

    Dates with fewer than 2 usable values are omitted entirely: with one name there
    is no peer to compare against, and returning that name's own return would make
    every excess exactly zero, i.e. manufacture a 50% excess hit rate out of
    nothing. Callers must treat a missing date as "not measurable", not as zero.

    (This is the inclusive mean; pair it with :func:`daily_mean_and_count` and
    :func:`leave_one_out_excess` to remove the row's own contribution.)
    """
    means, counts = daily_mean_and_count(rows, date_key=date_key, value_key=value_key)
    return {day: mean for day, mean in means.items() if counts[day] >= 2}


def leave_one_out_excess(
    row: Dict[str, Any],
    benchmark_by_date: Dict[Any, float],
    counts_by_date: Dict[Any, int],
    *,
    date_key: str = "signal_date",
    value_key: str = "forward_return",
) -> Optional[float]:
    """Excess of one row over its date's benchmark, excluding the row itself.

    ``benchmark_by_date`` holds the *inclusive* mean and ``counts_by_date`` the
    number of values behind it, so the leave-one-out mean is exact:

        (n * mean - value) / (n - 1)

    Returns ``None`` when the date has no usable peers.
    """
    day = row.get(date_key)
    value = row.get(value_key)
    if day is None or value is None or day not in benchmark_by_date:
        return None
    n = int(counts_by_date.get(day, 0))
    if n < 2:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v:
        return None
    return v - (n * float(benchmark_by_date[day]) - v) / (n - 1)



def summarize_accuracy(
    rows: Sequence[Dict[str, Any]],
    *,
    direction_key: str = "direction_bucket",
    correct_key: str = "label_correct",
    date_key: str = "signal_date",
    bullish: str = "BULLISH",
    bearish: str = "BEARISH",
    boostrap_samples: int = 2000,
) -> Dict[str, Any]:
    """Full accuracy summary for one cohort, dedup-relevant fields included.

    ``rows`` are expected to be already deduplicated by
    :func:`dedupe_by_window`, which is what makes ``effective_n`` meaningful.
    Windows whose raw rows disagreed are excluded from the rate and reported
    separately as ``conflict_n`` — a window the system could not call consistently
    is not a prediction to be scored.
    """
    scored = [
        row
        for row in rows
        if not row.get("conflict")
        and row.get(direction_key) in (bullish, bearish)
        and row.get(correct_key) is not None
    ]
    conflict_n = sum(1 for row in rows if row.get("conflict"))

    result: Dict[str, Any] = {
        "effective_n": len(scored),
        "dedup_input_n": len(rows),
        "conflict_n": conflict_n,
        "conflict_rate": (conflict_n / len(rows)) if rows else None,
    }

    for label, bucket in (("bullish", bullish), ("bearish", bearish)):
        subset = [row for row in scored if row.get(direction_key) == bucket]
        metrics = hit_rate(subset, correct_key=correct_key)
        pairs = [
            (row.get(date_key), 1.0 if row.get(correct_key) else 0.0) for row in subset
        ]
        stats = day_clustered_stats(pairs)
        low, high = bootstrap_ci_by_date(pairs, n_boot=boostrap_samples)
        result[f"{label}_n"] = metrics["n"]
        result[f"{label}_hits"] = metrics["hits"]
        result[f"{label}_rate"] = metrics["rate"]
        result[f"{label}_ci_low"] = low
        result[f"{label}_ci_high"] = high
        result[f"{label}_se"] = stats.se
        result[f"{label}_t"] = stats.t
        result[f"{label}_n_days"] = stats.n_days

    concentration_stats = concentration(scored)
    result.update(concentration_stats)
    return result
