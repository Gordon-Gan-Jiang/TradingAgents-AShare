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

# 其余三个分钟级能力的默认参数。全部经 `_clamp_int` 显式收敛：
# `x or default` 会把**合法值 0** 换成默认值（本仓库已踩过四次），
# 因此数值参数一律"None 才是未指定"，0 会被 clamp 到区间下限。
DEFAULT_DAILY_TOP_K = 3
DAILY_TOP_K_RANGE = (1, 20)
DAILY_MODES = ("recommended", "watchlist", "tracking", "all")
DEFAULT_HORIZONS = ("short",)
ALLOWED_HORIZONS = ("short", "medium")
DEFAULT_ANALYSTS = ("market", "social", "news", "fundamentals", "macro", "smart_money")


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


class AnalysisTaskPort(Protocol):
    """其余三个分钟级能力的提交端口（全文研报 / 每日复盘 / 模型对比）。

    实现由应用层提供（`api/services/chat_service.build_registry` 的闭包里绑定
    `user_id`——给模型一个 user_id 参数就等于给它越权的可能）。

    **未实现的方法对应的工具不注册**：留一个"在册但一调就失败"的条目，只会让 Agent
    把预算浪费在必然失败的调用上（`build_registry` 的既有纪律）。

    返回值一律是 `Mapping`，键就是证据要用的字段：

    * `submit_deep_analysis` → `{"job_id": str}`
    * `submit_daily_review` → `{"run_id": str, "status": str, "queued_jobs": int, "job_ids": [...]}`
    * `submit_model_arena` → `{"experiment_id": str, "input_snapshot_hash": str, "job_ids": [...]}`
    """

    def submit_deep_analysis(
        self,
        *,
        symbol: str,
        trade_date: str,
        horizons: Sequence[str],
        selected_analysts: Sequence[str],
        query: str,
    ) -> Mapping[str, Any]: ...

    def submit_daily_review(
        self,
        *,
        mode: str,
        top_k: int,
        market: str,
        horizons: Sequence[str],
        selected_analysts: Sequence[str],
    ) -> Mapping[str, Any]: ...

    def submit_model_arena(
        self,
        *,
        symbol: str,
        trade_date: str,
        horizons: Sequence[str],
        selected_analysts: Sequence[str],
        model_profile_ids: Sequence[str],
        query: str,
    ) -> Mapping[str, Any]: ...


def _fmt(value: Any) -> str:
    if value in (None, ""):
        return "未提供"
    return str(value)


def _clamp_int(value: Any, *, default: int, lo: int, hi: int) -> int:
    """显式收敛数值参数。

    `value is None` 才回落到默认值；`0`、`""`、`False` 都是**调用方给的值**，
    由区间收敛决定最终取值。写成 `x or default` 会让 0 静默变成默认值——
    本仓库已经因为这一条踩过四次坑，所以这里不给"顺手写短一点"的机会。
    """
    if value is None:
        return int(default)
    try:
        number = int(value)
    except (TypeError, ValueError):
        return int(default)
    return max(lo, min(hi, number))


def _clean_choice_list(
    value: Any, *, allowed: Sequence[str], default: Sequence[str]
) -> list[str]:
    """把字符串列表收敛到允许集合。

    空列表 = 调用方没指定 → 用声明过的默认值（对**集合**语义而言"空"就是"没给"，
    与数值 0 的情况不同；数值参数见 `_clamp_int` 的注释）。
    """
    allow = {str(item) for item in allowed}
    items: list[str] = []
    for raw in value or []:
        text = str(raw or "").strip()
        if text and text in allow and text not in items:
            items.append(text)
    return items or [str(item) for item in default]


def _clean_text_list(value: Any, *, default: Sequence[str]) -> list[str]:
    items: list[str] = []
    for raw in value or []:
        text = str(raw or "").strip()
        if text and text not in items:
            items.append(text)
    return items or [str(item) for item in default]


