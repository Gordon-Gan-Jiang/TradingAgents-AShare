"""分钟级能力的**可复用提交入口**（L3 异步协议的服务侧）。

## 这个模块修的是哪个问题

平台有三个分钟级能力：全文研报生成（`/v1/analyze`）、每日复盘（`/v1/daily-product/run`）、
模型对比（`/v1/model-arena/runs`）。它们的**提交**动作此前只存在于路由处理函数里，
而且三步都内联着：

    建任务记录（`_set_job`） → 推 `job.created` 事件（`_emit_job_event`）
    → 投递后台执行（`_create_tracked_task(_run_job(...))`）

后果不是"路由不好看"，而是**服务层没有可复用的提交入口**：对话 Agent 想调这三个能力，
只能把这条链路照抄一份到工具端口里——两份实现必然漂移，而且漂移的是"任务到底有没有
真的排上队"这种要命的地方。

本模块把这三步收成**一份实现**：HTTP 路由与对话 Agent 调的是同一批函数。

## 边界（据实说明，不含糊）

* **本模块不导入 `api.main`（模块级）**。FastAPI 应用层持有低层原语——内存任务表
  `_jobs`、事件队列、后台任务保活、以及真正的分析管道 `_run_job`——它们通过注入的
  `JobRuntime` 进来（默认实现 `default_job_runtime()` 在**调用时**才从 `api.main`
  解析）。必须惰性解析的原因有两条，都不是风格问题：

  1. `api.main` 在 import 期就装配了本模块，模块级导入会形成循环导入；
  2. 更重要：模块级捕获会让 `patch("api.main._create_tracked_task")` 这类替换**失效**
     ——注册表在替换之前就把真实实现捕获进闭包了。本仓库在
     `chat_service.build_registry` 上已经吃过一次这个亏（注释还在原处）。

* 请求/响应模型（`AnalyzeRequest`、`DailyProductRunRequest`…）仍定义在 `api/main.py`
  （测试与 130 多个端点都在用它），本模块按需惰性取用。这与仓库既有做法一致
  （`insights_t1_service`、`recommendation_service`、`chat_service` 都是这样引用
  `api.main` 的应用层能力的）。

* **三个能力都不依赖 HTTP 请求上下文**：只需要 `user_id`（路由从 `current_user` 取，
  对话侧由端口闭包绑定）。这一点是它们能被封成工具的前提；若哪天某个能力需要
  `Request`/`BackgroundTasks`，那它就不该被封成工具，而应如实说明。

## 为什么提交要"立即返回"

分钟的活儿放进同步调用面会同时坏三件事：客户端超时重试（同一任务被提交多次）、
对话循环卡住、预算记账失真。所以除 `dry_run`（HTTP 专用的"就地跑完"语义）之外，
提交一律只做"建记录 + 推事件 + 投递"，不等结果。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence

from api.database import (
    ImportedPortfolioPositionDB,
    WatchlistItemDB,
    get_db_ctx,
)
from api.services import (
    prompt_template_service,
    recommendation_feedback_service,
    recommendation_service,
    scheduled_service,
)
from tradingagents.agents.utils.context_utils import USER_CONTEXT_KEYS, normalize_user_context
from tradingagents.dataflows.trade_calendar import cn_today_str

logger = logging.getLogger(__name__)

# 与工具层的 `_SOURCE_KEY` 对齐：证据来源键（"异步任务"这一维度）
TASK_SOURCE_KEY = "async_task"

DEFAULT_HORIZONS: tuple[str, ...] = ("short",)
DEFAULT_ANALYSTS: tuple[str, ...] = (
    "market",
    "social",
    "news",
    "fundamentals",
    "macro",
    "smart_money",
)
DEFAULT_DAILY_TOP_K = 3


# ══════════════════════════════════════════════════════════════════════════
# 运行时（应用层注入的低层原语）
# ══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class JobRuntime:
    """任务运行时：由应用层（`api/main.py`）提供。

    这里刻意**只**放"应用层才知道的东西"——内存任务表、事件队列、后台任务保活、
    真正的分析管道。业务编排（提交序列）留在本模块，两边不重叠：

    * `new_job_id` / `now_iso`：任务号与时间戳
    * `set_job` / `get_job`：任务记录读写（`api.main._jobs`）
    * `ensure_event_queue` / `emit_event`：SSE 事件通道
    * `create_task`：后台任务投递（asyncio task + 引用保活）
    * `run_analysis_job`：深度分析管道（分钟级，`api.main._run_job`）
    * `set_daily_run`：每日批跑记录落库（`api.main._set_daily_product_run`）
    * `finalize_daily_run`：批跑收尾与推送（分钟级轮询，`api.main._finalize_daily_product_run_and_push`）
    """

    new_job_id: Callable[[], str]
    now_iso: Callable[[], str]
    set_job: Callable[..., None]
    get_job: Callable[[str], Mapping[str, Any]]
    ensure_event_queue: Callable[[str], Any]
    emit_event: Callable[[str, str, Mapping[str, Any]], None]
    create_task: Callable[..., Any]
    run_analysis_job: Callable[..., Awaitable[None]]
    set_daily_run: Callable[..., None]
    finalize_daily_run: Callable[[str], Awaitable[None]]


def default_job_runtime() -> JobRuntime:
    """应用层默认运行时：**调用时**才从 `api.main` 解析（见模块 docstring）。"""
    from api.main import (
        _emit_job_event,
        _ensure_job_event_queue,
        _finalize_daily_product_run_and_push,
        _get_job,
        _run_job,
        _set_daily_product_run,
        _set_job,
        _submit_background_task,
        _utcnow_iso,
    )
    from uuid import uuid4

    return JobRuntime(
        new_job_id=lambda: uuid4().hex,
        now_iso=_utcnow_iso,
        set_job=_set_job,
        get_job=_get_job,
        ensure_event_queue=_ensure_job_event_queue,
        emit_event=_emit_job_event,
        # `_submit_background_task` 而不是 `_create_tracked_task`：对话工具的 handler
        # 跑在线程池里，那里没有事件循环，投递必须转回应用主循环（见其 docstring）。
        create_task=_submit_background_task,
        run_analysis_job=_run_job,
        set_daily_run=_set_daily_product_run,
        finalize_daily_run=_finalize_daily_product_run_and_push,
    )


# ══════════════════════════════════════════════════════════════════════════
# 领域异常（路由翻译成 400；工具降级为「提交失败（类型名）」）
# ══════════════════════════════════════════════════════════════════════════


class PromptTemplateError(ValueError):
    """提示词模板解析失败（用户给的模板 ID 不可用）。

    单独一个类型，是为了让路由**只**把这一种失败翻译成 400：原实现用裸 `ValueError`
    包住模板调用，重构时若把整个提交序列塞进 `except ValueError`，任务表读到脏值之类
    的内部错误也会被说成"你参数不对"。
    """


class ArenaRequestError(ValueError):
    """模型对比的入参校验失败（没有可用模型档案 / 指定了不属于该用户的档案）。"""


# ══════════════════════════════════════════════════════════════════════════
# 提交结果（中性结构，路由再做一层到响应模型的映射）
# ══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class SubmittedJob:
    job_id: str
    status: str
    created_at: str


@dataclass(frozen=True)
class SubmittedDailyReview:
    run_id: str
    status: str
    mode: str
    summary: Dict[str, Any]
    recommendation: Any
    jobs: tuple


@dataclass(frozen=True)
class SubmittedArenaRun:
    experiment_id: str
    input_snapshot_hash: str
    symbol: str
    trade_date: str
    jobs: tuple


# ══════════════════════════════════════════════════════════════════════════
# 用户上下文（从 api/main.py 迁入：纯逻辑，属提交路径的一部分）
# ══════════════════════════════════════════════════════════════════════════


def extract_request_user_context(request: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    for key in USER_CONTEXT_KEYS:
        value = getattr(request, key, None)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if key == "constraints" and not value:
            continue
        payload[key] = value
    return payload


def merge_user_context_payload(
    explicit_context: Dict[str, Any],
    inferred_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    merged = normalize_user_context(inferred_context or {})
    merged.update(normalize_user_context(explicit_context or {}))
    return merged


def compose_analysis_user_context(
    db: Any,
    user_id: str,
    symbol: str,
    *,
    explicit_context: Optional[Dict[str, Any]] = None,
    inferred_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Compose the LLM-facing user context for an analysis run.

    这里刻意【不】合并导入持仓（imported positions）：深度分析必须保持客观。
    持仓、成本、浮盈一旦进入提示词，模型就会围绕"我的持仓该怎么办"作答，
    结论随用户持仓漂移并引入噪音。

    持仓相关的减仓/卖出决策改由独立的确定性退出引擎承担
    （api/services/exit_engine_service.py）：它读取报告锚点与真实持仓自行
    计算触发条件，不经过 LLM 上下文。db/user_id/symbol 参数保留仅为兼容
    既有调用方签名。
    """
    return merge_user_context_payload(explicit_context or {}, inferred_context or {})


