"""持仓退出评估引擎（确定性，无 LLM）单元测试。

覆盖：硬止损 / 移动止盈 / 分批止盈（含多档取最高档）/ 时间止损 / 逻辑失效复核 /
无计划 / 停牌无行情 / 默认持有，以及 A 股可执行性校正（T+1、100 股整手、涨跌停）。

所有断言都是确定性的数值与字符串比较——本模块不调用模型、不访问网络。
"""

from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.database import Base, ImportedPortfolioPositionDB
from api.services import exit_engine_service as ees
from api.services import trade_plan_service as tps


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


# --------------------------------------------------------------------------- #
# 测试夹具
# --------------------------------------------------------------------------- #

# 一份"完整可用"的计划：有硬止损、三档止盈、移动止盈，但没有逻辑失效条件
# （有失效条件的计划必然落到"观察/人工复核"，所以默认持有类测试不能用它）。
BASE_PLAN = {
    "id": "plan-1",
    "symbol": "002384.SZ",
    "signal_trade_date": "2026-06-01",
    "hard_stop_price": 11.50,
    "take_profit_ladder": [[13.5, 30.0], [14.8, 40.0], [16.0, 30.0]],
    "trailing_stop_pct": 8.0,
    "source": tps.SOURCE_BLOCK,
}

# 只带硬止损的极简计划：用于隔离单一规则
STOP_ONLY_PLAN = {
    "id": "plan-2",
    "symbol": "600000.SH",
    "hard_stop_price": 9.50,
    "source": tps.SOURCE_EXTRACTED,
}

# 只带硬止损 + 移动止盈（没有止盈阶梯）：用于隔离移动止盈规则
TRAILING_ONLY_PLAN = {
    "id": "plan-3",
    "symbol": "600000.SH",
    "hard_stop_price": 5.00,
    "trailing_stop_pct": 8.0,
    "source": tps.SOURCE_BLOCK,
}


def _holding(**overrides):
    """默认持仓：1000 股、全部可卖、成本 12 元。"""
    kwargs = dict(
        symbol="002384.SZ",
        name="东山精密",
        position=1000.0,
        available_position=1000.0,
        average_cost=12.0,
    )
    kwargs.update(overrides)
    return ees.evaluate_holding(**kwargs)


def _add_position(db, *, user_id, symbol, position, available, cost, name="标的", source="manual"):
    row = ImportedPortfolioPositionDB(
        id=uuid4().hex,
        user_id=user_id,
        source=source,
        symbol=symbol,
        security_name=name,
        current_position=position,
        available_position=available,
        average_cost=cost,
    )
    db.add(row)
    db.commit()
    return row


def _add_plan(db, *, user_id, symbol, report_id, plan, signal_trade_date=None):
    row = tps.upsert_trade_plan(
        db,
        user_id=user_id,
        report_id=report_id,
        symbol=symbol,
        signal_trade_date=signal_trade_date,
        plan=plan,
        source=plan.get("source") or tps.SOURCE_BLOCK,
    )
    db.commit()
    return row


# --------------------------------------------------------------------------- #
# 规则 3：硬止损
# --------------------------------------------------------------------------- #


class TestHardStop:
    def test_below_stop_liquidates(self):
        d = _holding(price=11.40, plan=dict(BASE_PLAN))
        assert d.action == "清仓"
        assert d.priority == "high"
        assert "hard_stop" in d.triggers
        assert d.suggested_pct == 100.0
        assert d.suggested_shares == 1000
        assert "11.5" in d.reasons[0] and "硬止损" in d.reasons[0]

    def test_price_equal_to_stop_liquidates(self):
        d = _holding(price=11.50, plan=dict(BASE_PLAN))
        assert d.action == "清仓"
        assert "hard_stop" in d.triggers

    def test_just_above_stop_holds(self):
        d = _holding(price=11.51, plan=dict(BASE_PLAN))
        assert d.action == "持有"
        assert d.priority == "low"
        assert d.triggers == []
        assert d.suggested_shares == 0
        assert d.suggested_pct == 0.0

    def test_stop_reason_mentions_signal_date(self):
        plan = dict(BASE_PLAN)
        plan["signal_trade_date"] = "2026-06-01"
        d = _holding(price=11.0, plan=plan)
        assert "2026-06-01" in d.reasons[0]

    def test_stop_reason_without_signal_date_is_graceful(self):
        plan = dict(STOP_ONLY_PLAN)
        d = _holding(symbol="600000.SH", price=9.0, plan=plan)
        assert d.action == "清仓"
        assert "—" in d.reasons[0]

    def test_zero_stop_means_not_applicable(self):
        plan = {"hard_stop_price": 0, "take_profit_ladder": [[30.0, 100.0]]}
        d = _holding(price=15.0, plan=plan)
        assert "hard_stop" not in d.triggers
        assert d.action == "持有"


# --------------------------------------------------------------------------- #
# 规则 4：移动止盈
# --------------------------------------------------------------------------- #


