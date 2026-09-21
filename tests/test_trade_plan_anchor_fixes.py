"""Tests for the trade-plan anchor fixes B3/B4/B5.

Three defects, all sharing one root cause: a plan's price anchors were written and
read without checking that their *direction* matched the plan's direction.

B3  ``_backfill_plan_anchors`` used ``ladder[0][0]`` — but ``ladder_targets`` sorts
    ascending, so the *lowest* rung (the first trim level) was recorded as the price
    target. That produced reports reading "bullish, target 40% below spot".
B4  ``build_from_extracted`` wrapped a single extracted number into a fake one-rung
    ``take_profit_ladder``, so an extracted scalar masqueraded as a model-authored
    scale-out plan.
B5  ``validate_plan`` only had a ``BUY`` branch and its output was advisory, so a
    SELL/HOLD plan — or any plan whose anchors contradicted its direction — was
    persisted and later read with long-side semantics.
"""

import pytest

from api.services import trade_plan_service as tps

BLOCK = """最终交易建议：买入

<!-- TRADE_PLAN
direction: BUY
entry_low: 10.00
entry_high: 10.50
hard_stop_price: 9.00
take_profit_ladder: 12.0:30, 13.5:40, 15.0:30
time_stop_days: 45
-->
"""


# ---------------------------------------------------------------------------
# B3: the target is the highest rung, not the lowest
# ---------------------------------------------------------------------------

class TestTargetPriceIsHighestRung:
    def test_ladder_is_sorted_ascending_so_index_zero_is_the_lowest(self):
        """Why the old code was wrong: index 0 is the first *trim*, not the target."""
        plan = {"take_profit_ladder": [[15.0, 30.0], [12.0, 30.0], [13.5, 40.0]]}
        assert tps.ladder_targets(plan) == [(12.0, 30.0), (13.5, 40.0), (15.0, 30.0)]

    def test_target_price_takes_the_highest_rung(self):
        plan = {"take_profit_ladder": [[12.0, 30.0], [13.5, 40.0], [15.0, 30.0]]}
        assert tps.plan_target_price(plan) == pytest.approx(15.0)

    def test_target_price_refuses_a_ladder_below_the_entry(self):
        """A "bullish" ladder under the entry price is not a bullish ladder."""
        plan = {
            "direction": "BUY",
            "entry_high": 20.0,
            "take_profit_ladder": [[12.0, 30.0], [15.0, 70.0]],
        }
        assert tps.plan_target_price(plan) is None

    def test_target_price_refuses_a_bearish_plan(self):
        plan = {
            "direction": "SELL",
            "entry_high": 20.0,
            "take_profit_ladder": [[30.0, 50.0]],
        }
        assert tps.plan_target_price(plan) is None

    def test_target_price_absent_when_no_levels(self):
        assert tps.plan_target_price({"direction": "BUY"}) is None
        assert tps.plan_target_price(None) is None

    def test_backfill_uses_the_highest_rung(self):
        from api.main import _backfill_plan_anchors

        result = {
            "final_trade_decision": BLOCK,
            "target_price": None,
            "stop_loss_price": None,
        }
        _backfill_plan_anchors(result)
        assert result["target_price"] == pytest.approx(15.0)
        assert result["stop_loss_price"] == pytest.approx(9.0)
        assert result["target_price_source"] == "trade_plan"

    def test_backfill_never_yields_a_target_below_the_entry(self):
        """The old code produced "bullish + target far below spot"; that must be impossible."""
        from api.main import _backfill_plan_anchors

        text = BLOCK.replace("12.0:30, 13.5:40, 15.0:30", "8.0:30, 9.5:70")
        result = {"final_trade_decision": text, "target_price": None, "stop_loss_price": None}
        _backfill_plan_anchors(result)
        plan = result["trade_plan"]
        entry = plan.get("entry_high") or plan.get("entry_low")
        assert result["target_price"] is None
        assert entry == pytest.approx(10.50)


# ---------------------------------------------------------------------------
# B4: a single extracted target stays a single target
# ---------------------------------------------------------------------------