def apply_user_context_to_request(request: Any, user_context: Dict[str, Any]) -> Any:
    request.objective = user_context.get("objective")
    request.risk_profile = user_context.get("risk_profile")
    request.investment_horizon = user_context.get("investment_horizon")
    request.cash_available = user_context.get("cash_available")
    request.current_position = user_context.get("current_position")
    request.current_position_pct = user_context.get("current_position_pct")
    request.average_cost = user_context.get("average_cost")
    request.max_loss_pct = user_context.get("max_loss_pct")
    request.constraints = user_context.get("constraints", [])
    request.user_notes = user_context.get("user_notes")
    return request


def apply_prompt_template_to_request(
    db: Any,
    user_id: str,
    request: Any,
    *,
    fallback_template_id: str,
    horizon: Optional[str] = None,
    default_only_when_missing: bool = True,
) -> Any:
    """套用提示词模板。模板 ID 不可用 → `PromptTemplateError`（路由翻 400）。

    依赖应用层的股票名缓存（`api.main._normalize_symbol` /
    `_get_reverse_stock_map`：只读预热缓存，不触网、不阻塞），因此这两个辅助函数
    惰性取用而不是搬进服务层。
    """
    from api.main import _get_reverse_stock_map, _normalize_symbol

    template_id = (request.prompt_template_id or "").strip()
    if not template_id and request.query:
        return request
    if default_only_when_missing and not template_id and not request.query:
        return request
    symbol = _normalize_symbol((request.symbol or "").strip().upper())
    if not symbol:
        return request
    code_to_name = _get_reverse_stock_map()
    try:
        template = prompt_template_service.resolve_template(
            db,
            user_id,
            template_id or fallback_template_id,
            fallback_template_id=fallback_template_id,
            scope=prompt_template_service.SCOPE_DEEP_ANALYSIS,
        )
    except ValueError as exc:
        raise PromptTemplateError(str(exc)) from exc
    horizon_value = horizon or (request.horizons[0] if request.horizons else "short")
    rendered = prompt_template_service.render_template(
        template,
        symbol=symbol,
        name=code_to_name.get(symbol, symbol),
        horizon=horizon_value,
        trade_date=request.trade_date,
        prompt_vars=request.prompt_vars or {},
    )
    request.symbol = symbol
    request.query = rendered["query"]
    request.user_intent = rendered["user_intent"]
    request.prompt_template_id = template["id"]
    return request