class TestTrailingStop:
    def test_drawdown_from_peak_triggers(self):
        # 高点 20，回撤阈值 8% → 触发价 18.4
        d = _holding(price=18.30, peak_price=20.0, plan=dict(TRAILING_ONLY_PLAN))
        assert d.action == "减仓"
        assert d.priority == "high"
        assert "trailing_stop" in d.triggers
        assert d.suggested_pct == 50.0
        assert d.suggested_shares == 500
        assert "20" in d.reasons[0]

    def test_drawdown_exactly_at_threshold_triggers(self):
        d = _holding(price=18.40, peak_price=20.0, plan=dict(TRAILING_ONLY_PLAN))
        assert "trailing_stop" in d.triggers
        assert d.action == "减仓"

    def test_drawdown_not_enough_does_not_trigger(self):
        d = _holding(price=18.50, peak_price=20.0, plan=dict(TRAILING_ONLY_PLAN))
        assert "trailing_stop" not in d.triggers
        assert d.action == "持有"

    def test_without_peak_never_triggers(self):
        d = _holding(price=12.0, peak_price=None, plan=dict(TRAILING_ONLY_PLAN))
        assert "trailing_stop" not in d.triggers
        assert d.action == "持有"

    def test_without_peak_hard_stop_still_works(self):
        plan = dict(BASE_PLAN)
        d = _holding(price=5.0, peak_price=None, plan=plan)
        assert "trailing_stop" not in d.triggers
        # 5.0 同时跌破硬止损 11.5，硬止损应接管
        assert d.action == "清仓"
        assert "hard_stop" in d.triggers

    def test_without_trailing_pct_never_triggers(self):
        plan = dict(STOP_ONLY_PLAN)
        d = _holding(symbol="600000.SH", price=9.9, peak_price=12.0, plan=plan)
        assert "trailing_stop" not in d.triggers
        assert d.action == "持有"

    def test_zero_peak_is_ignored(self):
        d = _holding(price=11.0, peak_price=0, plan=dict(TRAILING_ONLY_PLAN))
        assert "trailing_stop" not in d.triggers


# --------------------------------------------------------------------------- #
# 规则 5：分批止盈
# --------------------------------------------------------------------------- #


class TestTakeProfit:
    @pytest.mark.parametrize(
        "price,expected_pct,expected_tier",
        [
            (13.50, 30.0, "13.5"),
            (14.80, 40.0, "14.8"),
            (16.00, 30.0, "16"),
        ],
    )
    def test_single_tier(self, price, expected_pct, expected_tier):
        d = _holding(price=price, plan=dict(BASE_PLAN))
        assert d.action == "减仓"
        assert d.priority == "medium"
        assert d.triggers == ["take_profit"]
        assert d.suggested_pct == expected_pct
        assert expected_tier in d.reasons[0]

    def test_between_tiers_uses_lowest_reached(self):
        d = _holding(price=14.00, plan=dict(BASE_PLAN))
        assert d.suggested_pct == 30.0
        assert "13.5" in d.reasons[0]

    def test_multi_tier_uses_highest_reached(self):
        d = _holding(price=15.00, plan=dict(BASE_PLAN))
        assert d.suggested_pct == 40.0, "应取已触及的最高档（14.8 → 40%）"
        assert "14.8" in d.reasons[0]
        assert any("2 个止盈档位" in r for r in d.reasons)

    def test_all_tiers_reached_uses_top_tier(self):
        d = _holding(price=20.00, plan=dict(BASE_PLAN))
        assert d.action == "减仓"
        assert d.suggested_pct == 30.0
        assert any("3 个止盈档位" in r for r in d.reasons)

    def test_below_all_tiers_holds(self):
        d = _holding(price=12.00, plan=dict(BASE_PLAN))
        assert "take_profit" not in d.triggers
        assert d.action == "持有"

    def test_hard_stop_dominates_take_profit(self):
        """脏数据：计划止损价高于现价（计划本身就异常）且现价已过止盈档——止损优先。"""
        plan = dict(BASE_PLAN)
        plan["hard_stop_price"] = 22.0
        d = _holding(price=21.0, plan=plan)
        assert d.action == "清仓"
        assert d.priority == "high"
        assert d.triggers[0] == "hard_stop"
        assert "take_profit" in d.triggers
        assert d.suggested_pct == 100.0

    def test_trailing_stop_dominates_take_profit(self):
        plan = dict(BASE_PLAN)
        plan["hard_stop_price"] = 5.0
        d = _holding(price=14.00, peak_price=20.0, plan=plan)
        assert d.action == "减仓"
        assert d.priority == "high"
        assert d.triggers[0] == "trailing_stop"
        assert "take_profit" in d.triggers
        assert d.suggested_pct == 50.0


# --------------------------------------------------------------------------- #
# 规则 6：时间止损
# --------------------------------------------------------------------------- #


class TestTimeStop:
    def test_time_stop_fires(self):
        plan = dict(BASE_PLAN)
        plan["time_stop_days"] = 10
        d = _holding(price=12.50, trading_days_held=10, plan=plan)
        assert d.action == "观察"
        assert d.priority == "low"
        assert "time_stop" in d.triggers
        assert d.suggested_shares == 0
        assert "10 个交易日" in d.reasons[0]

    def test_time_stop_not_yet(self):
        plan = dict(BASE_PLAN)
        plan["time_stop_days"] = 10
        d = _holding(price=12.50, trading_days_held=9, plan=plan)
        assert "time_stop" not in d.triggers
        assert d.action == "持有"

    def test_unknown_days_held_never_fires(self):
        plan = dict(BASE_PLAN)
        plan["time_stop_days"] = 10
        d = _holding(price=12.50, trading_days_held=None, plan=plan)
        assert "time_stop" not in d.triggers
        assert d.action == "持有"

    def test_take_profit_beats_time_stop(self):
        plan = dict(BASE_PLAN)
        plan["time_stop_days"] = 3
        d = _holding(price=13.60, trading_days_held=30, plan=plan)
        assert d.action == "减仓"
        assert "take_profit" in d.triggers
        assert "time_stop" not in d.triggers


