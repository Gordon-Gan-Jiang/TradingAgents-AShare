"""会话状态（Phase 3.1–3.4）：槽位、证据账本、未覆盖延续、指代消解。

## 这个测试锁住什么

1. **证据编号跨轮全局唯一**：第二轮的新证据接着最大编号往下排，于是"第二轮引用
   第一轮的 E1"是合法且无歧义的（不是两条事实共用一个号）。
2. **未覆盖项跨轮延续**：上一轮声明未覆盖的源，下一轮若没覆盖到就必须继续出现在
   声明里；真的覆盖到了才清除。它**不得静默消失**。
3. **指代消解是确定性的**：显式代码优先 → 指代词读槽位；槽位为空时明确返回
   "没有槽位"，而不是让模型去猜一个标的。
4. **状态可序列化、可跨轮恢复**，且**坏 payload 不抛错**（降级成空状态）。
5. **状态不读环境时钟、不发请求**（与 signals/grounding 同一套边界约束）。
"""

from __future__ import annotations

import pytest

from tradingagents.grounding.types import Coverage, EvidenceItem


def _item(item_id: str, field: str = "价格", value: str = "10.00") -> EvidenceItem:
    return EvidenceItem(
        id=item_id, field=field, value=value, unit="", source_key="stock_data", as_of="2026-09-21"
    )


def _state(**kwargs):
    from tradingagents.agent.state import ConversationState

    return ConversationState(**kwargs)


# ── 证据账本跨轮累积（3.3）────────────────────────────────────────────────


def test_evidence_ids_are_globally_unique_across_turns():
    state = _state(conversation_id="c1")
    state = state.advance(evidence=[_item("E1"), _item("E2")], coverage=Coverage(available=("stock_data",)))

    assert state.evidence_ids() == ("E1", "E2")
    assert state.turn == 1

    state = state.advance(evidence=[_item("E1")], coverage=Coverage(available=("stock_data",)))

    # 第二轮的那条不能再叫 E1：否则"引用 E1"会指向两条不同的事实
    assert state.evidence_ids() == ("E1", "E2", "E3")
    assert [entry.turn for entry in state.ledger] == [1, 1, 2]


def test_ledger_keeps_previous_turn_evidence_citable():
    state = _state()
    state = state.advance(evidence=[_item("E1", field="现价", value="1700.00")])
    state = state.advance(evidence=[_item("E1", field="涨跌幅", value="+2.30%")])

    first = state.ledger_as_evidence()[0]
    assert first.id == "E1" and first.field == "现价", "第一轮的证据必须还能被引用"
    assert state.ledger_as_evidence()[1].id == "E2"


def test_ledger_is_bounded():
    from tradingagents.agent.state import MAX_LEDGER_ENTRIES

    state = _state()
    for _ in range(MAX_LEDGER_ENTRIES + 5):
        state = state.advance(evidence=[_item("E1")])
    assert len(state.ledger) == MAX_LEDGER_ENTRIES, "账本必须有上限（它要进提示词）"


# ── 未覆盖项跨轮延续（3.4）────────────────────────────────────────────────


def test_uncovered_carries_over_until_actually_covered():
    state = _state()
    state = state.advance(
        coverage=Coverage(available=("stock_data",), unavailable=("northbound（北向资金未覆盖）",))
    )
    assert state.uncovered == ("northbound（北向资金未覆盖）",)

    # 第二轮覆盖了别的源：未覆盖项必须继续在场，不得静默消失
    state = state.advance(coverage=Coverage(available=("stock_data",)))
    assert state.uncovered == ("northbound（北向资金未覆盖）",)


def test_uncovered_is_cleared_only_when_the_source_is_covered():
    state = _state()
    state = state.advance(coverage=Coverage(unavailable=("northbound（北向资金未覆盖）",)))
    state = state.advance(coverage=Coverage(available=("northbound",)))

    assert state.uncovered == (), "真的覆盖到了才清除"


def test_uncovered_key_comparison_ignores_the_human_readable_suffix():
    from tradingagents.agent.state import uncovered_key

    assert uncovered_key("northbound（北向资金未覆盖）") == "northbound"
    assert uncovered_key("northbound") == "northbound"
    assert uncovered_key("") == ""


def test_uncovered_is_deduplicated_without_losing_the_description():
    state = _state()
    state = state.advance(coverage=Coverage(unavailable=("northbound（未覆盖）",)))
    state = state.advance(coverage=Coverage(unavailable=("northbound（还是没覆盖）",)))

    assert state.uncovered == ("northbound（未覆盖）",), "同一源只留一条，保留最早的说法"


# ── 指代消解（3.2）────────────────────────────────────────────────────────


def test_slots_provide_deterministic_reference_resolution():
    state = _state(slots={"symbol": "600519.SH"})

    assert state.resolve_symbol("600519 现在多少钱") == ("600519.SH", "explicit_code")
    assert state.resolve_symbol("它现在怎么样") == ("600519.SH", "slot_reference")
    assert state.resolve_symbol("这只还能拿吗") == ("600519.SH", "slot_reference")