def _resolve_runtime(runtime: Optional[JobRuntime]) -> JobRuntime:
    return runtime if runtime is not None else default_job_runtime()


def _register_job(
    *,
    runtime: JobRuntime,
    user_id: str,
    symbol: str,
    trade_date: str,
    extra: Optional[Dict[str, Any]] = None,
    event_extra: Optional[Dict[str, Any]] = None,
) -> tuple[str, str]:
    """建任务记录 + 推 `job.created`。返回 `(job_id, created_at)`。

    这一段就是"提交"的公共骨架：不管调用方是 HTTP 路由还是对话工具，落到任务表里的
    字段与事件完全一致——这正是封工具时最容易被抄歪的部分。

    `extra` 只影响任务记录，`event_extra` 只影响事件负载：两者刻意分开。既有行为里
    `/v1/analyze` 的 `job.created` **不带** model_profile_id / experiment_id，而
    `/v1/model-arena/runs` 带；SSE 消费者按字段读，合成一个参数就是线上事件漂移。
    """
    job_id = runtime.new_job_id()
    created_at = runtime.now_iso()
    payload: Dict[str, Any] = {
        "job_id": job_id,
        "user_id": user_id,
        "status": "pending",
        "created_at": created_at,
        "started_at": None,
        "finished_at": None,
        "symbol": symbol,
        "trade_date": trade_date,
        "error": None,
        "result": None,
        "decision": None,
    }
    payload.update(extra or {})
    runtime.set_job(job_id, **payload)
    runtime.ensure_event_queue(job_id)
    event_data: Dict[str, Any] = {
        "job_id": job_id,
        "symbol": symbol,
        "trade_date": trade_date,
    }
    event_data.update(event_extra or {})
    runtime.emit_event(job_id, "job.created", event_data)
    return job_id, created_at


# ══════════════════════════════════════════════════════════════════════════
# 一、全文研报生成（/v1/analyze）
# ══════════════════════════════════════════════════════════════════════════


def _open_deep_analysis(*, request: Any, user_id: str, runtime: JobRuntime) -> SubmittedJob:
    """套模板 → 合并用户上下文 → 建任务记录 → 推事件。"""
    with get_db_ctx() as db:
        apply_prompt_template_to_request(
            db,
            user_id,
            request,
            fallback_template_id=prompt_template_service.DEFAULT_MANUAL_TEMPLATE_ID,
            default_only_when_missing=False,
        )
        merged_user_context = compose_analysis_user_context(
            db,
            user_id,
            request.symbol,
            explicit_context=extract_request_user_context(request),
        )
    apply_user_context_to_request(request, merged_user_context)

    job_id, created_at = _register_job(
        runtime=runtime,
        user_id=user_id,
        symbol=request.symbol,
        trade_date=request.trade_date,
        extra={
            "model_profile_id": request.model_profile_id,
            "experiment_id": request.experiment_id,
            "input_snapshot_hash": request.input_snapshot_hash,
        },
    )
    return SubmittedJob(job_id=job_id, status="pending", created_at=created_at)


def _dispatch_deep_analysis(*, request: Any, user_id: str, runtime: JobRuntime) -> SubmittedJob:
    """建记录 + 推事件 + 投递后台执行（不等结果）：HTTP 与对话工具共用的一步。"""
    submitted = _open_deep_analysis(request=request, user_id=user_id, runtime=runtime)
    runtime.create_task(
        runtime.run_analysis_job(submitted.job_id, request, True, True, user_id, "api")
    )
    return submitted


async def submit_deep_analysis(
    *,
    request: Any,
    user_id: str,
    runtime: Optional[JobRuntime] = None,
) -> SubmittedJob:
    """HTTP 入口（`/v1/analyze`）。`dry_run=True` 时就地执行并返回终态。

    这是**唯一**的深度分析提交实现：路由与对话工具共用 `_open_deep_analysis` /
    `_dispatch_deep_analysis`，区别只在"要不要等"。`dry_run` 是 HTTP 专用的调试语义，
    对话工具永远不等。
    """
    rt = _resolve_runtime(runtime)
    if not getattr(request, "dry_run", False):
        return _dispatch_deep_analysis(request=request, user_id=user_id, runtime=rt)
    submitted = _open_deep_analysis(request=request, user_id=user_id, runtime=rt)
    await rt.run_analysis_job(submitted.job_id, request, True, True, user_id, "api")
    status = str(rt.get_job(submitted.job_id).get("status") or "completed")
    return SubmittedJob(job_id=submitted.job_id, status=status, created_at=submitted.created_at)


