"""MCP 出口（8.1/8.2/8.11/8.12）：SDK 挂载、鉴权、既有路由回归、端到端。

## 本文件验证的三件事

1. **lifespan 必须真的合并**。FastAPI 不运行被挂载子应用的 lifespan，而 MCP 的
   streamable-http 会话依赖 `session_manager.run()`。缺了合并，端点连得上但
   会话初始化失败——「连得上、用不了」是最难排查的一类故障。因此这里用
   **真实 uvicorn + 真实 MCP 客户端**做端到端，而不是只断言路由存在。
2. **没有匿名读路径**：未带 Token 的请求必须在进入 MCP 应用之前被拒绝。
3. **既有 HTTP 路由不受影响**：挂载是增量操作。

## 实测到的 SDK 版本偏差

设计文档写的是 v1 的 `FastMCP`；实际安装的是 **mcp 2.2.0**，`FastMCP` 已改名
`MCPServer`（`mcp.server.mcpserver`）。本文件的写法按 v2 API。
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time

import pytest
import uvicorn
from fastapi.testclient import TestClient


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _Server:
    """在后台线程跑真实 uvicorn。"""

    def __init__(self, app, port: int) -> None:
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="on")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self.port = port

    def __enter__(self):
        self._thread.start()
        deadline = time.time() + 20
        while time.time() < deadline:
            if getattr(self._server, "started", False):
                return self
            time.sleep(0.05)
        raise RuntimeError("uvicorn 未能在 20 秒内启动")

    def __exit__(self, *exc) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)


@pytest.fixture()
def wired(monkeypatch):
    """用内存端口装配工具，使 MCP 端到端不需要外网。"""
    from tests.test_acceptance_conversational_signal_agent import _ports_factory

    from api.services import chat_service, signal_service

    factory = _ports_factory()
    monkeypatch.setattr(signal_service, "build_ports_factory", lambda user_id=None: factory)
    monkeypatch.setattr(chat_service, "build_ports_factory", lambda user_id=None: factory)
    return factory


def _issue_token(client: TestClient, email: str) -> str:
    """登录并创建一个 API Token（MCP 面复用的正是这套凭证）。"""
    code = client.post("/v1/auth/request-code", json={"email": email}).json()["dev_code"]
    jwt = client.post("/v1/auth/verify-code", json={"email": email, "code": code}).json()["access_token"]
    created = client.post(
        "/v1/tokens",
        json={"name": "mcp-e2e"},
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert created.status_code in (200, 201), created.text
    body = created.json()
    return body.get("token") or body.get("plain_token") or body.get("value")


# ══════════════════════════════════════════════════════════════════════════
# 白名单与装配
# ══════════════════════════════════════════════════════════════════════════


def test_mcp_server_exposes_only_synchronous_tools(wired):
    from api.mcp_server import build_mcp_server

    server, core = build_mcp_server(ports_factory=wired, news_fetcher=lambda *a: [])
    names = core.exposed_names()

    assert "signal_washout_probe" in names
    assert "signal_washout_probe_batch" in names
    assert "data_symbol_news" in names
    # 迁移后的数据工具也在册；样本用 `get_fundamentals`（`get_stock_data` 已退役）。
    assert "get_fundamentals" in names
    # 昂贵管道与写操作不可达
    assert "analyze_pipeline" not in names


def test_exposed_names_match_list_tools(wired):
    """暴露名单只有一处定义：`exposed_names()` 与 `list_tools()` 必须一致。"""
    from api.mcp_server import build_mcp_server

    _server, core = build_mcp_server(ports_factory=wired, news_fetcher=lambda *a: [])
    assert set(core.exposed_names()) == {t["name"] for t in core.list_tools()}


# ══════════════════════════════════════════════════════════════════════════
# 8.11 既有路由回归
# ══════════════════════════════════════════════════════════════════════════


def test_mounting_does_not_break_existing_routes():
    """挂载 MCP 是增量操作：既有端点必须行为不变。"""
    from api.main import app

    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/healthz").status_code == 200
    # 既有对话入口仍在（未被新端点替换）
    assert client.post("/v1/chat/completions", json={"messages": []}).status_code in (400, 401, 422)


def test_mcp_endpoint_is_mounted(wired):
    from api.main import app

    paths = {getattr(route, "path", "") for route in app.routes}
    assert "/mcp" in paths


# ══════════════════════════════════════════════════════════════════════════
# 鉴权：无匿名读路径
# ══════════════════════════════════════════════════════════════════════════


def test_mcp_requires_a_token_before_reaching_the_app(wired, monkeypatch):
    """未带 Token 的请求必须在 ASGI 层被拒绝。

    放在 ASGI 层而不是工具内部，是为了保证工具**根本无法**在未认证时被到达，
    而不是「每个工具都记得检查」。
    """
    from api.mcp_server import McpAuthMiddleware

    seen: list[str] = []

    async def inner(scope, receive, send):  # pragma: no cover - 不应被调用
        seen.append("reached")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = McpAuthMiddleware(inner, resolver=lambda token: None)

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    asyncio.run(
        middleware(
            {"type": "http", "headers": [], "method": "POST", "path": "/mcp"},
            lambda: None,
            send,
        )
    )

    assert sent[0]["status"] == 401
    assert seen == [], "未认证请求不得进入 MCP 应用"


def test_mcp_middleware_sets_user_for_authenticated_requests():
    from api.mcp_server import McpAuthMiddleware, current_mcp_user

    observed: list[str | None] = []

    async def inner(scope, receive, send):
        observed.append(current_mcp_user())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = McpAuthMiddleware(inner, resolver=lambda token: "user-1" if token == "ok" else None)
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    asyncio.run(
        middleware(
            {"type": "http", "headers": [(b"authorization", b"Bearer ok")], "path": "/mcp"},
            lambda: None,
            send,
        )
    )
    assert sent[0]["status"] == 200
    assert observed == ["user-1"]
    # 请求结束后必须复位，避免串到下一个请求
    assert current_mcp_user() is None


def test_token_resolution_rejects_unknown_tokens():
    from api.mcp_server import resolve_api_token

    assert resolve_api_token(None) is None
    assert resolve_api_token("") is None
    assert resolve_api_token("not-a-platform-token") is None


# ══════════════════════════════════════════════════════════════════════════
# 8.12 端到端：真实 uvicorn + 真实 MCP 客户端
# ══════════════════════════════════════════════════════════════════════════


def _payload_of(result) -> dict:
    """从 CallToolResult 取结构化载荷。

    优先 `structured_content`（v2 对 dict 返回值的原生承载），回退到文本
    再解析——两者都试，避免把客户端版本差异误判成服务端问题。
    """
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict) and structured:
        return structured
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            return json.loads(text)
    raise AssertionError("MCP 工具未返回任何载荷")


def _mcp_only_app(factory, token: str = "test-token"):
    """只挂 MCP 的最小应用。

    ## 为什么不直接用 `api.main.app`

    `api.main` 的 lifespan 会在 shutdown 时关闭**模块级**线程池与调度任务。
    在测试进程里对它跑真实 uvicorn，会在测试中途把后续用例依赖的执行器关掉——
    这正是「单独跑通过、全量跑失败」的跨用例污染。这里用最小应用验证
    **MCP 传输本身**（挂载 + lifespan 合并 + 鉴权 + 工具调用），
    对真实应用的挂载则由 `test_mcp_endpoint_is_mounted` 与
    `test_mounting_does_not_break_existing_routes` 用 TestClient 断言。
    """
    from contextlib import asynccontextmanager

    from fastapi import FastAPI

    from api.mcp_server import build_mcp_asgi, run_mcp_lifespan

    asgi = build_mcp_asgi(
        resolver=lambda candidate: "mcp-test-user" if candidate == token else None,
        registry_provider=lambda: __import__(
            "tradingagents.tools", fromlist=["build_registry"]
        ).build_registry(ports_factory=factory, news_fetcher=lambda *a: []),
    )

    @asynccontextmanager
    async def lifespan(_app):
        # 与 api/main.py 相同的合并方式：FastAPI 不运行挂载子应用的 lifespan
        async with run_mcp_lifespan(asgi):
            yield

    app = FastAPI(lifespan=lifespan)
    app.mount("/mcp", asgi)
    return app


def _list_and_call(port: int, token: str, tool: str, arguments: dict):
    """用官方 SDK 客户端连接并调用一个工具。"""
    from mcp import ClientSession
    # mcp 2.x：客户端函数名是 `streamable_http_client`（v1 的
    # `streamablehttp_client` 已不存在），且 headers 由 http_client 承载。
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    async def run():
        url = f"http://127.0.0.1:{port}/mcp/"
        async with create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as http_client:
            async with streamable_http_client(url, http_client=http_client) as streams:
                read, write = streams[0], streams[1]
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    result = await session.call_tool(tool, arguments)
                    return tools, result

    return asyncio.run(run())


def test_mcp_end_to_end_over_real_transport(wired, monkeypatch):
    """真实 HTTP + 真实 MCP 客户端：列出工具并调用一次判定探针。

    这条用例是「lifespan 合并正确」的唯一可信证据——只断言路由存在的话，
    session manager 没启动也会"通过"。

    `NO_PROXY`：客户端连的是**本机**测试服务器，任何代理都不该介入。实测这台
    机器系统代理开着（Clash 类工具 127.0.0.1:7897），httpx 默认会读系统代理，
    于是 localhost 请求被接管、服务端收到 502 而 uvicorn 侧零异常——一条与代理
    毫无关系的用例因此变红。这里就地隔离，不依赖外部环境变量。
    """
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    token = "test-token"
    app = _mcp_only_app(wired, token=token)

    port = _free_port()
    with _Server(app, port):
        tools, result = _list_and_call(
            port, token, "signal_washout_probe",
            {"symbol": "600519.SH", "as_of": "2026-01-16"},
        )

    names = {t.name for t in tools.tools}
    assert "signal_washout_probe" in names
    # 样本用 `get_fundamentals`：`get_stock_data` 已从工具面退役。
    assert "get_fundamentals" in names
    assert "analyze_pipeline" not in names

    # mcp 2.x：字段名是 snake_case（is_error / structured_content）；
    # 返回 dict 的工具会同时给出 structured_content 与文本回退。
    assert result.is_error is False
    payload = _payload_of(result)
    assert payload["data"]["state"] in {
        "washout", "distribution", "trend_break", "insufficient_evidence"
    }
    assert payload["data"]["confidence"] is not None
    assert payload["coverage"]["available"] is not None
    assert payload["disclaimer"]


def test_mcp_end_to_end_rejects_requests_without_token(wired):
    """端到端验证无匿名读路径（真实 transport，不是中间件单测）。"""
    app = _mcp_only_app(wired)

    port = _free_port()
    with _Server(app, port):
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError

        request = Request(
            f"http://127.0.0.1:{port}/mcp/",
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode(),
            headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=10) as response:  # pragma: no cover
                status = response.status
        except HTTPError as exc:
            status = exc.code

    assert status == 401
