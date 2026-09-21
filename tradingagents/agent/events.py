"""对话回合的事件契约。

## 为什么事件是唯一输出契约

服务端 SSE、命令行输出、测试断言都消费同一套事件类型。加一个消费端
（例如企业微信推送「今日判定」）只需要写一个新的 `EventSink` 实现，
**不需要改 Agent 循环**——这是「新增功能不修改旧文件」的具体体现。

## 为什么终止事件必须无条件发出

客户端靠它判断「回合结束了」。缺失终止事件会让前端永久停在 loading 状态；
而失败路径最容易漏掉它——因此规格把它写成硬约束，并由测试覆盖
成功/失败/取消三种情况。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from tradingagents.grounding.types import TurnVerdict

# 回合状态
STATUS_COMPLETED = "completed"
STATUS_DEGRADED = "degraded"
STATUS_CANCELLED = "cancelled"
STATUS_FAILED = "failed"


@dataclass(frozen=True)
class SessionEvent:
    """会话标识。回合开始即发出，让前端能续接下一轮。

    修复审计 A1：此前 9 种事件无一携带会话 id，于是「会话持久化了但客户端
    不知道 id」——多轮对话在结构上不可能发生。会话 id 必须**在流里回传**，
    而不是让客户端去列表接口里猜。
    """

    conversation_id: str


@dataclass(frozen=True)
class AnswerDelta:
    """回答文本增量。前端据此逐字渲染。"""

    text: str


@dataclass(frozen=True)
class AnswerDone:
    text: str


@dataclass(frozen=True)
class ToolStart:
    tool: str
    arguments: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolDone:
    tool: str
    ok: bool
    detail: str | None = None
    elapsed_ms: int = 0
    evidence_count: int = 0


@dataclass(frozen=True)
class VerdictEvent:
    """结构化判定。前端据此渲染判定卡片，而不是从正文里解析。"""

    verdict: TurnVerdict


@dataclass(frozen=True)
class ProposalEvent:
    """一个待确认的动作提议。

    刻意是独立事件而不是正文里的一句话：前端据此渲染可点击的确认按钮，
    用户也不会误以为动作已经执行了。
    """

    proposal: Mapping[str, Any]


@dataclass(frozen=True)
class ActionExecutedEvent:
    """动作已实际执行（仅可能出现在产品内对话面）。"""

    kind: str
    summary: str
    ok: bool


@dataclass(frozen=True)
class ClarificationNeeded:
    """需要用户补充信息才能继续。

    缺标的时猜一个，等于制造一个错误答案；把候选列出来让用户选，是唯一
    诚实的做法。
    """

    question: str
    candidates: tuple[str, ...] = ()


@dataclass(frozen=True)
class Notice:
    """过程性提示：预算截断、无新证据、可提议升级等。"""

    text: str
    kind: str = "notice"


@dataclass(frozen=True)
class ErrorEvent:
    message: str


@dataclass(frozen=True)
class EndEvent:
    status: str
    reason: str | None = None


class EventSink(Protocol):
    def emit(self, event: Any) -> None: ...


class NullSink:
    """丢弃全部事件。用于不需要输出的场景（例如内部批处理）。"""

    def emit(self, event: Any) -> None:  # pragma: no cover - 无副作用
        return None


class ListSink:
    """内存收集器。测试与调试用，也是「同一套事件可被任意消费」的证明。"""

    def __init__(self) -> None:
        self.events: list[Any] = []

    def emit(self, event: Any) -> None:
        self.events.append(event)

    def of(self, kind: type) -> list[Any]:
        return [e for e in self.events if isinstance(e, kind)]