def submit_deep_analysis_task(
    *,
    user_id: str,
    symbol: str,
    trade_date: str = "",
    horizons: Optional[Sequence[str]] = None,
    selected_analysts: Optional[Sequence[str]] = None,
    query: str = "",
    runtime: Optional[JobRuntime] = None,
) -> SubmittedJob:
    """对话工具入口：按标量构造请求并异步提交（永不 `dry_run`）。

    `trade_date=""` → 今天；`horizons`/`selected_analysts` 为 `None` → 平台默认。
    刻意不用 `or` 兜底：`0`/空值被 falsy 替换是本仓库反复踩过的坑（数值参数尤其）。
    """
    from api.main import AnalyzeRequest

    rt = _resolve_runtime(runtime)
    request = AnalyzeRequest(
        symbol=symbol or "",
        trade_date=trade_date if trade_date else cn_today_str(),
        horizons=list(horizons) if horizons is not None else list(DEFAULT_HORIZONS),
        selected_analysts=(
            list(selected_analysts) if selected_analysts is not None else list(DEFAULT_ANALYSTS)
        ),
        query=(query or None),
        dry_run=False,
    )
    return _dispatch_deep_analysis(request=request, user_id=user_id, runtime=rt)


# ══════════════════════════════════════════════════════════════════════════
# 二、每日复盘（/v1/daily-product/run）
# ══════════════════════════════════════════════════════════════════════════


def build_daily_review_request(
    *,
    mode: str = "recommended",
    top_k: int = DEFAULT_DAILY_TOP_K,
    market: str = "cn",
    horizons: Optional[Sequence[str]] = None,
    selected_analysts: Optional[Sequence[str]] = None,
    include_tracking: bool = True,
    include_watchlist: bool = True,
    ensure_scheduled: bool = False,
) -> Any:
    """构造每日批跑请求（对话工具用；HTTP 路由直接用请求体）。"""
    from api.main import DailyProductRunRequest

    return DailyProductRunRequest(
        mode=mode,
        top_k=top_k,
        market=market,
        horizons=list(horizons) if horizons is not None else list(DEFAULT_HORIZONS),
        selected_analysts=(
            list(selected_analysts) if selected_analysts is not None else list(DEFAULT_ANALYSTS)
        ),
        include_tracking=include_tracking,
        include_watchlist=include_watchlist,
        ensure_scheduled=ensure_scheduled,
    )


def apply_backtest_feedback_to_weights(
    *,
    user_id: str,
    momentum_weight: Optional[float],
    activity_weight: Optional[float],
    near_high_weight: Optional[float],
) -> tuple[Optional[float], Optional[float], Optional[float], Dict[str, Any]]:
    """Use the latest completed backtest stats to slightly bias recommendation weights."""
    from api.services import backtest_service

    jobs = backtest_service.list_jobs()
    latest = None
    for job in jobs:
        if str(job.get("status") or "") != "completed":
            continue
        if str(job.get("user_id") or "") != user_id:
            continue
        latest = job
        break
    if latest is None:
        resolved = {
            "momentum": 0.4 if momentum_weight is None else float(momentum_weight),
            "activity": 0.35 if activity_weight is None else float(activity_weight),
            "near_high": 0.25 if near_high_weight is None else float(near_high_weight),
        }
        total = sum(resolved.values())
        resolved = {k: round(v / total, 4) for k, v in resolved.items()}
        learning_info: Dict[str, Any] = {"applied": False, "reason": "no_strategy_feedback"}
        with get_db_ctx() as db:
            resolved, learning_info = recommendation_feedback_service.build_learning_weight_adjustment(
                db,
                user_id=user_id,
                base_weights=resolved,
            )
        return resolved["momentum"], resolved["activity"], resolved["near_high"], {
            "applied": bool(learning_info.get("applied")),
            "reason": "no_backtest",
            "strategy_learning": learning_info,
        }

    stats = latest.get("stats") or {}
    win_rate = float(stats.get("win_rate") or 0.0)
    avg_return = float(stats.get("avg_return_pct") or 0.0)

    base_m = 0.4 if momentum_weight is None else float(momentum_weight)
    base_a = 0.35 if activity_weight is None else float(activity_weight)
    base_n = 0.25 if near_high_weight is None else float(near_high_weight)

    # Simple feedback principle:
    # - High win-rate / positive return -> favor momentum
    # - Low win-rate / negative return -> favor activity + near-high stability
    m_shift = ((win_rate - 50.0) / 100.0) * 0.2 + (avg_return / 100.0)
    m = max(0.0, min(10.0, base_m + m_shift))
    a = max(0.0, min(10.0, base_a - m_shift * 0.5))
    n = max(0.0, min(10.0, base_n - m_shift * 0.5))
    total = m + a + n
    if total <= 0:
        return momentum_weight, activity_weight, near_high_weight, {"applied": False, "reason": "invalid_total"}
    normalized = {
        "momentum": round(m / total, 4),
        "activity": round(a / total, 4),
        "near_high": round(n / total, 4),
    }
    learning_info = {"applied": False}
    with get_db_ctx() as db:
        normalized, learning_info = recommendation_feedback_service.build_learning_weight_adjustment(
            db,
            user_id=user_id,
            base_weights=normalized,
        )
    return normalized["momentum"], normalized["activity"], normalized["near_high"], {
        "applied": True,
        "backtest_job_id": latest.get("job_id"),
        "win_rate": win_rate,
        "avg_return_pct": avg_return,
        "strategy_learning": learning_info,
    }


DAILY_PRODUCT_STRATEGY_SKILLS: List[Dict[str, str]] = [
    {"id": "market_strategy", "name": "市场策略框架", "reference": "market-strategy.md"},
    {"id": "risk_scoring", "name": "风险评分标准", "reference": "risk-scoring-criteria.md"},
    {"id": "analysis_framework", "name": "分析框架", "reference": "analysis-framework.md"},
    {"id": "backtesting_guidelines", "name": "回测指引", "reference": "backtesting-guidelines.md"},
    {"id": "chart_guide", "name": "图表解读指引", "reference": "chart-guide.md"},
]


