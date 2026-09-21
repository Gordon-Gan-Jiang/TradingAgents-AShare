"""回合崩溃时的对外表现：**必须能被用户看见，且必须结束**。

## 为什么需要这个文件

线上实测（`POST /v1/chat`，账号未配置模型）拿到的是：

    event: _CrashEvent
    data: {"value": "<api.routers.chat._CrashEvent object at 0x129dac980>"}

三个问题叠在一起：

1. `_CrashEvent` 不是 dataclass，`_serialize` 走了 `{"value": str(event)}` 兜底，
   **崩溃原因整条丢失**，客户端只拿到一个内存地址（还泄漏了服务端堆布局）。
2. 事件名 `_CrashEvent` 不在前端 `chatStream.ts` 的 switch 里，落到 `default:`
   被**静默忽略**；同时**没有发 `EndEvent`**，于是 `finished` 永远为 false——
   界面永久转圈，用户既看不到错误也等不到结束。
3. HTTP 状态是 200，所以"服务端崩了"这件事在状态码上完全不可见。

因此：崩溃必须以**前端已认识的事件**（`ErrorEvent`）说明原因，并以
`EndEvent(status="failed")` 收尾。测试直接锁住这三点。
"""

from __future__ import annotations

import json


def _sse_events(text: str) -> list[tuple[str, dict]]:
    """把 SSE 文本解析成 (event_name, data) 列表。"""
    out: list[tuple[str, dict]] = []
    name = None
    for line in text.splitlines():
        if line.startswith("event: "):
            name = line[len("event: "):].strip()
        elif line.startswith("data: ") and name is not None:
            out.append((name, json.loads(line[len("data: "):])))
    return out


def _post_chat(client, auth, message: str):
    return client.post(
        "/v1/chat",
        json={"messages": [{"role": "user", "content": message}]},
        headers=auth,
    )


def _client_and_auth():
    from fastapi.testclient import TestClient

    from api.main import app
    from tests.test_symbol_resolution import _jwt

    return TestClient(app, raise_server_exceptions=False), {"Authorization": f"Bearer {_jwt()}"}


def test_crash_emits_error_event_with_reason(monkeypatch):
    """崩溃原因必须到达客户端，而不是 `<object at 0x...>`。"""
    import openai

    from api.services import chat_service

    def _boom(**kwargs):
        raise openai.OpenAIError(
            "The api_key client option must be set either by passing api_key "
            "to the client or by setting the OPENAI_API_KEY environment variable"
        )

    monkeypatch.setattr(chat_service, "build_answer_model", _boom)

    client, auth = _client_and_auth()
    resp = _post_chat(client, auth, "600519.SH 是不是在洗盘")
    assert resp.status_code == 200  # 流已开始，只能靠事件报错

    events = _sse_events(resp.text)
    names = [n for n, _ in events]

    assert "_CrashEvent" not in names, "内部兜底事件名不该出现在线上协议里"
    assert "ErrorEvent" in names, f"崩溃必须发 ErrorEvent，实际事件：{names}"

    message = next(d["message"] for n, d in events if n == "ErrorEvent")
    assert message.strip(), "错误消息不得为空"
    assert "object at 0x" not in message, "不得把 Python 对象 repr 泄漏给客户端"
    assert "<api." not in message, "不得泄漏服务端内部类名"