def test_reference_without_slot_says_so_instead_of_guessing():
    state = _state()
    symbol, reason = state.resolve_symbol("它现在怎么样")

    assert symbol is None
    assert reason == "reference_without_slot", "没有槽位时必须明说，不能猜一个标的"
    assert state.resolve_symbol("今天大盘怎么样") == (None, "no_symbol_in_text")


def test_plural_reference_reads_the_symbols_slot():
    state = _state(slots={"symbol": "600519.SH", "symbols": ["300750.SZ", "000001.SZ"]})
    symbol, reason = state.resolve_symbol("这两只哪只更强")
    assert symbol == "300750.SZ"
    assert reason == "slot_plural_reference"


@pytest.mark.parametrize(
    "raw,expected",
    [("600519", "600519.SH"), ("600519.sh", "600519.SH"), ("300750", "300750.SZ"),
     ("000001", "000001.SZ"), ("830799", "830799.BJ"), ("600519.SH", "600519.SH")],
)
def test_symbol_normalization(raw, expected):
    from tradingagents.agent.state import normalize_symbol

    assert normalize_symbol(raw) == expected


def test_slots_can_be_cleared():
    state = _state(slots={"symbol": "600519.SH"})
    state = state.advance(slots={"symbol": None})
    assert state.current_symbol() is None


def test_advance_normalizes_the_symbol_slot():
    state = _state().advance(slots={"symbol": "600519"})
    assert state.current_symbol() == "600519.SH"


# ── 待确认提议与任务号（3.1 / 3.5）──────────────────────────────────────────


def test_pending_survives_until_explicitly_cleared():
    state = _state()
    state = state.advance(pending={"kind": "watchlist_add", "params": {"symbol": "600519.SH"}})
    assert state.pending is not None

    # 不传 pending = 保持原样（中间轮次不该把提议丢掉）
    state = state.advance(evidence=[_item("E1")])
    assert state.pending is not None

    # 显式传 None = 清除（确认或拒绝之后必须清除）
    state = state.advance(pending=None)
    assert state.pending is None


def test_job_ids_accumulate_and_deduplicate():
    state = _state()
    state = state.advance(job_ids=["job-1"])
    state = state.advance(job_ids=["job-1", "job-2"])

    assert state.job_ids == ("job-1", "job-2"), "任务号去重（同一任务可能被重复回报）"


# ── 序列化与恢复（3.1）────────────────────────────────────────────────────


def test_state_round_trips_through_its_payload():
    from tradingagents.agent.state import ConversationState

    state = (
        _state(conversation_id="c9")
        .advance(
            evidence=[_item("E1", field="现价", value=1700.0)],
            coverage=Coverage(available=("stock_data",), unavailable=("northbound（未覆盖）",)),
            slots={"symbol": "600519.SH", "as_of": "2026-09-21"},
            pending={"kind": "watchlist_add"},
            job_ids=["job-7"],
        )
    )

    restored = ConversationState.from_payload(state.to_payload())

    assert restored.turn == state.turn == 1
    assert restored.slots == state.slots
    assert restored.uncovered == state.uncovered
    assert restored.pending == state.pending
    assert restored.job_ids == state.job_ids
    assert restored.evidence_ids() == state.evidence_ids()
    assert restored.ledger[0].item.value == 1700.0, "数值必须原样还原（不得变成字符串）"


def test_bad_payload_degrades_instead_of_raising():
    from tradingagents.agent.state import ConversationState

    assert ConversationState.from_payload(None).turn == 0
    assert ConversationState.from_payload("not-a-mapping").slots == {}

    messy = ConversationState.from_payload(
        {
            "turn": "not-a-number",
            "slots": ["not", "a", "mapping"],
            "ledger": ["nope", {"id": ""}, {"turn": 1, "id": "E1", "field": "价格"}],
            "uncovered": "not-a-list",
            "job_ids": {"nope": 1},
        }
    )
    assert messy.turn == 0
    assert messy.slots == {}
    assert messy.uncovered == ()
    assert messy.job_ids == ()
    assert len(messy.ledger) == 1, "坏条目跳过，好的留下"


def test_state_module_has_no_io_or_ambient_clock():
    """与 signals/grounding 同级约束：状态语义必须纯（可回放、可单测）。"""
    import ast
    import pathlib

    path = pathlib.Path(__file__).resolve().parent.parent / "tradingagents" / "agent" / "state.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    banned = {"requests", "httpx", "akshare", "openai", "api", "sqlalchemy"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in banned
        elif isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] not in banned
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Name):
                assert (node.func.value.id, node.func.attr) not in {
                    ("datetime", "now"), ("datetime", "today"), ("time", "time"),
                }