# --------------------------------------------------------------------------- #
# 规则 7：逻辑失效复核
# --------------------------------------------------------------------------- #


class TestInvalidationReview:
    def test_conditions_are_always_surfaced(self):
        plan = dict(BASE_PLAN)
        plan["invalidation_conditions"] = ["跌破20日线", "毛利率低于15%"]
        d = _holding(price=12.50, plan=plan)
        assert "invalidation_review" in d.triggers
        assert d.reasons[-1] == "需人工复核计划失效条件：跌破20日线；毛利率低于15%"
        # 没有任何价格触发时，动作是"观察/medium"而不是"持有"
        assert d.action == "观察"
        assert d.priority == "medium"

    def test_conditions_surfaced_alongside_hard_stop(self):
        plan = dict(BASE_PLAN)
        plan["invalidation_conditions"] = ["跌破20日线"]
        d = _holding(price=11.0, plan=plan)
        assert d.action == "清仓"
        assert d.priority == "high"
        assert "hard_stop" in d.triggers
        assert "invalidation_review" in d.triggers

    def test_conditions_do_not_downgrade_take_profit_action(self):
        plan = dict(BASE_PLAN)
        plan["invalidation_conditions"] = ["跌破20日线"]
        d = _holding(price=13.60, plan=plan)
        assert d.action == "减仓"
        assert d.suggested_pct == 30.0
        assert "invalidation_review" in d.triggers

    def test_no_conditions_no_review_trigger(self):
        d = _holding(price=12.50, plan=dict(BASE_PLAN))
        assert "invalidation_review" not in d.triggers

    def test_time_stop_keeps_its_priority_over_invalidation(self):
        plan = dict(BASE_PLAN)
        plan["time_stop_days"] = 5
        plan["invalidation_conditions"] = ["跌破20日线"]
        d = _holding(price=12.50, trading_days_held=5, plan=plan)
        # 时间止损已给出"观察/low"，失效复核只追加提示，不改变已定优先级
        assert d.action == "观察"
        assert d.priority == "low"
        assert "time_stop" in d.triggers
        assert "invalidation_review" in d.triggers

    def test_conditions_surfaced_even_when_suspended(self):
        """失效条件与行情无关，停牌时同样不能被吞掉。"""
        plan = dict(BASE_PLAN)
        plan["invalidation_conditions"] = ["商誉减值超5亿"]
        d = _holding(price=None, plan=plan, suspended=True)
        assert d.action == "观察"
        assert d.priority == "low"
        assert "suspended" in d.triggers
        assert "invalidation_review" in d.triggers
        assert any("商誉减值超5亿" in r for r in d.reasons)


# --------------------------------------------------------------------------- #
# 规则 2：没有可监控计划
# --------------------------------------------------------------------------- #


class TestNoPlan:
    @pytest.mark.parametrize("plan", [None, {}])
    def test_missing_plan_is_observed(self, plan):
        d = _holding(price=12.50, plan=plan)
        assert d.action == "观察"
        assert d.priority == "medium"
        assert d.triggers == ["no_plan"]
        assert d.plan_available is False
        assert "002384.SZ" in d.reasons[0]
        assert d.suggested_shares == 0

    def test_unmonitorable_plan_counts_as_no_plan(self):
        plan = {"hard_constraints": ["总仓不超10%"], "de_risk_triggers": ["单日跌幅超5%"]}
        d = _holding(price=12.50, plan=plan)
        assert d.action == "观察"
        assert "no_plan" in d.triggers
        assert d.plan_available is False

    def test_garbage_plan_object_is_ignored(self):
        """脏数据防御：计划不是字典时视为没有计划，绝不抛异常。"""
        d = _holding(price=12.50, plan="这不是计划")
        assert d.action == "观察"
        assert "no_plan" in d.triggers
        assert d.plan_available is False

    def test_plan_carrying_only_entry_price_is_monitorable(self):
        d = _holding(price=12.50, plan={"entry_low": 12.0, "entry_high": 12.9})
        assert d.plan_available is True
        assert d.action == "持有"

    def test_hard_stop_requires_a_plan(self):
        """没有计划就没有止损锚点——绝不能凭空清仓。"""
        d = _holding(price=5.0, plan=None)
        assert d.action == "观察"
        assert d.suggested_shares == 0


# --------------------------------------------------------------------------- #
# 规则 1：停牌 / 无行情
# --------------------------------------------------------------------------- #


