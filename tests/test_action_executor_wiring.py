"""动作执行器接线：把「提议 → 确认」真正接上可执行的服务。

## 这个测试锁住什么

1. **生产装配里真的传了执行器**：`loop.py` 的确认分支要求显式传入
   `action_executor`，而生产代码此前从未传过——于是用户回复「确认」只会得到
   「当前环境未配置动作执行器，操作未执行」。提议→确认这条链路**在生产里从来
   没有执行过**，只在测试里被走通过。这条断言把它钉住。
2. **能执行的动作真的执行了**：`watchlist_add` 落到 `watchlist_service`（真库）。
3. **不能执行的动作如实拒绝，而不是静默什么都不做**：平台没有"用户自定义阈值
   预警"的服务口径，深度分析入口也只在路由内联代码里。宁可给一句明确的
   "未执行"，也不硬造一张没人消费的预警表。
4. 执行器绑定 user_id（不从提议里取用户），且需要已绑定用户时才允许执行。
"""

from __future__ import annotations

import pytest


def test_run_turn_receives_an_action_executor():
    """装配守卫：`chat_service` 必须把执行器传给 run_turn。

    只看源码文本是弱的，但这一条守的正是"接线漏了"这类静默失败——调用点少传
    一个可选参数不会有任何测试变红，代价是确认功能永久失效。
    """
    import inspect

    from api.services import chat_service

    source = inspect.getsource(chat_service)
    assert "action_executor=build_action_executor(user_id)" in source, (
        "run_turn 必须显式收到绑定用户的动作执行器"
    )


def test_watchlist_add_executes_through_the_existing_service():
    from api.database import get_db_ctx, WatchlistItemDB
    from api.services import chat_service

    executor = chat_service.build_action_executor("action-user")
    summary = executor(
        {"kind": "watchlist_add", "params": {"symbol": "600519.SH"}}, "action-user"
    )

    assert "600519.SH" in summary
    with get_db_ctx() as db:
        rows = (
            db.query(WatchlistItemDB)
            .filter(WatchlistItemDB.user_id == "action-user")
            .all()
        )
    assert [row.symbol for row in rows] == ["600519.SH"], "确认后必须真的写进自选"


def test_duplicate_watchlist_add_reports_failure_not_silence():
    from api.services import chat_service

    executor = chat_service.build_action_executor("dup-user")
    executor({"kind": "watchlist_add", "params": {"symbol": "600519.SH"}}, "dup-user")
    with pytest.raises(Exception) as excinfo:
        executor({"kind": "watchlist_add", "params": {"symbol": "600519.SH"}}, "dup-user")
    assert "已在自选列表" in str(excinfo.value)


def test_actions_without_a_service_entry_are_refused_with_a_reason():
    """没有服务入口的动作必须**明确拒绝并说明原因**，不得静默当作成功。"""
    from api.services import chat_service

    executor = chat_service.build_action_executor("u-1")

    with pytest.raises(NotImplementedError) as alert:
        executor(
            {"kind": "price_alert",
             "params": {"symbol": "600519.SH", "threshold": 180.0, "direction": "below"}},
            "u-1",
        )
    assert "未执行" in str(alert.value)
    assert "离场计划" in str(alert.value), "必须说清为什么不能执行"

    with pytest.raises(NotImplementedError) as deep:
        executor({"kind": "deep_analysis", "params": {"symbol": "600519.SH"}}, "u-1")
    assert "未执行" in str(deep.value)


def test_unknown_kind_and_missing_symbol_are_refused():
    from api.services import chat_service

    executor = chat_service.build_action_executor("u-1")
    with pytest.raises(ValueError):
        executor({"kind": "unknown_kind", "params": {}}, "u-1")
    with pytest.raises(ValueError):
        executor({"kind": "watchlist_add", "params": {}}, "u-1")


def test_executor_accepts_the_object_form_and_requires_a_bound_user():
    """loop 传下来的可能是 dict（`as_event_payload`），对象形式也要能用。"""
    from tradingagents.agent.actions import ActionProposal

    from api.services import chat_service

    executor = chat_service.build_action_executor("obj-user")
    summary = executor(
        ActionProposal(kind="watchlist_add", summary="加自选", params={"symbol": "000001.SZ"}),
        "obj-user",
    )
    assert "000001.SZ" in summary

    unbound = chat_service.build_action_executor(None)
    with pytest.raises(LookupError):
        unbound({"kind": "watchlist_add", "params": {"symbol": "600519.SH"}}, "")


def test_surface_gate_keeps_external_surfaces_read_only():
    from api.services import chat_service

    assert chat_service.can_execute_actions_for_surface("product_chat") is True
    assert chat_service.can_execute_actions_for_surface("mcp") is False
