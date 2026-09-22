"""② L3 端到端：真实提交 → `poll_async_job` 查到终态。

## 为什么必须单独验这一条

L3 是**异步协议**：`submit_*` 立即返回任务号，`poll_async_job` 取终态。这条链路
有三处只有真跑才会暴露的断点：

1. **任务号是不是真的排上了队**。`asyncio.create_task` 在没有事件循环的线程里直接
   抛 `RuntimeError`——而"抛错"在这里是最坏的形态：任务号已经发给用户、任务记录
   已经落库，却没有任何东西会跑，用户以为分析在做。
2. **轮询读的是不是同一个任务表**。回测任务在 `backtest_service` 的内存表 + 落库，
   研报/复盘/对比在 `api.main._jobs` 与 `_daily_product_runs`。只查一张表时，
   工具会"拿不到自己刚提交的任务"，模型看到的是"查无此任务"。
3. **`db=None` 时服务自开会话**。对话工具没有 FastAPI 的 `Depends(get_db)`，
   提交入口必须自己开一个会话；这条路径此前只有 1 条单测覆盖。

## 本文件的策略

* 回测路径（不需要事件循环 → 起的是普通线程）用来验**终态**：给一个采样后为空的
  区间，任务走完 `pending → running → completed` 全过程且**真的落终态**。
  这一条不碰模型，因此可以在闸门里稳定运行。
* 研报/复盘/对比路径需要应用主循环，用 `TestClient`（会跑 lifespan）把运行时装起来，
  只验"提交成功 + 轮询读得到同一记录"——跑完整管道要分钟级与模型调用，不作为闸门。
"""

from __future__ import annotations

import time

import pytest


def _registry(user_id: str):
    """生产注册表（工具面与真服务同源）。"""
    from api.services import chat_service

    return chat_service.build_registry(user_id)


def _poll_until_terminal(registry, job_id: str, *, timeout_s: float = 180.0):
    """用 `poll_async_job` 工具本身轮询到终态（不是绕过工具去读内部表）。"""
    spec = registry.get("poll_async_job")
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        last = spec.handler(job_id=job_id, as_of="2026-09-21")
        status = str((last.data or {}).get("status") or "")
        if status in ("completed", "failed", "cancelled", "succeeded"):
            return last, status
        time.sleep(1.0)
    return last, str(((last.data if last else {}) or {}).get("status") or "timeout")


def test_backtest_job_reaches_a_terminal_state():
    """提交 → 轮询 → **终态**（回测路径，不需要事件循环）。"""
    from api.services import chat_service

    user_id = "l3-e2e-user"
    registry = _registry(user_id)
    spec = registry.get("submit_backtest_job")
    assert spec is not None, "submit_backtest_job 必须在册"
    assert spec.asynchronous is True, "L3 必须是异步协议"

    # 区间取**同一天**且采样间隔大于区间长度 → 采样后为空，任务立即走完
    submitted = spec.handler(
        symbol="600519.SH",
        start_date="2026-09-19",
        end_date="2026-09-19",
        hold_days=5,
        sample_interval=30,
        as_of="2026-09-21",
    )
    job_id = str((submitted.data or {}).get("job_id") or "")
    assert job_id, f"提交必须返回任务号，实际 {submitted.data}"

    polled, status = _poll_until_terminal(registry, job_id)
    assert status == "completed", f"未走到终态：{status}｜{polled.data}"

    # 轮询的证据必须可引用（回答层要靠它，不是只看 data）
    assert polled.evidence, "终态必须给出可引用证据"
    assert "completed" in str(polled.evidence[0].value)


def test_poll_reports_unknown_job_id_honestly():
    """查不到的任务号必须**显式说出来**，而不是伪装成"还在跑"。"""
    registry = _registry("l3-e2e-user")
    result = registry.get("poll_async_job").handler(job_id="no-such-job-id", as_of="2026-09-21")

    assert (result.data or {}).get("status") == "unknown"
    assert "未找到该任务号" in result.coverage.unavailable


