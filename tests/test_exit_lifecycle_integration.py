"""端到端闭环测试：研报文本 → 交易计划 → 落库 → 退出引擎 → 卖出建议。

这是整个 P0-2 / P0-3 修复的**决定性测试**。产品的核心缺陷不是"分析能力不足"，
而是研报里已经写好的止损/止盈/仓位纪律从来没有变成可监控对象：
生产库实测 3922 份研报提到止盈，只有 532 份结构化出目标价（丢失 86%），
且唯一的卖出逻辑是虚拟盘里写死的 -6%/+10%。

本文件验证这条链路真的接通了：
  1. 模型输出的 TRADE_PLAN 文本块被解析成结构化计划；
  2. 计划落库（同一 report_id 幂等）；
  3. 退出引擎读取**该标的自己的**计划锚点，而不是全局硬编码；
  4. 价格越过锚点时给出清仓/减仓，并带上 A 股可执行性约束。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.database import (
    Base,
    ImportedPortfolioPositionDB,
    TradePlanDB,
    UserDB,
)
from api.services import exit_engine_service as exit_engine
from api.services import trade_plan_service as tps

USER_ID = "u-integration"
SYMBOL = "002384.SZ"

# 真实研报里出现过的形态：总仓上限、首仓、硬止损、分批止盈、移动止盈、时间止损、
# 以及可判定的逻辑失效条件。格式与提示词契约一致（`<!-- TRADE_PLAN ... -->`，
# 阶梯为「价格:减仓百分比」）。
REPORT_TEXT = """最终交易建议：买入

<!-- TRADE_PLAN
direction: BUY
horizon_days: 20
entry_low: 188.00
entry_high: 195.50
position_cap_pct: 9
first_tranche_pct: 2
hard_stop_price: 182.13
take_profit_ladder: 214.9:30, 231.0:40
trailing_stop_pct: 8
time_stop_days: 45
invalidation_conditions: 商誉减值超过5亿元 | 毛利率低于15%
-->
"""

# 模型不总是守规矩：全角冒号、人民币符号、千分位、括号写百分比、缺分隔符。
# 解析器必须容忍，否则"格式一乱就整份计划丢失"，等于回到修复前的状态。
MESSY_REPORT_TEXT = """最终交易建议：买入

<!-- TRADE_PLAN
direction：买入
hard_stop_price: ￥182.13元
take_profit_ladder: 214.9(30%), 231.0 40%
trailing_stop_pct: 8%
-->
"""

# 只有价格锚点、不含基本面失效条件的计划——未触发时应明确"持有"。
PRICE_ONLY_REPORT_TEXT = """最终交易建议：买入

<!-- TRADE_PLAN
direction: BUY
horizon_days: 20
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
    try:
        yield session
    finally:
        session.close()


def _seed_user_and_holding(db, *, position=1000.0, available=1000.0, cost=190.0):
    db.add(UserDB(id=USER_ID, email="integration@test.local"))
    db.add(
        ImportedPortfolioPositionDB(
            id="pos-1",
            user_id=USER_ID,
            source="manual",
            symbol=SYMBOL,
            security_name="东山精密",
            current_position=position,
            available_position=available,
            average_cost=cost,
        )
    )
    db.commit()


def _persist_plan(db, text=REPORT_TEXT):
    """走真实落库入口；返回落库行（该函数返回 4 元组：行/计划/来源/警告）。"""
    row, plan, source, warnings = tps.persist_plan_from_analysis(
        db,
        user_id=USER_ID,
        report_id="report-1",
        symbol=SYMBOL,
        name="东山精密",
        signal_trade_date="2026-04-10",
        decision_text=text,
        trader_plan_text=text,
    )
    return row, plan, source, warnings


# --------------------------------------------------------------------------- #
# 1. 文本 → 结构化计划
# --------------------------------------------------------------------------- #

