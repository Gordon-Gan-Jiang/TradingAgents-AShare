"""L3 任务工具：分钟级能力的**异步协议**。

## 为什么必须异步

分钟的活儿放进同步工具面，会同时坏三件事：
1. MCP 客户端超时后重试 → 同一任务被提交多次；
2. 对话循环卡在工具调用上，用户既没回答也没反馈；
3. 预算记账失真——一次调用吃掉整轮的时间预算。

这正是今天"7 分析师深度管道无法被对话调用"的结构性原因。协议因此固定为：
**submit 立即返回任务号（<1s）→ poll 读状态 → 终态一次性消费**。

## 分类上的一个刻意选择

`submit_backtest_job` 是 L3（异步）；`poll_async_job` 是 **L2**（同步、廉价）——
轮询本身就是一次快速读，把它也标成 L3 会违反"L3 必须异步"这条自定规则。
规则要一致，否则分层就失去意义。
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence

from tradingagents.grounding.types import Coverage, EvidenceItem

from .spec import (
    PLAN_LAYER_EVIDENCE,
    PLAN_LAYER_TASK,
    RESEARCH_DISCLAIMER,
    ToolResult,
    ToolSpec,
)

TASK_CONTRACT = "ASYNC_TASK"
_SOURCE_KEY = "async_task"

# 回测任务的默认参数：区间与持有期保守，避免一次误触就发起巨大运算
DEFAULT_HOLD_DAYS = 5
DEFAULT_SAMPLE_INTERVAL = 5


class TaskPort(Protocol):
    """任务端口。实现由应用层提供（闭包内绑定 user_id）。"""

    def submit_backtest(
        self,
        *,
        symbol: str,
        start_date: str,
        end_date: str,
        hold_days: int,
        sample_interval: int,
    ) -> str: ...

    def job_status(self, *, job_id: str) -> Mapping[str, Any] | None: ...


def _fmt(value: Any) -> str:
    if value in (None, ""):
        return "未提供"
    return str(value)


def make_task_tools(tasks: TaskPort) -> tuple[ToolSpec, ...]:
    def submit_backtest_job(
        *,
        symbol: str,
        start_date: str,
        end_date: str,
        hold_days: int = DEFAULT_HOLD_DAYS,
        sample_interval: int = DEFAULT_SAMPLE_INTERVAL,
        as_of: str = "",
    ) -> ToolResult:
        if not symbol or not start_date or not end_date:
            return ToolResult(
                data={"job_id": None},
                evidence=(),
                coverage=Coverage(available=(), unavailable=(_SOURCE_KEY,)),
                degraded=True,
                degraded_reason="提交回测需要 symbol、start_date、end_date",
                as_of=as_of,
                disclaimer=RESEARCH_DISCLAIMER,
            )
        try:
            job_id = str(
                tasks.submit_backtest(
                    symbol=symbol,
                    start_date=start_date,
                    end_date=end_date,
                    hold_days=int(hold_days or DEFAULT_HOLD_DAYS),
                    sample_interval=int(sample_interval or DEFAULT_SAMPLE_INTERVAL),
                )
            )
        except Exception as exc:  # noqa: BLE001 — 提交失败要如实回报，不抛给模型
            return ToolResult(
                data={"job_id": None, "error": type(exc).__name__},
                evidence=(),
                coverage=Coverage(available=(), unavailable=(_SOURCE_KEY,)),
                degraded=True,
                degraded_reason=f"回测任务提交失败（{type(exc).__name__}）",
                as_of=as_of,
                disclaimer=RESEARCH_DISCLAIMER,
            )

        evidence = (
            EvidenceItem(
                id="E1", field="回测任务号", value=job_id, unit="",
                source_key=_SOURCE_KEY, as_of=as_of,
            ),
            EvidenceItem(
                id="E2", field="回测任务参数",
                value=f"{symbol}｜{start_date} 至 {end_date}｜持有 {hold_days} 日｜每 {sample_interval} 日取样",
                unit="", source_key=_SOURCE_KEY, as_of=as_of,
            ),
            # 这条是给模型的**行为约束**：任务需要分钟级时间，回答不得承诺即时结果
            EvidenceItem(
                id="E3", field="回测任务说明",
                value="任务已在后台执行（分钟级）。请用 poll_async_job 轮询任务号获取进度与结果，"
                      "不得在回答中声称已得到回测结论。",
                unit="", source_key=_SOURCE_KEY, as_of=as_of,
            ),
        )
        return ToolResult(
            data={"job_id": job_id, "status": "pending"},
            evidence=evidence,
            coverage=Coverage(available=(_SOURCE_KEY,), unavailable=()),
            as_of=as_of,
            disclaimer=RESEARCH_DISCLAIMER,
        )

    def poll_async_job(*, job_id: str, as_of: str = "") -> ToolResult:
        if not job_id:
            return ToolResult(
                data={}, evidence=(),
                coverage=Coverage(available=(), unavailable=(_SOURCE_KEY,)),
                degraded=True, degraded_reason="轮询需要 job_id",
                as_of=as_of, disclaimer=RESEARCH_DISCLAIMER,
            )
        try:
            row = tasks.job_status(job_id=str(job_id))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                data={"job_id": job_id, "error": type(exc).__name__},
                evidence=(), coverage=Coverage(available=(), unavailable=(_SOURCE_KEY,)),
                degraded=True, degraded_reason=f"任务状态读取失败（{type(exc).__name__}）",
                as_of=as_of, disclaimer=RESEARCH_DISCLAIMER,
            )
        if not row:
            # 任务号不存在与"任务没跑完"是两件事：前者必须显式说出来
            return ToolResult(
                data={"job_id": job_id, "status": "unknown"},
                evidence=(),
                coverage=Coverage(
                    available=(_SOURCE_KEY,), unavailable=("未找到该任务号",)
                ),
                as_of=as_of, disclaimer=RESEARCH_DISCLAIMER,
            )

        status = str(row.get("status") or "unknown")
        parts = [f"任务 {job_id}", f"状态 {status}"]
        for key in ("progress", "message", "error", "started_at", "finished_at"):
            if row.get(key) not in (None, ""):
                parts.append(f"{key} {_fmt(row.get(key))}")
        evidence = [
            EvidenceItem(
                id="E1", field="任务状态", value="｜".join(parts), unit="",
                source_key=_SOURCE_KEY, as_of=as_of,
            )
        ]
        if status in ("completed", "succeeded") and row.get("result"):
            evidence.append(
                EvidenceItem(
                    id="E2", field="任务结果",
                    value=str(row.get("result"))[:600], unit="",
                    source_key=_SOURCE_KEY, as_of=as_of,
                )
            )

        return ToolResult(
            data={"job_id": job_id, "status": status, "result": row.get("result")},
            evidence=tuple(evidence),
            coverage=Coverage(available=(_SOURCE_KEY,), unavailable=()),
            as_of=as_of,
            disclaimer=RESEARCH_DISCLAIMER,
        )

    submit_spec = ToolSpec(
        name="submit_backtest_job",
        title="提交回测任务（异步）",
        description=(
            "提交一次**分钟级**的回测任务，立即返回任务号。"
            "拿到任务号后用 poll_async_job 轮询进度与结果。"
            "这是异步协议：不得在回答中声称已得到回测结论。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "标的代码，如 600519.SH"},
                "start_date": {"type": "string", "description": "起始日 YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "结束日 YYYY-MM-DD"},
                "hold_days": {"type": "integer", "description": f"持有天数，默认 {DEFAULT_HOLD_DAYS}"},
                "sample_interval": {
                    "type": "integer",
                    "description": f"取样间隔（交易日），默认 {DEFAULT_SAMPLE_INTERVAL}",
                },
                "as_of": {"type": "string", "description": "基准日 YYYY-MM-DD"},
            },
            "required": ["symbol", "start_date", "end_date"],
        },
        output_contract=TASK_CONTRACT,
        handler=submit_backtest_job,
        cost="medium",
        plan_layer=PLAN_LAYER_TASK,
        asynchronous=True,
        # 提交本身要快；真正的分钟级运算在后台
        latency_budget_ms=8000,
        freshness_sources=(_SOURCE_KEY,),
    )

    poll_spec = ToolSpec(
        name="poll_async_job",
        title="轮询任务状态",
        description=(
            "按任务号读取后台任务的当前状态（含进度、错误与完成后的结果）。"
            "用于异步任务的续接。任务号不存在时会显式声明。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "submit_backtest_job 返回的任务号"},
                "as_of": {"type": "string", "description": "基准日 YYYY-MM-DD"},
            },
            "required": ["job_id"],
        },
        output_contract=TASK_CONTRACT,
        handler=poll_async_job,
        cost="cheap",
        # 轮询是快速读 → L2。把它标成 L3 会违反"L3 必须异步"这条自定规则。
        plan_layer=PLAN_LAYER_EVIDENCE,
        latency_budget_ms=5000,
        freshness_sources=(_SOURCE_KEY,),
    )
    return (submit_spec, poll_spec)
