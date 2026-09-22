"""对话端点：SSE 流式回答。

## 与既有 `/v1/chat/completions` 的关系

既有点**保持不动**（仍返回任务回执，兼容既有调用方）。新端点是它的补充，
不是替换：它把「用户提问」变成「有依据的回答」，而不是「已启动分析任务」。

## 为什么用后台线程 + 队列转发事件

Agent 循环是同步的（LLM 与工具都是阻塞调用），而 SSE 需要一个异步生成器。
用 `asyncio.to_thread` 起一个线程跑循环，事件经 `queue.Queue` 转发——
这与仓库既有的 `_stream_job_events` 是同一套做法，不引入新的并发模型。

## 取消语义

客户端断开 → 生成器被关闭 → 把取消标志交给循环，循环按 `cancelled` 记账，
**不落为 completed**。半途而废的回答被记成完成，会让「回答质量」的统计
从根上失真。
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
from dataclasses import asdict, is_dataclass
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from api.dependencies import current_user
from api.services import chat_service
from tradingagents.agent.events import EndEvent, ErrorEvent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["chat"])

# 追问文案必须给出**两条**可行动路径。旧文案只说「请输入代码或公司名称」，
# 于是「今天走到好的票，放量票找十个给我」被误报成「你没说清楚」——那是一个
# 合法的全市场筛选需求，归因错了比拒绝更糟。
CLARIFY_DETAIL = (
    "未能从消息中识别出单只股票标的。两种用法："
    "① 研判单只股票，请输入代码（如 600519.SH）或可识别的公司名称；"
    "② 全市场选股/筛选（如「今天放量票找十个给我」「帮我挑5只强势股」），"
    "直接用筛选措辞描述即可。"
)

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _serialize(event: Any) -> dict[str, Any]:
    """事件 → JSON。事件类型名即 SSE 的 event 字段。"""
    kind = type(event).__name__
    if is_dataclass(event):
        payload = asdict(event)
    else:
        # 曾经这里退化成 `{"value": str(event)}`，把一个非 dataclass 的事件
        # （`_CrashEvent`）序列化成 `<... object at 0x...>`：崩溃原因整条丢失，
        # 还泄漏服务端内存布局。宁可在服务端响亮地报错，也不给客户端一堆 repr。
        logger.error("non-dataclass event %s dropped from the stream payload", kind)
        payload = {}
    return {"event": kind, "data": payload}


@router.post("/chat")
async def chat(
    body: chat_service.ChatRequest,
    user=Depends(current_user),
):
    """对话：流式返回回答文本、工具轨迹与结构化判定。

    响应是 `text/event-stream`，事件类型包括：
    `AnswerDelta` / `AnswerDone` / `ToolStart` / `ToolDone` / `VerdictEvent` /
    `Notice` / `ErrorEvent` / `EndEvent`。
    """
    request = chat_service.build_turn_request(
        body, user_id=user.id, planner=chat_service.build_planner(user_id=user.id)
    )
    if request is None:
        raise HTTPException(
            status_code=400,
            detail=CLARIFY_DETAIL,
        )

    events: "queue.Queue[Any]" = queue.Queue()
    cancelled = {"value": False}

    def _run() -> None:
        try:
            chat_service.run_turn_streaming(
                request=request,
                user_id=user.id,
                body=body,
                events=events,
                is_cancelled=lambda: cancelled["value"],
            )
        except Exception as exc:  # noqa: BLE001 — 必须转成事件，否则前端会永久 loading
            # 用前端已认识的 ErrorEvent（`chatStream.ts` 有对应分支渲染 message），
            # 并**必须**补一个 EndEvent：缺了它前端的 finished 永远为 false，
            # 界面会一直转圈，用户既看不到原因也等不到结束。
            logger.exception("chat turn crashed")
            events.put(ErrorEvent(message=chat_service.describe_turn_failure(exc)))
            events.put(EndEvent(status="failed", reason="crash"))
        finally:
            events.put(None)

    async def _stream() -> AsyncIterator[str]:
        task = asyncio.create_task(asyncio.to_thread(_run))
        try:
            while True:
                item = await asyncio.to_thread(events.get)
                if item is None:
                    break
                payload = _serialize(item)
                yield f"event: {payload['event']}\ndata: {json.dumps(payload['data'], ensure_ascii=False)}\n\n"
        finally:
            cancelled["value"] = True
            # 等后台线程收尾，避免「响应已结束但循环还在跑」的悬挂工作
            try:
                await asyncio.wait_for(task, timeout=5)
            except Exception:  # noqa: BLE001
                pass

    return StreamingResponse(_stream(), media_type="text/event-stream", headers=_SSE_HEADERS)


@router.get("/chat/traces")
def list_chat_traces(
    conversation_id: str | None = Query(None, description="留空则返回该用户最近的 trace"),
    limit: int = Query(20, ge=1, le=200),
    user=Depends(current_user),
):
    """列出回合 trace：计划、每步执行、校验结论、终态。

    存在的理由：用户（或我们）说"这次答得不对"时，必须能回答"这一轮到底做了
    什么"。没有它，改进只能靠感觉。
    """
    from api.services import trace_service

    return {
        "traces": trace_service.list_turn_traces(
            user_id=user.id, conversation_id=conversation_id, limit=limit
        )
    }


@router.get("/chat/traces/summary")
def summarize_chat_traces(
    limit: int = Query(500, ge=1, le=2000),
    user=Depends(current_user),
):
    """按档位与工具归因延迟、token 与失败率（只读）。

    **必须声明在 `/chat/traces/{trace_id}` 之前**：FastAPI 按声明顺序匹配，
    放在后面的话 `summary` 会被当成某个 trace 的 id 吃掉，端点永远 404。

    存在的理由：本轮的延迟实验证明这条路径**方差大到单次采样没有意义**
    （同一问题同一配置重复三次 = 8.4 s / 13.2 s / 19.8 s）。要判断一次改动
    有没有用，就必须有"按档位取中位与 P90"的地方，否则只会在噪声里下结论。
    """
    from api.services import trace_service

    return trace_service.summarize_turn_traces(user_id=user.id, limit=limit)


@router.get("/chat/traces/{trace_id}")
def get_chat_trace(trace_id: str, user=Depends(current_user)):
    from api.services import trace_service

    payload = trace_service.get_turn_trace(user_id=user.id, trace_id=trace_id)
    if payload is None:
        # 不披露是否存在：跨用户探测得不到任何信息
        raise HTTPException(status_code=404, detail="trace 不存在")
    return payload


@router.get("/conversations")
def list_conversations(
    limit: int = Query(50, ge=1, le=200),
    user=Depends(current_user),
):
    return chat_service.list_conversations(user_id=user.id, limit=limit)


@router.get("/conversations/{conversation_id}")
def get_conversation(
    conversation_id: str,
    limit: int = Query(100, ge=1, le=500),
    user=Depends(current_user),
):
    payload = chat_service.get_conversation(
        user_id=user.id, conversation_id=conversation_id, limit=limit
    )
    if payload is None:
        # 不披露记录是否存在：跨用户探测得不到任何信息
        raise HTTPException(status_code=404, detail="会话不存在")
    return payload