class TestReportTextBecomesStructuredPlan:
    def test_report_text_parses_into_clean_anchors(self):
        plan, warnings = tps.parse_trade_plan_block(REPORT_TEXT)
        assert warnings == []
        assert plan["hard_stop_price"] == pytest.approx(182.13)
        assert plan["trailing_stop_pct"] == pytest.approx(8.0)
        assert plan["time_stop_days"] == 45
        # 分批止盈：价格 + 减仓比例
        assert [t[0] for t in plan["take_profit_ladder"]] == pytest.approx([214.9, 231.0])
        assert [t[1] for t in plan["take_profit_ladder"]] == pytest.approx([30.0, 40.0])

    def test_messy_model_output_still_yields_a_usable_plan(self):
        """格式被写坏也必须能救回来——否则等于"格式一乱计划就丢"。"""
        plan, warnings = tps.parse_trade_plan_block(MESSY_REPORT_TEXT)
        assert warnings == []
        assert plan["hard_stop_price"] == pytest.approx(182.13)
        assert plan["trailing_stop_pct"] == pytest.approx(8.0)
        assert [t[0] for t in plan["take_profit_ladder"]] == pytest.approx([214.9, 231.0])
        assert tps.is_monitorable(plan) is True

    def test_text_without_the_block_yields_nothing_and_no_crash(self):
        """没有机读块时安静降级，绝不能因为格式问题丢掉整份报告。"""
        plan, warnings = tps.parse_trade_plan_block("最终交易建议：买入。正文里提到 182 元止损。")
        assert plan == {}
        assert warnings == []

    def test_discipline_text_survives_as_conditions(self):
        plan, _ = tps.parse_trade_plan_block(REPORT_TEXT)
        assert plan["position_cap_pct"] == pytest.approx(9.0)
        assert plan["first_tranche_pct"] == pytest.approx(2.0)
        assert any("商誉" in c for c in plan["invalidation_conditions"])

    def test_persisted_plan_is_monitorable(self, db):
        row, plan, source, warnings = _persist_plan(db)
        assert row is not None
        assert warnings == []
        assert source == tps.SOURCE_BLOCK
        stored = tps.plan_dict_from_row(row)
        assert stored["hard_stop_price"] == pytest.approx(182.13)
        assert tps.is_monitorable(stored) is True

    def test_repeated_persist_is_idempotent(self, db):
        """同一 report_id 重复保存不得产生第二行计划。"""
        _persist_plan(db)
        _persist_plan(db)
        assert db.query(TradePlanDB).count() == 1


# --------------------------------------------------------------------------- #
# 2. 计划 → 退出决策（链路的核心）
# --------------------------------------------------------------------------- #

class TestPlanDrivesExitDecision:
    """关键：用的是**这只股票自己那份计划**的锚点，不是全局 -6%/+10%。"""

    def _decide(self, db, price, **kwargs):
        decisions = exit_engine.evaluate_portfolio(
            db,
            user_id=USER_ID,
            quotes={SYMBOL: {"price": price, "previous_close": price, "change_pct": 0.0}},
            today="2026-04-14",
            **kwargs,
        )
        assert len(decisions) == 1
        return decisions[0]

    def test_above_stop_surfaces_review_when_plan_has_uncheckable_conditions(self, db):
        """计划里写了"商誉减值/毛利率"这类系统无法自行验证的失效条件时，
        结论是**观察 + 需人工复核**，而不是假装"一切正常、继续持有"。
        系统的诚实边界：它不能评估基本面，就不该给出"全清"的假安心。
        """
        _seed_user_and_holding(db)
        _persist_plan(db)
        decision = self._decide(db, 195.0)
        assert decision.action == "观察"
        assert "invalidation_review" in decision.triggers
        assert any("复核" in r for r in decision.reasons)
        assert decision.plan_available is True

    def test_price_only_plan_holds_when_nothing_is_triggered(self, db):
        """只有价格锚点（无基本面失效条件）时，未触发就应明确说"持有"。"""
        _seed_user_and_holding(db)
        _persist_plan(db, text=PRICE_ONLY_REPORT_TEXT)
        decision = self._decide(db, 195.0)
        assert decision.action == "持有"
        assert decision.triggers == []

    def test_below_plan_stop_triggers_liquidation(self, db):
        """182.13 是这只股票**自己**的硬止损；成本 190 时亏损仅 4.3%，
        远未触及全局 -6% 阈值——旧逻辑在这里会继续持有。"""
        _seed_user_and_holding(db)
        _persist_plan(db)
        decision = self._decide(db, 181.0)
        assert decision.action == "清仓"
        assert "hard_stop" in decision.triggers

    def test_global_threshold_would_have_missed_this(self, db):
        """反证：-4.7% 的浮亏不触发任何全局阈值，却已跌破该标的的计划止损。"""
        _seed_user_and_holding(db)
        _persist_plan(db)
        decision = self._decide(db, 181.0)
        assert decision.pnl_pct is not None and decision.pnl_pct > -6.0
        assert decision.action == "清仓"

    def test_ladder_target_triggers_partial_reduction(self, db):
        _seed_user_and_holding(db)
        _persist_plan(db)
        decision = self._decide(db, 215.0)
        assert decision.action == "减仓"
        assert "take_profit" in decision.triggers

    def test_no_plan_degrades_to_watch_not_silence(self, db):
        """没有计划时不得静默——明确告知"无可用计划"，而不是假装给了建议。"""
        _seed_user_and_holding(db)
        decision = self._decide(db, 195.0)
        assert decision.action == "观察"
        assert decision.plan_available is False

    def test_every_decision_carries_the_disclaimer(self, db):
        _seed_user_and_holding(db)
        _persist_plan(db)
        assert self._decide(db, 195.0).label == exit_engine.EXIT_DISCLAIMER