def _submit_degraded(*, reason: str, as_of: str, error_type: str | None = None) -> ToolResult:
    """提交失败：如实降级，**不外传上游原文**（只给原因类型）。"""
    return ToolResult(
        data={"job_id": None, **({"error": error_type} if error_type else {})},
        evidence=(),
        coverage=Coverage(available=(), unavailable=(_SOURCE_KEY,)),
        degraded=True,
        degraded_reason=reason,
        as_of=as_of,
        disclaimer=RESEARCH_DISCLAIMER,
    )


def _async_task_note(what: str, how_to_continue: str) -> str:
    """给模型的**行为约束**：任务在后台跑，回答里不得声称已有结论。

    这是防"编造已完成"的关键一条：分钟级管道不可能在这一次工具调用里出结果，
    若不说清，模型最容易顺手写一句"根据回测结果……"。
    """
    return (
        f"{what}已在后台执行（分钟级）。请用 poll_async_job 轮询{how_to_continue}获取进度与结果，"
        "不得在回答中声称已得到结论。"
    )


def _supports(port: Any, method: str) -> bool:
    return callable(getattr(port, method, None))


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
                    # 显式 clamp：`0` 是调用方给的值，不能被 falsy 替换成默认值
                    hold_days=_clamp_int(hold_days, default=DEFAULT_HOLD_DAYS, lo=1, hi=250),
                    sample_interval=_clamp_int(
                        sample_interval, default=DEFAULT_SAMPLE_INTERVAL, lo=1, hi=60
                    ),
                )
            )
        except Exception as exc:  # noqa: BLE001 — 提交失败要如实回报，不抛给模型
            return _submit_degraded(
                reason=f"回测任务提交失败（{type(exc).__name__}）",
                as_of=as_of,
                error_type=type(exc).__name__,
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

    specs: list[ToolSpec] = [submit_spec, poll_spec]

    # ── 全文研报生成 ────────────────────────────────────────────────────
    def submit_deep_analysis_job(
        *,
        symbol: str,
        trade_date: str = "",
        horizons: Sequence[str] | None = None,
        selected_analysts: Sequence[str] | None = None,
        query: str = "",
        as_of: str = "",
    ) -> ToolResult:
        if not symbol:
            return _submit_degraded(reason="提交深度分析需要 symbol", as_of=as_of)
        eff_horizons = _clean_choice_list(
            horizons, allowed=ALLOWED_HORIZONS, default=DEFAULT_HORIZONS
        )
        eff_analysts = _clean_text_list(selected_analysts, default=DEFAULT_ANALYSTS)
        try:
            payload = dict(
                tasks.submit_deep_analysis(
                    symbol=str(symbol),
                    trade_date=str(trade_date or ""),
                    horizons=eff_horizons,
                    selected_analysts=eff_analysts,
                    query=str(query or ""),
                )
            )
        except Exception as exc:  # noqa: BLE001 — 失败如实回报，抛给模型只会毁掉整轮
            return _submit_degraded(
                reason=f"深度分析任务提交失败（{type(exc).__name__}）",
                as_of=as_of,
                error_type=type(exc).__name__,
            )
        job_id = str(payload.get("job_id") or "")
        if not job_id:
            return _submit_degraded(reason="深度分析任务提交失败（未返回任务号）", as_of=as_of)
        evidence = (
            EvidenceItem(
                id="E1", field="深度分析任务号", value=job_id, unit="",
                source_key=_SOURCE_KEY, as_of=as_of,
            ),
            EvidenceItem(
                id="E2", field="深度分析任务参数",
                value=(
                    f"{symbol}｜交易日 {trade_date or '默认最近交易日'}｜"
                    f"周期 {'/'.join(eff_horizons)}｜分析师 {'/'.join(eff_analysts)}"
                ),
                unit="", source_key=_SOURCE_KEY, as_of=as_of,
            ),
            EvidenceItem(
                id="E3", field="深度分析任务说明",
                value=_async_task_note("全文研报生成（多分析师管道）", "该任务号"),
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

    # ── 每日复盘（全市场批量分析） ───────────────────────────────────────
    def submit_daily_review_job(
        *,
        mode: str = "recommended",
        top_k: int = DEFAULT_DAILY_TOP_K,
        market: str = "cn",
        horizons: Sequence[str] | None = None,
        selected_analysts: Sequence[str] | None = None,
        as_of: str = "",
    ) -> ToolResult:
        eff_mode = str(mode or "").strip() or "recommended"
        if eff_mode not in DAILY_MODES:
            return _submit_degraded(
                reason=f"提交每日复盘需要 mode ∈ {DAILY_MODES}", as_of=as_of
            )
        eff_top_k = _clamp_int(
            top_k, default=DEFAULT_DAILY_TOP_K, lo=DAILY_TOP_K_RANGE[0], hi=DAILY_TOP_K_RANGE[1]
        )
        eff_market = str(market or "").strip() or "cn"
        eff_horizons = _clean_choice_list(
            horizons, allowed=ALLOWED_HORIZONS, default=DEFAULT_HORIZONS
        )
        eff_analysts = _clean_text_list(selected_analysts, default=DEFAULT_ANALYSTS)
        try:
            payload = dict(
                tasks.submit_daily_review(
                    mode=eff_mode,
                    top_k=eff_top_k,
                    market=eff_market,
                    horizons=eff_horizons,
                    selected_analysts=eff_analysts,
                )
            )
        except Exception as exc:  # noqa: BLE001
            return _submit_degraded(
                reason=f"每日复盘任务提交失败（{type(exc).__name__}）",
                as_of=as_of,
                error_type=type(exc).__name__,
            )
        run_id = str(payload.get("run_id") or "")
        if not run_id:
            return _submit_degraded(reason="每日复盘任务提交失败（未返回批跑号）", as_of=as_of)
        queued = payload.get("queued_jobs")
        job_ids = [str(j) for j in (payload.get("job_ids") or []) if str(j or "")]
        evidence = [
            EvidenceItem(
                id="E1", field="每日复盘批跑号", value=run_id, unit="",
                source_key=_SOURCE_KEY, as_of=as_of,
            ),
            EvidenceItem(
                id="E2", field="每日复盘参数",
                value=f"模式 {eff_mode}｜标的数 {eff_top_k}｜市场 {eff_market}｜周期 {'/'.join(eff_horizons)}",
                unit="", source_key=_SOURCE_KEY, as_of=as_of,
            ),
            EvidenceItem(
                id="E3", field="每日复盘任务说明",
                value=_async_task_note(
                    f"每日复盘（已排入 {queued if queued is not None else '未知'} 个分析任务）",
                    "该批跑号与各标的任务号",
                ),
                unit="", source_key=_SOURCE_KEY, as_of=as_of,
            ),
        ]
        if job_ids:
            evidence.append(
                EvidenceItem(
                    id="E4", field="本批各标的任务号",
                    value="、".join(job_ids[:10]), unit="",
                    source_key=_SOURCE_KEY, as_of=as_of,
                )
            )
        return ToolResult(
            data={
                "run_id": run_id,
                "status": str(payload.get("status") or "pending"),
                "queued_jobs": queued,
                "job_ids": job_ids,
            },
            evidence=tuple(evidence),
            coverage=Coverage(available=(_SOURCE_KEY,), unavailable=()),
            as_of=as_of,
            disclaimer=RESEARCH_DISCLAIMER,
        )

    # ── 模型对比 ────────────────────────────────────────────────────────
    def submit_model_arena_job(
        *,
        symbol: str,
        trade_date: str = "",
        horizons: Sequence[str] | None = None,
        selected_analysts: Sequence[str] | None = None,
        model_profile_ids: Sequence[str] | None = None,
        query: str = "",
        as_of: str = "",
    ) -> ToolResult:
        if not symbol:
            return _submit_degraded(reason="提交模型对比需要 symbol", as_of=as_of)
        eff_horizons = _clean_choice_list(
            horizons, allowed=ALLOWED_HORIZONS, default=DEFAULT_HORIZONS
        )
        eff_analysts = _clean_text_list(selected_analysts, default=DEFAULT_ANALYSTS)
        # 空列表 = 用该用户**全部启用的**模型档案（不指定就不指定，不在这里编默认）
        eff_profiles = [
            str(pid or "").strip() for pid in (model_profile_ids or []) if str(pid or "").strip()
        ]
        try:
            payload = dict(
                tasks.submit_model_arena(
                    symbol=str(symbol),
                    trade_date=str(trade_date or ""),
                    horizons=eff_horizons,
                    selected_analysts=eff_analysts,
                    model_profile_ids=eff_profiles,
                    query=str(query or ""),
                )
            )
        except Exception as exc:  # noqa: BLE001
            return _submit_degraded(
                reason=f"模型对比任务提交失败（{type(exc).__name__}）",
                as_of=as_of,
                error_type=type(exc).__name__,
            )
        experiment_id = str(payload.get("experiment_id") or "")
        job_ids = [str(j) for j in (payload.get("job_ids") or []) if str(j or "")]
        if not experiment_id or not job_ids:
            return _submit_degraded(reason="模型对比任务提交失败（未返回实验号或任务号）", as_of=as_of)
        evidence = (
            EvidenceItem(
                id="E1", field="模型对比实验号", value=experiment_id, unit="",
                source_key=_SOURCE_KEY, as_of=as_of,
            ),
            EvidenceItem(
                id="E2", field="各模型任务号",
                value="、".join(job_ids), unit="",
                source_key=_SOURCE_KEY, as_of=as_of,
            ),
            EvidenceItem(
                id="E3", field="模型对比参数",
                value=(
                    f"{symbol}｜交易日 {trade_date or '默认最近交易日'}｜"
                    f"{len(job_ids)} 个模型｜周期 {'/'.join(eff_horizons)}｜"
                    f"输入快照 {payload.get('input_snapshot_hash') or '未提供'}"
                ),
                unit="", source_key=_SOURCE_KEY, as_of=as_of,
            ),
            EvidenceItem(
                id="E4", field="模型对比任务说明",
                value=_async_task_note("模型对比（每个模型一个分析任务）", "各任务号"),
                unit="", source_key=_SOURCE_KEY, as_of=as_of,
            ),
        )
        return ToolResult(
            data={
                "experiment_id": experiment_id,
                "job_id": job_ids[0],
                "job_ids": job_ids,
                "status": "pending",
            },
            evidence=evidence,
            coverage=Coverage(available=(_SOURCE_KEY,), unavailable=()),
            as_of=as_of,
            disclaimer=RESEARCH_DISCLAIMER,
        )

    # 未实现该方法的端口**不注册**对应工具：在册但一调就失败只会浪费 Agent 预算。
    if _supports(tasks, "submit_deep_analysis"):
        specs.append(
            ToolSpec(
                name="submit_deep_analysis_job",
                title="提交全文研报生成（异步）",
                description=(
                    "提交一次**分钟级**的深度分析（多分析师管道 → 全文研报），立即返回任务号。"
                    "拿到任务号后用 poll_async_job 轮询。这是异步协议："
                    "不得在回答中声称已得到研报结论。"
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "标的代码，如 600519.SH"},
                        "trade_date": {
                            "type": "string",
                            "description": "交易日 YYYY-MM-DD，缺省用最近交易日",
                        },
                        "horizons": {
                            "type": "array",
                            "items": {"type": "string", "enum": list(ALLOWED_HORIZONS)},
                            "description": f"分析周期，默认 {list(DEFAULT_HORIZONS)}",
                        },
                        "selected_analysts": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "分析师组合，默认平台全量组合",
                        },
                        "query": {"type": "string", "description": "自定义分析意图（可选）"},
                        "as_of": {"type": "string", "description": "基准日 YYYY-MM-DD"},
                    },
                    "required": ["symbol"],
                },
                output_contract=TASK_CONTRACT,
                handler=submit_deep_analysis_job,
                cost="medium",
                plan_layer=PLAN_LAYER_TASK,
                asynchronous=True,
                latency_budget_ms=8000,
                freshness_sources=(_SOURCE_KEY,),
            )
        )

    if _supports(tasks, "submit_daily_review"):
        specs.append(
            ToolSpec(
                name="submit_daily_review_job",
                title="提交每日复盘（异步）",
                description=(
                    "提交一次**分钟级**的每日复盘批跑（选池 + 逐标的深度分析），立即返回批跑号。"
                    "拿到批跑号后用 poll_async_job 轮询。这是异步协议："
                    "不得在回答中声称已得到复盘结论。"
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "mode": {
                            "type": "string",
                            "enum": list(DAILY_MODES),
                            "description": "选池模式，默认 recommended（智能推荐）",
                        },
                        "top_k": {
                            "type": "integer",
                            "description": f"分析标的数，默认 {DEFAULT_DAILY_TOP_K}",
                        },
                        "market": {
                            "type": "string",
                            "enum": ["cn", "us"],
                            "description": "市场，默认 cn",
                        },
                        "horizons": {
                            "type": "array",
                            "items": {"type": "string", "enum": list(ALLOWED_HORIZONS)},
                            "description": f"分析周期，默认 {list(DEFAULT_HORIZONS)}",
                        },
                        "selected_analysts": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "分析师组合，默认平台全量组合",
                        },
                        "as_of": {"type": "string", "description": "基准日 YYYY-MM-DD"},
                    },
                    "required": [],
                },
                output_contract=TASK_CONTRACT,
                handler=submit_daily_review_job,
                cost="medium",
                plan_layer=PLAN_LAYER_TASK,
                asynchronous=True,
                # 比另两个提交工具宽：每日复盘在**提交阶段**就要先选池（推荐管道是
                # 网络取数，数十秒量级），后台跑的才是分钟级分析。预算给得太紧会有
                # 一个很坏的失败形态——工具已被判定超时降级，而任务其实已经排上队了。
                latency_budget_ms=60000,
                freshness_sources=(_SOURCE_KEY,),
            )
        )

    if _supports(tasks, "submit_model_arena"):
        specs.append(
            ToolSpec(
                name="submit_model_arena_job",
                title="提交模型对比（异步）",
                description=(
                    "提交一次**分钟级**的多模型对比（同一标的、同一输入快照，每个模型各出一个任务），"
                    "立即返回实验号与各模型任务号。拿到任务号后用 poll_async_job 轮询。"
                    "这是异步协议：不得在回答中声称已得到对比结论。"
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "标的代码，如 600519.SH"},
                        "trade_date": {
                            "type": "string",
                            "description": "交易日 YYYY-MM-DD，缺省用最近交易日",
                        },
                        "model_profile_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "参与对比的模型档案 ID，缺省用该用户全部启用档案",
                        },
                        "horizons": {
                            "type": "array",
                            "items": {"type": "string", "enum": list(ALLOWED_HORIZONS)},
                            "description": f"分析周期，默认 {list(DEFAULT_HORIZONS)}",
                        },
                        "selected_analysts": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "分析师组合，默认平台全量组合",
                        },
                        "query": {"type": "string", "description": "自定义分析意图（可选）"},
                        "as_of": {"type": "string", "description": "基准日 YYYY-MM-DD"},
                    },
                    "required": ["symbol"],
                },
                output_contract=TASK_CONTRACT,
                handler=submit_model_arena_job,
                cost="medium",
                plan_layer=PLAN_LAYER_TASK,
                asynchronous=True,
                latency_budget_ms=8000,
                freshness_sources=(_SOURCE_KEY,),
            )
        )

    return tuple(specs)
