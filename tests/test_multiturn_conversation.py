"""多轮对话：会话 id 回传、历史注入、指代消解（审计 A1 修复）。

## 修复前的实际状态（审计发现）

设计声称"会话持久化 + 多轮上下文"，实际只有**落库**、没有**回灌**：

* 9 种 SSE 事件里**没有任何一种携带会话 id** → 前端即使用户想续接也无从知道 id
* `_build_prompt` 从不注入历史消息 → 模型看不到上一轮
* 前端 `ConversationalPanel` 因此从不传 `conversationId`

后果：用户问「600519 是不是在洗盘」得到回答后问「那它还能拿吗」，系统**不知道
「它」指什么**——所谓"对话 Agent"实质是单轮问答。

## 修复的三处

1. 新增 `SessionEvent` 事件，回合开始即回传 `conversation_id`
2. `TurnRequest.history` + `_build_prompt` 注入最近 N 轮问答
3. 前端保存 id 并在下一轮回传

## 测试重点

指代消解不能只靠"注入了历史"这种弱断言——必须验证**历史真的进了提示词**，
且**同一会话的消息按时间序、跨用户不可见**。
"""

from __future__ import annotations

from tests.test_agent_loop import (
    CollectingSink,
    FrozenClock,
    QueueModel,
    _grounded_answer,
    _registry,
    _request,
)


def _run(model, *, request=None, sink=None, **kw):
    from tradingagents.agent.loop import run_turn

    sink = sink or CollectingSink()
    result = run_turn(
        request=request or _request(),
        registry=kw.pop("registry", None) or _registry(),
        model=model,
        sink=sink,
        clock=kw.pop("clock", None) or FrozenClock(),
        middleware=kw.pop("middleware", None),
        **kw,
    )
    return result, sink


# ══════════════════════════════════════════════════════════════════════════
# 会话 id 回传
# ══════════════════════════════════════════════════════════════════════════


def test_session_event_carries_the_conversation_id():
    """前端必须能在**流里**拿到会话 id，否则无从续接。

    「落库了但客户端不知道 id」等于没落——这是审计发现的原始问题。
    """
    from tradingagents.agent.events import SessionEvent

    result, sink = _run(
        QueueModel([_grounded_answer]),
        request=_request(conversation_id="conv-123"),
    )

    sessions = sink.of(SessionEvent)
    assert len(sessions) == 1
    assert sessions[0].conversation_id == "conv-123"
    assert result.conversation_id == "conv-123"


def test_session_event_is_emitted_before_any_work():
    """会话 id 必须尽早发出，让前端在等待回答时就能建立会话状态。"""
    from tradingagents.agent.events import SessionEvent

    _, sink = _run(QueueModel([_grounded_answer]), request=_request(conversation_id="conv-early"))

    assert isinstance(sink.events[0], SessionEvent)
    assert sink.events[0].conversation_id == "conv-early"


def test_session_event_is_emitted_even_for_clarification():
    """追问回合同样属于该会话——它也必须回传 id。"""
    from tradingagents.agent.events import SessionEvent

    _, sink = _run(
        QueueModel([]),
        request=_request(text="帮我看看还能不能拿", symbol=None, conversation_id="conv-clarify"),
    )
    assert sink.of(SessionEvent)[0].conversation_id == "conv-clarify"


def test_session_event_absent_when_no_conversation():
    """没有会话时不得凭空造一个 id（那会让前端以为有会话可续）。"""
    from tradingagents.agent.events import SessionEvent

    _, sink = _run(QueueModel([_grounded_answer]), request=_request(conversation_id=None))
    assert sink.of(SessionEvent) == []


# ══════════════════════════════════════════════════════════════════════════
# 历史注入
# ══════════════════════════════════════════════════════════════════════════


def test_history_is_injected_into_the_prompt():
    """历史必须真的进提示词——否则模型无法消解「它」「这只」这类指代。"""
    seen: list[str] = []

    class _Recorder(QueueModel):
        def propose(self, *, prompt, evidence, verdict):
            seen.append(prompt)
            return super().propose(prompt=prompt, evidence=evidence, verdict=verdict)

    history = (
        {"role": "user", "content": "600519 是不是在洗盘"},
        {"role": "assistant", "content": "判定为洗盘（震仓），置信 61。"},
    )
    _run(
        _Recorder([_grounded_answer]),
        request=_request(text="那它还能拿吗", conversation_id="conv-h", history=history),
    )

    assert seen, "模型未被调用"
    prompt = seen[0]
    assert "600519 是不是在洗盘" in prompt
    assert "判定为洗盘" in prompt
    assert "那它还能拿吗" in prompt


