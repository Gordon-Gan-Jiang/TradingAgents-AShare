"""会话与判定的持久化契约。

## 为什么领域层定义 Protocol，实现放在应用层

`tradingagents/` 是纯引擎包，**不得** import `api/`（由架构边界测试强制）。
但会话与判定的读写又必须在 API 层落库。解法是把契约留在领域层、实现留在
应用层：

    tradingagents/agent/repository.py    ← Protocol + 内存实现（测试用）
    api/services/conversation_service.py ← SQLAlchemy 实现

这样领域层的编排逻辑可以在没有数据库的条件下被完整测试，也保证「换存储」
不会波及判定逻辑。

## 为什么判定是一等实体而不是消息的一部分

校准闭环要按 `(symbol, as_of, signal_version)` 检索判定并回填 T+3/T+5 结果。
若判定只是聊天记录 JSON 里的一个字段，就无法索引、无法去重、无法统计——
这正是本仓库「实现了却没人用」那类断点的典型成因。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence

STATUS_COMPLETED = "completed"
STATUS_DEGRADED = "degraded"
STATUS_CANCELLED = "cancelled"
STATUS_FAILED = "failed"


def new_id() -> str:
    return uuid.uuid4().hex


@dataclass(frozen=True)
class ConversationRecord:
    id: str
    user_id: str
    title: str = ""
    primary_symbol: str | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class MessageRecord:
    id: str
    conversation_id: str
    role: str
    content: str
    tier: str = ""
    model: str = ""
    tokens: int = 0
    latency_ms: int = 0
    status: str = STATUS_COMPLETED
    verdict_id: str | None = None
    tool_calls: tuple[str, ...] = ()
    weak_verification: bool = False
    created_at: str = ""


@dataclass(frozen=True)
class VerdictRecord:
    """一次探针判定的完整快照。

    `contributions` 必须是**全量权重与观测**，这样 T+5 回填后能按维度统计
    判别力，并在权重版本切换时重放校验——若只存最终分数，校准就无从谈起。
    """

    id: str
    user_id: str
    symbol: str
    as_of: str
    probe: str
    state: str
    confidence: int
    margin: float
    contributions: Mapping[str, Any] = field(default_factory=dict)
    modulator: Mapping[str, Any] = field(default_factory=dict)
    falsifiers: tuple[Mapping[str, Any], ...] = ()
    unavailable: tuple[str, ...] = ()
    daily_only: bool = False
    high_confidence: bool = False
    signal_version: str = ""
    conversation_id: str | None = None
    created_at: str = ""


@dataclass(frozen=True)
class OutcomeRecord:
    """判定在某个持有期上的实际结果。"""

    id: str
    verdict_id: str
    horizon_days: int
    ref_date: str
    max_high: float | None = None
    close: float | None = None
    ret_pct: float | None = None
    threshold_hit: bool = False
    evaluable: bool = True
    reason: str | None = None
    evaluated_at: str = ""


class ConversationRepository(Protocol):
    def create(self, *, user_id: str, title: str = "", primary_symbol: str | None = None,
               conversation_id: str | None = None) -> ConversationRecord: ...

    def get(self, *, conversation_id: str, user_id: str) -> ConversationRecord | None: ...

    def append_message(self, message: MessageRecord) -> MessageRecord: ...

    def recent_messages(self, *, conversation_id: str, user_id: str,
                        limit: int = 20) -> Sequence[MessageRecord]: ...

    def list_for_user(self, *, user_id: str, limit: int = 50) -> Sequence[ConversationRecord]: ...


class VerdictRepository(Protocol):
    def save(self, verdict: VerdictRecord) -> VerdictRecord: ...

    def get(self, *, verdict_id: str, user_id: str) -> VerdictRecord | None: ...

    def list_for_user(self, *, user_id: str, symbol: str | None = None,
                      limit: int = 100) -> Sequence[VerdictRecord]: ...

    def save_outcome(self, outcome: OutcomeRecord) -> OutcomeRecord: ...

    def outcomes_for(self, *, verdict_id: str, user_id: str) -> Sequence[OutcomeRecord]: ...

    def pending_outcomes(self, *, horizon_days: int, before_date: str,
                         limit: int = 200) -> Sequence[VerdictRecord]: ...


class InMemoryConversationRepository:
    """内存实现。测试与本地试跑用。

    刻意实现与 SQLAlchemy 版**完全相同的语义**（包括用户隔离），这样
    「测试通过但生产串号」这类问题不会因为实现差异而漏掉。
    """

    def __init__(self) -> None:
        self._conversations: dict[str, ConversationRecord] = {}
        self._messages: dict[str, list[MessageRecord]] = {}

    def create(self, *, user_id, title="", primary_symbol=None, conversation_id=None):
        cid = conversation_id or new_id()
        record = ConversationRecord(
            id=cid, user_id=user_id, title=title, primary_symbol=primary_symbol
        )
        self._conversations[cid] = record
        self._messages.setdefault(cid, [])
        return record

    def get(self, *, conversation_id, user_id):
        record = self._conversations.get(conversation_id)
        if record is None or record.user_id != user_id:
            return None
        return record

    def append_message(self, message: MessageRecord) -> MessageRecord:
        self._messages.setdefault(message.conversation_id, []).append(message)
        return message

    def recent_messages(self, *, conversation_id, user_id, limit=20):
        if self.get(conversation_id=conversation_id, user_id=user_id) is None:
            return ()
        return tuple(self._messages.get(conversation_id, [])[-limit:])

    def list_for_user(self, *, user_id, limit=50):
        items = [c for c in self._conversations.values() if c.user_id == user_id]
        return tuple(items[-limit:])


class InMemoryVerdictRepository:
    def __init__(self) -> None:
        self._verdicts: dict[str, VerdictRecord] = {}
        self._outcomes: dict[str, list[OutcomeRecord]] = {}

    def save(self, verdict: VerdictRecord) -> VerdictRecord:
        self._verdicts[verdict.id] = verdict
        self._outcomes.setdefault(verdict.id, [])
        return verdict

    def get(self, *, verdict_id, user_id):
        record = self._verdicts.get(verdict_id)
        if record is None or record.user_id != user_id:
            return None
        return record

    def list_for_user(self, *, user_id, symbol=None, limit=100):
        items = [
            v for v in self._verdicts.values()
            if v.user_id == user_id and (symbol is None or v.symbol == symbol)
        ]
        return tuple(items[-limit:])

    def save_outcome(self, outcome: OutcomeRecord) -> OutcomeRecord:
        bucket = self._outcomes.setdefault(outcome.verdict_id, [])
        for i, existing in enumerate(bucket):
            if existing.horizon_days == outcome.horizon_days:
                # 幂等：同一 horizon 重复回填不得产生第二行
                bucket[i] = outcome
                return outcome
        bucket.append(outcome)
        return outcome

    def outcomes_for(self, *, verdict_id, user_id):
        if self.get(verdict_id=verdict_id, user_id=user_id) is None:
            return ()
        return tuple(self._outcomes.get(verdict_id, []))

    def pending_outcomes(self, *, horizon_days, before_date, limit=200):
        pending = []
        for verdict in self._verdicts.values():
            if verdict.as_of >= before_date:
                continue
            recorded = {o.horizon_days for o in self._outcomes.get(verdict.id, [])}
            if horizon_days not in recorded:
                pending.append(verdict)
        return tuple(pending[:limit])