def test_crash_ends_the_stream(monkeypatch):
    """崩溃后必须发 EndEvent(failed)，否则前端永久 loading。"""
    from api.services import chat_service

    monkeypatch.setattr(
        chat_service, "build_answer_model",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    client, auth = _client_and_auth()
    resp = _post_chat(client, auth, "600519.SH 是不是在洗盘")

    events = _sse_events(resp.text)
    ends = [d for n, d in events if n == "EndEvent"]
    assert ends, "崩溃路径必须发 EndEvent，否则界面停在 loading"
    assert ends[-1]["status"] == "failed"


def test_missing_model_configuration_is_actionable(monkeypatch):
    """未配置模型是**用户可自行修复**的问题，提示必须告诉用户去配置。"""
    import openai

    from api.services import chat_service

    monkeypatch.setattr(
        chat_service, "build_answer_model",
        lambda **kw: (_ for _ in ()).throw(openai.OpenAIError("api_key client option must be set")),
    )

    client, auth = _client_and_auth()
    resp = _post_chat(client, auth, "600519.SH 是不是在洗盘")

    message = next(d["message"] for n, d in _sse_events(resp.text) if n == "ErrorEvent")
    assert "模型" in message, f"应提示这是模型配置问题，实际：{message!r}"
    assert "API" in message or "Key" in message or "密钥" in message


def test_describe_turn_failure_maps_known_causes():
    """分类映射要覆盖常见故障，且**不把异常原文**回给客户端。"""
    from api.services.chat_service import describe_turn_failure

    # 不以类名、只以报文特征识别的场景
    for text, expected in (
        ("invalid_api_key provided", "鉴权"),
        ("Rate limit reached for gpt-x", "限流"),
        ("Request timed out after 60s", "超时"),
    ):
        message = describe_turn_failure(RuntimeError(text + " (see https://internal.example/v1)"))
        assert expected in message, f"{text!r} → {message!r}"
        assert "internal.example" not in message, "不得把上游 URL 回传给客户端"

    # 以异常类名识别的场景（openai 的异常类需要 response/body，这里用同名替身）
    auth_error = type("AuthenticationError", (Exception,), {})("request failed")
    assert "鉴权" in describe_turn_failure(auth_error)

    unknown = describe_turn_failure(RuntimeError("secret-ish detail: /Users/ops/db.sqlite"))
    assert "RuntimeError" in unknown, "未知异常至少给出类型，便于用户报障"
    assert "db.sqlite" not in unknown, "未知异常的原文不得外泄"


def test_serialize_never_leaks_object_repr():
    """非 dataclass 事件不得退化成 `<X object at 0x...>`。"""
    from api.routers.chat import _serialize

    class _Weird:
        pass

    payload = _serialize(_Weird())
    assert payload["event"] == "_Weird"
    assert "0x" not in json.dumps(payload, ensure_ascii=False)
    assert payload["data"] == {}, "拿不到内容就交空对象并在服务端报错，不要泄漏 repr"


def test_every_protocol_event_is_a_dataclass():
    """结构性守卫：协议里的事件必须都是 dataclass。

    只要有一个不是，`_serialize` 就会走空兜底——事件名发出去了、内容却是空的，
    前端按 name 匹配到分支却读到 undefined。这正是 `_CrashEvent` 的事故形态，
    所以用一条结构断言把它挡在源头，而不是等线上再发现。
    """
    import dataclasses
    import inspect

    from tradingagents.agent import events as ev

    checked = []
    for name, obj in vars(ev).items():
        if not inspect.isclass(obj) or obj.__module__ != ev.__name__:
            continue
        if not name.endswith("Event"):
            continue
        assert dataclasses.is_dataclass(obj), (
            f"{name} 不是 dataclass：SSE 序列化会丢内容（见本文件顶部事故记录）"
        )
        checked.append(name)
    assert "SessionEvent" in checked and "EndEvent" in checked and "ErrorEvent" in checked


def _wire_fake_turn(monkeypatch, *, propose_raises: BaseException):
    """装配一个"模型会抛错"的回合，用于检查对外可见的错误文本。"""
    from api.services import chat_service, signal_service
    from tests.test_acceptance_conversational_signal_agent import _ports_factory
    from tradingagents.tools import build_registry

    factory = _ports_factory()
    monkeypatch.setattr(signal_service, "build_ports_factory", lambda user_id=None: factory)
    monkeypatch.setattr(chat_service, "build_ports_factory", lambda user_id=None: factory)
    monkeypatch.setattr(
        chat_service, "build_registry",
        lambda user_id=None: build_registry(ports_factory=factory, news_fetcher=lambda *a: []),
    )

    class _Model:
        def propose(self, **kwargs):
            raise propose_raises

        def repair(self, **kwargs):  # pragma: no cover
            return kwargs["previous"]

    monkeypatch.setattr(chat_service, "build_answer_model", lambda **kw: _Model())


def test_model_error_text_is_not_forwarded_raw(monkeypatch):
    """上游报错原文（含 request id / 内网 URL / 疑似密钥）不得回传客户端。"""
    _wire_fake_turn(
        monkeypatch,
        propose_raises=RuntimeError(
            "unauthorized: api_key=sk-live-SECRET at https://internal.example/v1 "
            "request id: 0217899885898891cf090e11a9bfabfa587f9990748ed82cffaa8"
        ),
    )

    client, auth = _client_and_auth()
    resp = _post_chat(client, auth, "600519.SH 是不是在洗盘")
    events = _sse_events(resp.text)

    errors = [d["message"] for n, d in events if n == "ErrorEvent"]
    assert errors, f"应发出 ErrorEvent，实际事件：{[n for n, _ in events]}"
    for message in errors:
        assert "sk-live-SECRET" not in message, "疑似密钥不得外泄"
        assert "internal.example" not in message, "内网地址不得外泄"
        assert "0217899885" not in message, "上游 request id 不得外泄"
    assert "鉴权" in errors[-1], f"401 类错误应提示鉴权问题，实际：{errors[-1]!r}"

    # EndEvent.reason 同样在协议里，也不能带原文
    for n, d in events:
        if n == "EndEvent":
            assert "sk-live-SECRET" not in str(d.get("reason", ""))


def test_unexpected_model_error_still_ends_and_reports_type(monkeypatch):
    """未知异常也要有可读提示 + 结束事件。"""
    _wire_fake_turn(monkeypatch, propose_raises=ValueError("boom"))

    client, auth = _client_and_auth()
    resp = _post_chat(client, auth, "600519.SH 是不是在洗盘")
    events = _sse_events(resp.text)

    message = next(d["message"] for n, d in events if n == "ErrorEvent")
    assert "ValueError" in message and "失败" in message
    ends = [d for n, d in events if n == "EndEvent"]
    assert ends and ends[-1]["status"] == "failed"
