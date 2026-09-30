"""数据源探测报告的对外端点。

## 为什么要有这一层

"数据源到底能不能用"此前**没有任何地方能回答**：链上的静默降级会把一个源
几个月不工作这件事藏得严严实实（Baostock 上游主机名迁移就是这么被埋掉的）。
这四个端点把探测结果变成可看、可回看、可手动触发的产品能力：

* `GET  /v1/data-sources/matrix`   —— **预期**：谁负责哪个维度、谁是主源/兜底
* `GET  /v1/data-sources/status`   —— **现状**：最新一轮探测的明细 + 最近若干轮汇总
* `GET  /v1/data-sources/history`  —— **趋势**：某个源/维度随时间的变化
* `POST /v1/data-sources/probe`    —— 手动触发一次探测

`matrix` 与 `status` 必须成对呈现：只看 `status` 无法知道"某个维度是不是本来就
没有源负责"，只看 `matrix` 无法知道"今天到底通不通"。

## 手动探测为什么要后台跑

一轮探测是 20+ 次**真实**网络请求（刻意串行，避免刺激免费源的限流/封禁），
十几秒到几十秒都正常。同步等会让前端 30s 超时先炸掉，于是这里：
起后台线程跑，最多同步等 `wait_seconds`（默认 20s）——
跑得快就是"点一下就看到结果"，跑得慢就返回 `running: true` 让页面轮询。
两种情况下返回体都是完整的 status 形状，前端不需要分支处理。
"""

from __future__ import annotations

import logging
import threading

from fastapi import APIRouter, Depends, HTTPException, Query

from api.database import get_db
from api.dependencies import current_user
from api.services import data_source_probe_service as probe

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/data-sources", tags=["data-sources"])

#: 同一时刻只允许一轮探测：并发探测既会让免费源更快封我们，
#: 也会让"这一轮到底测了什么"变得不可解释。
_probe_thread_lock = threading.Lock()
_probe_thread: threading.Thread | None = None


def _probe_running() -> bool:
    thread = _probe_thread
    return thread is not None and thread.is_alive()


def _run_probe_job(deadline_seconds: float) -> None:
    """后台探测任务。自己开 session —— 请求的 session 在响应后就关了。"""
    from api.database import SessionLocal

    db = SessionLocal()
    try:
        summary = probe.run_probe(
            db,
            trigger=probe.TRIGGER_MANUAL,
            deadline_seconds=deadline_seconds,
        )
        logger.info(
            "[data-sources] 手动探测完成 status=%s ok=%s degraded=%s failed=%s 用时=%sms",
            summary.status,
            summary.ok_count,
            summary.degraded_count,
            summary.failed_count,
            summary.duration_ms,
        )
    except Exception:  # 后台线程的异常必须自己吞掉并留痕，否则静默消失
        logger.exception("[data-sources] 手动探测失败")
    finally:
        db.close()


@router.get("/matrix")
def get_matrix(user=Depends(current_user)):
    """能力矩阵：**预期**的分工与优先级（不触发任何网络请求）。"""
    return probe.matrix_payload()


@router.get("/status")
def get_status(db=Depends(get_db), user=Depends(current_user)):
    """最新探测结果：明细 + 最近若干轮汇总。从未探测过时 `latest_run` 为 null。"""
    payload = probe.status_payload(db)
    # `running` 让页面知道"现在正有一轮在跑"，从而决定要不要轮询。
    payload["running"] = _probe_running()
    payload["enabled"] = probe.probe_enabled()
    payload["scheduled_at"] = probe.probe_at()
    return payload


@router.get("/history")
def get_history(
    days: int = Query(30, ge=1, le=365, description="回看天数"),
    source: str | None = Query(None, description="只看某个数据源，如 cn_baostock"),
    tool: str | None = Query(None, description="只看某个维度，如 get_daily_bars_df"),
    limit: int = Query(2000, ge=1, le=10000, description="返回点数上限"),
    db=Depends(get_db),
    user=Depends(current_user),
):
    """时间序列，供页面画趋势（"这个源是哪天开始坏的"）。"""
    rows = probe.history_points(db, days=days, source=source, tool=tool, limit=limit)
    return {"points": [probe.result_to_dict(r) for r in rows]}


@router.post("/probe")
def trigger_probe(
    deadline_seconds: float = Query(
        25.0, ge=1.0, le=120.0, description="单个目标的硬截止（秒）"
    ),
    wait_seconds: float = Query(
        20.0, ge=0.0, le=60.0, description="最多同步等多久，超时则转后台由前端轮询"
    ),
    db=Depends(get_db),
    user=Depends(current_user),
):
    """立即跑一轮探测。

    返回体与 `GET /status` 同形，额外带 `running`：`false` 表示这一轮已经跑完
    （返回的就是刚跑出来的结果），`true` 表示还在跑、请继续轮询 `/status`。
    """
    global _probe_thread

    with _probe_thread_lock:
        if _probe_running():
            raise HTTPException(status_code=409, detail="已有一轮探测正在进行中")
        _probe_thread = threading.Thread(
            target=_run_probe_job,
            args=(deadline_seconds,),
            name="data-source-probe",
            daemon=True,
        )
        _probe_thread.start()
        thread = _probe_thread

    thread.join(timeout=wait_seconds)

    payload = probe.status_payload(db)
    payload["running"] = thread.is_alive()
    payload["enabled"] = probe.probe_enabled()
    payload["scheduled_at"] = probe.probe_at()
    return payload