def resolve_daily_product_strategy_skills(
    *,
    strategy_mode: str,
    requested_skills: List[str],
    market: str,
    feedback_info: Dict[str, Any],
) -> List[str]:
    allow = {item["id"] for item in DAILY_PRODUCT_STRATEGY_SKILLS}
    requested = [s for s in (requested_skills or []) if s in allow]
    if strategy_mode == "manual":
        return requested

    # Auto mode: keep a stable baseline, then adapt by market + backtest outcome.
    selected: List[str] = ["analysis_framework", "market_strategy"]
    if market == "cn":
        selected.append("chart_guide")
    selected.append("risk_scoring")

    if bool(feedback_info.get("applied")):
        win_rate = float(feedback_info.get("win_rate") or 0.0)
        avg_ret = float(feedback_info.get("avg_return_pct") or 0.0)
        if win_rate < 45.0 or avg_ret < 0.0:
            # Weak backtest regime -> strengthen risk & backtest discipline.
            selected.append("backtesting_guidelines")

    # Merge user-requested skills in auto mode (dedupe, keep order).
    selected.extend(requested)
    deduped: List[str] = []
    seen: set[str] = set()
    for skill in selected:
        if skill in seen:
            continue
        seen.add(skill)
        deduped.append(skill)
    return deduped


def build_daily_product_query(symbol: str, strategy_skills: List[str]) -> str:
    if not strategy_skills:
        return f"daily_product 批量分析 {symbol}"
    return (
        f"daily_product 策略编排分析 {symbol}；"
        f"请融合策略技能：{','.join(strategy_skills)}，"
        "给出可执行交易计划并说明关键风险。"
    )


def submit_daily_review(
    *,
    request: Any,
    user_id: str,
    db: Any = None,
    runtime: Optional[JobRuntime] = None,
) -> SubmittedDailyReview:
    """每日复盘（全市场批量分析）的提交入口。

    与原路由内联实现的**唯一**差别：`db` 可以不给——对话工具没有 FastAPI 的
    `Depends(get_db)`，此时服务自己开一个会话。HTTP 路由仍把自己那个会话传进来，
    行为逐字不变。
    """
    rt = _resolve_runtime(runtime)
    if db is not None:
        return _submit_daily_review(request=request, user_id=user_id, db=db, runtime=rt)
    with get_db_ctx() as own_db:
        return _submit_daily_review(request=request, user_id=user_id, db=own_db, runtime=rt)


def submit_daily_review_task(
    *,
    user_id: str,
    mode: str = "recommended",
    top_k: int = DEFAULT_DAILY_TOP_K,
    market: str = "cn",
    horizons: Optional[Sequence[str]] = None,
    selected_analysts: Optional[Sequence[str]] = None,
    include_tracking: bool = True,
    include_watchlist: bool = True,
    runtime: Optional[JobRuntime] = None,
) -> SubmittedDailyReview:
    """对话工具入口：构造请求 → 自开会话 → 走同一份提交实现。"""
    request = build_daily_review_request(
        mode=mode,
        top_k=top_k,
        market=market,
        horizons=horizons,
        selected_analysts=selected_analysts,
        include_tracking=include_tracking,
        include_watchlist=include_watchlist,
    )
    return submit_daily_review(request=request, user_id=user_id, runtime=runtime)