def test_history_is_ordered_and_bounded():
    """历史必须按时间序、且只带最近 N 轮——无限历史会撑爆提示词预算。"""
    from tradingagents.agent.loop import MAX_HISTORY_TURNS, _build_prompt

    history = tuple(
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"片段{i}"}
        for i in range(40)
    )
    prompt = _build_prompt(
        _request(text="当前问题"),
        __import__("tradingagents.grounding.types", fromlist=["EvidenceSet"]).EvidenceSet(),
        __import__("tradingagents.agent.middleware", fromlist=["TurnContext"]).TurnContext(
            user_id="u1", tier="medium", text="当前问题"
        ),
        (),
        history=history,
    )

    # 最新的必须在，最旧的被截断
    assert "片段39" in prompt
    assert "片段0" not in prompt
    assert prompt.index("片段38") < prompt.index("片段39")
    assert MAX_HISTORY_TURNS > 0


def test_no_history_leaves_prompt_unchanged_behaviour():
    """无历史时提示词不出现空的「历史」段落——不留噪声。"""
    from tradingagents.agent.loop import _build_prompt
    from tradingagents.agent.middleware import TurnContext
    from tradingagents.grounding.types import EvidenceSet

    ctx = TurnContext(user_id="u1", tier="medium", text="q")
    with_none = _build_prompt(_request(text="q"), EvidenceSet(), ctx, (), history=())
    assert "历史" not in with_none


def test_empty_history_entries_are_skipped():
    """空内容不得产生空行——历史里混着空消息是常态（取消的回合）。"""
    from tradingagents.agent.loop import _build_prompt
    from tradingagents.agent.middleware import TurnContext
    from tradingagents.grounding.types import EvidenceSet

    ctx = TurnContext(user_id="u1", tier="medium", text="q")
    prompt = _build_prompt(
        _request(text="q"),
        EvidenceSet(),
        ctx,
        (),
        history=({"role": "user", "content": ""}, {"role": "user", "content": "有内容"}),
    )
    assert "有内容" in prompt
    assert "\n\n\n" not in prompt


# ══════════════════════════════════════════════════════════════════════════
# 端到端：指代消解
# ══════════════════════════════════════════════════════════════════════════


def test_second_turn_can_refer_to_the_first_turn_symbol():
    """「那它还能拿吗」必须能拿到上一轮的标的与判定上下文。

    这是"多轮"与"连续两次单轮"的区别所在。
    """
    seen: list[str] = []

    class _Recorder(QueueModel):
        def propose(self, *, prompt, evidence, verdict):
            seen.append(prompt)
            return super().propose(prompt=prompt, evidence=evidence, verdict=verdict)

    history = (
        {"role": "user", "content": "600519 是不是在洗盘"},
        {"role": "assistant", "content": "判定为洗盘（震仓），置信 61；失效条件：次日放量下跌。"},
    )
    result, _ = _run(
        _Recorder([_grounded_answer]),
        request=_request(text="那它还能拿吗", conversation_id="conv-ref", history=history),
    )

    assert "600519" in seen[0]
    assert "震仓" in seen[0]
    assert result.conversation_id == "conv-ref"
    assert result.tool_calls == ("signal_washout_probe",)


# ══════════════════════════════════════════════════════════════════════════
# 服务层：历史加载与用户隔离
# ══════════════════════════════════════════════════════════════════════════


def test_service_loads_history_for_the_same_conversation():
    from api.database import ChatConversationDB, ChatMessageDB, get_db_ctx
    from api.services.chat_service import load_recent_history
    from tradingagents.agent.repository import new_id

    cid = new_id()
    with get_db_ctx() as db:
        db.add(ChatConversationDB(id=cid, user_id="hist-u1", title="t"))
        db.add(ChatMessageDB(id=new_id(), conversation_id=cid, user_id="hist-u1",
                             role="user", content="600519 是不是在洗盘"))
        db.add(ChatMessageDB(id=new_id(), conversation_id=cid, user_id="hist-u1",
                             role="assistant", content="判定为洗盘（震仓）"))
        db.commit()

    history = load_recent_history(user_id="hist-u1", conversation_id=cid)
    assert [h["role"] for h in history] == ["user", "assistant"]
    assert "洗盘" in history[0]["content"]


def test_service_history_is_isolated_per_user():
    """历史读取必须限定 user_id：跨用户读取会把别人的问题喂进本次提示词。

    这是最严重的一类串号——不是数据泄露到界面，而是**泄露进模型的上下文**。
    """
    from api.database import ChatConversationDB, ChatMessageDB, get_db_ctx
    from api.services.chat_service import load_recent_history
    from tradingagents.agent.repository import new_id

    cid = new_id()
    with get_db_ctx() as db:
        db.add(ChatConversationDB(id=cid, user_id="owner-u"))
        db.add(ChatMessageDB(id=new_id(), conversation_id=cid, user_id="owner-u",
                             role="user", content="我的持仓成本是 1900"))
        db.commit()

    assert load_recent_history(user_id="intruder", conversation_id=cid) == ()


