"""Tests for the exit engine's direction gate (C1).

The exit engine's price rules are written for a **long** position: the stop sits
below (``px <= stop``) and the profit ladder sits above (``px >= tier``). But
``trade_plans.direction`` can be SELL or HOLD, and a SELL plan's price anchor is a
"bearish thesis invalidated" level, not a long stop.

The old implementation ignored ``direction`` entirely, so a SELL plan whose
invalidation level sat above the current price matched ``px <= stop_price`` on day
one and emitted **清仓 100%** — pushed to real users over WeCom/WPS and turned into
simulated sell orders.

The replacement is a three-valued gate (see ``anchor_gate``):

* ``incoherent`` — direction is explicitly non-BUY, or the anchors prove themselves
  wrong (stop >= entry, ladder <= entry). The long-side rules must not run.
* ``unknown`` — direction is absent and nothing disproves a long reading. The
  judgement still runs (log-only, don't intercept) but the result is marked
  non-actionable so it cannot be pushed or traded.
* ``ok`` — explicitly a coherent long plan.
"""

import pytest

from api.services import exit_engine_service as ees


def _plan(**overrides):
    """A coherent long plan: stop 9.0 below entry 10.5, ladder 12.0 above."""
    plan = {
        "id": "plan-1",
        "symbol": "600519.SH",
        "direction": "BUY",
        "entry_low": 10.0,
        "entry_high": 10.5,
        "hard_stop_price": 9.0,
        "take_profit_ladder": [[12.0, 50.0]],
        "signal_trade_date": "2026-09-01",
    }
    plan.update(overrides)
    return plan


def _evaluate(plan, price, **overrides):
    kwargs = dict(
        symbol="600519.SH",
        name="贵州茅台",
        position=1000.0,
        available_position=1000.0,
        average_cost=10.0,
        price=price,
        plan=plan,
    )
    kwargs.update(overrides)
    return ees.evaluate_holding(**kwargs)


# ---------------------------------------------------------------------------
# anchor_gate: three-valued classification
# ---------------------------------------------------------------------------

def test_coherent_buy_plan_is_ok():
    gate, reason = ees.anchor_gate(_plan())
    assert gate == ees.ANCHOR_OK
    assert reason == ""


def test_english_and_chinese_bullish_directions_both_accepted():
    assert ees.anchor_gate(_plan(direction="BUY"))[0] == ees.ANCHOR_OK
    assert ees.anchor_gate(_plan(direction="看多"))[0] == ees.ANCHOR_OK
    assert ees.anchor_gate(_plan(direction="偏多"))[0] == ees.ANCHOR_OK


@pytest.mark.parametrize("direction", ["SELL", "HOLD", "卖出", "观望", "中性"])
def test_non_bullish_direction_is_incoherent(direction):
    """An explicit non-BUY plan must never be read with long-side semantics."""
    gate, reason = ees.anchor_gate(_plan(direction=direction))
    assert gate == ees.ANCHOR_INCOHERENT
    assert direction in reason or "方向" in reason


def test_stop_at_or_above_entry_is_incoherent():
    gate, reason = ees.anchor_gate(_plan(hard_stop_price=10.5))
    assert gate == ees.ANCHOR_INCOHERENT
    assert "止损" in reason
    assert ees.anchor_gate(_plan(hard_stop_price=11.0))[0] == ees.ANCHOR_INCOHERENT


def test_stop_below_entry_is_coherent():
    assert ees.anchor_gate(_plan(hard_stop_price=10.4))[0] == ees.ANCHOR_OK


def test_ladder_at_or_below_entry_is_incoherent():
    gate, reason = ees.anchor_gate(_plan(take_profit_ladder=[[10.5, 50.0]]))
    assert gate == ees.ANCHOR_INCOHERENT
    assert "止盈" in reason
    assert ees.anchor_gate(_plan(take_profit_ladder=[[9.0, 50.0]]))[0] == ees.ANCHOR_INCOHERENT


def test_missing_direction_is_unknown_not_incoherent():
    plan = _plan()
    plan.pop("direction")
    gate, reason = ees.anchor_gate(plan)
    assert gate == ees.ANCHOR_UNKNOWN
    assert "方向" in reason


def test_missing_direction_but_incoherent_anchors_still_incoherent():
    """Absent direction does not excuse anchors that prove themselves wrong."""
    plan = _plan(hard_stop_price=11.0)
    plan.pop("direction")
    assert ees.anchor_gate(plan)[0] == ees.ANCHOR_INCOHERENT


def test_missing_entry_price_without_direction_is_unknown():
    plan = _plan()
    plan.pop("direction")
    plan.pop("entry_low")
    plan.pop("entry_high")
    assert ees.anchor_gate(plan)[0] == ees.ANCHOR_UNKNOWN


def test_missing_entry_price_with_buy_direction_is_ok():
    plan = _plan()
    plan.pop("entry_low")
    plan.pop("entry_high")
    assert ees.anchor_gate(plan)[0] == ees.ANCHOR_OK


def test_no_plan_is_incoherent():
    assert ees.anchor_gate(None)[0] == ees.ANCHOR_INCOHERENT
    assert ees.anchor_gate({})[0] == ees.ANCHOR_INCOHERENT