# --------------------------------------------------------------------------- #
# 3. A 股可执行性：信号对了不代表今天卖得掉
# --------------------------------------------------------------------------- #

class TestAShareExecutability:
    def test_t1_blocks_selling_shares_bought_today(self, db):
        """当日买入的股份不可卖出：动作与理由保留，但股数归零。"""
        _seed_user_and_holding(db, position=1000.0, available=0.0)
        _persist_plan(db)
        decisions = exit_engine.evaluate_portfolio(
            db,
            user_id=USER_ID,
            quotes={SYMBOL: {"price": 181.0, "previous_close": 181.0, "change_pct": 0.0}},
            today="2026-04-14",
        )
        decision = decisions[0]
        assert decision.action == "清仓"          # 判断不丢
        assert decision.suggested_shares == 0      # 但今天执行不了
        assert "t1_blocked" in decision.triggers

    def test_limit_down_warns_but_keeps_the_signal(self, db):
        """跌停时卖单可能无法成交——警告，但不抹掉判断。"""
        _seed_user_and_holding(db)
        _persist_plan(db)
        decisions = exit_engine.evaluate_portfolio(
            db,
            user_id=USER_ID,
            quotes={SYMBOL: {"price": 181.0, "previous_close": 201.0, "change_pct": -9.95}},
            today="2026-04-14",
        )
        decision = decisions[0]
        assert decision.action == "清仓"
        assert any("跌停" in c for c in decision.constraints)

    def test_suggested_shares_are_whole_lots(self, db):
        _seed_user_and_holding(db, position=1234.0, available=1234.0)
        _persist_plan(db)
        decisions = exit_engine.evaluate_portfolio(
            db,
            user_id=USER_ID,
            quotes={SYMBOL: {"price": 215.0, "previous_close": 215.0, "change_pct": 0.0}},
            today="2026-04-14",
        )
        shares = decisions[0].suggested_shares
        assert shares == 0 or shares % 100 == 0


# --------------------------------------------------------------------------- #
# 4. 用户隔离与"缺失不伪造"
# --------------------------------------------------------------------------- #

class TestIsolationAndHonesty:
    def test_other_users_holdings_are_not_evaluated(self, db):
        _seed_user_and_holding(db)
        _persist_plan(db)
        assert exit_engine.evaluate_portfolio(db, user_id="someone-else") == []

    def test_missing_quote_does_not_invent_a_price(self, db):
        _seed_user_and_holding(db)
        _persist_plan(db)
        decisions = exit_engine.evaluate_portfolio(db, user_id=USER_ID, quotes={}, today="2026-04-14")
        # 取不到行情时必须降级为观察，绝不能凭空判定已止损
        assert decisions[0].action in ("观察", "持有")
        assert decisions[0].action != "清仓"