class TestNoFabricatedLadder:
    def test_extracted_target_is_not_wrapped_into_a_ladder(self):
        plan = tps.build_from_extracted(target_price=14.0, stop_loss_price=11.0)
        assert plan["target_price"] == pytest.approx(14.0)
        assert "take_profit_ladder" not in plan

    def test_extracted_target_is_still_monitorable(self):
        """Honesty must not silently disable monitoring."""
        plan = tps.build_from_extracted(target_price=14.0, stop_loss_price=11.0)
        assert tps.is_monitorable(plan)
        # ...but a target alone (no stop) is not a monitorable exit plan
        assert tps.is_monitorable(tps.build_from_extracted(target_price=14.0, stop_loss_price=None))

    def test_target_levels_unifies_ladder_and_single_target(self):
        ladder = {"take_profit_ladder": [[12.0, 30.0], [15.0, 70.0]]}
        single = {"target_price": 15.0}
        assert tps.target_levels(ladder) == [(12.0, 30.0), (15.0, 70.0)]
        assert tps.target_levels(single) == [(15.0, 100.0)]
        assert tps.target_levels({}) == []

    def test_ladder_wins_when_both_are_present(self):
        both = {"take_profit_ladder": [[12.0, 30.0]], "target_price": 99.0}
        assert tps.target_levels(both) == [(12.0, 30.0)]

    def test_compose_extracted_source_reports_a_single_target(self):
        plan, source, _ = tps.compose_trade_plan(target_price=14.0, stop_loss_price=11.0)
        assert source == tps.SOURCE_EXTRACTED
        assert "take_profit_ladder" not in plan
        assert tps.target_levels(plan) == [(14.0, 100.0)]

    def test_exit_engine_monitors_a_single_target(self):
        from api.services import exit_engine_service as ees

        plan = {
            "id": "p",
            "direction": "BUY",
            "entry_low": 10.0,
            "entry_high": 10.5,
            "hard_stop_price": 9.0,
            "target_price": 12.0,
        }
        decision = ees.evaluate_holding(
            symbol="600519.SH", position=1000.0, available_position=1000.0,
            average_cost=10.0, price=12.5, plan=plan,
        )
        assert ees.TRIGGER_TAKE_PROFIT in decision.triggers
        assert decision.action == ees.ACTION_REDUCE
        assert decision.suggested_pct == 100.0


# ---------------------------------------------------------------------------
# B5: validation is direction-aware and blocks contradictions
# ---------------------------------------------------------------------------

