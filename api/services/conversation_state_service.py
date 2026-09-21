"""会话状态的持久化（Phase 3.1）。

## 职责边界

语义在引擎层（`tradingagents/agent/state.py`，纯值对象、可单测），**持久化在这里**。
这条分工与工具端口同源：引擎不该知道 SQLAlchemy 存在，应用层不该重新实现语义。

## 两条纪律

1. **读写都必须同时匹配 `conversation_id` 与 `user_id`**：只按会话 id 查会让猜中
   一个 id 就能读到别人的槽位、证据与未覆盖项。与 `load_recent_history` 同一纪律。
2. **坏 payload / 写失败都不抛错给对话**：状态读不到就当空状态（用户只是少一点
   上下文），写失败只记日志——状态是**增强**，不能让整轮回答失败。但读不到与
   真的是空状态必须可区分：返回值里带 `state_loaded` 由调用方决定怎么声明。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from tradingagents.agent.state import ConversationState

logger = logging.getLogger(__name__)


def load_state(db: Session, *, user_id: str, conversation_id: str | None) -> ConversationState:
    """读出会话状态；没有就返回空状态（**不抛错**）。"""
    if not conversation_id or not user_id:
        return ConversationState(conversation_id=conversation_id or "")
    try:
        from api.database import ChatConversationStateDB

        row = (
            db.query(ChatConversationStateDB)
            .filter(
                ChatConversationStateDB.conversation_id == conversation_id,
                ChatConversationStateDB.user_id == user_id,
            )
            .first()
        )
    except Exception as exc:  # noqa: BLE001 — 状态读不到不该让回答失败
        logger.info("conversation state load failed for %s: %s", conversation_id, exc)
        return ConversationState(conversation_id=conversation_id)

    if row is None:
        return ConversationState(conversation_id=conversation_id)
    return ConversationState.from_payload(row.payload, conversation_id=conversation_id)


def save_state(db: Session, *, user_id: str, state: ConversationState) -> bool:
    """写入（upsert）会话状态。返回是否写入成功——**失败不抛错**。

    只在 `turn > 0` 时落库：一个还没跑过任何一轮的状态没有任何可续接的内容，
    写它只会制造"有状态但全是空"的假象。
    """
    if not user_id or not state.conversation_id or state.turn <= 0:
        return False
    try:
        from api.database import ChatConversationStateDB

        row = (
            db.query(ChatConversationStateDB)
            .filter(
                ChatConversationStateDB.conversation_id == state.conversation_id,
                ChatConversationStateDB.user_id == user_id,
            )
            .first()
        )
        payload = state.to_payload()
        if row is None:
            db.add(
                ChatConversationStateDB(
                    conversation_id=state.conversation_id,
                    user_id=user_id,
                    payload=payload,
                )
            )
        else:
            row.payload = payload
            row.updated_at = datetime.now(timezone.utc)
        db.commit()
        return True
    except Exception as exc:  # noqa: BLE001 — 落库失败只记日志，回答照常完成
        logger.warning("conversation state save failed for %s: %s", state.conversation_id, exc)
        try:
            db.rollback()
        except Exception:  # pragma: no cover — 回滚失败没有更多可做的
            pass
        return False


def state_summary(state: ConversationState) -> dict[str, Any]:
    """给 trace / 调试看的一行摘要（不含证据正文，避免 trace 被账本撑爆）。"""
    return {
        "turn": state.turn,
        "slots": dict(state.slots or {}),
        "evidence_ids": list(state.evidence_ids()),
        "uncovered": list(state.uncovered),
        "pending_kind": (state.pending or {}).get("kind") if state.pending else None,
        "job_ids": list(state.job_ids),
    }
