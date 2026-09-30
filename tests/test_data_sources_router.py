"""数据源探测端点与定时调度测试。

两条边界必须钉住：

1. **架构边界**：新端点必须走 `api/routers/`，不能写进 `api/main.py`
   （由 `tests/test_architecture_boundaries.py` 强制，这里再针对本路由确认一次）。
2. **调度判定**：定时探测"该跑的时候跑、不该跑的时候别跑"。调度逻辑最容易出的
   问题是**非交易日也跑**（非交易日的空数据会把报告污染成一片红）和
   **同一交易日反复跑**（UTC 与北京时间错位一天就会这样），这两条都直接测。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.database import Base, get_db
from api.routers import data_sources as router_module
from api.services import data_source_probe_service as probe


@pytest.fixture
def client(db_session):
    app = FastAPI()
    app.include_router(router_module.router)
    app.dependency_overrides[get_db] = lambda: db_session
    # 端点声明了 current_user 依赖；这里替换成假用户，避免依赖真实鉴权链路。
    from api.dependencies import current_user

    app.dependency_overrides[current_user] = lambda: type(
        "U", (), {"id": "test-user", "username": "tester"}
    )()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def db_session():
    """跨线程共享的**同一个**内存库。

    必须用 StaticPool：TestClient 在**另一个线程**里跑请求，而内存 SQLite 的默认
    池是 SingletonThreadPool，每个线程会拿到各自的连接——也就是各自的空库，
    表现为 "no such table"（表其实建过，只是建在别的连接上）。
    """
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


# ---------------------------------------------------------------- 端点


def test_matrix_endpoint_returns_expected_coverage(client):
    resp = client.get("/v1/data-sources/matrix")
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"], "能力矩阵为空"
    tools = {item["tool"] for item in body["items"]}
    assert "get_daily_bars_df" in tools
    daily = next(i for i in body["items"] if i["tool"] == "get_daily_bars_df")
    assert daily["sources"][0]["source"] == "cn_baostock"
    assert daily["sources"][0]["tier"] == 1


def test_status_endpoint_is_valid_before_any_probe(client):
    resp = client.get("/v1/data-sources/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["latest_run"] is None
    assert body["results"] == []
    assert "running" in body and body["running"] is False
    assert "enabled" in body


def test_history_endpoint_returns_points_list(client):
    resp = client.get("/v1/data-sources/history", params={"days": 7})
    assert resp.status_code == 200
    assert resp.json() == {"points": []}


def test_history_endpoint_validates_days(client):
    assert client.get("/v1/data-sources/history", params={"days": 0}).status_code == 422
    assert (
        client.get("/v1/data-sources/history", params={"days": 9999}).status_code == 422
    )


def test_status_reflects_a_persisted_run(client, db_session):
    provider = type(
        "FakeProvider",
        (),
        {
            "get_daily_bars_df": lambda self, *a, **kw: __import__("pandas").DataFrame(
                {"date": ["2026-09-23"]}
            )
        },
    )()
    registry = type("R", (), {"get": lambda self, n: provider if n == "fake_src" else None})()
    probe.run_probe(
        db_session,
        ctx=probe.ProbeContext(
            symbol="600519.SH", board="银行", day="2026-09-23", start="2026-09-11", end="2026-09-23"
        ),
        targets=[("fake_src", "get_daily_bars_df")],
        registry=registry,
    )

    body = client.get("/v1/data-sources/status").json()
    assert body["latest_run"]["ok_count"] == 1
    assert body["results"][0]["tool"] == "get_daily_bars_df"
    assert len(body["runs"]) == 1


# ---------------------------------------------------------------- 调度


def test_scheduling_is_disabled_by_env(monkeypatch, db_session):
    monkeypatch.setenv("TA_DATA_SOURCE_PROBE_ENABLED", "0")
    due, reason = probe.should_run_scheduled_probe(db_session)
    assert due is False and reason == "disabled"


def test_scheduling_skips_non_trading_days(monkeypatch, db_session):
    """非交易日不跑：那天的空数据只会把报告染红，属于自己制造假故障。"""
    monkeypatch.delenv("TA_DATA_SOURCE_PROBE_ENABLED", raising=False)
    monkeypatch.setattr(probe, "is_cn_trading_day", lambda day: False)
    due, reason = probe.should_run_scheduled_probe(
        db_session, now=datetime(2026, 9, 26, 9, 30, tzinfo=probe.TZ_CN)
    )
    assert due is False and reason == "non-trading-day"


def test_scheduling_waits_until_the_configured_time(monkeypatch, db_session):
    monkeypatch.delenv("TA_DATA_SOURCE_PROBE_ENABLED", raising=False)
    monkeypatch.setenv("TA_DATA_SOURCE_PROBE_AT", "09:00")
    monkeypatch.setattr(probe, "is_cn_trading_day", lambda day: True)
    due, reason = probe.should_run_scheduled_probe(
        db_session, now=datetime(2026, 9, 24, 8, 30, tzinfo=probe.TZ_CN)
    )
    assert due is False and reason == "before-scheduled-time"


def test_scheduling_is_due_on_a_trading_day_after_the_time(monkeypatch, db_session):
    monkeypatch.delenv("TA_DATA_SOURCE_PROBE_ENABLED", raising=False)
    monkeypatch.setenv("TA_DATA_SOURCE_PROBE_AT", "09:00")
    monkeypatch.setattr(probe, "is_cn_trading_day", lambda day: True)
    due, reason = probe.should_run_scheduled_probe(
        db_session, now=datetime(2026, 9, 24, 9, 0, tzinfo=probe.TZ_CN)
    )
    assert due is True and reason == "due"


def test_scheduling_does_not_rerun_twice_in_one_day(monkeypatch, db_session):
    """同一交易日只跑一次——按**北京时间**判断，避免 UTC 错位导致重复触发。"""
    from api.database import DataSourceProbeRunDB

    monkeypatch.delenv("TA_DATA_SOURCE_PROBE_ENABLED", raising=False)
    monkeypatch.setenv("TA_DATA_SOURCE_PROBE_AT", "09:00")
    monkeypatch.setattr(probe, "is_cn_trading_day", lambda day: True)

    now_cn = datetime(2026, 9, 24, 9, 5, tzinfo=probe.TZ_CN)
    # 刚跑过：存 UTC 时间戳（项目惯例），对应北京时间 2026-09-24 09:01
    db_session.add(
        DataSourceProbeRunDB(
            id="run-1",
            started_at=now_cn.astimezone(probe.timezone.utc),
            status="ok",
            trigger=probe.TRIGGER_SCHEDULED,
            trading_day=True,
        )
    )
    db_session.commit()

    due, reason = probe.should_run_scheduled_probe(db_session, now=now_cn)
    assert due is False and reason == "already-ran-today"

    # 换一天就该跑了
    tomorrow = datetime(2026, 9, 25, 9, 5, tzinfo=probe.TZ_CN)
    due, reason = probe.should_run_scheduled_probe(db_session, now=tomorrow)
    assert due is True and reason == "due"


def test_probe_at_normalization():
    assert probe._normalize_hhmm("9:00") == "09:00"
    assert probe._normalize_hhmm("09:00:00") == "09:00"
    assert probe._normalize_hhmm("") == "09:00"
    assert probe._normalize_hhmm("garbage") == "09:00"
    assert probe._normalize_hhmm("23:59") == "23:59"


# ---------------------------------------------------------------- 架构边界


def test_endpoints_live_in_the_router_not_in_main():
    """新端点不得写进 api/main.py（架构边界测试同款约束）。

    断言的是**路由装饰器**，不是字符串出现——main.py 的注释里提到页面路径
    是完全正当的（"结果见 /data-sources 页面"），按字符串判会误报。
    """
    import re
    from pathlib import Path

    main_src = Path("api/main.py").read_text(encoding="utf-8")
    declared = re.findall(r'@app\.(?:get|post|put|delete|patch)\(\s*"([^"]+)"', main_src)
    offenders = [p for p in declared if p.startswith("/v1/data-sources")]
    assert not offenders, f"端点声明在 api/main.py，应移到 api/routers/：{offenders}"

    router_src = Path("api/routers/data_sources.py").read_text(encoding="utf-8")
    for path in ("/matrix", "/status", "/history", "/probe"):
        assert f'"{path}"' in router_src


def test_router_is_exported_and_mounted():
    import api.routers as routers

    assert "data_sources" in routers.__all__

    # 注意：「真的挂到 app 上了吗」不在这里断言。
    # 早先的写法是把 api/main.py 当**字符串**读、断言 `"_data_sources_router" in main_src`，
    # 那种断言在 router 被注释掉、或挂载语句写错时照样通过。
    # 真实行为断言见 tests/test_data_source_probe_scheduler.py:
    #   - test_probe_endpoints_are_really_mounted
    #   - test_lifespan_actually_starts_and_stops_the_probe_loop


# ---------------------------------------------------------------------------
# 新增字段的 HTTP 契约（timeout 状态 / row_unit / routed）
# ---------------------------------------------------------------------------

def test_matrix_endpoint_exposes_the_routed_flag(client):
    """`/matrix` 必须带 `routed`：False 表示这条路**没有 router 兜底链**。

    基准指数日线就是这种（由 mainline_scoring 直接调用）。页面要能说明
    "它没有备用源"，否则会看起来和普通维度一样。
    """
    body = client.get("/v1/data-sources/matrix").json()
    items = {item["tool"]: item for item in body["items"]}

    assert "get_index_daily_bars_df" in items, "直连能力没出现在矩阵里"
    assert items["get_index_daily_bars_df"]["routed"] is False
    # 常规维度必须仍是 True，不能一锅端
    assert items["get_daily_bars_df"]["routed"] is True


def test_status_endpoint_carries_timeout_count_and_row_unit(client, db_session):
    """`/status` 必须带 `timeout_count`，明细必须带 `row_unit`。"""
    from datetime import datetime, timezone

    run = probe.DataSourceProbeRunDB(
        id="run-timeout",
        started_at=datetime.now(timezone.utc),
        status=probe.STATUS_DEGRADED,
        trigger=probe.TRIGGER_MANUAL,
        trading_day=True,
        total_count=3,
        ok_count=1,
        degraded_count=1,
        timeout_count=1,
        failed_count=0,
        unsupported_count=0,
    )
    db_session.add(run)
    db_session.add(
        probe.DataSourceProbeResultDB(
            id="res-timeout",
            run_id="run-timeout",
            source="cn_akshare",
            tool="get_lhb_detail",
            status=probe.STATUS_TIMEOUT,
            latency_ms=25005,
            row_count=None,
            row_unit=None,
            error_kind="timeout",
        )
    )
    db_session.commit()

    body = client.get("/v1/data-sources/status").json()
    assert body["latest_run"]["timeout_count"] == 1
    # 超时**不能**被算进 failed：否则"慢"和"坏"在告警里又混成一个数
    assert body["latest_run"]["failed_count"] == 0

    row = next(r for r in body["results"] if r["tool"] == "get_lhb_detail")
    assert row["status"] == "timeout"
    assert "row_unit" in row, "明细缺少 row_unit，前端无法判断要不要写 ≈"


def test_history_endpoint_carries_row_unit(client, db_session):
    """`/history` 走的是同一个 result_to_dict，单位也必须带出来。"""
    from datetime import datetime, timezone

    run = probe.DataSourceProbeRunDB(
        id="run-hist",
        started_at=datetime.now(timezone.utc),
        status=probe.STATUS_OK,
        trigger=probe.TRIGGER_MANUAL,
        trading_day=True,
        total_count=1,
        ok_count=1,
    )
    db_session.add(run)
    db_session.add(
        probe.DataSourceProbeResultDB(
            id="res-hist",
            run_id="run-hist",
            source="cn_baostock",
            tool="get_stock_data",
            status=probe.STATUS_OK,
            latency_ms=120,
            row_count=13,
            row_unit=probe.ROW_UNIT_LINES,
            checked_at=datetime.now(timezone.utc),
        )
    )
    db_session.commit()

    points = client.get("/v1/data-sources/history").json()["points"]
    assert points, "history 没有返回任何点"
    assert points[0]["row_unit"] == probe.ROW_UNIT_LINES
    assert points[0]["row_count"] == 13