def test_deep_analysis_submit_without_db_opens_its_own_session(monkeypatch):
    """研报提交（`db=None` 自开会话）必须真的把任务记录下来并可查询。

    这一条覆盖 objective 点名的路径：对话工具没有 FastAPI 的 `Depends(get_db)`，
    提交入口要**自己开会话**；而 HTTP 路由仍把自己那个会话传进来，两条路必须共用
    同一份提交实现（`_submit_deep_analysis`）。

    ## 为什么把运行时打桩

    真实提交会把分钟级分析管道投到应用主循环上跑——那要模型、要网络、要分钟级，
    而且**会污染后续用例**（实测：留下一个在跑的管道后，`test_signals_chat_api`
    与 `test_multiturn_conversation` 的 11 条 HTTP 用例开始失败）。
    所以这里只把 `create_task` 与 `run_analysis_job` 换成记录器：要验的是
    "提交序列 + 自开会话 + 记录可查"，不是管道本身。
    """
    from api.services import analysis_job_service

    dispatched: list[str] = []

    def _fake_create_task(coro, **kw):
        dispatched.append("scheduled")
        # `run_analysis_job` 已被换成普通函数（返回 None），所以 coro 可能不是协程；
        # 能关就关，免得留下未 await 的协程告警。
        close = getattr(coro, "close", None)
        if callable(close):
            close()
        return None

    runtime = analysis_job_service.JobRuntime(
        new_job_id=lambda: "e2e-deep-job",
        now_iso=lambda: "2026-09-21T00:00:00+00:00",
        set_job=analysis_job_service.default_job_runtime().set_job,
        get_job=analysis_job_service.default_job_runtime().get_job,
        ensure_event_queue=lambda job_id: None,
        emit_event=lambda job_id, kind, payload: None,
        create_task=_fake_create_task,
        run_analysis_job=lambda *a, **kw: None,
        set_daily_run=lambda **kw: None,
        finalize_daily_run=None,
    )

    submitted = analysis_job_service.submit_deep_analysis_task(
        user_id="l3-e2e-user", symbol="600519.SH", runtime=runtime
    )

    assert submitted.job_id == "e2e-deep-job"
    assert dispatched == ["scheduled"], "提交必须真的把任务投出去"
    # `db=None` → 服务自开会话；记录写进去了才查得到
    row = analysis_job_service.default_job_runtime().get_job("e2e-deep-job")
    assert row, "自开会话写入的任务记录查不到——db=None 那条路径没有真的落库"


def test_deep_analysis_tool_reports_submission_failure_honestly(monkeypatch):
    """提交链路抛错时，工具要如实回报而不是声称"已在后台执行"。"""
    from api.services import analysis_job_service

    def _boom(*a, **kw):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(analysis_job_service, "submit_deep_analysis_task", _boom)

    registry = _registry("l3-e2e-user")
    result = registry.get("submit_deep_analysis_job").handler(
        symbol="600519.SH", as_of="2026-09-21"
    )

    assert result.degraded is True
    assert "提交失败" in str(result.degraded_reason)
    assert not (result.data or {}).get("job_id")


def test_poll_surfaces_that_every_sampled_date_failed(monkeypatch):
    """轮询必须让用户看出"跑完了但每一天都出错"，而不是只看到 `completed`。

    ## 实测

    真实提交一次回测（1 个交易日），5 秒后 `poll_async_job` 给出终态：

        任务 91058cb0…｜状态 completed｜started_at …｜finished_at …

    而 `_run_backtest` 对每个交易日的异常是**逐条捕获**进 `records[i]["error"]` 的：
    管道因为拿不到可用模型 Key 立刻抛错，任务仍然报 `completed`。用户看到"已完成"
    却什么都没有——这正是"跑完了 ≠ 跑成功"这一类诚实性问题。

    终态是异步协议的终点，它必须**说明这一趟到底产出了什么**。修法不去改 `status`
    的既有语义（前端与其它调用方都读它），而是让端口把进度与错误数放进 `message`
    ——`poll_async_job` 本来就会把它渲染进证据。
    """
    from api.services import backtest_service, chat_service

    monkeypatch.setattr(
        backtest_service,
        "get_job",
        lambda job_id: {
            "job_id": job_id,
            "status": "completed",
            "started_at": "2026-09-22T04:16:02+00:00",
            "finished_at": "2026-09-22T04:16:03+00:00",
            "total_dates": 1,
            "completed_dates": 1,
            "records": [{"date": "2026-09-18", "action": "HOLD", "error": "AuthenticationError: no key"}],
            "stats": {"total_signals": 0},
            "error": None,
        },
    )

    registry = chat_service.build_registry("l3-e2e-user")
    polled = registry.get("poll_async_job").handler(job_id="bt-1", as_of="2026-09-21")

    text = "｜".join(str(item.value) for item in polled.evidence)
    assert "completed" in text
    assert "出错" in text, f"没说出「有一天出错」：{text}"
