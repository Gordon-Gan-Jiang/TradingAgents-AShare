"""回合 trace：把"这一轮到底做了什么"落库，供排查与评测。

## 为什么必须落库，而不是只写日志

Phase 0 的目标是**让改进可被证明**。三件事都依赖 trace：

1. **排查**：用户说"这次答得不对"，得能回答"计划是什么、哪一步空了、模型看到几张证据"。
   日志会被轮转、会被 grep 淹没，且无法按问句聚合。
2. **评测**：黄金集跑分要按问句比对期望（`text` 字段），线上失败样本也要能进集合。
3. **成本归因**：按档位/工具看 tokens 与耗时，才知道预算该调哪里。

## 只存摘要，不存工具原始返回

单轮工具返回可达 10-50KB（见 `ChatMessageDB` 的同类取舍）。trace 存的是
`{tool, arguments, ok, elapsed_ms, evidence_count}` —— 这些足以回答"为什么慢、
哪步空、模型看到了多少"，而原始数据按需重算比存旧快照更正确。
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

# 单条 trace 的 JSON 字段上限：防止一个异常巨大的计划把行写爆
_MAX_JSON_CHARS = 20_000


def _clip(payload: Any) -> Any:
    """把 JSON 字段裁剪到合理体积，并显式标注被裁剪。"""
    if payload is None:
        return None
    try:
        text = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        return {"_unserializable": str(type(payload).__name__)}
    if len(text) <= _MAX_JSON_CHARS:
        return payload
    return {"_clipped": True, "_preview": text[:_MAX_JSON_CHARS]}


def save_turn_trace(
    *,
    conversation_id: str,
    user_id: str,
    text: str,
    tier: str = "",
    status: str = "",
    plan: Mapping[str, Any] | None = None,
    steps: Sequence[Mapping[str, Any]] | None = None,
    tool_calls: Sequence[str] = (),
    tokens: int = 0,
    model_calls: int = 0,
    model: str = "",
    latency_ms: int = 0,
    rejected_claims: int = 0,
    weak_verification: bool = False,
    verdict_id: str | None = None,
    unavailable: Sequence[str] = (),
) -> str | None:
    """写一轮 trace。没有会话 id 时跳过（无从归属）。"""
    from api.database import ChatTurnTraceDB, get_db_ctx
    from tradingagents.agent.repository import new_id

    if not conversation_id:
        return None

    trace_id = new_id()
    with get_db_ctx() as db:
        db.add(
            ChatTurnTraceDB(
                id=trace_id,
                conversation_id=conversation_id,
                user_id=user_id,
                text=text or "",
                tier=tier or "",
                status=status or "",
                plan=_clip(dict(plan) if plan else None),
                steps=_clip([dict(step) for step in (steps or [])]),
                tool_calls=(list(tool_calls) or None),
                tokens=int(tokens or 0),
                model_calls=int(model_calls or 0),
                model=model or "",
                latency_ms=int(latency_ms or 0),
                rejected_claims=int(rejected_claims or 0),
                weak_verification=bool(weak_verification),
                verdict_id=verdict_id,
                unavailable=_clip(list(unavailable)) if unavailable else None,
            )
        )
        db.commit()
    return trace_id


def list_turn_traces(
    *,
    user_id: str,
    conversation_id: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """按会话（或该用户全部）列出 trace，最新在前。"""
    from api.database import ChatTurnTraceDB, get_db_ctx

    limit = max(1, min(int(limit), 200))
    with get_db_ctx() as db:
        query = db.query(ChatTurnTraceDB).filter(ChatTurnTraceDB.user_id == user_id)
        if conversation_id:
            query = query.filter(ChatTurnTraceDB.conversation_id == conversation_id)
        rows = (
            query.order_by(ChatTurnTraceDB.created_at.desc(), ChatTurnTraceDB.id.desc())
            .limit(limit)
            .all()
        )
        return [_to_dict(row) for row in rows]


def get_turn_trace(*, user_id: str, trace_id: str) -> dict[str, Any] | None:
    from api.database import ChatTurnTraceDB, get_db_ctx

    with get_db_ctx() as db:
        row = (
            db.query(ChatTurnTraceDB)
            .filter(ChatTurnTraceDB.id == trace_id, ChatTurnTraceDB.user_id == user_id)
            .first()
        )
        return _to_dict(row) if row is not None else None


def _to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "conversation_id": row.conversation_id,
        "text": row.text,
        "tier": row.tier,
        "status": row.status,
        "plan": row.plan or {},
        "steps": row.steps or [],
        "tool_calls": list(row.tool_calls or []),
        "tokens": row.tokens,
        "model_calls": row.model_calls,
        "model": row.model,
        "latency_ms": row.latency_ms,
        "rejected_claims": row.rejected_claims,
        "weak_verification": bool(row.weak_verification),
        "verdict_id": row.verdict_id,
        "unavailable": list(row.unavailable or []),
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
