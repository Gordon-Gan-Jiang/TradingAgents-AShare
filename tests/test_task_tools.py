"""L3 异步任务协议：submit 立即返回任务号，poll 读状态。

## 本文件锁住什么

1. **L3 必须异步**（构造期强制）：同步的分钟级工具 = 客户端必然超时。
2. **submit 不得等待结果**：只返回任务号与参数，并**明确告诉模型不得声称已有结论**
   ——这是防"编造已完成"的关键一条。
3. **轮询是 L2**：它是一次快速读，标成 L3 会违反"L3 必须异步"这条自定规则。
4. **任务号不存在 ≠ 任务未完成**：必须显式声明，否则模型会把"查不到"当成"还在跑"。
5. 提交失败要如实回报（含原因类型），不抛错、不外传上游原文。
"""

from __future__ import annotations

import pytest


class _FakeTasks:
    def __init__(self, *, job_id="job-1", status=None, error: Exception | None = None):
        self._job_id = job_id
        self._status = status
        self._error = error
        self.submits: list[dict] = []
        self.polls: list[str] = []

    def submit_backtest(self, *, symbol, start_date, end_date, hold_days, sample_interval):
        self.submits.append({
            "symbol": symbol, "start_date": start_date, "end_date": end_date,
            "hold_days": hold_days, "sample_interval": sample_interval,
        })
        if self._error is not None:
            raise self._error
        return self._job_id

    def job_status(self, *, job_id):
        self.polls.append(job_id)
        if self._error is not None:
            raise self._error
        return self._status


def _tools(port):
    from tradingagents.tools.tasks import make_task_tools

    return {spec.name: spec for spec in make_task_tools(port)}


def test_submit_returns_job_id_without_waiting():
    port = _FakeTasks(job_id="abc123")
    spec = _tools(port)["submit_backtest_job"]
    result = spec.handler(symbol="600519.SH", start_date="2026-01-01", end_date="2026-06-30",
                          as_of="2026-09-21")

    assert spec.asynchronous is True, "分钟级能力必须异步"
    assert result.data["job_id"] == "abc123"
    assert result.data["status"] == "pending"


def test_submit_evidence_forbids_claiming_results():
    """必须明确约束模型：不得声称已得到回测结论。"""
    port = _FakeTasks()
    spec = _tools(port)["submit_backtest_job"]
    result = spec.handler(symbol="600519.SH", start_date="2026-01-01", end_date="2026-06-30")

    joined = " ".join(str(item.value) for item in result.evidence)
    assert "poll_async_job" in joined, "要告诉模型怎么续接"
    assert "不得" in joined, "要明确禁止声称已有结论——这是防编造的关键一条"


def test_submit_requires_dates():
    spec = _tools(_FakeTasks())["submit_backtest_job"]
    result = spec.handler(symbol="600519.SH", start_date="", end_date="")
    assert result.degraded is True
    assert result.coverage.unavailable == ("async_task",)


def test_submit_failure_degrades_without_leaking_detail():
    spec = _tools(_FakeTasks(error=RuntimeError("queue full: internal detail")))[
        "submit_backtest_job"
    ]
    result = spec.handler(symbol="600519.SH", start_date="2026-01-01", end_date="2026-06-30")
    assert result.degraded is True
    assert "internal detail" not in str(result.data)


def test_poll_reports_status_and_result():
    port = _FakeTasks(status={"status": "completed", "progress": 100, "result": "命中率 62%"})
    spec = _tools(port)["poll_async_job"]
    result = spec.handler(job_id="abc123", as_of="2026-09-21")

    joined = " ".join(str(item.value) for item in result.evidence)
    assert "completed" in joined
    assert "命中率 62%" in joined, "完成后结果必须进证据，否则回答没有依据"


def test_poll_unknown_job_is_declared():
    """任务号不存在 ≠ 任务未完成：必须说清，否则会被当成"还在跑"。"""
    spec = _tools(_FakeTasks(status=None))["poll_async_job"]
    result = spec.handler(job_id="ghost", as_of="2026-09-21")

    assert result.evidence == ()
    assert any("未找到" in item for item in result.coverage.unavailable)


def test_poll_is_l2_while_submit_is_l3():
    from tradingagents.tools.spec import PLAN_LAYER_EVIDENCE, PLAN_LAYER_TASK

    tools = _tools(_FakeTasks())
    assert tools["submit_backtest_job"].plan_layer == PLAN_LAYER_TASK
    assert tools["poll_async_job"].plan_layer == PLAN_LAYER_EVIDENCE, (
        "轮询是快速读，标成 L3 会违反「L3 必须异步」这条自定规则"
    )


def test_task_tools_are_registered_with_a_port():
    from tradingagents.tools import build_registry
    from tradingagents.tools.spec import PLAN_LAYER_TASK

    without = build_registry(ports_factory=lambda *a: object(), news_fetcher=lambda *a: [])
    assert "submit_backtest_job" not in without.names()

    with_port = build_registry(
        ports_factory=lambda *a: object(), news_fetcher=lambda *a: [],
        task_port=_FakeTasks(),
    )
    assert "submit_backtest_job" in with_port.names_by_layer(PLAN_LAYER_TASK)