def test_long_side_helper_is_boolean_view():
    assert ees.long_side_anchors_coherent(_plan())[0] is True
    assert ees.long_side_anchors_coherent(_plan(direction="SELL"))[0] is False
    unknown = _plan()
    unknown.pop("direction")
    assert ees.long_side_anchors_coherent(unknown)[0] is True


# ---------------------------------------------------------------------------
# the regression: a SELL plan must not produce 清仓
# ---------------------------------------------------------------------------

def test_sell_plan_with_invalidation_above_price_does_not_liquidate():
    """This is the exact harm: stop >= price on day one used to mean 清仓 100%."""
    plan = _plan(direction="SELL", hard_stop_price=12.0, take_profit_ladder=[[8.0, 50.0]])
    decision = _evaluate(plan, 10.0)

    assert decision.action == ees.ACTION_WATCH
    assert decision.suggested_pct == 0.0
    assert decision.suggested_shares == 0
    assert decision.is_actionable_sell is False
    assert ees.TRIGGER_PLAN_NOT_LONG in decision.triggers
    assert ees.TRIGGER_HARD_STOP not in decision.triggers


def test_sell_plan_never_reports_take_profit():
    """A SELL plan's ladder points down; reading it upward is nonsense."""
    plan = _plan(direction="SELL", take_profit_ladder=[[8.0, 50.0]])
    decision = _evaluate(plan, 13.0)
    assert ees.TRIGGER_TAKE_PROFIT not in decision.triggers
    assert decision.action == ees.ACTION_WATCH


def test_hold_plan_does_not_liquidate():
    decision = _evaluate(_plan(direction="HOLD"), 8.0)
    assert decision.action == ees.ACTION_WATCH
    assert decision.suggested_pct == 0.0


def test_buy_plan_with_contradictory_stop_does_not_liquidate():
    """Even a BUY plan whose stop sits above entry would fire on day one."""
    decision = _evaluate(_plan(hard_stop_price=11.0), 10.5)
    assert decision.action == ees.ACTION_WATCH
    assert decision.is_actionable_sell is False
    assert ees.TRIGGER_PLAN_NOT_LONG in decision.triggers


# ---------------------------------------------------------------------------
# coherent long plans keep working exactly as before
# ---------------------------------------------------------------------------

def test_coherent_buy_plan_still_liquidates_on_stop():
    decision = _evaluate(_plan(), 8.5)
    assert decision.action == ees.ACTION_EXIT
    assert decision.suggested_pct == 100.0
    assert decision.suggested_shares == 1000
    assert decision.anchor_gate == ees.ANCHOR_OK
    assert decision.direction_gate_passed is True
    assert decision.is_actionable_sell is True


def test_coherent_buy_plan_still_reduces_on_ladder():
    decision = _evaluate(_plan(), 12.5)
    assert decision.action == ees.ACTION_REDUCE
    assert decision.suggested_pct == 50.0
    assert decision.suggested_shares == 500
    assert decision.is_actionable_sell is True


def test_coherent_buy_plan_holds_when_nothing_triggers():
    decision = _evaluate(_plan(), 10.2)
    assert decision.action == ees.ACTION_HOLD
    assert decision.suggested_pct == 0.0
    assert decision.is_actionable_sell is False


# ---------------------------------------------------------------------------
# log-only tier: absent direction keeps judging but is not actionable
# ---------------------------------------------------------------------------

def test_absent_direction_still_judges_but_is_not_actionable():
    """`unknown` = record, don't intercept. The signal survives; the action doesn't."""
    plan = _plan()
    plan.pop("direction")
    decision = _evaluate(plan, 8.5)

    assert decision.action == ees.ACTION_EXIT
    assert decision.anchor_gate == ees.ANCHOR_UNKNOWN
    assert decision.direction_gate_passed is False
    assert decision.is_actionable_sell is False


def test_absent_direction_judgement_is_exposed_for_observation():
    """The observation tier must be visible, or 'log-only' would be silent."""
    plan = _plan()
    plan.pop("direction")
    decision = _evaluate(plan, 8.5)
    assert decision.plan_direction is None
    assert decision.to_dict()["anchor_gate"] == ees.ANCHOR_UNKNOWN
    assert decision.to_dict()["is_actionable_sell"] is False


def test_unknown_tier_does_not_claim_direction_gate_passed():
    plan = _plan()
    plan.pop("direction")
    assert _evaluate(plan, 8.5).direction_gate_passed is False


# ---------------------------------------------------------------------------
# serialization / contract
# ---------------------------------------------------------------------------

def test_to_dict_exposes_the_gate_fields():
    decision = _evaluate(_plan(), 8.5)
    data = decision.to_dict()
    assert data["plan_direction"] == "BUY"
    assert data["anchor_gate"] == ees.ANCHOR_OK
    assert data["long_side_ok"] is True
    assert data["is_actionable_sell"] is True


def test_incoherent_decision_records_a_user_visible_constraint():
    decision = _evaluate(_plan(direction="SELL"), 10.0)
    assert any("人工复核" in c for c in decision.constraints)


def test_incoherent_reason_explains_why_the_signal_was_suppressed():
    decision = _evaluate(_plan(direction="SELL"), 10.0)
    assert any("多头语义" in r for r in decision.reasons)


def test_unknown_tier_does_not_pollute_executability_constraints():
    """`constraints` documents A-share executability; the gate rides on its own field."""
    plan = _plan()
    plan.pop("direction")
    decision = _evaluate(plan, 8.5)
    assert decision.constraints == []
