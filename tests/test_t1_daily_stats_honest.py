"""D1/D2/D3: the published T+1 accuracy must use an honest denominator.

Three separate defects are pinned here.

**D1 — row count is not sample size.** ``_refresh_report_daily_stats`` computed
``correct = sum(1 for r in day_rows if r.label_correct)`` over *report rows*. But two
reports on the same symbol and signal date share one forward return; they are one
observation, not two. Measured on the real database: 4601 evaluated rows collapse to
~1583 unique price windows, the busiest single key holds 100 rows, and two symbols
supply 31.5% of all rows. So per-day accuracy was effectively decided by whichever
handful of names the user happened to re-analyse, and any confidence interval built
on the row count would overstate precision roughly threefold.

**D2 — abstention must not be a free score.** The cohort filter

    direction_bucket not in (None, "neutral", "unknown")

drops every row without a directional call. That is correct for the numerator, but on
its own it means the newly-permitted abstention (F3) *raises* accuracy: the model
declines the hard cases and the surviving set looks better, with nothing recording
that coverage fell. The excluded count is now persisted so the trade-off is visible.

**D3 — no uncertainty was reported at all.** ``ci_low``/``ci_high`` (Wilson, which is
the only interval that behaves at n = 1–5), ``effective_n``, ``unique_symbols`` and
``top_symbol_share`` are now stored and exposed.

The window identity is ``(symbol, signal_date, 1)``: report T+1 grading is always one
trading day close→close (see ``_report_price_window``).
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.database import Base, ReportDB, ReportT1OutcomeDB, T1DailyStatDB, TradePlanDB
from api.services import insights_t1_service as svc
from api.services import t1_stats


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _report(db, rid: str, symbol: str, trade_date: str = "2026-05-06"):
    db.add(
        ReportDB(
            id=rid,
            user_id="u1",
            symbol=symbol,
            trade_date=trade_date,
            status="completed",
            decision="BUY",
            direction="看多",
            created_at=datetime(2026, 5, 6, 10, 0, tzinfo=timezone.utc),
        )
    )


def _outcome(
    db,
    rid: str,
    symbol: str,
    *,
    signal_date: str = "2026-05-07",
    bucket: str = "BULLISH",
    correct: bool | None = True,
    ret: float = 1.0,
):
    db.add(
        ReportT1OutcomeDB(
            id=f"o_{rid}",
            user_id="u1",
            report_id=rid,
            symbol=symbol,
            signal_trade_date=signal_date,
            t1_trade_date="2026-05-08",
            return_t1_pct=ret,
            direction_bucket=bucket,
            label_correct=correct,
            status="evaluated",
        )
    )


def _stats_for(db, day: str, scope: str = "all") -> T1DailyStatDB:
    return (
        db.query(T1DailyStatDB)
        .filter(T1DailyStatDB.metric == "report_accuracy_t1")
        .filter(T1DailyStatDB.scope == scope)
        .filter(T1DailyStatDB.signal_trade_date == day)
        .one()
    )


def _refresh(db):
    svc.refresh_t1_daily_stats(db, user_id="u1")
    db.commit()


# ---------------------------------------------------------------------------
# D1: dedup by price window
# ---------------------------------------------------------------------------

class TestRowCountIsNotSampleSize:
    def test_repeat_reports_on_one_window_count_once(self, db):
        """10 reports, same symbol+day, all correct → effective_n is 1, not 10."""
        for i in range(10):
            _report(db, f"r{i}", "600519.SH")
            _outcome(db, f"r{i}", "600519.SH", correct=True)
        db.commit()
        _refresh(db)

        row = _stats_for(db, "2026-05-07")
        assert row.sample_count == 10, "原始行数仍应保留，用于对比覆盖率"
        assert row.effective_n == 1
        assert row.unique_symbols == 1
        assert row.accuracy_pct == 100.0
        assert row.top_symbol_share == pytest.approx(1.0)

    def test_row_count_would_have_inflated_the_interval(self, db):
        """The Wilson interval is computed on effective_n, so it stays wide."""
        for i in range(10):
            _report(db, f"r{i}", "600519.SH")
            _outcome(db, f"r{i}", "600519.SH", correct=True)
        db.commit()
        _refresh(db)

        row = _stats_for(db, "2026-05-07")
        # One observation cannot support a confident interval.
        assert row.ci_low is not None and row.ci_high is not None
        assert row.ci_low < 0.5, f"单一样本不应给出窄区间，得到 ci_low={row.ci_low}"
        narrow, _ = t1_stats.wilson_interval(10, 10)
        assert row.ci_low < narrow, "去重前的区间会让这一天看起来远比实际确定"

    def test_distinct_symbols_stay_distinct(self, db):
        for sym in ("600519.SH", "300750.SZ", "000001.SZ"):
            _report(db, f"r_{sym}", sym)
            _outcome(db, f"r_{sym}", sym, correct=True)
        db.commit()
        _refresh(db)

        row = _stats_for(db, "2026-05-07")
        assert row.effective_n == 3
        assert row.unique_symbols == 3
        assert row.top_symbol_share == pytest.approx(1 / 3)

    def test_mixed_outcomes_produce_the_deduped_rate(self, db):
        # 600519: two rows, both correct → 1 window, correct
        _report(db, "a", "600519.SH")
        _outcome(db, "a", "600519.SH", correct=True)
        _report(db, "b", "600519.SH")
        _outcome(db, "b", "600519.SH", correct=True)
        # 300750: one row, wrong → 1 window, wrong
        _report(db, "c", "300750.SZ")
        _outcome(db, "c", "300750.SZ", correct=False, ret=-1.0)
        db.commit()
        _refresh(db)

        row = _stats_for(db, "2026-05-07")
        assert row.sample_count == 3
        assert row.effective_n == 2
        assert row.accuracy_pct == 50.0, "按行数是 66.7%，按窗口是 50%"


# ---------------------------------------------------------------------------
# conflicting directions must not be resolved silently
# ---------------------------------------------------------------------------

class TestConflictWindows:
    def test_a_window_with_both_directions_is_excluded(self, db):
        """Same symbol+day called BULLISH by one report and BEARISH by another."""
        _report(db, "a", "600519.SH")
        _outcome(db, "a", "600519.SH", bucket="BULLISH", correct=True)
        _report(db, "b", "600519.SH")
        _outcome(db, "b", "600519.SH", bucket="BEARISH", correct=False, ret=2.0)
        db.commit()
        _refresh(db)

        row = _stats_for(db, "2026-05-07")
        assert row.conflict_count == 1, "方向矛盾的窗口必须被记录，而不是按行序/多数静默取一个"
        assert row.effective_n == 0, "矛盾窗口不是一次预测，不进分母"
        assert row.accuracy_pct is None

    def test_conflict_is_counted_even_when_other_windows_score(self, db):
        _report(db, "a", "600519.SH")
        _outcome(db, "a", "600519.SH", bucket="BULLISH", correct=True)
        _report(db, "b", "600519.SH")
        _outcome(db, "b", "600519.SH", bucket="BEARISH", correct=False, ret=2.0)
        _report(db, "c", "300750.SZ")
        _outcome(db, "c", "300750.SZ", bucket="BULLISH", correct=True)
        db.commit()
        _refresh(db)

        row = _stats_for(db, "2026-05-07")
        assert row.conflict_count == 1
        assert row.effective_n == 1
        assert row.accuracy_pct == 100.0

    def test_agreeing_duplicates_are_not_a_conflict(self, db):
        _report(db, "a", "600519.SH")
        _outcome(db, "a", "600519.SH", bucket="BULLISH", correct=True)
        _report(db, "b", "600519.SH")
        _outcome(db, "b", "600519.SH", bucket="BULLISH", correct=True)
        db.commit()
        _refresh(db)

        row = _stats_for(db, "2026-05-07")
        assert row.conflict_count == 0
        assert row.effective_n == 1


# ---------------------------------------------------------------------------
# D2: abstention is recorded, not rewarded
# ---------------------------------------------------------------------------

class TestAbstentionIsRecorded:
    def test_abstained_rows_are_counted_separately(self, db):
        """A neutral call must leave the denominator but appear in abstain_count."""
        _report(db, "a", "600519.SH")
        _outcome(db, "a", "600519.SH", bucket="BULLISH", correct=True)
        _report(db, "b", "300750.SZ")
        _outcome(db, "b", "300750.SZ", bucket="neutral", correct=None, ret=0.2)
        db.commit()
        _refresh(db)

        row = _stats_for(db, "2026-05-07")
        assert row.accuracy_pct == 100.0
        assert row.effective_n == 1
        assert row.abstain_count == 1, "弃权必须可见，否则提高弃权率就等于免费提高命中率"

    def test_unknown_bucket_also_counts_as_abstention(self, db):
        _report(db, "a", "600519.SH")
        _outcome(db, "a", "600519.SH", bucket="BULLISH", correct=True)
        _report(db, "b", "300750.SZ")
        _outcome(db, "b", "300750.SZ", bucket="unknown", correct=None, ret=0.2)
        db.commit()
        _refresh(db)

        row = _stats_for(db, "2026-05-07")
        assert row.abstain_count == 1

    def test_a_day_of_pure_abstention_still_produces_a_row(self, db):
        """Coverage collapse must be visible as a zero-sample day, not a missing day."""
        _report(db, "a", "600519.SH")
        _outcome(db, "a", "600519.SH", bucket="neutral", correct=None, ret=0.1)
        db.commit()
        _refresh(db)

        row = _stats_for(db, "2026-05-07")
        assert row.effective_n == 0
        assert row.accuracy_pct is None
        assert row.abstain_count == 1

    def test_abstention_does_not_leak_into_effective_n(self, db):
        _report(db, "a", "600519.SH")
        _outcome(db, "a", "600519.SH", bucket="BULLISH", correct=True)
        for i in range(5):
            _report(db, f"n{i}", f"30075{i}.SZ")
            _outcome(db, f"n{i}", f"30075{i}.SZ", bucket="neutral", correct=None, ret=0.0)
        db.commit()
        _refresh(db)

        row = _stats_for(db, "2026-05-07")
        assert row.effective_n == 1
        assert row.abstain_count == 5


# ---------------------------------------------------------------------------
# D3: uncertainty is reported
# ---------------------------------------------------------------------------

class TestUncertaintyIsReported:
    def test_ci_contains_the_point_estimate(self, db):
        for i, sym in enumerate(("600519.SH", "300750.SZ", "000001.SZ", "601318.SH")):
            _report(db, f"r{i}", sym)
            _outcome(db, f"r{i}", sym, correct=(i % 2 == 0))
        db.commit()
        _refresh(db)

        row = _stats_for(db, "2026-05-07")
        assert row.accuracy_pct == 50.0
        assert row.ci_low is not None and row.ci_high is not None
        assert row.ci_low * 100.0 <= row.accuracy_pct <= row.ci_high * 100.0

    def test_small_samples_get_wide_intervals(self, db):
        _report(db, "a", "600519.SH")
        _outcome(db, "a", "600519.SH", correct=True)
        db.commit()
        _refresh(db)

        row = _stats_for(db, "2026-05-07")
        assert row.ci_high is not None and row.ci_high == pytest.approx(1.0)
        assert row.ci_low is not None and row.ci_low < 0.3

    def test_no_scoreable_rows_means_no_interval(self, db):
        _report(db, "a", "600519.SH")
        _outcome(db, "a", "600519.SH", bucket="neutral", correct=None)
        db.commit()
        _refresh(db)

        row = _stats_for(db, "2026-05-07")
        assert row.ci_low is None and row.ci_high is None


# ---------------------------------------------------------------------------
# the API surface exposes the honest numbers
# ---------------------------------------------------------------------------

class TestTrendExposesHonestFields:
    def test_trend_reports_effective_n_and_ci(self, db):
        for i in range(6):
            _report(db, f"r{i}", "600519.SH")
            _outcome(db, f"r{i}", "600519.SH", correct=True)
        db.commit()
        _refresh(db)

        points = svc.report_accuracy_t1_trend(db, user_id="u1", scope="all")
        assert len(points) == 1
        pt = points[0]
        assert pt["sample_count"] == 6
        assert pt["effective_n"] == 1
        assert pt["unique_symbols"] == 1
        assert pt["conflict_count"] == 0
        assert "ci_low_pct" in pt and "ci_high_pct" in pt
        assert pt["ci_low_pct"] < pt["accuracy_pct"] <= pt["ci_high_pct"]

    def test_raw_fallback_agrees_with_the_aggregate_table(self, db):
        """The fallback path must not use a different denominator than the table."""
        for i in range(4):
            _report(db, f"r{i}", "600519.SH")
            _outcome(db, f"r{i}", "600519.SH", correct=True)
        db.commit()

        points = svc.report_accuracy_t1_trend(db, user_id="u1", scope="all")
        assert len(points) == 1
        assert points[0]["sample_count"] == 4
        assert points[0]["effective_n"] == 1, "回退路径也必须按价格窗口去重"
        assert points[0]["accuracy_pct"] == 100.0


# ---------------------------------------------------------------------------
# D3: the aggregate verdict — "is this distinguishable from a coin flip?"
# ---------------------------------------------------------------------------

class TestAggregateSummaryVerdict:
    def _add(self, db, symbol, day, correct):
        rid = f"{symbol}_{day}"
        _report(db, rid, symbol, trade_date=day)
        _outcome(db, rid, symbol, signal_date=day, correct=correct)

    def test_a_small_mixed_sample_does_not_beat_a_coin_flip(self, db):
        """5/8 = 62.5% sounds good but the interval still spans 50%.

        (8/8 would in fact be significant — p≈0.008 — so this uses a realistic
        small sample rather than a perfect one.)
        """
        for i in range(8):
            self._add(db, f"60051{i}.SH", f"2026-05-{7 + i:02d}", i < 5)
        db.commit()

        s = svc.report_accuracy_t1_summary(db, user_id="u1", days=400, scope="all")
        assert s["effective_n"] == 8
        assert s["accuracy_pct"] == 62.5
        assert s["ci_low_pct"] < 50.0, "8 个样本中的 5 次命中不足以宣称优于掷硬币"
        assert s["verdict"] == "not_significant"
        assert s["not_significant"] is True
        assert s["beats_coin_flip"] is False

    def test_a_perfect_small_sample_is_still_reported_with_its_interval(self, db):
        """8/8 is significant, but the interval is wide and must be shown."""
        for i in range(8):
            self._add(db, f"60051{i}.SH", f"2026-05-{7 + i:02d}", True)
        db.commit()

        s = svc.report_accuracy_t1_summary(db, user_id="u1", days=400, scope="all")
        assert s["accuracy_pct"] == 100.0
        assert s["verdict"] == "beats_coin_flip"
        assert s["ci_low_pct"] < 100.0, "即使全对也必须给出区间，不能只报点估计"

    def test_a_large_decisive_sample_is_flagged_as_beating_a_coin_flip(self, db):
        """Bucketed by day so clustering is honest; 200 windows at 65%."""
        for day_i in range(10):
            day = f"2026-06-{day_i + 1:02d}"
            for i in range(20):
                # 13 of 20 correct each day → 65%
                self._add(db, f"60{i:04d}.SH", day, i < 13)
        db.commit()

        s = svc.report_accuracy_t1_summary(db, user_id="u1", days=400, scope="all")
        assert s["effective_n"] == 200
        assert s["accuracy_pct"] == 65.0
        assert s["ci_low_pct"] > 50.0
        assert s["verdict"] == "beats_coin_flip"
        assert s["beats_coin_flip"] is True

    def test_underpowered_is_reported_with_the_required_sample_size(self, db):
        for day_i in range(4):
            for i in range(5):
                self._add(db, f"60{day_i}{i:03d}.SH", f"2026-07-{day_i + 1:02d}", i < 3)
        db.commit()

        s = svc.report_accuracy_t1_summary(db, user_id="u1", days=400, scope="all")
        assert s["effective_n"] == 20
        assert s["required_n"] > 20
        assert s["underpowered"] is True

    def test_duplicate_reports_do_not_inflate_the_summary(self, db):
        """200 rows on ONE window is still one observation."""
        for i in range(200):
            _report(db, f"r{i}", "600519.SH", trade_date="2026-08-03")
            _outcome(db, f"r{i}", "600519.SH", signal_date="2026-08-04", correct=True)
        db.commit()

        s = svc.report_accuracy_t1_summary(db, user_id="u1", days=400, scope="all")
        assert s["row_count"] == 200
        assert s["effective_n"] == 1, "重复研报不能把有效样本量刷上去"
        assert s["verdict"] == "not_significant"

    def test_coverage_and_abstention_are_reported(self, db):
        self._add(db, "600519.SH", "2026-09-01", True)
        _report(db, "n1", "300750.SZ", trade_date="2026-09-01")
        _outcome(db, "n1", "300750.SZ", signal_date="2026-09-02", bucket="neutral", correct=None)
        db.commit()

        s = svc.report_accuracy_t1_summary(db, user_id="u1", days=400, scope="all")
        assert s["effective_n"] == 1
        assert s["row_count"] == 2
        assert s["abstain_count"] == 1
        assert s["coverage_pct"] == 50.0

    def test_empty_database_returns_a_usable_shape(self, db):
        s = svc.report_accuracy_t1_summary(db, user_id="u1", days=400, scope="all")
        assert s["effective_n"] == 0
        assert s["accuracy_pct"] is None
        assert s["note"]

    def test_worse_than_coin_flip_is_named(self, db):
        for day_i in range(10):
            day = f"2026-10-{day_i + 1:02d}"
            for i in range(20):
                self._add(db, f"30{i:04d}.SZ", day, i < 4)  # 20%
        db.commit()

        s = svc.report_accuracy_t1_summary(db, user_id="u1", days=400, scope="all")
        assert s["accuracy_pct"] == 20.0
        assert s["verdict"] == "worse_than_coin_flip"

    def test_clustered_estimate_is_also_reported(self, db):
        """A second estimator that does not let one busy day dominate."""
        for day_i in range(6):
            day = f"2026-11-{day_i + 1:02d}"
            for i in range(10):
                self._add(db, f"60{i:04d}.SH", day, i < 6)
        db.commit()

        s = svc.report_accuracy_t1_summary(db, user_id="u1", days=400, scope="all")
        cl = s["clustered"]
        assert cl["n_days"] == 6
        assert cl["accuracy_pct"] == 60.0, "每天都是 6/10，按日等权也应得 60%"
        assert cl["se_pct"] is not None
        assert cl["bootstrap_ci_low_pct"] is not None

    def test_clustered_and_pooled_diverge_when_one_day_dominates(self, db):
        """Pooled rate is dragged by a high-volume day; the equal-weight one is not.

        Day A: 1 judgement, correct (100%).
        Day B: 9 judgements, 0 correct (0%).
        Pooled = 1/10 = 10%.  Equal-weight-by-day = (100% + 0%)/2 = 50%.
        """
        self._add(db, "600519.SH", "2026-12-01", True)
        for i in range(9):
            self._add(db, f"30{i:04d}.SZ", "2026-12-02", False)
        db.commit()

        s = svc.report_accuracy_t1_summary(db, user_id="u1", days=400, scope="all")
        assert s["effective_n"] == 10
        assert s["accuracy_pct"] == 10.0
        assert s["clustered"]["accuracy_pct"] == 50.0
        assert s["clustered"]["n_days"] == 2

    def test_clustered_is_absent_with_a_single_day(self, db):
        """One date carries no information about dispersion — do not fake a CI."""
        for i in range(5):
            self._add(db, f"60{i:04d}.SH", "2026-12-05", True)
        db.commit()

        s = svc.report_accuracy_t1_summary(db, user_id="u1", days=400, scope="all")
        assert s["effective_n"] == 5
        assert s["clustered"] == {}


# ---------------------------------------------------------------------------
# P5 (D5): de-beta — absolute accuracy is mostly market beta
# ---------------------------------------------------------------------------

class TestDeBetaDualReporting:
    """Absolute hit rate counts a bullish call as a win on a day the market rose.

    The plan's P5 requires reporting both the absolute rate and the rate measured
    against the market, because only the latter reflects judgement. Measured on the
    real database the gap is the whole story: absolute 52.34% collapses to an
    excess 50.50% — i.e. every apparent edge was beta.

    Peers are added as **neutral** calls: they supply the day's benchmark (the
    benchmark is built from every evaluated row, exactly as production does) while
    staying out of the scored set, so each test grades only the call it is about.
    """

    def _add_ret(self, db, symbol, day, ret, direction="BULLISH"):
        rid = f"{symbol}_{day}"
        _report(db, rid, symbol, trade_date=day)
        correct = (ret > 0) if direction == "BULLISH" else (ret < 0)
        db.add(
            ReportT1OutcomeDB(
                id=f"o_{rid}",
                user_id="u1",
                report_id=rid,
                symbol=symbol,
                signal_trade_date=day,
                t1_trade_date=day,
                return_t1_pct=ret,
                direction_bucket=direction,
                label_correct=correct,
                status="evaluated",
            )
        )

    def _add_peer(self, db, symbol, day, ret):
        """A neutral call: contributes to the benchmark, never to the score."""
        rid = f"peer_{symbol}_{day}"
        _report(db, rid, symbol, trade_date=day)
        db.add(
            ReportT1OutcomeDB(
                id=f"o_{rid}",
                user_id="u1",
                report_id=rid,
                symbol=symbol,
                signal_trade_date=day,
                t1_trade_date=day,
                return_t1_pct=ret,
                direction_bucket="neutral",
                label_correct=None,
                status="evaluated",
            )
        )

    def test_a_rising_market_inflates_absolute_accuracy(self, db):
        """The called name rises, but less than the peers it is benchmarked against."""
        self._add_ret(db, "600519.SH", "2026-05-07", 1.0)
        for i in range(3):
            self._add_peer(db, f"60{i:04d}.SZ", "2026-05-07", 5.0)
        db.commit()

        s = svc.report_accuracy_t1_summary(db, user_id="u1", days=400, scope="all")
        assert s["accuracy_pct"] == 100.0, "绝对口径：股票涨了，算对"
        assert s["excess"]["accuracy_pct"] == 0.0, "相对口径：跑输同侪，算错"
        assert s["excess"]["accuracy_pct"] < s["accuracy_pct"]

    def test_a_bullish_call_on_a_falling_day_is_wrong_absolutely_and_relatively(self, db):
        self._add_ret(db, "600519.SH", "2026-05-08", -3.0)
        for i in range(3):
            self._add_peer(db, f"60{i:04d}.SZ", "2026-05-08", -1.0)
        db.commit()

        s = svc.report_accuracy_t1_summary(db, user_id="u1", days=400, scope="all")
        assert s["accuracy_pct"] == 0.0
        assert s["excess"]["accuracy_pct"] == 0.0, "跌得比同侪更多 → 超额也为负 → 错"

    def test_a_bullish_call_beating_its_peers_is_correct_on_both(self, db):
        self._add_ret(db, "600519.SH", "2026-05-11", 4.0)
        for i in range(3):
            self._add_peer(db, f"30{i:04d}.SZ", "2026-05-11", 1.0)
        db.commit()

        s = svc.report_accuracy_t1_summary(db, user_id="u1", days=400, scope="all")
        assert s["accuracy_pct"] == 100.0
        assert s["excess"]["accuracy_pct"] == 100.0

    def test_bearish_call_is_correct_when_it_underperforms_peers(self, db):
        self._add_ret(db, "600519.SH", "2026-05-12", -5.0, direction="BEARISH")
        for i in range(3):
            self._add_peer(db, f"60{i:04d}.SZ", "2026-05-12", -1.0)
        db.commit()

        s = svc.report_accuracy_t1_summary(db, user_id="u1", days=400, scope="all")
        assert s["accuracy_pct"] == 100.0
        assert s["excess"]["accuracy_pct"] == 100.0

    def test_days_without_peers_are_not_measured_not_assumed(self, db):
        """A lone name has no benchmark — it must be excluded, never scored as 50%."""
        self._add_ret(db, "600519.SH", "2026-05-13", 2.0)
        db.commit()

        s = svc.report_accuracy_t1_summary(db, user_id="u1", days=400, scope="all")
        assert s["effective_n"] == 1
        assert s["excess"]["effective_n"] == 0, "无同侪可比 → 超额不可测，不能凭空算成 50%"
        assert s["excess"]["accuracy_pct"] is None
        assert s["excess"]["days_without_peers"] == 1

    def test_spread_uses_percent_units_and_compares_against_cost(self, db):
        """Returns are already in percentage points — do not scale them again."""
        day = "2026-05-14"
        self._add_ret(db, "600519.SH", day, 2.0, direction="BULLISH")
        self._add_ret(db, "300750.SZ", day, 0.0, direction="BEARISH")
        self._add_peer(db, "000001.SZ", day, 1.0)
        db.commit()

        s = svc.report_accuracy_t1_summary(db, user_id="u1", days=400, scope="all")
        sp = s["excess_spread"]
        assert sp["unit"] == "percentage_points_per_day"
        assert sp["bullish_n"] == 1
        assert sp["bearish_n"] == 1
        assert sp["bullish_mean_excess_pct"] > 0 > sp["bearish_mean_excess_pct"]
        assert abs(sp["long_short_spread_pct"]) < 20.0, (
            f"价差 {sp['long_short_spread_pct']} 量级过大，很可能把已是百分点的收益又乘了 100"
        )
        assert sp["cost_round_trip_pct"] == 0.25
        assert sp["covers_cost"] is True

    def test_spread_that_does_not_cover_cost_is_flagged(self, db):
        """A tiny edge must not be reported as tradeable."""
        day = "2026-05-15"
        self._add_ret(db, "600519.SH", day, 1.02, direction="BULLISH")
        self._add_ret(db, "300750.SZ", day, 0.98, direction="BEARISH")
        self._add_peer(db, "000001.SZ", day, 1.00)
        db.commit()

        s = svc.report_accuracy_t1_summary(db, user_id="u1", days=400, scope="all")
        sp = s["excess_spread"]
        assert sp["long_short_spread_pct"] < 0.25
        assert sp["covers_cost"] is False

    def test_excess_block_is_always_present(self, db):
        _report(db, "n1", "600519.SH")
        _outcome(db, "n1", "600519.SH", bucket="neutral", correct=None)
        db.commit()

        s = svc.report_accuracy_t1_summary(db, user_id="u1", days=400, scope="all")
        assert "excess" in s
        assert s["excess"]["effective_n"] == 0
        assert "benchmark" in s["excess"]


# ---------------------------------------------------------------------------
# A5/D5 + F1: the metric must declare which horizon it measures
# ---------------------------------------------------------------------------


class TestHorizonScopeAndPlanMismatch:
    """A5/D5 + F1: the metric must declare *which* horizon it measures.

    The T+1 scorer compares two closes one trading day apart, so what it measures is
    the next-day **direction** — always. The reports it grades, however, carry trade
    plans whose target price / stop loss / time stop belong to a much longer horizon.
    Two failure modes follow if that is left implicit:

    * a reader treats the hit rate as "did the plan work?" — it never did; and
    * nobody can tell how many scored windows actually carried a medium-term plan.

    These tests pin the declared scope and the honest treatment of *unrecorded* plan
    horizons (no backfill: a guessed holding period must not masquerade as a record).
    """

    def _one(self, db, *, plan_days=None, horizon=None):
        _report(db, "r1", "600519")
        _outcome(db, "r1", "600519")
        db.commit()
        if horizon is not None or plan_days is not None:
            db.add(
                TradePlanDB(
                    id="p1",
                    user_id="u1",
                    report_id="r1",
                    symbol="600519",
                    horizon=horizon,
                    horizon_days=plan_days,
                )
            )
        row = db.query(ReportT1OutcomeDB).filter(ReportT1OutcomeDB.report_id == "r1").one()
        if plan_days is not None:
            row.plan_horizon_days = plan_days
        db.commit()
        return row

    def test_summary_declares_the_horizon_it_measures(self, db):
        self._one(db)
        s = svc.report_accuracy_t1_summary(db, user_id="u1", days=400, scope="all")
        assert s["horizon"] == "t1"

    def test_plan_block_is_present_and_declares_its_scope(self, db):
        self._one(db)
        s = svc.report_accuracy_t1_summary(db, user_id="u1", days=400, scope="all")
        ph = s["plan_horizon"]
        assert ph["measured_horizon"] == "t1"
        assert ph["unit"] == "natural_days"
        assert "次日方向" in ph["note"]

    def test_windows_without_plan_are_counted(self, db):
        self._one(db)
        s = svc.report_accuracy_t1_summary(db, user_id="u1", days=400, scope="all")
        ph = s["plan_horizon"]
        assert ph["windows_with_plan"] == 0
        assert ph["windows_without_plan"] == s["effective_n"]
        assert ph["multi_day_plan_windows"] == 0

    def test_unrecorded_plan_horizon_is_not_reported_as_absent(self, db):
        """A plan with no recorded horizon must show up, not vanish."""
        self._one(db, horizon="dual", plan_days=None)
        s = svc.report_accuracy_t1_summary(db, user_id="u1", days=400, scope="all")
        ph = s["plan_horizon"]
        assert ph["windows_with_unrecorded_plan_horizon"] == 1, (
            "附有计划但周期未记录的窗口必须被单独报出，否则会被读成「没有计划」"
        )
        assert ph["windows_with_plan"] == 0, "不得把未记录当成已记录"

    def test_a_recorded_twenty_day_plan_is_flagged_as_mismatch(self, db):
        self._one(db, plan_days=20)
        s = svc.report_accuracy_t1_summary(db, user_id="u1", days=400, scope="all")
        ph = s["plan_horizon"]
        assert ph["windows_with_plan"] == 1
        assert ph["median_plan_days"] == 20
        assert ph["multi_day_plan_windows"] == 1, "20 天期计划承载在 T+1 窗口上就是错配"

    def test_a_one_day_plan_is_not_a_mismatch(self, db):
        self._one(db, plan_days=1)
        s = svc.report_accuracy_t1_summary(db, user_id="u1", days=400, scope="all")
        assert s["plan_horizon"]["multi_day_plan_windows"] == 0

    def test_fill_stamps_the_measured_horizon_on_every_branch(self, db):
        """Every branch of the fill must declare the scope, including un-evaluable ones.

        Stamping only on the success path would leave `horizon` NULL exactly where the
        data is weakest, and a NULL scope is indistinguishable from a pre-A5 row.
        """
        now = datetime(2026, 5, 6, 10, 0, tzinfo=timezone.utc)
        # 无 trade_date / created_at ⇒ 拿不到 p1 ⇒ 走 insufficient_data 早退分支。
        # 该分支在取价之前就返回，所以 close_cache_inst 可以是 None。
        row = ReportT1OutcomeDB(
            id="o_branch", user_id="u1", report_id="r_branch", symbol="600519", created_at=now
        )
        status = svc._fill_report_t1_outcome(
            row,
            symbol="600519",
            trade_date=None,
            created_at=None,
            direction="看多",
            decision="BUY",
            close_cache_inst=None,
            now=now,
            plan_horizon_days=20,
        )
        assert status in {"insufficient_data", "pending"}, status
        assert row.horizon == "t1", "无法取价的分支也必须声明衡量口径"
        assert row.plan_horizon_days == 20, "计划周期与能否取价无关，应一并落库"


class TestPlanHorizonLookup:
    """`_plan_horizon_days_by_report` — the F1 lookup, including its vocabulary fallback."""

    def _plan(self, db, rid, horizon, days):
        db.add(
            TradePlanDB(
                id=f"p_{rid}", user_id="u1", report_id=rid,
                symbol="600519", horizon=horizon, horizon_days=days,
            )
        )

    def test_returns_recorded_horizon_days(self, db):
        self._plan(db, "r1", "short", 7)
        db.commit()
        assert svc._plan_horizon_days_by_report(db, {"r1"}) == {"r1": 7}

    def test_falls_back_to_the_period_word_when_days_are_missing(self, db):
        """Every real `trade_plans.horizon_days` is NULL, so the word must carry the load."""
        self._plan(db, "r2", "short", None)
        self._plan(db, "r3", "medium", None)
        self._plan(db, "r4", "dual", None)
        db.commit()
        assert svc._plan_horizon_days_by_report(db, {"r2", "r3", "r4"}) == {
            "r2": 5, "r3": 20, "r4": 20,
        }

    def test_unknown_period_word_yields_none_rather_than_a_guess(self, db):
        self._plan(db, "r5", "mystery", None)
        db.commit()
        assert svc._plan_horizon_days_by_report(db, {"r5"}) == {"r5": None}

    def test_absent_plan_is_not_in_the_map(self, db):
        self._plan(db, "r6", "short", 5)
        db.commit()
        got = svc._plan_horizon_days_by_report(db, {"r6", "no_such_report"})
        assert "no_such_report" not in got, "没有计划与计划未记录是两件事"

    def test_empty_report_ids_short_circuits(self, db):
        assert svc._plan_horizon_days_by_report(db, set()) == {}

    def test_scope_filter_still_applies(self, db):
        self._plan(db, "r7", "short", 5)
        db.commit()
        assert svc._plan_horizon_days_by_report(db, {"other"}) == {}


class TestHorizonColumnIsReadNotJustWritten:
    """A5 的反面教材：加了列、写了值，却没人读。

    `reports.horizon` 曾长期只建不写（实测 0/7504），`report_t1_outcomes.horizon`
    虽然写了却无人读取，而摘要里的 `"horizon": "t1"` 是**硬编码常量**——看起来口径
    已经声明，实际上声明与数据无关。这些测试要求口径来自**数据**：列里是什么，摘要
    就必须报什么。
    """

    def _one(self, db, *, out_horizon, rep_horizon):
        _report(db, "r1", "600519")
        _outcome(db, "r1", "600519")
        db.commit()
        db.query(ReportDB).filter(ReportDB.id == "r1").update({"horizon": rep_horizon})
        db.query(ReportT1OutcomeDB).filter(ReportT1OutcomeDB.report_id == "r1").update(
            {"horizon": out_horizon}
        )
        db.commit()

    def _summary(self, db):
        return svc.report_accuracy_t1_summary(db, user_id="u1", days=400, scope="all")

    def test_scored_horizon_values_are_derived_from_the_column(self, db):
        self._one(db, out_horizon="t1", rep_horizon="short")
        s = self._summary(db)
        assert s["plan_horizon"]["scored_horizon_values"] == {"t1": 1}
        assert s["plan_horizon"]["foreign_horizon_values"] == []

    def test_null_horizon_is_reported_as_null_not_guessed(self, db):
        """存量行周期真的不可知，必须报 null，不得回填成 t1。"""
        _report(db, "r1", "600519")
        _outcome(db, "r1", "600519")
        db.commit()
        s = self._summary(db)
        assert s["plan_horizon"]["scored_horizon_values"] == {"null": 1}
        assert s["horizon"] == "t1"

    def test_a_non_t1_row_makes_the_scope_claim_visibly_fail(self, db):
        """一旦混入非次日行，口径声明必须失效可见，而不是继续自称只评次日。"""
        self._one(db, out_horizon="medium", rep_horizon="medium")
        s = self._summary(db)
        assert s["plan_horizon"]["foreign_horizon_values"] == ["medium"]
        assert s["plan_horizon"]["scored_horizon_values"] == {"medium": 1}

    def test_graded_report_horizon_is_read_back(self, db):
        """`reports.horizon` 必须被读取：dual 表示当时给了两套结论。"""
        self._one(db, out_horizon="t1", rep_horizon="dual")
        s = self._summary(db)
        assert s["plan_horizon"]["graded_report_horizon_values"] == {"dual": 1}

    def test_unrecorded_report_horizon_shows_as_null(self, db):
        self._one(db, out_horizon="t1", rep_horizon=None)
        s = self._summary(db)
        assert s["plan_horizon"]["graded_report_horizon_values"] == {"null": 1}


class TestStaleAggregateNeverShadowsHonestFields:
    """存量聚合行不得遮蔽诚实度量 —— D2/D3 最隐蔽的失效方式。

    `report_accuracy_t1_trend` 原本只要 `t1_daily_stats` 里有行就**原样返回**。而
    D2/D3 修复之前的 921 行，诚实列（`effective_n` / `ci_low` / `ci_high` /
    `abstain_count` / `conflict_count`）**全是 NULL** —— 于是接口对每个数据点都返回
    None：去重分母、置信区间、弃权与矛盾计数一起消失。写入路径其实完全正常，界面上
    却是一片空白，而且没有任何报错。这组测试要求"表里有行"不等于"表可信"。
    """

    def _seed_scored_day(self, db, *, n=4):
        for i in range(n):
            _report(db, f"r{i}", "600519.SH")
            _outcome(db, f"r{i}", "600519.SH", correct=True)
        db.commit()

    def test_prefix_row_triggers_recomputation(self, db):
        """表里只有存量行（effective_n 为 NULL）时，必须改为现算。"""
        self._seed_scored_day(db)
        db.add(
            T1DailyStatDB(
                id="stale1",
                user_id="u1",
                metric="report_accuracy_t1",
                scope="all",
                signal_trade_date="2026-05-07",
                t1_trade_date="2026-05-08",
                sample_count=4,
                accuracy_pct=100.0,
                # 诚实列全为 NULL —— 与修复前写入的 921 行一致
                effective_n=None,
                ci_low=None,
                ci_high=None,
                abstain_count=None,
                conflict_count=None,
            )
        )
        db.commit()

        points = svc.report_accuracy_t1_trend(db, user_id="u1", scope="all")
        assert len(points) == 1
        pt = points[0]
        assert pt["effective_n"] is not None, "存量行遮蔽了 effective_n"
        assert pt["ci_low_pct"] is not None, "存量行遮蔽了 Wilson 区间"
        assert pt["conflict_count"] is not None, "存量行遮蔽了矛盾窗口计数"
        assert pt["effective_n"] == 1, "应走与聚合表同口径的去重"

    def test_no_point_has_null_effective_n(self, db):
        """整条序列都不允许出现 effective_n=None —— 那正是被遮蔽的症状。"""
        self._seed_scored_day(db)
        db.add(
            T1DailyStatDB(
                id="stale1",
                user_id="u1",
                metric="report_accuracy_t1",
                scope="all",
                signal_trade_date="2026-05-07",
                t1_trade_date="2026-05-08",
                sample_count=4,
                accuracy_pct=100.0,
            )
        )
        db.commit()
        points = svc.report_accuracy_t1_trend(db, user_id="u1", scope="all")
        assert points, "应有数据点"
        assert all(p["effective_n"] is not None for p in points)
        assert all(p["ci_low_pct"] is not None for p in points)

    def test_fully_written_row_is_served_verbatim(self, db):
        """诚实列齐全时必须直接用表里的值，不得每次重算（否则写入端形同虚设）。"""
        self._seed_scored_day(db)
        db.add(
            T1DailyStatDB(
                id="fresh1",
                user_id="u1",
                metric="report_accuracy_t1",
                scope="all",
                signal_trade_date="2026-05-07",
                t1_trade_date="2026-05-08",
                sample_count=4,
                accuracy_pct=25.0,
                effective_n=99,  # 刻意与原始数据不一致，用来证明读的是表
                ci_low=0.1,
                ci_high=0.4,
                abstain_count=5,
                conflict_count=2,
            )
        )
        db.commit()
        points = svc.report_accuracy_t1_trend(db, user_id="u1", scope="all")
        assert len(points) == 1
        pt = points[0]
        assert pt["effective_n"] == 99, "未使用表里的值 —— 守卫误判或过度重算"
        assert pt["sample_count"] == 4
        assert pt["ci_low_pct"] == 10.0
        assert pt["ci_high_pct"] == 40.0
        assert pt["abstain_count"] == 5
        assert pt["conflict_count"] == 2

    def test_mixed_series_is_recomputed_as_a_whole(self, db):
        """新老混在一起时整体重算，避免同一张图上有的点有区间、有的没有。"""
        self._seed_scored_day(db)
        db.add_all(
            [
                T1DailyStatDB(
                    id="stale1",
                    user_id="u1",
                    metric="report_accuracy_t1",
                    scope="all",
                    signal_trade_date="2026-05-07",
                    t1_trade_date="2026-05-08",
                    sample_count=4,
                    accuracy_pct=100.0,
                    effective_n=None,
                ),
                T1DailyStatDB(
                    id="fresh1",
                    user_id="u1",
                    metric="report_accuracy_t1",
                    scope="all",
                    signal_trade_date="2026-05-06",
                    t1_trade_date="2026-05-07",
                    sample_count=4,
                    accuracy_pct=25.0,
                    effective_n=99,
                    ci_low=0.1,
                    ci_high=0.4,
                    abstain_count=5,
                    conflict_count=2,
                ),
            ]
        )
        db.commit()
        points = svc.report_accuracy_t1_trend(db, user_id="u1", scope="all")
        assert points
        assert all(p["effective_n"] != 99 for p in points), (
            "新老混合时必须整体重算，不能拼出半新半旧的序列"
        )
        assert all(p["effective_n"] is not None for p in points)

    def test_raw_fallback_reports_abstain_count(self, db):
        """回退路径也必须报弃权数 —— 否则"允许弃权"会变成免费的分数提升。

        弃权行 `label_correct` 为空，回退路径的查询把它们排除在外，此前直接返回
        `abstain_count: None`。少了这一列，模型越难越弃权、命中率自动上升，
        而覆盖率下滑没有任何信号。
        """
        _report(db, "r1", "600519.SH")
        _outcome(db, "r1", "600519.SH", correct=True)
        # 两条弃权：方向中性 + 无方向
        _report(db, "r2", "600519.SH")
        _outcome(db, "r2", "600519.SH", bucket="neutral", correct=None)
        _report(db, "r3", "600519.SH")
        _outcome(db, "r3", "600519.SH", bucket=None, correct=None)
        db.commit()

        points = svc.report_accuracy_t1_trend(db, user_id="u1", scope="all")
        assert points
        assert points[0]["abstain_count"] == 2, (
            f"弃权数应为 2，实际 {points[0]['abstain_count']}"
        )