def _submit_daily_review(
    *,
    request: Any,
    user_id: str,
    db: Any,
    runtime: JobRuntime,
) -> SubmittedDailyReview:
    from api.main import (
        AnalyzeRequest,
        DailyProductRunJob,
        DailyProductRunResponse,
        RecommendationItemResponse,
        RecommendationResponse,
        _get_reverse_stock_map_cached_only,
        _normalize_symbol,
    )

    run_id = runtime.new_job_id()
    run_created_at = runtime.now_iso()
    code_to_name = _get_reverse_stock_map_cached_only()
    recommendation_payload: Optional[Any] = None
    target_symbols: list[str] = []
    symbol_to_name: dict[str, str] = {}

    effective_momentum = request.momentum_weight
    effective_activity = request.activity_weight
    effective_near_high = request.near_high_weight
    feedback_info: dict[str, Any] = {"applied": False}
    if request.use_backtest_feedback:
        (
            effective_momentum,
            effective_activity,
            effective_near_high,
            feedback_info,
        ) = apply_backtest_feedback_to_weights(
            user_id=user_id,
            momentum_weight=request.momentum_weight,
            activity_weight=request.activity_weight,
            near_high_weight=request.near_high_weight,
        )
    effective_strategy_skills = resolve_daily_product_strategy_skills(
        strategy_mode=request.strategy_mode,
        requested_skills=request.strategy_skills,
        market=request.market,
        feedback_info=feedback_info,
    )

    if request.mode == "recommended":
        rec = recommendation_service.recommend_for_user(
            db=db,
            user_id=user_id,
            top_k=request.top_k,
            candidate_limit=request.candidate_limit,
            source_mode=request.recommendation_source,
            scan_limit=request.scan_limit,
            include_tracking=request.include_tracking,
            include_watchlist=request.include_watchlist,
            seed_symbols=request.seed_symbols,
            min_change_pct=request.min_change_pct,
            market=request.market,
            min_price=request.min_price,
            max_price=request.max_price,
            min_amount=request.min_amount,
            min_turnover_rate=request.min_turnover_rate,
            min_volume_ratio=request.min_volume_ratio,
            limit_up_threshold_pct=request.limit_up_threshold_pct,
            limit_down_threshold_pct=request.limit_down_threshold_pct,
            enforce_tradability=request.enforce_tradability,
            profile=request.score_profile,
            momentum_weight=effective_momentum,
            activity_weight=effective_activity,
            near_high_weight=effective_near_high,
        )
        recommendation_payload = RecommendationResponse(
            pool_size=int(rec.get("pool_size") or 0),
            scored_size=int(rec.get("scored_size") or 0),
            items=[RecommendationItemResponse(**item) for item in rec.get("items") or []],
            scoring_model=rec.get("scoring_model"),
            analysis_jobs=[],
        )
        for item in rec.get("items") or []:
            symbol = str(item.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            target_symbols.append(symbol)
            symbol_to_name[symbol] = str(item.get("name") or symbol)
    else:
        if request.mode in {"tracking", "all"}:
            rows = (
                db.query(ImportedPortfolioPositionDB)
                .filter(ImportedPortfolioPositionDB.user_id == user_id)
                .order_by(
                    ImportedPortfolioPositionDB.market_value.desc(),
                    ImportedPortfolioPositionDB.current_position.desc(),
                    ImportedPortfolioPositionDB.symbol.asc(),
                )
                .all()
            )
            for row in rows:
                symbol = str(row.symbol or "").strip().upper()
                if not symbol:
                    continue
                symbol_to_name.setdefault(symbol, str(row.security_name or symbol).strip() or symbol)
        if request.mode in {"watchlist", "all"}:
            rows = (
                db.query(WatchlistItemDB)
                .filter(WatchlistItemDB.user_id == user_id)
                .order_by(WatchlistItemDB.sort_order.asc(), WatchlistItemDB.created_at.desc())
                .all()
            )
            for row in rows:
                symbol = _normalize_symbol(str(row.symbol or ""))
                if not symbol:
                    continue
                symbol_to_name.setdefault(symbol, code_to_name.get(symbol) or symbol)

        target_symbols = list(symbol_to_name.keys())[: max(1, min(int(request.top_k), 20))]

    if not target_symbols:
        empty_response = DailyProductRunResponse(
            run_id=run_id,
            status="completed",
            mode=request.mode,
            summary={
                "total_targets": 0,
                "queued_jobs": 0,
                "scheduled_created": 0,
                "backtest_feedback": feedback_info,
                "strategy_mode": request.strategy_mode,
                "strategy_skills": effective_strategy_skills,
                "recommendation_source": request.recommendation_source,
            },
            recommendation=recommendation_payload,
            jobs=[],
        )
        runtime.set_daily_run(
            run_id,
            user_id=user_id,
            mode=request.mode,
            status="completed",
            created_at=run_created_at,
            finished_at=runtime.now_iso(),
            jobs=[],
            recommendation=recommendation_payload.model_dump() if recommendation_payload else None,
            summary=empty_response.summary,
        )
        return SubmittedDailyReview(
            run_id=run_id,
            status=empty_response.status,
            mode=request.mode,
            summary=dict(empty_response.summary),
            recommendation=recommendation_payload,
            jobs=(),
        )

    queued_jobs: list[Any] = []
    for symbol in target_symbols:
        with get_db_ctx() as ctx_db:
            merged_user_context = compose_analysis_user_context(ctx_db, user_id, symbol)
        analyze_req = AnalyzeRequest(
            symbol=symbol,
            trade_date=cn_today_str(),
            horizons=request.horizons or ["short"],
            selected_analysts=request.selected_analysts,
            query=build_daily_product_query(symbol, effective_strategy_skills),
        )
        apply_user_context_to_request(analyze_req, merged_user_context)
        job_id, now = _register_job(
            runtime=runtime,
            user_id=user_id,
            symbol=symbol,
            trade_date=analyze_req.trade_date,
            extra={"request_source": "daily_product_batch"},
        )
        runtime.create_task(
            runtime.run_analysis_job(job_id, analyze_req, True, True, user_id, "daily_product_batch"),
            label=f"Daily product analyze task ({symbol})",
        )
        queued_jobs.append(
            DailyProductRunJob(
                symbol=symbol,
                name=symbol_to_name.get(symbol) or code_to_name.get(symbol) or symbol,
                job_id=job_id,
                status="pending",
                created_at=now,
            )
        )

    scheduled_result: dict[str, Any] = {"created": [], "existing": [], "skipped_limit": []}
    if request.ensure_scheduled and target_symbols:
        try:
            scheduled_result = scheduled_service.ensure_scheduled_for_symbols(
                db=db,
                user_id=user_id,
                symbols=target_symbols,
                horizon=request.schedule_horizon,
                trigger_time=request.schedule_trigger_time,
            )
            db.commit()
        except Exception as exc:
            logger.info("[Daily Product] ensure_scheduled failed for user=%s: %s", user_id, exc)

    run_row = {
        "run_id": run_id,
        "user_id": user_id,
        "mode": request.mode,
        "status": "pending",
        "created_at": run_created_at,
        "finished_at": None,
        "jobs": [j.model_dump() for j in queued_jobs],
        "recommendation": recommendation_payload.model_dump() if recommendation_payload else None,
        "summary": {
            "total_targets": len(target_symbols),
            "queued_jobs": len(queued_jobs),
            "completed_jobs": 0,
            "failed_jobs": 0,
            "running_jobs": 0,
            "pending_jobs": len(queued_jobs),
            "scheduled_created": len(scheduled_result.get("created") or []),
            "scheduled_existing": len(scheduled_result.get("existing") or []),
            "scheduled_skipped_limit": len(scheduled_result.get("skipped_limit") or []),
            "backtest_feedback": feedback_info,
            "strategy_mode": request.strategy_mode,
            "strategy_skills": effective_strategy_skills,
            "recommendation_source": request.recommendation_source,
            "scan_limit": request.scan_limit,
        },
    }
    if recommendation_payload and recommendation_payload.items:
        recommendation_feedback_service.persist_scan_results(
            db,
            user_id=user_id,
            run_id=run_id,
            source_mode=request.recommendation_source,
            market=request.market,
            score_profile=request.score_profile,
            items=[item.model_dump() for item in recommendation_payload.items],
            selected_symbols=target_symbols,
            feedback_horizon_days=5 if "short" in (request.horizons or ["short"]) else 10,
        )
        db.commit()
    runtime.set_daily_run(run_id, **{k: v for k, v in run_row.items() if k != "run_id"})
    runtime.create_task(
        runtime.finalize_daily_run(run_id),
        label=f"Daily product run monitor ({run_id[:8]})",
    )

    return SubmittedDailyReview(
        run_id=run_id,
        status="pending",
        mode=request.mode,
        summary=dict(run_row["summary"]),
        recommendation=recommendation_payload,
        jobs=tuple(queued_jobs),
    )


# ══════════════════════════════════════════════════════════════════════════
# 三、模型对比（/v1/model-arena/runs）
# ══════════════════════════════════════════════════════════════════════════


def _build_model_arena_snapshot_hash(payload: Dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _resolve_arena_profiles(*, db: Any, user_id: str, requested_ids: Sequence[str]) -> tuple[list[str], dict[str, Any]]:
    from api.services import model_profile_service

    profiles = model_profile_service.list_model_profiles(db, user_id, include_inactive=False)
    profile_map = {str(item.get("id") or ""): item for item in profiles}
    selected_ids = [str(pid or "").strip() for pid in requested_ids if str(pid or "").strip()]
    if not selected_ids:
        selected_ids = [str(item.get("id") or "") for item in profiles if item.get("is_active")]
    selected_ids = [pid for pid in selected_ids if pid]
    selected_ids = list(dict.fromkeys(selected_ids))  # preserve order, dedupe
    if not selected_ids:
        raise ArenaRequestError("请先配置并启用至少一个模型配置")
    invalid_ids = [pid for pid in selected_ids if pid not in profile_map]
    if invalid_ids:
        raise ArenaRequestError(f"模型配置不可用：{', '.join(invalid_ids)}")
    return selected_ids, profile_map


@dataclass(frozen=True)
class _ArenaPlanEntry:
    """一次模型对比里"某个模型"的那份任务：记录已建、事件已推，只差投递。"""

    job: Any  # ModelArenaRunJob（应用层模型）
    analyze_request: Any  # AnalyzeRequest


@dataclass(frozen=True)
class _ArenaPlan:
    experiment_id: str
    input_snapshot_hash: str
    symbol: str
    trade_date: str
    entries: tuple


def _prepare_arena_run(*, request: Any, user_id: str, runtime: JobRuntime) -> _ArenaPlan:
    """模型对比的**公共准备序列**：校验 → 建每个模型的任务记录 → 推事件。

    同步、无 `await`：这样 HTTP 路由与对话工具（同步 handler）能共用同一份实现，
    区别只剩下"投递后要不要等"。实验号与输入快照哈希在这一层生成——它们是"同输入
    跨模型可比"的唯一依据，不能交给调用方各自编。
    """
    from api.main import AnalyzeRequest, ModelArenaRunJob, _normalize_symbol

    symbol = _normalize_symbol(request.symbol)
    if not symbol:
        raise ArenaRequestError("symbol is required")

    with get_db_ctx() as db:
        selected_ids, profile_map = _resolve_arena_profiles(
            db=db, user_id=user_id, requested_ids=request.model_profile_ids
        )
        merged_user_context = compose_analysis_user_context(
            db,
            user_id,
            symbol,
            explicit_context={
                "objective": request.objective,
                "risk_profile": request.risk_profile,
                "investment_horizon": request.investment_horizon,
                "cash_available": request.cash_available,
                "current_position": request.current_position,
                "current_position_pct": request.current_position_pct,
                "average_cost": request.average_cost,
                "max_loss_pct": request.max_loss_pct,
                "constraints": request.constraints,
                "user_notes": request.user_notes,
            },
        )

    snapshot_hash = _build_model_arena_snapshot_hash(
        {
            "symbol": symbol,
            "trade_date": request.trade_date,
            "selected_analysts": request.selected_analysts,
            "horizons": request.horizons,
            "query": request.query or "",
            "prompt_template_id": request.prompt_template_id or "",
            "prompt_vars": request.prompt_vars or {},
            "user_context": merged_user_context,
        }
    )
    experiment_id = runtime.new_job_id()
    entries: list[_ArenaPlanEntry] = []

    for profile_id in selected_ids:
        profile = profile_map.get(profile_id) or {}
        analyze_req = AnalyzeRequest(
            symbol=symbol,
            trade_date=request.trade_date,
            selected_analysts=request.selected_analysts,
            config_overrides=request.config_overrides,
            dry_run=request.dry_run,
            query=request.query,
            horizons=request.horizons,
            prompt_template_id=request.prompt_template_id,
            prompt_vars=request.prompt_vars,
            model_profile_id=profile_id,
            experiment_id=experiment_id,
            input_snapshot_hash=snapshot_hash,
            objective=merged_user_context.get("objective"),
            risk_profile=merged_user_context.get("risk_profile"),
            investment_horizon=merged_user_context.get("investment_horizon"),
            cash_available=merged_user_context.get("cash_available"),
            current_position=merged_user_context.get("current_position"),
            current_position_pct=merged_user_context.get("current_position_pct"),
            average_cost=merged_user_context.get("average_cost"),
            max_loss_pct=merged_user_context.get("max_loss_pct"),
            constraints=merged_user_context.get("constraints") or [],
            user_notes=merged_user_context.get("user_notes"),
        )

        with get_db_ctx() as db:
            apply_prompt_template_to_request(
                db,
                user_id,
                analyze_req,
                fallback_template_id=prompt_template_service.DEFAULT_MANUAL_TEMPLATE_ID,
                default_only_when_missing=False,
            )

        job_id, now = _register_job(
            runtime=runtime,
            user_id=user_id,
            symbol=analyze_req.symbol,
            trade_date=analyze_req.trade_date,
            extra={
                "model_profile_id": profile_id,
                "model_profile_name": profile.get("name"),
                "experiment_id": experiment_id,
                "input_snapshot_hash": snapshot_hash,
            },
            event_extra={
                "model_profile_id": profile_id,
                "experiment_id": experiment_id,
            },
        )
        entries.append(
            _ArenaPlanEntry(
                job=ModelArenaRunJob(
                    model_profile_id=profile_id,
                    model_profile_name=profile.get("name"),
                    job_id=job_id,
                    symbol=analyze_req.symbol,
                    trade_date=analyze_req.trade_date,
                    status="pending",
                    created_at=now,
                ),
                analyze_request=analyze_req,
            )
        )

    return _ArenaPlan(
        experiment_id=experiment_id,
        input_snapshot_hash=snapshot_hash,
        symbol=symbol,
        trade_date=request.trade_date,
        entries=tuple(entries),
    )


async def submit_model_arena_run(
    *,
    request: Any,
    user_id: str,
    runtime: Optional[JobRuntime] = None,
) -> SubmittedArenaRun:
    """模型对比的 HTTP 入口（`/v1/model-arena/runs`），保留 `dry_run` 就地执行语义。

    逐个模型配置起一个任务：共享同一个 `experiment_id` 与 `input_snapshot_hash`。
    """
    rt = _resolve_runtime(runtime)
    plan = _prepare_arena_run(request=request, user_id=user_id, runtime=rt)
    jobs = []
    for entry in plan.entries:
        if getattr(entry.analyze_request, "dry_run", False):
            await rt.run_analysis_job(
                entry.job.job_id, entry.analyze_request, True, True, user_id, "model_arena"
            )
            status_value = str(rt.get_job(entry.job.job_id).get("status") or "completed")
        else:
            rt.create_task(
                rt.run_analysis_job(
                    entry.job.job_id, entry.analyze_request, True, True, user_id, "model_arena"
                )
            )
            status_value = "pending"
        jobs.append(entry.job.model_copy(update={"status": status_value}))
    return SubmittedArenaRun(
        experiment_id=plan.experiment_id,
        input_snapshot_hash=plan.input_snapshot_hash,
        symbol=plan.symbol,
        trade_date=plan.trade_date,
        jobs=tuple(jobs),
    )


def submit_model_arena_task(
    *,
    user_id: str,
    symbol: str,
    trade_date: str = "",
    horizons: Optional[Sequence[str]] = None,
    selected_analysts: Optional[Sequence[str]] = None,
    model_profile_ids: Optional[Sequence[str]] = None,
    query: str = "",
    runtime: Optional[JobRuntime] = None,
) -> SubmittedArenaRun:
    """对话工具入口：同一次对比、同一个准备序列（永不 `dry_run`，永不等待）。

    同步实现是刻意的：工具 handler 是同步函数，而这里**不需要**协程——投递本身
    （`runtime.create_task`）已经是"排上队就返回"。
    """
    from api.main import ModelArenaRunRequest

    rt = _resolve_runtime(runtime)
    request = ModelArenaRunRequest(
        symbol=symbol or "",
        trade_date=trade_date if trade_date else cn_today_str(),
        horizons=list(horizons) if horizons is not None else list(DEFAULT_HORIZONS),
        selected_analysts=(
            list(selected_analysts) if selected_analysts is not None else list(DEFAULT_ANALYSTS)
        ),
        model_profile_ids=list(model_profile_ids) if model_profile_ids is not None else [],
        query=(query or None),
        dry_run=False,
    )
    plan = _prepare_arena_run(request=request, user_id=user_id, runtime=rt)
    for entry in plan.entries:
        rt.create_task(
            rt.run_analysis_job(
                entry.job.job_id, entry.analyze_request, True, True, user_id, "model_arena"
            )
        )
    return SubmittedArenaRun(
        experiment_id=plan.experiment_id,
        input_snapshot_hash=plan.input_snapshot_hash,
        symbol=plan.symbol,
        trade_date=plan.trade_date,
        jobs=tuple(entry.job for entry in plan.entries),
    )
