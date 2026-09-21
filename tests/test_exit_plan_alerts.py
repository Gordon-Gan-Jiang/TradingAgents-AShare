"""退出纪律提醒的端到端验证（P0-3 的最后一段）。

产品的推送一直是**单向**的：只在"涨停逼近"和"急拉"时提醒，跌下去一声不响。
也就是说，最能决定盈亏的方向完全没有通知。本文件验证反方向真的接通了：

  计划里的硬止损 → 现价跌破 → 生成推送（含建议股数与免责声明）
  且同一 (用户, 标的, 触发原因) 当天不重复轰炸。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.database import Base, ImportedPortfolioPositionDB, UserDB
from api.services import exit_engine_service
from api.services import tracking_price_alert_service as alerts
from api.services import trade_plan_service as tps

USER_ID = "u-alert"
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
    session.add(UserDB(id=USER_ID, email="alert@test.local"))
    session.add(
        ImportedPortfolioPositionDB(
            id="pos-1",
            user_id=USER_ID,
            source="manual",
            symbol=SYMBOL,
            security_name="东山精密",
            current_position=1000.0,
            available_position=1000.0,
            average_cost=190.0,
        )
    )
    session.commit()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def sent(monkeypatch):
    """打开所有闸门，捕获推送内容而不是真的发出去。"""
    captured: list[dict] = []

    monkeypatch.setattr(alerts, "price_alerts_enabled", lambda: True)
    monkeypatch.setattr(alerts, "is_cn_trading_day", lambda *a, **k: True)
    monkeypatch.setattr(alerts, "_in_trading_session", lambda now: True)

    class _Cfg:
        wecom_webhook_encrypted = "enc-wecom"
        wps_webhook_encrypted = None

    monkeypatch.setattr(alerts.auth_service, "get_user_llm_config", lambda db, uid: _Cfg())
    monkeypatch.setattr(alerts.auth_service, "decrypt_secret", lambda v: v)

    def _fake_send(*, user, text_body, markdown_body, wecom_webhook, wps_webhook):
        captured.append({"text": text_body, "markdown": markdown_body})
        return True

    monkeypatch.setattr(alerts, "_send_alert_to_enabled_channels", _fake_send)

    # 每次用例从干净的内存态开始，避免模块级去重字典互相污染
    monkeypatch.setattr(alerts, "_exit_alert_sent", {})
    monkeypatch.setattr(alerts, "_peak_price", {})
    return captured


def _persist_plan(db):
    tps.persist_plan_from_analysis(
        db,
        user_id=USER_ID,
        report_id="r-alert",
        symbol=SYMBOL,
        name="东山精密",
        signal_trade_date="2026-04-10",
        decision_text=PLAN_TEXT,
        trader_plan_text=PLAN_TEXT,
    )
    db.commit()


def _set_price(monkeypatch, price, change_pct=0.0):
    monkeypatch.setattr(
        alerts,
        "_fetch_live_quotes",
        lambda symbols: {SYMBOL: {"price": price, "previous_close": price, "change_pct": change_pct}},
    )


class TestDownsideAlertIsNowWired:
    def test_stop_loss_breach_pushes_an_alert(self, db, sent, monkeypatch):
        """核心断言：跌破止损会推送。修复前这条路径根本不存在。"""
        _persist_plan(db)
        _set_price(monkeypatch, 181.0)
        assert alerts.run_exit_plan_alerts(db) == 1
        assert len(sent) == 1
        body = sent[0]["text"]
        assert "硬止损" in body
        assert "东山精密" in body
        assert exit_engine_service.EXIT_DISCLAIMER in body

    def test_alert_includes_suggested_shares_and_constraints(self, db, sent, monkeypatch):
        _persist_plan(db)
        _set_price(monkeypatch, 181.0)
        alerts.run_exit_plan_alerts(db)
        assert "建议动作" in sent[0]["text"]

    def test_healthy_holding_does_not_push(self, db, sent, monkeypatch):
        """没触发就不该打扰——推送必须稀缺，否则用户学会忽略它。"""
        _persist_plan(db)
        _set_price(monkeypatch, 195.0)
        assert alerts.run_exit_plan_alerts(db) == 0
        assert sent == []

    def test_same_trigger_is_not_re_sent_same_day(self, db, sent, monkeypatch):
        """75 秒轮询一次，同一天同一原因只能推一条。"""
        _persist_plan(db)
        _set_price(monkeypatch, 181.0)
        assert alerts.run_exit_plan_alerts(db) == 1
        assert alerts.run_exit_plan_alerts(db) == 0
        assert len(sent) == 1

    def test_holding_without_plan_does_not_push(self, db, sent, monkeypatch):
        """无计划时不推送（面板里提示），避免把"没有计划"变成每日噪音。"""
        _set_price(monkeypatch, 181.0)
        assert alerts.run_exit_plan_alerts(db) == 0
        assert sent == []

    def test_alerts_are_off_when_disabled(self, db, sent, monkeypatch):
        _persist_plan(db)
        _set_price(monkeypatch, 181.0)
        monkeypatch.setattr(alerts, "price_alerts_enabled", lambda: False)
        assert alerts.run_exit_plan_alerts(db) == 0
        assert sent == []

    def test_no_holdings_is_a_noop(self, db, sent, monkeypatch):
        monkeypatch.setattr(alerts, "price_alerts_enabled", lambda: True)
        db.query(ImportedPortfolioPositionDB).delete()
        db.commit()
        assert alerts.run_exit_plan_alerts(db) == 0


# --------------------------------------------------------------------------- #
# C2：方向校验未通过时不得推送
# --------------------------------------------------------------------------- #

# 看空计划：失效价 200.00 高于现价——旧实现把它当多头止损位，
# 于是只要现价 <= 200 就命中"跌破硬止损"，首日推送"清仓 100%"。
SELL_PLAN_TEXT = """最终交易建议：卖出