class TestSuspendedAndNoQuote:
    def test_suspended_flag(self):
        d = _holding(price=12.0, plan=dict(BASE_PLAN), suspended=True)
        assert d.action == "观察"
        assert d.priority == "low"
        assert d.triggers == ["suspended"]
        assert "停牌期间无法成交" in d.constraints
        assert "无法评估" in d.reasons[0]

    def test_missing_price(self):
        d = _holding(price=None, plan=dict(BASE_PLAN))
        assert d.action == "观察"
        assert "suspended" in d.triggers
        assert "停牌期间无法成交" in d.constraints

    def test_zero_price(self):
        d = _holding(price=0, plan=dict(BASE_PLAN))
        assert d.action == "观察"
        assert "suspended" in d.triggers

    def test_suspended_still_reports_plan_availability(self):
        d = _holding(price=None, plan=dict(BASE_PLAN))
        assert d.plan_available is True
        assert d.suggested_shares == 0

    def test_negative_price_treated_as_missing(self):
        d = _holding(price=-1.0, plan=dict(BASE_PLAN))
        assert d.action == "观察"


# --------------------------------------------------------------------------- #
# A 股可执行性：T+1 / 整手 / 涨跌停
# --------------------------------------------------------------------------- #


class TestT1AndLotRules:
    def test_t1_blocks_sell_but_keeps_signal(self):
        d = _holding(price=11.0, position=1000, available_position=0, plan=dict(BASE_PLAN))
        assert d.action == "清仓", "T+1 阻断不能把该止损这件事抹掉"
        assert d.priority == "high"
        assert d.suggested_shares == 0
        assert "t1_blocked" in d.triggers
        assert "T+1：今日买入的股份不可卖出，需等下一交易日" in d.constraints

    def test_t1_blocks_reduce_as_well(self):
        d = _holding(price=13.60, position=1000, available_position=0, plan=dict(BASE_PLAN))
        assert d.action == "减仓"
        assert d.suggested_pct == 30.0
        assert d.suggested_shares == 0
        assert "t1_blocked" in d.triggers

    def test_t1_no_constraint_when_nothing_to_sell(self):
        d = _holding(price=12.50, position=1000, available_position=0, plan=dict(BASE_PLAN))
        assert d.action == "持有"
        assert "t1_blocked" not in d.triggers
        assert d.constraints == []

    def test_available_position_clamps_suggestion(self):
        d = _holding(price=13.60, position=1000, available_position=300, plan=dict(BASE_PLAN))
        assert d.suggested_pct == 30.0
        assert d.suggested_shares == 300

    def test_lot_rounding_floors_to_hundred(self):
        # 1050 * 30% = 315 → 300 股
        d = _holding(price=13.60, position=1050, available_position=1050, plan=dict(BASE_PLAN))
        assert d.suggested_shares == 300

    def test_lot_rounding_full_position(self):
        d = _holding(price=11.0, position=1234, available_position=1234, plan=dict(BASE_PLAN))
        assert d.suggested_shares == 1200

    def test_sub_lot_position_can_be_sold_entirely(self):
        d = _holding(price=11.0, position=50, available_position=50, plan=dict(BASE_PLAN))
        assert d.action == "清仓"
        assert d.suggested_shares == 50

    def test_sub_lot_available_uses_exact_remainder(self):
        # 持仓 1000，其中 950 是今日买入 → 只有 50 股可卖
        d = _holding(price=11.0, position=1000, available_position=50, plan=dict(BASE_PLAN))
        assert d.action == "清仓"
        assert d.suggested_shares == 50

    def test_tiny_reduce_does_not_round_up_to_whole_position(self):
        """1% 的减仓信号（1000 股 → 10 股，不足一手）不能因为整手取整变成"清仓"。"""
        plan = dict(BASE_PLAN)
        plan["take_profit_ladder"] = [[12.0, 1.0]]
        d = _holding(price=12.50, position=1000, available_position=1000, plan=plan)
        assert d.action == "减仓"
        assert d.suggested_pct == 1.0
        assert d.suggested_shares == 0

    def test_no_position_no_shares(self):
        d = _holding(price=11.0, position=0, available_position=0, plan=dict(BASE_PLAN))
        assert d.suggested_shares == 0


