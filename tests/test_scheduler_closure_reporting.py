"""P2b：闭环刷新的结果**不能丢掉**。

## 缺陷

调度器（`api/main.py::_mainline_scheduler_loop`）里：

    mainline_service.refresh_t1_outcomes(db, user_id="system")   # ← 返回值被丢弃

`refresh_t1_outcomes` 明明返回了 `{"evaluated_reports": n, "skipped": [...]}`，
其中 `skipped[].reason` 正是"为什么这份报告没算出来"的答案——
**算出来了，又被扔掉了**。于是基准缓存坏了两周多，运维侧一个字都看不到。

这与工具层已经确立的原则不一致：那边要求失败必须降级可见
（`degraded` / `unavailable` 都要显式声明），而闭环这边连已经算出的原因都丢。

## 为什么抽成独立函数

原调用点在 `while True` 的异步循环体里，无法直接单测。
抽出 `_refresh_mainline_t1_and_report()` 既修了缺陷，也让它可以被测试——
"测不了的代码"正是这个洞能长期存在的原因之一。
"""
from __future__ import annotations

import logging

import pytest

from api import main as api_main


class _FakeDb:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch_db(monkeypatch):
    monkeypatch.setattr(api_main, "get_db_ctx", lambda: _FakeDb())


def test_reports_zero_evaluation_with_reason(monkeypatch, caplog):
    """有报告但一份都没算出来时，**必须**把原因记下来。"""
    _patch_db(monkeypatch)
    monkeypatch.setattr(
        api_main.mainline_service,
        "refresh_t1_outcomes",
        lambda db, user_id=None: {
            "evaluated_reports": 0,
            "skipped": [
                {"id": "r1", "reason": "MainlineDataUnavailable: 基准数据陈旧：末日 2026-08-31 早于报告日 2026-09-10"},
            ],
        },
    )

    with caplog.at_level(logging.INFO):
        result = api_main._refresh_mainline_t1_and_report()

    assert result["evaluated_reports"] == 0
    joined = caplog.text
    assert "基准数据陈旧" in joined or "2026-08-31" in joined, (
        f"刷新失败的原因没有出现在日志里，运维看不到：{joined!r}"
    )


def test_reports_success_count(monkeypatch, caplog):
    """正常情况下要报出评估了几份，便于确认闭环真的在动。"""
    _patch_db(monkeypatch)
    monkeypatch.setattr(
        api_main.mainline_service,
        "refresh_t1_outcomes",
        lambda db, user_id=None: {"evaluated_reports": 7, "skipped": []},
    )

    with caplog.at_level(logging.INFO):
        result = api_main._refresh_mainline_t1_and_report()

    assert result["evaluated_reports"] == 7
    assert "7" in caplog.text, f"评估条数没有出现在日志里：{caplog.text!r}"


def test_skipped_reasons_are_bounded(monkeypatch, caplog):
    """原因很多时不能把日志刷爆——要有条数上限并说明被截断。"""
    _patch_db(monkeypatch)
    monkeypatch.setattr(
        api_main.mainline_service,
        "refresh_t1_outcomes",
        lambda db, user_id=None: {
            "evaluated_reports": 0,
            "skipped": [{"id": f"r{i}", "reason": "no forward data yet"} for i in range(50)],
        },
    )

    with caplog.at_level(logging.INFO):
        api_main._refresh_mainline_t1_and_report()

    assert "50" in caplog.text, "被跳过的总数必须报出来"


def test_refresh_exception_is_logged_and_swallowed(monkeypatch, caplog):
    """刷新本身抛异常时：记日志、不往上抛（调度循环必须活着）。"""
    _patch_db(monkeypatch)

    def boom(db, user_id=None):
        raise RuntimeError("数据库锁死")

    monkeypatch.setattr(api_main.mainline_service, "refresh_t1_outcomes", boom)

    with caplog.at_level(logging.INFO):
        result = api_main._refresh_mainline_t1_and_report()

    assert "数据库锁死" in caplog.text
    assert result is None or result.get("evaluated_reports", 0) == 0


def test_scheduler_loop_uses_the_reporting_helper():
    """回归护栏：调度循环里不许再出现"调用后丢弃返回值"的写法。"""
    import inspect

    src = inspect.getsource(api_main._mainline_scheduler_loop)
    assert "_refresh_mainline_t1_and_report" in src, (
        "调度循环没有走带日志的封装，返回值又会被丢掉"
    )