class TestDirectionAwareValidation:
    def test_valid_buy_plan_has_no_problems(self):
        plan = {
            "direction": "BUY", "entry_low": 10.0, "entry_high": 10.5,
            "hard_stop_price": 9.0, "take_profit_ladder": [[12.0, 100.0]],
        }
        assert tps.validate_plan(plan) == []

    def test_buy_stop_above_entry_is_a_problem(self):
        plan = {"direction": "BUY", "entry_high": 10.5, "hard_stop_price": 11.0}
        assert any("止损价" in p for p in tps.validate_plan(plan))

    def test_buy_target_below_entry_is_a_problem(self):
        plan = {"direction": "BUY", "entry_high": 10.5, "take_profit_ladder": [[9.0, 100.0]]}
        assert any("止盈价" in p for p in tps.validate_plan(plan))

    def test_sell_plan_is_validated_too(self):
        """The old code's `direction == "BUY"` guard let every SELL plan through."""
        bad = {"direction": "SELL", "entry_low": 10.0, "hard_stop_price": 9.0}
        assert any("失效价" in p for p in tps.validate_plan(bad))

        good = {"direction": "SELL", "entry_low": 10.0, "hard_stop_price": 12.0}
        assert tps.validate_plan(good) == []

    def test_sell_target_above_entry_is_a_problem(self):
        bad = {"direction": "SELL", "entry_low": 10.0, "take_profit_ladder": [[12.0, 100.0]]}
        assert any("目标位" in p for p in tps.validate_plan(bad))

    def test_hold_plan_must_not_carry_price_anchors(self):
        plan = {"direction": "HOLD", "hard_stop_price": 9.0}
        assert any("持有计划" in p for p in tps.validate_plan(plan))

    def test_unknown_direction_is_flagged_fail_closed(self):
        assert any("未标明方向" in p for p in tps.validate_plan({"hard_stop_price": 9.0}))
        assert any("无法识别" in p for p in tps.validate_plan({"direction": "maybe"}))

    def test_inverted_entry_range_is_flagged(self):
        plan = {"direction": "BUY", "entry_low": 12.0, "entry_high": 10.0, "hard_stop_price": 9.0}
        assert any("入场区间" in p for p in tps.validate_plan(plan))

    def test_non_positive_position_cap_is_flagged(self):
        assert any("正数" in p for p in tps.validate_plan({"direction": "BUY", "position_cap_pct": 0}))

    def test_missing_stop_for_buy_is_flagged_but_not_blocking(self):
        plan = {"direction": "BUY", "entry_high": 10.5}
        assert any("缺少硬止损" in p for p in tps.validate_plan(plan))
        assert tps.blocking_plan_problems(plan) == []

    def test_missing_direction_is_advisory_only(self):
        """Incompleteness is not contradiction — don't reject plans for it."""
        plan = {"hard_stop_price": 9.0, "entry_high": 10.5}
        assert tps.blocking_plan_problems(plan) == []

    @pytest.mark.parametrize(
        "plan",
        [
            {"direction": "BUY", "entry_high": 10.5, "hard_stop_price": 11.0},
            {"direction": "BUY", "entry_high": 10.5, "take_profit_ladder": [[9.0, 100.0]]},
            {"direction": "SELL", "entry_low": 10.0, "hard_stop_price": 9.0},
            {"direction": "SELL", "entry_low": 10.0, "take_profit_ladder": [[12.0, 100.0]]},
            {"direction": "HOLD", "hard_stop_price": 9.0},
            {"direction": "maybe", "hard_stop_price": 9.0},
        ],
    )
    def test_contradictory_plans_are_blocking(self, plan):
        assert tps.blocking_plan_problems(plan) != []

    def test_blocking_problems_is_a_subset_of_validate_plan(self):
        """One source of truth: blocking must never invent a problem validate_plan missed."""
        for plan in (
            {"direction": "BUY", "entry_high": 10.5, "hard_stop_price": 11.0},
            {"direction": "SELL", "entry_low": 10.0, "hard_stop_price": 9.0},
            {"direction": "BUY"},
        ):
            assert set(tps.blocking_plan_problems(plan)) <= set(tps.validate_plan(plan))


class TestPersistenceRefusesContradictoryAnchors:
    def _db(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from api.database import Base

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        return sessionmaker(bind=engine)()

    def test_contradictory_plan_is_not_persisted(self):
        db = self._db()
        row = tps.upsert_trade_plan(
            db,
            user_id="u",
            report_id="r1",
            symbol="600519.SH",
            plan={"direction": "BUY", "entry_high": 10.5, "hard_stop_price": 11.0},
        )
        assert row is None
        assert db.query(tps.TradePlanDB).count() == 0

    def test_incomplete_plan_is_still_persisted(self):
        """A plan missing a stop or a direction is incomplete, not contradictory."""
        db = self._db()
        row = tps.upsert_trade_plan(
            db,
            user_id="u",
            report_id="r2",
            symbol="600519.SH",
            plan={"hard_stop_price": 9.0},
        )
        assert row is not None
        assert db.query(tps.TradePlanDB).count() == 1

    def test_coherent_buy_plan_is_persisted(self):
        db = self._db()
        row = tps.upsert_trade_plan(
            db,
            user_id="u",
            report_id="r3",
            symbol="600519.SH",
            plan={
                "direction": "BUY", "entry_low": 10.0, "entry_high": 10.5,
                "hard_stop_price": 9.0, "take_profit_ladder": [[12.0, 100.0]],
            },
        )
        assert row is not None
        assert row.direction == "BUY"


class TestTargetPriceRoundTripsThroughTheDb:
    def test_target_price_survives_persistence(self):
        """B4's honest field must not be dropped on the way to the database."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from api.database import Base

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()

        row = tps.upsert_trade_plan(
            db,
            user_id="u",
            report_id="r4",
            symbol="600519.SH",
            plan={"direction": "BUY", "entry_high": 10.5, "hard_stop_price": 9.0, "target_price": 14.0},
        )
        assert row is not None
        assert row.target_price == pytest.approx(14.0)

        restored = tps.plan_dict_from_row(row)
        assert restored["target_price"] == pytest.approx(14.0)
        assert tps.target_levels(restored) == [(14.0, 100.0)]
