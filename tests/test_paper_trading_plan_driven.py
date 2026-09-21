"""虚拟盘的两处核心修正：计划驱动的卖出判断，以及 A 股真实交易成本。

修复前的两个问题（都在生产代码里，且此前**没有任何测试覆盖**）：

1. `generate_daily_operation_plan` 用全局写死的 `-6%` / `+10%` 判断卖出，
   与研报结论无关——一只日波动 3% 的银行股和一只日波动 9% 的题材股被套用
   同一条止损线。现在优先使用该标的自己那份交易计划。
2. `execute_trade` 只算 `gross * fee_rate` 一次佣金，**完全漏掉卖出印花税
   0.05%** 与佣金最低 5 元，使虚拟盘收益系统性偏乐观。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.database import Base, UserDB
from api.services import paper_trading_service as pts
from api.services import trade_plan_service as tps

USER_ID = "u-paper"
SYMBOL = "002384.SZ"

PLAN_TEXT = """最终交易建议：买入

<!-- TRADE_PLAN
direction: BUY
entry_low: 188.00
entry_high: 195.50
position_cap_pct: 9
first_tranche_pct: 2
hard_stop_price: 182.13
take_profit_ladder: 214.9:30, 231.0:40
trailing_stop_pct: 8
time_stop_days: 45
invalidation_conditions: []
-->
"""


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(UserDB(id=USER_ID, email="paper@test.local"))
    session.commit()
    try:
        yield session
    finally:
        session.close()


def _persist_plan(db):
    tps.persist_plan_from_analysis(
        db,
        user_id=USER_ID,
        report_id="r-paper",
        symbol=SYMBOL,
        name="东山精密",
        signal_trade_date="2026-04-10",
        decision_text=PLAN_TEXT,
        trader_plan_text=PLAN_TEXT,
    )
    db.commit()


def _hint(db, price, *, cost=190.0, quantity=1000.0):
    return pts._plan_driven_action(
        db,
        user_id=USER_ID,
        symbol=SYMBOL,
        name="东山精密",
        quantity=quantity,
        avg_cost=cost,
        live_price=price,
        pnl_pct=(price - cost) / cost * 100.0,
        quote={"price": price, "previous_close": price, "change_pct": 0.0},
        trade_date="2026-04-14",
    )


# --------------------------------------------------------------------------- #
# 计划驱动的卖出判断
# --------------------------------------------------------------------------- #

class TestPlanDrivenSellDecision:
    def test_no_plan_returns_none_so_caller_can_fall_back(self, db):
        """没有可用计划时必须返回 None，由调用方兜底，而不是编一个建议。"""
        assert _hint(db, 195.0) is None

    def test_below_own_plan_stop_sells_before_global_threshold(self, db):
        """181 跌破该标的自己的止损 182.13；此时浮亏仅 -4.7%，
        旧的 -6% 全局阈值会继续持有。"""
        _persist_plan(db)
        hint = _hint(db, 181.0)
        assert hint is not None
        assert hint["action"] == "SELL"
        assert "hard_stop" in hint["plan_triggered"]
        assert hint["plan_source"] == tps.SOURCE_BLOCK

    def test_ladder_target_triggers_partial_sell(self, db):
        _persist_plan(db)
        hint = _hint(db, 215.0)
        assert hint["action"] == "SELL"
        assert "take_profit" in hint["plan_triggered"]
        assert hint["quantity"] < 1000.0  # 分批而非清仓

    def test_untriggered_plan_holds_with_the_plan_reason(self, db):
        _persist_plan(db)
        hint = _hint(db, 195.0)
        assert hint["action"] == "HOLD"
        assert hint["quantity"] == 0.0

    def test_reason_cites_the_plan_not_a_global_threshold(self, db):
        """理由必须来自这只股票的计划，旧实现只会写"触发止损阈值(-6.00%)"。"""
        _persist_plan(db)
        hint = _hint(db, 181.0)
        assert "182.13" in hint["reason"]
        assert "-6.0" not in hint["reason"]


# --------------------------------------------------------------------------- #
# A 股真实交易成本
# --------------------------------------------------------------------------- #

class TestAShareTradingCost:
    def test_buy_pays_commission_and_transfer_fee_only(self, db):
        """买入不缴印花税。10000 元成交：佣金 max(3, 5)=5，过户费 0.1。"""
        pts.execute_trade(
            db, user_id=USER_ID, symbol=SYMBOL, side="BUY", quantity=100, price=100.0
        )
        portfolio = pts._ensure_portfolio(db, USER_ID)
        assert float(portfolio.cash_balance) == pytest.approx(1_000_000.0 - 10_000.0 - 5.1)

    def test_sell_pays_stamp_tax(self, db):
        """卖出缴印花税 0.05%：11000 元成交 → 印花税 5.5，佣金 max(3.3,5)=5，过户费 0.11。"""
        pts.execute_trade(
            db, user_id=USER_ID, symbol=SYMBOL, side="BUY", quantity=100, price=100.0
        )
        before = float(pts._ensure_portfolio(db, USER_ID).cash_balance)
        pts.execute_trade(
            db, user_id=USER_ID, symbol=SYMBOL, side="SELL", quantity=100, price=110.0
        )
        after = float(pts._ensure_portfolio(db, USER_ID).cash_balance)
        proceeds = 11_000.0 - (5.0 + 5.5 + 0.11)
        assert after - before == pytest.approx(proceeds)

    def test_sell_costs_more_than_buy_on_the_same_notional(self, db):
        """同样金额、同样价格下卖出成本更高——这是印花税的直接体现。"""
        buy = pts.MIN_COMMISSION + 10_000.0 * pts.TRANSFER_FEE_RATE
        sell = pts.MIN_COMMISSION + 10_000.0 * pts.STAMP_TAX_RATE + 10_000.0 * pts.TRANSFER_FEE_RATE
        assert sell > buy


# --------------------------------------------------------------------------- #
# C3：方向校验未通过时不得生成模拟卖单
# --------------------------------------------------------------------------- #

# 看空计划：失效价 200 高于现价，旧实现据此生成"清仓"模拟卖单，污染模拟业绩。
SELL_PLAN_TEXT = """最终交易建议：卖出