class TestLimitBoards:
    def test_main_board_limit_up_detected(self):
        d = _holding(symbol="600000.SH", name="浦发银行", price=11.0,
                     previous_close=10.0, change_pct=10.0, plan=dict(STOP_ONLY_PLAN))
        assert "limit_up_blocked" in d.triggers
        assert "已接近/封住涨停，卖单可能无法成交" in d.constraints

    def test_near_limit_up_within_tolerance(self):
        d = _holding(symbol="600000.SH", name="浦发银行", price=10.99,
                     change_pct=9.9, plan=dict(STOP_ONLY_PLAN))
        assert "limit_up_blocked" in d.triggers

    def test_moderate_gain_not_flagged(self):
        d = _holding(symbol="600000.SH", name="浦发银行", price=10.5,
                     change_pct=5.0, plan=dict(STOP_ONLY_PLAN))
        assert "limit_up_blocked" not in d.triggers
        assert d.constraints == []

    def test_chinext_uses_20_pct_band(self):
        d = _holding(symbol="300123.SZ", name="某某科技", price=22.0,
                     change_pct=19.9, plan=dict(STOP_ONLY_PLAN))
        assert "limit_up_blocked" in d.triggers

    def test_chinext_10_pct_is_not_limit_up(self):
        d = _holding(symbol="300123.SZ", name="某某科技", price=20.0,
                     change_pct=9.8, plan=dict(STOP_ONLY_PLAN))
        assert "limit_up_blocked" not in d.triggers

    def test_st_stock_uses_5_pct_band(self):
        d = _holding(symbol="600000.SH", name="ST某某", price=10.5,
                     change_pct=4.95, plan=dict(STOP_ONLY_PLAN))
        assert "limit_up_blocked" in d.triggers

    def test_beijing_exchange_uses_30_pct_band(self):
        d = _holding(symbol="430047.BJ", name="某某股份", price=13.0,
                     change_pct=29.9, plan=dict(STOP_ONLY_PLAN))
        assert "limit_up_blocked" in d.triggers

    def test_limit_down_detected(self):
        d = _holding(symbol="600000.SH", name="浦发银行", price=9.0,
                     change_pct=-10.0, plan=dict(STOP_ONLY_PLAN))
        assert "limit_down_blocked" in d.triggers
        assert "已接近/封住跌停，卖单可能无法成交" in d.constraints

    def test_limit_up_does_not_zero_the_suggestion(self):
        """涨停卖出很难但不是不可能——只提示，不清零。"""
        plan = dict(STOP_ONLY_PLAN)
        plan["take_profit_ladder"] = [[10.0, 100.0]]
        d = _holding(symbol="600000.SH", name="浦发银行", price=11.0,
                     change_pct=10.0, plan=plan)
        assert d.action == "减仓"
        assert d.suggested_shares == 1000
        assert "limit_up_blocked" in d.triggers

    def test_change_pct_derived_from_previous_close(self):
        d = _holding(symbol="600000.SH", name="浦发银行", price=11.0,
                     previous_close=10.0, plan=dict(STOP_ONLY_PLAN))
        assert "limit_up_blocked" in d.triggers

    def test_limit_constraints_apply_to_hold_decisions_too(self):
        d = _holding(symbol="600000.SH", name="浦发银行", price=10.45,
                     change_pct=4.5, plan=dict(STOP_ONLY_PLAN))
        assert d.action == "持有"
        assert d.constraints == []


# --------------------------------------------------------------------------- #
# 浮动盈亏与字段契约
# --------------------------------------------------------------------------- #


class TestFieldsAndContract:
    def test_pnl_pct_computed(self):
        d = _holding(price=12.60, average_cost=12.0, plan=dict(BASE_PLAN))
        assert d.pnl_pct == pytest.approx(5.0)

    def test_pnl_pct_negative(self):
        d = _holding(price=11.40, average_cost=12.0, plan=dict(BASE_PLAN))
        assert d.pnl_pct == pytest.approx(-5.0)

    def test_pnl_pct_none_without_cost(self):
        d = _holding(price=12.0, average_cost=None, plan=dict(BASE_PLAN))
        assert d.pnl_pct is None

    def test_position_fields_echoed_back(self):
        d = _holding(position=800, available_position=300, average_cost=10.5, price=11.0)
        assert d.position == 800.0
        assert d.available_position == 300.0
        assert d.average_cost == pytest.approx(10.5)
        assert d.price == pytest.approx(11.0)

    def test_label_is_disclaimer(self):
        d = _holding(price=12.0)
        assert d.label == ees.EXIT_DISCLAIMER

    def test_to_dict_contains_contract_keys(self):
        d = _holding(price=11.0, plan=dict(BASE_PLAN), plan_id="p1", plan_source="trade_plan_block")
        data = d.to_dict()
        for key in [
            "symbol", "name", "action", "priority", "reasons", "triggers",
            "suggested_shares", "suggested_pct", "price", "previous_close",
            "pnl_pct", "position", "available_position", "average_cost",
            "constraints", "plan_id", "plan_source", "plan_available", "label",
        ]:
            assert key in data, key
        assert data["label"] == ees.EXIT_DISCLAIMER
        assert data["plan_id"] == "p1"
        assert data["plan_source"] == "trade_plan_block"

    def test_plan_id_and_source_from_plan_dict(self):
        d = _holding(price=12.0, plan=dict(BASE_PLAN))
        assert d.plan_id == "plan-1"
        assert d.plan_source == tps.SOURCE_BLOCK

    def test_triggers_are_documented_codes(self):
        cases = [
            _holding(price=11.0, plan=dict(BASE_PLAN)),
            _holding(price=13.6, plan=dict(BASE_PLAN)),
            _holding(price=12.0, plan=None),
            _holding(price=None, plan=dict(BASE_PLAN)),
            _holding(price=11.0, available_position=0, plan=dict(BASE_PLAN)),
        ]
        for d in cases:
            assert set(d.triggers) <= set(ees.ALL_TRIGGERS), d.triggers
            assert d.action in ees.ALL_ACTIONS
            assert d.priority in ees.ALL_PRIORITIES

    def test_symbol_and_name_normalized(self):
        d = _holding(symbol=" 002384.sz ", name="东山精密", price=12.0)
        assert d.symbol == "002384.SZ"
        assert d.name == "东山精密"


# --------------------------------------------------------------------------- #
# 交易日推算
# --------------------------------------------------------------------------- #


