"""探测**调度**的行为测试：tick / loop / lifespan 三件事真的会发生。

## 为什么单独一个文件

审计发现这里的验证是假的：`_data_source_probe_tick`（真正干活的函数）**一次都没被
执行过**，而"已经接线"的证据是把 `api/main.py` 当**字符串**读、断言
`"_data_source_probe_loop" in main_src`——循环体写成 `pass` 那个测试照样绿。

所以这里改成行为验证：
* tick：该跑就跑、不该跑就不跑、失败要告警、会话一定要关；
* loop：tick 抛异常后循环**必须继续**（否则一次网络抖动就永久停掉探测）；
* lifespan：进入 `TestClient(app)` 后探测任务**真的被创建**、退出时**真的被取消**。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from api.services import data_source_probe_service as probe
from api.services.data_source_probe_service import TZ_CN


# --------------------------------------------------------------- 替身


class _FakeSession:
    """最小 Session 替身：只关心"有没有被关掉"。"""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeSummary:
    def __init__(self, *, failed: int = 0, outcomes: list | None = None) -> None:
        self.status = "ok" if not failed else "failed"
        self.ok_count = 1
        self.degraded_count = 0
        self.failed_count = failed
        self.unsupported_count = 0
        self.duration_ms = 12
        self.outcomes = outcomes or []


class _FakeOutcome:
    def __init__(self, source: str, tool: str, status: str, error_kind: str) -> None:
        self.source = source
        self.tool = tool
        self.status = status
        self.error_kind = error_kind


@pytest.fixture
def fake_session(monkeypatch):
    """把 tick 里的 `SessionLocal()` 换成替身，并记录它。"""
    import api.database as database

    session = _FakeSession()
    monkeypatch.setattr(database, "SessionLocal", lambda: session)
    return session


# ----------------------------------------------------------------- tick


def test_tick_does_not_run_probe_when_not_due(monkeypatch, fake_session):
    """不该跑的时候**一次都不能跑**——09:00 之前空跑会污染报告。"""
    from api import main as api_main

    calls: list[str] = []
    monkeypatch.setattr(
        probe, "should_run_scheduled_probe", lambda db, **kw: (False, "before-scheduled-time")
    )
    monkeypatch.setattr(probe, "run_probe", lambda *a, **kw: calls.append("ran"))

    api_main._data_source_probe_tick()

    assert calls == [], "还没到点就跑了探测"
    assert fake_session.closed, "提前返回也必须关掉会话"


def test_tick_runs_probe_with_the_scheduled_trigger(monkeypatch, fake_session):
    """该跑的时候，必须用 `scheduled` 触发器（报告要能区分谁触发的）。"""
    from api import main as api_main

    seen: list[dict] = []
    monkeypatch.setattr(probe, "should_run_scheduled_probe", lambda db, **kw: (True, "due"))

    def _run(db, *, trigger=None, **kw):
        seen.append({"trigger": trigger, "db": db})
        return _FakeSummary()

    monkeypatch.setattr(probe, "run_probe", _run)

    api_main._data_source_probe_tick()

    assert len(seen) == 1, "该跑没跑"
    assert seen[0]["trigger"] == probe.TRIGGER_SCHEDULED
    assert fake_session.closed


def test_tick_warns_when_sources_fail(monkeypatch, fake_session, caplog):
    """有源失败必须**留下 warning**。

    本次事故的教训就是"某源不可用"从来没有浮出水面；如果定时探测失败也只是
    debug 日志，那等于又埋回去了。
    """
    from api import main as api_main

    monkeypatch.setattr(probe, "should_run_scheduled_probe", lambda db, **kw: (True, "due"))
    monkeypatch.setattr(
        probe,
        "run_probe",
        lambda *a, **kw: _FakeSummary(
            failed=2,
            outcomes=[
                _FakeOutcome("cn_akshare", "get_daily_bars_df", probe.STATUS_FAILED, "error"),
                _FakeOutcome("cn_baostock", "get_news", probe.STATUS_FAILED, "timeout"),
            ],
        ),
    )

    with caplog.at_level(logging.WARNING):
        api_main._data_source_probe_tick()

    assert any(
        "cn_akshare.get_daily_bars_df" in r.getMessage() and r.levelno >= logging.WARNING
        for r in caplog.records
    ), f"失败没有被 warning 出来：{[r.getMessage() for r in caplog.records]}"


def test_tick_does_not_warn_when_all_sources_ok(monkeypatch, fake_session, caplog):
    """全好就别报警——否则每天一条噪声，很快就没人看了。"""
    from api import main as api_main

    monkeypatch.setattr(probe, "should_run_scheduled_probe", lambda db, **kw: (True, "due"))
    monkeypatch.setattr(probe, "run_probe", lambda *a, **kw: _FakeSummary())

    with caplog.at_level(logging.WARNING):
        api_main._data_source_probe_tick()

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_tick_closes_the_session_even_when_probe_explodes(monkeypatch, fake_session):
    """探测抛异常时会话**仍必须关闭**——否则连接池迟早被耗干。"""
    from api import main as api_main

    monkeypatch.setattr(probe, "should_run_scheduled_probe", lambda db, **kw: (True, "due"))

    def _boom(*a, **kw):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(probe, "run_probe", _boom)

    with pytest.raises(RuntimeError):
        api_main._data_source_probe_tick()

    assert fake_session.closed, "异常路径泄漏了数据库会话"


# ----------------------------------------------------------------- loop


def test_loop_survives_a_tick_failure(monkeypatch):
    """核心回归点：tick 抛异常后循环**必须继续**。

    循环里 `await asyncio.sleep()` 在 try 之外、tick 在 try 之内，正是为了让
    单次抖动不至于永久停掉探测。这条测试在旧版（只有字符串断言）下是不存在的，
    把 `try/except` 删掉也不会有任何测试变红。
    """
    from api import main as api_main

    ticks: list[int] = []

    def _flaky_tick() -> None:
        ticks.append(len(ticks))
        if len(ticks) == 1:
            raise RuntimeError("first tick dies")

    monkeypatch.setattr(api_main, "_data_source_probe_tick", _flaky_tick)
    monkeypatch.setattr(probe, "probe_interval_seconds", lambda: 0.01)

    async def _scenario() -> list[int]:
        task = asyncio.create_task(api_main._data_source_probe_loop())
        deadline = time.monotonic() + 5.0
        # 等到至少 3 次 tick（第 1 次是炸掉的那次）
        while len(ticks) < 3 and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return ticks

    ticks_seen = asyncio.run(_scenario())

    assert len(ticks_seen) >= 3, (
        f"第一次 tick 抛异常后循环停了（只跑了 {len(ticks_seen)} 次）——"
        "一次网络抖动就会永久停掉探测"
    )


# -------------------------------------------------------------- lifespan


def test_lifespan_actually_starts_and_stops_the_probe_loop(monkeypatch):
    """进入 app 生命周期必须**真的启动**探测循环，退出时必须**取消**它。

    这条替掉了原来读 main.py 源码做子串断言的写法：那种写法在循环体是 `pass`
    甚至任务创建后被立刻丢弃时也会通过，证明不了任何行为。
    """
    started = asyncio.Event()
    cancelled = {"value": False}

    async def _fake_loop() -> None:
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled["value"] = True
            raise

    from api import main as api_main
    from api.main import app

    monkeypatch.setattr(api_main, "_data_source_probe_loop", _fake_loop)

    with patch("api.main._load_cn_stock_map", return_value={}):
        with TestClient(app) as client:
            assert client.get("/healthz").status_code == 200
            assert started.is_set(), "进入 lifespan 后探测循环没有被启动"

    assert cancelled["value"], "退出 lifespan 时探测循环没有被取消（会泄漏任务）"


def test_probe_endpoints_are_really_mounted():
    """端点必须真的挂在 app 上（而不是只在源码里出现过字符串）。"""
    from api.main import app

    paths = {getattr(r, "path", None) for r in app.routes}
    for expected in (
        "/v1/data-sources/matrix",
        "/v1/data-sources/status",
        "/v1/data-sources/history",
        "/v1/data-sources/probe",
    ):
        assert expected in paths, f"{expected} 没有挂到 app 上；实际：{sorted(p for p in paths if p)}"
