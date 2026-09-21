"""F4: the buyability gate must actually see the chase risk it claims to filter.

``filter_buyable`` advertises three individual-stock risk gates — limit-up streak
(``lianban``), turnover band, and "near limit-up" (``chg_1d``) — but on the live
payload every one of them is inert. The stored candidates in
``mainline_reports.candidates`` carry exactly:

    symbol, name, mainline, tier, score, reasons, entry_hint, risk

Measured over the 60 most recent reports (224 candidates): ``lianban``, ``turnover``,
``chg_1d`` and ``source`` are absent in **all** of them. So

* ``candidate.get("lianban") or 0`` is always ``0`` → never ``>= 4``;
* ``turnover is not None`` is always ``False``;
* ``source == "cons"`` is always ``False`` (``source`` is ``None``).

The only live gate was the mainline-cycle check, which is about the *sector*, not the
stock. Meanwhile the LLM's own ``entry_hint``/``risk`` text says things like
"不建议次日高开追入", "建议不追高", "禁止追高", "避免在当日高点附近接盘" — and the
gate ignored all of it, marking the candidate buyable and assigning a position tier.

That matters because the measured next-day relationship is negative for extended
names (``docs/short-horizon-alpha-probe.md`` Table A: ``mom_5`` −0.0212, t=−2.51;
``dist_ma20`` −0.0294; ``range_10`` −0.0419, t=−3.53; ``vol_shock`` −0.0331, t=−5.36),
and always-long drifts to −1.59% with a 42.1% up-rate over the following 20 days.
The system was green-lighting precisely the entries its own analysis told it to skip.
"""

import pytest

from api.services.mainline_autodive_service import (
    CHASE_1D_PCT,
    MAX_LIANBAN,
    TURNOVER_MAX,
    detect_chase_risk,
    filter_buyable,
)

# Field set actually observed in production (all 224 candidates).
LIVE_CANDIDATE = {
    "symbol": "688981.SH",
    "name": "中芯国际",
    "mainline": "半导体设备",
    "tier": "核心",
    "score": 88,
    "reasons": ["主线龙头", "资金持续流入"],
    "entry_hint": "回踩5日线分批低吸，作为底仓配置",
    "risk": "板块退潮时回撤可能加大",
}


def _cand(**overrides):
    cand = dict(LIVE_CANDIDATE)
    cand.update(overrides)
    return cand


# ---------------------------------------------------------------------------
# the inert-gate finding, pinned
# ---------------------------------------------------------------------------

class TestLegacyNumericGatesWereInert:
    @pytest.mark.parametrize("missing", ["lianban", "turnover", "chg_1d", "source"])
    def test_live_payload_lacks_the_numeric_risk_fields(self, missing):
        assert missing not in LIVE_CANDIDATE

    def test_a_real_candidate_passes_the_sector_gate_untouched(self):
        buyable, reason = filter_buyable(LIVE_CANDIDATE, cycle_position="主升")
        assert buyable, reason

    def test_missing_source_no_longer_waives_the_chase_check(self):
        """The old `source == "cons"` guard meant non-cons candidates were never checked."""
        extended = _cand(chg_1d=CHASE_1D_PCT + 1.0)
        buyable, reason = filter_buyable(extended, cycle_position="主升")
        assert not buyable
        assert "追高" in reason


# ---------------------------------------------------------------------------
# numeric evidence, when it is present
# ---------------------------------------------------------------------------

class TestNumericChaseEvidence:
    @pytest.mark.parametrize("chg", [CHASE_1D_PCT, 8.6, 9.9, 12.0])
    def test_single_day_gain_at_or_above_threshold_is_chase(self, chg):
        assert detect_chase_risk(_cand(chg_1d=chg)) is not None

    @pytest.mark.parametrize("chg", [0.0, 2.5, CHASE_1D_PCT - 0.1])
    def test_modest_gain_is_not_chase(self, chg):
        assert detect_chase_risk(_cand(chg_1d=chg)) is None

    def test_threshold_is_tighter_than_the_old_near_limit_up_rule(self):
        """9.9% only caught near-limit-up; the measured negative IC starts far lower."""
        assert CHASE_1D_PCT < 9.9

    def test_non_numeric_gain_is_ignored_not_crashed(self):
        assert detect_chase_risk(_cand(chg_1d="8.6%")) is None

    @pytest.mark.parametrize("lianban", [MAX_LIANBAN, 5])
    def test_limit_up_streak_is_chase(self, lianban):
        assert detect_chase_risk(_cand(lianban=lianban)) is not None

    def test_low_limit_up_streak_is_fine(self):
        assert detect_chase_risk(_cand(lianban=1)) is None

    def test_overheated_turnover_is_chase(self):
        assert detect_chase_risk(_cand(turnover=TURNOVER_MAX + 1)) is not None

    def test_healthy_turnover_is_fine(self):
        assert detect_chase_risk(_cand(turnover=8.0)) is None