class TestTradingDays:
    def test_counts_weekdays_only(self):
        # 语义：统计 (信号日, 今日] 区间内的交易日——信号日当天不算"已持有"
        assert ees.count_trading_days_between("2026-06-01", "2026-06-05") == 4
        assert ees.count_trading_days_between("2026-06-05", "2026-06-08") == 1
        assert ees.count_trading_days_between("2026-06-06", "2026-06-07") == 0

    def test_same_day_is_zero(self):
        assert ees.count_trading_days_between("2026-06-01", "2026-06-01") == 0

    def test_invalid_dates_return_none(self):
        assert ees.count_trading_days_between(None, "2026-06-05") is None
        assert ees.count_trading_days_between("2026-06-01", "乱七八糟") is None

    def test_reverse_order_is_zero(self):
        assert ees.count_trading_days_between("2026-06-05", "2026-06-01") == 0

    def test_absurd_span_returns_none(self):
        assert ees.count_trading_days_between("1990-01-01", "2026-06-05") is None

    def test_days_held_from_plan_signal_date(self):
        plan = {"signal_trade_date": "2026-06-01"}
        assert ees.infer_trading_days_held(plan, "2026-06-05") == 4
        assert ees.infer_trading_days_held(plan, None) is None
        assert ees.infer_trading_days_held({}, "2026-06-05") is None
        assert ees.infer_trading_days_held(None, "2026-06-05") is None


# --------------------------------------------------------------------------- #
# 组合评估
# --------------------------------------------------------------------------- #


