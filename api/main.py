from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import traceback
from contextlib import asynccontextmanager
from io import StringIO
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from threading import Lock, Thread
import requests
from fastapi import Body
from typing import Any, Dict, List, Literal, Optional, Tuple
from uuid import uuid4

import logging
import time
from urllib.parse import urlparse

# Configure standard logging to include timestamps
logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

from dotenv import load_dotenv

# 优先加载仓库根目录的 .env；再按 cwd 查找（后者默认不覆盖已存在的变量）。
# 仅 load_dotenv() 时，若从子目录启动 uvicorn，会漏掉根目录 .env（如 TA_VLM_API_KEY）。
_REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_REPO_ROOT / ".env")
load_dotenv()

from fastapi import FastAPI, File, HTTPException, Depends, Query, Request, UploadFile, status, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, field_serializer
from sqlalchemy.orm import Session
from sqlalchemy.exc import OperationalError
import pandas as pd

from api.database import UserDB, UserLLMConfigDB, ScheduledAnalysisDB, VersionStatsDB, ReportDB, ImportedPortfolioPositionDB, WatchlistItemDB, FeedbackDB, DailyProductRunDB, DailyReviewDB, PaperPortfolioDB, init_db, get_db, get_db_ctx
from api.services import (
    auth_service,
    consensus_service,
    feedback_service,
    insights_t1_service,
    mainline_service,
    mainline_cycle_service,
    mainline_autodive_service,
    model_arena_service,
    portfolio_import_service,
    model_profile_service,
    recommendation_eval_service,
    recommendation_feedback_service,
    recommendation_service,
    report_quality_service,
    prompt_template_service,
    report_service,
    paper_trading_service,
    scheduled_service,
    stock_analysis_skill_service,
    stock_team_report_service,
    token_service,
    tracking_board_service,
    trade_ledger_service,
    trade_plan_service,
    watchlist_service,
)
from api.services.wecom_notification_service import send_message
from api.services.wps_notification_service import send_markdown_message

def _get_real_ip(request: Request) -> Optional[str]:
    """Extract real client IP, preferring Cloudflare/proxy headers."""
    if request is None:
        return None
    # Cloudflare Tunnel injects the real client IP here
    ip = request.headers.get("CF-Connecting-IP")
    if ip:
        return ip.strip()
    # Standard proxy header fallback
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.graph.data_collector import DataCollector

# 全局共享 DataCollector：同一 ticker+date 的数据只拉一次，所有 job 复用缓存
_shared_data_collector = DataCollector()
from tradingagents.dataflows.trade_calendar import cn_today_str
from tradingagents.dataflows.providers.cn_akshare_provider import set_scheduled_task_context
from tradingagents.dataflows.market_collector import get_shared_market_collector
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.interface import route_to_vendor
from tradingagents.graph.intent_parser import parse_intent as _parse_intent
from tradingagents.agents.utils.context_utils import USER_CONTEXT_KEYS, normalize_user_context
from tradingagents.agents.utils.agent_states import current_tracker_var
from tradingagents.agents.utils.direction import coerce_persistable_decision


_LOCAL_DEV_CORS_PORTS = (8000, 4173, 4174, 4175, 5173, 5174, 5175)


def _local_dev_cors_origins() -> list[str]:
    return [
        f"http://{host}:{port}"
        for host in ("127.0.0.1", "localhost")
        for port in _LOCAL_DEV_CORS_PORTS
    ]


def _cors_allow_origins() -> list[str]:
    raw = os.getenv("CORS_ALLOW_ORIGINS", "").strip()
    local_origins = _local_dev_cors_origins()
    if not raw:
        return local_origins
    origins = [item.strip() for item in raw.split(",") if item.strip()]
    # 开发态始终放行本地 Vite / preview，避免脚本用 4173 时登录被 CORS 拦下。
    if os.getenv("ENV", "").lower() != "prod":
        for origin in local_origins:
            if origin not in origins:
                origins.append(origin)
    return origins


def _cors_allow_origin_regex() -> str | None:
    raw = os.getenv("CORS_ALLOW_ORIGIN_REGEX", "").strip()
    if raw:
        return raw
    if os.getenv("ENV", "").lower() == "prod":
        return None
    # 覆盖脚本临时换端口（如 5176、4176）的本机开发场景
    return r"https?://(localhost|127\.0\.0\.1):(4|5|8)\d{3}$"


def _report_version_stats() -> None:
    """Report anonymous version stats to the official site."""
    import threading, uuid

    def _send():
        try:
            requests.post(
                "https://app.510168.xyz/api/version-stats",
                json={"v": APP_VERSION, "nonce": uuid.uuid4().hex},
                timeout=30,
            )
        except Exception:
            pass

    threading.Thread(target=_send, daemon=True).start()


_scheduler_task: Optional[asyncio.Task] = None
_stock_map_warm_task: Optional[asyncio.Task] = None
_mainline_scheduler_task: Optional[asyncio.Task] = None
_tracking_alert_task: Optional[asyncio.Task] = None
_recommendation_push_task: Optional[asyncio.Task] = None
_auto_daily_product_task: Optional[asyncio.Task] = None
_auto_daily_ops_task: Optional[asyncio.Task] = None
_strategy_feedback_task: Optional[asyncio.Task] = None
_auto_t1_eval_task: Optional[asyncio.Task] = None
_scheduled_analysis_max_concurrency = int(os.getenv("TA_SCHEDULED_ANALYSIS_MAX_CONCURRENCY", "10"))
_scheduled_analysis_semaphore: Optional[asyncio.Semaphore] = None
_scheduled_analysis_queue_lock: Optional[asyncio.Lock] = None
_scheduled_analysis_waiting_job_ids: list[str] = []
_scheduled_analysis_running_job_ids: set[str] = set()
_auto_daily_product_sent: dict[tuple[str, str], float] = {}
_auto_daily_ops_sent: dict[tuple[str, str], float] = {}


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _ensure_scheduled_analysis_queue() -> tuple[asyncio.Semaphore, asyncio.Lock]:
    global _scheduled_analysis_semaphore, _scheduled_analysis_queue_lock
    if _scheduled_analysis_semaphore is None:
        _scheduled_analysis_semaphore = asyncio.Semaphore(_scheduled_analysis_max_concurrency)
    if _scheduled_analysis_queue_lock is None:
        _scheduled_analysis_queue_lock = asyncio.Lock()
    return _scheduled_analysis_semaphore, _scheduled_analysis_queue_lock


async def _scheduled_analysis_acquire(job_id: str, symbol: str) -> None:
    semaphore, queue_lock = _ensure_scheduled_analysis_queue()

    async with queue_lock:
        _scheduled_analysis_waiting_job_ids.append(job_id)
        queue_position = _scheduled_analysis_waiting_job_ids.index(job_id) + 1
        running_count = len(_scheduled_analysis_running_job_ids)
        _set_job(
            job_id,
            queue_position=queue_position,
            waiting_ahead_count=max(0, queue_position - 1),
            scheduled_running_count=running_count,
            scheduled_concurrency_limit=_scheduled_analysis_max_concurrency,
        )
        _log(
            f"[Scheduled Queue] queued job={job_id} symbol={symbol} "
            f"waiting_position={queue_position} running={running_count}/{_scheduled_analysis_max_concurrency}"
        )

    await semaphore.acquire()

    async with queue_lock:
        if job_id in _scheduled_analysis_waiting_job_ids:
            _scheduled_analysis_waiting_job_ids.remove(job_id)
        _scheduled_analysis_running_job_ids.add(job_id)
        _set_job(
            job_id,
            queue_position=None,
            waiting_ahead_count=None,
            scheduled_running_count=len(_scheduled_analysis_running_job_ids),
            scheduled_concurrency_limit=_scheduled_analysis_max_concurrency,
        )
        _log(
            f"[Scheduled Queue] acquired slot job={job_id} symbol={symbol} "
            f"running={len(_scheduled_analysis_running_job_ids)}/{_scheduled_analysis_max_concurrency} "
            f"waiting={len(_scheduled_analysis_waiting_job_ids)}"
        )


async def _scheduled_analysis_release(job_id: str, symbol: str) -> None:
    semaphore, queue_lock = _ensure_scheduled_analysis_queue()

    async with queue_lock:
        _scheduled_analysis_running_job_ids.discard(job_id)
        if job_id in _scheduled_analysis_waiting_job_ids:
            _scheduled_analysis_waiting_job_ids.remove(job_id)
        _set_job(
            job_id,
            queue_position=None,
            waiting_ahead_count=None,
            scheduled_running_count=len(_scheduled_analysis_running_job_ids),
            scheduled_concurrency_limit=_scheduled_analysis_max_concurrency,
        )
        _log(
            f"[Scheduled Queue] released slot job={job_id} symbol={symbol} "
            f"running={len(_scheduled_analysis_running_job_ids)}/{_scheduled_analysis_max_concurrency} "
            f"waiting={len(_scheduled_analysis_waiting_job_ids)}"
        )

    semaphore.release()


@asynccontextmanager
async def _scheduled_analysis_slot(job_id: str, symbol: str):
    if _scheduled_analysis_max_concurrency <= 0:
        # 0 = 不限制并发，跳过 semaphore
        yield
        return
    await _scheduled_analysis_acquire(job_id, symbol)
    try:
        yield
    finally:
        await _scheduled_analysis_release(job_id, symbol)


async def _scheduler_loop():
    """Background loop: check every minute for scheduled tasks to trigger.

    Each task has its own trigger_time (HH:MM). The scheduler runs on trading
    days only and honors the configured trigger time as-is, including during
    trading hours. Tasks are triggered when current time >= task.trigger_time
    and the task hasn't run today yet.
    """
    from tradingagents.dataflows.trade_calendar import is_cn_trading_day
    from zoneinfo import ZoneInfo

    _log("[Scheduler] Started.")
    while True:
        await asyncio.sleep(60)
        try:
            now = datetime.now(tz=ZoneInfo("Asia/Shanghai"))
            today = now.strftime("%Y-%m-%d")
            current_hhmm = now.strftime("%H:%M")

            if not is_cn_trading_day(today):
                continue
            # 这里不再按交易时段跳过：用户可以设置任意触发时间，跳过会让
            # 08:01–19:59 的定时分析被静默延后到 20:00 才执行（用户以为没跑）。
            # 资源压力由 _scheduled_analysis_slot 的并发槽位控制。

            with get_db_ctx() as db:
                tasks = scheduled_service.get_pending_tasks(db, today, current_hhmm)
                if not tasks:
                    continue

                # 先标记为"已触发"防止下次循环重复启动
                for task in tasks:
                    task.last_run_date = today
                    task.last_run_status = "running"
                db.commit()

                # commit 后 SQLAlchemy 会 expire 属性，db.close() 后访问会抛
                # DetachedInstanceError，所以这里拷贝成 dict 传给 job 函数
                task_snapshots = [
                    {
                        "id": task.id,
                        "user_id": task.user_id,
                        "symbol": task.symbol,
                        "horizon": task.horizon,
                        "prompt_template_id": task.prompt_template_id,
                        "prompt_vars": task.prompt_vars_json or {},
                    }
                    for task in tasks
                ]

                _log(f"[Scheduler] Launching {len(task_snapshots)} tasks (staggered)")
                for i, snap in enumerate(task_snapshots):
                    if i > 0:
                        await asyncio.sleep(1)
                    _create_tracked_task(_run_scheduled_job(snap, today))

        except Exception as e:
            logger.error(f"[Scheduler] Error: {e}")


def _tracking_price_alerts_tick() -> None:
    from api.services import tracking_price_alert_service

    with get_db_ctx() as db:
        tracking_price_alert_service.run_tracking_price_alerts(db)
        # 双向提醒的第二半：跌破硬止损 / 触及分批止盈 / 移动止盈回撤 / 时间止损到期。
        # 过去只提醒"涨"的方向，真正决定盈亏的下跌方向没有任何提醒。
        try:
            tracking_price_alert_service.run_exit_plan_alerts(db)
        except Exception as e:
            logger.error("[ExitAlert] Error: %s", e)


async def _tracking_price_alert_loop() -> None:
    """交易时段内周期性检查跟踪标的：涨停、急拉，以及按交易计划的退出纪律提醒。"""
    from api.services import tracking_price_alert_service

    _log("[TrackingAlert] Started.")
    while True:
        await asyncio.sleep(max(30, tracking_price_alert_service.alert_poll_interval_sec()))
        try:
            await asyncio.to_thread(_tracking_price_alerts_tick)
        except Exception as e:
            logger.error("[TrackingAlert] Error: %s", e)


def _recommendation_push_tick() -> None:
    with get_db_ctx() as db:
        recommendation_service.run_scheduled_recommendation_pushes(db)


async def _recommendation_push_loop() -> None:
    _log("[RecommendationPush] Started.")
    while True:
        await asyncio.sleep(max(60, recommendation_service.recommendation_push_interval_sec()))
        try:
            await asyncio.to_thread(_recommendation_push_tick)
        except Exception as e:
            logger.error("[RecommendationPush] Error: %s", e)


def _auto_daily_product_enabled() -> bool:
    return str(os.getenv("TA_AUTO_DAILY_PRODUCT_ENABLED", "1")).strip().lower() not in (
        "0",
        "false",
        "no",
    )


def _auto_daily_product_interval_sec() -> int:
    return max(60, int(os.getenv("TA_AUTO_DAILY_PRODUCT_INTERVAL_SEC", "180")))


def _auto_daily_product_tick() -> None:
    from tradingagents.dataflows.trade_calendar import is_cn_trading_day
    from zoneinfo import ZoneInfo

    if not _auto_daily_product_enabled():
        return
    now = datetime.now(tz=ZoneInfo("Asia/Shanghai"))
    today = now.strftime("%Y-%m-%d")
    if not is_cn_trading_day(today):
        return
    phase = recommendation_service._in_push_window(now)
    if phase != "close":
        return

    top_k = max(1, min(int(os.getenv("TA_AUTO_DAILY_PRODUCT_TOP_K", "3")), 10))
    scan_limit_raw = int(os.getenv("TA_AUTO_DAILY_PRODUCT_SCAN_LIMIT", "1200"))
    scan_limit = max(50, min(scan_limit_raw, 5000))
    with get_db_ctx() as db:
        users = db.query(UserDB).filter(UserDB.is_active == True).all()  # noqa: E712
        for user in users:
            run_key = (user.id, f"{today}:{phase}")
            if run_key in _auto_daily_product_sent:
                continue
            cfg = auth_service.get_user_llm_config(db, user.id)
            wecom_hook = auth_service.decrypt_secret(getattr(cfg, "wecom_webhook_encrypted", None))
            wps_hook = auth_service.decrypt_secret(getattr(cfg, "wps_webhook_encrypted", None))
            if not wecom_hook and not wps_hook:
                continue
            request = DailyProductRunRequest(
                mode="recommended",
                top_k=top_k,
                candidate_limit=80,
                recommendation_source="market_scan",
                scan_limit=scan_limit,
                include_tracking=False,
                include_watchlist=False,
                min_change_pct=-1.0,
                market="cn",
                score_profile="ashare_balanced",
                use_backtest_feedback=True,
                strategy_mode="auto",
                horizons=["short"],
                ensure_scheduled=False,
            )
            _start_daily_product_run(request=request, user_id=user.id, db=db)
            _auto_daily_product_sent[run_key] = time.monotonic()


async def _auto_daily_product_loop() -> None:
    _log("[AutoDailyProduct] Started.")
    while True:
        await asyncio.sleep(_auto_daily_product_interval_sec())
        try:
            await asyncio.to_thread(_auto_daily_product_tick)
        except Exception as e:
            logger.error("[AutoDailyProduct] Error: %s", e)


def _auto_daily_ops_enabled() -> bool:
    return str(os.getenv("TA_AUTO_DAILY_OPS_ENABLED", "1")).strip().lower() not in (
        "0",
        "false",
        "no",
    )


def _auto_daily_ops_interval_sec() -> int:
    return max(60, int(os.getenv("TA_AUTO_DAILY_OPS_INTERVAL_SEC", "120")))


def _auto_daily_ops_include_recommendations() -> bool:
    return str(os.getenv("TA_AUTO_DAILY_OPS_INCLUDE_RECOMMENDATIONS", "1")).strip().lower() not in (
        "0",
        "false",
        "no",
    )


def _auto_daily_ops_recommendation_top_k() -> int:
    return max(1, min(int(os.getenv("TA_AUTO_DAILY_OPS_TOP_K", "3")), 10))


def _auto_daily_ops_auto_execute() -> bool:
    return str(os.getenv("TA_AUTO_DAILY_OPS_AUTO_EXECUTE", "1")).strip().lower() not in (
        "0",
        "false",
        "no",
    )


def _auto_daily_ops_target_minutes() -> int:
    raw = str(os.getenv("TA_AUTO_DAILY_OPS_TRIGGER_TIME", "14:30")).strip()
    try:
        hh, mm = raw.split(":", 1)
        hour = max(0, min(23, int(hh)))
        minute = max(0, min(59, int(mm)))
        return hour * 60 + minute
    except Exception:
        return 14 * 60 + 30


def _auto_daily_ops_window_minutes() -> int:
    return max(1, min(60, int(os.getenv("TA_AUTO_DAILY_OPS_WINDOW_MINUTES", "10"))))


def _in_daily_ops_window(now: datetime) -> bool:
    minutes = now.hour * 60 + now.minute
    trigger = _auto_daily_ops_target_minutes()
    window = _auto_daily_ops_window_minutes()
    return trigger <= minutes <= (trigger + window)


def _list_auto_daily_ops_user_ids(db: Session) -> list[str]:
    user_ids: list[str] = []
    users = db.query(UserDB).filter(UserDB.is_active == True).all()  # noqa: E712
    for user in users:
        has_imported = (
            db.query(ImportedPortfolioPositionDB.id)
            .filter(ImportedPortfolioPositionDB.user_id == user.id)
            .first()
            is not None
        )
        has_paper = (
            db.query(PaperPortfolioDB.id)
            .filter(PaperPortfolioDB.user_id == user.id)
            .first()
            is not None
        )
        if has_imported or has_paper:
            user_ids.append(user.id)
    return user_ids


def _execute_daily_operation_pipeline(
    *,
    db: Session,
    user_id: str,
    trade_date: str,
    include_recommendations: bool,
    recommendation_top_k: int,
    auto_execute: bool,
) -> dict[str, Any]:
    plan = paper_trading_service.generate_daily_operation_plan(
        db,
        user_id=user_id,
        trade_date=trade_date,
        include_recommendations=include_recommendations,
        recommendation_top_k=recommendation_top_k,
    )
    executed: list[dict[str, Any]] = []
    execute_errors: list[dict[str, Any]] = []
    if auto_execute:
        for action in plan.get("actions") or []:
            if action.get("action") not in {"BUY", "SELL"}:
                continue
            try:
                result = paper_trading_service.execute_trade(
                    db,
                    user_id=user_id,
                    symbol=str(action.get("symbol") or ""),
                    security_name=str(action.get("name") or "") or None,
                    side=str(action.get("action") or ""),
                    quantity=float(action.get("quantity") or 0.0),
                    price=_to_float(action.get("price")),
                    reason=str(action.get("reason") or "daily_ops_auto"),
                    trade_date=trade_date,
                )
                executed.append(result)
            except Exception as exc:
                execute_errors.append(
                    {
                        "symbol": action.get("symbol"),
                        "action": action.get("action"),
                        "error": str(exc),
                    }
                )
    review = paper_trading_service.create_daily_review(db, user_id=user_id, trade_date=trade_date)
    return {
        "plan": plan,
        "executed": executed,
        "execution_errors": execute_errors,
        "review": review,
    }


def _auto_daily_ops_tick() -> None:
    from tradingagents.dataflows.trade_calendar import is_cn_trading_day
    from zoneinfo import ZoneInfo

    if not _auto_daily_ops_enabled():
        return

    now = datetime.now(tz=ZoneInfo("Asia/Shanghai"))
    today = now.strftime("%Y-%m-%d")
    if not is_cn_trading_day(today):
        return
    if not _in_daily_ops_window(now):
        return

    with get_db_ctx() as db:
        user_ids = _list_auto_daily_ops_user_ids(db)
        for user_id in user_ids:
            run_key = (user_id, today)
            if run_key in _auto_daily_ops_sent:
                continue
            result = _execute_daily_operation_pipeline(
                db=db,
                user_id=user_id,
                trade_date=today,
                include_recommendations=_auto_daily_ops_include_recommendations(),
                recommendation_top_k=_auto_daily_ops_recommendation_top_k(),
                auto_execute=_auto_daily_ops_auto_execute(),
            )
            _auto_daily_ops_sent[run_key] = time.monotonic()
            _log(
                f"[AutoDailyOps] user={user_id} trade_date={today} "
                f"actions={len((result.get('plan') or {}).get('actions') or [])} "
                f"executed={len(result.get('executed') or [])}"
            )


async def _auto_daily_ops_loop() -> None:
    _log("[AutoDailyOps] Started.")
    while True:
        await asyncio.sleep(_auto_daily_ops_interval_sec())
        try:
            await asyncio.to_thread(_auto_daily_ops_tick)
        except Exception as e:
            logger.error("[AutoDailyOps] Error: %s", e)


def _strategy_feedback_tick() -> None:
    with get_db_ctx() as db:
        recommendation_feedback_service.refresh_feedback_pipeline(
            db,
            user_id=None,
            eval_limit=120,
            retries=3,
        )
        db.commit()


async def _strategy_feedback_loop() -> None:
    interval = max(300, int(os.getenv("TA_STRATEGY_FEEDBACK_INTERVAL_SEC", "900")))
    _log("[StrategyFeedback] Started.")
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(_strategy_feedback_tick)
        except Exception as e:
            logger.error("[StrategyFeedback] Error: %s", e)


# date → bool: already ran auto T+1 eval for that date
_auto_t1_eval_done: dict[str, bool] = {}


def _auto_t1_eval_tick() -> None:
    """After market close (≥ 15:10 CST) evaluate T+1 for every active user once per day.

    The 10-minute buffer gives data providers time to publish final close prices.
    Env knobs:
      TA_AUTO_T1_EVAL_ENABLED   – set to 0/false/no to disable (default: enabled)
      TA_AUTO_T1_EVAL_AFTER_MIN – minutes after midnight to start (default: 910 = 15:10)
    """
    if str(os.getenv("TA_AUTO_T1_EVAL_ENABLED", "1")).strip().lower() in ("0", "false", "no"):
        return

    from tradingagents.dataflows.trade_calendar import is_cn_trading_day
    from zoneinfo import ZoneInfo

    now = datetime.now(tz=ZoneInfo("Asia/Shanghai"))
    today = now.strftime("%Y-%m-%d")

    if not is_cn_trading_day(today):
        return

    after_min = int(os.getenv("TA_AUTO_T1_EVAL_AFTER_MIN", "910"))  # 15:10 default
    current_min = now.hour * 60 + now.minute
    if current_min < after_min:
        return

    if _auto_t1_eval_done.get(today):
        return

    with get_db_ctx() as db:
        users = db.query(UserDB).filter(UserDB.is_active == True).all()  # noqa: E712
        evaluated_users = 0
        for user in users:
            try:
                result = insights_t1_service.refresh_all_t1(db, user_id=user.id)
                db.commit()
                total_eval = (result.get("market_scan", {}) or {}).get("evaluated", 0) + \
                             (result.get("reports", {}) or {}).get("evaluated", 0)
                if total_eval > 0:
                    evaluated_users += 1
            except Exception as e:
                logger.error("[AutoT1Eval] user=%s error: %s", user.id, e)

    _auto_t1_eval_done[today] = True
    _log(f"[AutoT1Eval] {today} done — {evaluated_users}/{len(users)} users had new evaluations.")


async def _auto_t1_eval_loop() -> None:
    """Check every 5 minutes whether it's time to run auto T+1 evaluation."""
    interval = max(60, int(os.getenv("TA_AUTO_T1_EVAL_INTERVAL_SEC", "300")))
    _log("[AutoT1Eval] Started.")
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(_auto_t1_eval_tick)
        except Exception as e:
            logger.error("[AutoT1Eval] Error: %s", e)


def _resolve_scheduled_trade_date(trade_date: str) -> str:
    """Use the requested trading day, or fall back to the latest CN trading day."""
    from tradingagents.dataflows.trade_calendar import is_cn_trading_day, previous_cn_trading_day

    return trade_date if is_cn_trading_day(trade_date) else previous_cn_trading_day(trade_date)


def _build_scheduled_analyze_request(
    db: Session,
    user_id: str,
    symbol: str,
    horizon: str,
    trade_date: str,
    scheduled_user_context: Optional[Dict[str, Any]] = None,
    prompt_template_id: Optional[str] = None,
    prompt_vars: Optional[Dict[str, Any]] = None,
) -> "AnalyzeRequest":
    # 同上：定时分析也不注入导入持仓，保持分析客观。
    scheduled_user_context = scheduled_user_context or {}
    code_to_name = _get_reverse_stock_map()
    template = prompt_template_service.resolve_template(
        db,
        user_id,
        prompt_template_id or prompt_template_service.DEFAULT_SCHEDULED_TEMPLATE_ID,
        fallback_template_id=prompt_template_service.DEFAULT_SCHEDULED_TEMPLATE_ID,
    )
    rendered = prompt_template_service.render_template(
        template,
        symbol=symbol,
        name=code_to_name.get(symbol, symbol),
        horizon=horizon,
        trade_date=trade_date,
        prompt_vars=prompt_vars or {},
        user_context=scheduled_user_context,
    )
    return AnalyzeRequest(
        symbol=symbol,
        trade_date=trade_date,
        horizons=[horizon],
        query=rendered["query"],
        user_intent=rendered["user_intent"],
        prompt_template_id=template["id"],
        prompt_vars=prompt_vars or {},
        objective=scheduled_user_context.get("objective"),
        # 注意：定时分析不再携带持仓/成本，避免模型围绕用户持仓作答而引入噪音。
        # 建仓/减仓/清仓的判断交给独立的确定性引擎，见 api/services/exit_engine_service.py。
        user_notes=scheduled_user_context.get("user_notes"),
    )


def _backfill_plan_anchors(result: Dict[str, Any]) -> None:
    """用交易计划回填研报缺失的止盈/止损锚点。

    生产库实测：3922 份研报正文提到止盈，但只有 532 份成功结构化出 target_price
    （约 86% 的止盈锚点被丢弃）；止损 3967 份提到 vs 2432 份结构化成功。计划块里的
    硬止损与分批止盈第一档就是这份研报自己的锚点，用它回填可以把丢失率大幅压低，
    也让下游（退出引擎、预警、复盘）拿得到可比较的数字。
    """
    try:
        plan, source, _ = trade_plan_service.compose_trade_plan(
            decision_text=str(result.get("final_trade_decision") or ""),
            trader_plan_text=str(result.get("trader_investment_plan") or ""),
            risk_feedback_state=result.get("risk_feedback_state"),
            target_price=result.get("target_price"),
            stop_loss_price=result.get("stop_loss_price"),
        )
    except Exception as exc:
        _log(f"TradePlan anchor backfill failed (non-fatal): {exc}")
        return

    if not plan:
        return

    if result.get("stop_loss_price") is None and plan.get("hard_stop_price"):
        result["stop_loss_price"] = float(plan["hard_stop_price"])
        result["stop_loss_source"] = "trade_plan"
    if result.get("target_price") is None:
        # 取止盈阶梯的最高档并做方向校验。旧实现取 ladder[0][0]，那是按价格升序
        # 排列后的**最低**一档减仓位，被当成目标价后产出大量"看多 + 目标价低于
        # 现价"的自相矛盾记录。语义上目标价是"这份研报想看到的价格"，即最高档。
        target = trade_plan_service.plan_target_price(plan)
        if target is not None:
            result["target_price"] = target
            result["target_price_source"] = "trade_plan"

    result["trade_plan"] = plan
    result["trade_plan_source"] = source


def _persist_trade_plan_from_result(
    save_db,
    *,
    user_id: Optional[str],
    job_id: str,
    request,
    result: Dict[str, Any],
    horizon: Optional[str] = None,
) -> None:
    """把研报里的交易计划落成可监控的一等对象（失败不影响主流程）。

    过去方向/入场/止损/分批止盈/时间止损只以散文形式存在于决策文本里：生产库实测
    提到"止盈"的研报 3922 份而结构化目标价只有 532 份（约 86% 锚点丢失），导致
    这些计划既不能被监控、也不能触发预警、更无法复盘。这里把计划持久化，供
    api/services/exit_engine_service.py 与双向预警使用。
    """
    try:
        trade_plan_service.persist_plan_from_analysis(
            save_db,
            user_id=user_id,
            report_id=job_id,
            symbol=request.symbol,
            signal_trade_date=request.trade_date,
            decision_text=str(result.get("final_trade_decision") or ""),
            trader_plan_text=str(result.get("trader_investment_plan") or ""),
            risk_feedback_state=result.get("risk_feedback_state"),
            target_price=result.get("target_price"),
            stop_loss_price=result.get("stop_loss_price"),
            risk_gate=(result.get("consensus_summary") or {}).get("risk_gate"),
            confidence=result.get("confidence"),
            horizon=horizon,
        )
    except Exception as exc:  # 计划落库是增强而非主链路，绝不因此丢报告
        _log(f"TradePlan persistence failed (non-fatal): {exc}")


async def _send_scheduled_report_notifications(
    user_id: str,
    report_id: str,
    symbol: str,
    *,
    background: bool = True,
    log_prefix: str = "Scheduler",
) -> None:
    """
    Send configured report notifications (email + WeCom + WPS) when enabled.
    When background=True, delivery runs as tracked background tasks (scheduled analysis).
    When background=False, wait for this symbol's channels before returning (e.g. daily product batch after summary).
    """
    try:
        from api.services.email_report_service import send_report_email_with_retry
        from api.services.wecom_notification_service import send_report_message_with_retry
        from api.services.wps_notification_service import send_report_markdown_with_retry

        email_user = None
        report_to_send = None
        webhook_url = None
        wps_webhook_url = None
        wecom_report_enabled = True
        wps_report_enabled = True
        with get_db_ctx() as db:
            user = db.query(UserDB).filter(UserDB.id == user_id).first()
            report = db.query(ReportDB).filter(ReportDB.id == report_id).first()
            user_cfg = auth_service.get_user_llm_config(db, user_id)
            webhook_url = auth_service.decrypt_secret(getattr(user_cfg, "wecom_webhook_encrypted", None))
            wps_webhook_url = auth_service.decrypt_secret(getattr(user_cfg, "wps_webhook_encrypted", None))
            if report:
                db.expunge(report)
                report_to_send = report
            if user:
                wecom_report_enabled = getattr(user, "wecom_report_enabled", True)
                wps_report_enabled = getattr(user, "wps_report_enabled", True)
                if getattr(user, "email_report_enabled", True):
                    db.expunge(user)
                    email_user = user

        to_run: list[tuple[str, Any]] = []
        if email_user and report_to_send:
            _log(f"[{log_prefix}] Sending email report for {symbol} to {email_user.email}")
            to_run.append(
                (
                    "email",
                    send_report_email_with_retry(email_user, report_to_send),
                )
            )
        if report_to_send and webhook_url and wecom_report_enabled:
            _log(f"[{log_prefix}] Sending WeCom report for {symbol}")
            code_to_name = _get_reverse_stock_map()
            wecom_stock_name = code_to_name.get(symbol, symbol)
            to_run.append(
                (
                    "wecom",
                    send_report_message_with_retry(report_to_send, webhook_url, wecom_stock_name),
                )
            )
        if report_to_send and wps_webhook_url and wps_report_enabled:
            _log(f"[{log_prefix}] Sending WPS协作 report for {symbol}")
            code_to_name = _get_reverse_stock_map()
            wps_stock_name = code_to_name.get(symbol, symbol)
            to_run.append(
                (
                    "wps",
                    send_report_markdown_with_retry(report_to_send, wps_webhook_url, wps_stock_name),
                )
            )
        if not to_run:
            return
        if background:
            for label, coro in to_run:
                _create_tracked_task(
                    coro,
                    label=f"{log_prefix} {label} notification ({symbol})",
                )
        else:
            results = await asyncio.gather(*[c for _, c in to_run], return_exceptions=True)
            for (label, _), res in zip(to_run, results):
                if isinstance(res, BaseException):
                    logger.warning(
                        f"[{log_prefix}] {label} notification failed for {symbol}: {res}",
                    )
    except Exception as e:
        logger.warning(f"[{log_prefix}] Notification send failed for {symbol}: {e}")


async def _run_scheduled_analysis_once(
    task: dict,
    requested_trade_date: str,
    job_id: str,
    *,
    mark_schedule_run: bool,
) -> None:
    """Execute one scheduled analysis, optionally recording it as the daily scheduled run."""
    task_id = task["id"]
    user_id = task["user_id"]
    symbol = task["symbol"]
    horizon = task.get("horizon") or "short"

    _ensure_job_event_queue(job_id)
    actual_trade_date = _resolve_scheduled_trade_date(requested_trade_date)
    _log(f"[Scheduler] {symbol} trade_date={actual_trade_date} (requested={requested_trade_date})")

    # 标记当前上下文为定时任务，akshare 并发锁会据此限制槽位，为前端保留带宽
    set_scheduled_task_context(True)
    try:
        async with _scheduled_analysis_slot(job_id, symbol):
            with get_db_ctx() as db:
                # 定时分析不注入导入持仓：保持分析客观，减仓/卖出交给独立退出引擎。
                scheduled_user_context = task.get("manual_user_context") or {}
                req = _build_scheduled_analyze_request(
                    db=db,
                    user_id=user_id,
                    symbol=symbol,
                    horizon=horizon,
                    trade_date=actual_trade_date,
                    scheduled_user_context=scheduled_user_context,
                    prompt_template_id=task.get("prompt_template_id"),
                    prompt_vars=task.get("prompt_vars") or {},
                )

            await _run_job(job_id, req, False, True, user_id, "scheduled" if mark_schedule_run else "scheduled_manual")
        job_state = _get_job(job_id)
        if job_state.get("status") == "failed":
            raise RuntimeError(job_state.get("error") or f"scheduled analysis job {job_id} failed")
        with get_db_ctx() as db:
            if mark_schedule_run:
                scheduled_service.mark_run_success(db, task_id, requested_trade_date, job_id)
            else:
                scheduled_service.record_manual_test_result(db, task_id, "success", report_id=job_id)
        _log(f"[Scheduler] Completed {symbol}")

        await _send_scheduled_report_notifications(user_id, job_id, symbol)
    except Exception as e:
        logger.error(f"[Scheduler] Failed {symbol}: {e}\n{traceback.format_exc()}")
        with get_db_ctx() as db:
            if mark_schedule_run:
                scheduled_service.mark_run_failed(db, task_id, requested_trade_date)
            else:
                scheduled_service.record_manual_test_result(db, task_id, "failed")


async def _run_scheduled_job(task: dict, trade_date: str):
    """Execute a single scheduled analysis job.

    Args:
        task: dict with keys id, user_id, symbol, horizon (plain values,
              not an ORM instance, to avoid DetachedInstanceError).
        trade_date: YYYY-MM-DD string.
    """
    user_id = task["user_id"]
    symbol = task["symbol"]

    _log(f"[Scheduler] Running {symbol} for user={user_id}")
    job_id = uuid4().hex
    try:
        await _run_scheduled_analysis_once(
            task,
            trade_date,
            job_id,
            mark_schedule_run=True,
        )
    finally:
        _job_events.pop(job_id, None)


async def _mainline_scheduler_loop() -> None:
    """每日收盘后自动生成市场主线报告（默认 15:35，交易日触发，可用 MAINLINE_SCHEDULE_HHMM 覆盖）。"""
    from tradingagents.dataflows.trade_calendar import is_cn_trading_day

    hhmm = os.getenv("MAINLINE_SCHEDULE_HHMM", "15:35").strip()
    try:
        hour, minute = int(hhmm.split(":")[0]), int(hhmm.split(":")[1])
    except Exception:
        hour, minute = 15, 35
    while True:
        now = datetime.now()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        today = datetime.now().strftime("%Y-%m-%d")
        if not is_cn_trading_day(today):
            continue
        job_id = uuid4().hex
        now_iso = _utcnow_iso()
        _log(f"[Mainline Scheduler] auto-run {today} (job={job_id})")
        _set_job(
            job_id,
            job_id=job_id,
            user_id="system",
            status="pending",
            created_at=now_iso,
            symbol="MAINLINE",
            trade_date=today,
        )
        _ensure_job_event_queue(job_id)
        _emit_job_event(
            job_id, "job.created", {"job_id": job_id, "type": "mainline", "trade_date": today, "source": "scheduler"}
        )
        with get_db_ctx() as db:
            mainline_service.create_run(
                db, user_id="system", run_id=job_id, trade_date=today, perspective="short", job_id=job_id
            )
        _create_tracked_task(
            mainline_service.run_mainline_job_with_timeout(
                job_id,
                "system",
                today,
                "short",
                set_job=_set_job,
                emit_event=_emit_job_event,
                market_collector=get_shared_market_collector(),
            ),
            label=f"mainline scheduled {job_id}",
        )
        # 闭环：顺带评估既往主线报告的 T+1 兑现（昨日主线今日是否兑现）
        try:
            with get_db_ctx() as db:
                mainline_service.refresh_t1_outcomes(db, user_id="system")
        except Exception as exc:
            _log(f"[Mainline Scheduler] T+1 refresh failed: {type(exc).__name__}: {exc}")

        # 周期分析 + 自动深挖（识别 → 周期 → 决策 → 可买入筛选 → 自动创建个股深度分析任务）
        try:
            with get_db_ctx() as db:
                mainline_cycle_service.run_cycle_analysis(db, today, user_id="system")
                mainline_autodive_service.run_autodive(
                    db, today, user_id="system",
                    create_task=lambda sym: _create_mainline_deep_dive_task("system", sym),
                )
                mainline_autodive_service.reconcile_deep_dive_statuses(db, user_id="system")
                mainline_cycle_service.verify_decisions(db, user_id="system")
                # 主线验证闭环回填（P2-2）：对评估窗口已关闭的决策补算 outcome / forward_excess_ret。
                # 按用户隔离匹配 T+1 兑现记录，并在缺乏兑现数据时按需补建（user_id=None → 覆盖所有用户）。
                mainline_cycle_service.backfill_decision_outcomes(db)
        except Exception as exc:
            _log(f"[Mainline Scheduler] cycle/autodive failed: {type(exc).__name__}: {exc}")


async def _warm_stock_map() -> None:
    """Preload the A-share name map without blocking startup or stranding a thread.

    The load runs on a daemon thread (_schedule_stock_map_refresh), so a slow
    upstream can never pin a request handler or a thread-pool worker. The old
    asyncio.wait_for() approach gave up but could not cancel the worker, which is
    what left a zombie thread holding the map lock and an AkShare permit.
    """
    _ensure_stock_map_warm_start()
    if _stock_map_is_fresh(time.time()):
        _log(
            f"[StockMap] warm start is fresh and complete "
            f"({len(_cn_stock_map or {})} names); skipping the slow upstream refresh."
        )
        return
    if _cn_stock_map:
        _log(
            f"[StockMap] warm start has {len(_cn_stock_map)} names but is incomplete "
            "(missing a market); refreshing from upstream in the background."
        )
    _schedule_stock_map_refresh()
    # NOTE: compare wall-clock time here — _cn_stock_map_failed_at is a time.time()
    # epoch, so passing time.monotonic() would make the backoff test always true.
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if _cn_stock_map or _stock_map_in_backoff(time.time()):
            break
        await asyncio.sleep(0.5)
    if _cn_stock_map:
        _log(f"Stock map pre-loaded on startup ({len(_cn_stock_map)} entries).")
    elif _stock_map_in_backoff(time.time()):
        _log("[StockMap] startup load failed; backing off and will retry in the background.")
    else:
        _log(
            "[StockMap] startup preload still running; requests use stock codes until it lands."
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize resources on startup and cleanup on shutdown."""
    global _scheduler_task, _stock_map_warm_task, _mainline_scheduler_task, _tracking_alert_task, _recommendation_push_task, _auto_daily_product_task, _auto_daily_ops_task, _strategy_feedback_task, _auto_t1_eval_task, _scheduled_analysis_semaphore, _scheduled_analysis_queue_lock, _executor, _light_executor, _market_executor
    if getattr(_executor, "_shutdown", False):
        _executor = ThreadPoolExecutor(max_workers=int(os.getenv("TA_MAX_WORKERS", "2")))
    if getattr(_light_executor, "_shutdown", False):
        _light_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="api-light")
    if getattr(_market_executor, "_shutdown", False):
        _market_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="market")
    init_db()
    _log("Database initialized.")
    with _jobs_lock:
        _jobs.clear()
        _job_events.clear()
    _background_tasks.clear()
    _scheduled_analysis_semaphore = asyncio.Semaphore(_scheduled_analysis_max_concurrency)
    _scheduled_analysis_queue_lock = asyncio.Lock()
    _scheduled_analysis_waiting_job_ids.clear()
    _scheduled_analysis_running_job_ids.clear()
    _auto_daily_product_sent.clear()
    _auto_daily_ops_sent.clear()
    _log(f"[Scheduled Queue] Concurrency limit set to {_scheduled_analysis_max_concurrency}")

    # Security: warn loudly if using default secret key
    if not os.getenv("TA_APP_SECRET_KEY"):
        _log("=" * 70)
        _log("WARNING: TA_APP_SECRET_KEY is not set!")
        _log("Using hardcoded default key. ALL encryption and JWT signing")
        _log("is INSECURE. Set TA_APP_SECRET_KEY env var before production use.")
        _log("=" * 70)

    _report_version_stats()
    # 启动时把卡在 running 的定时任务重置
    with get_db_ctx() as db:
        stale = db.query(ScheduledAnalysisDB).filter(
            ScheduledAnalysisDB.last_run_status == "running"
        ).all()
        if stale:
            recovered_count = 0
            reset_count = 0
            for item in stale:
                # 检查是否已有对应的成功报告（任务实际完成了但状态卡在 running）
                # 必须同时校验报告是本次运行产生的（created_at >= last_run_date），
                # 否则 last_report_id 指向的旧报告会导致假成功。
                has_report = (
                    item.last_report_id
                    and item.last_run_date
                    and db.query(ReportDB).filter(
                        ReportDB.id == item.last_report_id,
                        ReportDB.status == "completed",
                        ReportDB.created_at >= item.last_run_date,
                    ).first()
                )
                if has_report:
                    item.last_run_status = "success"
                    # 保留 last_run_date，不重新触发
                    recovered_count += 1
                else:
                    item.last_run_status = "stale"
                    item.last_run_date = None  # 真正未完成，允许重新触发
                    reset_count += 1
            db.commit()
            _log(
                f"[Scheduler] Reset {len(stale)} stale 'running' tasks on startup "
                f"(recovered={recovered_count}, reset_to_stale={reset_count})."
            )
        report_reset = report_service.recover_stale_active_reports(db)
        if report_reset["total"]:
            _log(
                "[Reports] Recovered %s stale active reports on startup (marked failed)."
                % report_reset["total"]
            )
        mainline_reset = mainline_service.recover_stale_runs(db)
        if mainline_reset:
            _log(
                "[Mainline] Recovered %s stale pending/running runs on startup (marked failed)."
                % mainline_reset
            )
    # Pre-load trade calendar (uses mini_racer/V8 which is not thread-safe)
    from tradingagents.dataflows.trade_calendar import _load_cn_trade_dates
    _load_cn_trade_dates()
    _log("Trade calendar pre-loaded.")
    # Stock map 走 akshare，可能阻塞数十秒。后台预热，避免卡住 /healthz 与登录接口。
    # Warm-start from the on-disk cache first (fast, local) so the first requests
    # after a restart already resolve Chinese names instead of waiting on AkShare.
    _ensure_stock_map_warm_start()
    _stock_map_warm_task = asyncio.create_task(_warm_stock_map())
    _scheduler_task = asyncio.create_task(_scheduler_loop())
    _mainline_scheduler_task = asyncio.create_task(_mainline_scheduler_loop())
    _tracking_alert_task = asyncio.create_task(_tracking_price_alert_loop())
    _recommendation_push_task = asyncio.create_task(_recommendation_push_loop())
    _auto_daily_product_task = asyncio.create_task(_auto_daily_product_loop())
    _auto_daily_ops_task = asyncio.create_task(_auto_daily_ops_loop())
    _strategy_feedback_task = asyncio.create_task(_strategy_feedback_loop())
    _auto_t1_eval_task = asyncio.create_task(_auto_t1_eval_loop())
    yield
    _log("Shutting down: Cleaning up resources...")
    if _stock_map_warm_task:
        _stock_map_warm_task.cancel()
    if _scheduler_task:
        _scheduler_task.cancel()
    if _mainline_scheduler_task:
        _mainline_scheduler_task.cancel()
    if _tracking_alert_task:
        _tracking_alert_task.cancel()
    if _recommendation_push_task:
        _recommendation_push_task.cancel()
    if _auto_daily_product_task:
        _auto_daily_product_task.cancel()
    if _auto_daily_ops_task:
        _auto_daily_ops_task.cancel()
    if _strategy_feedback_task:
        _strategy_feedback_task.cancel()
    if _auto_t1_eval_task:
        _auto_t1_eval_task.cancel()
    _executor.shutdown(wait=False, cancel_futures=True)
    _light_executor.shutdown(wait=False, cancel_futures=True)
    _market_executor.shutdown(wait=False, cancel_futures=True)
    _log("Executor shutdown complete.")


_is_prod = os.getenv("ENV", "").lower() == "prod"


def _get_version() -> str:
    """Get app version: APP_VERSION env > package metadata > 'dev'."""
    v = os.getenv("APP_VERSION")
    if v:
        return v
    try:
        from importlib.metadata import version as pkg_version
        for dist_name in ("alphapilot-ashare", "tradingagents"):
            try:
                return pkg_version(dist_name)
            except Exception:
                continue
        return "dev"
    except Exception:
        return "dev"


APP_VERSION = _get_version()

app = FastAPI(
    title="AlphaPilot A-Share API",
    version=APP_VERSION,
    lifespan=lifespan,
    docs_url=None if _is_prod else "/docs",
    redoc_url=None if _is_prod else "/redoc",
    openapi_url=None if _is_prod else "/openapi.json",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_allow_origins(),
    allow_origin_regex=_cors_allow_origin_regex(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_executor = ThreadPoolExecutor(max_workers=int(os.getenv("TA_MAX_WORKERS", "2")))
# 登录/健康检查专用线程池，避免被 akshare 等同步任务占满默认 anyio 线程池。
_light_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="api-light")
_market_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="market")
_jobs_lock = Lock()
_jobs: Dict[str, Dict[str, Any]] = {}
_daily_product_runs_lock = Lock()
_daily_product_runs: Dict[str, Dict[str, Any]] = {}

# Runtime config overrides via PATCH /v1/config
_global_config_overrides: Dict[str, Any] = {}

# Allowlist for config_overrides from client requests.
# Security: prevents injection of api_key, backend_url, or other sensitive keys.
_CONFIG_OVERRIDES_ALLOWLIST = {
    "llm_provider", "deep_think_llm", "quick_think_llm",
    "llm_temperature",
    "max_debate_rounds", "max_risk_discuss_rounds",
    "decision_critic_enabled", "decision_critic_revision_threshold",
    "prompt_language",
    "methodology_ashare_fundamentals", "methodology_ashare_news_events", "methodology_ashare_sector_macro",
    "methodology_stock_analysis_team",
    "methodology_extra_path", "methodology_extra_path_news", "methodology_extra_path_macro",
    "finskills_root", "stock_analysis_team_root",
}
_job_events: Dict[str, "asyncio.Queue[Dict[str, Any]]"] = {}
# Hold references to fire-and-forget tasks so they are not garbage collected
_background_tasks: set = set()

_MODEL_RUNTIME_KEYS = {"llm_provider", "backend_url", "quick_think_llm", "deep_think_llm", "api_key"}
_DEEPSEEK_ALLOWED_MODELS = {
    "deepseek-chat",       # DeepSeek-V3.2 对话
    "deepseek-reasoner",   # DeepSeek-V3.2 推理
    "deepseek-v4-flash",
    "deepseek-v4-pro",
}
_DEEPSEEK_FALLBACK_MODEL = "deepseek-v4-flash"

# ── A-share stock name → code cache ──────────────────────────────────────────
_cn_stock_map: Optional[Dict[str, str]] = None  # name -> "XXXXXX.SH/SZ"
_cn_stock_reverse_map: Optional[Dict[str, str]] = None  # code -> name
# Stock and fund names are tracked separately because their 6-digit code spaces
# overlap: 000001 is both 平安银行 and the OTC fund 华夏成长混合. Keeping
# provenance lets the reverse map always resolve a code to the real stock.
_cn_stock_names: Dict[str, str] = {}  # name -> code, exchange listings only
_cn_fund_names: Dict[str, str] = {}  # name -> code, ETFs / OTC funds only
# NOTE: the maps themselves are deliberately NOT lock-guarded. Readers only do
# dict lookups (atomic under the GIL), and the old _cn_stock_map_lock was held
# across a multi-minute AkShare fetch — that is exactly what froze every request
# needing a stock name. Fetch serialisation now lives in
# _cn_stock_map_fetch_lock, which readers never touch.


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_t1_window_datetime(raw: Optional[str]) -> Optional[datetime]:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return dt.astimezone(timezone.utc)


def _default_t1_refresh_window() -> tuple[datetime, datetime]:
    from tradingagents.dataflows.trade_calendar import previous_cn_trading_day

    tz_cn = ZoneInfo("Asia/Shanghai")
    today = cn_today_str()
    prev_day = previous_cn_trading_day(today)
    start_local = datetime.fromisoformat(f"{prev_day}T15:00:00").replace(tzinfo=tz_cn)
    end_local = datetime.fromisoformat(f"{today}T15:00:00").replace(tzinfo=tz_cn)
    return (start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc))


_JOB_TIMEOUT = int(os.getenv("TA_JOB_TIMEOUT", "1800"))  # seconds
def _create_tracked_task(coro, *, label: str = "Background task") -> asyncio.Task:
    """Create an asyncio task and keep a reference to prevent GC.
    Also logs unhandled exceptions via a done callback."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)

    def _on_done(t: asyncio.Task):
        _background_tasks.discard(t)
        if not t.cancelled() and t.exception():
            logger.error("%s failed: %s", label, t.exception())

    task.add_done_callback(_on_done)
    return task


def _log(msg: str):
    """Helper to log with timestamp via standard logging."""
    logger.info(msg)


def _serialize_datetime_utc(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat()


_cn_stock_map_loaded_at: float = 0  # timestamp of last SUCCESSFUL load
_cn_stock_map_failed_at: float = 0  # timestamp of last FAILED load (negative cache)
_cn_stock_map_refresh_lock = Lock()
_cn_stock_map_refreshing = False
_stock_map_cold_warned = False
_stock_map_disk_lock = Lock()
_stock_map_disk_loaded = False
_STOCK_MAP_TTL = 7 * 86400  # 7 days
# A failed upstream fetch must never be retried on every call: doing so turned a
# slow/unreachable AkShare into a hang inside request handlers. Back off instead.
_STOCK_MAP_RETRY_BACKOFF = int(os.getenv("TA_STOCK_MAP_RETRY_BACKOFF", "300"))
# The upstream name list takes MINUTES to fetch (measured: ~7 min for ~32k names),
# so the map is persisted to disk and warm-started on boot. Without this, every
# restart serves raw stock codes until the slow fetch lands.
_STOCK_MAP_CACHE_FILE = Path(
    os.getenv("TA_STOCK_MAP_CACHE_FILE", str(_REPO_ROOT / "results" / "cache" / "cn_stock_map.json"))
)


def _stock_map_covers_all_markets() -> bool:
    """True when both SH and SZ listings are present.

    A cache holding only SZ names is NOT good enough: the SSE endpoint that
    supplies SH names is slow and flaky, so a run that loses it would otherwise
    look "fresh" for 7 days and never retry — leaving every 6xxxxx symbol showing
    its code instead of its Chinese name.
    """
    codes = set(_cn_stock_names.values())
    return any(c.endswith(".SH") for c in codes) and any(c.endswith(".SZ") for c in codes)


def _stock_map_is_fresh(now: float) -> bool:
    """True when a successful, COMPLETE load is cached and still within its TTL."""
    return (
        _cn_stock_map is not None
        and _cn_stock_map_loaded_at > 0
        and (now - _cn_stock_map_loaded_at) <= _STOCK_MAP_TTL
        and _stock_map_covers_all_markets()
    )


def _stock_map_in_backoff(now: float) -> bool:
    """True while a recent failure suppresses further upstream attempts."""
    return (
        _cn_stock_map_failed_at > 0
        and (now - _cn_stock_map_failed_at) < _STOCK_MAP_RETRY_BACKOFF
    )


def _warn_stock_map_cold() -> None:
    """Warn once per cold period that names are degrading to raw symbols."""
    global _stock_map_cold_warned
    if _stock_map_cold_warned:
        return
    _stock_map_cold_warned = True
    _log(
        "[StockMap] name map unavailable — stock names fall back to codes until a "
        "background refresh succeeds."
    )


def _as_name_map(raw: Any) -> Dict[str, str]:
    """Coerce a decoded JSON object into a {name: code} mapping."""
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if str(k).strip() and str(v).strip()}


def _read_stock_map_from_disk() -> tuple[Dict[str, str], Dict[str, str], float]:
    """Return (stock_names, fund_names, loaded_at) from the persisted cache.

    Kept separate so the reverse map can always let a real stock beat an OTC fund
    that shares its code. Never raises.
    """
    try:
        if not _STOCK_MAP_CACHE_FILE.exists():
            return {}, {}, 0.0
        with _STOCK_MAP_CACHE_FILE.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        if not isinstance(payload, dict):
            return {}, {}, 0.0
        loaded_at = float(payload.get("loaded_at") or 0.0)
        stocks = _as_name_map(payload.get("stocks"))
        funds = _as_name_map(payload.get("funds"))
        if not stocks and not funds:
            # Back-compat with the first cache format, which stored one merged map
            # with no provenance.
            legacy = _as_name_map(payload.get("map"))
            if legacy:
                return legacy, {}, loaded_at
        return stocks, funds, loaded_at
    except Exception as exc:
        _log(f"[StockMap] disk cache read skipped: {exc}")
        return {}, {}, 0.0


def _write_stock_map_to_disk(
    stocks: Dict[str, str], funds: Dict[str, str], loaded_at: float
) -> None:
    """Persist the name maps (atomic replace) so restarts start warm. Never raises."""
    if not stocks and not funds:
        return
    try:
        _STOCK_MAP_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STOCK_MAP_CACHE_FILE.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(
                {"loaded_at": loaded_at, "stocks": stocks, "funds": funds},
                fh,
                ensure_ascii=False,
            )
        os.replace(tmp, _STOCK_MAP_CACHE_FILE)
    except Exception as exc:
        _log(f"[StockMap] disk cache write skipped: {exc}")


def _publish_stock_maps(
    stocks: Dict[str, str], funds: Dict[str, str], loaded_at: float
) -> None:
    """Install the name maps, letting stock names win over fund names per code."""
    global _cn_stock_names, _cn_fund_names, _cn_stock_map, _cn_stock_reverse_map
    global _cn_stock_map_loaded_at
    reverse: Dict[str, str] = {code: name for name, code in funds.items()}
    reverse.update({code: name for name, code in stocks.items()})  # stocks take precedence
    _cn_stock_names = dict(stocks)
    _cn_fund_names = dict(funds)
    _cn_stock_map = {**funds, **stocks}  # name -> code for lookups
    _cn_stock_reverse_map = reverse
    _cn_stock_map_loaded_at = loaded_at


def _ensure_stock_map_warm_start() -> None:
    """Populate the name cache from disk once per process.

    Runs at startup (before requests) and is cheap/local, so the very first
    request after a restart already resolves Chinese names instead of waiting
    minutes on AkShare. Deliberately does NOT take _cn_stock_map_fetch_lock,
    which a slow network load may be holding for minutes.
    """
    global _stock_map_disk_loaded
    if _stock_map_disk_loaded or _cn_stock_map:
        return
    with _stock_map_disk_lock:
        if _stock_map_disk_loaded or _cn_stock_map:
            return
        stocks, funds, loaded_at = _read_stock_map_from_disk()
        if stocks or funds:
            _publish_stock_maps(stocks, funds, loaded_at)
            age_h = (time.time() - loaded_at) / 3600.0 if loaded_at else -1.0
            _log(
                f"[StockMap] warm-started {len(stocks)} stocks + {len(funds)} funds "
                f"from disk (age {age_h:.1f}h)."
            )
        _stock_map_disk_loaded = True


# A-share listing sources. NOTE: ak.stock_info_a_code_name() is deliberately NOT
# used — it aggregates endpoints measured hanging for minutes in this deployment
# (the cause of the StockMap request timeouts), while the exchange-specific
# listings below are reachable. Every source is bounded so one dead endpoint
# cannot stall the whole map.
_STOCK_MAP_FETCH_TIMEOUT = int(os.getenv("TA_STOCK_MAP_FETCH_TIMEOUT", "120"))
# The SSE listing endpoint is the only source of SH names available here (push2
# is unreachable) and it is slow AND flaky: measured ~7 minutes on success, plus
# ChunkedEncodingError/SSL EOF. It runs on a daemon thread that no request ever
# waits on, so it gets a generous bound; results are published per source, so a
# slow SSE never delays the SZ/fund names.
_STOCK_MAP_SLOW_FETCH_TIMEOUT = int(os.getenv("TA_STOCK_MAP_SLOW_FETCH_TIMEOUT", "600"))
_STOCK_MAP_SOURCE_ATTEMPTS = int(os.getenv("TA_STOCK_MAP_SOURCE_ATTEMPTS", "2"))
_cn_stock_map_fetch_lock = Lock()
_CODE_COLUMNS = ("code", "证券代码", "A股代码", "基金代码", "股票代码", "代码")
_NAME_COLUMNS = ("name", "证券简称", "A股简称", "基金简称", "股票简称", "简称")


def _call_bounded(fn, timeout: float):
    """Run fn() on a daemon thread, waiting at most `timeout` seconds.

    Returns (value, error). On timeout the worker is ABANDONED — Python cannot
    kill a thread stuck in a socket read — so callers must treat that source as
    failed and carry on. Bounding the wait is what stops one dead upstream from
    stalling the entire name map.
    """
    box: Dict[str, Any] = {"done": False, "value": None, "error": None}

    def _runner() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # reported to the caller, never raised here
            box["error"] = exc
        finally:
            box["done"] = True

    worker = Thread(target=_runner, name="stock-map-fetch", daemon=True)
    worker.start()
    worker.join(timeout)
    if not box["done"]:
        return None, TimeoutError(f"upstream call exceeded {timeout:.0f}s")
    if box["error"] is not None:
        return None, box["error"]
    return box["value"], None


def _names_from_df(df: Any) -> Dict[str, str]:
    """Extract name→normalized-code from a listing frame, across AkShare schemas."""
    if df is None or getattr(df, "empty", True):
        return {}
    # NOTE: never truth-test a pandas Index (``df.columns or []`` raises
    # "truth value of a Index is ambiguous").
    raw_cols = getattr(df, "columns", None)
    cols = list(raw_cols) if raw_cols is not None else []
    code_col = next((c for c in _CODE_COLUMNS if c in cols), None)
    name_col = next((c for c in _NAME_COLUMNS if c in cols), None)
    if not code_col or not name_col:
        return {}
    out: Dict[str, str] = {}
    for _, row in df.iterrows():
        name = str(row.get(name_col, "")).strip()
        code = str(row.get(code_col, "")).strip()
        if name and code:
            out[name] = _normalize_symbol(code)
    return out


def _fetch_listing_source(
    label: str, fn, timeout: Optional[float] = None
) -> tuple[Dict[str, str], Optional[str]]:
    """Fetch one listing source, retrying a bounded number of times.

    The exchange endpoints are intermittently slow or truncated (measured:
    ChunkedEncodingError, SSL EOF and multi-minute stalls), so a single attempt
    is not enough — one flaky source would silently drop ~1700 SH names.
    """
    from tradingagents.dataflows.providers.cn_akshare_provider import AKSHARE_CALL_LOCK

    bound = float(timeout or _STOCK_MAP_FETCH_TIMEOUT)

    def _guarded():
        with AKSHARE_CALL_LOCK:
            return fn()

    problems: list = []
    for attempt in range(1, max(1, _STOCK_MAP_SOURCE_ATTEMPTS) + 1):
        value, err = _call_bounded(_guarded, bound)
        if err is not None:
            problems.append(f"{label}[try{attempt}]: {type(err).__name__}: {err}")
            continue
        names = _names_from_df(value)
        if names:
            return names, None
        problems.append(f"{label}[try{attempt}]: no parsable rows")
    return {}, "; ".join(problems)


def _fetch_stock_map_from_sources(on_partial=None) -> tuple[Dict[str, str], Dict[str, str], list, list]:
    """Collect A-share + fund names from every reachable source. Never raises.

    Returns (stock_names, fund_names, problems, counts). Stocks and funds stay
    separate so callers keep the stock/fund provenance.

    Reliable sources run FIRST and `on_partial(kind, names)` is invoked after each
    successful one, so usable names land within seconds even while the slow SSE
    endpoint is still grinding.
    """
    import akshare as ak

    sources = (
        ("sz-a", lambda: ak.stock_info_sz_name_code(symbol="A股列表"), None, "stock"),
        ("fund", lambda: ak.fund_name_em(), None, "fund"),
        (
            "sh-main",
            lambda: ak.stock_info_sh_name_code(symbol="主板A股"),
            _STOCK_MAP_SLOW_FETCH_TIMEOUT,
            "stock",
        ),
        (
            "sh-star",
            lambda: ak.stock_info_sh_name_code(symbol="科创板"),
            _STOCK_MAP_SLOW_FETCH_TIMEOUT,
            "stock",
        ),
    )
    stock_names: Dict[str, str] = {}
    fund_names: Dict[str, str] = {}
    problems: list = []
    counts: list = []
    for label, fn, timeout, kind in sources:
        names, problem = _fetch_listing_source(label, fn, timeout)
        if problem:
            problems.append(problem)
            continue
        target = fund_names if kind == "fund" else stock_names
        fresh: Dict[str, str] = {}
        for name, code in names.items():
            if not name or not code or target.get(name) == code:
                continue
            target[name] = code
            fresh[name] = code
        counts.append(f"{label}={len(fresh)}")
        if fresh and on_partial is not None:
            try:
                on_partial(kind, fresh)
            except Exception as exc:  # a sink failure must not lose the fetch
                problems.append(f"{label}: on_partial failed: {exc}")
    return stock_names, fund_names, problems, counts


def _load_cn_stock_map() -> Dict[str, str]:
    """Refresh the cached name maps from upstream (7-day TTL, negative cache).

    Performs BLOCKING network I/O, but NEVER holds a lock that readers take and
    never queues behind a load already in flight: if a fetch is running, callers
    get the current cache immediately. That is what stops one slow source from
    turning every name lookup into a multi-minute stall.
    """
    global _cn_stock_map_failed_at, _stock_map_cold_warned
    now = time.time()
    if _stock_map_is_fresh(now) or _stock_map_in_backoff(now):
        return _cn_stock_map or {}
    # Serialise fetches on a lock that readers never touch; skip if one is running.
    if not _cn_stock_map_fetch_lock.acquire(blocking=False):
        return _cn_stock_map or {}
    try:
        now = time.time()
        if _stock_map_is_fresh(now) or _stock_map_in_backoff(now):
            return _cn_stock_map or {}
        stocks_acc: Dict[str, str] = {}
        funds_acc: Dict[str, str] = {}
        published = {"stocks": 0, "funds": 0}

        def _absorb() -> None:
            """Publish everything fetched so far. Called after every source."""
            base_stocks, base_funds = _cn_stock_names, _cn_fund_names
            if not base_stocks and not base_funds:
                base_stocks, base_funds, _ = _read_stock_map_from_disk()
            # Merge rather than replace: a partial load (one flaky source) must
            # never drop names we had already resolved.
            merged_stocks = {**base_stocks, **stocks_acc}
            # A fund must never claim a code that a real stock already owns.
            stock_codes = set(merged_stocks.values())
            merged_funds = {
                name: code
                for name, code in {**base_funds, **funds_acc}.items()
                if code not in stock_codes
            }
            stamp = time.time()
            _publish_stock_maps(merged_stocks, merged_funds, stamp)
            _write_stock_map_to_disk(merged_stocks, merged_funds, stamp)
            published["stocks"] = len(merged_stocks)
            published["funds"] = len(merged_funds)

        def _on_partial(kind: str, names: Dict[str, str]) -> None:
            """Sink invoked after each source so fast names are usable at once."""
            (funds_acc if kind == "fund" else stocks_acc).update(names)
            _absorb()
            _log(
                f"[StockMap] partial: {kind} +{len(names)} "
                f"(now {published['stocks']} stocks + {published['funds']} funds)."
            )

        try:
            stocks, funds, problems, counts = _fetch_stock_map_from_sources(_on_partial)
        except Exception as exc:  # defensive: the fetchers are not supposed to raise
            stocks, funds, problems, counts = {}, {}, [f"unexpected: {type(exc).__name__}: {exc}"], []
        stocks_acc.update(stocks)
        funds_acc.update(funds)
        if stocks_acc or funds_acc:
            _absorb()
            _cn_stock_map_failed_at = 0.0
            _stock_map_cold_warned = False
            detail = f" [{' '.join(counts)}]" if counts else ""
            if problems:
                detail += f" partial: {'; '.join(problems)}"
            _log(
                f"[StockMap] Loaded {published['stocks']} stocks + "
                f"{published['funds']} funds = {len(_cn_stock_map or {})} names.{detail}"
            )
        else:
            # Negative-cache the failure so one bad upstream does not become one
            # upstream call per request.
            _cn_stock_map_failed_at = now
            _log(
                f"[StockMap] Failed to load: {'; '.join(problems) or 'no data'} "
                f"(backing off {_STOCK_MAP_RETRY_BACKOFF}s instead of retrying per request)"
            )
            if _cn_stock_map is None:
                _publish_stock_maps({}, {}, 0.0)
        return _cn_stock_map or {}
    finally:
        _cn_stock_map_fetch_lock.release()


def _schedule_stock_map_refresh() -> None:
    """Fire-and-forget background refill of the name map while it is cold.

    Request handlers never block on AkShare: they read the cache, degrade to raw
    symbols, and call this so the map heals itself a moment later.
    """
    global _cn_stock_map_refreshing
    import time as _time
    now = _time.time()
    if _stock_map_is_fresh(now) or _stock_map_in_backoff(now):
        return
    with _cn_stock_map_refresh_lock:
        if _cn_stock_map_refreshing:
            return
        _cn_stock_map_refreshing = True

    def _refresh() -> None:
        global _cn_stock_map_refreshing
        try:
            _load_cn_stock_map()
        except Exception as exc:  # defensive: a refresh must never crash the app
            _log(f"[StockMap] background refresh failed: {exc}")
        finally:
            with _cn_stock_map_refresh_lock:
                _cn_stock_map_refreshing = False

    Thread(target=_refresh, name="stock-map-refresh", daemon=True).start()


def _get_reverse_stock_map() -> Dict[str, str]:
    """Return the cached code→name mapping, never blocking on the network.

    This used to perform a blocking on-demand AkShare load, which let a slow or
    unreachable upstream stall request handlers (and re-fetched on every call,
    because a failed load never recorded a timestamp). It now reads the warmed
    cache only and schedules a background refresh when cold.

    The returned mapping is the shared cache object — treat it as read-only.
    """
    return _get_reverse_stock_map_cached_only()


def _get_reverse_stock_map_cached_only() -> Dict[str, str]:
    """Return the code→name mapping from the warmed cache only (never blocks).

    When the cache is cold this returns an empty mapping so callers fall back to
    stock codes, logs a single warning, and kicks off a background refresh — the
    original silent fallback is what made the reports list quietly drop Chinese
    names with nothing in the logs to explain it.

    The returned mapping is the shared cache object — treat it as read-only.
    """
    if _cn_stock_reverse_map:
        return _cn_stock_reverse_map
    _warn_stock_map_cold()
    _schedule_stock_map_refresh()
    return _cn_stock_reverse_map or {}


def _get_stock_name_map_cached_only() -> Dict[str, str]:
    """Return the name→code mapping from the warmed cache only (never blocks).

    Counterpart of _get_reverse_stock_map_cached_only() for name→code lookups.
    The returned mapping is the shared cache object — treat it as read-only.
    """
    if _cn_stock_map:
        return _cn_stock_map
    _warn_stock_map_cold()
    _schedule_stock_map_refresh()
    return _cn_stock_map or {}


def _search_cn_stock_by_name(query: str) -> Optional[str]:
    """Look up A-share stock code by company name (exact then partial match).

    Reads the warmed name cache only — this runs inside request handlers (manual
    analysis, watchlist import), so a cold cache degrades to None and schedules a
    background refresh rather than blocking on AkShare.
    """
    query = query.strip()
    if not query:
        return None
    stock_map = _get_stock_name_map_cached_only()
    if not stock_map:
        return None
    # 1. Exact match
    if query in stock_map:
        return stock_map[query]
    # 2. Partial match: query is substring of a stock name or vice versa
    candidates = [(name, code) for name, code in stock_map.items()
                  if query in name or name in query]
    if len(candidates) == 1:
        return candidates[0][1]
    # 3. If multiple partial matches, pick the one with shortest name (closest match)
    if candidates:
        candidates.sort(key=lambda x: len(x[0]))
        return candidates[0][1]
    return None


def _split_watchlist_batch_text(text: str) -> List[str]:
    return [token.strip() for token in re.split(r"[\s,，、；;]+", text.strip()) if token.strip()]


def _resolve_watchlist_identifier(
    raw: str,
    name_to_code: Dict[str, str],
    code_to_name: Dict[str, str],
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    token = raw.strip()
    if not token:
        return None, None, "输入为空"
    if token in name_to_code:
        symbol = name_to_code[token]
        return symbol, code_to_name.get(symbol, token), None
    symbol = _normalize_symbol(token)
    if symbol in code_to_name:
        return symbol, code_to_name.get(symbol, symbol), None
    return None, None, f"未识别的股票代码或名称: {token}"


_auth_scheme = HTTPBearer(auto_error=False)

FIXED_TEAMS = {
    "Analyst Team": [
        "Market Analyst",
        "Social Analyst",
        "News Analyst",
        "Fundamentals Analyst",
        "Macro Analyst",
        "Smart Money Analyst",
        "Volume Price Analyst",
    ],
    "Research Team": ["Bull Researcher", "Bear Researcher", "Research Manager"],
    "Trading Team": ["Trader"],
    "Risk Management": ["Aggressive Analyst", "Neutral Analyst", "Conservative Analyst"],
    "Portfolio Management": ["Portfolio Manager"],
}
ANALYST_ORDER = ["market", "social", "news", "fundamentals", "macro", "smart_money", "volume_price"]
ANALYST_AGENT_NAMES = {
    "market": "Market Analyst",
    "social": "Social Analyst",
    "news": "News Analyst",
    "fundamentals": "Fundamentals Analyst",
    "macro": "Macro Analyst",
    "volume_price": "Volume Price Analyst",
    "smart_money": "Smart Money Analyst",
    "bull": "Bull Researcher",
    "bear": "Bear Researcher",
    "Bull_Initial": "Bull Researcher",
    "Bear_Initial": "Bear Researcher",
    "Bull_Rebuttal": "Bull Researcher",
    "Bear_Rebuttal": "Bear Researcher",
    "research_manager": "Research Manager",
    "trader": "Trader",
    "aggressive": "Aggressive Analyst",
    "neutral": "Neutral Analyst",
    "conservative": "Conservative Analyst",
    "portfolio_manager": "Portfolio Manager",
}
ANALYST_REPORT_MAP = {
    "market": "market_report",
    "social": "sentiment_report",
    "news": "news_report",
    "fundamentals": "fundamentals_report",
    "macro": "macro_report",
    "smart_money": "smart_money_report",
    "volume_price": "volume_price_report",
}

# All analysts always run — each uses its own natural time window
# (technical/funds → short, fundamentals/macro → medium)
def _get_horizon_analysts(horizon: str, available: List[str]) -> List[str]:
    """Return all available analysts regardless of horizon."""
    return list(available)


def _announcements_file() -> Path:
    return Path(__file__).resolve().parent / "announcements.json"


def _load_latest_announcement() -> Optional[Dict[str, Any]]:
    path = _announcements_file()
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        _log(f"[Announcements] Failed to read {path.name}: {exc}")
        return None

    announcements = raw.get("announcements") if isinstance(raw, dict) else raw
    if not isinstance(announcements, list):
        return None

    for item in announcements:
        if not isinstance(item, dict):
            continue
        if item.get("active", True) is False:
            continue
        return item
    return None


class UserContextInput(BaseModel):
    objective: Optional[str] = Field(None, description="用户目标动作，如建仓/加仓/减仓/止损/观察")
    risk_profile: Optional[str] = Field(None, description="风险偏好，如保守/平衡/激进")
    investment_horizon: Optional[str] = Field(None, description="持有周期，如短线/波段/中线")
    cash_available: Optional[float] = Field(None, description="可用资金")
    current_position: Optional[float] = Field(None, description="当前持仓数量")
    current_position_pct: Optional[float] = Field(None, description="当前仓位占比")
    average_cost: Optional[float] = Field(None, description="当前持仓成本")
    max_loss_pct: Optional[float] = Field(None, description="最大容忍亏损百分比")
    constraints: List[str] = Field(default_factory=list, description="用户的硬约束列表")
    user_notes: Optional[str] = Field(None, description="用户补充说明")


class AnalyzeRequest(UserContextInput):
    symbol: str = Field(default="", description="股票代码，如 600519.SH（当 query 包含代码时可省略）")
    trade_date: str = Field(default_factory=cn_today_str, description="交易日期 YYYY-MM-DD")
    selected_analysts: List[str] = Field(
        default_factory=lambda: ["market", "social", "news", "fundamentals", "macro", "smart_money"]
    )
    config_overrides: Dict[str, Any] = Field(default_factory=dict)
    dry_run: bool = False
    # When set, triggers intent-driven analysis via streaming dual-horizon path
    query: Optional[str] = Field(default=None, description="自然语言查询，如：分析贵州茅台短线机会")
    horizons: List[str] = Field(default_factory=lambda: ["short"], description="分析周期列表，如 ['short'] 或 ['short','medium']")
    # Pre-parsed intent from _ai_extract_symbol_and_date (avoids second LLM call in _run_job)
    user_intent: Optional[Dict[str, Any]] = Field(default=None, description="预解析的用户意图，由 chat_completions 传入")
    prompt_template_id: Optional[str] = Field(default=None, description="提示词模板 ID（用于统一深度分析请求）")
    prompt_vars: Dict[str, Any] = Field(default_factory=dict, description="模板变量覆盖")
    model_profile_id: Optional[str] = Field(default=None, description="模型配置 ID（不传则使用默认运行时模型）")
    experiment_id: Optional[str] = Field(default=None, description="模型对比实验批次 ID")
    input_snapshot_hash: Optional[str] = Field(default=None, description="同输入快照哈希，用于跨模型公平对比")


class AnalyzeResponse(BaseModel):
    job_id: str
    status: Literal["pending", "running", "completed", "failed"]
    created_at: str


class MainlineAnalyzeRequest(BaseModel):
    trade_date: str = Field(default_factory=cn_today_str, description="交易日期 YYYY-MM-DD")
    perspective: Literal["short", "medium"] = Field("short", description="视角：short=短线题材 / medium=中期行业")
    user_focus: Optional[str] = Field(None, description="用户关注方向，如：只看 AI 相关")
    yesterday_mainlines: Optional[List[Dict[str, Any]]] = Field(None, description="昨日主线列表（状态机输入）")
    model_profile_id: Optional[str] = Field(None, description="使用的模型配置 ID（不传则用默认/首个启用的模型配置）")


class MainlineAnalyzeResponse(BaseModel):
    job_id: str
    status: Literal["pending", "running", "completed", "failed"]
    created_at: str


class MainlineRunListResponse(BaseModel):
    runs: List[Dict[str, Any]]


class MainlineBoardSpotResponse(BaseModel):
    trade_date: str
    type: str
    source: Optional[str]
    boards: List[Dict[str, Any]]
    warnings: List[str]


class RecommendationRequest(BaseModel):
    top_k: int = Field(5, ge=1, le=20, description="返回推荐数量")
    candidate_limit: int = Field(80, ge=10, le=300, description="候选池上限")
    source_mode: Literal["user_pool", "market_scan"] = Field("user_pool", description="推荐来源：用户池 / 全市场扫描")
    scan_limit: Optional[int] = Field(None, ge=50, le=5000, description="全市场扫描时的股票数量上限")
    include_tracking: bool = Field(True, description="候选池包含跟踪看板持仓")
    include_watchlist: bool = Field(True, description="候选池包含自选列表")
    seed_symbols: List[str] = Field(default_factory=list, description="额外候选代码列表")
    min_change_pct: float = Field(-2.0, description="最低当日涨跌幅过滤阈值")
    market: Literal["cn", "us"] = Field("cn", description="推荐市场模板（影响默认评分权重）")
    min_price: float = Field(2.0, ge=0.0, le=10000.0, description="最低价格过滤（全市场扫描）")
    max_price: float = Field(10000.0, ge=0.0, le=10000.0, description="最高价格过滤（全市场扫描）")
    min_amount: float = Field(200000000.0, ge=0.0, description="最低成交额过滤（全市场扫描）")
    min_turnover_rate: float = Field(0.8, ge=0.0, description="最低换手率过滤（可买入约束）")
    min_volume_ratio: float = Field(0.6, ge=0.0, description="最低量比过滤（可买入约束）")
    limit_up_threshold_pct: float = Field(9.6, ge=-30.0, le=30.0, description="接近涨停过滤阈值（涨跌幅%）")
    limit_down_threshold_pct: float = Field(-9.6, ge=-30.0, le=30.0, description="接近跌停过滤阈值（涨跌幅%）")
    enforce_tradability: bool = Field(True, description="是否启用可买入硬过滤")
    score_profile: Optional[str] = Field(None, description="评分模板，如 ashare_balanced/us_balanced/ashare_aggressive")
    momentum_weight: Optional[float] = Field(None, ge=0.0, le=10.0, description="动量权重（可选）")
    activity_weight: Optional[float] = Field(None, ge=0.0, le=10.0, description="活跃度权重（可选）")
    near_high_weight: Optional[float] = Field(None, ge=0.0, le=10.0, description="逼近日高权重（可选）")
    auto_start_analysis: bool = Field(False, description="是否自动对推荐标的启动分析任务")
    auto_top_n: int = Field(0, ge=0, le=10, description="自动启动分析的数量（0 表示等于 top_k）")
    horizons: List[str] = Field(default_factory=lambda: ["short"], description="自动分析周期")
    selected_analysts: List[str] = Field(
        default_factory=lambda: ["market", "social", "news", "fundamentals", "macro", "smart_money"]
    )


class RecommendationItemResponse(BaseModel):
    symbol: str
    code: str
    name: str
    score: float
    reasons: List[str]
    live_price: Optional[float] = None
    price_change_pct: Optional[float] = None
    day_high: Optional[float] = None
    day_open: Optional[float] = None
    amount: Optional[float] = None
    volume: Optional[float] = None
    volume_ratio: Optional[float] = None
    turnover_rate: Optional[float] = None
    sector: Optional[str] = None
    quote_time: Optional[str] = None
    quote_source: Optional[str] = None
    strategy_hits: List[str] = Field(default_factory=list)
    risk_flags: List[str] = Field(default_factory=list)
    score_breakdown: Optional[Dict[str, float]] = None


class RecommendationAnalyzeJob(BaseModel):
    symbol: str
    job_id: str
    status: Literal["pending", "running", "completed", "failed"] = "pending"
    created_at: str


class RecommendationResponse(BaseModel):
    pool_size: int
    scored_size: int
    items: List[RecommendationItemResponse]
    scoring_model: Optional[Dict[str, Any]] = None
    analysis_jobs: List[RecommendationAnalyzeJob] = Field(default_factory=list)


class DailyProductRunRequest(BaseModel):
    mode: Literal["recommended", "watchlist", "tracking", "all"] = Field(
        "recommended",
        description="批量分析模式：recommended=智能推荐；watchlist=自选；tracking=跟踪池；all=合并池",
    )
    top_k: int = Field(5, ge=1, le=20, description="触发分析的股票数量")
    candidate_limit: int = Field(80, ge=10, le=300, description="候选池上限（recommended模式）")
    recommendation_source: Literal["user_pool", "market_scan"] = Field(
        "user_pool",
        description="recommended 模式的候选来源：用户池 / 全市场扫描",
    )
    scan_limit: Optional[int] = Field(None, ge=50, le=5000, description="全市场扫描时的股票数量上限")
    include_tracking: bool = Field(True, description="recommended 模式候选池是否包含跟踪持仓")
    include_watchlist: bool = Field(True, description="recommended 模式候选池是否包含自选列表")
    seed_symbols: List[str] = Field(default_factory=list, description="额外候选代码（recommended 模式）")
    min_change_pct: float = Field(-2.0, description="最低当日涨跌幅阈值（recommended 模式）")
    market: Literal["cn", "us"] = Field("cn", description="推荐市场模板")
    min_price: float = Field(2.0, ge=0.0, le=10000.0, description="最低价格过滤（全市场扫描）")
    max_price: float = Field(10000.0, ge=0.0, le=10000.0, description="最高价格过滤（全市场扫描）")
    min_amount: float = Field(200000000.0, ge=0.0, description="最低成交额过滤（全市场扫描）")
    min_turnover_rate: float = Field(0.8, ge=0.0, description="最低换手率过滤（可买入约束）")
    min_volume_ratio: float = Field(0.6, ge=0.0, description="最低量比过滤（可买入约束）")
    limit_up_threshold_pct: float = Field(9.6, ge=-30.0, le=30.0, description="接近涨停过滤阈值（涨跌幅%）")
    limit_down_threshold_pct: float = Field(-9.6, ge=-30.0, le=30.0, description="接近跌停过滤阈值（涨跌幅%）")
    enforce_tradability: bool = Field(True, description="是否启用可买入硬过滤")
    score_profile: Optional[str] = Field(None, description="推荐评分模板")
    momentum_weight: Optional[float] = Field(None, ge=0.0, le=10.0, description="动量权重")
    activity_weight: Optional[float] = Field(None, ge=0.0, le=10.0, description="活跃度权重")
    near_high_weight: Optional[float] = Field(None, ge=0.0, le=10.0, description="逼近日高权重")
    use_backtest_feedback: bool = Field(False, description="是否根据最近回测结果自动微调推荐权重")
    strategy_skills: List[str] = Field(default_factory=list, description="可选策略 skill 列表（用于编排提示）")
    strategy_mode: Literal["manual", "auto"] = Field("manual", description="策略 skill 编排模式")
    horizons: List[str] = Field(default_factory=lambda: ["short"], description="分析周期")
    selected_analysts: List[str] = Field(
        default_factory=lambda: ["market", "social", "news", "fundamentals", "macro", "smart_money"]
    )
    ensure_scheduled: bool = Field(False, description="是否将本次触发标的同步加入定时分析")
    schedule_horizon: Literal["short", "medium"] = Field("short", description="加入定时分析的周期")
    schedule_trigger_time: str = Field("20:00", description="加入定时分析的触发时间 HH:MM")


class DailyProductRunJob(BaseModel):
    symbol: str
    name: str
    job_id: str
    status: Literal["pending", "running", "completed", "failed"] = "pending"
    created_at: str


class DailyProductRunResponse(BaseModel):
    run_id: str
    status: Literal["pending", "running", "completed", "failed"]
    mode: str
    summary: Dict[str, Any]
    recommendation: Optional[RecommendationResponse] = None
    jobs: List[DailyProductRunJob] = Field(default_factory=list)


class BatchScheduledTriggerJob(BaseModel):
    item_id: str
    job_id: str
    symbol: str
    name: str
    status: Literal["pending", "running", "completed", "failed"]
    created_at: str
    current_position: Optional[float] = None
    average_cost: Optional[float] = None
    waiting_ahead_count: Optional[int] = None
    scheduled_running_count: Optional[int] = None
    scheduled_concurrency_limit: Optional[int] = None


class BatchScheduledTriggerResponse(BaseModel):
    summary: Dict[str, int]
    jobs: List[BatchScheduledTriggerJob]


class JobStatusResponse(BaseModel):
    job_id: str
    status: Literal["pending", "running", "completed", "failed"]
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    symbol: str
    trade_date: str
    error: Optional[str] = None
    waiting_ahead_count: Optional[int] = None
    scheduled_running_count: Optional[int] = None
    scheduled_concurrency_limit: Optional[int] = None
    progress: Optional[int] = None
    phase: Optional[str] = None
    progress_detail: Optional[str] = None


class ChatMessage(BaseModel):
    role: str
    content: Any


class ChatCompletionRequest(UserContextInput):
    model: Optional[str] = "tradingagents-ashare"
    messages: List[ChatMessage]
    stream: bool = True
    selected_analysts: List[str] = Field(
        default_factory=lambda: ["market", "social", "news", "fundamentals", "macro", "smart_money"]
    )
    config_overrides: Dict[str, Any] = Field(default_factory=dict)
    model_profile_id: Optional[str] = Field(default=None, description="模型配置 ID（不传则使用默认运行时模型）")
    dry_run: bool = False


class KlineResponse(BaseModel):
    symbol: str
    start_date: str
    end_date: str
    candles: List[Dict[str, Any]]


# Report API Models
class ReportCreateRequest(BaseModel):
    symbol: str = Field(..., description="股票代码")
    trade_date: str = Field(..., description="交易日期 YYYY-MM-DD")
    decision: Optional[str] = Field(None, description="交易决策")
    result_data: Optional[Dict[str, Any]] = Field(None, description="完整分析结果")


class ReportResponse(BaseModel):
    id: str
    user_id: Optional[str]
    symbol: str
    name: Optional[str] = None
    trade_date: str
    status: Literal["pending", "running", "completed", "failed"] = "completed"
    error: Optional[str] = None
    decision: Optional[str]
    direction: Optional[str]
    confidence: Optional[int]
    target_price: Optional[float]
    stop_loss_price: Optional[float]
    risk_items: Optional[List[Dict[str, Any]]] = None
    key_metrics: Optional[List[Dict[str, Any]]] = None
    analyst_traces: Optional[List[Dict[str, Any]]] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    waiting_ahead_count: Optional[int] = None
    scheduled_running_count: Optional[int] = None
    scheduled_concurrency_limit: Optional[int] = None
    model_profile_id: Optional[str] = None
    model_profile_name: Optional[str] = None
    llm_provider: Optional[str] = None
    quick_think_llm: Optional[str] = None
    deep_think_llm: Optional[str] = None
    backend_url: Optional[str] = None
    experiment_id: Optional[str] = None
    input_snapshot_hash: Optional[str] = None
    freshness_status: Optional[str] = None
    freshness_summary: Optional[Dict[str, Any]] = None

    model_config = {"from_attributes": True}

    @field_serializer("created_at", "updated_at", when_used="json")
    def serialize_report_datetimes(self, value: Optional[datetime]) -> Optional[str]:
        return _serialize_datetime_utc(value)


class ReportDetailResponse(ReportResponse):
    market_report: Optional[str]
    sentiment_report: Optional[str]
    news_report: Optional[str]
    fundamentals_report: Optional[str]
    macro_report: Optional[str]
    smart_money_report: Optional[str]
    volume_price_report: Optional[str]
    game_theory_report: Optional[str]
    investment_plan: Optional[str]
    trader_investment_plan: Optional[str]
    final_trade_decision: Optional[str]
    result_data: Optional[Dict[str, Any]]


class ReportListResponse(BaseModel):
    total: int
    reports: List[ReportResponse]


class ReportBatchDeleteRequest(BaseModel):
    report_ids: List[str] = Field(default_factory=list)


class ReportBatchDeleteResponse(BaseModel):
    deleted_ids: List[str]
    missing_ids: List[str]


class LatestReportsBySymbolsRequest(BaseModel):
    symbols: List[str] = Field(default_factory=list)


class LatestReportsBySymbolsResponse(BaseModel):
    reports: List[ReportResponse]


class PortfolioOverviewResponse(BaseModel):
    watchlist: List[dict]
    scheduled: List[dict]
    latest_reports: List[ReportResponse]
    portfolio_import: Optional[dict] = None


class WatchlistAddRequest(BaseModel):
    text: Optional[str] = None
    symbol: Optional[str] = None


class WatchlistBatchIdsRequest(BaseModel):
    item_ids: List[str] = Field(default_factory=list)


class ScheduledBatchIdsRequest(BaseModel):
    item_ids: List[str] = Field(default_factory=list)


class ScheduledBatchUpdateRequest(BaseModel):
    item_ids: List[str] = Field(default_factory=list)
    is_active: Optional[bool] = None
    horizon: Optional[str] = None
    trigger_time: Optional[str] = None
    prompt_template_id: Optional[str] = None
    prompt_vars: Optional[Dict[str, Any]] = None


class ScheduledEnsureBatchRequest(BaseModel):
    symbols: List[str] = Field(default_factory=list)
    horizon: str = Field(default="short")
    trigger_time: str = Field(default="20:00")
    prompt_template_id: Optional[str] = None
    prompt_vars: Optional[Dict[str, Any]] = None


class PromptTemplateResponse(BaseModel):
    id: str
    user_id: Optional[str] = None
    scope: str
    name: str
    description: Optional[str] = None
    template_text: str
    intent_json: Dict[str, Any] = Field(default_factory=dict)
    is_builtin: bool
    is_active: bool
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class PromptTemplateListResponse(BaseModel):
    templates: List[PromptTemplateResponse]
    defaults: Dict[str, str]


class PromptTemplateCreateRequest(BaseModel):
    scope: str = Field(default=prompt_template_service.SCOPE_DEEP_ANALYSIS)
    name: str
    description: Optional[str] = None
    template_text: str
    intent_json: Dict[str, Any] = Field(default_factory=dict)
    is_active: bool = True


class PromptTemplateUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    template_text: Optional[str] = None
    intent_json: Optional[Dict[str, Any]] = None
    is_active: Optional[bool] = None


class AnnouncementItemResponse(BaseModel):
    title: str
    detail: str


class AnnouncementResponse(BaseModel):
    id: str
    tag: Optional[str] = None
    title: str
    summary: Optional[str] = None
    published_at: str
    items: List[AnnouncementItemResponse]
    cta_label: Optional[str] = None
    cta_path: Optional[str] = None


class LatestAnnouncementResponse(BaseModel):
    announcement: Optional[AnnouncementResponse] = None


class UserResponse(BaseModel):
    id: str
    email: str
    created_at: Optional[datetime] = None
    last_login_at: Optional[datetime] = None
    email_report_enabled: bool = True

    model_config = {"from_attributes": True}

    @field_serializer("created_at", "last_login_at", when_used="json")
    def serialize_user_datetimes(self, value: Optional[datetime]) -> Optional[str]:
        return _serialize_datetime_utc(value)


class AuthRequestCodeRequest(BaseModel):
    email: str


class AuthVerifyCodeRequest(BaseModel):
    email: str
    code: str


class AuthVerifyCodeResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


class UserRuntimeConfigResponse(BaseModel):
    llm_provider: str
    deep_think_llm: str
    quick_think_llm: str
    backend_url: str
    max_debate_rounds: int
    max_risk_discuss_rounds: int
    decision_critic_enabled: bool = True
    decision_critic_revision_threshold: float = 40.0
    has_api_key: bool = False
    has_wecom_webhook: bool = False
    wecom_webhook_display: Optional[str] = None
    has_wps_webhook: bool = False
    wps_webhook_display: Optional[str] = None
    server_fallback_enabled: bool = True
    email_report_enabled: bool = True
    wecom_report_enabled: bool = True
    wps_report_enabled: bool = True
    methodology_ashare_fundamentals: bool = True
    methodology_ashare_news_events: bool = True
    methodology_ashare_sector_macro: bool = True
    methodology_stock_analysis_team: bool = True
    methodology_extra_path: Optional[str] = None
    methodology_extra_path_news: Optional[str] = None
    methodology_extra_path_macro: Optional[str] = None
    finskills_root: Optional[str] = None
    stock_analysis_team_root: Optional[str] = None


class UserRuntimeConfigUpdateRequest(BaseModel):
    llm_provider: Optional[str] = None
    deep_think_llm: Optional[str] = None
    quick_think_llm: Optional[str] = None
    backend_url: Optional[str] = None
    max_debate_rounds: Optional[int] = None
    max_risk_discuss_rounds: Optional[int] = None
    decision_critic_enabled: Optional[bool] = None
    decision_critic_revision_threshold: Optional[float] = Field(None, ge=0.0, le=100.0)
    methodology_ashare_fundamentals: Optional[bool] = None
    methodology_ashare_news_events: Optional[bool] = None
    methodology_ashare_sector_macro: Optional[bool] = None
    methodology_stock_analysis_team: Optional[bool] = None
    methodology_extra_path: Optional[str] = None
    methodology_extra_path_news: Optional[str] = None
    methodology_extra_path_macro: Optional[str] = None
    finskills_root: Optional[str] = None
    stock_analysis_team_root: Optional[str] = None
    email_report_enabled: Optional[bool] = None
    wecom_report_enabled: Optional[bool] = None
    wps_report_enabled: Optional[bool] = None
    api_key: Optional[str] = None
    wecom_webhook_url: Optional[str] = None
    wps_webhook_url: Optional[str] = None
    clear_api_key: bool = False
    clear_wecom_webhook: bool = False
    clear_wps_webhook: bool = False
    warmup: bool = True
    force_warmup: bool = False


class UserRuntimeWarmupRequest(UserRuntimeConfigUpdateRequest):
    prompt: str = "你好"


class RuntimeWarmupResult(BaseModel):
    model: str
    targets: List[str] = Field(default_factory=list)
    content: Optional[str] = None
    error: Optional[str] = None


class UserRuntimeWarmupResponse(BaseModel):
    prompt: str
    results: List[RuntimeWarmupResult]


class ModelProfileResponse(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    llm_provider: str
    backend_url: Optional[str] = None
    quick_think_llm: Optional[str] = None
    deep_think_llm: Optional[str] = None
    is_default: bool = False
    is_active: bool = True
    tags: List[str] = Field(default_factory=list)
    has_api_key: bool = False
    last_probe_status: Optional[str] = None
    last_probe_error: Optional[str] = None
    last_probe_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class ModelProfileListResponse(BaseModel):
    profiles: List[ModelProfileResponse] = Field(default_factory=list)


class ModelProfileCreateRequest(BaseModel):
    name: str = Field(..., description="模型配置名称")
    description: Optional[str] = Field(None, description="模型配置说明")
    llm_provider: str = Field(..., description="模型 provider，如 openai/openrouter/anthropic")
    backend_url: Optional[str] = Field(None, description="可选自定义接口地址")
    quick_think_llm: Optional[str] = Field(None, description="常规模型名")
    deep_think_llm: Optional[str] = Field(None, description="推理模型名")
    api_key: Optional[str] = Field(None, description="可选模型 API Key（加密存储）")
    is_default: bool = Field(False, description="是否设为默认模型配置")
    is_active: bool = Field(True, description="是否启用")
    tags: List[str] = Field(default_factory=list, description="标签")


class ModelProfileUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    llm_provider: Optional[str] = None
    backend_url: Optional[str] = None
    quick_think_llm: Optional[str] = None
    deep_think_llm: Optional[str] = None
    api_key: Optional[str] = None
    clear_api_key: bool = False
    is_default: Optional[bool] = None
    is_active: Optional[bool] = None
    tags: Optional[List[str]] = None


class ModelProfileWarmupRequest(BaseModel):
    prompt: str = "你好"


class ModelProfileWarmupResponse(BaseModel):
    profile_id: str
    prompt: str
    results: List[RuntimeWarmupResult]
    profile: ModelProfileResponse


class ModelArenaPromoteRequest(BaseModel):
    profile_id: str
    lookback_days: int = Field(90, ge=7, le=400)
    scope: str = Field("portfolio", description="portfolio 或 all")
    min_samples: int = Field(20, ge=1, le=5000)
    min_accuracy_improvement_pct: float = Field(0.0, ge=0.0, le=50.0)
    max_return_drop_pct: float = Field(0.5, ge=0.0, le=50.0)
    force: bool = False


class ModelArenaRollbackRequest(BaseModel):
    profile_id: str
    reason: Optional[str] = None


class ModelArenaRunRequest(UserContextInput):
    symbol: str = Field(..., description="股票代码，如 600519.SH")
    trade_date: str = Field(default_factory=cn_today_str, description="交易日期 YYYY-MM-DD")
    model_profile_ids: List[str] = Field(default_factory=list, description="参与对比的模型配置 ID 列表")
    selected_analysts: List[str] = Field(
        default_factory=lambda: ["market", "social", "news", "fundamentals", "macro", "smart_money"]
    )
    horizons: List[str] = Field(default_factory=lambda: ["short"])
    query: Optional[str] = Field(default=None, description="可选自然语言分析意图")
    dry_run: bool = False
    prompt_template_id: Optional[str] = None
    prompt_vars: Dict[str, Any] = Field(default_factory=dict)
    config_overrides: Dict[str, Any] = Field(default_factory=dict)


class ModelArenaRunJob(BaseModel):
    model_profile_id: Optional[str] = None
    model_profile_name: Optional[str] = None
    job_id: str
    symbol: str
    trade_date: str
    status: Literal["pending", "running", "completed", "failed"] = "pending"
    created_at: str


class ModelArenaRunResponse(BaseModel):
    experiment_id: str
    input_snapshot_hash: str
    symbol: str
    trade_date: str
    jobs: List[ModelArenaRunJob] = Field(default_factory=list)


class WecomWebhookWarmupRequest(BaseModel):
    wecom_webhook_url: Optional[str] = None
    content: Optional[str] = None


class WecomWebhookWarmupResponse(BaseModel):
    sent: bool = True
    message: str
    webhook_display: Optional[str] = None


class WpsWebhookWarmupRequest(BaseModel):
    wps_webhook_url: Optional[str] = None
    content: Optional[str] = None


class WpsWebhookWarmupResponse(BaseModel):
    sent: bool = True
    message: str
    webhook_display: Optional[str] = None


class PortfolioPositionItem(BaseModel):
    symbol: str = Field(..., description="股票代码，如 600519.SH 或 600519")
    name: Optional[str] = Field(None, description="股票名称")
    current_position: Optional[float] = Field(None, description="持仓数量")
    available_position: Optional[float] = Field(None, description="可用数量")
    average_cost: Optional[float] = Field(None, description="成本价")
    market_value: Optional[float] = Field(None, description="市值")
    current_position_pct: Optional[float] = Field(None, description="仓位占比 %")


class PortfolioImportSyncRequest(BaseModel):
    positions: List[PortfolioPositionItem] = Field(..., description="持仓列表")
    source: str = Field("manual", description="持仓来源标识")
    auto_apply_scheduled: bool = Field(True, description="是否自动将持仓股票加入定时任务")


class TradeLedgerRecordRequest(BaseModel):
    """成交台账的一条记录（真实成交，非虚拟盘）。"""

    symbol: str = Field(..., description="标的代码")
    action: str = Field(..., description="BUY / SELL")
    price: float = Field(..., gt=0, description="成交价")
    shares: float = Field(..., gt=0, description="成交股数")
    trade_date: str = Field(..., description="成交日期 YYYY-MM-DD")
    name: Optional[str] = Field(None, description="标的名称")
    sell_reason: Optional[str] = Field(
        None,
        description="卖出原因：stop_loss / take_profit / trailing_stop / time_stop / invalidation / rebalance / manual",
    )
    sell_reason_note: Optional[str] = Field(None, description="卖出原因补充说明")
    report_id: Optional[str] = Field(None, description="关联研报")
    plan_id: Optional[str] = Field(None, description="关联交易计划")
    note: Optional[str] = Field(None, description="备注")


class TradeLedgerImportRequest(BaseModel):
    """批量导入买卖点（券商导出或截图识别结果）。"""

    symbol: str = Field(..., description="标的代码")
    trade_points: List[Dict[str, Any]] = Field(..., description="买卖点列表")
    name: Optional[str] = Field(None, description="标的名称")
    source: str = Field("import", description="来源标识")


class PaperPortfolioBootstrapRequest(BaseModel):
    initial_cash: Optional[float] = Field(None, ge=0.0, description="模拟账户初始资金，不传则按导入持仓自动推算")
    source: Optional[str] = Field(None, description="仅同步指定来源的导入持仓")
    reset_existing: bool = Field(True, description="是否重置已有模拟仓位与成交记录")


class PaperTradeRequest(BaseModel):
    symbol: str = Field(..., description="股票代码，如 600519.SH")
    name: Optional[str] = Field(None, description="股票名称，用于首次建仓展示")
    side: Literal["BUY", "SELL"] = Field(..., description="交易方向")
    quantity: float = Field(..., gt=0, description="成交数量")
    price: Optional[float] = Field(None, gt=0, description="成交价，不传则按实时价")
    reason: Optional[str] = Field(None, description="交易原因")
    fee_rate: float = Field(0.0003, ge=0.0, le=0.01, description="手续费率")
    trade_date: str = Field(default_factory=cn_today_str, description="交易日期 YYYY-MM-DD")


class DailyOperationRequest(BaseModel):
    trade_date: str = Field(default_factory=cn_today_str, description="交易日期 YYYY-MM-DD")
    include_recommendations: bool = Field(True, description="是否在持仓动作外追加推荐买入")
    recommendation_top_k: int = Field(3, ge=1, le=10, description="推荐候选上限")
    auto_execute: bool = Field(False, description="是否自动执行建议中的 BUY/SELL")


class UserTokenResponse(BaseModel):
    id: str
    name: str
    token: str
    token_hint: Optional[str] = None
    last_used_at: Optional[datetime] = None
    created_at: datetime

    model_config = {"from_attributes": True}

    @field_serializer("created_at", "last_used_at", when_used="json")
    def serialize_token_datetimes(self, value: Optional[datetime]) -> Optional[str]:
        return _serialize_datetime_utc(value)


class UserTokenListItem(BaseModel):
    """Token info for list endpoint — never exposes the full token."""
    id: str
    name: str
    token_hint: Optional[str] = None
    last_used_at: Optional[datetime] = None
    created_at: datetime

    model_config = {"from_attributes": True}

    @field_serializer("created_at", "last_used_at", when_used="json")
    def serialize_token_datetimes(self, value: Optional[datetime]) -> Optional[str]:
        return _serialize_datetime_utc(value)


class UserTokenCreateRequest(BaseModel):
    name: str


def _deep_merge(base: Dict[str, Any], overrides: Dict[str, Any]) -> Dict[str, Any]:
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def _user_config_overrides(user_id: Optional[str], db: Optional[Session] = None) -> Dict[str, Any]:
    if not user_id:
        return {}

    def _query(sess: Session) -> Dict[str, Any]:
        user_cfg = auth_service.get_user_llm_config(sess, user_id)
        if not user_cfg:
            return {}
        result: Dict[str, Any] = {}
        for key in (
            "llm_provider",
            "backend_url",
            "quick_think_llm",
            "deep_think_llm",
            "max_debate_rounds",
            "max_risk_discuss_rounds",
            "decision_critic_enabled",
            "decision_critic_revision_threshold",
        ):
            value = getattr(user_cfg, key, None)
            if value is not None:
                result[key] = value
        api_key = auth_service.decrypt_secret(user_cfg.api_key_encrypted)
        if api_key:
            result["api_key"] = api_key
        return result

    if db is not None:
        return _query(db)
    with get_db_ctx() as own_db:
        return _query(own_db)


def _host_from_base_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return (urlparse(raw).hostname or "").lower()
    except Exception:
        return ""


def _sanitize_model_backend_compatibility(config: Dict[str, Any]) -> None:
    """Prevent known provider/model mismatch from breaking runtime analysis."""
    provider = str(config.get("llm_provider") or "openai").strip().lower()
    host = _host_from_base_url(config.get("backend_url"))
    if provider != "openai" or host != "api.deepseek.com":
        return

    for key in ("quick_think_llm", "deep_think_llm"):
        model = str(config.get(key) or "").strip()
        if not model:
            continue
        if model in _DEEPSEEK_ALLOWED_MODELS:
            continue
        logger.warning(
            "[RuntimeConfig] incompatible model '%s' for DeepSeek endpoint; "
            "auto-fallback %s='%s'",
            model,
            key,
            _DEEPSEEK_FALLBACK_MODEL,
        )
        config[key] = _DEEPSEEK_FALLBACK_MODEL


def _build_runtime_config(
    overrides: Dict[str, Any],
    user_id: Optional[str] = None,
    db: Optional[Session] = None,
    *,
    trusted_overrides: bool = False,
    strategy: str = "default",
) -> Dict[str, Any]:
    config = deepcopy(DEFAULT_CONFIG)
    server_fallback_enabled = os.getenv("ALLOW_SERVER_LLM_FALLBACK", "1").strip().lower() in ("1", "true", "yes", "on")
    config["server_fallback_enabled"] = server_fallback_enabled

    # Security: filter request overrides to allowlist only.
    # For trusted internal override bundles (e.g., resolved model profiles), keep full keys.
    if trusted_overrides:
        overrides = dict(overrides or {})
    else:
        overrides = {k: v for k, v in (overrides or {}).items() if k in _CONFIG_OVERRIDES_ALLOWLIST}

    # Apply global config overrides (from PATCH /v1/config).
    # In profile-selected strategy, model-runtime keys must come from profile.
    if _global_config_overrides:
        global_overrides = dict(_global_config_overrides)
        if strategy == "profile_selected":
            global_overrides = {k: v for k, v in global_overrides.items() if k not in _MODEL_RUNTIME_KEYS}
        config = _deep_merge(config, global_overrides)
    
    # Fetch user specific overrides from DB (pass db to reuse caller's session)
    user_overrides = _user_config_overrides(user_id, db=db)

    # ── Critical: Filter out empty strings before merging ──
    # This prevents an empty DB field from wiping out an Env Var default.
    if strategy == "profile_selected":
        user_overrides = {k: v for k, v in user_overrides.items() if k not in _MODEL_RUNTIME_KEYS}
    filtered_user_overrides = {k: v for k, v in user_overrides.items() if v not in (None, "", [])}
    filtered_request_overrides = {k: v for k, v in overrides.items() if v not in (None, "", [])}

    if filtered_user_overrides:
        config = _deep_merge(config, filtered_user_overrides)
    if filtered_request_overrides:
        config = _deep_merge(config, filtered_request_overrides)

    # ── Intelligent fallback between models ──
    # If one is provided but the other is missing (even after env var merge), cross-fill.
    quick = config.get("quick_think_llm")
    deep = config.get("deep_think_llm")

    if not deep and quick:
        config["deep_think_llm"] = quick
    if not quick and deep:
        config["quick_think_llm"] = deep

    _sanitize_model_backend_compatibility(config)

    from tradingagents.llm_clients.validators import validate_llm_provider

    config["llm_provider"] = validate_llm_provider(config.get("llm_provider") or "openai")

    if os.getenv("TA_DECISION_CRITIC", "1").strip().lower() in ("0", "false", "no", "off"):
        config["decision_critic_enabled"] = False

    return config


def _merge_model_profile_overrides(
    db: Session,
    *,
    user_id: Optional[str],
    model_profile_id: Optional[str],
    request_overrides: Dict[str, Any],
) -> Dict[str, Any]:
    merged = {k: v for k, v in (request_overrides or {}).items() if k in _CONFIG_OVERRIDES_ALLOWLIST}
    if not user_id or not model_profile_id:
        return merged
    profile_overrides = model_profile_service.resolve_runtime_overrides(
        db,
        user_id=user_id,
        profile_id=model_profile_id,
    )
    profile_overrides.update(merged)
    return profile_overrides


def _resolve_effective_model_profile_id(
    db: Session,
    *,
    user_id: Optional[str],
    model_profile_id: Optional[str],
) -> Optional[str]:
    """方案A：未显式指定模型配置时，自动落到「系统默认模型」——数据库 model_profiles 中 is_default=True 的配置。

    优先级：显式传入的 model_profile_id > 用户默认配置（is_default=True）> None（继续走 env/用户设置页兜底）。
    只有存在被标记为默认的配置时才启用，避免把「用户建过但没设默认的配置」误当成系统默认。
    """
    if model_profile_id:
        return model_profile_id
    if not user_id:
        return None
    profiles = model_profile_service.list_model_profiles(db, user_id, include_inactive=False)
    default = next((p for p in profiles if p.get("is_default")), None)
    if default is None:
        return None
    return str(default.get("id") or "") or None


class RequireUser:
    def __init__(self, allow_api_token: bool = True):
        self.allow_api_token = allow_api_token

    def __call__(
        self,
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(_auth_scheme),
    ) -> UserDB:
        if not credentials:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")

        token = credentials.credentials

        with get_db_ctx() as db:
            # 1. 优先尝试 JWT (网页登录)
            try:
                payload = auth_service.decode_access_token(token)
                user_id = str(payload.get("sub") or "")
                user = auth_service.get_user_by_id(db, user_id)
                if user and user.is_active:
                    # expunge 使 ORM 对象脱离 session，close 后仍可访问属性
                    db.expunge(user)
                    return user
            except Exception:
                # 不是有效的 JWT 或已过期，尝试 API Token
                pass

            # 2. 尝试 API Token (仅在允许时)
            if self.allow_api_token and token.startswith(token_service.TOKEN_PREFIX):
                user = token_service.verify_token(db, token)
                if user and user.is_active:
                    db.expunge(user)
                    return user

        detail = "身份验证失败或该接口不支持 API Token 访问" if self.allow_api_token else "该接口仅限网页端登录访问"
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


# 快捷依赖定义
_require_api_user = RequireUser(allow_api_token=True)    # 允许 API Token
_require_web_user = RequireUser(allow_api_token=False)   # 仅限网页登录


def _optional_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_auth_scheme),
) -> Optional[UserDB]:
    if not credentials:
        return None
    try:
        payload = auth_service.decode_access_token(credentials.credentials)
    except Exception:
        return None
    user_id = str(payload.get("sub") or "")
    if not user_id:
        return None
    with get_db_ctx() as db:
        user = auth_service.get_user_by_id(db, user_id)
        if user:
            db.expunge(user)
        return user


def _set_job(job_key: str, **kwargs) -> None:
    with _jobs_lock:
        if job_key not in _jobs:
            _jobs[job_key] = {}
        _jobs[job_key].update(kwargs)


def _get_job(job_key: str) -> Dict[str, Any]:
    with _jobs_lock:
        return dict(_jobs.get(job_key, {}))


def _set_daily_product_run(run_id: str, **kwargs: Any) -> None:
    with _daily_product_runs_lock:
        if run_id not in _daily_product_runs:
            _daily_product_runs[run_id] = {"run_id": run_id}
        _daily_product_runs[run_id].update(kwargs)
        snapshot = dict(_daily_product_runs[run_id])
    with get_db_ctx() as db:
        row = db.query(DailyProductRunDB).filter(DailyProductRunDB.id == run_id).first()
        if row is None:
            row = DailyProductRunDB(id=run_id, user_id=str(snapshot.get("user_id") or ""), mode=str(snapshot.get("mode") or "recommended"), status=str(snapshot.get("status") or "pending"))
            db.add(row)
        row.user_id = str(snapshot.get("user_id") or row.user_id or "")
        row.mode = str(snapshot.get("mode") or row.mode or "recommended")
        row.status = str(snapshot.get("status") or "pending")
        row.summary_json = snapshot.get("summary")
        row.recommendation_json = snapshot.get("recommendation")
        row.jobs_json = snapshot.get("jobs")
        row.created_at = _parse_db_datetime(snapshot.get("created_at")) or row.created_at
        row.finished_at = _parse_db_datetime(snapshot.get("finished_at"))
        db.commit()


def _get_daily_product_run(run_id: str) -> Dict[str, Any]:
    with _daily_product_runs_lock:
        row = _daily_product_runs.get(run_id, {})
    if row:
        normalized = dict(row)
        normalized.setdefault("run_id", run_id)
        return json.loads(json.dumps(normalized))
    with get_db_ctx() as db:
        db_row = db.query(DailyProductRunDB).filter(DailyProductRunDB.id == run_id).first()
        if db_row is None:
            return {}
        normalized = _daily_product_run_row_to_dict(db_row)
    with _daily_product_runs_lock:
        _daily_product_runs[run_id] = dict(normalized)
    return json.loads(json.dumps(normalized))


def _list_daily_product_runs(user_id: str, limit: int = 20) -> List[Dict[str, Any]]:
    with get_db_ctx() as db:
        rows = (
            db.query(DailyProductRunDB)
            .filter(DailyProductRunDB.user_id == user_id)
            .order_by(DailyProductRunDB.created_at.desc())
            .limit(max(1, min(int(limit), 100)))
            .all()
        )
        normalized_rows = [_daily_product_run_row_to_dict(row) for row in rows]
    with _daily_product_runs_lock:
        for row in normalized_rows:
            _daily_product_runs[str(row.get("run_id") or "")] = dict(row)
    return normalized_rows


def _daily_product_run_row_to_dict(row: DailyProductRunDB) -> Dict[str, Any]:
    return {
        "run_id": row.id,
        "user_id": row.user_id,
        "mode": row.mode,
        "status": row.status,
        "summary": row.summary_json or {},
        "recommendation": row.recommendation_json,
        "jobs": row.jobs_json or [],
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }


def _parse_db_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


def _ensure_job_event_queue(job_id: str) -> "asyncio.Queue[Dict[str, Any]]":
    with _jobs_lock:
        q = _job_events.get(job_id)
        if q is None:
            q = asyncio.Queue()
            _job_events[job_id] = q
        return q


def _emit_job_event(job_id: str, event: str, data: Dict[str, Any]) -> None:
    """Thread-safe event emitter: uses call_soon_threadsafe when called from a
    non-event-loop thread (e.g. inside asyncio.to_thread callbacks)."""
    payload = {
        "event": event,
        "data": data,
        "timestamp": _utcnow_iso(),
    }
    q = _ensure_job_event_queue(job_id)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # We are inside the event loop – safe to call directly
        q.put_nowait(payload)
    else:
        # Called from a worker thread – schedule on the main loop
        try:
            main_loop = asyncio.get_event_loop()
            main_loop.call_soon_threadsafe(q.put_nowait, payload)
        except RuntimeError:
            # Fallback: no event loop available at all
            q.put_nowait(payload)


def _attach_job_runtime_state(target: Any, job_id: Optional[str]) -> Any:
    if not job_id:
        return target
    job = _get_job(job_id)
    if not job:
        return target

    for field in (
        "waiting_ahead_count",
        "scheduled_running_count",
        "scheduled_concurrency_limit",
        "model_profile_id",
        "model_profile_name",
        "llm_provider",
        "quick_think_llm",
        "deep_think_llm",
        "backend_url",
        "experiment_id",
        "input_snapshot_hash",
    ):
        value = job.get(field)
        if value is not None or hasattr(target, field):
            setattr(target, field, value)
    return target


def _build_report_model_info(
    config: Dict[str, Any],
    model_profile_id: Optional[str],
    model_profile_name: Optional[str] = None,
    experiment_id: Optional[str] = None,
    input_snapshot_hash: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "model_profile_id": model_profile_id,
        "model_profile_name": model_profile_name,
        "llm_provider": str(config.get("llm_provider") or "").strip() or None,
        "quick_think_llm": str(config.get("quick_think_llm") or "").strip() or None,
        "deep_think_llm": str(config.get("deep_think_llm") or "").strip() or None,
        "backend_url": str(config.get("backend_url") or "").strip() or None,
        "experiment_id": str(experiment_id or "").strip() or None,
        "input_snapshot_hash": str(input_snapshot_hash or "").strip() or None,
    }


def _attach_report_model_info(target: Any) -> Any:
    payload = getattr(target, "result_data", None)
    info: Dict[str, Any] = {}
    if isinstance(payload, dict):
        nested = payload.get("model_info")
        if isinstance(nested, dict):
            info.update(nested)
        for key in (
            "model_profile_id",
            "model_profile_name",
            "llm_provider",
            "quick_think_llm",
            "deep_think_llm",
            "backend_url",
            "experiment_id",
            "input_snapshot_hash",
        ):
            if key in payload and payload.get(key) is not None:
                info[key] = payload.get(key)

    for key in (
        "model_profile_id",
        "model_profile_name",
        "llm_provider",
        "quick_think_llm",
        "deep_think_llm",
        "backend_url",
        "experiment_id",
        "input_snapshot_hash",
    ):
        value = info.get(key)
        if value is not None:
            setattr(target, key, value)

    if isinstance(payload, dict):
        fs = payload.get("freshness_summary")
        if isinstance(fs, dict):
            setattr(target, "freshness_summary", fs)
        if payload.get("freshness_status") and not getattr(target, "freshness_status", None):
            setattr(target, "freshness_status", payload.get("freshness_status"))
    if getattr(target, "freshness_status", None) is None:
        col_fs = getattr(target, "freshness_status", None)
        if col_fs:
            setattr(target, "freshness_status", col_fs)
    return target


def _fill_missing_model_profile_names(db: Session, *, user_id: str, reports: list[Any]) -> None:
    missing_ids: set[str] = set()
    for report in reports:
        pid = str(getattr(report, "model_profile_id", "") or "").strip()
        pname = str(getattr(report, "model_profile_name", "") or "").strip()
        if pid and not pname:
            missing_ids.add(pid)
    if not missing_ids:
        return

    name_map: dict[str, str] = {}
    for profile_id in missing_ids:
        row = model_profile_service.get_model_profile(db, user_id, profile_id)
        if row and str(row.name or "").strip():
            name_map[profile_id] = str(row.name).strip()
    if not name_map:
        return

    for report in reports:
        pid = str(getattr(report, "model_profile_id", "") or "").strip()
        if pid and not str(getattr(report, "model_profile_name", "") or "").strip():
            setattr(report, "model_profile_name", name_map.get(pid))


def _extract_request_user_context(request: UserContextInput) -> Dict[str, Any]:
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


def _merge_user_context_payload(
    explicit_context: Dict[str, Any],
    inferred_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    merged = normalize_user_context(inferred_context or {})
    merged.update(normalize_user_context(explicit_context or {}))
    return merged


def _compose_analysis_user_context(
    db: Session,
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
    return _merge_user_context_payload(explicit_context or {}, inferred_context or {})


def _apply_user_context_to_request(request: "AnalyzeRequest", user_context: Dict[str, Any]) -> "AnalyzeRequest":
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


def _apply_prompt_template_to_request(
    db: Session,
    user_id: str,
    request: "AnalyzeRequest",
    *,
    fallback_template_id: str,
    horizon: Optional[str] = None,
    default_only_when_missing: bool = True,
) -> "AnalyzeRequest":
    template_id = (request.prompt_template_id or "").strip()
    if not template_id and request.query:
        return request
    if default_only_when_missing and not template_id and not request.query:
        return request
    symbol = _normalize_symbol((request.symbol or "").strip().upper())
    if not symbol:
        return request
    code_to_name = _get_reverse_stock_map()
    template = prompt_template_service.resolve_template(
        db,
        user_id,
        template_id or fallback_template_id,
        fallback_template_id=fallback_template_id,
        scope=prompt_template_service.SCOPE_DEEP_ANALYSIS,
    )
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


def _build_result_payload(final_state: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "symbol": final_state.get("company_of_interest"),
        "trade_date": final_state.get("trade_date"),
        "direction": None,
        "instrument_context": final_state.get("instrument_context"),
        "market_context": final_state.get("market_context"),
        "user_context": final_state.get("user_context"),
        "workflow_context": final_state.get("workflow_context"),
        "market_report": final_state.get("market_report"),
        "sentiment_report": final_state.get("sentiment_report"),
        "news_report": final_state.get("news_report"),
        "fundamentals_report": final_state.get("fundamentals_report"),
        "macro_report": final_state.get("macro_report"),
        "smart_money_report": final_state.get("smart_money_report"),
        "volume_price_report": final_state.get("volume_price_report"),
        "game_theory_report": final_state.get("game_theory_report"),
        "game_theory_signals": final_state.get("game_theory_signals"),
        "analyst_traces": final_state.get("analyst_traces"),
        "investment_plan": final_state.get("investment_plan"),
        "trader_investment_plan": final_state.get("trader_investment_plan"),
        "risk_feedback_state": final_state.get("risk_feedback_state"),
        "final_trade_decision": final_state.get("final_trade_decision"),
        "metadata": final_state.get("metadata"),
    }


def _attach_consensus_summary(result: Dict[str, Any]) -> Dict[str, Any]:
    summary = consensus_service.build_consensus_summary(
        result_data=result,
        final_direction=result.get("direction"),
        final_confidence=result.get("confidence"),
    )
    if summary:
        result["consensus_summary"] = summary
    return result


def _attach_freshness_to_job_result(
    result: Dict[str, Any],
    symbol: str,
    trade_date: str,
) -> Dict[str, Any]:
    """Attach freshness_summary from DataCollector pool before persisting report."""
    if result.get("freshness_summary"):
        return result
    pool_data = _shared_data_collector.get(symbol, trade_date) or {}
    freshness_pool = pool_data.get("freshness_pool")
    try:
        from tradingagents.dataflows.freshness import attach_freshness_to_result

        attach_freshness_to_result(result, freshness_pool, trade_date, symbol)
    except Exception as exc:
        logger.error("Failed to attach freshness summary: %s", exc)
        from tradingagents.dataflows.freshness import attach_freshness_to_result as _attach

        _attach(result, None, trade_date, symbol)
    return result


def _safe_stream_state_dict(
    value: Any,
    *,
    key: str,
    job_id: str,
    horizon: Optional[str],
) -> Dict[str, Any]:
    """Ensure streamed LangGraph state fields keep dict shape.

    We observed rare cases where debate states arrive as plain strings in stream
    chunks; that breaks `.get(...)` access and aborts the whole horizon.
    """
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    preview = str(value).replace("\n", " ")[:200]
    logger.warning(
        "[StreamState] job=%s horizon=%s key=%s expected=dict got=%s preview=%s",
        job_id,
        horizon or "-",
        key,
        type(value).__name__,
        preview,
    )
    return {}


def _normalize_stream_chunk(
    chunk: Any,
    *,
    job_id: str,
    horizon: Optional[str],
) -> Dict[str, Any]:
    """Normalize stream chunks into dict and coerce known nested state fields."""
    if not isinstance(chunk, dict):
        logger.warning(
            "[StreamState] job=%s horizon=%s chunk expected=dict got=%s preview=%s",
            job_id,
            horizon or "-",
            type(chunk).__name__,
            str(chunk)[:200],
        )
        return {}
    normalized = dict(chunk)
    if "investment_debate_state" in normalized:
        normalized["investment_debate_state"] = _safe_stream_state_dict(
            normalized.get("investment_debate_state"),
            key="investment_debate_state",
            job_id=job_id,
            horizon=horizon,
        )
    if "risk_debate_state" in normalized:
        normalized["risk_debate_state"] = _safe_stream_state_dict(
            normalized.get("risk_debate_state"),
            key="risk_debate_state",
            job_id=job_id,
            horizon=horizon,
        )
    return normalized


class AgentProgressTracker:
    # 阶段标题映射
    STAGE_TITLES = {
        "market_analysis": "市场分析完成",
        "sentiment_analysis": "舆情分析完成",
        "news_analysis": "新闻分析完成",
        "fundamentals_analysis": "基本面分析完成",
        "research_decision": "研究团队决策",
        "trader_plan": "交易计划制定",
        "risk_assessment": "风险评估完成",
        "final_decision": "最终决策",
    }
    
    def __init__(self, selected_analysts: List[str], job_id: str, horizon: Optional[str] = None):
        self.job_id = job_id
        self.horizon = horizon
        self.selected_analysts = [a.lower() for a in selected_analysts]
        self.status: Dict[str, str] = {}
        self.start_times: Dict[str, float] = {}  # 记录每个 agent 开始时间
        self.report_sections: Dict[str, Optional[str]] = {
            "market_report": None,
            "sentiment_report": None,
            "news_report": None,
            "fundamentals_report": None,
            "macro_report": None,
            "smart_money_report": None,
            "volume_price_report": None,
            "game_theory_report": None,
            "investment_plan": None,
            "trader_investment_plan": None,
            "final_trade_decision": None,
        }
        # 跟踪已完成的阶段，避免重复发送里程碑
        self._completed_stages: set = set()
        # 跟踪已发送的 writing 状态，避免重复发送
        self._writing_status_sent: set = set()
        
        for team_agents in FIXED_TEAMS.values():
            for agent in team_agents:
                self.status[agent] = "pending"

        # 未选中的分析师标记为 skipped（仍展示，便于固定 12-agent 看板）
        for key in ANALYST_ORDER:
            agent = ANALYST_AGENT_NAMES[key]
            if key not in self.selected_analysts:
                self.status[agent] = "skipped"

    def _emit_milestone(self, stage: str, summary: str = "") -> None:
        """发送用户可见的里程碑事件"""
        if stage in self._completed_stages:
            return
        self._completed_stages.add(stage)
        
        title = self.STAGE_TITLES.get(stage, stage)
        _emit_job_event(
            self.job_id,
            "agent.milestone",
            {
                "stage": stage,
                "title": title,
                "summary": summary,
                "timestamp": _utcnow_iso(),
                "horizon": self.horizon,
            },
        )
        _log(f"[Milestone] {title}: {summary[:100]}...")

    def _emit_report_chunked(self, job_id: str, section: str, content: str) -> None:
        """将报告内容分片发送，直接透传不做人工延迟
        
        按较大块分片（如按段落），让前端自然渲染
        """
        # 按段落分割，保持Markdown结构
        paragraphs = content.split('\n\n')
        
        for i, para in enumerate(paragraphs):
            if not para.strip():
                continue
                
            _emit_job_event(
                job_id,
                "agent.report.chunk",
                {
                    "section": section,
                    "chunk": para + '\n\n',
                    "index": i,
                    "is_complete": False,
                    "horizon": self.horizon,
                },
            )
        
        # 发送完成标记
        _emit_job_event(
            job_id,
            "agent.report.chunk",
            {
                "section": section,
                "chunk": "",
                "index": -1,
                "is_complete": True,
                "horizon": self.horizon,
            },
        )

    def snapshot(self) -> Dict[str, Any]:
        agents = []
        for team, members in FIXED_TEAMS.items():
            for m in members:
                agents.append({"team": team, "agent": m, "status": self.status.get(m, "pending")})
        return {"agents": agents, "horizon": self.horizon}

    def _set_status(self, agent: str, status: str) -> None:
        prev = self.status.get(agent)
        if prev == status:
            return
        self.status[agent] = status
        
        # 记录时间
        if status == "in_progress":
            self.start_times[agent] = time.time()
        elif status == "completed" and agent in self.start_times:
            duration = time.time() - self.start_times[agent]
            _log(f"[Timer] Agent {agent} ({self.horizon or 'main'}) finished in {duration:.2f}s")

        _emit_job_event(
            self.job_id,
            "agent.status",
            {"agent": agent, "status": status, "previous_status": prev, "horizon": self.horizon},
        )

    def _update_research_team_status(self, status: str) -> None:
        for agent in ["Bull Researcher", "Bear Researcher", "Research Manager"]:
            self._set_status(agent, status)

    def _generate_stage_summary(self, stage: str, chunk: Dict[str, Any]) -> str:
        """根据阶段生成简要总结"""
        if stage == "market_analysis":
            report = chunk.get("market_report", "")
            # 提取关键信息
            if "支撑" in report or "压力" in report:
                return "技术面关键位已识别"
            return "技术面分析完成"
        elif stage == "sentiment_analysis":
            return "舆情数据已收集"
        elif stage == "news_analysis":
            return "新闻影响已评估"
        elif stage == "fundamentals_analysis":
            return "基本面指标已计算"
        elif stage == "research_decision":
            return "多空观点已形成"
        elif stage == "trader_plan":
            return "交易策略已制定"
        elif stage == "risk_assessment":
            return "风险水平已评估"
        elif stage == "final_decision":
            decision = chunk.get("final_trade_decision", "")
            return f"最终建议: {decision[:50]}..." if len(decision) > 50 else f"最终建议: {decision}"
        return ""

    def _emit_writing_status(self, agent_name: str, report_type: str) -> None:
        """发送正在编写报告的状态（每个agent只发送一次）"""
        # 检查是否已经发送过
        status_key = f"{agent_name}:{report_type}"
        if status_key in self._writing_status_sent:
            return
        self._writing_status_sent.add(status_key)
        
        report_names = {
            "market_report": "市场分析",
            "sentiment_report": "舆情分析",
            "news_report": "新闻分析",
            "fundamentals_report": "基本面分析",
            "investment_plan": "投资计划",
            "trader_investment_plan": "交易计划",
            "final_trade_decision": "最终交易决策",
        }
        _emit_job_event(
            self.job_id,
            "agent.writing",
            {
                "agent": agent_name,
                "report": report_type,
                "report_name": report_names.get(report_type, report_type),
                "status": "writing",
                "horizon": self.horizon,
            },
        )

    def _emit_token(self, agent_name: str, report_type: str, token: str) -> None:
        """推送 Token 级别的流式内容（跳过空 token，避免思维模型推理阶段刷屏）"""
        if not token:
            return
        _emit_job_event(
            self.job_id,
            "agent.token",
            {
                "agent": agent_name,
                "report": report_type,
                "token": token,
                "horizon": self.horizon,
            },
        )

    def emit_debate_token(
        self, debate: str, agent: str, round_num: int, token: str,
    ) -> None:
        """推送辩论 token（流式输出，每个 chunk 调用一次）"""
        if not token:
            return
        try:
            _emit_job_event(
                self.job_id,
                "agent.debate.token",
                {
                    "debate": debate,
                    "agent": agent,
                    "round": round_num,
                    "token": token,
                    "horizon": self.horizon,
                },
            )
        except Exception:
            pass

    def emit_debate_message(
        self, debate: str, agent: str, round_num: int,
        content: str, is_verdict: bool = False,
    ) -> None:
        """推送辩论消息（每个 agent 每轮完成后调用一次）"""
        if not content:
            return
        try:
            _emit_job_event(
                self.job_id,
                "agent.debate",
                {
                    "debate": debate,
                    "agent": agent,
                    "round": round_num,
                    "content": content,
                    "is_verdict": is_verdict,
                    "horizon": self.horizon,
                },
            )
        except Exception:
            logging.getLogger(__name__).warning(
                "Failed to emit debate message for %s in %s", agent, debate, exc_info=True,
            )

    def apply_chunk(self, chunk: Dict[str, Any]) -> None:
        # 分析师阶段状态推进
        found_active = False
        for analyst_key in ANALYST_ORDER:
            if analyst_key not in self.selected_analysts:
                continue

            agent_name = ANALYST_AGENT_NAMES[analyst_key]
            report_key = ANALYST_REPORT_MAP[analyst_key]
            has_report = bool(chunk.get(report_key))

            if has_report:
                if self.status.get(agent_name) != "completed":
                    self._set_status(agent_name, "completed")
                    self.report_sections[report_key] = chunk.get(report_key)
            elif not found_active:
                # 只在状态从 pending 变为 in_progress 时发送 writing 状态
                prev_status = self.status.get(agent_name)
                if prev_status != "in_progress":
                    self._set_status(agent_name, "in_progress")
                    # 发送正在分析的状态（只发送一次）
                    self._emit_writing_status(agent_name, report_key)
                found_active = True
            else:
                self._set_status(agent_name, "pending")

        # 分析师全部完成后，启动 Bull Researcher
        if not found_active and self.selected_analysts:
            if self.status.get("Bull Researcher") == "pending":
                self._set_status("Bull Researcher", "in_progress")

        # 研究团队状态更新
        debate_state = _safe_stream_state_dict(
            chunk.get("investment_debate_state"),
            key="investment_debate_state",
            job_id=self.job_id,
            horizon=self.horizon,
        )
        bull_hist = str(debate_state.get("bull_history", "")).strip()
        bear_hist = str(debate_state.get("bear_history", "")).strip()
        judge = str(debate_state.get("judge_decision", "")).strip()
        if bull_hist or bear_hist:
            self._update_research_team_status("in_progress")
        if judge:
            self._update_research_team_status("completed")
            if self.status.get("Trader") != "in_progress":
                self._set_status("Trader", "in_progress")
                self._emit_writing_status("Trader", "trader_investment_plan")

        # 交易团队
        if chunk.get("trader_investment_plan"):
            if self.status.get("Trader") != "completed":
                self._set_status("Trader", "completed")
                self._set_status("Aggressive Analyst", "in_progress")

        # 风控与组合团队（发送最终决策）
        risk_state = _safe_stream_state_dict(
            chunk.get("risk_debate_state"),
            key="risk_debate_state",
            job_id=self.job_id,
            horizon=self.horizon,
        )
        risk_judge = str(risk_state.get("judge_decision", "")).strip()

        if risk_judge:
            if self.status.get("Portfolio Manager") != "completed":
                self._set_status("Portfolio Manager", "in_progress")
                self._set_status("Aggressive Analyst", "completed")
                self._set_status("Conservative Analyst", "completed")
                self._set_status("Neutral Analyst", "completed")
                self._set_status("Portfolio Manager", "completed")
                final_summary = self._generate_stage_summary("final_decision", chunk)
                self._emit_milestone("final_decision", final_summary)


def _extract_message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts).strip()
    return str(content)


def _generate_tool_description(tool_name: str, tool_args: Dict[str, Any]) -> str:
    """生成工具调用的可读描述"""
    if tool_name == "get_indicators":
        indicator = tool_args.get("indicator")
        if isinstance(indicator, str) and indicator:
            indicator_map = {
                "close_50_sma": "50日均线",
                "close_200_sma": "200日均线",
                "close_10_ema": "10日EMA",
                "close_20_ema": "20日EMA",
                "rsi": "RSI",
                "macd": "MACD",
                "boll": "布林中轨",
                "boll_ub": "布林上轨",
                "boll_lb": "布林下轨",
                "atr": "ATR波动率",
                "vwma": "VWMA量价均线",
                "obv": "OBV能量潮",
            }
            return f"计算 {indicator_map.get(indicator, indicator)}"
        return "获取技术指标"
    elif tool_name == "get_stock_data":
        return "获取股票历史数据"
    elif tool_name == "get_fundamentals":
        metrics = tool_args.get("metrics", [])
        if metrics:
            return f"获取 {', '.join(metrics[:2])}{' 等' if len(metrics) > 2 else ''} 基本面数据"
        return "获取基本面数据"
    elif tool_name == "get_income_statement":
        return "获取利润表"
    elif tool_name == "get_balance_sheet":
        return "获取资产负债表"
    elif tool_name == "get_cash_flow":
        return "获取现金流量表"
    elif tool_name == "get_news":
        return "获取相关新闻"
    elif tool_name == "get_social_sentiment":
        return "获取舆情数据"
    return f"调用 {tool_name}"


async def _run_job(
    job_id: str,
    request: AnalyzeRequest,
    stream_events: bool = False,
    save_report: bool = True,
    user_id: Optional[str] = None,
    request_source: str = "api",
) -> None:
    # 用 asyncio.Task + sleep 竞速代替 wait_for，避免 cancel 卡在 to_thread 导致
    # semaphore 永远不释放的问题。超时后标记失败但不 cancel 内部协程（让线程自然结束）。
    inner_task = asyncio.create_task(
        _run_job_inner(job_id, request, stream_events, save_report, user_id, request_source)
    )
    done, _ = await asyncio.wait({inner_task}, timeout=_JOB_TIMEOUT)
    if inner_task in done:
        # 正常完成（可能成功也可能异常）
        if not inner_task.cancelled() and inner_task.exception():
            _log(f"[Job {job_id}] failed: {inner_task.exception()}")
        return
    # 超时：标记失败，但不 cancel 内部 task（避免 cancel 卡住）
    err_msg = f"任务超时（超过 {_JOB_TIMEOUT} 秒），已自动终止"
    _log(f"[Job {job_id}] {err_msg}")
    _set_job(job_id, status="failed", error=err_msg, finished_at=_utcnow_iso())
    try:
        def _record_timeout():
            with get_db_ctx() as db:
                report_service.mark_report_failed(db, job_id, err_msg)
        await asyncio.to_thread(_record_timeout)
    except Exception:
        pass
    _emit_job_event(job_id, "job.failed", {"job_id": job_id, "error": err_msg})


async def _run_job_inner(
    job_id: str,
    request: AnalyzeRequest,
    stream_events: bool = False,
    save_report: bool = True,
    user_id: Optional[str] = None,
    request_source: str = "api",
) -> None:
    job_start_t = time.time()
    # Normalize for logic but keep original for display
    display_name = request.symbol
    normalized_symbol = _normalize_symbol(request.symbol)
    selected_model_profile_name: Optional[str] = None

    # ── Step 0: Initialize report in DB (short-lived session) ──
    def _init_and_configure():
        nonlocal selected_model_profile_name
        with get_db_ctx() as db:
            try:
                report_service.init_report(
                    db=db,
                    report_id=job_id,
                    symbol=normalized_symbol,
                    trade_date=request.trade_date,
                    user_id=user_id,
                )
            except Exception as e:
                _log(f"CRITICAL: Failed to initialize report in DB: {e}")
            # 方案A：未显式指定模型配置时，自动落到数据库中的「系统默认模型」（is_default=True 的 model profile）
            effective_profile_id = _resolve_effective_model_profile_id(
                db,
                user_id=user_id,
                model_profile_id=request.model_profile_id,
            )
            merged_overrides = _merge_model_profile_overrides(
                db,
                user_id=user_id,
                model_profile_id=effective_profile_id,
                request_overrides=request.config_overrides,
            )
            if user_id and effective_profile_id:
                profile_row = model_profile_service.get_model_profile(db, user_id, effective_profile_id)
                if profile_row:
                    selected_model_profile_name = str(profile_row.name or "").strip() or None
            config = _build_runtime_config(
                merged_overrides,
                user_id=user_id,
                trusted_overrides=True,
                strategy="profile_selected" if effective_profile_id else "default",
            )
            model_info = _build_report_model_info(
                config,
                effective_profile_id,
                selected_model_profile_name,
                request.experiment_id,
                request.input_snapshot_hash,
            )
            early_payload = {"model_info": dict(model_info), **model_info}
            report_service.update_report_partial(
                db,
                job_id,
                status="running",
                result_data=early_payload,
            )
        return config, effective_profile_id

    config, effective_profile_id = await asyncio.to_thread(_init_and_configure)
    runtime_model_info = _build_report_model_info(
        config,
        effective_profile_id,
        selected_model_profile_name,
        request.experiment_id,
        request.input_snapshot_hash,
    )
    _set_job(
        job_id,
        status="running",
        started_at=_utcnow_iso(),
        symbol=normalized_symbol,
        **runtime_model_info,
    )

    _emit_job_event(
        job_id,
        "job.running",
        {
            "job_id": job_id,
            "symbol": normalized_symbol,
            "display_name": display_name,
            "trade_date": request.trade_date
        },
    )
    # Ensure request object uses the normalized symbol for internal logic
    request.symbol = normalized_symbol
    user_context_payload = _extract_request_user_context(request)
    tracker = AgentProgressTracker(request.selected_analysts, job_id)
    _emit_job_event(job_id, "agent.snapshot", tracker.snapshot())

    try:
        if request.dry_run:
            result = {
                "mode": "dry_run",
                "symbol": request.symbol,
                "trade_date": request.trade_date,
                "model_profile_id": effective_profile_id,
                "experiment_id": request.experiment_id,
                "input_snapshot_hash": request.input_snapshot_hash,
                "selected_analysts": request.selected_analysts,
                "user_context": user_context_payload,
                "llm_provider": config.get("llm_provider"),
                "data_vendors": config.get("data_vendors"),
            }
            _set_job(
                job_id,
                status="completed",
                result=result,
                decision="DRY_RUN",
                finished_at=_utcnow_iso(),
            )
            _emit_job_event(
                job_id,
                "job.completed",
                {"job_id": job_id, "decision": "DRY_RUN", "result": result},
            )
            return

        _shared_data_collector.ref(request.symbol, request.trade_date)
        graph = TradingAgentsGraph(
            selected_analysts=request.selected_analysts,
            debug=False,
            config=config,
            data_collector=_shared_data_collector,
        )
        final_state: Optional[Dict[str, Any]] = None

        # 强制单周期：多个 horizon 时只取第一个，避免 dual-horizon 双倍开销
        if not request.horizons:
            request.horizons = ["short"]
        elif len(request.horizons) > 1:
            request.horizons = [request.horizons[0]]

        # ── Dual-horizon intent-driven path ──────────────────────────────────
        if request.query:
            # 1. 组装用户意图
            intent_start_t = time.time()
            ticker = request.symbol or display_name

            # 优先使用已由 chat_completions 预解析的 intent（单次 LLM），避免二次调用
            if request.user_intent:
                user_intent = dict(request.user_intent)
                user_intent["ticker"] = ticker
                user_intent["horizons"] = request.horizons
            else:
                # 直接 POST /v1/analyze 时的兜底（无预解析 intent）
                user_intent = await asyncio.to_thread(_parse_intent, request.query, graph.quick_thinking_llm, fallback_ticker=ticker)
                if not request.horizons:
                    request.horizons = user_intent["horizons"]
                user_intent["horizons"] = request.horizons
            _log(f"[Timer] Intent Parsing took {time.time() - intent_start_t:.2f}s")

            inferred_user_context = user_intent.get("user_context") or {}
            user_context_payload = _merge_user_context_payload(
                user_context_payload,
                inferred_user_context,
            )
            user_intent["user_context"] = user_context_payload

            # Use normalized ticker from intent parser if available
            ticker = user_intent.get("ticker") or ticker
            nt = _normalize_symbol(str(ticker))
            ticker = nt

            # 2. 一次性采集数据，短线/中线共用缓存
            lookback_label = "14天关键行情" if request.horizons == ["short"] else "90天全量行情、财务、新闻、资金"
            _emit_job_event(job_id, "agent.tool_call", {
                "agent": "数据采集", "tool": "data_collector",
                "description": f"预加载 {ticker} 近{lookback_label}数据…",
            })
            _log(f"[DualHorizon] Collecting data for {ticker} {request.trade_date} (horizons={request.horizons})…")
            collect_start_t = time.time()
            await asyncio.to_thread(graph.data_collector.collect, ticker, request.trade_date, horizons=request.horizons)
            _log(f"[Timer] Data Collection step in _run_job took {time.time() - collect_start_t:.2f}s")

            _emit_job_event(job_id, "agent.tool_call", {
                "agent": "数据采集", "tool": "data_collector",
                "description": "数据采集完成，开始多维度分析",
            })

            report_keys = (
                "market_report", "sentiment_report", "news_report", "fundamentals_report",
                "macro_report", "smart_money_report", "volume_price_report",
                "investment_plan", "trader_investment_plan", "final_trade_decision",
            )

            horizon_states: Dict[str, Any] = {}

            async def _process_horizon(horizon: str):
                """Async helper to run analysis for a single horizon."""
                # 根据周期过滤 analyst，共享已采集的数据缓存
                horizon_analysts = _get_horizon_analysts(horizon, request.selected_analysts)
                horizon_graph = TradingAgentsGraph(
                    selected_analysts=horizon_analysts,
                    debug=False,
                    config=config,
                    data_collector=graph.data_collector,
                )

                horizon_label = "短线" if horizon == "short" else "中线"
                _emit_job_event(job_id, "agent.horizon_start", {
                    "horizon": horizon, "label": horizon_label,
                })
                # 每轮重置 tracker，前端进度条重新走一遍
                h_tracker = AgentProgressTracker(horizon_analysts, job_id, horizon=horizon)
                _emit_job_event(job_id, "agent.snapshot", h_tracker.snapshot())
                # 告知前端本轮参与的 analyst 即将开始
                for analyst_key in ANALYST_ORDER:
                    if analyst_key in horizon_analysts:
                        aname = ANALYST_AGENT_NAMES[analyst_key]
                        h_tracker._set_status(aname, "in_progress")
                        h_tracker._emit_writing_status(aname, ANALYST_REPORT_MAP[analyst_key])

                h_args = horizon_graph.propagator.get_graph_args()

                # Use thread_id for LangGraph checkpointer persistence
                if "config" not in h_args:
                    h_args["config"] = {}
                h_args["config"]["configurable"] = {"thread_id": f"{job_id}_{horizon}"}

                init_state = horizon_graph.propagator.create_initial_state(
                    ticker, request.trade_date,
                    user_context=user_context_payload,
                    selected_analysts=horizon_analysts,
                    request_source=request_source,
                    user_intent=user_intent, horizon=horizon,
                )
                from tradingagents.dataflows.freshness import inject_freshness_into_state

                inject_freshness_into_state(
                    init_state, graph.data_collector, ticker, request.trade_date
                )
                last_report: Dict[str, str] = {}
                seen: Dict[str, bool] = {}   # 追踪哪些字段已出现过，避免重复事件
                horizon_final = None

                # DB 更新使用短生命周期 session，避免长期占用连接池
                def _horizon_partial_update(updates: dict):
                    with get_db_ctx() as _hdb:
                        report_service.update_report_partial(_hdb, job_id, **updates)

                # 通过 ContextVar 将 tracker 传入 async 节点（LangGraph 不传递 schema 外的字段）
                _tracker_token = current_tracker_var.set(h_tracker)
                try:
                    async for chunk in horizon_graph.graph.astream(init_state, **h_args):
                        chunk = _normalize_stream_chunk(chunk, job_id=job_id, horizon=horizon)
                        horizon_final = chunk

                        # ── 并行感知的状态推进 ──────────────────
                        # 1. 每个 analyst 报告首次出现 → completed
                        for analyst_key in ANALYST_ORDER:
                            if analyst_key not in horizon_analysts:
                                continue
                            rkey = ANALYST_REPORT_MAP[analyst_key]
                            aname = ANALYST_AGENT_NAMES[analyst_key]
                            if chunk.get(rkey) and not seen.get(rkey):
                                seen[rkey] = True
                                h_tracker._set_status(aname, "completed")

                        # 2. 分析师全部完成后 → Bull/Bear/ResearchManager 开始
                        all_analysts_done = all(
                            seen.get(ANALYST_REPORT_MAP.get(a, "")) for a in h_tracker.selected_analysts
                        )
                        if all_analysts_done and not seen.get("_research_started"):
                            seen["_research_started"] = True
                            h_tracker._set_status(ANALYST_AGENT_NAMES["bull"], "in_progress")
                            h_tracker._set_status(ANALYST_AGENT_NAMES["bear"], "in_progress")
                            h_tracker._set_status(ANALYST_AGENT_NAMES["research_manager"], "in_progress")

                        # 3. research judge → 研究团队完成, Trader 开始
                        debate = chunk.get("investment_debate_state") or {}
                        if debate.get("judge_decision") and not seen.get("judge_decision"):
                            seen["judge_decision"] = True
                            for r_key in ["bull", "bear", "research_manager"]:
                                h_tracker._set_status(ANALYST_AGENT_NAMES[r_key], "completed")
                            h_tracker._set_status(ANALYST_AGENT_NAMES["trader"], "in_progress")
                            h_tracker._emit_writing_status(ANALYST_AGENT_NAMES["trader"], "trader_investment_plan")

                        # 4. trader plan → Trader completed, 风控开始
                        if chunk.get("trader_investment_plan") and not seen.get("trader_investment_plan"):
                            seen["trader_investment_plan"] = True
                            h_tracker._set_status(ANALYST_AGENT_NAMES["trader"], "completed")
                            h_tracker._set_status(ANALYST_AGENT_NAMES["aggressive"], "in_progress")

                        # 5. risk judge → 风控全部完成
                        risk = chunk.get("risk_debate_state") or {}
                        if risk.get("judge_decision") and not seen.get("risk_judge_decision"):
                            seen["risk_judge_decision"] = True
                            for r_key in ["aggressive", "neutral", "conservative", "portfolio_manager"]:
                                h_tracker._set_status(ANALYST_AGENT_NAMES[r_key], "completed")
                        # ── end 并行感知 ────────────────────────────────────────────

                        # 报告分片推送与数据库即时更新
                        db_updates = {}
                        for key in report_keys:
                            value = chunk.get(key)
                            if value and value != last_report.get(key):
                                last_report[key] = value
                                db_updates[key] = str(value)
                                h_tracker._emit_report_chunked(job_id, key, str(value))

                        if db_updates:
                            await asyncio.to_thread(_horizon_partial_update, db_updates)
                except Exception as e:
                    _log(f"Error during horizon streaming ({horizon}): {e}")
                    raise
                finally:
                    current_tracker_var.reset(_tracker_token)

                horizon_states[horizon] = horizon_final
                for agent, st in h_tracker.status.items():
                    if st not in ("completed", "skipped"):
                        h_tracker._set_status(agent, "completed")
                _emit_job_event(job_id, "agent.horizon_done", {"horizon": horizon})

            # 3. 按解析出的 horizons 并行运行 astream()，事件实时推给前端
            results = await asyncio.gather(
                *[_process_horizon(h) for h in request.horizons],
                return_exceptions=True,
            )
            horizon_errors = []
            for i, r in enumerate(results):
                if isinstance(r, Exception):
                    _log(f"Horizon '{request.horizons[i]}' failed: {r}")
                    horizon_errors.append(f"{request.horizons[i]}: {r}")
            if horizon_errors:
                raise RuntimeError(f"Horizon analysis failed: {'; '.join(horizon_errors)}")

            short_r = graph._build_horizon_result("short", horizon_states.get("short") or {})
            medium_r = graph._build_horizon_result("medium", horizon_states.get("medium") or {})
            primary_r = short_r if horizon_states.get("short") else medium_r
            result = {
                "symbol": ticker,
                "trade_date": request.trade_date,
                "mode": "dual_horizon",
                "user_intent": user_intent,
                "short_term": short_r,
                "medium_term": medium_r,
                # Hoist primary horizon's report fields to top level so that
                # resolve_report_fields / create_report can find them directly.
                "final_trade_decision": primary_r.get("final_trade_decision", ""),
                "investment_plan": primary_r.get("investment_plan", ""),
                "trader_investment_plan": primary_r.get("trader_investment_plan", ""),
                "market_report": primary_r.get("market_report", ""),
                "sentiment_report": primary_r.get("sentiment_report", ""),
                "news_report": primary_r.get("news_report", ""),
                "fundamentals_report": primary_r.get("fundamentals_report", ""),
                "macro_report": primary_r.get("macro_report", ""),
                "smart_money_report": primary_r.get("smart_money_report", ""),
                "volume_price_report": primary_r.get("volume_price_report", ""),
                # 双视角路径必须把主视角的风控状态提到顶层：共识层的 risk_gate /
                # flip_conditions 从 result_data["risk_feedback_state"] 读取，
                # 缺失时会静默退化为默认 "pass"（实测 99.94% 恒为 pass）。
                "risk_feedback_state": dict(primary_r.get("risk_feedback_state") or {}),
                "metadata": dict(primary_r.get("metadata") or {}),
                "analyst_traces": report_quality_service.merge_dual_horizon_traces(
                    short_r.get("analyst_traces", []),
                    medium_r.get("analyst_traces", []),
                ),
            }
            report_quality_service.reconcile_verdict_with_consensus(result)
            report_quality_service.attach_methodology_snapshot(result, config)
            # Abstention is recorded as "NA", never as the literal "UNKNOWN": a
            # placeholder string in the decision column is indistinguishable from a
            # real decision in every downstream average, and it was previously
            # counted as a scored observation.
            decision = coerce_persistable_decision(
                graph.process_signal(str(result.get("final_trade_decision") or ""))
            )
            result["decision"] = decision
            # LLM 结构化提取（目标价、止损、信心、风险、关键指标）
            # 注意：必须在 _set_job(status="completed") 之前完成，否则 SSE 超时
            # 会因为看到 status="completed" 而提前关闭流，导致 job.completed 事件丢失。
            structured = None
            try:
                structured = await asyncio.to_thread(
                    report_service.extract_structured_data,
                    final_trade_decision=str(result.get("final_trade_decision") or ""),
                    fundamentals_report=primary_r.get("fundamentals_report", ""),
                    config=config,
                )
            except Exception as e:
                _log(f"Structured extraction failed (non-fatal): {e}")

            resolved = await asyncio.to_thread(
                report_service.resolve_report_fields,
                result_data=result,
                target_price_override=structured.target_price if structured else None,
                stop_loss_override=structured.stop_loss_price if structured else None,
            )
            result.update({
                "direction": resolved["direction"],
                "confidence": resolved["confidence"],
                "target_price": resolved["target_price"],
                "stop_loss_price": resolved["stop_loss_price"],
            })
            model_info = _build_report_model_info(
                config,
                effective_profile_id,
                selected_model_profile_name,
                request.experiment_id,
                request.input_snapshot_hash,
            )
            result.update(model_info)
            result["model_info"] = dict(model_info)
            _attach_consensus_summary(result)
            _attach_freshness_to_job_result(result, request.symbol, request.trade_date)
            # 计划块里自带的止损/止盈锚点回填到研报字段，避免结构化提取漏掉就永久丢失
            _backfill_plan_anchors(result)

            # 自动保存报告到数据库
            if save_report:
                def _save_report_sync():
                    with get_db_ctx() as save_db:
                        report_service.create_report(
                            db=save_db,
                            symbol=request.symbol,
                            trade_date=request.trade_date,
                            decision=decision,
                            result_data=result,
                            user_id=user_id,
                            risk_items=([r.model_dump() for r in structured.risks] if structured else None),
                            key_metrics=([m.model_dump() for m in structured.key_metrics] if structured else None),
                            target_price_override=result["target_price"],
                            stop_loss_override=result["stop_loss_price"],
                            report_id=job_id,
                            analyst_traces=result.get("analyst_traces"),
                        )
                        _persist_trade_plan_from_result(
                            save_db,
                            user_id=user_id,
                            job_id=job_id,
                            request=request,
                            result=result,
                            horizon="dual",
                        )
                        save_db.commit()

                try:
                    await asyncio.to_thread(_save_report_sync)
                except Exception as e:
                    _log(f"Failed to save report: {e}")

            # 所有后处理完成后再标记 completed，防止 SSE 超时提前关闭流
            _set_job(job_id, status="completed", result=result,
                     decision=decision, finished_at=_utcnow_iso())
            _emit_job_event(job_id, "job.completed", {
                "job_id": job_id, "decision": decision,
                "direction": result["direction"],
                "result": result, "mode": "dual_horizon",
                "risk_items": [r.model_dump() for r in structured.risks] if structured else [],
                "key_metrics": [m.model_dump() for m in structured.key_metrics] if structured else [],
                "confidence": result["confidence"],
                "target_price": result["target_price"],
                "stop_loss_price": result["stop_loss_price"],
            })
            _log(f"Job completed successfully: {job_id}")
            _log(f"[Timer] TOTAL Job execution (dual_horizon) took {time.time() - job_start_t:.2f}s")
            return
        # ── End dual-horizon path ─────────────────────────────────────────────

        if stream_events:
            init_state = graph.propagator.create_initial_state(
                request.symbol,
                request.trade_date,
                user_context=user_context_payload,
                selected_analysts=request.selected_analysts,
                request_source=request_source,
            )
            args = graph.propagator.get_graph_args()
            
            # Pass job_id as thread_id for LangGraph checkpointer persistence
            if "config" not in args:
                args["config"] = {}
            args["config"]["configurable"] = {"thread_id": job_id}

            report_keys = (
                "market_report",
                "sentiment_report",
                "news_report",
                "fundamentals_report",
                "macro_report",
                "smart_money_report",
                "volume_price_report",
                "investment_plan",
                "trader_investment_plan",
                "final_trade_decision",
            )
            last_report: Dict[str, str] = {}
            seen: Dict[str, bool] = {}

            _tracker_token = current_tracker_var.set(tracker)
            try:
                async for chunk in graph.graph.astream(init_state, **args):
                    chunk = _normalize_stream_chunk(chunk, job_id=job_id, horizon=None)
                    final_state = chunk
                    # ── 并行感知的状态推进 ──────────────────
                    # 1. 每个 analyst 报告首次出现 → completed
                    for analyst_key in ANALYST_ORDER:
                        if analyst_key not in request.selected_analysts:
                            continue
                        rkey = ANALYST_REPORT_MAP[analyst_key]
                        aname = ANALYST_AGENT_NAMES[analyst_key]
                        if chunk.get(rkey) and not seen.get(rkey):
                            seen[rkey] = True
                            tracker._set_status(aname, "completed")

                    # 2. 分析师全部完成 → 研究团队开始
                    all_analysts_done = all(
                        seen.get(ANALYST_REPORT_MAP.get(a, "")) for a in tracker.selected_analysts
                    )
                    if all_analysts_done and not seen.get("_research_started"):
                        seen["_research_started"] = True
                        tracker._set_status(ANALYST_AGENT_NAMES["bull"], "in_progress")
                        tracker._set_status(ANALYST_AGENT_NAMES["bear"], "in_progress")
                        tracker._set_status(ANALYST_AGENT_NAMES["research_manager"], "in_progress")

                    debate = chunk.get("investment_debate_state") or {}
                    if debate.get("judge_decision") and not seen.get("judge_decision"):
                        seen["judge_decision"] = True
                        for r_key in ["bull", "bear", "research_manager"]:
                            tracker._set_status(ANALYST_AGENT_NAMES[r_key], "completed")
                        tracker._set_status(ANALYST_AGENT_NAMES["trader"], "in_progress")

                    if chunk.get("trader_investment_plan") and not seen.get("trader_investment_plan"):
                        seen["trader_investment_plan"] = True
                        tracker._set_status(ANALYST_AGENT_NAMES["trader"], "completed")
                        tracker._set_status(ANALYST_AGENT_NAMES["aggressive"], "in_progress")

                    risk = chunk.get("risk_debate_state") or {}
                    if risk.get("judge_decision") and not seen.get("risk_judge_decision"):
                        seen["risk_judge_decision"] = True
                        for r_key in ["aggressive", "neutral", "conservative", "portfolio_manager"]:
                            tracker._set_status(ANALYST_AGENT_NAMES[r_key], "completed")
                    # ────────────────────────────────────────────

                    # ── Partial DB Persistence & UI Streaming ──
                    db_updates = {}
                    for key in report_keys:
                        value = chunk.get(key)
                        if value and value != last_report.get(key):
                            last_report[key] = value
                            db_updates[key] = str(value)
                            # 立即推送报告分片，前端即可“即产即看”
                            tracker._emit_report_chunked(job_id, key, str(value))
                    
                    if db_updates:
                        def _partial_update(updates=db_updates):
                            with get_db_ctx() as _db:
                                report_service.update_report_partial(_db, job_id, **updates)
                        await asyncio.to_thread(_partial_update)
                    
                    # ── Message & Tool Call Handling ──
                    messages = chunk.get("messages", [])
                    if messages:
                        msg = messages[-1]
                        content = _extract_message_text(getattr(msg, "content", ""))
                        agent_name = getattr(msg, "name", None)

                        if content:
                            _log(f"[Agent Message] {agent_name}: {content[:200]}...")

                        for tool_call in getattr(msg, "tool_calls", []) or []:
                            tool_name = tool_call.get("name", "unknown") if isinstance(tool_call, dict) else getattr(tool_call, "name", "unknown")
                            tool_args = tool_call.get("args", {}) if isinstance(tool_call, dict) else getattr(tool_call, "args", {})
                            _log(f"[Tool Call] {agent_name}: {tool_name}")

                            agent_display = agent_name
                            if not agent_display:
                                tool_to_agent = {
                                    "get_stock_data": "数据获取",
                                    "get_indicators": "技术分析师",
                                    "get_fundamentals": "基本面分析师",
                                    "get_income_statement": "基本面分析师",
                                    "get_balance_sheet": "基本面分析师",
                                    "get_cash_flow": "基本面分析师",
                                    "get_news": "新闻分析师",
                                    "get_social_sentiment": "舆情分析师",
                                }
                                agent_display = tool_to_agent.get(tool_name, "系统")

                            tool_description = _generate_tool_description(tool_name, tool_args)
                            _emit_job_event(
                                job_id,
                                "agent.tool_call",
                                {
                                    "agent": agent_display,
                                    "tool": tool_name,
                                    "description": tool_description,
                                },
                            )
                
            except Exception as e:
                _log(f"Error during default streaming: {e}")
            finally:
                current_tracker_var.reset(_tracker_token)
        else:
            final_state, _ = await asyncio.to_thread(
                lambda: graph.propagate(
                    request.symbol,
                    request.trade_date,
                    user_context=user_context_payload,
                    selected_analysts=request.selected_analysts,
                    request_source=request_source,
                    thread_id=job_id,
                )
            )

        if not final_state:
            raise RuntimeError("graph returned empty final state")

        result = _build_result_payload(final_state)
        hz0 = request.horizons[0] if request.horizons else "short"
        report_quality_service.apply_result_quality_pass(result, config=config, default_trace_horizon=hz0)
        # See the note on the other process_signal call site: abstention is "NA",
        # never the literal "UNKNOWN".
        decision = coerce_persistable_decision(
            graph.process_signal(str(result.get("final_trade_decision") or ""))
        )
        result["decision"] = decision

        # 全量收口为 completed/skipped
        for agent, status in tracker.status.items():
            if status not in ("completed", "skipped"):
                tracker._set_status(agent, "completed")

        # LLM 结构化提取（非阻塞，失败不影响主流程）
        # 注意：_set_job(status="completed") 必须在此之后调用，否则 SSE 超时会提前关闭流
        structured = None
        try:
            structured = await asyncio.to_thread(
                report_service.extract_structured_data,
                final_trade_decision=result.get("final_trade_decision", ""),
                fundamentals_report=result.get("fundamentals_report", ""),
                config=config,
            )
        except Exception as e:
            _log(f"Structured extraction failed (non-fatal): {e}")

        # 一次性解析所有字段（方向、信心、目标价等）
        resolved = await asyncio.to_thread(
            report_service.resolve_report_fields,
            result_data=result,
            target_price_override=structured.target_price if structured else None,
            stop_loss_override=structured.stop_loss_price if structured else None,
        )

        # 注入结果字典以便通知和保存使用
        result.update({
            "direction": resolved["direction"],
            "confidence": resolved["confidence"],
            "target_price": resolved["target_price"],
            "stop_loss_price": resolved["stop_loss_price"],
        })
        model_info = _build_report_model_info(
            config,
            effective_profile_id,
            selected_model_profile_name,
            request.experiment_id,
            request.input_snapshot_hash,
        )
        result.update(model_info)
        result["model_info"] = dict(model_info)
        _attach_consensus_summary(result)
        _attach_freshness_to_job_result(result, request.symbol, request.trade_date)
        # 计划块里自带的止损/止盈锚点回填到研报字段，避免结构化提取漏掉就永久丢失
        _backfill_plan_anchors(result)

        # 自动保存/收口报告到数据库
        if save_report:
            def _save_report_final_sync():
                with get_db_ctx() as save_db:
                    report_service.create_report(
                        db=save_db,
                        symbol=request.symbol,
                        trade_date=request.trade_date,
                        decision=decision,
                        result_data=result,
                        user_id=user_id,
                        risk_items=([r.model_dump() for r in structured.risks] if structured else None),
                        key_metrics=([m.model_dump() for m in structured.key_metrics] if structured else None),
                        target_price_override=result["target_price"],
                        stop_loss_override=result["stop_loss_price"],
                        report_id=job_id,
                        analyst_traces=result.get("analyst_traces"),
                    )
                    _persist_trade_plan_from_result(
                        save_db,
                        user_id=user_id,
                        job_id=job_id,
                        request=request,
                        result=result,
                        horizon=hz0,
                    )
                    save_db.commit()

            try:
                await asyncio.to_thread(_save_report_final_sync)
            except Exception as e:
                _log(f"Failed to finalize report: {e}")
        # 所有后处理完成后再标记 completed，防止 SSE 超时提前关闭流
        _set_job(
            job_id,
            status="completed",
            result=result,
            decision=decision,
            finished_at=_utcnow_iso(),
        )
        _emit_job_event(
            job_id,
            "job.completed",
            {
                "job_id": job_id,
                "decision": decision,
                "direction": result["direction"],
                "result": result,
                "risk_items": [r.model_dump() for r in structured.risks] if structured else [],
                "key_metrics": [m.model_dump() for m in structured.key_metrics] if structured else [],
                "confidence": result["confidence"],
                "target_price": result["target_price"],
                "stop_loss_price": result["stop_loss_price"],
            },
        )
        _log(f"Job completed successfully: {job_id}")
        _log(f"[Timer] TOTAL Job execution (single_horizon) took {time.time() - job_start_t:.2f}s")
    except Exception as exc:
        err_msg = f"{type(exc).__name__}: {exc}"
        _set_job(
            job_id,
            status="failed",
            error=err_msg,
            traceback=traceback.format_exc(),
            finished_at=_utcnow_iso(),
        )
        
        # ── Persistent failure recording (short-lived session) ──
        try:
            def _record_failure():
                with get_db_ctx() as err_db:
                    report_service.mark_report_failed(err_db, job_id, f"{err_msg}\n\n{traceback.format_exc()}")
            await asyncio.to_thread(_record_failure)
        except Exception as db_exc:
            _log(f"Failed to record failure in DB: {db_exc}")

        _emit_job_event(
            job_id,
            "job.failed",
            {"job_id": job_id, "error": err_msg},
        )
    finally:
        _shared_data_collector.evict(request.symbol, request.trade_date)


def _normalize_symbol(raw: str, *, use_name_map: bool = True) -> str:
    s = raw.strip().upper()
    # Priority: 6-digit CN stock code
    m = re.search(r"(\d{6})(?:\.(SH|SZ|SS))?", s)
    if m:
        code = m.group(1)
        suffix = m.group(2)
        if suffix:
            if suffix == "SS":
                return f"{code}.SH"
            return f"{code}.{suffix}"
        market = "SH" if code.startswith(("5", "6", "9")) else "SZ"
        return f"{code}.{market}"
    # Fallback: 1-6 letter ticker
    m2 = re.search(r"([A-Z]{1,6}(?:\.[A-Z]{1,3})?)", s)
    if m2:
        return m2.group(1)
        
    # Final Fallback: Check Chinese Name Map (e.g. "三花智控" -> "002050.SZ")
    # Pass use_name_map=False from request paths: this lookup would otherwise
    # trigger a blocking AkShare load inside a handler.
    if use_name_map:
        stock_map = _get_stock_name_map_cached_only()
        if s in stock_map:
            return stock_map[s]
        
    return s


def _extract_chat_text(messages: List[ChatMessage]) -> str:
    if not messages:
        return ""
    last = messages[-1]
    return _extract_message_text(last.content)


def _extract_symbol_and_date(text: str) -> tuple[Optional[str], Optional[str]]:
    # Date extraction (flexible boundaries)
    date_match = re.search(r"\d{4}-\d{2}-\d{2}", text)
    date = date_match.group(0) if date_match else None

    # Priority 1: A-Share 6-digit code (even if stuck to Chinese characters)
    sym_match = re.search(r"(\d{6}(?:\.(?:SH|SZ|SS))?)", text, re.IGNORECASE)
    if sym_match:
        return _normalize_symbol(sym_match.group(1)), date

    # Priority 2: US Stocks or other Tickers (use boundaries for letters to avoid partial words)
    us_match = re.search(r"\b([A-Z]{1,6}(?:\.[A-Z]{1,3})?)\b", text.upper())
    if us_match:
        return us_match.group(1), date

    return None, date


def _fallback_extract_symbol(text: str) -> Optional[str]:
    """Best-effort stock symbol extraction without LLM dependency."""
    symbol, _ = _extract_symbol_and_date(text)
    if symbol:
        return symbol
    # Also try fuzzy company-name lookup on the full user text.
    # _search_cn_stock_by_name supports partial matching (name in query / query in name).
    by_name = _search_cn_stock_by_name(text.strip())
    if by_name:
        return by_name
    return None


def _sse_pack(event: str, data: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _parse_stock_csv(raw: str) -> List[Dict[str, Any]]:
    if not raw:
        return []
    lines = [ln for ln in raw.splitlines() if ln.strip() and not ln.startswith("#")]
    if not lines:
        return []

    try:
        df = pd.read_csv(StringIO("\n".join(lines)))
    except Exception:
        return []

    if "Date" not in df.columns:
        return []

    rename_map = {k: k.strip() for k in df.columns}
    df = df.rename(columns=rename_map)
    required = ["Date", "Open", "High", "Low", "Close"]
    for col in required:
        if col not in df.columns:
            return []

    for col in ["Open", "High", "Low", "Close", "Volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df.dropna(subset=["Date", "Open", "High", "Low", "Close"]).sort_values("Date")
    if df.empty:
        return []

    candles: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        candles.append(
            {
                "date": row["Date"].strftime("%Y-%m-%d"),
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
                "volume": float(row["Volume"]) if "Volume" in df.columns and pd.notna(row.get("Volume")) else None,
            }
        )
    return candles


CN_INDEX_SYMBOL_MAP = {
    "000001.SH": "sh000001",
    "399001.SZ": "sz399001",
    "399006.SZ": "sz399006",
    "000300.SH": "sh000300",
    "000688.SH": "sh000688",
    "000905.SH": "sh000905",
    "000852.SH": "sh000852",
    "899050.BJ": "bj899050",
}


def _is_cn_index_symbol(symbol: str) -> bool:
    return symbol.upper() in CN_INDEX_SYMBOL_MAP


def _normalize_kline_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    col_map = {
        "日期": "Date",
        "date": "Date",
        "Date": "Date",
        "开盘": "Open",
        "open": "Open",
        "Open": "Open",
        "最高": "High",
        "high": "High",
        "High": "High",
        "最低": "Low",
        "low": "Low",
        "Low": "Low",
        "收盘": "Close",
        "close": "Close",
        "Close": "Close",
        "成交量": "Volume",
        "volume": "Volume",
        "Volume": "Volume",
        "成交额": "Amount",
        "amount": "Amount",
        "Amount": "Amount",
        "涨跌幅": "ChangePercent",
        "涨跌额": "Change",
        "换手率": "TurnoverRate",
    }
    out = df.rename(columns=col_map).copy()
    required = ["Date", "Open", "High", "Low", "Close"]
    if any(col not in out.columns for col in required):
        return pd.DataFrame()

    out["Date"] = pd.to_datetime(out["Date"], errors="coerce")
    out = out.dropna(subset=["Date"]).sort_values("Date")
    for col in ["Open", "High", "Low", "Close", "Volume", "Amount", "ChangePercent", "Change", "TurnoverRate"]:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["Open", "High", "Low", "Close"])
    return out.reset_index(drop=True)


def _fetch_index_kline(symbol: str, start_date: str, end_date: str) -> List[Dict[str, Any]]:
    import akshare as ak  # type: ignore

    symbol_key = symbol.upper()
    vendor_symbol = CN_INDEX_SYMBOL_MAP.get(symbol_key)
    if not vendor_symbol:
        return []

    yyyymmdd_start = start_date.replace("-", "")
    yyyymmdd_end = end_date.replace("-", "")
    last_exc: Exception | None = None

    for fetcher in (
        lambda: ak.stock_zh_index_daily_em(
            symbol=vendor_symbol,
            start_date=yyyymmdd_start,
            end_date=yyyymmdd_end,
        ),
        lambda: ak.stock_zh_index_daily(symbol=vendor_symbol),
        lambda: ak.index_zh_a_hist(
            symbol=symbol_key.split(".")[0],
            period="daily",
            start_date=yyyymmdd_start,
            end_date=yyyymmdd_end,
        ),
    ):
        try:
            raw_df = fetcher()
            df = _normalize_kline_df(raw_df)
            if df.empty:
                continue
            df = df[(df["Date"] >= pd.to_datetime(start_date)) & (df["Date"] <= pd.to_datetime(end_date))]
            if df.empty:
                continue
            candles: List[Dict[str, Any]] = []
            prev_close: float | None = None
            for _, row in df.iterrows():
                close = float(row["Close"])
                change = float(row["Change"]) if "Change" in df.columns and pd.notna(row.get("Change")) else (close - prev_close if prev_close is not None else None)
                change_pct = (
                    float(row["ChangePercent"])
                    if "ChangePercent" in df.columns and pd.notna(row.get("ChangePercent"))
                    else ((change / prev_close) * 100 if prev_close not in (None, 0) and change is not None else None)
                )
                candles.append(
                    {
                        "date": row["Date"].strftime("%Y-%m-%d"),
                        "open": float(row["Open"]),
                        "high": float(row["High"]),
                        "low": float(row["Low"]),
                        "close": close,
                        "volume": float(row["Volume"]) if "Volume" in df.columns and pd.notna(row.get("Volume")) else None,
                        "amount": float(row["Amount"]) if "Amount" in df.columns and pd.notna(row.get("Amount")) else None,
                        "change": change,
                        "change_percent": change_pct,
                        "turnover_rate": float(row["TurnoverRate"]) if "TurnoverRate" in df.columns and pd.notna(row.get("TurnoverRate")) else None,
                    }
                )
                prev_close = close
            return candles
        except Exception as exc:
            last_exc = exc
            continue

    if last_exc:
        _log(f"[kline] index fetch failed for {symbol}: {type(last_exc).__name__}: {last_exc}")
    return []


async def _stream_job_events(job_id: str):
    q = _ensure_job_event_queue(job_id)
    yield _sse_pack("job.ready", {"job_id": job_id})
    while True:
        try:
            event = await asyncio.wait_for(q.get(), timeout=15)
            yield _sse_pack(event["event"], event["data"])
            if event["event"] in ("job.completed", "job.failed"):
                yield "event: done\ndata: [DONE]\n\n"
                break
        except asyncio.TimeoutError:
            # 仅在 status 已完成且无更多事件时关闭（兜底，正常路径不会触发）
            status = _jobs.get(job_id, {}).get("status")
            if status in ("completed", "failed"):
                yield "event: done\ndata: [DONE]\n\n"
                break
            yield _sse_pack("ping", {"timestamp": _utcnow_iso()})


@app.get("/healthz")
async def healthz() -> Dict[str, str]:
    return {"status": "ok"}


# Simple in-memory rate limiter for version stats: {ip: last_timestamp}
_vs_rate_limit: Dict[str, float] = {}
_VS_RATE_INTERVAL = 3600  # at most once per hour per IP


@app.post("/api/version-stats")
def version_stats(payload: Dict[str, Any] = Body(...), request: Request = None, db: Session = Depends(get_db)):
    """Collect anonymous version statistics from deployed instances."""
    remote_ip = _get_real_ip(request)

    # Rate limit by IP
    now = time.time()
    if remote_ip:
        last = _vs_rate_limit.get(remote_ip, 0)
        if now - last < _VS_RATE_INTERVAL:
            return {"status": "ok"}
        _vs_rate_limit[remote_ip] = now

    record = VersionStatsDB(
        version=str(payload.get("v", ""))[:50],
        nonce=str(payload.get("nonce", ""))[:64],
        remote_ip=remote_ip,
    )
    db.add(record)
    db.commit()
    return {"status": "ok"}


@app.get("/v1/market/kline", response_model=KlineResponse)
def get_kline(
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> KlineResponse:
    end = end_date or cn_today_str()
    if start_date:
        start = start_date
    else:
        start = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=120)).strftime("%Y-%m-%d")

    if _is_cn_index_symbol(symbol):
        candles = _fetch_index_kline(symbol, start, end)
    else:
        # Normalize symbol (convert "阳光电源" -> "300274.SZ")
        symbol = _normalize_symbol(symbol)
        config = _build_runtime_config({})
        set_config(config)
        raw = route_to_vendor("get_stock_data", symbol, start, end)
        candles = _parse_stock_csv(raw)
    if not candles:
        raise HTTPException(status_code=404, detail="no kline data")
    return KlineResponse(
        symbol=symbol,
        start_date=start,
        end_date=end,
        candles=candles,
    )


def _normalize_ths_code(code: str) -> str:
    """Convert THS/XQ code like SH601xxx → 601xxx.SH"""
    code = str(code).strip()
    if code.upper().startswith("SH"):
        return f"{code[2:]}.SH"
    if code.upper().startswith("SZ"):
        return f"{code[2:]}.SZ"
    if code.upper().startswith("BJ") or code.upper().startswith("NQ"):
        return f"{code[2:]}.BJ"
    # Bare 6-digit code — guess exchange
    if code.startswith(("6", "5")):
        return f"{code}.SH"
    if code.startswith(("0", "3", "2")):
        return f"{code}.SZ"
    return code


@app.get("/v1/market/hot-stocks")
def get_hot_stocks(source: str = "em", limit: int = 30) -> Dict:
    """Return hot A-share stocks from different sources.
    
    Args:
        source: Data source selection
            - 'em': 东方财富热榜 (EastMoney hot stocks)
            - 'xq': 雪球热门 (Xueqiu most-followed stocks)
            - 'ths': 连涨榜 (Consecutive rising stocks, not general hot list)
        limit: Maximum number of stocks to return
    
    Returns:
        Dict with stocks list, total count, source info, and fallback status
    """
    import akshare as ak

    # 定义数据源尝试顺序（如果主数据源失败，自动尝试备用源）
    source_configs = {
        "em": ("stock_hot_rank_em", None, "东方财富热榜"),
        "xq": ("stock_hot_follow_xq", "最热门", "雪球热门"),
        "ths": ("stock_rank_lxsz_ths", None, "连涨榜"),
    }

    if source not in source_configs:
        raise HTTPException(status_code=400, detail=f"Unknown source: {source}")

    # 尝试主数据源，失败则尝试其他源
    sources_to_try = [source] + [s for s in ["xq", "em", "ths"] if s != source]
    last_error = None

    for src in sources_to_try:
        try:
            func_name, param, desc = source_configs[src]
            func = getattr(ak, func_name)

            # 调用 akshare 函数
            if param:
                df = func(symbol=param).head(limit)
            else:
                df = func().head(limit)

            stocks = []

            if src == "em":
                for i, (_, row) in enumerate(df.iterrows()):
                    stocks.append({
                        "rank": i + 1,
                        "symbol": _normalize_ths_code(str(row.get("代码", ""))),
                        "name": str(row.get("股票名称", "")),
                        "price": float(row.get("最新价", 0) or 0),
                        "change": float(row.get("涨跌额", 0) or 0),
                        "change_pct": float(row.get("涨跌幅", 0) or 0),
                        "extra": "",
                    })

            elif src == "xq":
                for i, (_, row) in enumerate(df.iterrows()):
                    stocks.append({
                        "rank": i + 1,
                        "symbol": _normalize_ths_code(str(row.get("股票代码", ""))),
                        "name": str(row.get("股票简称", "")),
                        "price": float(row.get("最新价", 0) or 0),
                        "change": 0.0,
                        "change_pct": 0.0,
                        "extra": f"关注 {int(row.get('关注', 0)):,}",
                    })

            elif src == "ths":
                for i, (_, row) in enumerate(df.iterrows()):
                    days = int(row.get("连涨天数", 0) or 0)
                    change_pct = float(row.get("连续涨跌幅", 0) or 0)
                    stocks.append({
                        "rank": i + 1,
                        "symbol": _normalize_ths_code(str(row.get("股票代码", ""))),
                        "name": str(row.get("股票简称", "")),
                        "price": float(row.get("收盘价", 0) or 0),
                        "change": 0.0,
                        "change_pct": change_pct,
                        "extra": f"连涨{days}天",
                    })

            # 成功获取数据
            fallback_msg = f" (fallback from {source_configs[source][2]})" if src != source else ""
            _log(f"Hot stocks: successfully fetched from {desc}{fallback_msg}")
            return {
                "stocks": stocks,
                "total": len(stocks),
                "source": src,
                "requested_source": source,
                "fallback": src != source,
            }

        except Exception as e:
            last_error = e
            _log(f"Hot stocks: {desc} failed - {type(e).__name__}: {str(e)[:100]}")
            continue

    # 所有数据源都失败
    raise HTTPException(
        status_code=503,
        detail=f"All data sources failed. Last error: {type(last_error).__name__}: {str(last_error)[:200]}"
    )


def _build_mainline_runtime_config(
    db: Session,
    user_id: str,
    model_profile_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """主线任务运行时 LLM 配置：系统配置 + 用户设置页（user_llm_configs）+ 模型管理页（model_profiles）。

    优先级：DEFAULT_CONFIG < 系统配置(PATCH /v1/config) < 用户设置页 < 模型管理页(默认/指定 profile)。
    模型管理页的 api_key 是主要来源之一，缺失时返回 None 由任务内给出可读指引。
    """
    try:
        cfg = _build_runtime_config({}, user_id=user_id, db=db)
    except Exception:
        return None
    try:
        profile_id = model_profile_id
        if not profile_id:
            profiles = model_profile_service.list_model_profiles(db, user_id, include_inactive=False)
            default = next((p for p in profiles if p.get("is_default")), None)
            profile_id = str((default or (profiles[0] if profiles else None) or {}).get("id") or "")
        if profile_id:
            profile_overrides = model_profile_service.resolve_runtime_overrides(
                db, user_id=user_id, profile_id=profile_id
            )
            profile_overrides = {k: v for k, v in profile_overrides.items() if v not in (None, "", [])}
            if profile_overrides:
                cfg = _deep_merge(cfg, profile_overrides)
    except Exception:
        pass  # 模型配置不可用时静默回退到设置页/系统配置
    return cfg


def _create_mainline_deep_dive_task(user_id: str, symbol: str) -> str:
    """为主线可买入标的创建个股多智能体深度分析任务，返回 job_id。"""
    normalized = _normalize_symbol(symbol)
    if not normalized:
        raise ValueError(f"invalid symbol: {symbol}")
    request = AnalyzeRequest(
        symbol=normalized,
        trade_date=cn_today_str(),
        horizons=["short", "medium"],
        selected_analysts=["market", "social", "news", "fundamentals", "macro", "smart_money"],
    )
    job_id = uuid4().hex
    now = _utcnow_iso()
    _set_job(
        job_id, job_id=job_id, user_id=user_id, status="pending", created_at=now,
        symbol=normalized, trade_date=request.trade_date, error=None, result=None, decision=None,
    )
    _ensure_job_event_queue(job_id)
    _emit_job_event(
        job_id, "job.created",
        {"job_id": job_id, "source": "mainline_autodive", "symbol": normalized, "trade_date": request.trade_date},
    )
    _create_tracked_task(_run_job(job_id, request, True, True, user_id, "mainline_autodive"))
    return job_id


@app.post("/v1/mainline/analyze", response_model=MainlineAnalyzeResponse)
async def analyze_mainline(
    request: MainlineAnalyzeRequest,
    current_user: UserDB = Depends(_require_api_user),
) -> MainlineAnalyzeResponse:
    """触发市场主线分析：板块数据 → 主线识别 → 主线选股（后台任务 + SSE 事件流）。"""
    job_id = uuid4().hex
    now = _utcnow_iso()
    trade_date = request.trade_date or cn_today_str()
    _set_job(
        job_id,
        job_id=job_id,
        user_id=current_user.id,
        status="pending",
        created_at=now,
        started_at=None,
        finished_at=None,
        symbol="MAINLINE",
        trade_date=trade_date,
        error=None,
        result=None,
        decision=None,
        progress=8,
        phase="pending",
        progress_detail="任务已创建，等待执行",
    )
    _ensure_job_event_queue(job_id)
    _emit_job_event(
        job_id,
        "job.created",
        {
            "job_id": job_id,
            "type": "mainline",
            "trade_date": trade_date,
            "perspective": request.perspective,
        },
    )
    with get_db_ctx() as db:
        runtime_config = _build_mainline_runtime_config(
            db, current_user.id, request.model_profile_id
        )
        mainline_service.create_run(
            db,
            user_id=current_user.id,
            run_id=job_id,
            trade_date=trade_date,
            perspective=request.perspective,
            job_id=job_id,
        )
    _create_tracked_task(
        mainline_service.run_mainline_job_with_timeout(
            job_id,
            current_user.id,
            trade_date,
            request.perspective,
            user_focus=request.user_focus,
            yesterday_mainlines=request.yesterday_mainlines,
            set_job=_set_job,
            emit_event=_emit_job_event,
            market_collector=get_shared_market_collector(),
            config=runtime_config,
        ),
        label=f"mainline job {job_id}",
    )
    return MainlineAnalyzeResponse(job_id=job_id, status="pending", created_at=now)


@app.get("/v1/mainline/runs", response_model=MainlineRunListResponse)
def list_mainline_runs(
    limit: int = Query(20, ge=1, le=100),
    current_user: UserDB = Depends(_require_api_user),
) -> MainlineRunListResponse:
    with get_db_ctx() as db:
        runs = mainline_service.list_runs(db, current_user.id, limit=limit)
    return MainlineRunListResponse(runs=runs)


@app.get("/v1/mainline/latest")
def latest_mainline(
    perspective: str = Query("short"),
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    with get_db_ctx() as db:
        run = mainline_service.get_latest_run(db, current_user.id, perspective=perspective)
    if run is None:
        raise HTTPException(status_code=404, detail="暂无主线报告，请先触发 /v1/mainline/analyze")
    return run


def _get_mainline_run_sync(user_id: str, run_id: str) -> Optional[Dict[str, Any]]:
    with get_db_ctx() as db:
        return mainline_service.get_run(db, user_id, run_id)


@app.get("/v1/mainline/runs/{run_id}")
async def get_mainline_run(
    run_id: str,
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    loop = asyncio.get_running_loop()
    run = await loop.run_in_executor(_light_executor, _get_mainline_run_sync, current_user.id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="mainline run not found")
    return run


@app.get("/v1/mainline/boards/spot", response_model=MainlineBoardSpotResponse)
async def mainline_boards_spot(
    type: str = Query("industry", description="industry=行业板块 / concept=概念板块"),
    current_user: UserDB = Depends(_require_api_user),
) -> MainlineBoardSpotResponse:
    """板块涨幅榜（真实数据，不入 LLM），前端 Tab3 直接展示。"""
    trade_date = cn_today_str()
    collector = get_shared_market_collector()
    loop = asyncio.get_running_loop()
    pool = await loop.run_in_executor(
        _market_executor,
        lambda: collector.collect(trade_date, "short", include_breadth=False),
    )
    if type == "concept":
        boards = pool.get("concept_spot") or []
        source = (pool.get("sources") or {}).get("concept_spot")
    else:
        boards = pool.get("industry_spot") or []
        source = (pool.get("sources") or {}).get("industry_spot")
    return MainlineBoardSpotResponse(
        trade_date=trade_date,
        type=type,
        source=source,
        boards=boards,
        warnings=pool.get("warnings") or [],
    )


@app.post("/v1/mainline/candidates/{candidate_id}/watchlist")
def mainline_candidate_to_watchlist(
    candidate_id: str,
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    """把主线候选股一键加入自选。"""
    with get_db_ctx() as db:
        cand = mainline_service.get_candidate(db, current_user.id, candidate_id)
        if cand is None:
            raise HTTPException(status_code=404, detail="candidate not found")
        symbol = cand.get("symbol") or ""
        normalized = _normalize_symbol(symbol)
        if not normalized:
            raise HTTPException(status_code=400, detail=f"invalid symbol: {symbol}")
        item = watchlist_service.add_watchlist_item(db, current_user.id, normalized)
    return {"ok": True, "candidate": cand, "watchlist_item": item}


@app.post("/v1/mainline/candidates/{candidate_id}/analyze")
async def mainline_candidate_analyze(
    candidate_id: str,
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    """对主线候选股一键发起个股多智能体深度分析（复用 /v1/analyze 任务链路）。"""
    with get_db_ctx() as db:
        cand = mainline_service.get_candidate(db, current_user.id, candidate_id)
        if cand is None:
            raise HTTPException(status_code=404, detail="candidate not found")
        symbol = cand.get("symbol") or ""
        normalized = _normalize_symbol(symbol)
        if not normalized:
            raise HTTPException(status_code=400, detail=f"invalid symbol: {symbol}")
    request = AnalyzeRequest(
        symbol=normalized,
        trade_date=cn_today_str(),
        horizons=["short", "medium"],
        selected_analysts=["market", "social", "news", "fundamentals", "macro", "smart_money"],
    )
    job_id = uuid4().hex
    now = _utcnow_iso()
    _set_job(
        job_id,
        job_id=job_id,
        user_id=current_user.id,
        status="pending",
        created_at=now,
        started_at=None,
        finished_at=None,
        symbol=normalized,
        trade_date=request.trade_date,
        error=None,
        result=None,
        decision=None,
    )
    _ensure_job_event_queue(job_id)
    _emit_job_event(
        job_id,
        "job.created",
        {"job_id": job_id, "source": "mainline_candidate", "symbol": normalized, "trade_date": request.trade_date},
    )
    _create_tracked_task(_run_job(job_id, request, True, True, current_user.id, "mainline_candidate"))
    return {"ok": True, "job_id": job_id, "status": "pending", "candidate": cand}


@app.post("/v1/mainline/t1/refresh")
def mainline_t1_refresh(
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    """补算当前用户已完成主线报告的 T+1 兑现（主线代表板块前瞻收益 vs 基准）。"""
    with get_db_ctx() as db:
        return mainline_service.refresh_t1_outcomes(db, user_id=current_user.id)


@app.get("/v1/mainline/t1/overview")
def mainline_t1_overview(
    days: int = Query(30, ge=1, le=365),
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    """T+1 兑现统计看板（当前用户：总数/分布/平均超额/胜率）。"""
    with get_db_ctx() as db:
        return mainline_service.t1_overview(db, user_id=current_user.id, days=days)


@app.get("/v1/mainline/t1/outcomes")
def mainline_t1_outcomes(
    report_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    """T+1 兑现明细列表。"""
    with get_db_ctx() as db:
        items = mainline_service.list_t1_outcomes(db, user_id=current_user.id, report_id=report_id, limit=limit)
    return {"items": items, "total": len(items)}


@app.get("/v1/mainline/backtest/latest")
def mainline_backtest_latest(
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    """最近一次规则层回测结果（CLI `python -m tradingagents.dataflows.mainline_backtest --compare` 生成，只读）。"""
    results_dir = Path(os.getenv("TA_RESULTS_DIR", "results"))
    files = sorted(
        results_dir.glob("mainline_backtest_*_compare.json"), reverse=True
    ) if results_dir.exists() else []
    if not files:
        return {
            "found": False,
            "message": "尚无回测结果。运行: python -m tradingagents.dataflows.mainline_backtest --compare",
        }
    try:
        data = json.loads(files[0].read_text(encoding="utf-8"))
    except Exception:
        return {"found": False, "message": "回测结果文件解析失败"}
    return {
        "found": True,
        "file": files[0].name,
        "comparison": data.get("comparison"),
        "top_n": data.get("top_n"),
        "config": data.get("config"),
    }


@app.get("/v1/mainline/cycles")
def mainline_cycles(
    status: Optional[str] = Query(None, description="active|ended"),
    limit: int = Query(50, ge=1, le=200),
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    """主线周期档案列表（周期位置/进度/退潮预警/动作建议）。"""
    with get_db_ctx() as db:
        items = mainline_cycle_service.list_cycles(db, user_id=current_user.id, status=status, limit=limit)
    return {"items": items, "total": len(items)}


@app.get("/v1/mainline/cycles/{mainline_key}")
def mainline_cycle_track(
    mainline_key: str,
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    """单条主线周期档案与历史轨迹。"""
    with get_db_ctx() as db:
        cycle = mainline_cycle_service.get_cycle(db, user_id=current_user.id, mainline_key=mainline_key)
    if cycle is None:
        raise HTTPException(status_code=404, detail="主线档案不存在")
    return cycle


@app.get("/v1/mainline/rotation")
def mainline_rotation(
    days: int = Query(30, ge=1, le=120),
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    """近 N 日主线轮动回顾。"""
    with get_db_ctx() as db:
        timeline = mainline_cycle_service.rotation_timeline(db, user_id=current_user.id, days=days)
    return {"days": days, "timeline": timeline}


@app.get("/v1/mainline/decisions")
def mainline_decisions(
    days: int = Query(30, ge=1, le=120),
    limit: int = Query(100, ge=1, le=300),
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    """主线决策卡列表（周期/动作/仓位/验证条件/事后结果）。"""
    with get_db_ctx() as db:
        items = mainline_cycle_service.list_decisions(db, user_id=current_user.id, days=days, limit=limit)
    return {"items": items, "total": len(items)}


@app.get("/v1/mainline/capability")
def mainline_capability(
    days: int = Query(90, ge=1, le=365),
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    """能力曲线：历史决策验证统计（命中率/按周期分桶）。"""
    with get_db_ctx() as db:
        return mainline_cycle_service.capability_curve(db, user_id=current_user.id, days=days)


@app.post("/v1/mainline/cycles/verify")
def mainline_cycles_verify(
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    """补算未验证决策的事后结果（能力曲线数据源）。"""
    with get_db_ctx() as db:
        return mainline_cycle_service.verify_decisions(db, user_id=current_user.id)


# 主线验证闭环回填（P2-2）：对评估窗口已关闭的决策补算 outcome / forward_excess_ret，
# 必要时先补建该报告的 T+1 兑现记录（网络不可用时诚实跳过，仅返回统计）。
@app.post("/v1/mainline/cycles/backfill")
def mainline_cycles_backfill(
    as_of_date: Optional[str] = Query(None, description="评估基准日，默认今天"),
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    """回填主线决策卡的验证结果（能力曲线数据源，按当前用户隔离）。"""
    with get_db_ctx() as db:
        return mainline_cycle_service.backfill_decision_outcomes(
            db, user_id=current_user.id, as_of_date=as_of_date
        )


@app.post("/v1/mainline/autodive/run")
async def mainline_autodive_run(
    trade_date: Optional[str] = Query(None, description="默认今天/最近交易日"),
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    """手动触发自动深挖：周期分析 → 可买入筛选 → 自动创建个股深度分析任务。

    注意：必须为 async 端点——`_create_mainline_deep_dive_task` 内部用
    `asyncio.create_task` 创建后台任务，要求当前线程有运行中的事件循环；
    同步端点会在线程池线程执行，抛 `RuntimeError: no running event loop`。
    """
    td = trade_date or cn_today_str()

    # 周期分析是纯同步 DB 计算，放后台线程避免阻塞事件循环
    def _run_cycle_analysis() -> None:
        with get_db_ctx() as db:
            mainline_cycle_service.run_cycle_analysis(db, td, user_id=current_user.id)

    await asyncio.to_thread(_run_cycle_analysis)

    # run_autodive 的 create_task 回调（_create_mainline_deep_dive_task）必须
    # 在主事件循环线程执行；其 DB 操作量小（毫秒级），直接在主线程跑。
    with get_db_ctx() as db:
        res = mainline_autodive_service.run_autodive(
            db, td, user_id=current_user.id,
            create_task=lambda sym: _create_mainline_deep_dive_task(current_user.id, sym),
        )
        mainline_autodive_service.reconcile_deep_dive_statuses(db, user_id=current_user.id)
    return res


@app.get("/v1/mainline/autodive/runs")
def mainline_autodive_runs(
    trade_date: Optional[str] = Query(None),
    buyable_only: bool = Query(False),
    limit: int = Query(100, ge=1, le=300),
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    """自动深挖任务与可买入标的状态列表。"""
    with get_db_ctx() as db:
        items = mainline_autodive_service.list_trade_candidates(
            db, user_id=current_user.id, trade_date=trade_date, buyable_only=buyable_only, limit=limit
        )
    return {"items": items, "total": len(items)}


@app.get("/v1/mainline/logs/daily")
def mainline_daily_log(
    trade_date: Optional[str] = Query(None, description="默认今天/最近交易日"),
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    """主线作战日志：当日主线决策 + 可买入 + 深挖状态 + 退潮预警。"""
    td = trade_date or cn_today_str()
    with get_db_ctx() as db:
        return mainline_autodive_service.daily_operation_log(db, user_id=current_user.id, trade_date=td)


@app.post("/v1/analyze", response_model=AnalyzeResponse)
async def analyze(
    request: AnalyzeRequest,
    current_user: UserDB = Depends(_require_api_user),
) -> AnalyzeResponse:
    with get_db_ctx() as db:
        try:
            _apply_prompt_template_to_request(
                db,
                current_user.id,
                request,
                fallback_template_id=prompt_template_service.DEFAULT_MANUAL_TEMPLATE_ID,
                default_only_when_missing=False,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        merged_user_context = _compose_analysis_user_context(
            db,
            current_user.id,
            request.symbol,
            explicit_context=_extract_request_user_context(request),
        )
    _apply_user_context_to_request(request, merged_user_context)

    job_id = uuid4().hex
    now = _utcnow_iso()
    _set_job(
        job_id,
        job_id=job_id,
        user_id=current_user.id,
        status="pending",
        created_at=now,
        started_at=None,
        finished_at=None,
        symbol=request.symbol,
        trade_date=request.trade_date,
        model_profile_id=request.model_profile_id,
        experiment_id=request.experiment_id,
        input_snapshot_hash=request.input_snapshot_hash,
        error=None,
        result=None,
        decision=None,
    )
    _ensure_job_event_queue(job_id)
    _emit_job_event(
        job_id,
        "job.created",
        {"job_id": job_id, "symbol": request.symbol, "trade_date": request.trade_date},
    )
    if request.dry_run:
        await _run_job(job_id, request, True, True, current_user.id, "api")
        final_status = _jobs.get(job_id, {}).get("status", "completed")
        return AnalyzeResponse(job_id=job_id, status=final_status, created_at=now)
    _create_tracked_task(_run_job(job_id, request, True, True, current_user.id, "api"))
    return AnalyzeResponse(job_id=job_id, status="pending", created_at=now)


def _build_model_arena_snapshot_hash(payload: Dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@app.post("/v1/model-arena/runs", response_model=ModelArenaRunResponse)
async def create_model_arena_run(
    request: ModelArenaRunRequest,
    current_user: UserDB = Depends(_require_api_user),
) -> ModelArenaRunResponse:
    symbol = _normalize_symbol(request.symbol)
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")

    with get_db_ctx() as db:
        profiles = model_profile_service.list_model_profiles(db, current_user.id, include_inactive=False)
        profile_map = {str(item.get("id") or ""): item for item in profiles}
        selected_ids = [str(pid or "").strip() for pid in request.model_profile_ids if str(pid or "").strip()]
        if not selected_ids:
            selected_ids = [str(item.get("id") or "") for item in profiles if item.get("is_active")]
        selected_ids = [pid for pid in selected_ids if pid]
        selected_ids = list(dict.fromkeys(selected_ids))  # preserve order, dedupe
        if not selected_ids:
            raise HTTPException(status_code=400, detail="请先配置并启用至少一个模型配置")
        invalid_ids = [pid for pid in selected_ids if pid not in profile_map]
        if invalid_ids:
            raise HTTPException(status_code=400, detail=f"模型配置不可用：{', '.join(invalid_ids)}")

        merged_user_context = _compose_analysis_user_context(
            db,
            current_user.id,
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
    experiment_id = uuid4().hex
    now = _utcnow_iso()
    jobs: list[ModelArenaRunJob] = []

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
            try:
                _apply_prompt_template_to_request(
                    db,
                    current_user.id,
                    analyze_req,
                    fallback_template_id=prompt_template_service.DEFAULT_MANUAL_TEMPLATE_ID,
                    default_only_when_missing=False,
                )
            except ValueError as exc:
                raise HTTPException(400, str(exc))

        job_id = uuid4().hex
        _set_job(
            job_id,
            job_id=job_id,
            user_id=current_user.id,
            status="pending",
            created_at=now,
            started_at=None,
            finished_at=None,
            symbol=analyze_req.symbol,
            trade_date=analyze_req.trade_date,
            model_profile_id=profile_id,
            model_profile_name=profile.get("name"),
            experiment_id=experiment_id,
            input_snapshot_hash=snapshot_hash,
            error=None,
            result=None,
            decision=None,
        )
        _ensure_job_event_queue(job_id)
        _emit_job_event(
            job_id,
            "job.created",
            {
                "job_id": job_id,
                "symbol": analyze_req.symbol,
                "trade_date": analyze_req.trade_date,
                "model_profile_id": profile_id,
                "experiment_id": experiment_id,
            },
        )
        if analyze_req.dry_run:
            await _run_job(job_id, analyze_req, True, True, current_user.id, "model_arena")
            status_value = _jobs.get(job_id, {}).get("status", "completed")
        else:
            _create_tracked_task(_run_job(job_id, analyze_req, True, True, current_user.id, "model_arena"))
            status_value = "pending"
        jobs.append(
            ModelArenaRunJob(
                model_profile_id=profile_id,
                model_profile_name=profile.get("name"),
                job_id=job_id,
                symbol=analyze_req.symbol,
                trade_date=analyze_req.trade_date,
                status=status_value,  # type: ignore[arg-type]
                created_at=now,
            )
        )

    return ModelArenaRunResponse(
        experiment_id=experiment_id,
        input_snapshot_hash=snapshot_hash,
        symbol=symbol,
        trade_date=request.trade_date,
        jobs=jobs,
    )


@app.get("/v1/prompt-templates", response_model=PromptTemplateListResponse)
def list_prompt_templates(
    scope: Optional[str] = Query(default=prompt_template_service.SCOPE_DEEP_ANALYSIS),
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    templates = prompt_template_service.list_templates(db, current_user.id, scope=scope)
    return {
        "templates": templates,
        "defaults": prompt_template_service.get_template_defaults(),
    }


@app.post("/v1/prompt-templates", response_model=PromptTemplateResponse, status_code=201)
def create_prompt_template(
    body: PromptTemplateCreateRequest,
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    try:
        return prompt_template_service.create_custom_template(
            db,
            current_user.id,
            scope=body.scope,
            name=body.name,
            description=body.description,
            template_text=body.template_text,
            intent_json=body.intent_json,
            is_active=body.is_active,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.patch("/v1/prompt-templates/{template_id}", response_model=PromptTemplateResponse)
def update_prompt_template(
    template_id: str,
    body: PromptTemplateUpdateRequest,
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    try:
        return prompt_template_service.update_custom_template(
            db,
            current_user.id,
            template_id,
            name=body.name,
            description=body.description,
            template_text=body.template_text,
            intent_json=body.intent_json,
            is_active=body.is_active,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/v1/recommendations", response_model=RecommendationResponse)
async def recommend_symbols(
    request: RecommendationRequest,
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    """
    从用户候选池（跟踪持仓 + 自选 + 可选 seed）中输出推荐列表。
    可选自动启动前 N 只的分析任务。
    """
    rec = recommendation_service.recommend_for_user(
        db=db,
        user_id=current_user.id,
        top_k=request.top_k,
        candidate_limit=request.candidate_limit,
        source_mode=request.source_mode,
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
        momentum_weight=request.momentum_weight,
        activity_weight=request.activity_weight,
        near_high_weight=request.near_high_weight,
    )

    # Persist recommendation rows so T+1 quality curve can evaluate them (daily-product already persists).
    if rec.get("items") and str(request.market or "cn").lower() == "cn":
        run_id = uuid4().hex
        selected_syms: set[str] = set()
        if request.auto_start_analysis:
            run_n = request.auto_top_n or request.top_k
            run_n = max(1, min(int(run_n), 10))
            for it in (rec.get("items") or [])[:run_n]:
                sym = str(it.get("symbol") or "").strip().upper()
                if sym:
                    selected_syms.add(sym)
        recommendation_feedback_service.persist_scan_results(
            db,
            user_id=current_user.id,
            run_id=run_id,
            source_mode=request.source_mode,
            market=request.market or "cn",
            score_profile=request.score_profile,
            items=rec["items"],
            selected_symbols=selected_syms,
            feedback_horizon_days=None,
        )
        db.commit()

    analysis_jobs: list[RecommendationAnalyzeJob] = []
    if request.auto_start_analysis and rec.get("items"):
        run_n = request.auto_top_n or request.top_k
        run_n = max(1, min(int(run_n), 10))
        picks = rec["items"][:run_n]
        # Launch in small batches to keep burst load stable: at most 5 symbols per batch.
        batch_size = 5
        for batch_start in range(0, len(picks), batch_size):
            batch = picks[batch_start: batch_start + batch_size]
            for item in batch:
                symbol = item.get("symbol") or ""
                if not symbol:
                    continue
                with get_db_ctx() as ctx_db:
                    merged_user_context = _compose_analysis_user_context(
                        ctx_db,
                        current_user.id,
                        symbol,
                    )
                analyze_req = AnalyzeRequest(
                    symbol=symbol,
                    trade_date=cn_today_str(),
                    horizons=request.horizons or ["short"],
                    selected_analysts=request.selected_analysts,
                    query=f"推荐候选自动分析 {symbol}",
                )
                _apply_user_context_to_request(analyze_req, merged_user_context)
                job_id = uuid4().hex
                now = _utcnow_iso()
                _set_job(
                    job_id,
                    job_id=job_id,
                    user_id=current_user.id,
                    status="pending",
                    created_at=now,
                    started_at=None,
                    finished_at=None,
                    symbol=symbol,
                    trade_date=analyze_req.trade_date,
                    error=None,
                    result=None,
                    decision=None,
                )
                _ensure_job_event_queue(job_id)
                _emit_job_event(
                    job_id,
                    "job.created",
                    {"job_id": job_id, "symbol": symbol, "trade_date": analyze_req.trade_date},
                )
                _create_tracked_task(
                    _run_job(job_id, analyze_req, True, True, current_user.id, "recommendation"),
                    label=f"Recommendation analyze task ({symbol})",
                )
                analysis_jobs.append(
                    RecommendationAnalyzeJob(
                        symbol=symbol,
                        job_id=job_id,
                        status="pending",
                        created_at=now,
                    )
                )
            if batch_start + batch_size < len(picks):
                # Yield control between batches; keeps API responsive under heavier bursts.
                await asyncio.sleep(0)

    return RecommendationResponse(
        pool_size=int(rec.get("pool_size") or 0),
        scored_size=int(rec.get("scored_size") or 0),
        items=[RecommendationItemResponse(**item) for item in rec.get("items") or []],
        scoring_model=rec.get("scoring_model"),
        analysis_jobs=analysis_jobs,
    )


@app.post("/v1/recommendations/push/manual")
def manual_push_recommendations(
    phase: str = Query("close"),
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Manually trigger recommendation push (open/close) for current user."""
    try:
        return recommendation_service.manual_trigger_recommendation_push(
            db=db,
            user_id=current_user.id,
            phase=phase,
            force=True,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


def _apply_backtest_feedback_to_weights(
    *,
    user_id: str,
    momentum_weight: Optional[float],
    activity_weight: Optional[float],
    near_high_weight: Optional[float],
) -> tuple[Optional[float], Optional[float], Optional[float], Dict[str, Any]]:
    """Use the latest completed backtest stats to slightly bias recommendation weights."""
    jobs = _bt.list_jobs()
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
    learning_info: Dict[str, Any] = {"applied": False}
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


_DAILY_PRODUCT_STRATEGY_SKILLS: List[Dict[str, str]] = [
    {"id": "market_strategy", "name": "市场策略框架", "reference": "market-strategy.md"},
    {"id": "risk_scoring", "name": "风险评分标准", "reference": "risk-scoring-criteria.md"},
    {"id": "analysis_framework", "name": "分析框架", "reference": "analysis-framework.md"},
    {"id": "backtesting_guidelines", "name": "回测指引", "reference": "backtesting-guidelines.md"},
    {"id": "chart_guide", "name": "图表解读指引", "reference": "chart-guide.md"},
]


def _resolve_daily_product_strategy_skills(
    *,
    strategy_mode: str,
    requested_skills: List[str],
    market: str,
    feedback_info: Dict[str, Any],
) -> List[str]:
    allow = {item["id"] for item in _DAILY_PRODUCT_STRATEGY_SKILLS}
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


def _build_daily_product_query(symbol: str, strategy_skills: List[str]) -> str:
    if not strategy_skills:
        return f"daily_product 批量分析 {symbol}"
    return (
        f"daily_product 策略编排分析 {symbol}；"
        f"请融合策略技能：{','.join(strategy_skills)}，"
        "给出可执行交易计划并说明关键风险。"
    )


async def _finalize_daily_product_run_and_push(run_id: str) -> None:
    """Poll run jobs until done, then push summary to WeCom/WPS if configured."""
    while True:
        run = _get_daily_product_run(run_id)
        if not run:
            return
        jobs = run.get("jobs") or []
        if not jobs:
            _set_daily_product_run(run_id, status="completed", finished_at=_utcnow_iso())
            return
        statuses = []
        completed = failed = running = pending = 0
        for j in jobs:
            st = str((_get_job(j.get("job_id", "")).get("status") or j.get("status") or "pending"))
            statuses.append(st)
            if st == "completed":
                completed += 1
            elif st == "failed":
                failed += 1
            elif st == "running":
                running += 1
            else:
                pending += 1
        _set_daily_product_run(
            run_id,
            status=("running" if running or pending else "completed"),
            summary={
                **(run.get("summary") or {}),
                "completed_jobs": completed,
                "failed_jobs": failed,
                "running_jobs": running,
                "pending_jobs": pending,
            },
        )
        if all(s in ("completed", "failed") for s in statuses):
            _set_daily_product_run(run_id, status="completed", finished_at=_utcnow_iso())
            break
        await asyncio.sleep(5)

    run = _get_daily_product_run(run_id)
    if not run:
        return
    uid = str(run.get("user_id") or "")
    if not uid:
        return
    summary = run.get("summary") or {}
    lines = [
        "【每日批跑汇总】",
        f"模式：{run.get('mode')}",
        f"总数：{summary.get('total_targets', 0)}",
        f"完成：{summary.get('completed_jobs', 0)}",
        f"失败：{summary.get('failed_jobs', 0)}",
    ]
    recommendation = run.get("recommendation") or {}
    rec_items = recommendation.get("items") or []
    if rec_items:
        lines.append("推荐候选：")
        for item in rec_items[:5]:
            symbol = str(item.get("symbol") or "")
            name = str(item.get("name") or symbol)
            score = float(item.get("score") or 0.0)
            lines.append(f"- {name}（{symbol}）评分 {score:.1f}")
    jobs = run.get("jobs") or []
    for j in jobs[:8]:
        symbol = str(j.get("symbol") or "")
        name = str(j.get("name") or symbol)
        st = str((_get_job(j.get("job_id", "")).get("status") or j.get("status") or "pending"))
        lines.append(f"- {name}（{symbol}）：{st}")
    text = "\n".join(lines)[:1800]
    md = "## 每日批跑汇总\n\n" + "\n".join(f"- {ln}" if ln.startswith("- ") else ln for ln in lines)[0:4800]

    with get_db_ctx() as db:
        user = auth_service.get_user_by_id(db, uid)
        cfg = auth_service.get_user_llm_config(db, uid)
        wecom_hook = auth_service.decrypt_secret(getattr(cfg, "wecom_webhook_encrypted", None))
        wps_hook = auth_service.decrypt_secret(getattr(cfg, "wps_webhook_encrypted", None))
        if user and getattr(user, "wecom_report_enabled", True) and wecom_hook:
            try:
                send_message(text, wecom_hook)
            except Exception as exc:
                _log(f"[Daily Product] wecom summary push failed: {exc}")
        if user and getattr(user, "wps_report_enabled", True) and wps_hook:
            try:
                send_markdown_message(md, wps_hook)
            except Exception as exc:
                _log(f"[Daily Product] wps summary push failed: {exc}")

    # 汇总推送后，按与定时分析相同渠道推送各标的已完成的报告（邮件 / 企微 / WPS）
    for j in jobs:
        job_id = str(j.get("job_id") or "").strip()
        symbol = str(j.get("symbol") or "").strip()
        if not job_id or not symbol:
            continue
        st = str((_get_job(job_id).get("status") or j.get("status") or "pending"))
        if st != "completed":
            continue
        try:
            await _send_scheduled_report_notifications(
                uid,
                job_id,
                symbol,
                background=False,
                log_prefix="Daily Product",
            )
        except Exception as exc:
            _log(f"[Daily Product] report notification failed for {symbol}: {exc}")
        # 略作间隔，降低企微/协作机器人连续推送限流风险
        await asyncio.sleep(0.5)


def _start_daily_product_run(
    *,
    request: DailyProductRunRequest,
    user_id: str,
    db: Session,
) -> DailyProductRunResponse:
    run_id = uuid4().hex
    run_created_at = _utcnow_iso()
    code_to_name = _get_reverse_stock_map_cached_only()
    recommendation_payload: RecommendationResponse | None = None
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
        ) = _apply_backtest_feedback_to_weights(
            user_id=user_id,
            momentum_weight=request.momentum_weight,
            activity_weight=request.activity_weight,
            near_high_weight=request.near_high_weight,
        )
    effective_strategy_skills = _resolve_daily_product_strategy_skills(
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
        _set_daily_product_run(
            run_id,
            user_id=user_id,
            mode=request.mode,
            status="completed",
            created_at=run_created_at,
            finished_at=_utcnow_iso(),
            jobs=[],
            recommendation=recommendation_payload.model_dump() if recommendation_payload else None,
            summary=empty_response.summary,
        )
        return empty_response

    queued_jobs: list[DailyProductRunJob] = []
    for symbol in target_symbols:
        with get_db_ctx() as ctx_db:
            merged_user_context = _compose_analysis_user_context(
                ctx_db,
                user_id,
                symbol,
            )
        analyze_req = AnalyzeRequest(
            symbol=symbol,
            trade_date=cn_today_str(),
            horizons=request.horizons or ["short"],
            selected_analysts=request.selected_analysts,
            query=_build_daily_product_query(symbol, effective_strategy_skills),
        )
        _apply_user_context_to_request(analyze_req, merged_user_context)
        job_id = uuid4().hex
        now = _utcnow_iso()
        _set_job(
            job_id,
            job_id=job_id,
            user_id=user_id,
            status="pending",
            created_at=now,
            started_at=None,
            finished_at=None,
            symbol=symbol,
            trade_date=analyze_req.trade_date,
            error=None,
            result=None,
            decision=None,
            request_source="daily_product_batch",
        )
        _ensure_job_event_queue(job_id)
        _emit_job_event(
            job_id,
            "job.created",
            {"job_id": job_id, "symbol": symbol, "trade_date": analyze_req.trade_date},
        )
        _create_tracked_task(
            _run_job(job_id, analyze_req, True, True, user_id, "daily_product_batch"),
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
            _log(f"[Daily Product] ensure_scheduled failed for user={user_id}: {exc}")

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
    _set_daily_product_run(run_id, **{k: v for k, v in run_row.items() if k != "run_id"})
    _create_tracked_task(
        _finalize_daily_product_run_and_push(run_id),
        label=f"Daily product run monitor ({run_id[:8]})",
    )

    return DailyProductRunResponse(
        run_id=run_id,
        status="pending",
        mode=request.mode,
        summary=run_row["summary"],
        recommendation=recommendation_payload,
        jobs=queued_jobs,
    )


@app.post("/v1/daily-product/run", response_model=DailyProductRunResponse)
async def run_daily_product_batch(
    request: DailyProductRunRequest,
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    """
    产品增强批量分析入口（保留当前多Agent能力）：
    - recommended: 先智能推荐再批量分析
    - watchlist/tracking/all: 按用户股票池直接批量分析
    """
    return _start_daily_product_run(
        request=request,
        user_id=current_user.id,
        db=db,
    )


@app.get("/v1/daily-product/runs")
def list_daily_product_runs(
    limit: int = Query(20, ge=1, le=100),
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    rows = _list_daily_product_runs(current_user.id, limit=limit)
    return {"runs": rows, "total": len(rows)}


@app.get("/v1/daily-product/runs/{run_id}", response_model=DailyProductRunResponse)
def get_daily_product_run(
    run_id: str,
    current_user: UserDB = Depends(_require_api_user),
) -> DailyProductRunResponse:
    run = _get_daily_product_run(run_id)
    if not run or str(run.get("user_id") or "") != current_user.id:
        raise HTTPException(status_code=404, detail="daily product run not found")
    recommendation = run.get("recommendation")
    recommendation_payload = RecommendationResponse(**recommendation) if isinstance(recommendation, dict) else None
    jobs = [DailyProductRunJob(**item) for item in (run.get("jobs") or [])]
    return DailyProductRunResponse(
        run_id=run_id,
        status=str(run.get("status") or "pending"),
        mode=str(run.get("mode") or "recommended"),
        summary=run.get("summary") or {},
        recommendation=recommendation_payload,
        jobs=jobs,
    )


@app.get("/v1/daily-product/runs/{run_id}/scan-results")
def get_daily_product_run_scan_results(
    run_id: str,
    limit: int = Query(50, ge=1, le=200),
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    run = _get_daily_product_run(run_id)
    if not run or str(run.get("user_id") or "") != current_user.id:
        raise HTTPException(status_code=404, detail="daily product run not found")
    with get_db_ctx() as db:
        items = recommendation_feedback_service.list_scan_results(
            db,
            user_id=current_user.id,
            run_id=run_id,
            limit=limit,
        )
    return {"items": items, "total": len(items)}


@app.get("/v1/recommendations/history")
def list_recommendation_history(
    limit: int = Query(50, ge=1, le=200),
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    with get_db_ctx() as db:
        items = recommendation_feedback_service.list_scan_results(
            db,
            user_id=current_user.id,
            limit=limit,
        )
    return {"items": items, "total": len(items)}


@app.get("/v1/recommendations/strategy-stats")
def get_recommendation_strategy_stats(
    limit: int = Query(50, ge=1, le=200),
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    with get_db_ctx() as db:
        stats = recommendation_feedback_service.list_strategy_feedback_stats(
            db,
            user_id=current_user.id,
            limit=limit,
        )
        base = {"momentum": 0.30, "activity": 0.25, "near_high": 0.20, "sector": 0.15, "volume_ratio": 0.10}
        learned_weights, learning_info = recommendation_feedback_service.build_learning_weight_adjustment(
            db,
            user_id=current_user.id,
            base_weights=base,
        )
    return {
        "stats": stats,
        "learned_weights": learned_weights,
        "learning_info": learning_info,
        "total": len(stats),
    }


@app.get("/v1/recommendations/profiles")
def list_recommendation_profiles() -> Dict[str, Any]:
    """Return available scoring profiles so the frontend can offer a quick picker."""
    from api.services.market_scanner_service import list_profiles
    return {"profiles": list_profiles()}


@app.post("/v1/recommendations/strategy-stats/refresh")
def refresh_recommendation_strategy_stats(
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    with get_db_ctx() as db:
        try:
            data = recommendation_feedback_service.refresh_feedback_pipeline(
                db,
                user_id=current_user.id,
                eval_limit=300,
                retries=3,
            )
            db.commit()
        except OperationalError as exc:
            db.rollback()
            if "database is locked" in str(exc).lower():
                raise HTTPException(status_code=503, detail="数据库忙，请稍后重试")
            raise
    return data


@app.post("/v1/recommendations/eval-runs/refresh")
def refresh_recommendation_eval_run(
    baseline_profile: str = Query("ashare_balanced"),
    variant_profile: str = Query("ashare_aggressive"),
    lookback_days: int = Query(60, ge=7, le=365),
    top_k: int = Query(5, ge=1, le=20),
    benchmark_symbol: str = Query("000300.SH"),
    source_mode: Literal["user_pool", "market_scan"] = Query("market_scan"),
    market: Literal["cn", "us"] = Query("cn"),
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    """Build one A/B evaluation snapshot from historical evaluated scan rows."""
    with get_db_ctx() as db:
        try:
            data = recommendation_eval_service.refresh_eval_run(
                db,
                user_id=current_user.id,
                baseline_profile=baseline_profile,
                variant_profile=variant_profile,
                lookback_days=lookback_days,
                top_k=top_k,
                benchmark_symbol=benchmark_symbol,
                source_mode=source_mode,
                market=market,
            )
            db.commit()
        except OperationalError as exc:
            db.rollback()
            if "database is locked" in str(exc).lower():
                raise HTTPException(status_code=503, detail="数据库忙，请稍后重试")
            raise
    return data


@app.get("/v1/recommendations/eval-runs/latest")
def get_latest_recommendation_eval_run(
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    with get_db_ctx() as db:
        row = recommendation_eval_service.get_latest_eval_run(db, user_id=current_user.id)
    return {"run": row}


@app.post("/v1/insights/t1/refresh")
def refresh_insights_t1(
    window_start: Optional[str] = Query(None, description="ISO datetime; default previous trading day 15:00 CST"),
    window_end: Optional[str] = Query(None, description="ISO datetime; default current trading day 15:00 CST"),
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    """Refresh T+1 with default window [prev trading day 15:00, today 15:00)."""
    from sqlalchemy.exc import IntegrityError

    ws = _parse_t1_window_datetime(window_start)
    we = _parse_t1_window_datetime(window_end)
    # Do NOT fall back to _default_t1_refresh_window when both are None.
    # The default window [prev_day 15:00, today 15:00) covers only reports whose
    # signal date is "today" → T+1 is "tomorrow" → nothing can be evaluated yet.
    # The auto-task correctly passes no window and processes ALL pending records;
    # the manual refresh should behave identically.
    if ws is not None and we is not None and ws >= we:
        raise HTTPException(status_code=400, detail="window_start must be earlier than window_end")

    with get_db_ctx() as db:
        try:
            result = insights_t1_service.refresh_all_t1(
                db,
                user_id=current_user.id,
                window_start=ws,
                window_end=we,
            )
            db.commit()
        except IntegrityError:
            db.rollback()
            result = insights_t1_service.refresh_all_t1(
                db,
                user_id=current_user.id,
                window_start=ws,
                window_end=we,
            )
            db.commit()
    return result


@app.get("/v1/insights/t1/recommendations-trend")
def get_insights_t1_recommendations_trend(
    days: int = Query(90, ge=7, le=400),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    with get_db_ctx() as db:
        series = insights_t1_service.recommendation_t1_trend(
            db, user_id=current_user.id, days=days, start_date=start_date, end_date=end_date
        )
    return {"series": series, "metric": "t1_close_to_close", "description": "按信号日聚合：扫描推荐在 T0 收盘→下一交易日收盘的收益"}


@app.get("/v1/insights/t1/reports-accuracy-trend")
def get_insights_t1_reports_accuracy_trend(
    days: int = Query(90, ge=7, le=400),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    with get_db_ctx() as db:
        series = insights_t1_service.report_accuracy_t1_trend(
            db, user_id=current_user.id, days=days, start_date=start_date, end_date=end_date, scope="portfolio"
        )
        series_all = insights_t1_service.report_accuracy_t1_trend(
            db, user_id=current_user.id, days=days, start_date=start_date, end_date=end_date, scope="all"
        )
        summary = insights_t1_service.report_accuracy_t1_summary(
            db, user_id=current_user.id, days=days, start_date=start_date, end_date=end_date, scope="portfolio"
        )
        summary_all = insights_t1_service.report_accuracy_t1_summary(
            db, user_id=current_user.id, days=days, start_date=start_date, end_date=end_date, scope="all"
        )
    return {
        "series": series,
        "series_all": series_all,
        "summary": summary,
        "summary_all": summary_all,
        "metric": "directional_accuracy_t1",
        "description": "当前持仓标的已完成报告：方向（多/空）与 T+1 涨跌是否一致；中性/无方向不计入命中率，但计入 abstain_count（弃权率）",
        "description_all": "全部已完成深度分析报告（不限持仓）：方向（多/空）与 T+1 涨跌是否一致；中性/无方向不计入命中率，但计入 abstain_count（弃权率）",
        "denominator_note": (
            "命中率按**唯一价格窗口**计算：同一标的同一信号日的多份研报共享同一次前瞻收益，"
            "只算一次（effective_n）。row_count 是研报行数，两者差距反映重复度。"
            "ci_low/ci_high 为 Wilson 95% 区间；summary.not_significant=true 表示该区间跨过 50%，"
            "即现有样本无法把该命中率与掷硬币区分开；summary.underpowered=true 表示有效样本量"
            "远低于 required_n（要在 80% 功效下区分于 50% 所需）。"
            "summary.clustered 是按日等权 + 按日聚类的第二种估计量：同一交易日内的判断是"
            "横截面相关的，池化口径会被少数高产交易日抬起来，聚类口径更保守。"
            "summary.excess 是**去掉市场 beta 后的相对口径**：只有看多且跑赢同日同侪、"
            "或看空且跑输同侪才算对（基准为同日其它标的 T+1 收益均值，逐行留一）。"
            "绝对口径会被大盘涨跌抬高——真实库上绝对 52.34% 在去 beta 后只剩 50.5%，"
            "所以**与基线比较必须用相对口径**。summary.excess_spread 多空价差单位为"
            "百分点/日，covers_cost=false 表示这点价差不足以覆盖一个来回约 0.25% 的成本。"
            "horizon 恒为 \"t1\"：本指标只评**次日方向**（价格窗口固定为一个交易日收盘→收盘）。"
            "summary.plan_horizon 说明被评报告所附交易计划自己的持有期：目标价/止损/时间止损"
            "属于更长期限，其达成情况**不在**本指标内。multi_day_plan_windows 是承载了多日"
            "计划的窗口数，越大表示期限错配越严重；windows_with_unrecorded_plan_horizon 是"
            "「附有计划但该窗口尚未按新列重新评估」的数量（刻意不回填，以免把猜的持有期"
            "当成记录值）。"
        ),
    }


@app.get("/v1/insights/t1/recommendations-detail")
def get_insights_t1_recommendations_detail(
    date: str = Query(...),
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    with get_db_ctx() as db:
        items = insights_t1_service.recommendation_t1_detail(db, user_id=current_user.id, date=date)
    return {"date": date, "items": items}


@app.get("/v1/insights/t1/reports-detail")
def get_insights_t1_reports_detail(
    date: str = Query(...),
    scope: str = Query(
        "portfolio",
        description="portfolio=仅持仓且已评估；all=该信号日全部 T+1 结果（含待收盘/行情不足，不限持仓）",
    ),
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    if scope not in ("portfolio", "all"):
        raise HTTPException(status_code=400, detail="scope must be portfolio or all")
    with get_db_ctx() as db:
        items = insights_t1_service.report_accuracy_t1_detail(
            db, user_id=current_user.id, date=date, scope=scope
        )
    return {"date": date, "items": items, "scope": scope}


@app.get("/v1/insights/t1/multi-model-consensus-trend")
def get_insights_t1_multi_model_consensus_trend(
    days: int = Query(90, ge=7, le=400),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    min_models: int = Query(2, ge=2, le=12),
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    with get_db_ctx() as db:
        # One load feeds both scopes: calling the builder twice doubled every
        # query and every name resolution inside a single request.
        trends = model_arena_service.build_multi_model_consensus_t1_trends(
            db,
            user_id=current_user.id,
            days=days,
            start_date=start_date,
            end_date=end_date,
            min_models=min_models,
        )
    return {
        "series": trends["series"],
        "series_all": trends["series_all"],
        "metric": "multi_model_consensus_accuracy_t1",
        "min_models": min_models,
        "description": "同一信号日、同一股票：≥2 个不同模型深度报告方向一致（仅持仓）",
        "description_all": "同一信号日、同一股票：≥2 个不同模型深度报告方向一致（全部深度报告）",
    }


@app.get("/v1/insights/t1/multi-model-consensus-detail")
def get_insights_t1_multi_model_consensus_detail(
    date: str = Query(...),
    scope: str = Query("all", description="portfolio 或 all"),
    direction: Optional[str] = Query(None, description="bullish 或 bearish；省略则返回两种共识"),
    min_models: int = Query(2, ge=2, le=12),
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    if scope not in ("portfolio", "all"):
        raise HTTPException(status_code=400, detail="scope must be portfolio or all")
    if direction is not None and direction not in ("bullish", "bearish"):
        raise HTTPException(status_code=400, detail="direction must be bullish or bearish")
    with get_db_ctx() as db:
        payload = model_arena_service.build_multi_model_consensus_t1_detail(
            db,
            user_id=current_user.id,
            date=date,
            scope=scope,
            direction=direction,
            min_models=min_models,
        )
    return payload


@app.get("/v1/model-arena/leaderboard")
def get_model_arena_leaderboard(
    days: int = Query(90, ge=7, le=400),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    scope: str = Query("portfolio", description="portfolio 或 all"),
    min_samples: int = Query(1, ge=1, le=5000),
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    if scope not in ("portfolio", "all"):
        raise HTTPException(status_code=400, detail="scope must be portfolio or all")
    with get_db_ctx() as db:
        payload = model_arena_service.build_model_leaderboard(
            db,
            user_id=current_user.id,
            days=days,
            start_date=start_date,
            end_date=end_date,
            scope=scope,
            min_samples=min_samples,
        )
    return payload


@app.get("/v1/model-arena/symbol-compare")
def get_model_arena_symbol_compare(
    symbol: str = Query(..., description="股票代码，如 600519.SH"),
    days: int = Query(60, ge=7, le=400),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    scope: str = Query("all", description="portfolio 或 all"),
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    if scope not in ("portfolio", "all"):
        raise HTTPException(status_code=400, detail="scope must be portfolio or all")
    with get_db_ctx() as db:
        payload = model_arena_service.build_symbol_compare(
            db,
            user_id=current_user.id,
            symbol=symbol,
            days=days,
            start_date=start_date,
            end_date=end_date,
            scope=scope,
        )
    return payload


@app.get("/v1/model-arena/model-detail")
def get_model_arena_model_detail(
    model_profile_id: Optional[str] = Query(None),
    model_key: Optional[str] = Query(None),
    days: int = Query(90, ge=7, le=400),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    scope: str = Query("portfolio", description="portfolio 或 all"),
    limit: int = Query(300, ge=1, le=1000),
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    if scope not in ("portfolio", "all"):
        raise HTTPException(status_code=400, detail="scope must be portfolio or all")
    if not str(model_profile_id or "").strip() and not str(model_key or "").strip():
        raise HTTPException(status_code=400, detail="model_profile_id or model_key is required")
    with get_db_ctx() as db:
        payload = model_arena_service.build_model_t1_detail(
            db,
            user_id=current_user.id,
            model_profile_id=model_profile_id,
            model_key=model_key,
            days=days,
            start_date=start_date,
            end_date=end_date,
            scope=scope,
            limit=limit,
        )
    return payload


@app.get("/v1/model-arena/model-trend")
def get_model_arena_model_trend(
    days: int = Query(90, ge=7, le=400),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    scope: str = Query("portfolio", description="portfolio 或 all"),
    top_n: int = Query(5, ge=1, le=12),
    min_samples: int = Query(10, ge=1, le=5000),
    model_keys: Optional[str] = Query(None, description="逗号分隔的 model_key，指定后仅返回这些模型曲线"),
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    if scope not in ("portfolio", "all"):
        raise HTTPException(status_code=400, detail="scope must be portfolio or all")
    keys = [k.strip() for k in str(model_keys or "").split(",") if k.strip()] or None
    with get_db_ctx() as db:
        payload = model_arena_service.build_model_accuracy_trend(
            db,
            user_id=current_user.id,
            days=days,
            start_date=start_date,
            end_date=end_date,
            scope=scope,
            top_n=top_n,
            min_samples=min_samples,
            model_keys=keys,
        )
    return payload


@app.get("/v1/model-arena/drift-alerts")
def get_model_arena_drift_alerts(
    scope: str = Query("portfolio", description="portfolio 或 all"),
    lookback_days: int = Query(120, ge=30, le=400),
    recent_days: int = Query(7, ge=3, le=30),
    baseline_days: int = Query(30, ge=7, le=120),
    min_recent_samples: int = Query(8, ge=1, le=200),
    alert_drop_pct: float = Query(8.0, ge=0.1, le=50.0),
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    if scope not in ("portfolio", "all"):
        raise HTTPException(status_code=400, detail="scope must be portfolio or all")
    with get_db_ctx() as db:
        payload = model_arena_service.detect_model_drift(
            db,
            user_id=current_user.id,
            scope=scope,
            lookback_days=lookback_days,
            recent_days=recent_days,
            baseline_days=baseline_days,
            min_recent_samples=min_recent_samples,
            alert_drop_pct=alert_drop_pct,
        )
    return payload


@app.post("/v1/model-arena/promote-default")
def promote_model_arena_default(
    request: ModelArenaPromoteRequest,
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    if request.scope not in ("portfolio", "all"):
        raise HTTPException(status_code=400, detail="scope must be portfolio or all")
    with get_db_ctx() as db:
        profile = model_profile_service.get_model_profile(db, current_user.id, request.profile_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="模型配置不存在")
        if not profile.is_active:
            raise HTTPException(status_code=400, detail="目标模型配置已停用")

        gate = model_arena_service.evaluate_promotion_gate(
            db,
            user_id=current_user.id,
            target_profile_id=request.profile_id,
            lookback_days=request.lookback_days,
            scope=request.scope,
            min_samples=request.min_samples,
            min_accuracy_improvement_pct=request.min_accuracy_improvement_pct,
            max_return_drop_pct=request.max_return_drop_pct,
        )
        if not gate.get("passed") and not request.force:
            detail = "；".join(gate.get("reasons") or ["未通过晋升门槛"])
            raise HTTPException(status_code=400, detail=f"晋升门禁未通过：{detail}")

        updated = model_profile_service.update_model_profile(
            db,
            user_id=current_user.id,
            profile_id=request.profile_id,
            is_default=True,
            is_active=True,
        )
    return {
        "promoted": True,
        "forced": bool(request.force and not gate.get("passed")),
        "profile": updated,
        "gate": gate,
    }


@app.post("/v1/model-arena/rollback-default")
def rollback_model_arena_default(
    request: ModelArenaRollbackRequest,
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    with get_db_ctx() as db:
        profile = model_profile_service.get_model_profile(db, current_user.id, request.profile_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="模型配置不存在")
        if not profile.is_active:
            raise HTTPException(status_code=400, detail="回滚目标模型配置已停用")
        updated = model_profile_service.update_model_profile(
            db,
            user_id=current_user.id,
            profile_id=request.profile_id,
            is_default=True,
            is_active=True,
        )
    return {"rolled_back": True, "profile": updated, "reason": (request.reason or "").strip() or None}


@app.get("/v1/daily-product/strategy-skills")
def list_daily_product_strategy_skills(
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    return {
        "routing_modes": [
            {"id": "manual", "name": "手动编排", "description": "仅使用传入的 strategy_skills"},
            {"id": "auto", "name": "自动编排", "description": "基于市场与回测反馈自动选择策略技能"},
        ],
        "skills": _DAILY_PRODUCT_STRATEGY_SKILLS,
        "default_auto_skills": ["analysis_framework", "market_strategy", "risk_scoring"],
    }


def _require_job_owner(job_id: str, current_user: UserDB) -> Dict[str, Any]:
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    owner_id = job.get("user_id")
    if owner_id and owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@app.get("/v1/jobs/{job_id}", response_model=JobStatusResponse)
async def get_job_status(job_id: str, current_user: UserDB = Depends(_require_api_user)) -> JobStatusResponse:
    job = _require_job_owner(job_id, current_user)
    return JobStatusResponse(
        job_id=job["job_id"],
        status=job["status"],
        created_at=job["created_at"],
        started_at=job.get("started_at"),
        finished_at=job.get("finished_at"),
        symbol=job["symbol"],
        trade_date=job["trade_date"],
        error=job.get("error"),
        waiting_ahead_count=job.get("waiting_ahead_count"),
        scheduled_running_count=job.get("scheduled_running_count"),
        scheduled_concurrency_limit=job.get("scheduled_concurrency_limit"),
        progress=job.get("progress"),
        phase=job.get("phase"),
        progress_detail=job.get("progress_detail"),
    )


@app.get("/v1/jobs/{job_id}/result")
def get_job_result(job_id: str, current_user: UserDB = Depends(_require_api_user)) -> Dict[str, Any]:
    job = _require_job_owner(job_id, current_user)
    if job["status"] != "completed":
        raise HTTPException(status_code=409, detail=f"job status is {job['status']}")
    return {
        "job_id": job_id,
        "status": job["status"],
        "decision": job.get("decision"),
        "result": job.get("result"),
        "finished_at": job.get("finished_at"),
    }


@app.get("/v1/jobs/{job_id}/events")
def stream_job_events(job_id: str, current_user: UserDB = Depends(_require_api_user)):
    _require_job_owner(job_id, current_user)
    return StreamingResponse(
        _stream_job_events(job_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


async def _ai_extract_symbol_and_date_streaming(
    text: str, config: Dict[str, Any], job_id: str
) -> tuple[Optional[str], Optional[str], List[str], List[str], List[str], Dict[str, Any]]:
    """
    Async streaming version of _ai_extract_symbol_and_date.
    Emits agent.token events so the frontend can show streaming output during extraction.
    """
    from tradingagents.llm_clients.factory import create_llm_client
    import json as _json

    today = datetime.now().strftime("%Y-%m-%d")
    llm_name: Optional[str] = None
    llm_date: Optional[str] = None
    llm_horizons: List[str] = ["short"]
    llm_focus_areas: List[str] = []
    llm_specific_questions: List[str] = []
    llm_user_context: Dict[str, Any] = {}

    try:
        client = create_llm_client(
            provider=config.get("llm_provider", "openai"),
            model=config.get("quick_think_llm"),
            base_url=config.get("backend_url"),
            api_key=config.get("api_key"),
        )
        prompt = f"""你是金融数据助手。从用户消息中提取以下字段并以 JSON 输出。

字段说明：
- stock_name：用户提到的公司名称或股票代码原文（如"华盛天成"、"贵州茅台"、"600519"、"AAPL"）；美股直接填 ticker。
- date：YYYY-MM-DD 格式。今天是 {today}，如未提及则填今天。
- horizons：分析周期，只能选一个：
  * 用户明确提到"中线/中期/几个月/季度/长期/趋势投资"→ ["medium"]
  * 其他所有情况（含未提及）→ ["short"]
- focus_areas：用户关注的分析维度关键词列表，如 ["技术面", "资金面", "业绩"]，未提及则 []。
- specific_questions：用户提出的具体问题列表，如 ["近期有无催化剂？", "主力是否出货？"]，未提及则 []。
- user_context：从自然语言中提取的账户与约束对象。若未提及返回 {{}}。可包含：
  * objective：建仓 / 加仓 / 减仓 / 止损 / 观察 / 持有处理
  * risk_profile：保守 / 平衡 / 激进
  * investment_horizon：短线 / 波段 / 中线 / 长期
  * cash_available / current_position / current_position_pct / average_cost / max_loss_pct：数字
  * constraints：字符串数组
  * user_notes：仅保留重要但未能结构化归类的信息

仅输出 JSON，不要任何其他文字：
{{"stock_name": "...", "date": "YYYY-MM-DD", "horizons": ["short"], "focus_areas": [], "specific_questions": [], "user_context": {{}}}}

如果无法识别股票标的：{{"stock_name": null, "date": null, "horizons": ["short"], "focus_areas": [], "specific_questions": [], "user_context": {{}}}}

用户消息："{text}"
"""
        llm = client.get_llm()
        _log(f"[LLM Debug] Streaming StockExtract with model: {getattr(llm, 'model_name', 'unknown')}")

        full_content = ""
        async for chunk in llm.astream(prompt):
            token = chunk.content if hasattr(chunk, "content") else str(chunk)
            full_content += token
            if token:
                _emit_job_event(job_id, "agent.token", {
                    "agent": "意图解析",
                    "report": "stock_extract",
                    "token": token,
                })

        _log(f"[LLM Debug] StockExtract response: {full_content[:200]}")
        m = re.search(r"\{.*\}", full_content, re.DOTALL)
        if m:
            data = _json.loads(m.group(0))
            llm_name = (data.get("stock_name") or "").strip() or None
            llm_date = data.get("date") or today
            llm_horizons = data.get("horizons") or ["short"]
            llm_focus_areas = data.get("focus_areas") or []
            llm_specific_questions = data.get("specific_questions") or []
            llm_user_context = normalize_user_context(data.get("user_context") or {})
    except Exception as e:
        _log(f"[StockExtract streaming] LLM failed: {e}")

    if not llm_name:
        fallback_symbol = await asyncio.to_thread(_fallback_extract_symbol, text)
        fallback_date = (llm_date or today)
        if fallback_symbol:
            _log(f"[StockExtract streaming] Fallback parsed symbol='{fallback_symbol}' from raw text")
        return fallback_symbol, fallback_date, llm_horizons, llm_focus_areas, llm_specific_questions, llm_user_context

    _log(f"[StockExtract] extracted name='{llm_name}', date={llm_date}, horizons={llm_horizons}")
    if re.match(r"^\d{6}$", llm_name) or re.match(r"^[A-Za-z]{1,6}(\.[A-Za-z]+)?$", llm_name):
        symbol = _normalize_symbol(llm_name)
        return symbol or None, llm_date, llm_horizons, llm_focus_areas, llm_specific_questions, llm_user_context

    local_code = await asyncio.to_thread(_search_cn_stock_by_name, llm_name)
    if local_code:
        return local_code, llm_date, llm_horizons, llm_focus_areas, llm_specific_questions, llm_user_context

    fallback = _normalize_symbol(llm_name)
    if fallback:
        return fallback, llm_date, llm_horizons, llm_focus_areas, llm_specific_questions, llm_user_context

    fallback_symbol = await asyncio.to_thread(_fallback_extract_symbol, text)
    if fallback_symbol:
        _log(f"[StockExtract streaming] Name resolve failed, fallback symbol='{fallback_symbol}' from raw text")
    return fallback_symbol, llm_date, llm_horizons, llm_focus_areas, llm_specific_questions, llm_user_context


def _ai_extract_symbol_and_date(
    text: str, config: Dict[str, Any]
) -> tuple[Optional[str], Optional[str], List[str], List[str], List[str], Dict[str, Any]]:
    """
    Single-LLM extraction: stock name, date, horizons, focus_areas, specific_questions.
    Then resolves the stock name to an authoritative code via akshare.
    Returns (symbol, date, horizons, focus_areas, specific_questions, inferred_user_context).
    """
    from tradingagents.llm_clients.factory import create_llm_client
    import json as _json

    today = datetime.now().strftime("%Y-%m-%d")

    llm_name: Optional[str] = None
    llm_date: Optional[str] = None
    llm_horizons: List[str] = ["short"]
    llm_focus_areas: List[str] = []
    llm_specific_questions: List[str] = []
    llm_user_context: Dict[str, Any] = {}
    try:
        client = create_llm_client(
            provider=config.get("llm_provider", "openai"),
            model=config.get("quick_think_llm"),
            base_url=config.get("backend_url"),
            api_key=config.get("api_key"),
        )
        prompt = f"""你是金融数据助手。从用户消息中提取以下字段并以 JSON 输出。

字段说明：
- stock_name：用户提到的公司名称或股票代码原文（如"华盛天成"、"贵州茅台"、"600519"、"AAPL"）；美股直接填 ticker。
- date：YYYY-MM-DD 格式。今天是 {today}，如未提及则填今天。
- horizons：分析周期，只能选一个：
  * 用户明确提到"中线/中期/几个月/季度/长期/趋势投资"→ ["medium"]
  * 其他所有情况（含未提及）→ ["short"]
- focus_areas：用户关注的分析维度关键词列表，如 ["技术面", "资金面", "业绩"]，未提及则 []。
- specific_questions：用户提出的具体问题列表，如 ["近期有无催化剂？", "主力是否出货？"]，未提及则 []。
- user_context：从自然语言中提取的账户与约束对象。若未提及返回 {{}}。可包含：
  * objective：建仓 / 加仓 / 减仓 / 止损 / 观察 / 持有处理
  * risk_profile：保守 / 平衡 / 激进
  * investment_horizon：短线 / 波段 / 中线 / 长期
  * cash_available / current_position / current_position_pct / average_cost / max_loss_pct：数字
  * constraints：字符串数组
  * user_notes：仅保留重要但未能结构化归类的信息

仅输出 JSON，不要任何其他文字：
{{"stock_name": "...", "date": "YYYY-MM-DD", "horizons": ["short"], "focus_areas": [], "specific_questions": [], "user_context": {{}}}}

如果无法识别股票标的：{{"stock_name": null, "date": null, "horizons": ["short"], "focus_areas": [], "specific_questions": [], "user_context": {{}}}}

用户消息："{text}"
"""
        llm = client.get_llm()
        
        # 调试日志：打印请求参数
        target_url = getattr(llm, 'openai_api_base', 'default')
        _log(f"[LLM Debug] Requesting StockExtract with model: {getattr(llm, 'model_name', 'unknown')} at {target_url}")
        _log(f"[LLM Debug] Prompt: {prompt[:500]}...")

        response = llm.invoke(prompt)
        raw = response if isinstance(response, str) else getattr(response, "content", str(response))
        
        # 调试日志：打印原始响应
        _log(f"[LLM Debug] Raw Response: {raw}")

        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            data = _json.loads(m.group(0))
            llm_name = (data.get("stock_name") or "").strip() or None
            llm_date = data.get("date") or today
            llm_horizons = data.get("horizons") or ["short"]
            llm_focus_areas = data.get("focus_areas") or []
            llm_specific_questions = data.get("specific_questions") or []
            llm_user_context = normalize_user_context(data.get("user_context") or {})
    except Exception as e:
        _log(f"[StockExtract] LLM failed: {e}")

    if not llm_name:
        _log(f"[StockExtract] LLM returned no stock name for: '{text[:40]}'")
        fallback_symbol = _fallback_extract_symbol(text)
        fallback_date = llm_date or today
        if fallback_symbol:
            _log(f"[StockExtract] Fallback parsed symbol='{fallback_symbol}' from raw text")
        return fallback_symbol, fallback_date, llm_horizons, llm_focus_areas, llm_specific_questions, llm_user_context

    _log(f"[StockExtract] LLM extracted name='{llm_name}', date={llm_date}, horizons={llm_horizons}")

    # ── Step 2: If looks like a direct code (digits / letters), normalize it ──
    if re.match(r"^\d{6}$", llm_name) or re.match(r"^[A-Za-z]{1,6}(\.[A-Za-z]+)?$", llm_name):
        symbol = _normalize_symbol(llm_name)
        _log(f"[StockExtract] Direct code: {symbol}")
        return symbol or None, llm_date, llm_horizons, llm_focus_areas, llm_specific_questions, llm_user_context

    # ── Step 3: Search akshare A-share name database ──────────────────────────
    local_code = _search_cn_stock_by_name(llm_name)
    if local_code:
        _log(f"[StockExtract] akshare match: '{llm_name}' → {local_code}")
        return local_code, llm_date, llm_horizons, llm_focus_areas, llm_specific_questions, llm_user_context

    # ── Step 4: Last resort — treat LLM name as a raw code ────────────────────
    fallback = _normalize_symbol(llm_name)
    if fallback:
        _log(f"[StockExtract] Fallback normalize: '{llm_name}' → {fallback}")
        return fallback, llm_date, llm_horizons, llm_focus_areas, llm_specific_questions, llm_user_context

    _log(f"[StockExtract] Could not resolve '{llm_name}' to a stock code")
    fallback_symbol = _fallback_extract_symbol(text)
    if fallback_symbol:
        _log(f"[StockExtract] Name resolve failed, fallback symbol='{fallback_symbol}' from raw text")
    return fallback_symbol, llm_date, llm_horizons, llm_focus_areas, llm_specific_questions, llm_user_context

@app.post("/v1/chat/completions")
async def chat_completions(
    request: ChatCompletionRequest,
    current_user: UserDB = Depends(_require_api_user),
):
    text = _extract_chat_text(request.messages)
    with get_db_ctx() as db:
        # 方案A：未显式指定模型配置时，自动落到数据库中的「系统默认模型」（is_default=True 的 model profile）
        effective_profile_id = _resolve_effective_model_profile_id(
            db,
            user_id=current_user.id,
            model_profile_id=request.model_profile_id,
        )
        merged_overrides = _merge_model_profile_overrides(
            db,
            user_id=current_user.id,
            model_profile_id=effective_profile_id,
            request_overrides=request.config_overrides,
        )
    config = await asyncio.to_thread(
        _build_runtime_config,
        merged_overrides,
        user_id=current_user.id,
        trusted_overrides=True,
        strategy="profile_selected" if effective_profile_id else "default",
    )

    # ── 流式模式：立刻返回 SSE 流，在后台异步提取意图再启动任务 ──────────────────
    # 这样用户提交查询后立刻收到 job.ready，不用等待 thinking 模型的 StockExtract。
    if request.stream:
        job_id = uuid4().hex
        _ensure_job_event_queue(job_id)

        async def _extract_and_run():
            try:
                symbol, trade_date, horizons, focus_areas, specific_questions, inferred_user_context = \
                    await _ai_extract_symbol_and_date_streaming(text, config, job_id)

                if not symbol:
                    _emit_job_event(job_id, "job.failed", {
                        "error": "抱歉，我没能从您的消息中识别出股票标的。请输入代码（如 600519.SH）或可识别的公司名称。"
                    })
                    return

                pre_intent = {
                    "raw_query": text,
                    "ticker": symbol,
                    "horizons": horizons,
                    "focus_areas": focus_areas,
                    "specific_questions": specific_questions,
                }
                with get_db_ctx() as db:
                    merged_user_context = _compose_analysis_user_context(
                        db,
                        current_user.id,
                        symbol,
                        explicit_context=_extract_request_user_context(request),
                        inferred_context=inferred_user_context,
                    )
                pre_intent["user_context"] = merged_user_context
                analyze_req = AnalyzeRequest(
                    symbol=symbol,
                    trade_date=trade_date or cn_today_str(),
                    selected_analysts=request.selected_analysts,
                    config_overrides=request.config_overrides,
                    model_profile_id=request.model_profile_id,
                    dry_run=request.dry_run,
                    query=text,
                    horizons=horizons,
                    user_intent=pre_intent,
                    objective=merged_user_context.get("objective"),
                    risk_profile=merged_user_context.get("risk_profile"),
                    investment_horizon=merged_user_context.get("investment_horizon"),
                    cash_available=merged_user_context.get("cash_available"),
                    current_position=merged_user_context.get("current_position"),
                    current_position_pct=merged_user_context.get("current_position_pct"),
                    average_cost=merged_user_context.get("average_cost"),
                    max_loss_pct=merged_user_context.get("max_loss_pct"),
                    constraints=merged_user_context.get("constraints", []),
                    user_notes=merged_user_context.get("user_notes"),
                )
                now = _utcnow_iso()
                _set_job(
                    job_id,
                    job_id=job_id,
                    user_id=current_user.id,
                    status="pending",
                    created_at=now,
                    started_at=None,
                    finished_at=None,
                    symbol=analyze_req.symbol,
                    trade_date=analyze_req.trade_date,
                    error=None,
                    result=None,
                    decision=None,
                )
                _emit_job_event(
                    job_id,
                    "job.created",
                    {"job_id": job_id, "symbol": analyze_req.symbol, "trade_date": analyze_req.trade_date},
                )
                await _run_job(job_id, analyze_req, True, True, current_user.id, "chat")
            except Exception as exc:
                _log(f"[chat] _extract_and_run failed: {exc}")
                _emit_job_event(job_id, "job.failed", {"error": str(exc)})

        _create_tracked_task(_extract_and_run())
        return StreamingResponse(
            _stream_job_events(job_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    # ── 非流式模式：保持原有阻塞行为 ─────────────────────────────────────────────
    symbol, trade_date, horizons, focus_areas, specific_questions, inferred_user_context = \
        await asyncio.to_thread(_ai_extract_symbol_and_date, text, config)

    if not symbol:
        raise HTTPException(status_code=400, detail="抱歉，我没能从您的消息中识别出股票标的。请输入代码（如 600519.SH）或可识别的公司名称。")

    pre_intent = {
        "raw_query": text,
        "ticker": symbol,
        "horizons": horizons,
        "focus_areas": focus_areas,
        "specific_questions": specific_questions,
    }
    with get_db_ctx() as db:
        merged_user_context = _compose_analysis_user_context(
            db,
            current_user.id,
            symbol,
            explicit_context=_extract_request_user_context(request),
            inferred_context=inferred_user_context,
        )
    pre_intent["user_context"] = merged_user_context
    analyze_req = AnalyzeRequest(
        symbol=symbol,
        trade_date=trade_date or cn_today_str(),
        selected_analysts=request.selected_analysts,
        config_overrides=request.config_overrides,
        model_profile_id=request.model_profile_id,
        dry_run=request.dry_run,
        query=text,
        horizons=horizons,
        user_intent=pre_intent,
        objective=merged_user_context.get("objective"),
        risk_profile=merged_user_context.get("risk_profile"),
        investment_horizon=merged_user_context.get("investment_horizon"),
        cash_available=merged_user_context.get("cash_available"),
        current_position=merged_user_context.get("current_position"),
        current_position_pct=merged_user_context.get("current_position_pct"),
        average_cost=merged_user_context.get("average_cost"),
        max_loss_pct=merged_user_context.get("max_loss_pct"),
        constraints=merged_user_context.get("constraints", []),
        user_notes=merged_user_context.get("user_notes"),
    )
    job_id = uuid4().hex
    now = _utcnow_iso()
    _set_job(
        job_id,
        job_id=job_id,
        user_id=current_user.id,
        status="pending",
        created_at=now,
        started_at=None,
        finished_at=None,
        symbol=analyze_req.symbol,
        trade_date=analyze_req.trade_date,
        error=None,
        result=None,
        decision=None,
    )
    _ensure_job_event_queue(job_id)
    _emit_job_event(
        job_id,
        "job.created",
        {"job_id": job_id, "symbol": analyze_req.symbol, "trade_date": analyze_req.trade_date},
    )
    if request.dry_run:
        await _run_job(job_id, analyze_req, True, True, current_user.id, "chat")
        status_text = _jobs.get(job_id, {}).get("status", "completed")
        decision_text = _jobs.get(job_id, {}).get("decision", "DRY_RUN")
        return {
            "id": f"chatcmpl-{job_id}",
            "object": "chat.completion",
            "created": int(datetime.now().timestamp()),
            "model": request.model,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": (
                            f"已完成分析任务：{job_id}\n"
                            f"symbol={analyze_req.symbol}, trade_date={analyze_req.trade_date}\n"
                            f"status={status_text}, decision={decision_text}"
                        ),
                    },
                }
            ],
        }
    _create_tracked_task(_run_job(job_id, analyze_req, True, True, current_user.id, "chat"))
    return {
        "id": f"chatcmpl-{job_id}",
        "object": "chat.completion",
        "created": int(datetime.now().timestamp()),
        "model": request.model,
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": (
                        f"已启动分析任务：{job_id}\n"
                        f"symbol={analyze_req.symbol}, trade_date={analyze_req.trade_date}\n"
                        f"可通过 /v1/jobs/{job_id} 与 /v1/jobs/{job_id}/result 查询结果。"
                    ),
                },
            }
        ],
    }


# Report API Endpoints
@app.post("/v1/reports", response_model=ReportResponse)
def create_report_endpoint(
    request: ReportCreateRequest,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(_require_api_user),
):
    """手动创建报告（通常由系统自动调用）."""
    # The body's `decision` is free text from the caller, and it used to be written
    # into reports.decision verbatim — so any client string ("ok", "", "买入?")
    # became a stored trading decision and entered every downstream average.
    # Coerce to the persisted vocabulary; anything unrecognised is recorded as the
    # explicit abstention value "NA".
    report = report_service.create_report(
        db=db,
        symbol=request.symbol,
        trade_date=request.trade_date,
        decision=coerce_persistable_decision(request.decision),
        result_data=request.result_data,
        user_id=current_user.id,
    )
    _attach_report_model_info(report)
    _fill_missing_model_profile_names(db, user_id=current_user.id, reports=[report])
    return report


@app.get("/v1/announcements/latest", response_model=LatestAnnouncementResponse)
def get_latest_announcement():
    return {"announcement": _load_latest_announcement()}


@app.get("/v1/reports", response_model=ReportListResponse)
def list_reports(
    symbol: Optional[str] = Query(None, description="按股票代码筛选"),
    search: Optional[str] = Query(
        None,
        description="按股票代码（模糊）或中文名称关键字搜索；与 symbol 同时传入时为交集",
    ),
    start_date: Optional[str] = Query(
        None,
        description="时间下限：YYYY-MM-DD 按 trade_date；含时分（ISO 或 YYYY-MM-DDTHH:MM）按 created_at 精确到分钟",
    ),
    end_date: Optional[str] = Query(
        None,
        description="时间上限：YYYY-MM-DD 按 trade_date；含时分（ISO 或 YYYY-MM-DDTHH:MM）按 created_at 精确到分钟",
    ),
    model_profile_id: Optional[str] = Query(None, description="按模型配置 ID 筛选（含同底层 LLM 名的历史报告）"),
    freshness_issue: Optional[bool] = Query(
        None, description="为 true 时仅返回数据异常/过时/部分滞后的报告"
    ),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(_require_api_user),
):
    """获取报告列表."""
    search_q = (search or "").strip() or None
    if search_q and len(search_q) > 64:
        search_q = search_q[:64]
    start_q = (start_date or "").strip() or None
    end_q = (end_date or "").strip() or None
    if start_q and end_q:
        from api.services.report_service import _parse_report_datetime_bound, _report_range_has_time_component

        if _report_range_has_time_component(start_q) or _report_range_has_time_component(end_q):
            start_dt = _parse_report_datetime_bound(start_q, is_end=False)
            end_dt = _parse_report_datetime_bound(end_q, is_end=True)
            if start_dt > end_dt:
                raise HTTPException(status_code=400, detail="start_date must be <= end_date")
        elif start_q > end_q:
            raise HTTPException(status_code=400, detail="start_date must be <= end_date")
    model_pid = str(model_profile_id or "").strip() or None
    model_deep: Optional[str] = None
    model_quick: Optional[str] = None
    if model_pid:
        profile = model_profile_service.get_model_profile(db, current_user.id, model_pid)
        if profile is None:
            raise HTTPException(status_code=404, detail="model profile not found")
        model_deep = str(profile.deep_think_llm or "").strip() or None
        model_quick = str(profile.quick_think_llm or "").strip() or None
    total = report_service.count_reports(
        db=db,
        user_id=current_user.id,
        symbol=symbol,
        search=search_q,
        start_date=start_q,
        end_date=end_q,
        model_profile_id=model_pid,
        deep_think_llm=model_deep,
        quick_think_llm=model_quick,
        freshness_issue=freshness_issue,
    )
    reports = report_service.get_reports_by_user(
        db=db,
        user_id=current_user.id,
        symbol=symbol,
        search=search_q,
        start_date=start_q,
        end_date=end_q,
        model_profile_id=model_pid,
        deep_think_llm=model_deep,
        quick_think_llm=model_quick,
        freshness_issue=freshness_issue,
        skip=skip,
        limit=limit,
    )
    code_to_name = _get_reverse_stock_map_cached_only()
    for r in reports:
        r.name = code_to_name.get(r.symbol, r.symbol)
        _attach_job_runtime_state(r, str(getattr(r, "id", "")))
        _attach_report_model_info(r)
    _fill_missing_model_profile_names(db, user_id=current_user.id, reports=reports)
    return {"total": total, "reports": reports}


@app.post("/v1/reports/latest-by-symbols", response_model=LatestReportsBySymbolsResponse)
def list_latest_reports_by_symbols(
    body: LatestReportsBySymbolsRequest,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(_require_api_user),
):
    reports = report_service.get_latest_reports_by_symbols(
        db=db,
        user_id=current_user.id,
        symbols=body.symbols,
    )
    for r in reports:
        _attach_report_model_info(r)
    _fill_missing_model_profile_names(db, user_id=current_user.id, reports=reports)
    return {"reports": reports}


@app.get("/v1/reports/{report_id}", response_model=ReportDetailResponse)
def get_report_endpoint(
    report_id: str,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(_require_api_user),
):
    """获取报告详情."""
    report = report_service.get_report(db, report_id, user_id=current_user.id)
    if not report:
        raise HTTPException(status_code=404, detail="报告不存在")
    if str(report.status or "") in report_service.ACTIVE_REPORT_STATUSES and not _get_job(report_id):
        report = report_service.finalize_orphan_report(db, report)
    code_to_name = _get_reverse_stock_map()
    report.name = code_to_name.get(report.symbol, report.symbol)
    _attach_job_runtime_state(report, report_id)
    _attach_report_model_info(report)
    _fill_missing_model_profile_names(db, user_id=current_user.id, reports=[report])
    return report


@app.get("/v1/reports/{report_id}/export/html")
def export_report_html(
    report_id: str,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(_require_api_user),
):
    """导出单份分析报告为 HTML（stock-analysis-team 风格）。"""
    report = report_service.get_report(db, report_id, user_id=current_user.id)
    if not report:
        raise HTTPException(status_code=404, detail="报告不存在")
    code_to_name = _get_reverse_stock_map()
    stock_name = code_to_name.get(report.symbol, report.symbol)
    html = stock_team_report_service.render_html_report(report, stock_name=stock_name)
    filename = f"{report.symbol}_{report.trade_date}_{report.id}.html"
    headers = {"Content-Disposition": f'inline; filename="{filename}"'}
    return Response(content=html, media_type="text/html; charset=utf-8", headers=headers)


@app.get("/v1/reports/{report_id}/export/stock-team-enhanced-html")
def export_stock_team_enhanced_report_html(
    report_id: str,
    market: Literal["cn", "us"] = Query("cn"),
    period: str = Query("6mo"),
    include_charts: bool = Query(True),
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(_require_api_user),
):
    """导出增强版 HTML 报告（stock-analysis-team 脚本生成）。"""
    report = report_service.get_report(db, report_id, user_id=current_user.id)
    if not report:
        raise HTTPException(status_code=404, detail="报告不存在")
    code_to_name = _get_reverse_stock_map()
    stock_name = code_to_name.get(report.symbol, report.symbol)
    try:
        result = stock_analysis_skill_service.generate_enhanced_html_from_report(
            report=report,
            stock_name=stock_name,
            market=market,
            period=period,
            include_charts=include_charts,
        )
    except ValueError as exc:
        detail = {
            "invalid_symbol": "报告中的股票代码不合法，无法生成增强报告",
            "invalid_period": "参数 period 必须是 1y、6mo、3mo、1mo 之一",
        }.get(str(exc), "参数错误")
        raise HTTPException(status_code=400, detail=detail)
    if result.get("error") == "skill_not_installed":
        raise HTTPException(
            status_code=503,
            detail="未找到增强报告脚本或缺少依赖，请执行: uv sync --group stock-analysis-team",
        )
    if result.get("error") == "timeout":
        raise HTTPException(status_code=504, detail=result.get("detail") or "增强报告生成超时")
    if result.get("error") == "script_failed":
        raise HTTPException(
            status_code=502,
            detail=result.get("stderr") or result.get("stdout") or "增强报告脚本执行失败",
        )
    html = str(result.get("html") or "")
    if not html:
        raise HTTPException(status_code=500, detail="增强报告生成失败：未产出HTML内容")
    filename = f"{report.symbol}_{report.trade_date}_{report.id}_enhanced.html"
    headers = {"Content-Disposition": f'inline; filename="{filename}"'}
    return Response(content=html, media_type="text/html; charset=utf-8", headers=headers)


@app.delete("/v1/reports/{report_id}")
def delete_report_endpoint(
    report_id: str,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(_require_api_user),
):
    """删除报告."""
    success = report_service.delete_report(db, report_id, user_id=current_user.id)
    if not success:
        raise HTTPException(status_code=404, detail="报告不存在")
    return {"message": "报告已删除"}


@app.post("/v1/reports/batch/delete", response_model=ReportBatchDeleteResponse)
def batch_delete_reports_endpoint(
    body: ReportBatchDeleteRequest,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(_require_api_user),
):
    try:
        return report_service.batch_delete_reports(db, body.report_ids, user_id=current_user.id)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ─── API Token Endpoints ────────────────────────────────────────────────────

@app.get("/v1/tokens", response_model=List[UserTokenListItem])
def list_tokens(
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(_require_web_user),
):
    """获取当前用户的所有 API Token（不返回完整 token）。"""
    return token_service.list_user_tokens(db, current_user.id)


@app.post("/v1/tokens", response_model=UserTokenResponse)
def create_token(
    request: UserTokenCreateRequest,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(_require_web_user),
):
    """创建一个新的 API Token。完整 token 仅在此接口返回一次。"""
    try:
        return token_service.create_token(db, current_user.id, request.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/v1/tokens/{token_id}")
def delete_token(
    token_id: str,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(_require_web_user),
):
    """吊销并删除一个 API Token。"""
    success = token_service.delete_token(db, current_user.id, token_id)
    if not success:
        raise HTTPException(status_code=404, detail="Token 不存在")
    return {"message": "Token 已吊销"}


# ─── Backtest Endpoints ───────────────────────────────────────────────────────

from api.services import backtest_service as _bt


class BacktestRequest(BaseModel):
    symbol: str
    start_date: str
    end_date: str
    selected_analysts: List[str] = ["market", "news", "fundamentals", "sentiment"]
    hold_days: int = 5
    sample_interval: int = 7
    config_overrides: Optional[Dict[str, Any]] = None


@app.post("/v1/backtest")
def submit_backtest(
    request: BacktestRequest,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(_require_api_user),
) -> Dict:
    """提交历史回测任务，返回 job_id."""
    config = _build_runtime_config(request.config_overrides or {}, user_id=current_user.id, db=db)
    job_id = _bt.submit(
        symbol=request.symbol,
        start_date=request.start_date,
        end_date=request.end_date,
        selected_analysts=request.selected_analysts,
        hold_days=request.hold_days,
        sample_interval=request.sample_interval,
        config=config,
        user_id=current_user.id,
    )
    return {"job_id": job_id, "status": "pending"}


@app.get("/v1/backtest")
def list_backtests() -> Dict:
    """列出所有回测任务."""
    jobs = _bt.list_jobs()
    return {"jobs": jobs, "total": len(jobs)}


@app.get("/v1/backtest/{job_id}")
def get_backtest(job_id: str) -> Dict:
    """获取回测任务状态和结果."""
    job = _bt.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="回测任务不存在")
    return job


@app.delete("/v1/backtest/{job_id}")
def delete_backtest(job_id: str) -> Dict:
    """删除回测任务."""
    if not _bt.delete_job(job_id):
        raise HTTPException(status_code=404, detail="回测任务不存在")
    return {"message": "已删除"}


# ─── Runtime Config Endpoints ────────────────────────────────────────────────

_CONFIG_ALLOWED_KEYS = {
    "llm_provider", "deep_think_llm", "quick_think_llm",
    "backend_url", "max_debate_rounds", "max_risk_discuss_rounds",
    "decision_critic_enabled", "decision_critic_revision_threshold",
    "methodology_ashare_fundamentals", "methodology_ashare_news_events", "methodology_ashare_sector_macro",
    "methodology_stock_analysis_team",
    "methodology_extra_path", "methodology_extra_path_news", "methodology_extra_path_macro",
    "finskills_root", "stock_analysis_team_root",
}
_CONFIG_PREFERENCE_KEYS = {"email_report_enabled", "wecom_report_enabled", "wps_report_enabled"}
_CONFIG_MODEL_KEYS = ("llm_provider", "backend_url", "quick_think_llm", "deep_think_llm")
_CONFIG_MODEL_LABELS = {
    "quick_think_llm": "常规模型",
    "deep_think_llm": "推理模型",
}
_CONFIG_PROBE_TIMEOUT_SECONDS = 12.0
_CONFIG_PROBE_PROMPT = "Reply with the single word OK."
_CONFIG_WARMUP_TIMEOUT_SECONDS = 20.0
_CONFIG_WARMUP_PROMPT = "Reply with the single word OK."


def _mask_secret_value(value: Optional[str], *, head: int = 4, tail: int = 4) -> Optional[str]:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    if len(normalized) <= head + tail:
        return "*" * max(6, len(normalized))
    return f"{normalized[:head]}{'*' * max(6, len(normalized) - head - tail)}{normalized[-tail:]}"


def _mask_wecom_webhook(webhook_url: Optional[str]) -> Optional[str]:
    normalized = str(webhook_url or "").strip()
    if not normalized:
        return None
    prefix = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key="
    if normalized.startswith(prefix):
        masked_key = _mask_secret_value(normalized[len(prefix):])
        return f"{prefix}{masked_key}"
    if normalized.startswith("http"):
        if "key=" in normalized:
            base, key = normalized.rsplit("key=", 1)
            return f"{base}key={_mask_secret_value(key)}"
        return _mask_secret_value(normalized, head=18, tail=8)
    return _mask_secret_value(normalized)


def _mask_wps_webhook(webhook_url: Optional[str]) -> Optional[str]:
    normalized = str(webhook_url or "").strip()
    if not normalized:
        return None
    prefix = "https://xz.wps.cn/api/v1/webhook/send?key="
    if normalized.startswith(prefix):
        masked_key = _mask_secret_value(normalized[len(prefix):])
        return f"{prefix}{masked_key}"
    if normalized.startswith("http") and "key=" in normalized:
        base, key = normalized.rsplit("key=", 1)
        return f"{base}key={_mask_secret_value(key)}"
    return _mask_secret_value(normalized)


def _warmup_model_names(config: Dict[str, Any]) -> List[str]:
    seen: set[str] = set()
    models: List[str] = []
    for key in ("quick_think_llm", "deep_think_llm"):
        value = str(config.get(key) or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        models.append(value)
    return models


def _warmup_model_targets(config: Dict[str, Any]) -> List[Tuple[str, List[str]]]:
    targets: Dict[str, List[str]] = {}
    for key in ("quick_think_llm", "deep_think_llm"):
        model = str(config.get(key) or "").strip()
        if not model:
            continue
        labels = targets.setdefault(model, [])
        label = _CONFIG_MODEL_LABELS.get(key, key)
        if label not in labels:
            labels.append(label)
    return [(model, labels) for model, labels in targets.items()]


def _should_trigger_config_warmup(
    before_cfg: UserRuntimeConfigResponse,
    after_cfg: UserRuntimeConfigResponse,
    updates: UserRuntimeConfigUpdateRequest,
) -> bool:
    if not updates.warmup:
        return False
    if updates.force_warmup:
        return True
    if updates.api_key:
        return True
    before = before_cfg.model_dump()
    after = after_cfg.model_dump()
    return any(before.get(key) != after.get(key) for key in _CONFIG_MODEL_KEYS)


def _build_pending_runtime_config(
    updates: UserRuntimeConfigUpdateRequest,
    user_id: str,
    db: Session,
) -> Dict[str, Any]:
    config = _build_runtime_config({}, user_id=user_id, db=db)
    for key in _CONFIG_ALLOWED_KEYS:
        value = getattr(updates, key, None)
        if value is not None:
            config[key] = value

    if updates.clear_api_key:
        config["api_key"] = ""
    elif updates.api_key:
        config["api_key"] = updates.api_key

    quick = config.get("quick_think_llm")
    deep = config.get("deep_think_llm")
    if not deep and quick:
        config["deep_think_llm"] = quick
    if not quick and deep:
        config["quick_think_llm"] = deep
    _sanitize_model_backend_compatibility(config)
    return config


def _should_probe_runtime_config(
    before_cfg: UserRuntimeConfigResponse,
    pending_cfg: Dict[str, Any],
    updates: UserRuntimeConfigUpdateRequest,
) -> bool:
    del before_cfg, pending_cfg
    if updates.clear_api_key:
        return False
    return bool(updates.api_key)


def _probe_runtime_config(config: Dict[str, Any]) -> Dict[str, str]:
    from tradingagents.llm_clients.factory import create_llm_client

    provider = str(config.get("llm_provider") or "openai")
    base_url = config.get("backend_url")
    api_key = str(config.get("api_key") or "").strip()
    model = str(config.get("quick_think_llm") or config.get("deep_think_llm") or "").strip()

    if not model or not api_key:
        return {"status": "skipped", "reason": "missing_model_or_key"}

    try:
        client = create_llm_client(
            provider=provider,
            model=model,
            base_url=base_url,
            api_key=api_key,
            timeout=_CONFIG_PROBE_TIMEOUT_SECONDS,
            max_retries=0,
        )
        llm = client.get_llm()
        response = llm.invoke(_CONFIG_PROBE_PROMPT)
        raw = response if isinstance(response, str) else getattr(response, "content", str(response))
        preview = str(raw).strip().replace("\n", " ")[:80] or "<empty>"
        return {"status": "ok", "model": model, "preview": preview}
    except Exception as exc:
        detail = str(exc).strip()
        lowered = detail.lower()
        if "401" in lowered or "invalid authentication" in lowered or "authenticationerror" in lowered:
            raise HTTPException(
                status_code=400,
                detail="模型 Key 验证失败：上游返回 401 Invalid Authentication，请检查 API Key 是否正确。",
            ) from exc
        raise HTTPException(
            status_code=400,
            detail=f"模型连接验证失败：{detail[:200] or 'unknown error'}",
        ) from exc


def _invoke_runtime_warmup(
    config: Dict[str, Any],
    prompt: str,
    user_id: str,
    timeout: float = _CONFIG_WARMUP_TIMEOUT_SECONDS,
) -> List[Dict[str, Any]]:
    from tradingagents.llm_clients.factory import create_llm_client

    provider = str(config.get("llm_provider") or "openai")
    base_url = config.get("backend_url")
    api_key = config.get("api_key")
    targets = _warmup_model_targets(config)

    if not targets:
        raise HTTPException(status_code=400, detail="请先配置至少一个可用模型。")

    _log(
        f"[LLM Warmup] user={user_id} invoking provider={provider} "
        f"models={[model for model, _ in targets]} base_url={base_url or 'default'}"
    )

    results: List[Dict[str, Any]] = []
    errors: List[str] = []
    for model, labels in targets:
        try:
            client = create_llm_client(
                provider=provider,
                model=model,
                base_url=base_url,
                api_key=api_key,
                timeout=timeout,
                max_retries=0,
            )
            llm = client.get_llm()
            response = llm.invoke(prompt)
            raw = response if isinstance(response, str) else getattr(response, "content", str(response))
            content = str(raw).strip() or "<empty>"
            preview = content.replace("\n", " ")[:80]
            _log(f"[LLM Warmup] user={user_id} model={model} success response={preview}")
            results.append({
                "model": model,
                "targets": labels,
                "content": content,
                "error": None,
            })
        except Exception as exc:
            detail = str(exc).strip() or "unknown error"
            errors.append(f"{model}: {detail}")
            logger.warning(
                "[LLM Warmup] user=%s model=%s failed: %s",
                user_id,
                model,
                exc,
            )
            results.append({
                "model": model,
                "targets": labels,
                "content": None,
                "error": detail[:200],
            })

    if not any(item.get("content") for item in results):
        raise HTTPException(
            status_code=400,
            detail=f"模型 warmup 失败：{'; '.join(errors)[:300]}",
        )

    return results


def _run_config_warmup(config: Dict[str, Any], user_id: str) -> None:
    models = _warmup_model_names(config)
    if not models:
        _log(f"[LLM Warmup] user={user_id} skipped: no models configured")
        return
    try:
        _invoke_runtime_warmup(config, _CONFIG_WARMUP_PROMPT, user_id, timeout=_CONFIG_WARMUP_TIMEOUT_SECONDS)
    except HTTPException as exc:
        logger.warning("[LLM Warmup] user=%s failed: %s", user_id, exc.detail)


def _config_response_for_user(user: Optional[UserDB], db: Session) -> UserRuntimeConfigResponse:
    cfg = _build_runtime_config({}, user_id=user.id if user else None, db=db)
    user_cfg = auth_service.get_user_llm_config(db, user.id) if user else None
    webhook_url = auth_service.decrypt_secret(getattr(user_cfg, "wecom_webhook_encrypted", None))
    wps_hook = auth_service.decrypt_secret(getattr(user_cfg, "wps_webhook_encrypted", None))
    return UserRuntimeConfigResponse(
        llm_provider=cfg["llm_provider"],
        deep_think_llm=cfg["deep_think_llm"],
        quick_think_llm=cfg["quick_think_llm"],
        backend_url=cfg["backend_url"],
        max_debate_rounds=cfg["max_debate_rounds"],
        max_risk_discuss_rounds=cfg["max_risk_discuss_rounds"],
        decision_critic_enabled=bool(cfg.get("decision_critic_enabled", True)),
        decision_critic_revision_threshold=float(cfg.get("decision_critic_revision_threshold", 40.0)),
        has_api_key=bool(user_cfg and user_cfg.api_key_encrypted),
        has_wecom_webhook=bool(webhook_url),
        wecom_webhook_display=_mask_wecom_webhook(webhook_url),
        has_wps_webhook=bool(wps_hook),
        wps_webhook_display=_mask_wps_webhook(wps_hook),
        server_fallback_enabled=bool(cfg.get("server_fallback_enabled", True)),
        email_report_enabled=user.email_report_enabled if user and hasattr(user, 'email_report_enabled') else True,
        wecom_report_enabled=user.wecom_report_enabled if user and hasattr(user, "wecom_report_enabled") else True,
        wps_report_enabled=getattr(user, "wps_report_enabled", True) if user else True,
        methodology_ashare_fundamentals=bool(cfg.get("methodology_ashare_fundamentals", True)),
        methodology_ashare_news_events=bool(cfg.get("methodology_ashare_news_events", True)),
        methodology_ashare_sector_macro=bool(cfg.get("methodology_ashare_sector_macro", True)),
        methodology_stock_analysis_team=bool(cfg.get("methodology_stock_analysis_team", True)),
        methodology_extra_path=(str(cfg.get("methodology_extra_path") or "").strip() or None),
        methodology_extra_path_news=(str(cfg.get("methodology_extra_path_news") or "").strip() or None),
        methodology_extra_path_macro=(str(cfg.get("methodology_extra_path_macro") or "").strip() or None),
        finskills_root=(str(cfg.get("finskills_root") or "").strip() or None),
        stock_analysis_team_root=(str(cfg.get("stock_analysis_team_root") or "").strip() or None),
    )


def _request_login_code_sync(email: str) -> Dict[str, str]:
    with get_db_ctx() as db:
        code = auth_service.upsert_login_code(db, email)
    # DB session 已释放，SMTP 不会阻塞连接池
    dev_code = auth_service.send_login_code(email, code)
    response = {"message": "验证码已发送"}
    if dev_code:
        response["dev_code"] = dev_code
    return response


@app.post("/v1/auth/request-code")
async def request_login_code(request: AuthRequestCodeRequest):
    email = auth_service.normalize_email(request.email)
    if not re.match(r"^[^@\s]+@[^@\s.]+\.[^@\s.]+$", email):
        raise HTTPException(status_code=400, detail="邮箱格式不正确")
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(_light_executor, _request_login_code_sync, email),
            timeout=15,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="发送验证码超时，请稍后重试")


@app.post("/v1/auth/verify-code", response_model=AuthVerifyCodeResponse)
def verify_login_code(body: AuthVerifyCodeRequest, request: Request, db: Session = Depends(get_db)):
    user = auth_service.verify_login_code(db, body.email, body.code, client_ip=_get_real_ip(request))
    if not user:
        raise HTTPException(status_code=400, detail="验证码错误或已过期")
    access_token = auth_service.create_access_token(user)
    return AuthVerifyCodeResponse(access_token=access_token, user=user)


@app.get("/v1/auth/me", response_model=UserResponse)
def get_me(current_user: UserDB = Depends(_require_web_user)):
    return current_user


@app.get("/v1/config", response_model=UserRuntimeConfigResponse)
def get_runtime_config(
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(_require_web_user),
):
    """获取当前用户运行时配置。"""
    return _config_response_for_user(current_user, db)


@app.get("/v1/model-profiles", response_model=ModelProfileListResponse)
def list_model_profiles(
    include_inactive: bool = Query(False, description="是否包含停用配置"),
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(_require_web_user),
):
    profiles = model_profile_service.list_model_profiles(
        db,
        current_user.id,
        include_inactive=include_inactive,
    )
    return {"profiles": profiles}


@app.post("/v1/model-profiles", response_model=ModelProfileResponse)
def create_model_profile(
    request: ModelProfileCreateRequest,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(_require_web_user),
):
    try:
        return model_profile_service.create_model_profile(
            db,
            user_id=current_user.id,
            name=request.name,
            description=request.description,
            llm_provider=request.llm_provider,
            backend_url=request.backend_url,
            quick_think_llm=request.quick_think_llm,
            deep_think_llm=request.deep_think_llm,
            api_key=request.api_key,
            is_default=request.is_default,
            is_active=request.is_active,
            tags=request.tags,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/v1/model-profiles/{profile_id}", response_model=ModelProfileResponse)
def update_model_profile(
    profile_id: str,
    request: ModelProfileUpdateRequest,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(_require_web_user),
):
    try:
        return model_profile_service.update_model_profile(
            db,
            user_id=current_user.id,
            profile_id=profile_id,
            name=request.name,
            description=request.description,
            llm_provider=request.llm_provider,
            backend_url=request.backend_url,
            quick_think_llm=request.quick_think_llm,
            deep_think_llm=request.deep_think_llm,
            api_key=request.api_key,
            clear_api_key=request.clear_api_key,
            is_default=request.is_default,
            is_active=request.is_active,
            tags=request.tags,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/v1/model-profiles/{profile_id}")
def delete_model_profile(
    profile_id: str,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(_require_web_user),
):
    ok = model_profile_service.delete_model_profile(
        db,
        user_id=current_user.id,
        profile_id=profile_id,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="模型配置不存在")
    return {"message": "模型配置已删除", "id": profile_id}


@app.post("/v1/model-profiles/{profile_id}/warmup", response_model=ModelProfileWarmupResponse)
def warmup_model_profile(
    profile_id: str,
    request: ModelProfileWarmupRequest,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(_require_web_user),
):
    prompt = (request.prompt or "").strip() or "你好"
    try:
        overrides = model_profile_service.resolve_runtime_overrides(
            db,
            user_id=current_user.id,
            profile_id=profile_id,
            include_inactive=True,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    runtime_config = _build_runtime_config(
        overrides,
        user_id=current_user.id,
        db=db,
        trusted_overrides=True,
        strategy="profile_selected",
    )
    try:
        results = _invoke_runtime_warmup(runtime_config, prompt, current_user.id)
        has_error = any((item or {}).get("error") for item in results)
        profile = model_profile_service.update_probe_status(
            db,
            user_id=current_user.id,
            profile_id=profile_id,
            status="failed" if has_error else "ok",
            error=None if not has_error else "warmup returned error",
        )
        return {
            "profile_id": profile_id,
            "prompt": prompt,
            "results": results,
            "profile": profile,
        }
    except HTTPException as exc:
        model_profile_service.update_probe_status(
            db,
            user_id=current_user.id,
            profile_id=profile_id,
            status="failed",
            error=str(exc.detail),
        )
        raise
    except Exception as exc:
        model_profile_service.update_probe_status(
            db,
            user_id=current_user.id,
            profile_id=profile_id,
            status="failed",
            error=str(exc),
        )
        raise HTTPException(status_code=400, detail=f"模型 warmup 失败：{exc}") from exc


@app.patch("/v1/config")
def update_runtime_config(
    updates: UserRuntimeConfigUpdateRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(_require_web_user),
):
    """更新当前用户运行时配置，下次分析时生效。"""
    normalized_wecom_webhook = None
    if updates.wecom_webhook_url:
        from api.services.wecom_notification_service import normalize_webhook_url

        try:
            normalized_wecom_webhook = normalize_webhook_url(updates.wecom_webhook_url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    normalized_wps_webhook = None
    if updates.wps_webhook_url:
        from api.services.wps_notification_service import normalize_wps_webhook_url

        try:
            normalized_wps_webhook = normalize_wps_webhook_url(updates.wps_webhook_url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    persistent_user = db.query(UserDB).filter(UserDB.id == current_user.id).first() or current_user
    before_cfg = _config_response_for_user(persistent_user, db)
    pending_cfg = _build_pending_runtime_config(updates, persistent_user.id, db)
    if _should_probe_runtime_config(before_cfg, pending_cfg, updates):
        probe = _probe_runtime_config(pending_cfg)
        _log(
            f"[LLM Probe] user={persistent_user.id} provider={pending_cfg.get('llm_provider')} "
            f"model={probe.get('model', '')} status={probe.get('status')}"
        )
    row = auth_service.upsert_user_llm_config(
        db,
        persistent_user.id,
        llm_provider=updates.llm_provider,
        deep_think_llm=updates.deep_think_llm,
        quick_think_llm=updates.quick_think_llm,
        backend_url=updates.backend_url,
        max_debate_rounds=updates.max_debate_rounds,
        max_risk_discuss_rounds=updates.max_risk_discuss_rounds,
        decision_critic_enabled=updates.decision_critic_enabled,
        decision_critic_revision_threshold=updates.decision_critic_revision_threshold,
        api_key=updates.api_key,
        wecom_webhook_url=normalized_wecom_webhook,
        wps_webhook_url=normalized_wps_webhook,
        clear_api_key=updates.clear_api_key,
        clear_wecom_webhook=updates.clear_wecom_webhook,
        clear_wps_webhook=updates.clear_wps_webhook,
    )
    user_pref_updated = False
    if updates.email_report_enabled is not None:
        persistent_user.email_report_enabled = updates.email_report_enabled
        user_pref_updated = True
    if updates.wecom_report_enabled is not None:
        persistent_user.wecom_report_enabled = updates.wecom_report_enabled
        user_pref_updated = True
    if updates.wps_report_enabled is not None:
        persistent_user.wps_report_enabled = updates.wps_report_enabled
        user_pref_updated = True
    if user_pref_updated:
        db.commit()
    current_cfg = _config_response_for_user(persistent_user, db)
    warmup_models = _warmup_model_names(current_cfg.model_dump())
    should_warmup = _should_trigger_config_warmup(before_cfg, current_cfg, updates)
    warmup_payload: Dict[str, Any]
    if should_warmup and warmup_models:
        warmup_payload = {
            "requested": True,
            "triggered": True,
            "status": "scheduled",
            "models": warmup_models,
            "message": f"模型配置已保存，后台正在预热 {len(warmup_models)} 个模型。",
        }
        background_tasks.add_task(
            _run_config_warmup,
            _build_runtime_config({}, user_id=persistent_user.id, db=db),
            persistent_user.id,
        )
    elif updates.warmup:
        warmup_payload = {
            "requested": True,
            "triggered": False,
            "status": "skipped",
            "models": warmup_models,
            "message": "模型配置已保存，本次未触发 warmup。",
        }
    else:
        warmup_payload = {
            "requested": False,
            "triggered": False,
            "status": "disabled",
            "models": [],
            "message": "模型配置已保存。",
        }
    filtered = {
        k: v
        for k, v in updates.model_dump().items()
        if v is not None
        and k not in {"api_key", "wecom_webhook_url", "wps_webhook_url", "warmup", "force_warmup"}
        and (
            k in _CONFIG_ALLOWED_KEYS
            or k in _CONFIG_PREFERENCE_KEYS
            or (k in {"clear_api_key", "clear_wecom_webhook", "clear_wps_webhook"} and bool(v))
        )
    }
    return {
        "message": "用户配置已更新",
        "applied": filtered,
        "has_api_key": bool(row.api_key_encrypted),
        "current": current_cfg,
        "warmup": warmup_payload,
    }


@app.post("/v1/config/warmup", response_model=UserRuntimeWarmupResponse)
def warmup_runtime_config(
    request: UserRuntimeWarmupRequest,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(_require_web_user),
):
    pending_cfg = _build_pending_runtime_config(request, current_user.id, db)
    prompt = (request.prompt or "").strip() or "你好"
    results = _invoke_runtime_warmup(pending_cfg, prompt, current_user.id)
    return {
        "prompt": prompt,
        "results": results,
    }


@app.post("/v1/config/wecom/warmup", response_model=WecomWebhookWarmupResponse)
async def warmup_wecom_webhook(
    request: WecomWebhookWarmupRequest,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(_require_web_user),
):
    from api.services.wecom_notification_service import build_test_message, normalize_webhook_url, send_message

    webhook_url = (request.wecom_webhook_url or "").strip()
    if not webhook_url:
        user_cfg = auth_service.get_user_llm_config(db, current_user.id)
        webhook_url = auth_service.decrypt_secret(getattr(user_cfg, "wecom_webhook_encrypted", None)) or ""
    if not webhook_url:
        raise HTTPException(status_code=400, detail="请先填写或保存企业微信 Webhook")
    try:
        webhook_url = normalize_webhook_url(webhook_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        sent = await asyncio.to_thread(send_message, build_test_message(request.content), webhook_url)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Webhook 测试发送失败：{exc}") from exc
    if not sent:
        raise HTTPException(status_code=400, detail="Webhook 测试发送失败，请检查地址或机器人状态")

    return {
        "sent": True,
        "message": "Webhook 测试发送成功",
        "webhook_display": _mask_wecom_webhook(webhook_url),
    }


@app.post("/v1/config/wps/warmup", response_model=WpsWebhookWarmupResponse)
async def warmup_wps_webhook(
    request: WpsWebhookWarmupRequest,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(_require_web_user),
):
    from api.services.wps_notification_service import (
        build_test_markdown,
        normalize_wps_webhook_url,
        send_markdown_message,
    )

    webhook_url = (request.wps_webhook_url or "").strip()
    if not webhook_url:
        user_cfg = auth_service.get_user_llm_config(db, current_user.id)
        webhook_url = auth_service.decrypt_secret(getattr(user_cfg, "wps_webhook_encrypted", None)) or ""
    if not webhook_url:
        raise HTTPException(status_code=400, detail="请先填写或保存 WPS 协作 Webhook")
    try:
        webhook_url = normalize_wps_webhook_url(webhook_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        sent = await asyncio.to_thread(
            send_markdown_message, build_test_markdown(request.content), webhook_url
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Webhook 测试发送失败：{exc}") from exc
    if not sent:
        raise HTTPException(status_code=400, detail="Webhook 测试发送失败，请检查地址或机器人状态")

    return {
        "sent": True,
        "message": "WPS 协作 Webhook 测试发送成功",
        "webhook_display": _mask_wps_webhook(webhook_url),
    }


# ── Stock Search ──────────────────────────────────────────────────────────────

@app.get("/v1/market/stock-search")
def search_stocks(
    q: str = Query("", min_length=1, max_length=20),
    current_user: UserDB = Depends(_require_api_user),
):
    """Search stocks by code prefix or name substring."""
    q = q.strip()
    if not q:
        return {"results": []}

    name_to_code = _get_stock_name_map_cached_only()
    code_to_name = _get_reverse_stock_map()
    results = []
    q_upper = q.upper()

    for code, name in code_to_name.items():
        if code.upper().startswith(q_upper) or code.split(".")[0].startswith(q):
            results.append({"symbol": code, "name": name})
            if len(results) >= 20:
                break

    if len(results) < 20:
        for name, code in name_to_code.items():
            if q in name and not any(r["symbol"] == code for r in results):
                results.append({"symbol": code, "name": name})
                if len(results) >= 20:
                    break

    return {"results": results}


def _annotate_scheduled_with_imported_context(items: List[dict], db: Session, user_id: str) -> List[dict]:
    imported_map: Dict[str, Dict[str, Any]] = {}
    for item in portfolio_import_service.list_imported_positions(db, user_id):
        imported_map[item["symbol"]] = item
    for item in items:
        imported = imported_map.get(item["symbol"])
        item["has_imported_context"] = imported is not None
        item["imported_current_position"] = imported.get("current_position") if imported else None
        item["imported_average_cost"] = imported.get("average_cost") if imported else None
        item["imported_trade_points_count"] = imported.get("trade_points_count") if imported else 0
    return items


def _attach_stock_names(items: List[dict], code_to_name: Dict[str, str]) -> List[dict]:
    for item in items:
        symbol = str(item.get("symbol") or "").upper()
        item["name"] = code_to_name.get(symbol, symbol or item.get("name") or "")
    return items


@app.get("/v1/portfolio/imports")
def get_portfolio_import_state(
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    return portfolio_import_service.get_import_state(db, current_user.id)


@app.get("/v1/trade-plans")
def list_trade_plans(
    symbol: Optional[str] = None,
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    """列出当前用户可监控的交易计划（入场区间 / 硬止损 / 分批止盈 / 时间止损）。"""
    rows = trade_plan_service.list_active_plans(db, user_id=current_user.id)
    if symbol:
        rows = [r for r in rows if (r.symbol or "").upper() == symbol.strip().upper()]
    plans = []
    for row in rows:
        plan = trade_plan_service.plan_dict_from_row(row)
        if not plan:
            continue
        plan["monitorable"] = trade_plan_service.is_monitorable(plan)
        plans.append(plan)
    return {"total": len(plans), "plans": plans}


@app.get("/v1/trade-plans/{symbol}")
def get_trade_plan(
    symbol: str,
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    """取某标的当前生效的交易计划——即"何时卖、卖多少"的书面依据。"""
    row = trade_plan_service.get_monitor_plan(db, user_id=current_user.id, symbol=symbol)
    if row is None:
        raise HTTPException(status_code=404, detail="该标的暂无可用交易计划，请先生成深度分析")
    plan = trade_plan_service.plan_dict_from_row(row) or {}
    plan["monitorable"] = trade_plan_service.is_monitorable(plan)
    return {"plan": plan}


@app.get("/v1/exit-advice")
def get_exit_advice(
    symbol: Optional[str] = None,
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    """按每份研报自己的交易计划，给出持有/减仓/清仓的纪律化提示。

    与虚拟盘的 -6%/+10% 硬编码无关：用的是该标的自己那份计划的止损/止盈/时间锚点，
    并已考虑 T+1 可卖数量、涨跌停不可成交与停牌。仅为纪律提示，不构成投资建议。
    """
    from api.services import exit_engine_service

    decisions = exit_engine_service.evaluate_portfolio(db, user_id=current_user.id)
    if symbol:
        target = symbol.strip().upper()
        decisions = [d for d in decisions if (d.symbol or "").upper() == target]
    return {
        "summary": exit_engine_service.summarize_decisions(decisions),
        "decisions": [d.to_dict() for d in decisions],
    }


def _ledger_trade_dict(row: Any) -> Dict[str, Any]:
    """台账行 JSON 化：直接按表列取值，避免再加一份易漂移的字段清单。"""
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


@app.get("/v1/trade-ledger")
def list_trade_ledger(
    symbol: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    """真实成交台账 + 卖出原因归因。

    产品过去只记录虚拟盘成交（生产库 12 条全是同一笔 BUY，**没有一条 SELL**），
    真实成交无台账，因此"卖出决策到底赚了还是亏了"这个问题无法回答。
    """
    rows = trade_ledger_service.list_trades(
        db,
        user_id=current_user.id,
        symbol=symbol,
        start_date=start_date,
        end_date=end_date,
    )
    return {
        "total": len(rows),
        "trades": [_ledger_trade_dict(row) for row in rows],
        "attribution": trade_ledger_service.attribution_by_sell_reason(
            db, user_id=current_user.id, start_date=start_date, end_date=end_date
        ),
        "summary": trade_ledger_service.attribution_summary(db, user_id=current_user.id),
    }


@app.post("/v1/trade-ledger")
def record_trade_ledger(
    body: TradeLedgerRecordRequest,
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    """记录一笔真实成交。同一笔重复提交会被幂等拦下，不会重复计入盈亏。"""
    row, meta = trade_ledger_service.record_trade(
        db,
        user_id=current_user.id,
        symbol=body.symbol,
        action=body.action,
        price=body.price,
        shares=body.shares,
        trade_date=body.trade_date,
        name=body.name,
        sell_reason=body.sell_reason,
        sell_reason_note=body.sell_reason_note,
        report_id=body.report_id,
        plan_id=body.plan_id,
        note=body.note,
    )
    db.commit()
    return {
        "trade": _ledger_trade_dict(row) if row is not None else None,
        "meta": meta,
        "position": trade_ledger_service.rebuild_position_state(
            db, user_id=current_user.id, symbol=body.symbol
        ),
    }


@app.get("/v1/trade-ledger/attribution")
def get_trade_attribution(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    """按卖出原因归因：哪一类卖出决策真正赚钱，哪一类一直在亏。"""
    return {
        "by_reason": trade_ledger_service.attribution_by_sell_reason(
            db, user_id=current_user.id, start_date=start_date, end_date=end_date
        ),
        "summary": trade_ledger_service.attribution_summary(db, user_id=current_user.id),
    }


@app.get("/v1/trade-ledger/positions/{symbol}")
def get_ledger_position(
    symbol: str,
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    """由台账重放的持仓与可卖数量（不依赖静态快照）。"""
    return trade_ledger_service.rebuild_position_state(
        db, user_id=current_user.id, symbol=symbol
    )


@app.post("/v1/trade-ledger/sync")
def sync_trade_ledger(
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    """用台账重放对账导入的持仓快照，暴露两者不一致之处。"""
    return trade_ledger_service.sync_from_imported_positions(db, user_id=current_user.id)


@app.post("/v1/trade-ledger/import")
def import_trade_ledger_points(
    body: TradeLedgerImportRequest,
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    """批量导入买卖点。重复导入是幂等的。"""
    try:
        return trade_ledger_service.import_trade_points(
            db,
            user_id=current_user.id,
            symbol=body.symbol,
            trade_points=body.trade_points,
            name=body.name,
            source=body.source,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/v1/portfolio/imports")
def sync_portfolio_import(
    body: PortfolioImportSyncRequest,
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    try:
        return portfolio_import_service.sync_positions(
            db=db,
            user_id=current_user.id,
            positions=[p.model_dump() for p in body.positions],
            source=body.source,
            auto_apply_scheduled=body.auto_apply_scheduled,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/v1/portfolio/imports/merge")
def merge_portfolio_import(
    body: PortfolioImportSyncRequest,
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    """合并写入持仓：在指定 source 上追加新标的或更新已有标的，不删除未出现在请求中的其他标的。"""
    try:
        return portfolio_import_service.merge_imported_positions(
            db=db,
            user_id=current_user.id,
            positions=[p.model_dump(exclude_unset=True) for p in body.positions],
            source=body.source,
            auto_apply_scheduled=body.auto_apply_scheduled,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.delete("/v1/portfolio/imports", status_code=204)
def clear_portfolio_import_state(
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    portfolio_import_service.clear_imported_portfolio(db, current_user.id)


@app.delete("/v1/portfolio/imports/position")
def delete_portfolio_import_position(
    symbol: str = Query(..., description="标的代码，如 600519.SH、000001.SZ"),
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    try:
        return portfolio_import_service.delete_imported_positions_for_symbol(db, current_user.id, symbol)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@app.post("/v1/portfolio/parse-image")
async def parse_position_image_endpoint(
    file: UploadFile = File(...),
    current_user: UserDB = Depends(_require_api_user),
):
    """Parse a broker position screenshot using server-side VLM."""
    from api.services.vlm_position_parser import parse_position_image

    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(400, "只支持图片文件")

    image_bytes = await file.read()
    if len(image_bytes) > 10 * 1024 * 1024:
        raise HTTPException(400, "图片不能超过 10MB")

    try:
        positions = await asyncio.to_thread(parse_position_image, image_bytes, file.content_type)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        logger.warning("[parse-image] VLM parsing failed: %s", exc)
        raise HTTPException(500, "图片解析失败，请稍后重试") from exc

    return {"positions": positions}


@app.get("/v1/paper-portfolio")
def get_paper_portfolio(
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    return paper_trading_service.get_portfolio_snapshot(db, current_user.id)


@app.post("/v1/paper-portfolio/bootstrap")
def bootstrap_paper_portfolio(
    body: PaperPortfolioBootstrapRequest,
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    try:
        return paper_trading_service.bootstrap_from_imported_positions(
            db,
            user_id=current_user.id,
            initial_cash=body.initial_cash,
            source=body.source,
            reset_existing=body.reset_existing,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/v1/paper-trades")
def create_paper_trade(
    body: PaperTradeRequest,
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    try:
        return paper_trading_service.execute_trade(
            db,
            user_id=current_user.id,
            symbol=body.symbol,
            security_name=body.name,
            side=body.side,
            quantity=body.quantity,
            price=body.price,
            fee_rate=body.fee_rate,
            reason=body.reason,
            trade_date=body.trade_date,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/v1/paper-portfolio/daily-ops")
def run_daily_operation(
    body: DailyOperationRequest,
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    return _execute_daily_operation_pipeline(
        db=db,
        user_id=current_user.id,
        trade_date=body.trade_date,
        include_recommendations=body.include_recommendations,
        recommendation_top_k=body.recommendation_top_k,
        auto_execute=body.auto_execute,
    )


@app.get("/v1/paper-portfolio/daily-reviews")
def list_daily_reviews(
    limit: int = Query(20, ge=1, le=100),
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(DailyReviewDB)
        .filter(DailyReviewDB.user_id == current_user.id)
        .order_by(DailyReviewDB.trade_date.desc())
        .limit(limit)
        .all()
    )
    return {
        "items": [
            {
                "trade_date": row.trade_date,
                "summary": row.summary_json or {},
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
            for row in rows
        ]
    }


@app.get("/v1/paper-portfolio/daily-reviews/{trade_date}")
def get_daily_review(
    trade_date: str,
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    row = (
        db.query(DailyReviewDB)
        .filter(DailyReviewDB.user_id == current_user.id, DailyReviewDB.trade_date == trade_date)
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="未找到该日期复盘")
    return {"trade_date": row.trade_date, "summary": row.summary_json or {}}


@app.get("/v1/dashboard/tracking-board")
def get_dashboard_tracking_board(
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    return tracking_board_service.get_tracking_board(db, current_user.id)


# ── Watchlist ─────────────────────────────────────────────────────────────────

@app.get("/v1/watchlist")
def list_watchlist(
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    items = watchlist_service.list_watchlist(db, current_user.id)
    _attach_stock_names(items, _get_reverse_stock_map())
    return {"items": items}


@app.post("/v1/watchlist")
def add_to_watchlist(
    body: WatchlistAddRequest,
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    text = str(body.text or body.symbol or "").strip()
    if not text:
        raise HTTPException(400, "text or symbol is required")

    tokens = _split_watchlist_batch_text(text)
    if not tokens:
        raise HTTPException(400, "至少提供一个股票代码或名称")

    name_to_code = _get_stock_name_map_cached_only()
    code_to_name = _get_reverse_stock_map()

    resolved_entries: List[Dict[str, Any]] = []
    results: List[Dict[str, Any]] = []
    for idx, token in enumerate(tokens):
        symbol, name, error = _resolve_watchlist_identifier(token, name_to_code, code_to_name)
        if error:
            results.append({
                "_order": idx,
                "input": token,
                "status": "invalid",
                "message": error,
            })
            continue
        resolved_entries.append({
            "_order": idx,
            "input": token,
            "symbol": symbol,
            "name": name,
        })

    add_results = watchlist_service.add_watchlist_items(
        db,
        current_user.id,
        [entry["symbol"] for entry in resolved_entries],
    )
    for entry, result in zip(resolved_entries, add_results):
        item = result.get("item")
        if item:
            item["name"] = entry["name"]
            item["has_scheduled"] = False
        results.append({
            "_order": entry["_order"],
            "input": entry["input"],
            "symbol": entry["symbol"],
            "name": entry["name"],
            "status": result["status"],
            "message": result["message"],
            "item": item,
        })

    results.sort(key=lambda row: row["_order"])
    for row in results:
        row.pop("_order", None)
    summary = {
        "total": len(tokens),
        "added": sum(1 for row in results if row["status"] == "added"),
        "duplicate": sum(1 for row in results if row["status"] == "duplicate"),
        "failed": sum(1 for row in results if row["status"] in {"invalid", "failed"}),
    }
    message_parts = [f"共处理 {summary['total']} 项"]
    if summary["added"]:
        message_parts.append(f"新增 {summary['added']} 项")
    if summary["duplicate"]:
        message_parts.append(f"重复 {summary['duplicate']} 项")
    if summary["failed"]:
        message_parts.append(f"失败 {summary['failed']} 项")
    return {
        "message": "，".join(message_parts),
        "summary": summary,
        "results": results,
    }


@app.post("/v1/watchlist/batch/delete")
def batch_delete_from_watchlist(
    body: WatchlistBatchIdsRequest,
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    try:
        return watchlist_service.batch_delete_watchlist_items(db, current_user.id, body.item_ids)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/v1/watchlist/{item_id}", status_code=204)
def delete_from_watchlist(
    item_id: str,
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    if not watchlist_service.delete_watchlist_item(db, current_user.id, item_id):
        raise HTTPException(404, "未找到该自选股")


# ── Scheduled Analysis ────────────────────────────────────────────────────────

@app.get("/v1/scheduled")
def list_scheduled_analyses(
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    items = scheduled_service.list_scheduled(db, current_user.id)
    _attach_stock_names(items, _get_reverse_stock_map_cached_only())
    return {"items": _annotate_scheduled_with_imported_context(items, db, current_user.id)}


@app.get("/v1/portfolio/overview", response_model=PortfolioOverviewResponse)
def get_portfolio_overview(
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    code_to_name = _get_reverse_stock_map_cached_only()

    watchlist_items = watchlist_service.list_watchlist(db, current_user.id)
    _attach_stock_names(watchlist_items, code_to_name)

    scheduled_items = scheduled_service.list_scheduled(db, current_user.id)
    _attach_stock_names(scheduled_items, code_to_name)
    scheduled_items = _annotate_scheduled_with_imported_context(scheduled_items, db, current_user.id)

    latest_reports = report_service.get_latest_reports_by_symbols(
        db=db,
        user_id=current_user.id,
        symbols=[item["symbol"] for item in watchlist_items],
    )
    for report in latest_reports:
        report.name = code_to_name.get(report.symbol, report.symbol)

    portfolio_import = portfolio_import_service.get_import_state(db, current_user.id)

    return {
        "watchlist": watchlist_items,
        "scheduled": scheduled_items,
        "latest_reports": latest_reports,
        "portfolio_import": portfolio_import,
    }


@app.post("/v1/scheduled", status_code=201)
def create_scheduled_analysis(
    body: dict,
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    symbol = body.get("symbol", "").strip().upper()
    horizon = body.get("horizon", "short")
    trigger_time = body.get("trigger_time", "20:00")
    prompt_template_id = body.get("prompt_template_id")
    prompt_vars = body.get("prompt_vars") or {}
    if not symbol:
        raise HTTPException(400, "symbol is required")
    code_to_name = _get_reverse_stock_map()
    if symbol not in code_to_name:
        raise HTTPException(400, f"未知的股票代码: {symbol}")
    try:
        item = scheduled_service.create_scheduled(
            db,
            current_user.id,
            symbol,
            horizon,
            trigger_time,
            prompt_template_id=prompt_template_id,
            prompt_vars=prompt_vars,
        )
        item["name"] = code_to_name.get(symbol, symbol)
        _annotate_scheduled_with_imported_context([item], db, current_user.id)
        return item
    except ValueError as e:
        raise HTTPException(400, str(e))


def _extract_scheduled_update_kwargs(body: dict) -> dict:
    kwargs = {}
    if "is_active" in body:
        kwargs["is_active"] = bool(body["is_active"])
    if "horizon" in body:
        kwargs["horizon"] = body["horizon"]
    if "trigger_time" in body:
        kwargs["trigger_time"] = body["trigger_time"]
    if "prompt_template_id" in body:
        kwargs["prompt_template_id"] = body["prompt_template_id"]
    if "prompt_vars" in body:
        kwargs["prompt_vars"] = body["prompt_vars"] or {}
    return kwargs


@app.patch("/v1/scheduled/batch")
def batch_update_scheduled_analyses(
    body: ScheduledBatchUpdateRequest,
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    kwargs = _extract_scheduled_update_kwargs(body.model_dump(exclude_unset=True))
    if not kwargs:
        raise HTTPException(400, "至少提供一个更新字段")
    try:
        items = scheduled_service.batch_update_scheduled(
            db,
            current_user.id,
            body.item_ids,
            **kwargs,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    code_to_name = _get_reverse_stock_map()
    for item in items:
        item["name"] = code_to_name.get(item["symbol"], item["symbol"])
    return {"items": _annotate_scheduled_with_imported_context(items, db, current_user.id)}


@app.post("/v1/scheduled/batch/delete")
def batch_delete_scheduled_analyses(
    body: ScheduledBatchIdsRequest,
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    try:
        return scheduled_service.batch_delete_scheduled(db, current_user.id, body.item_ids)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/v1/scheduled/batch/ensure")
def batch_ensure_scheduled_analyses(
    body: ScheduledEnsureBatchRequest,
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    symbols = []
    seen: set[str] = set()
    for raw in body.symbols:
        sym = _normalize_symbol(str(raw or "").strip().upper())
        if not sym or sym in seen:
            continue
        seen.add(sym)
        symbols.append(sym)
    if not symbols:
        raise HTTPException(400, "请至少提供一个股票代码")

    code_to_name = _get_reverse_stock_map()
    invalid = [sym for sym in symbols if sym not in code_to_name]
    if invalid:
        raise HTTPException(400, f"存在未知股票代码: {', '.join(invalid[:5])}")

    try:
        result = scheduled_service.ensure_scheduled_for_symbols(
            db,
            current_user.id,
            symbols=symbols,
            horizon=body.horizon,
            trigger_time=body.trigger_time,
            prompt_template_id=body.prompt_template_id,
            prompt_vars=body.prompt_vars,
        )
        db.commit()
    except ValueError as e:
        raise HTTPException(400, str(e))

    return result


@app.post("/v1/scheduled/batch/trigger", response_model=BatchScheduledTriggerResponse)
async def trigger_scheduled_analyses_batch(
    body: ScheduledBatchIdsRequest,
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    if not body.item_ids:
        raise HTTPException(400, "请至少选择 1 个定时任务")

    requested_trade_date = cn_today_str()
    actual_trade_date = _resolve_scheduled_trade_date(requested_trade_date)
    code_to_name = _get_reverse_stock_map()
    jobs: List[Dict[str, Any]] = []
    available_tasks = {
        task["id"]: task
        for task in scheduled_service.list_scheduled(db, current_user.id)
    }
    valid_item_ids = []
    missing_item_ids = []
    for raw_item_id in body.item_ids:
        item_id = str(raw_item_id or "").strip()
        if not item_id:
            continue
        if item_id in available_tasks:
            valid_item_ids.append(item_id)
        else:
            missing_item_ids.append(item_id)

    if not valid_item_ids:
        raise HTTPException(400, "选中的定时任务已失效，请刷新页面后重试")

    if missing_item_ids:
        _log(
            f"[Scheduled Batch Trigger] user={current_user.id} skipped missing item_ids={missing_item_ids}"
        )

    for item_id in valid_item_ids:
        task = available_tasks[item_id]

        task_snapshot = dict(task)
        task_snapshot["user_id"] = current_user.id
        # 不注入导入持仓：深度分析保持客观，减仓/卖出交给独立退出引擎。
        task_snapshot["manual_user_context"] = {}

        now = _utcnow_iso()
        job_id = uuid4().hex
        _set_job(
            job_id,
            job_id=job_id,
            status="pending",
            created_at=now,
            symbol=task["symbol"],
            trade_date=actual_trade_date,
            user_id=current_user.id,
            request_source="scheduled_manual_batch",
        )
        _ensure_job_event_queue(job_id)
        _emit_job_event(
            job_id,
            "job.queued",
            {"job_id": job_id, "symbol": task["symbol"], "trade_date": actual_trade_date},
        )
        _create_tracked_task(
            _run_scheduled_analysis_once(
                task_snapshot,
                requested_trade_date,
                job_id,
                mark_schedule_run=False,
            )
        )

        jobs.append({
            "item_id": task["id"],
            "job_id": job_id,
            "symbol": task["symbol"],
            "name": code_to_name.get(task["symbol"], task["symbol"]),
            "status": "pending",
            "created_at": now,
            "waiting_ahead_count": _get_job(job_id).get("waiting_ahead_count"),
            "scheduled_running_count": _get_job(job_id).get("scheduled_running_count"),
            "scheduled_concurrency_limit": _get_job(job_id).get("scheduled_concurrency_limit"),
        })

    return {
        "summary": {
            "total": len(jobs),
        },
        "jobs": jobs,
    }


@app.post("/v1/scheduled/{item_id}/trigger", response_model=AnalyzeResponse)
async def trigger_scheduled_analysis_once(
    item_id: str,
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    task = scheduled_service.get_scheduled(db, current_user.id, item_id)
    if task is None:
        raise HTTPException(404, "未找到该定时任务")

    requested_trade_date = cn_today_str()
    actual_trade_date = _resolve_scheduled_trade_date(requested_trade_date)
    now = _utcnow_iso()
    job_id = uuid4().hex

    task_snapshot = dict(task)
    task_snapshot["user_id"] = current_user.id
    # 不注入导入持仓：深度分析保持客观，减仓/卖出交给独立退出引擎。
    task_snapshot["manual_user_context"] = {}

    _set_job(
        job_id,
        job_id=job_id,
        status="pending",
        created_at=now,
        symbol=task["symbol"],
        trade_date=actual_trade_date,
        user_id=current_user.id,
        request_source="scheduled_manual",
    )
    _ensure_job_event_queue(job_id)
    _emit_job_event(
        job_id,
        "job.queued",
        {"job_id": job_id, "symbol": task["symbol"], "trade_date": actual_trade_date},
    )
    _create_tracked_task(
        _run_scheduled_analysis_once(
            task_snapshot,
            requested_trade_date,
            job_id,
            mark_schedule_run=False,
        )
    )
    return AnalyzeResponse(job_id=job_id, status="pending", created_at=now)


@app.patch("/v1/scheduled/{item_id}")
def update_scheduled_analysis(
    item_id: str,
    body: dict,
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    kwargs = _extract_scheduled_update_kwargs(body)
    try:
        result = scheduled_service.update_scheduled(db, current_user.id, item_id, **kwargs)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if result is None:
        raise HTTPException(404, "未找到该定时任务")
    code_to_name = _get_reverse_stock_map()
    result["name"] = code_to_name.get(result["symbol"], result["symbol"])
    _annotate_scheduled_with_imported_context([result], db, current_user.id)
    return result


@app.delete("/v1/scheduled/{item_id}", status_code=204)
def delete_scheduled_analysis(
    item_id: str,
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    if not scheduled_service.delete_scheduled(db, current_user.id, item_id):
        raise HTTPException(404, "未找到该定时任务")


# ─── Feedback endpoints ─────────────────────────────────────────────────────


class FeedbackCreateRequest(BaseModel):
    subject: str = Field(..., min_length=1, max_length=200)
    content: str = Field(..., min_length=1, max_length=5000)


class FeedbackItem(BaseModel):
    id: str
    user_email: str
    subject: str
    content: str
    admin_reply: Optional[str] = None
    replied_at: Optional[datetime] = None
    is_read: bool = False
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @field_serializer("replied_at", "created_at", "updated_at")
    def serialize_dt(self, v: Optional[datetime], _info: Any) -> Optional[str]:
        return v.isoformat() if v else None


class FeedbackListResponse(BaseModel):
    total: int
    feedbacks: List[FeedbackItem]


class FeedbackUnreadResponse(BaseModel):
    unread_count: int


def _fb_to_item(fb: FeedbackDB) -> FeedbackItem:
    return FeedbackItem(
        id=fb.id,
        user_email=fb.user_email,
        subject=fb.subject,
        content=fb.content,
        admin_reply=fb.admin_reply,
        replied_at=fb.replied_at,
        is_read=fb.is_read,
        created_at=fb.created_at,
        updated_at=fb.updated_at,
    )


@app.post("/v1/feedbacks", response_model=FeedbackItem, status_code=201)
def create_feedback(
    req: FeedbackCreateRequest,
    current_user: UserDB = Depends(_require_web_user),
    db: Session = Depends(get_db),
):
    fb = feedback_service.create_feedback(db, current_user, req.subject, req.content)
    return _fb_to_item(fb)


@app.get("/v1/feedbacks", response_model=FeedbackListResponse)
def list_feedbacks(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: UserDB = Depends(_require_web_user),
    db: Session = Depends(get_db),
):
    items, total = feedback_service.list_feedbacks(db, current_user.id, page, page_size)
    return FeedbackListResponse(total=total, feedbacks=[_fb_to_item(fb) for fb in items])


@app.get("/v1/feedbacks/unread-count", response_model=FeedbackUnreadResponse)
def feedback_unread_count(
    current_user: UserDB = Depends(_require_web_user),
    db: Session = Depends(get_db),
):
    count = feedback_service.unread_count(db, current_user.id)
    return FeedbackUnreadResponse(unread_count=count)


@app.get("/v1/feedbacks/{feedback_id}", response_model=FeedbackItem)
def get_feedback(
    feedback_id: str,
    current_user: UserDB = Depends(_require_web_user),
    db: Session = Depends(get_db),
):
    fb = feedback_service.get_feedback(db, feedback_id)
    if not fb or fb.user_id != current_user.id:
        raise HTTPException(404, "未找到该反馈")
    # auto mark read
    if not fb.is_read and fb.admin_reply:
        feedback_service.mark_read(db, feedback_id, current_user.id)
        fb.is_read = True
    return _fb_to_item(fb)


@app.post("/v1/feedbacks/{feedback_id}/read")
def mark_feedback_read(
    feedback_id: str,
    current_user: UserDB = Depends(_require_web_user),
    db: Session = Depends(get_db),
):
    fb = feedback_service.mark_read(db, feedback_id, current_user.id)
    if not fb:
        raise HTTPException(404, "未找到该反馈")
    return {"ok": True}


from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

# ─── Static Files & SPA Routing ──────────────────────────────────────────────

# Mount frontend if dist exists（相对 api/main.py 定位仓库根，不依赖 os.getcwd()）
_REPO_ROOT = Path(__file__).resolve().parent.parent
dist_path = _REPO_ROOT / "frontend" / "dist"
if dist_path.is_dir():
    app.mount(
        "/assets",
        StaticFiles(directory=str(dist_path / "assets")),
        name="assets",
    )

    @app.get("/{full_path:path}")
    async def serve_frontend(full_path: str):
        # 1. Define and resolve the absolute safe root
        base_path = os.path.realpath(str(dist_path))
        
        # 2. Resolve the requested path (handling .. and symlinks)
        # We lstrip("/") to prevent os.path.join from treating it as an absolute path
        fullpath = os.path.realpath(os.path.join(base_path, full_path.lstrip("/")))
        
        # 3. Security Check: The normalized path must start with the base_path
        if not fullpath.startswith(base_path):
            return FileResponse(os.path.join(base_path, "index.html"))
            
        # 4. Final check: if it's a valid file, serve it
        if os.path.isfile(fullpath):
            return FileResponse(fullpath)
            
        # Otherwise fallback to index.html for SPA routing
        return FileResponse(os.path.join(base_path, "index.html"))


def run() -> None:
    import uvicorn
    from pathlib import Path

    log_config = str(Path(__file__).parent / "logging_config.yaml")
    uvicorn.run("api.main:app", host="0.0.0.0", port=8000, reload=False, log_config=log_config)