<!-- TRADE_PLAN
direction: SELL
entry_low: 188.00
entry_high: 195.50
position_cap_pct: 5
first_tranche_pct: 2
hard_stop_price: 200.00
take_profit_ladder: 150.0:50
trailing_stop_pct: 8
time_stop_days: 45
invalidation_conditions: []
-->
"""

# 无方向字段的计划（历史数据）：方向未经确认，判定照常但不得对外推送。
NO_DIRECTION_PLAN_TEXT = """最终交易建议：买入

<!-- TRADE_PLAN
entry_low: 188.00
entry_high: 195.50
hard_stop_price: 182.13
take_profit_ladder: 214.9:30
time_stop_days: 45
-->
"""


class TestDirectionGateStopsPush:
    def _persist(self, db, text):
        tps.persist_plan_from_analysis(
            db,
            user_id=USER_ID,
            report_id="r-gate",
            symbol=SYMBOL,
            name="东山精密",
            signal_trade_date="2026-04-10",
            decision_text=text,
            trader_plan_text=text,
        )
        db.commit()

    def test_sell_plan_does_not_push_liquidation(self, db, sent, monkeypatch):
        """核心回归：看空计划的失效价曾被当成止损位，向真实用户推送清仓。"""
        self._persist(db, SELL_PLAN_TEXT)
        _set_price(monkeypatch, 190.0)
        assert alerts.run_exit_plan_alerts(db) == 0
        assert sent == []

    def test_plan_without_direction_does_not_push(self, db, sent, monkeypatch):
        """方向未经确认 → 只记录不推送，避免把未验证的价位发给用户。"""
        self._persist(db, NO_DIRECTION_PLAN_TEXT)
        _set_price(monkeypatch, 181.0)
        assert alerts.run_exit_plan_alerts(db) == 0
        assert sent == []

    def test_suppression_is_logged_for_observation(self, db, sent, monkeypatch, caplog):
        """“先只记录不拦截”要求抑制行为可观察，而不是静默丢弃。

        这里用**无方向计划**：方向缺失时判定照常产出"清仓"，闸门只压制对外动作——
        这正是 log-only 那一层。看空计划则由退出引擎直接降级为"观察"，
        在更早的分支就被拦下（见上一条用例）。
        """
        import logging

        self._persist(db, NO_DIRECTION_PLAN_TEXT)
        _set_price(monkeypatch, 181.0)
        with caplog.at_level(logging.INFO, logger="api.services.tracking_price_alert_service"):
            alerts.run_exit_plan_alerts(db)
        assert any("已抑制推送" in r.message for r in caplog.records)

    def test_coherent_buy_plan_still_pushes(self, db, sent, monkeypatch):
        """闸门不能误伤正常计划——这是它存在的全部意义。"""
        _persist_plan(db)
        _set_price(monkeypatch, 181.0)
        assert alerts.run_exit_plan_alerts(db) == 1
        assert len(sent) == 1