# ---------------------------------------------------------------------------
# textual evidence — the only evidence that exists today
# ---------------------------------------------------------------------------

class TestTextualChaseEvidence:
    @pytest.mark.parametrize(
        "text",
        [
            "当日已涨8.6%且换手极低，属缩量强势，不建议次日高开追入；等待回踩5日均线",
            "与华海清科同理，已接近8%涨幅属当日强势末端，建议不追高",
            "未封板且换手21.3%属高位分歧，禁止追高；等回踩5日线缩量企稳后轻仓试错",
            "跟随板块节奏，回踩5日线不破可参与，避免在当日高点附近接盘",
            "已3连板，追高风险大；不宜盲目打板，等开板回踩5日线企稳",
        ],
    )
    def test_real_chase_warnings_are_detected(self, text):
        assert detect_chase_risk(_cand(entry_hint=text, risk="")) is not None

    @pytest.mark.parametrize(
        "text",
        [
            "高换手低涨幅亦可能是多空分歧激烈或筹码派发，若次日不能放量上行，容易转为高位滞涨",
            "补涨股在主升末端易出现'一日游'，若主线退潮将首先被抛弃",
        ],
    )
    def test_generic_risk_narrative_is_not_relabelled_as_chase(self, text):
        """Narrative risk ≠ chase risk.

        These are real candidate ``risk`` strings. They warn about holding risk, not
        about entering after a run-up, so they must not be counted as chase. In
        production they were flagged only incidentally, via other text in the same
        candidate's ``risk`` field — not because the sentence itself is a chase
        warning. Keeping the two apart stops the filter from drifting into
        "flags anything that sounds cautious".
        """
        assert detect_chase_risk(_cand(entry_hint=text, risk="", reasons=[])) is None

    def test_warning_may_live_in_the_risk_field(self):
        cand = _cand(entry_hint="回踩5日线低吸", risk="主线处于高位分歧档，回撤可能较急")
        assert detect_chase_risk(cand) is not None

    def test_warning_may_live_in_reasons(self):
        cand = _cand(entry_hint="低吸", risk="", reasons=["高位接力风险较大", "板块分歧"])
        assert detect_chase_risk(cand) is not None

    @pytest.mark.parametrize(
        "text",
        [
            "板块不破5日线前提下，回踩5日线分批低吸，作为底仓配置",
            "低位滞后品种，建议等板块指数次日确认不破位、且个股放量站上5日线后再介入",
            "回踩至当日涨幅一半位置分批建仓",
        ],
    )
    def test_constructive_entry_advice_is_not_flagged(self, text):
        assert detect_chase_risk(_cand(entry_hint=text, risk="", reasons=[])) is None

    def test_empty_candidate_is_not_flagged(self):
        assert detect_chase_risk({}) is None

    def test_bare_gain_word_without_magnitude_is_not_enough(self):
        """"已涨" alone would also match "已涨0.3%" — deliberately not a pattern."""
        assert detect_chase_risk(_cand(entry_hint="该股已涨，可低吸", risk="", reasons=[])) is None


# ---------------------------------------------------------------------------
# the gate itself
# ---------------------------------------------------------------------------

class TestFilterBuyable:
    def test_sector_gate_still_wins(self):
        buyable, reason = filter_buyable(LIVE_CANDIDATE, cycle_position="退潮")
        assert not buyable
        assert "非布局窗口" in reason

    def test_chase_risk_blocks_a_buyable_candidate(self):
        buyable, reason = filter_buyable(
            _cand(entry_hint="未封板且换手21.3%属高位分歧，禁止追高"),
            cycle_position="主升",
        )
        assert not buyable
        assert "追涨" in reason

    def test_clean_candidate_passes(self):
        buyable, reason = filter_buyable(LIVE_CANDIDATE, cycle_position="主升")
        assert buyable
        assert reason == "通过可买入闸门"

    def test_pending_confirmation_stage_still_works(self):
        buyable, _ = filter_buyable(LIVE_CANDIDATE, cycle_position="发酵(待确认)")
        assert buyable

    def test_filter_is_pure_and_repeatable(self):
        cand = _cand(entry_hint="禁止追高")
        first = filter_buyable(cand, cycle_position="主升")
        second = filter_buyable(cand, cycle_position="主升")
        assert first == second
        assert cand == _cand(entry_hint="禁止追高")