<!-- TRADE_PLAN
direction: SELL
entry_low: 188.00
entry_high: 195.50
hard_stop_price: 200.00
take_profit_ladder: 150.0:50
time_stop_days: 45
-->
"""

# 无方向字段（历史计划）：方向未经确认，判定照常但不得生成卖单。
NO_DIRECTION_PLAN_TEXT = """最终交易建议：买入

<!-- TRADE_PLAN
entry_low: 188.00
entry_high: 195.50
hard_stop_price: 182.13
take_profit_ladder: 214.9:30
time_stop_days: 45
-->
"""


class TestDirectionGateStopsPaperSell:
    def _persist(self, db, text):
        tps.persist_plan_from_analysis(
            db,
            user_id=USER_ID,
            report_id="r-gate-paper",
            symbol=SYMBOL,
            name="东山精密",
            signal_trade_date="2026-04-10",
            decision_text=text,
            trader_plan_text=text,
        )
        db.commit()

    def test_sell_plan_does_not_generate_a_sell_order(self, db):
        """核心回归：看空计划的失效价曾被当成止损位，生成模拟清仓卖单。"""
        self._persist(db, SELL_PLAN_TEXT)
        hint = _hint(db, 190.0)
        assert hint is not None
        assert hint["action"] == "HOLD"
        assert hint["quantity"] == 0.0
        assert hint["anchor_gate"] == "incoherent"

    def test_unknown_direction_does_not_generate_a_sell_order(self, db):
        """方向缺失 → 判定照常产出清仓，但闸门不允许它变成卖单（只记录）。"""
        self._persist(db, NO_DIRECTION_PLAN_TEXT)
        hint = _hint(db, 181.0)
        assert hint is not None
        assert hint["action"] == "HOLD"
        assert hint["quantity"] == 0.0
        assert hint["anchor_gate"] == "unknown"
        # 判定本身仍然可见：信号没有被悄悄丢掉，只是不能对外动作
        assert "hard_stop" in hint["plan_triggered"]

    def test_gate_fields_are_exposed_for_audit(self, db):
        _persist_plan(db)
        hint = _hint(db, 181.0)
        assert hint["anchor_gate"] == "ok"
        assert hint["plan_direction"] == "BUY"

    def test_coherent_buy_plan_still_generates_the_sell_order(self, db):
        """闸门不能误伤正常计划。"""
        _persist_plan(db)
        hint = _hint(db, 181.0)
        assert hint["action"] == "SELL"
        assert hint["quantity"] == 1000.0
        assert pts.STAMP_TAX_RATE == pytest.approx(0.0005)