class TestEvaluatePortfolio:
    def test_filters_by_user_id(self, db):
        _add_position(db, user_id="u1", symbol="600000.SH", position=1000, available=1000, cost=10.0)
        _add_position(db, user_id="u2", symbol="000001.SZ", position=2000, available=2000, cost=12.0)

        quotes = {
            "600000.SH": {"price": 10.5, "previous_close": 10.4, "change_pct": 0.96},
            "000001.SZ": {"price": 11.0, "previous_close": 11.0, "change_pct": 0.0},
        }
        decisions = ees.evaluate_portfolio(db, user_id="u1", quotes=quotes, today="2026-06-05")
        assert [d.symbol for d in decisions] == ["600000.SH"]

    def test_unknown_user_returns_empty(self, db):
        _add_position(db, user_id="u1", symbol="600000.SH", position=1000, available=1000, cost=10.0)
        assert ees.evaluate_portfolio(db, user_id="nobody", quotes={}) == []
        assert ees.evaluate_portfolio(db, user_id="", quotes={}) == []

    def test_plan_availability_differs_per_symbol(self, db):
        _add_position(db, user_id="u1", symbol="600000.SH", position=1000, available=1000, cost=10.0)
        _add_position(db, user_id="u1", symbol="000001.SZ", position=2000, available=2000, cost=12.0,
                      source="import")
        _add_plan(db, user_id="u1", symbol="600000.SH", report_id="r1", plan=dict(STOP_ONLY_PLAN),
                  signal_trade_date="2026-06-01")

        quotes = {
            "600000.SH": {"price": 10.5, "previous_close": 10.4, "change_pct": 0.96},
            "000001.SZ": {"price": 11.0, "previous_close": 11.0, "change_pct": 0.0},
        }
        decisions = {d.symbol: d for d in ees.evaluate_portfolio(
            db, user_id="u1", quotes=quotes, today="2026-06-05"
        )}
        assert decisions["600000.SH"].plan_available is True
        assert decisions["600000.SH"].plan_id is not None
        assert decisions["000001.SZ"].plan_available is False
        assert "no_plan" in decisions["000001.SZ"].triggers

    def test_sorted_by_priority_then_pnl(self, db):
        # A：跌破止损（high，浮亏最大）
        _add_position(db, user_id="u1", symbol="600000.SH", position=1000, available=1000, cost=12.0)
        # B：触及止盈（medium，浮盈 5%）
        _add_position(db, user_id="u1", symbol="000001.SZ", position=1000, available=1000, cost=10.0,
                      source="import")
        # C：无计划（medium，无成本价 → pnl 未知，排最后）
        _add_position(db, user_id="u1", symbol="002415.SZ", position=500, available=500, cost=None,
                      source="manual2")

        _add_plan(db, user_id="u1", symbol="600000.SH", report_id="r1",
                  plan={"hard_stop_price": 11.0, "source": tps.SOURCE_BLOCK})
        _add_plan(db, user_id="u1", symbol="000001.SZ", report_id="r2",
                  plan={"hard_stop_price": 8.0, "take_profit_ladder": [[10.0, 50.0]],
                        "source": tps.SOURCE_BLOCK})

        quotes = {
            "600000.SH": {"price": 10.0, "change_pct": -1.0},
            "000001.SZ": {"price": 10.5, "change_pct": 1.0},
            "002415.SZ": {"price": 20.0, "change_pct": 0.5},
        }
        decisions = ees.evaluate_portfolio(db, user_id="u1", quotes=quotes, today="2026-06-05")
        assert [d.symbol for d in decisions] == ["600000.SH", "000001.SZ", "002415.SZ"]
        assert decisions[0].priority == "high"
        assert decisions[1].action == "减仓"
        assert decisions[2].action == "观察"

    def test_trading_days_held_derived_from_plan(self, db):
        _add_position(db, user_id="u1", symbol="600000.SH", position=1000, available=1000, cost=10.0)
        _add_plan(db, user_id="u1", symbol="600000.SH", report_id="r1",
                  plan={"hard_stop_price": 8.0, "time_stop_days": 3, "source": tps.SOURCE_BLOCK},
                  signal_trade_date="2026-06-01")

        quotes = {"600000.SH": {"price": 10.0, "change_pct": 0.0}}
        decisions = ees.evaluate_portfolio(db, user_id="u1", quotes=quotes, today="2026-06-15")
        assert len(decisions) == 1
        assert "time_stop" in decisions[0].triggers
        assert decisions[0].action == "观察"

    def test_time_stop_not_fired_before_deadline(self, db):
        _add_position(db, user_id="u1", symbol="600000.SH", position=1000, available=1000, cost=10.0)
        _add_plan(db, user_id="u1", symbol="600000.SH", report_id="r1",
                  plan={"hard_stop_price": 8.0, "time_stop_days": 30, "source": tps.SOURCE_BLOCK},
                  signal_trade_date="2026-06-01")

        decisions = ees.evaluate_portfolio(
            db, user_id="u1", quotes={"600000.SH": {"price": 10.0}}, today="2026-06-05"
        )
        assert decisions[0].action == "持有"

    def test_missing_quote_becomes_observed(self, db):
        _add_position(db, user_id="u1", symbol="600000.SH", position=1000, available=1000, cost=10.0)
        decisions = ees.evaluate_portfolio(db, user_id="u1", quotes={}, today="2026-06-05")
        assert len(decisions) == 1
        assert decisions[0].action == "观察"
        assert "suspended" in decisions[0].triggers

    def test_fetches_quotes_when_none(self, db, monkeypatch):
        _add_position(db, user_id="u1", symbol="600000.SH", position=1000, available=1000, cost=10.0)
        called = {}

        def fake_fetch(symbols):
            called["symbols"] = symbols
            return {"600000.SH": {"price": 11.0, "change_pct": 10.0}}

        monkeypatch.setattr(ees, "_fetch_live_quotes", fake_fetch)
        decisions = ees.evaluate_portfolio(db, user_id="u1", today="2026-06-05")
        assert called["symbols"] == ["600000.SH"]
        assert decisions[0].price == pytest.approx(11.0)

    def test_quote_fetch_failure_is_tolerated(self, db, monkeypatch):
        _add_position(db, user_id="u1", symbol="600000.SH", position=1000, available=1000, cost=10.0)
        monkeypatch.setattr(ees, "_fetch_live_quotes", lambda symbols: {})
        decisions = ees.evaluate_portfolio(db, user_id="u1", today="2026-06-05")
        assert len(decisions) == 1
        assert decisions[0].action == "观察"

    def test_broken_quote_fetch_does_not_raise(self, db, monkeypatch):
        _add_position(db, user_id="u1", symbol="600000.SH", position=1000, available=1000, cost=10.0)

        def boom(symbols):
            raise RuntimeError("行情源炸了")

        monkeypatch.setattr(ees, "_fetch_live_quotes", boom)
        decisions = ees.evaluate_portfolio(db, user_id="u1", today="2026-06-05")
        assert len(decisions) == 1

    def test_peak_price_provider_is_used(self, db):
        _add_position(db, user_id="u1", symbol="600000.SH", position=1000, available=1000, cost=10.0)
        _add_plan(db, user_id="u1", symbol="600000.SH", report_id="r1",
                  plan={"hard_stop_price": 5.0, "trailing_stop_pct": 10.0,
                        "source": tps.SOURCE_BLOCK})

        quotes = {"600000.SH": {"price": 18.0, "change_pct": -2.0}}
        decisions = ees.evaluate_portfolio(
            db, user_id="u1", quotes=quotes, today="2026-06-05",
            peak_price_provider=lambda symbol: 20.0,
        )
        assert "trailing_stop" in decisions[0].triggers

    def test_no_peak_provider_means_no_trailing_stop(self, db):
        _add_position(db, user_id="u1", symbol="600000.SH", position=1000, available=1000, cost=10.0)
        _add_plan(db, user_id="u1", symbol="600000.SH", report_id="r1",
                  plan={"hard_stop_price": 5.0, "trailing_stop_pct": 10.0,
                        "source": tps.SOURCE_BLOCK})

        quotes = {"600000.SH": {"price": 18.0, "change_pct": -2.0}}
        decisions = ees.evaluate_portfolio(db, user_id="u1", quotes=quotes, today="2026-06-05")
        assert "trailing_stop" not in decisions[0].triggers

    def test_broken_row_is_skipped_not_fatal(self, db, monkeypatch):
        _add_position(db, user_id="u1", symbol="600000.SH", position=1000,
                      available=1000, cost=10.0)
        _add_position(db, user_id="u1", symbol="000001.SZ", position=1000, available=1000,
                      cost=10.0, source="import")

        # 让第二只标的的计划查询炸掉，第一只仍应正常返回
        original = tps.get_monitor_plan

        def flaky(db_, *, user_id, symbol):
            if symbol == "000001.SZ":
                raise RuntimeError("计划查询失败")
            return original(db_, user_id=user_id, symbol=symbol)

        monkeypatch.setattr(tps, "get_monitor_plan", flaky)
        decisions = ees.evaluate_portfolio(
            db, user_id="u1",
            quotes={"600000.SH": {"price": 11.0}, "000001.SZ": {"price": 11.0}},
            today="2026-06-05",
        )
        assert [d.symbol for d in decisions] == ["600000.SH"]

    def test_every_decision_carries_the_label(self, db):
        _add_position(db, user_id="u1", symbol="600000.SH", position=1000, available=1000, cost=10.0)
        _add_position(db, user_id="u1", symbol="000001.SZ", position=1000, available=1000, cost=10.0,
                      source="import")
        _add_plan(db, user_id="u1", symbol="600000.SH", report_id="r1",
                  plan={"hard_stop_price": 11.0, "source": tps.SOURCE_BLOCK})

        quotes = {"600000.SH": {"price": 10.0, "change_pct": -1.0},
                  "000001.SZ": {"price": 10.0, "change_pct": 0.0}}
        for d in ees.evaluate_portfolio(db, user_id="u1", quotes=quotes, today="2026-06-05"):
            assert d.label == ees.EXIT_DISCLAIMER

    def test_empty_portfolio(self, db):
        assert ees.evaluate_portfolio(db, user_id="u1", quotes={}) == []


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #


class TestSummarizeDecisions:
    def test_counts(self, db):
        _add_position(db, user_id="u1", symbol="600000.SH", position=1000, available=1000, cost=12.0)
        _add_position(db, user_id="u1", symbol="000001.SZ", position=1000, available=1000, cost=10.0,
                      source="import")
        _add_position(db, user_id="u1", symbol="002415.SZ", position=1000, available=1000, cost=10.0,
                      source="manual2")
        _add_position(db, user_id="u1", symbol="300123.SZ", position=1000, available=1000, cost=10.0,
                      source="manual3")

        _add_plan(db, user_id="u1", symbol="600000.SH", report_id="r1",
                  plan={"hard_stop_price": 11.0, "source": tps.SOURCE_BLOCK})
        _add_plan(db, user_id="u1", symbol="000001.SZ", report_id="r2",
                  plan={"hard_stop_price": 8.0, "take_profit_ladder": [[10.0, 50.0]],
                        "source": tps.SOURCE_BLOCK})
        _add_plan(db, user_id="u1", symbol="300123.SZ", report_id="r3",
                  plan={"hard_stop_price": 8.0, "source": tps.SOURCE_BLOCK})

        quotes = {
            "600000.SH": {"price": 10.0, "change_pct": -1.0},   # 清仓 / high
            "000001.SZ": {"price": 10.5, "change_pct": 1.0},    # 减仓 / medium
            "002415.SZ": {"price": 20.0, "change_pct": 0.5},    # 观察 / medium（无计划）
            "300123.SZ": {"price": 10.0, "change_pct": 0.5},    # 持有 / low
        }
        decisions = ees.evaluate_portfolio(db, user_id="u1", quotes=quotes, today="2026-06-05")
        summary = ees.summarize_decisions(decisions)

        assert summary["total"] == 4
        assert summary["清仓"] == 1
        assert summary["减仓"] == 1
        assert summary["持有"] == 1
        assert summary["观察"] == 1
        assert summary["high_priority"] == 1
        assert summary["with_plan"] == 3
        assert summary["without_plan"] == 1
        assert summary["label"] == ees.EXIT_DISCLAIMER

    def test_empty_list(self):
        summary = ees.summarize_decisions([])
        assert summary["total"] == 0
        assert summary["清仓"] == 0
        assert summary["label"] == ees.EXIT_DISCLAIMER

    def test_none_is_tolerated(self):
        assert ees.summarize_decisions(None)["total"] == 0

    def test_plan_coverage_metric(self, db):
        """产品指标：有可监控计划的持仓占比。"""
        _add_position(db, user_id="u1", symbol="600000.SH", position=1000, available=1000, cost=10.0)
        _add_position(db, user_id="u1", symbol="000001.SZ", position=1000, available=1000, cost=10.0,
                      source="import")
        _add_plan(db, user_id="u1", symbol="600000.SH", report_id="r1",
                  plan={"hard_stop_price": 5.0, "source": tps.SOURCE_BLOCK})

        quotes = {"600000.SH": {"price": 10.0}, "000001.SZ": {"price": 10.0}}
        summary = ees.summarize_decisions(
            ees.evaluate_portfolio(db, user_id="u1", quotes=quotes, today="2026-06-05")
        )
        coverage = summary["with_plan"] / summary["total"]
        assert coverage == pytest.approx(0.5)


class TestPublicContract:
    def test_disclaimer_text(self):
        assert ees.EXIT_DISCLAIMER == "以上仅为纪律提示，不构成投资建议。"

    def test_trigger_code_set(self):
        assert set(ees.ALL_TRIGGERS) == {
            "hard_stop", "take_profit", "trailing_stop", "time_stop",
            "invalidation_review", "no_plan", "suspended", "t1_blocked",
            "limit_up_blocked", "limit_down_blocked", "plan_not_long",
        }

    def test_action_set(self):
        assert set(ees.ALL_ACTIONS) == {"持有", "减仓", "清仓", "观察"}

    def test_default_hold_reason(self):
        d = _holding(price=12.50, plan=dict(BASE_PLAN))
        assert d.reasons == ["现价未触及计划中的任何止损/止盈/时间条件，按计划持有"]
