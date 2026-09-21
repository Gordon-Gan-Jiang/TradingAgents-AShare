"""成交流水（交易台账）服务单元测试。

覆盖：A 股费用拆解（含此前完全缺失的印花税）、摊薄成本、卖出已实现盈亏、
T+1 可卖重放、越卖/越买兜底、幂等去重、参数校验、卖出原因分类学与回退、
按原因/按标的的盈亏归因、历史成交点宽容解析、与导入快照的对账。

约定：测试默认把"今天"冻结为 2024-05-10，并把交易日历判定替换为本地函数，
因此**不触网、不依赖真实日历**；另有一条用例使用真实 `cn_today_str()`。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.database import Base, ImportedPortfolioPositionDB, TradeLedgerDB
from api.services import trade_ledger_service as tls
from api.services.trade_plan_service import (
    SELL_REASON_INVALIDATION,
    SELL_REASON_LABELS,
    SELL_REASON_MANUAL,
    SELL_REASON_STOP_LOSS,
    SELL_REASON_TAKE_PROFIT,
    SELL_REASON_TRAILING_STOP,
    SELL_REASON_UNKNOWN,
)
from tradingagents.dataflows.trade_calendar import cn_today_str as real_cn_today_str

TODAY = "2024-05-10"
YESTERDAY = "2024-05-09"
USER = "u-ledger"


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def _frozen_today(monkeypatch):
    """冻结"今天"并屏蔽交易日历（akshare/网络），保证测试可复现且离线可跑。"""

    monkeypatch.setattr(tls, "cn_today_str", lambda: TODAY)
    monkeypatch.setattr(tls, "is_cn_trading_day", lambda date_str: True)


def _record(db, **overrides):
    """默认记一笔 2024-05-06 的买入，按需覆盖字段。"""

    kwargs = dict(
        user_id=USER,
        symbol="600519",
        action="BUY",
        price=10.0,
        shares=1000.0,
        trade_date="2024-05-06",
    )
    kwargs.update(overrides)
    return tls.record_trade(db, **kwargs)


def _count(db) -> int:
    return int(db.query(TradeLedgerDB).count())


def _prev_weekday(date_str: str) -> str:
    """上一个工作日（仅跳过周末，避免测试触发真实交易日历/网络）。"""

    cursor = datetime.strptime(date_str, "%Y-%m-%d").date() - timedelta(days=1)
    while cursor.weekday() >= 5:
        cursor -= timedelta(days=1)
    return cursor.strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- #
# 费用拆解
# --------------------------------------------------------------------------- #

class TestComputeTradeCost:
    def test_buy_has_no_stamp_tax(self):
        cost = tls.compute_trade_cost("BUY", 10.0, 1000.0)
        assert cost.amount == 10000.0
        assert cost.stamp_tax == 0.0
        assert cost.commission == 5.0          # 30000 元以下按最低 5 元
        assert cost.transfer_fee == 0.1
        assert cost.total_fee == 5.1

    def test_sell_pays_stamp_tax_five_per_ten_thousand(self):
        cost = tls.compute_trade_cost("SELL", 10.0, 1000.0)
        assert cost.amount == 10000.0
        assert cost.stamp_tax == pytest.approx(5.0)     # 0.05% × 10000
        assert cost.commission == 5.0
        assert cost.transfer_fee == 0.1
        assert cost.total_fee == pytest.approx(10.1)

    def test_min_commission_kicks_in_on_tiny_trade(self):
        cost = tls.compute_trade_cost("BUY", 3.0, 100.0)   # 成交额 300 元
        assert cost.amount == 300.0
        assert cost.commission == 5.0                      # 0.09 元 → 最低 5 元
        assert cost.transfer_fee == 0.0
        assert cost.total_fee == 5.0

    def test_commission_rate_applies_above_minimum(self):
        cost = tls.compute_trade_cost("BUY", 20.0, 10000.0)  # 成交额 200000 元
        assert cost.commission == 60.0                       # 万三 × 200000
        assert cost.transfer_fee == 2.0
        assert cost.total_fee == 62.0

    def test_net_amount_direction_differs_for_buy_and_sell(self):
        buy = tls.compute_trade_cost("BUY", 10.0, 1000.0)
        sell = tls.compute_trade_cost("SELL", 10.0, 1000.0)
        assert buy.net_amount == pytest.approx(10005.1)     # 买入：现金流出更多
        assert buy.net_amount > buy.amount
        assert sell.net_amount == pytest.approx(9989.9)     # 卖出：到手更少
        assert sell.net_amount < sell.amount

    def test_action_is_case_insensitive_and_overridable(self):
        assert tls.compute_trade_cost("sell", 10.0, 1000.0).stamp_tax == pytest.approx(5.0)
        custom = tls.compute_trade_cost(
            "SELL", 10.0, 1000.0, commission_rate=0.0001, min_commission=0.0,
            stamp_tax_rate=0.001, transfer_fee_rate=0.0,
        )
        assert custom.commission == 1.0
        assert custom.stamp_tax == 10.0

    def test_invalid_action_raises(self):
        with pytest.raises(ValueError):
            tls.compute_trade_cost("HOLD", 10.0, 1000.0)
        with pytest.raises(ValueError):
            tls.compute_trade_cost("BUY", 0.0, 1000.0)


# --------------------------------------------------------------------------- #
# 摊薄成本与已实现盈亏
# --------------------------------------------------------------------------- #

class TestPositionState:
    def test_two_buys_dilute_average_cost(self, db):
        """两次买入的含费摊薄成本 = (第一次成本额 + 第二次成本额) / 总股数。"""

        first, payload = _record(db, price=10.0, shares=1000.0)
        assert payload["status"] == "ok"
        # (10000 + 5 佣金 + 0.1 过户费) / 1000
        assert first.average_cost_after == pytest.approx(10.0051)
        assert first.position_after == 1000.0
        assert first.fee == pytest.approx(5.1)
        assert first.stamp_tax == 0.0

        second, payload = _record(db, price=12.0, shares=1000.0, trade_date="2024-05-07")
        # (10005.1 + 12000 + 5 + 0.12) / 2000 = 11.00511
        assert second.average_cost_after == pytest.approx(11.0051, abs=1e-4)
        assert payload["state"]["position"] == 2000.0
        assert payload["state"]["average_cost"] == pytest.approx(11.0051, abs=1e-4)
        assert payload["state"]["realized_pnl"] == 0.0
        assert payload["state"]["total_bought"] == 2000.0
        assert payload["state"]["total_bought_amount"] == 22000.0
        assert payload["state"]["total_buy_fee"] == pytest.approx(10.22)

    def test_sell_keeps_average_cost_and_books_realized_pnl(self, db):
        """卖出不摊薄成本；已实现盈亏要扣掉卖出端的佣金/印花税/过户费。"""

        buy, _ = _record(db, price=10.0, shares=1000.0)          # 摊薄成本 10.0051
        assert buy.average_cost_after == pytest.approx(10.0051)

        sell, payload = _record(
            db, action="SELL", price=11.0, shares=500.0, trade_date=YESTERDAY
        )
        assert payload["status"] == "ok"
        # 卖出费用 = 佣金 5 + 印花税 2.75 + 过户费 0.06 = 7.81
        assert sell.fee == pytest.approx(5.06)
        assert sell.stamp_tax == pytest.approx(2.75)
        assert sell.net_amount == pytest.approx(5492.19)
        # (11 - 10.0051) × 500 - 7.81 = 489.64
        assert sell.realized_pnl == pytest.approx(489.64, abs=1e-9)

        state = payload["state"]
        assert state["average_cost"] == pytest.approx(10.0051)   # 卖出后成本不变
        assert state["position"] == 500.0
        assert state["available"] == 500.0                      # 买入已结算，卖出后剩 500
        assert state["realized_pnl"] == pytest.approx(489.64)
        assert state["total_sold"] == 500.0
        assert state["total_sell_fee"] == pytest.approx(7.81)
        assert state["trade_count"] == 2

    def test_rebuild_matches_record_state_and_is_read_only(self, db):
        _record(db, price=10.0, shares=1000.0)
        _record(db, action="SELL", price=11.0, shares=400.0, trade_date="2024-05-07")

        before = _count(db)
        state = tls.rebuild_position_state(db, user_id=USER, symbol="600519")
        assert _count(db) == before                     # 只读，不产生新行
        assert state["position"] == 600.0
        # 卖出 400@11：佣金 5 + 印花税 2.2 + 过户费 0.04 = 7.24
        assert state["realized_pnl"] == pytest.approx(round((11.0 - 10.0051) * 400 - 7.24, 2), abs=1e-9)
        assert state["realized_pnl"] == pytest.approx(390.72, abs=1e-9)

    def test_empty_ledger_returns_zeros_with_warning(self, db):
        state = tls.rebuild_position_state(db, user_id=USER, symbol="000001")
        assert state["position"] == 0.0
        assert state["available"] == 0.0
        assert state["average_cost"] == 0.0
        assert state["realized_pnl"] == 0.0
        assert state["trade_count"] == 0
        assert state["warnings"]

    def test_symbol_is_normalized_to_uppercase(self, db):
        row, payload = _record(db, symbol="  sh600519 ")
        assert row.symbol == "SH600519"
        assert payload["state"]["symbol"] == "SH600519"


# --------------------------------------------------------------------------- #
# T+1 可卖
# --------------------------------------------------------------------------- #

class TestAvailableT1:
    def test_buy_today_is_not_sellable(self, db):
        row, payload = _record(db, trade_date=TODAY)
        assert row.position_after == 1000.0
        assert row.available_after == 0.0
        assert payload["state"]["available"] == 0.0

    def test_buy_yesterday_is_sellable(self, db):
        row, payload = _record(db, trade_date=YESTERDAY)
        assert row.available_after == 1000.0
        assert payload["state"]["available"] == 1000.0

    def test_only_settled_part_is_available(self, db):
        _record(db, trade_date=YESTERDAY, shares=1000.0)
        _, payload = _record(db, trade_date=TODAY, shares=800.0)
        assert payload["state"]["position"] == 1800.0
        assert payload["state"]["available"] == 1000.0    # 今日买入的 800 股不可卖

    def test_selling_today_buy_is_flagged(self, db):
        _, payload = _record(db, trade_date=TODAY, shares=1000.0)
        _, sell_payload = _record(
            db, action="SELL", price=11.0, shares=500.0, trade_date=TODAY
        )
        state = sell_payload["state"]
        assert state["position"] == 500.0
        assert state["available"] == 0.0                  # 当日买入不可卖
        assert any("T+1" in warning for warning in state["warnings"])

    def test_available_never_exceeds_position(self, db):
        """买入当日卖出（T+0）不应让可卖数量凭空出现。"""

        _record(db, trade_date=YESTERDAY, shares=1000.0)
        _, payload = _record(
            db, action="SELL", price=11.0, shares=1000.0, trade_date=YESTERDAY
        )
        state = payload["state"]
        assert state["position"] == 0.0
        assert state["available"] == 0.0
        assert state["available"] <= state["position"]

    def test_oversold_history_clamps_at_zero_with_warning(self, db):
        """历史导入不完整（卖出多于买入）时不得出现负数持仓，且必须给出提示。"""

        _record(db, price=10.0, shares=100.0, trade_date=YESTERDAY)
        row, payload = _record(
            db, action="SELL", price=11.0, shares=300.0, trade_date=TODAY
        )
        state = payload["state"]
        assert state["position"] == 0.0
        assert state["available"] == 0.0
        assert state["warnings"]
        assert any("超过当时持仓" in warning for warning in state["warnings"])
        # 只按实际持有的 100 股计算盈亏：(11 - 10.0501) × 100 - 6.68
        assert row.realized_pnl == pytest.approx(88.31, abs=0.01)

    def test_non_trading_day_buy_is_flagged(self, db, monkeypatch):
        monkeypatch.setattr(tls, "is_cn_trading_day", lambda date_str: False)
        _, payload = _record(db, trade_date=TODAY)
        assert payload["state"]["available"] == 0.0
        assert any("非交易日" in warning for warning in payload["state"]["warnings"])

    def test_uses_real_cn_today(self, db, monkeypatch):
        """不冻结日期：相对真实 cn_today_str() 校验 T+1 规则。"""

        real_today = real_cn_today_str()
        monkeypatch.setattr(tls, "cn_today_str", real_cn_today_str)
        previous = _prev_weekday(real_today)

        _record(db, trade_date=previous, shares=1000.0)
        _, payload = _record(db, trade_date=real_today, shares=1000.0)
        state = payload["state"]
        assert state["today"] == real_today
        assert state["position"] == 2000.0
        assert state["available"] == 1000.0      # 只有更早日期的买入已结算


# --------------------------------------------------------------------------- #
# 记账：幂等、校验、卖出原因
# --------------------------------------------------------------------------- #

class TestRecordTrade:
    def test_duplicate_is_skipped_not_duplicated(self, db):
        first, payload = _record(db)
        assert payload["status"] == "ok"
        assert first is not None

        again, dup_payload = _record(db, name="贵州茅台", note="重复导入")
        assert again is None
        assert dup_payload["status"] == "duplicate"
        assert _count(db) == 1
        assert dup_payload["state"]["position"] == 1000.0     # 状态仍可返回

    def test_different_price_creates_new_row(self, db):
        _record(db, price=10.0)
        row, payload = _record(db, price=10.5)
        assert payload["status"] == "ok"
        assert row is not None
        assert _count(db) == 2

    @pytest.mark.parametrize(
        "overrides, keyword",
        [
            ({"action": "HOLD"}, "action"),
            ({"action": "观望"}, "action"),
            ({"price": 0}, "price"),
            ({"price": -3.5}, "price"),
            ({"shares": 0}, "shares"),
            ({"shares": -100}, "shares"),
            ({"trade_date": "2024/05/06"}, "trade_date"),
            ({"trade_date": "20240506"}, "trade_date"),
            ({"trade_date": "2024-13-01"}, "trade_date"),
            ({"trade_date": "not-a-date"}, "trade_date"),
            ({"symbol": "   "}, "symbol"),
            ({"user_id": ""}, "user_id"),
        ],
    )
    def test_invalid_inputs_are_rejected(self, db, overrides, keyword):
        row, payload = _record(db, **overrides)
        assert row is None
        assert payload["status"] == "invalid"
        assert keyword in payload["reason"]
        assert payload["state"] is None

    def test_invalid_input_leaves_no_rows(self, db):
        _record(db, action="HOLD")
        _record(db, price=0)
        _record(db, trade_date="bad")
        assert _count(db) == 0

    def test_sell_reason_defaults_and_fallback(self, db):
        sell, payload = _record(
            db, action="SELL", price=11.0, shares=100.0, trade_date=YESTERDAY
        )
        assert sell.sell_reason == SELL_REASON_MANUAL      # 卖出缺省为人工决定

        unknown, unknown_payload = _record(
            db, action="SELL", price=11.5, shares=100.0, trade_date=YESTERDAY,
            sell_reason="moon_phase",
        )
        assert unknown.sell_reason == SELL_REASON_UNKNOWN
        assert any("不在标准分类中" in warning for warning in unknown_payload["warnings"])

        labelled, _ = _record(
            db, action="SELL", price=12.0, shares=100.0, trade_date=YESTERDAY,
            sell_reason="止损",
        )
        assert labelled.sell_reason == SELL_REASON_STOP_LOSS   # 中文标签同样接受

        buy, buy_payload = _record(
            db, price=13.0, trade_date=YESTERDAY, sell_reason=SELL_REASON_STOP_LOSS
        )
        assert buy.sell_reason is None                     # 买入不携带卖出原因
        assert any("买入不接受" in warning for warning in buy_payload["warnings"])

    def test_sell_reason_note_and_links_are_persisted(self, db):
        row, _ = _record(
            db, action="SELL", price=11.0, shares=100.0, trade_date=YESTERDAY,
            sell_reason=SELL_REASON_TRAILING_STOP,
            sell_reason_note="回撤 8% 触发移动止盈",
            report_id="rpt-1", plan_id="plan-1", source="suggested", note="按计划执行",
        )
        assert row.sell_reason == SELL_REASON_TRAILING_STOP
        assert row.sell_reason_note == "回撤 8% 触发移动止盈"
        assert row.report_id == "rpt-1"
        assert row.plan_id == "plan-1"
        assert row.source == "suggested"
        assert row.note == "按计划执行"

    def test_list_trades_filters(self, db):
        _record(db, trade_date="2024-05-06")
        _record(db, trade_date="2024-05-07", price=10.5)
        _record(db, symbol="000001", trade_date="2024-05-08")
        _record(db, user_id="other", trade_date="2024-05-08")

        assert len(tls.list_trades(db, user_id=USER)) == 3
        assert len(tls.list_trades(db, user_id=USER, symbol="600519")) == 2
        assert len(tls.list_trades(db, user_id=USER, symbol="000001")) == 1
        ranged = tls.list_trades(db, user_id=USER, start_date="2024-05-07", end_date="2024-05-07")
        assert [row.trade_date for row in ranged] == ["2024-05-07"]


# --------------------------------------------------------------------------- #
# 归因
# --------------------------------------------------------------------------- #

def _seed_attribution_book(db):
    """建一本有盈有亏的小台账：两次买入 + 三笔不同原因的卖出。"""

    _record(db, price=10.0, shares=1000.0, trade_date="2024-05-06")            # 摊薄成本 10.0051
    _record(db, price=12.0, shares=1000.0, trade_date="2024-05-07")            # 摊薄成本 11.0051
    _record(db, action="SELL", price=12.0, shares=300.0, trade_date="2024-05-08",
            sell_reason=SELL_REASON_TAKE_PROFIT)                               # +291.63
    _record(db, action="SELL", price=9.0, shares=200.0, trade_date="2024-05-09",
            sell_reason=SELL_REASON_STOP_LOSS)                                 # -406.94
    _record(db, action="SELL", price=11.0, shares=100.0, trade_date=TODAY,
            sell_reason=SELL_REASON_INVALIDATION)                              # -6.07


class TestAttribution:
    def test_groups_totals_and_sorting(self, db):
        _seed_attribution_book(db)
        result = tls.attribution_by_sell_reason(db, user_id=USER)

        reasons = [item["sell_reason"] for item in result["items"]]
        # 按 |已实现盈亏| 降序：止损 406.94 > 止盈 291.63 > 逻辑失效 6.07
        assert reasons == [SELL_REASON_STOP_LOSS, SELL_REASON_TAKE_PROFIT, SELL_REASON_INVALIDATION]

        by_reason = {item["sell_reason"]: item for item in result["items"]}
        stop = by_reason[SELL_REASON_STOP_LOSS]
        assert stop["label_zh"] == SELL_REASON_LABELS[SELL_REASON_STOP_LOSS]
        assert stop["count"] == 1
        assert stop["shares"] == 200.0
        assert stop["amount"] == 1800.0
        assert stop["realized_pnl"] == pytest.approx(-406.94, abs=0.01)
        assert stop["loss_count"] == 1
        assert stop["win_count"] == 0

        profit = by_reason[SELL_REASON_TAKE_PROFIT]
        assert profit["realized_pnl"] == pytest.approx(291.63, abs=0.01)
        assert profit["win_count"] == 1
        assert profit["loss_count"] == 0

        assert result["total_realized_pnl"] == pytest.approx(-121.38, abs=0.02)
        assert result["total_count"] == 3
        assert result["total_shares"] == 600.0
        assert result["win_count"] == 1
        assert result["loss_count"] == 2
        assert result["label"]

    def test_date_range_filter(self, db):
        _seed_attribution_book(db)
        result = tls.attribution_by_sell_reason(
            db, user_id=USER, start_date="2024-05-09", end_date="2024-05-09"
        )
        assert [item["sell_reason"] for item in result["items"]] == [SELL_REASON_STOP_LOSS]
        assert result["total_realized_pnl"] == pytest.approx(-406.94, abs=0.01)

    def test_ignores_buy_rows(self, db):
        _seed_attribution_book(db)
        result = tls.attribution_by_sell_reason(db, user_id=USER)
        assert all(item["sell_reason"] in SELL_REASON_LABELS for item in result["items"])
        assert result["total_count"] == 3          # 买入不参与卖出原因归因

    def test_missing_reason_grouped_as_unknown(self, db):
        # 模拟他方/历史写入的、没有卖出原因的流水行
        db.add(TradeLedgerDB(
            id="legacy-1", user_id=USER, symbol="600519", trade_date="2024-05-06",
            action="SELL", price=10.0, shares=100.0, amount=1000.0, realized_pnl=42.0,
            sell_reason=None,
        ))
        db.commit()

        result = tls.attribution_by_sell_reason(db, user_id=USER)
        unknown = next(item for item in result["items"] if item["sell_reason"] == SELL_REASON_UNKNOWN)
        assert unknown["label_zh"] == SELL_REASON_LABELS[SELL_REASON_UNKNOWN]
        assert unknown["realized_pnl"] == pytest.approx(42.0)
        assert unknown["missing_reason_count"] == 1


class TestAttributionSummary:
    def test_win_rate_and_average_win_loss(self, db):
        _seed_attribution_book(db)
        summary = tls.attribution_summary(db, user_id=USER)

        assert summary["trade_count"] == 5
        assert summary["buy_count"] == 2
        assert summary["sell_count"] == 3
        assert summary["win_count"] == 1
        assert summary["loss_count"] == 2
        assert summary["win_rate"] == pytest.approx(1 / 3, abs=1e-4)
        assert summary["average_win"] == pytest.approx(291.63, abs=0.01)
        assert summary["average_loss"] == pytest.approx(-206.5, abs=0.02)
        assert summary["average_loss_abs"] == pytest.approx(206.5, abs=0.02)
        assert summary["total_realized_pnl"] == pytest.approx(-121.38, abs=0.02)
        assert summary["missing_sell_reason_count"] == 0
        assert summary["attribution_coverage"] == 1.0
        assert summary["label"]

    def test_missing_reason_count_for_coverage(self, db):
        _seed_attribution_book(db)
        db.add(TradeLedgerDB(
            id="legacy-2", user_id=USER, symbol="600519", trade_date="2024-05-06",
            action="SELL", price=10.0, shares=100.0, amount=1000.0, realized_pnl=50.0,
            sell_reason=None,
        ))
        db.commit()

        summary = tls.attribution_summary(db, user_id=USER)
        assert summary["missing_sell_reason_count"] == 1
        assert summary["sell_count"] == 4
        assert summary["win_count"] == 2      # +291.63 与 +50
        assert summary["loss_count"] == 2     # -406.94 与 -6.07
        assert summary["win_rate"] == pytest.approx(0.5, abs=1e-4)
        assert summary["attribution_coverage"] == pytest.approx(0.75)
        assert summary["total_realized_pnl"] == pytest.approx(-71.38, abs=0.02)

    def test_by_symbol_breakdown(self, db):
        _seed_attribution_book(db)
        _record(db, symbol="000001", price=10.0, shares=500.0, trade_date="2024-05-06")
        _record(db, symbol="000001", action="SELL", price=13.0, shares=500.0,
                trade_date=YESTERDAY, sell_reason=SELL_REASON_TAKE_PROFIT)

        summary = tls.attribution_summary(db, user_id=USER)
        symbols = [item["symbol"] for item in summary["by_symbol"]]
        assert set(symbols) == {"600519", "000001"}
        # 按已实现盈亏降序：000001 盈利，600519 亏损
        assert symbols[0] == "000001"
        top = summary["by_symbol"][0]
        assert top["sell_count"] == 1
        assert top["buy_count"] == 1
        assert top["win_count"] == 1
        assert top["realized_pnl"] > 0
        assert summary["symbol_count"] == 2

    def test_empty_portfolio(self, db):
        summary = tls.attribution_summary(db, user_id="nobody")
        assert summary["trade_count"] == 0
        assert summary["win_rate"] == 0.0
        assert summary["average_win"] == 0.0
        assert summary["average_loss"] == 0.0
        assert summary["by_sell_reason"] == []
        assert summary["by_symbol"] == []


# --------------------------------------------------------------------------- #
# 历史成交点导入
# --------------------------------------------------------------------------- #

TOLERANT_POINTS = [
    # 英文键名
    {"date": "2024-05-06", "direction": "BUY", "price": 10.0, "quantity": 1000},
    # 斜杠日期 + 中文买卖方向 + volume + 备注推断卖出原因
    {"trade_date": "2024/05/07", "action": "证券卖出", "price": "11.00",
     "volume": "600", "note": "止盈减仓"},
    # 带时间的 datetime + 中文"买入" + amount 充当股数 + 千分位
    {"time": "2024-05-08 09:35:00", "type": "买入", "price": "10.5", "amount": "1,000"},
    # 全中文键名 + 紧凑日期 + "500股" + 备注里的止损
    {"成交日期": "20240509", "买卖方向": "卖出", "成交价格": "9.80",
     "成交数量": "500股", "备注": "跌破止损位，止损"},
    # 买入携带卖出原因应被忽略
    {"date": "2024-05-10", "action": "买入", "price": 10.0, "shares": 200,
     "sell_reason": "stop_loss"},
]


class TestImportTradePoints:
    def test_tolerant_parser_imports_all_shapes(self, db):
        result = tls.import_trade_points(
            db, user_id=USER, symbol="600519", trade_points=TOLERANT_POINTS,
            name="贵州茅台",
        )
        assert result["inserted"] == 5
        assert result["duplicates"] == 0
        assert result["skipped"] == 0
        assert result["reasons"] == []

        state = result["state"]
        assert state["position"] == 1100.0        # 1000 - 600 + 1000 - 500 + 200
        assert state["available"] == 900.0        # 今日买入的 200 股不可卖
        assert state["trade_count"] == 5

        rows = tls.list_trades(db, user_id=USER, symbol="600519")
        assert [row.action for row in rows] == ["BUY", "SELL", "BUY", "SELL", "BUY"]
        assert [row.trade_date for row in rows] == [
            "2024-05-06", "2024-05-07", "2024-05-08", "2024-05-09", "2024-05-10",
        ]
        assert rows[1].shares == 600.0            # volume → 股数
        assert rows[1].sell_reason == SELL_REASON_TAKE_PROFIT   # 备注"止盈" → 止盈
        assert rows[2].shares == 1000.0           # amount/千分位 → 股数
        assert rows[2].price == 10.5
        assert rows[3].shares == 500.0            # "500股" → 股数
        assert rows[3].sell_reason == SELL_REASON_STOP_LOSS     # 备注"止损" → 止损
        assert rows[4].sell_reason is None        # 买入不携带卖出原因
        assert all(row.name == "贵州茅台" for row in rows)
        assert all(row.source == "import" for row in rows)

    def test_reimport_is_idempotent(self, db):
        tls.import_trade_points(db, user_id=USER, symbol="600519", trade_points=TOLERANT_POINTS)
        before = _count(db)
        again = tls.import_trade_points(
            db, user_id=USER, symbol="600519", trade_points=TOLERANT_POINTS
        )
        assert again["inserted"] == 0
        assert again["duplicates"] == 5
        assert again["skipped"] == 0
        assert _count(db) == before == 5

    def test_malformed_points_are_skipped_without_raising(self, db):
        points = [
            {"date": "2024-05-06", "action": "买入", "price": 10.0, "shares": 100},
            {"date": "not-a-date", "action": "买入", "price": 10.0, "shares": 100},
            {"date": "2024-05-06", "action": "转入", "price": 10.0, "shares": 100},
            {"date": "2024-05-06", "price": 10.0, "shares": 100},          # 缺方向
            {"date": "2024-05-06", "action": "买入", "price": 0, "shares": 100},
            {"date": "2024-05-06", "action": "买入", "price": 10.0, "shares": 0},
            {"date": "2024-05-06", "action": "买入", "shares": 100},       # 缺价格
            "这是一条脏数据",                                              # 非字典
            None,
        ]
        result = tls.import_trade_points(
            db, user_id=USER, symbol="600519", trade_points=points
        )
        assert result["inserted"] == 1
        assert result["skipped"] == 8
        assert len(result["reasons"]) == 8
        assert all("跳过" in reason for reason in result["reasons"])
        assert _count(db) == 1

    def test_empty_or_missing_points_is_safe(self, db):
        none_result = tls.import_trade_points(
            db, user_id=USER, symbol="600519", trade_points=None
        )
        assert none_result["inserted"] == 0
        assert none_result["skipped"] == 0
        assert none_result["state"]["position"] == 0.0

        empty_result = tls.import_trade_points(
            db, user_id=USER, symbol="600519", trade_points=[]
        )
        assert empty_result["inserted"] == 0
        assert empty_result["duplicates"] == 0
        assert empty_result["skipped"] == 0

    def test_nested_trade_object_and_symbol_override(self, db):
        result = tls.import_trade_points(
            db, user_id=USER, symbol="600519",
            trade_points=[
                {"trade": {"date": "2024-05-06", "action": "买入", "price": 9.0, "shares": 300},
                 "备注": "补仓"},
                {"date": "2024-05-07", "action": "买入", "price": 9.5, "shares": 300,
                 "code": "000001"},
            ],
        )
        assert result["inserted"] == 2
        assert tls.rebuild_position_state(db, user_id=USER, symbol="600519")["position"] == 300.0
        assert tls.rebuild_position_state(db, user_id=USER, symbol="000001")["position"] == 300.0


# --------------------------------------------------------------------------- #
# 与导入快照对账
# --------------------------------------------------------------------------- #

def _snapshot(db, *, symbol, current_position, available_position, points,
              average_cost=None, source="manual", name="测试标的"):
    db.add(ImportedPortfolioPositionDB(
        id=f"snap-{symbol}",
        user_id=USER,
        source=source,
        symbol=symbol,
        security_name=name,
        current_position=current_position,
        available_position=available_position,
        average_cost=average_cost,
        trade_points_json=points,
        trade_points_count=len(points or []),
    ))
    db.commit()


class TestSyncFromImportedPositions:
    def test_detects_snapshot_vs_ledger_mismatch(self, db):
        _snapshot(
            db, symbol="600519.SH", current_position=1000.0, available_position=1000.0,
            points=[{"date": "2024-05-06", "action": "买入", "price": 10.0, "shares": 500}],
        )
        _snapshot(
            db, symbol="000001.SZ", current_position=500.0, available_position=0.0,
            points=[{"date": TODAY, "action": "买入", "price": 10.0, "shares": 500}],
        )
        _snapshot(
            db, symbol="300750.SZ", current_position=None, available_position=None,
            points=[],
        )

        result = tls.sync_from_imported_positions(db, user_id=USER)

        mismatched = {item["field"]: item for item in result["mismatches"]}
        assert set(mismatched) == {"position", "available"}
        assert all(item["symbol"] == "600519.SH" for item in result["mismatches"])
        assert mismatched["position"]["snapshot"] == 1000.0
        assert mismatched["position"]["ledger"] == 500.0
        assert mismatched["position"]["diff"] == -500.0
        assert "不一致" in mismatched["position"]["message"]

        by_symbol = {item["symbol"]: item for item in result["symbols"]}
        assert by_symbol["000001.SZ"]["mismatches"] == []      # 快照与台账一致
        assert by_symbol["000001.SZ"]["ledger"]["position"] == 500.0
        assert by_symbol["000001.SZ"]["ledger"]["available"] == 0.0
        assert by_symbol["300750.SZ"]["mismatches"] == []      # 快照无数据 → 不可对账

        assert result["summary"]["symbols"] == 3
        assert result["summary"]["mismatch_count"] == 2
        assert result["summary"]["symbols_with_mismatch"] == 1
        assert result["summary"]["ledger_trade_count"] == 2
        assert result["imported"]["inserted"] == 2
        assert result["label"]

    def test_matching_snapshot_reports_no_mismatch(self, db):
        _snapshot(
            db, symbol="600519.SH", current_position=1000.0, available_position=1000.0,
            points=[{"date": YESTERDAY, "action": "买入", "price": 10.0, "shares": 1000}],
            average_cost=10.0051,
        )
        result = tls.sync_from_imported_positions(db, user_id=USER)
        assert result["mismatches"] == []
        entry = result["symbols"][0]
        assert entry["ledger"]["position"] == 1000.0
        assert entry["ledger"]["available"] == 1000.0
        assert entry["average_cost_diff"] == pytest.approx(0.0, abs=1e-6)
        assert result["summary"]["matched_symbols"] == 1

    def test_alias_symbol_is_reconciled(self, db):
        """台账里是 6 位代码、快照带 .SH 后缀时不应误报不一致。"""

        _record(db, symbol="600520", price=10.0, shares=1000.0, trade_date="2024-05-06")
        _snapshot(
            db, symbol="600520.SH", current_position=1000.0, available_position=1000.0,
            points=[],
        )
        result = tls.sync_from_imported_positions(db, user_id=USER)
        assert result["mismatches"] == []
        entry = result["symbols"][0]
        assert entry["symbol"] == "600520.SH"
        assert entry["ledger_symbol"] == "600520"
        assert entry["ledger"]["position"] == 1000.0

    def test_sync_is_idempotent(self, db):
        _snapshot(
            db, symbol="600519.SH", current_position=500.0, available_position=500.0,
            points=[{"date": "2024-05-06", "action": "买入", "price": 10.0, "shares": 500}],
        )
        first = tls.sync_from_imported_positions(db, user_id=USER)
        assert first["imported"]["inserted"] == 1

        second = tls.sync_from_imported_positions(db, user_id=USER)
        assert second["imported"]["inserted"] == 0
        assert second["imported"]["duplicates"] == 1
        assert second["mismatches"] == []
        assert _count(db) == 1

    def test_no_imported_positions_is_safe(self, db):
        result = tls.sync_from_imported_positions(db, user_id="nobody")
        assert result["symbols"] == []
        assert result["mismatches"] == []
        assert result["summary"]["symbols"] == 0