def test_service_history_skips_blank_messages():
    from api.database import ChatConversationDB, ChatMessageDB, get_db_ctx
    from api.services.chat_service import load_recent_history
    from tradingagents.agent.repository import new_id

    cid = new_id()
    with get_db_ctx() as db:
        db.add(ChatConversationDB(id=cid, user_id="blank-u"))
        db.add(ChatMessageDB(id=new_id(), conversation_id=cid, user_id="blank-u",
                             role="assistant", content="   "))
        db.add(ChatMessageDB(id=new_id(), conversation_id=cid, user_id="blank-u",
                             role="user", content="有内容"))
        db.commit()

    history = load_recent_history(user_id="blank-u", conversation_id=cid)
    assert [h["content"] for h in history] == ["有内容"]


def test_service_history_empty_without_conversation():
    from api.services.chat_service import load_recent_history

    assert load_recent_history(user_id="u", conversation_id="") == ()


# ══════════════════════════════════════════════════════════════════════════
# HTTP 层：整条链路（最强证据）
# ══════════════════════════════════════════════════════════════════════════


def test_http_two_turns_share_one_conversation_and_inject_history(monkeypatch):
    """两轮 HTTP 请求必须落在同一会话，且第二轮提示词含第一轮内容。

    这是 A1 修复的最强证据：单元测试只能证明零件可用，只有走完整 HTTP 链路
    才能证明「id 下发 → 前端回传 → 历史加载 → 注入提示词」这条链没断。
    """
    import json

    from fastapi.testclient import TestClient

    from api.services import chat_service, signal_service
    from tests.test_acceptance_conversational_signal_agent import _ports_factory
    from tradingagents.tools import build_registry

    factory = _ports_factory()
    prompts: list[str] = []

    class _RecordingModel:
        def propose(self, *, prompt, evidence, verdict):
            prompts.append(prompt)
            from tradingagents.grounding.types import Claim, StructuredAnswer

            ref = evidence.items[0].id if evidence.items else None
            claims = (Claim(text="反抽量能恢复至 1.228", refs=(ref,)),) if ref else ()
            # 与真实模型同一依据：提示里列出了「必须声明的未覆盖维度」。
            # 该测试用户没有持仓 → 列表含 position，不声明就会被校验器拒绝。
            unavailable = list(verdict.unavailable) if verdict else []
            if "position" in prompt and "position" not in unavailable:
                unavailable.append("position")
            return StructuredAnswer(
                summary="缩量下杀后出现承接。",
                claims=claims,
                verdict_ref=verdict.id if verdict else None,
                declared_unavailable=tuple(unavailable),
            )

        def repair(self, **kw):  # pragma: no cover
            return kw["previous"]

    monkeypatch.setattr(signal_service, "build_ports_factory", lambda user_id=None: factory)
    monkeypatch.setattr(chat_service, "build_ports_factory", lambda user_id=None: factory)
    monkeypatch.setattr(
        chat_service, "build_registry",
        lambda user_id=None: build_registry(ports_factory=factory, news_fetcher=lambda *a: []),
    )
    monkeypatch.setattr(chat_service, "build_answer_model", lambda **kw: _RecordingModel())

    from api.main import app

    client = TestClient(app, raise_server_exceptions=False)
    code = client.post("/v1/auth/request-code", json={"email": "multiturn-http@test.com"}).json()["dev_code"]
    jwt = client.post(
        "/v1/auth/verify-code", json={"email": "multiturn-http@test.com", "code": code}
    ).json()["access_token"]
    auth = {"Authorization": f"Bearer {jwt}"}

    def _events(response):
        out, name = [], None
        for line in response.text.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: ") and name:
                out.append((name, json.loads(line[6:])))
        return out

    first = client.post(
        "/v1/chat",
        json={"messages": [{"role": "user", "content": "600519 是不是在洗盘"}],
              "symbol": "600519.SH", "as_of": "2026-01-16"},
        headers=auth,
    )
    first_events = _events(first)
    session = [d for k, d in first_events if k == "SessionEvent"]
    assert session, "第一轮必须下发 SessionEvent"
    conversation_id = session[0]["conversation_id"]

    second = client.post(
        "/v1/chat",
        json={"messages": [{"role": "user", "content": "那它还能拿吗"}],
              "symbol": "600519.SH", "as_of": "2026-01-16",
              "conversation_id": conversation_id},
        headers=auth,
    )
    second_events = _events(second)
    assert [d for k, d in second_events if k == "SessionEvent"][0]["conversation_id"] == conversation_id

    # 第二轮提示词必须含第一轮的问与答
    assert len(prompts) >= 2, prompts
    assert "600519 是不是在洗盘" in prompts[-1], prompts[-1][:400]
    assert "缩量下杀后出现承接" in prompts[-1], prompts[-1][:400]
    assert "那它还能拿吗" in prompts[-1]

    # 两轮落在同一会话
    conversations = client.get("/v1/conversations", headers=auth).json()
    assert conversations["count"] == 1
    detail = client.get(f"/v1/conversations/{conversation_id}", headers=auth).json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant", "user", "assistant"]
