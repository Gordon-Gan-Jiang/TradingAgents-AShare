"""会话状态：槽位、证据账本、未覆盖项、待确认提议、任务号。

## 为什么需要它（此前的多轮只有"文本历史"）

修复审计 A1 之后，多轮至少有了历史注入——但注入的是**上一轮问答的原文**。
于是三件事在结构上做不到：

1. **指代消解只能靠模型猜**。「它」「这只」靠模型从历史文本里推断标的——同一句
   话两次提问可能指向两只不同的股票。槽位是确定性的：上一轮解析出的标的存进
   `slots`，指代直接读槽位。
2. **证据编号会跨轮撞车**。每轮工具产出的证据都从 `E1` 开始；第二轮若引用
   「E1」，指的可能是第一轮的另一条事实。账本给出全局唯一的编号（并按轮次记录）。
3. **未覆盖项会静默消失**。上一轮声明「北向资金未覆盖」，下一轮回答里如果没再
   提，用户会以为这次覆盖到了。账本把未覆盖项**跨轮延续**，直到某一轮真的覆盖
   该源才清除。

## 为什么是纯值对象

状态必须可序列化（落库、跨进程恢复）、可测试（不依赖数据库），且**不读环境
时钟、不发请求**——与 `signals/`、`grounding/` 一样受 AST 边界测试约束。
持久化在应用层（`api/services/conversation_state_service.py`），本模块只管语义。

## 一条纪律：坏数据不抛错

落库的 payload 可能来自旧版本或被截断。`from_payload` 遇到不认识的字段就忽略，
遇到坏证据条目就跳过——**宁可降级成"状态少一点"，也不能让整轮对话因为读状态
失败而失败**。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, Sequence

from tradingagents.grounding.types import Coverage, EvidenceItem

# 账本与各字段的上限：状态是注入提示词的东西，无限增长会撑爆上下文
MAX_LEDGER_ENTRIES = 60
MAX_UNCOVERED = 20
MAX_JOB_IDS = 10
MAX_SLOT_TEXT = 200

_UNSET: Any = object()

# 指代词：命中即读槽位，**不**交给模型猜。
_SINGULAR_REFERENCE = (
    "它", "这只", "这个", "那只", "那个", "该股", "该标的", "此股",
    "刚才那只", "上面那只", "这一只", "同一只",
)
_PLURAL_REFERENCE = (
    "这两只", "那两个", "这几只", "这些", "那两个标的", "上述标的", "它们",
)

_CODE_RE = re.compile(r"(?<![\d.])([0368]\d{5})(?:\.(SH|SZ|BJ))?(?![\d.])")


def _as_text(value: Any) -> str:
    return str(value or "").strip()


def uncovered_key(entry: str) -> str:
    """未覆盖项的**比较键**：`"verdict_history（尚未回填）"` → `"verdict_history"`。

    工具在 `unavailable` 里写的是"源键（人话说明）"或纯源键。比较必须只看源键，
    否则同一维度的说明文字变一个字就被当成"还没覆盖"。
    """
    text = _as_text(entry)
    for separator in ("（", "(", "："):
        head, sep, _tail = text.partition(separator)
        if sep and head.strip():
            return head.strip()
    return text


def normalize_symbol(raw: Any) -> str:
    """把 `600519` / `600519.sh` / `600519.SH` 归一成 `600519.SH`。

    只做形态归一，**不猜交易所**之外的语义：无法识别为 6 位代码的原样返回。
    """
    text = _as_text(raw).upper()
    if not text:
        return ""
    match = _CODE_RE.search(text)
    if not match:
        return text
    code, suffix = match.group(1), match.group(2)
    if suffix:
        return f"{code}.{suffix}"
    if code.startswith(("6", "9")):
        return f"{code}.SH"
    if code.startswith(("0", "2", "3")):
        return f"{code}.SZ"
    if code.startswith("8"):
        return f"{code}.BJ"
    return code


@dataclass(frozen=True)
class LedgerEntry:
    """证据账本的一条：证据本身 + 它出自第几轮。"""

    turn: int
    item: EvidenceItem

    def as_dict(self) -> dict[str, Any]:
        return {
            "turn": int(self.turn),
            "id": self.item.id,
            "field": self.item.field,
            "value": self.item.value if isinstance(self.item.value, (str, int, float, bool)) else str(self.item.value),
            "unit": self.item.unit,
            "source_key": self.item.source_key,
            "as_of": self.item.as_of,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "LedgerEntry | None":
        try:
            item = EvidenceItem(
                id=_as_text(payload.get("id")),
                field=_as_text(payload.get("field")),
                value=payload.get("value", ""),
                unit=_as_text(payload.get("unit")),
                source_key=_as_text(payload.get("source_key")),
                as_of=_as_text(payload.get("as_of")),
            )
        except Exception:  # noqa: BLE001 — 坏条目跳过，不让状态读取失败
            return None
        if not item.id:
            return None
        try:
            turn = int(payload.get("turn") or 0)
        except (TypeError, ValueError):
            turn = 0
        return cls(turn=turn, item=item)


@dataclass(frozen=True)
class ConversationState:
    """一个会话的跨轮状态。不可变，每次推进返回新实例。"""

    conversation_id: str = ""
    turn: int = 0
    slots: Mapping[str, Any] = field(default_factory=dict)
    ledger: tuple[LedgerEntry, ...] = ()
    uncovered: tuple[str, ...] = ()
    pending: Mapping[str, Any] | None = None
    job_ids: tuple[str, ...] = ()

    # ── 序列化 ────────────────────────────────────────────────────────────

    def to_payload(self) -> dict[str, Any]:
        """JSON 安全的形态（落库用）。"""
        return {
            "version": 1,
            "conversation_id": self.conversation_id,
            "turn": int(self.turn),
            "slots": {str(k): v for k, v in (self.slots or {}).items()},
            "ledger": [entry.as_dict() for entry in self.ledger],
            "uncovered": list(self.uncovered),
            "pending": dict(self.pending) if isinstance(self.pending, Mapping) else None,
            "job_ids": list(self.job_ids),
        }

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, Any] | None, *, conversation_id: str = ""
    ) -> "ConversationState":
        """从落库 payload 还原。**坏数据不抛错**（降级成空状态）。"""
        if not isinstance(payload, Mapping):
            return cls(conversation_id=conversation_id)
        try:
            turn = int(payload.get("turn") or 0)
        except (TypeError, ValueError):
            turn = 0
        slots_raw = payload.get("slots")
        slots = (
            {str(k): v for k, v in slots_raw.items() if _as_text(k)}
            if isinstance(slots_raw, Mapping)
            else {}
        )
        ledger: list[LedgerEntry] = []
        for raw in payload.get("ledger") or []:
            if isinstance(raw, Mapping):
                entry = LedgerEntry.from_dict(raw)
                if entry is not None:
                    ledger.append(entry)
        uncovered_raw = payload.get("uncovered")
        uncovered = tuple(
            _as_text(x) for x in (uncovered_raw if isinstance(uncovered_raw, (list, tuple)) else [])
            if _as_text(x)
        )
        pending_raw = payload.get("pending")
        job_ids_raw = payload.get("job_ids")
        return cls(
            conversation_id=_as_text(payload.get("conversation_id")) or conversation_id,
            turn=turn,
            slots=slots,
            ledger=tuple(ledger[-MAX_LEDGER_ENTRIES:]),
            uncovered=uncovered[:MAX_UNCOVERED],
            pending=dict(pending_raw) if isinstance(pending_raw, Mapping) else None,
            job_ids=tuple(
                _as_text(x) for x in (job_ids_raw if isinstance(job_ids_raw, (list, tuple)) else [])
                if _as_text(x)
            )[:MAX_JOB_IDS],
        )

    # ── 只读视图 ──────────────────────────────────────────────────────────

    def evidence_ids(self) -> tuple[str, ...]:
        return tuple(entry.item.id for entry in self.ledger)

    def max_evidence_index(self) -> int:
        """账本里已用掉的最大编号数字（`E7` → 7）。用于给新证据接着编号。"""
        highest = 0
        for entry in self.ledger:
            digits = "".join(ch for ch in entry.item.id if ch.isdigit())
            if digits:
                try:
                    highest = max(highest, int(digits))
                except ValueError:  # pragma: no cover — digits 已保证可解析
                    continue
        return highest

    def ledger_as_evidence(self) -> tuple[EvidenceItem, ...]:
        """历史轮次的证据（供回答层引用第一轮的事实）。"""
        return tuple(entry.item for entry in self.ledger)

    def current_symbol(self) -> str | None:
        symbol = _as_text(self.slots.get("symbol"))
        return symbol or None

    # ── 指代消解（确定性） ────────────────────────────────────────────────

    def resolve_symbol(self, text: str) -> tuple[str | None, str]:
        """从文本确定标的：显式代码优先，其次**读槽位**消解指代。

        返回 `(symbol, 依据)`。依据会写进 trace，便于回答"为什么这次指的是它"。
        """
        content = _as_text(text)
        match = _CODE_RE.search(content)
        if match:
            return normalize_symbol(match.group(0)), "explicit_code"
        if any(marker in content for marker in _PLURAL_REFERENCE):
            symbols = self.slots.get("symbols")
            if isinstance(symbols, (list, tuple)) and symbols:
                return normalize_symbol(symbols[0]), "slot_plural_reference"
            if self.current_symbol():
                return self.current_symbol(), "slot_plural_reference_singular_fallback"
        if any(marker in content for marker in _SINGULAR_REFERENCE):
            if self.current_symbol():
                return self.current_symbol(), "slot_reference"
            return None, "reference_without_slot"
        return None, "no_symbol_in_text"

    # ── 推进 ──────────────────────────────────────────────────────────────

    def advance(
        self,
        *,
        evidence: Iterable[EvidenceItem] = (),
        coverage: Coverage | None = None,
        slots: Mapping[str, Any] | None = None,
        pending: Any = _UNSET,
        job_ids: Iterable[str] = (),
    ) -> "ConversationState":
        """累积一轮的结果，返回新状态。

        * `evidence` —— 本轮的证据。**重新编号**使其在账本内全局唯一（接着最大
          编号往下排），因此第二轮引用 `E3` 不会与第一轮的 `E3` 撞车。
        * `coverage` —— 本轮的覆盖声明。`available` 里的源会把对应的未覆盖项
          **清除**（说明这一轮真的覆盖到了），`unavailable` 里的项**延续**下去。
        * `slots` —— 本轮确认的槽位；值为 `None` 表示清除该槽位。
        * `pending` —— 用 `_UNSET` 表示"保持原样"；显式传 `None` 表示清除
          （确认或拒绝之后必须清除，否则下一轮会重复执行）。
        * `job_ids` —— 本轮提交的异步任务号（去重、有上限）。
        """
        offset = self.max_evidence_index()
        new_entries: list[LedgerEntry] = []
        for index, item in enumerate(evidence or (), start=1):
            new_entries.append(
                LedgerEntry(
                    turn=self.turn + 1,
                    item=replace(item, id=f"E{offset + index}"),
                )
            )

        merged_slots: dict[str, Any] = {str(k): v for k, v in (self.slots or {}).items()}
        for key, value in (slots or {}).items():
            name = _as_text(key)
            if not name:
                continue
            if value is None:
                merged_slots.pop(name, None)
            else:
                merged_slots[name] = value
        if isinstance(merged_slots.get("symbol"), str):
            merged_slots["symbol"] = normalize_symbol(merged_slots["symbol"]) or merged_slots["symbol"]

        available_keys = {
            uncovered_key(x) for x in ((coverage.available if coverage else ()) or ())
        }
        carried = [
            entry for entry in self.uncovered if uncovered_key(entry) not in available_keys
        ]
        fresh = [
            _as_text(x) for x in ((coverage.unavailable if coverage else ()) or ()) if _as_text(x)
        ]
        merged_uncovered: list[str] = []
        seen_keys: set[str] = set()
        for entry in carried + fresh:
            key = uncovered_key(entry)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            merged_uncovered.append(entry)

        merged_jobs = list(self.job_ids)
        for job in job_ids or ():
            text = _as_text(job)
            if text and text not in merged_jobs:
                merged_jobs.append(text)

        resolved_pending = self.pending if pending is _UNSET else pending

        return ConversationState(
            conversation_id=self.conversation_id,
            turn=self.turn + 1,
            slots=merged_slots,
            ledger=(self.ledger + tuple(new_entries))[-MAX_LEDGER_ENTRIES:],
            uncovered=tuple(merged_uncovered[:MAX_UNCOVERED]),
            pending=dict(resolved_pending) if isinstance(resolved_pending, Mapping) else None,
            job_ids=tuple(merged_jobs[-MAX_JOB_IDS:]),
        )

    def with_pending(self, pending: Mapping[str, Any] | None) -> "ConversationState":
        """只更新待确认提议（提议产生/确认/拒绝时用）。"""
        return replace(self, pending=dict(pending) if isinstance(pending, Mapping) else None)


def uncovered_keys(entries: Sequence[str]) -> tuple[str, ...]:
    """未覆盖项的源键清单（注入提示词与做判定时用）。"""
    return tuple(uncovered_key(entry) for entry in entries or () if _as_text(entry))
