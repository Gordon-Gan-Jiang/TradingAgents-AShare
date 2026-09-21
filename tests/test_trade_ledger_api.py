"""台账 API 的接线验证。

这里直接调用路由函数（而非起 TestClient），目的是验证**接线**本身：
参数名、服务签名、返回结构是否真的对得上。历史上这个项目最典型的问题
不是"能力没实现"，而是"实现了却没人调用"——所以每个新端点都值得一条接线测试。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import api.main as main
from api.database import Base, UserDB

USER_ID = "u-ledger-api"
SYMBOL = "600519.SH"


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(UserDB(id=USER_ID, email="ledger-api@test.local"))
    session.commit()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def user(db):
    return db.query(UserDB).filter(UserDB.id == USER_ID).first()


def _record(db, user, *, action, price, shares, date, sell_reason=None):
    return main.record_trade_ledger(
        main.TradeLedgerRecordRequest(
            symbol=SYMBOL,
            action=action,
            price=price,
            shares=shares,
            trade_date=date,
            name="贵州茅台",
            sell_reason=sell_reason,
        ),
        current_user=user,
        db=db,
    )


class TestLedgerApiWiring:
    def test_record_then_list_round_trip(self, db, user):
        """买入 → 卖出后，台账必须能列出两笔并给出归因。"""
        _record(db, user, action="BUY", price=100.0, shares=1000.0, date="2026-04-01")
        _record(
            db, user, action="SELL", price=110.0, shares=1000.0, date="2026-04-10",
            sell_reason="take_profit",
        )
        listed = main.list_trade_ledger(current_user=user, db=db)
        assert listed["total"] == 2
        assert {t["action"] for t in listed["trades"]} == {"BUY", "SELL"}

    def test_record_returns_rebuilt_position(self, db, user):
        """记账后立刻回放持仓，用户不必等下一次导入才能看到状态。"""
        result = _record(db, user, action="BUY", price=100.0, shares=1000.0, date="2026-04-01")
        assert result["position"]["position"] == pytest.approx(1000.0)
        assert result["trade"] is not None

    def test_duplicate_submission_is_idempotent(self, db, user):
        """重复提交同一笔成交不得重复计入盈亏。"""
        _record(db, user, action="BUY", price=100.0, shares=1000.0, date="2026-04-01")
        second = _record(db, user, action="BUY", price=100.0, shares=1000.0, date="2026-04-01")
        assert second["trade"] is None
        assert second["meta"]["status"] == "duplicate"
        assert main.list_trade_ledger(current_user=user, db=db)["total"] == 1

    def test_attribution_endpoint_returns_reason_groups(self, db, user):
        """归因端点必须按卖出原因分组——这是"哪种卖出决策真赚钱"的唯一答案来源。"""
        _record(db, user, action="BUY", price=100.0, shares=1000.0, date="2026-04-01")
        _record(
            db, user, action="SELL", price=90.0, shares=1000.0, date="2026-04-10",
            sell_reason="stop_loss",
        )
        payload = main.get_trade_attribution(current_user=user, db=db)
        assert "by_reason" in payload
        assert "summary" in payload

    def test_position_endpoint_replays_from_ledger(self, db, user):
        _record(db, user, action="BUY", price=100.0, shares=500.0, date="2026-04-01")
        position = main.get_ledger_position(symbol=SYMBOL, current_user=user, db=db)
        assert position["position"] == pytest.approx(500.0)

    def test_sync_endpoint_runs_without_imported_positions(self, db, user):
        """没有导入快照时对账也必须正常返回，而不是抛异常。"""
        assert isinstance(main.sync_trade_ledger(current_user=user, db=db), dict)

    def test_import_endpoint_accepts_trade_points(self, db, user):
        result = main.import_trade_ledger_points(
            main.TradeLedgerImportRequest(
                symbol=SYMBOL,
                trade_points=[
                    {"trade_date": "2026-04-01", "action": "买入", "price": 100.0, "shares": 100.0},
                ],
            ),
            current_user=user,
            db=db,
        )
        assert isinstance(result, dict)

    def test_ledger_is_isolated_per_user(self, db, user):
        """台账必须按用户隔离。"""
        _record(db, user, action="BUY", price=100.0, shares=1000.0, date="2026-04-01")
        other = UserDB(id="other-user", email="other@test.local")
        db.add(other)
        db.commit()
        assert main.list_trade_ledger(current_user=other, db=db)["total"] == 0
