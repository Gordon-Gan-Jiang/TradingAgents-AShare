"""回合 trace：计划 / 逐步观测 / 终态必须可查。

## 这个文件锁住什么

Phase 0 的第三条交付物。没有 trace，后面所有"改进了"的说法都无法核实：
用户说"这次答得不对"，我们只能猜；成本涨了，也不知道涨在哪个工具上。

四条纪律：

1. **每轮都有 trace**，包括降级与澄清回合——只记成功回合等于把最难查的那类藏起来。
2. **计划必须落库**（不只是工具名）："打算做什么、为什么这么打算"是可审计性的最小单位。
3. **逐步观测要看得到失败**：失败步骤必须带 detail，否则"哪一步空了"无从回答。
4. **trace 写失败不得影响回答**：它是诊断数据，不能把一次成功回答变成 500。
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4


def _user() -> str:
    from api.database import UserDB, get_db_ctx, init_db
    from api.services import auth_service

    init_db()
    email = auth_service.normalize_email(f"trace-{uuid4().hex[:8]}@test.com")
    now = datetime.now(timezone.utc)
    with get_db_ctx() as db:
        user = UserDB(
            id=str(uuid4()), email=email, is_active=True,
            created_at=now, updated_at=now, last_login_at=now,
        )
        db.add(user)
        db.commit()
        return user.id


# ── 1. 服务层 ─────────────────────────────────────────────────────────────


def test_trace_round_trip_keeps_plan_and_steps():
    from api.services import trace_service

    user_id = _user()
    trace_id = trace_service.save_turn_trace(
        conversation_id="conv-1",
        user_id=user_id,
        text="600519 是不是在洗盘",
        tier="medium",
        status="completed",
        plan={"tier": "medium", "symbol": "600519.SH", "planned": [{"tool": "signal_washout_probe"}]},
        steps=[{"tool": "signal_washout_probe", "arguments": {"symbol": "600519.SH"},
                "ok": True, "elapsed_ms": 812, "evidence_count": 9}],
        tool_calls=("signal_washout_probe",),
        tokens=1234,
        model_calls=1,
        model="deepseek-v4-flash",
        latency_ms=1500,
        unavailable=("position",),
    )
    assert trace_id

    row = trace_service.get_turn_trace(user_id=user_id, trace_id=trace_id)
    assert row is not None
    assert row["text"] == "600519 是不是在洗盘"
    assert row["plan"]["symbol"] == "600519.SH"
    assert row["steps"][0]["tool"] == "signal_washout_probe"
    assert row["steps"][0]["evidence_count"] == 9
    assert row["tokens"] == 1234
    assert row["unavailable"] == ["position"]
    assert row["created_at"], "要有时间戳才能按时间排查"


def test_failed_step_is_visible_with_detail():
    """失败步骤必须能看出"哪一步空了、为什么"。"""
    from api.services import trace_service

    user_id = _user()
    trace_id = trace_service.save_turn_trace(
        conversation_id="conv-2",
        user_id=user_id,
        text="600519 是不是在洗盘",
        status="degraded",
        steps=[{"tool": "signal_washout_probe", "arguments": {}, "ok": False,
                "elapsed_ms": 0, "evidence_count": 0,
                "detail": "InsufficientHistory: daily bars missing"}],
    )
    row = trace_service.get_turn_trace(user_id=user_id, trace_id=trace_id)
    assert row is not None
    assert row["steps"][0]["ok"] is False
    assert "InsufficientHistory" in row["steps"][0]["detail"]


def test_traces_are_scoped_to_the_owner():
    """跨用户不可见——trace 里有问句原文与成本，属于用户数据。"""
    from api.services import trace_service

    owner, other = _user(), _user()
    trace_id = trace_service.save_turn_trace(
        conversation_id="conv-3", user_id=owner, text="我的持仓"
    )
    assert trace_service.get_turn_trace(user_id=other, trace_id=trace_id) is None
    assert trace_service.list_turn_traces(user_id=other) == []


def test_list_filters_by_conversation_and_is_newest_first():
    from api.services import trace_service

    user_id = _user()
    for index in range(3):
        trace_service.save_turn_trace(
            conversation_id="conv-a", user_id=user_id, text=f"问题{index}"
        )
    trace_service.save_turn_trace(conversation_id="conv-b", user_id=user_id, text="别的会话")

    rows = trace_service.list_turn_traces(user_id=user_id, conversation_id="conv-a")
    assert len(rows) == 3
    assert all(row["conversation_id"] == "conv-a" for row in rows)
    assert len(trace_service.list_turn_traces(user_id=user_id)) == 4


def test_oversized_payload_is_clipped_not_rejected():
    """异常巨大的计划不该把写入搞崩（工具参数可能很长的列表）。"""
    from api.services import trace_service

    user_id = _user()
    big = {"planned": [{"tool": "x", "arguments": {"blob": "y" * 30_000}}]}
    trace_id = trace_service.save_turn_trace(
        conversation_id="conv-4", user_id=user_id, text="大计划", plan=big
    )
    row = trace_service.get_turn_trace(user_id=user_id, trace_id=trace_id)
    assert row is not None
    assert row["plan"].get("_clipped") is True


def test_missing_conversation_id_is_skipped():
    """没有会话 id 就没有归属，返回 None 而不是写脏数据。"""
    from api.services import trace_service

    assert trace_service.save_turn_trace(conversation_id="", user_id=_user(), text="x") is None


def test_unserializable_payload_does_not_raise():
    from api.services import trace_service

    user_id = _user()
    trace_id = trace_service.save_turn_trace(
        conversation_id="conv-5", user_id=user_id, text="x",
        plan={"bad": object()},
    )
    row = trace_service.get_turn_trace(user_id=user_id, trace_id=trace_id)
    assert row is not None
    assert "_unserializable" in row["plan"]


# ── 2. 循环侧：TurnResult 必须带上 plan 与 steps ──────────────────────────


def _run_simple_turn(monkeypatch, text: str = "600519 是不是在洗盘"):
    from api.services import chat_service, signal_service
    from tests.test_acceptance_conversational_signal_agent import _ports_factory
    from tradingagents.agent.events import ListSink
    from tradingagents.agent.loop import TurnRequest, run_turn
    from tradingagents.tools import build_registry

    factory = _ports_factory()
    monkeypatch.setattr(signal_service, "build_ports_factory", lambda user_id=None: factory)

    class _Clock:
        def __init__(self):
            self.t = 0.0

        def now(self):
            self.t += 0.5
            return self.t

    class _Model:
        def propose(self, *, prompt, evidence, verdict, has_tool_text=False):
            from tradingagents.grounding.types import StructuredAnswer

            return StructuredAnswer(
                summary="缩量下杀后出现承接。", claims=(), verdict_ref=None,
                declared_unavailable=(),
            )

        def repair(self, **kw):  # pragma: no cover
            return kw["previous"]

    sink = ListSink()
    result = run_turn(
        request=TurnRequest(
            text=text, user_id="u1", symbol="600519.SH", tier="medium", as_of="2026-09-21"
        ),
        registry=build_registry(ports_factory=factory, news_fetcher=lambda *a: []),
        model=_Model(),
        sink=sink,
        clock=_Clock(),
    )
    return result


def test_turn_result_carries_plan(monkeypatch):
    """计划要落进 TurnResult：否则 trace 只有工具名，无法复盘"打算做什么"。"""
    result = _run_simple_turn(monkeypatch)
    assert result.plan, "回合必须带回计划"
    assert result.plan["tier"] == "medium"
    assert result.plan["symbol"] == "600519.SH"
    assert [step["tool"] for step in result.plan["planned"]] == ["signal_washout_probe"]
    assert result.plan["planned"][0]["reason"]


def test_turn_result_carries_execution_steps(monkeypatch):
    result = _run_simple_turn(monkeypatch)
    assert result.steps, "回合必须带回逐步观测"
    step = result.steps[0]
    assert step["tool"] == "signal_washout_probe"
    assert step["arguments"]["symbol"] == "600519.SH"
    assert "ok" in step and "elapsed_ms" in step and "evidence_count" in step


def test_execution_steps_carry_evidence_ids(monkeypatch):
    """每步要记下**证据编号**，不能只记条数。

    只记 `evidence_count=11` 能说明"取到东西了"，但回答不了"哪条证据是哪个工具给的"。
    验证"证据能否被引用"（① 的核心问法之一）必须能从观测里连起来：工具 → 证据编号
    → 回答里的 refs。编号缺失时这条链在第一步就断了，只能靠再跑一次去猜。
    """
    result = _run_simple_turn(monkeypatch)
    step = result.steps[0]

    assert step["evidence_count"] > 0, "样本回合本身要产出证据，否则这条断言没有意义"
    assert step.get("evidence_ids"), "步骤里必须带证据编号"
    assert len(step["evidence_ids"]) == step["evidence_count"]


def test_plan_records_multi_symbol_and_screen_turns(monkeypatch):
    """多标的与筛选回合的计划形状也要正确（它们是本轮新增的两条路径）。"""
    from tradingagents.agent.loop import TurnRequest, _plan_tools

    multi = TurnRequest(text="600519 和 300750 哪个好", user_id="u1",
                        symbols=("600519.SH", "300750.SZ"), tier="medium")
    assert [c.tool for c in _plan_tools(multi)] == ["signal_washout_probe_batch"]

    screen = TurnRequest(text="放量票找十个给我", user_id="u1", screen=True, tier="medium")
    assert [c.tool for c in _plan_tools(screen)] == ["market_screen"]


# ── 3. HTTP：一轮对话后必须能查到 trace ───────────────────────────────────


def test_http_turn_is_traceable(monkeypatch):
    from fastapi.testclient import TestClient

    from api.main import app
    from api.services import chat_service, signal_service
    from tests.test_acceptance_conversational_signal_agent import _ports_factory
    from tests.test_symbol_resolution import _jwt
    from tradingagents.tools import build_registry

    factory = _ports_factory()
    monkeypatch.setattr(signal_service, "build_ports_factory", lambda user_id=None: factory)
    monkeypatch.setattr(chat_service, "build_ports_factory", lambda user_id=None: factory)
    monkeypatch.setattr(
        chat_service, "build_registry",
        lambda user_id=None: build_registry(ports_factory=factory, news_fetcher=lambda *a: []),
    )

    class _Model:
        def propose(self, *, prompt, evidence, verdict, has_tool_text=False):
            from tradingagents.grounding.types import StructuredAnswer

            return StructuredAnswer(
                summary="缩量下杀后出现承接。", claims=(), verdict_ref=None,
                declared_unavailable=(),
            )

        def repair(self, **kw):  # pragma: no cover
            return kw["previous"]

    monkeypatch.setattr(chat_service, "build_answer_model", lambda **kw: _Model())

    client = TestClient(app, raise_server_exceptions=False)
    auth = {"Authorization": f"Bearer {_jwt()}"}
    resp = client.post(
        "/v1/chat",
        json={"messages": [{"role": "user", "content": "600519 是不是在洗盘"}],
              "symbol": "600519.SH"},
        headers=auth,
    )
    assert resp.status_code == 200, resp.text[:200]

    listed = client.get("/v1/chat/traces", headers=auth)
    assert listed.status_code == 200
    traces = listed.json()["traces"]
    assert traces, "一轮对话后必须能查到 trace"
    trace = traces[0]
    assert trace["text"] == "600519 是不是在洗盘"
    assert trace["steps"], "trace 里要有逐步观测"

    detail = client.get(f"/v1/chat/traces/{trace['id']}", headers=auth)
    assert detail.status_code == 200
    assert detail.json()["id"] == trace["id"]
