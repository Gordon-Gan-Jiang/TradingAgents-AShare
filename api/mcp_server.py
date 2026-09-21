"""MCP 出口：把平台能力变成生态可调用项。

## 为什么先做「传输中立核心」

官方 `mcp` SDK 当前未安装，而 MCP 的工具描述、鉴权、白名单、审计这些**真正
有逻辑**的部分与传输无关。把它们先实现并测好，安装 SDK 后只需：

    from mcp.server.fastmcp import FastMCP
    server = FastMCP("alphapilot")
    for spec in list_exposed_tools():
        server.add_tool(make_mcp_handler(spec.name))
    app.mount(...)  # ⚠️ 必须与 FastAPI 的 lifespan 合并，见下

## 三条硬约束

1. **只暴露同步只读工具**。`pipeline_*` 是分钟级任务，放进同步工具面会造成
   MCP 客户端超时与重试风暴；写操作（自选/台账/配置）V1 一律不开放——
   外部 Agent 改用户状态的爆炸半径过大，且缺乏确认回路。
2. **鉴权复用既有 API Token**（`UserTokenDB` + `RequireUser(allow_api_token=True)`），
   不新建凭证体系。用户隔离天然成立，零新增鉴权代码。
3. **挂载必须合并 lifespan**。FastMCP 的 lifespan 与 `app.mount` 组合有已知
   陷阱（python-sdk#1712、fastmcp#1176），裸挂会导致 MCP 子应用的初始化与
   关闭不执行。
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from tradingagents.tools.adapters.mcp import to_mcp_tool
from tradingagents.tools.registry import ToolRegistry
from tradingagents.tools.spec import ToolResult, ToolSpec

logger = logging.getLogger(__name__)

SERVER_NAME = "alphapilot-ashare"
SERVER_VERSION = "0.1.0"

# 永不通过 MCP 暴露的工具（写操作与分钟级管道）
NEVER_EXPOSED: frozenset[str] = frozenset(
    {
        "analyze_pipeline",
        # 写操作：V1 一律不开放
        "watchlist_add",
        "watchlist_remove",
        "trade_ledger_append",
        "config_update",
    }
)

EXPOSURE_DENIED = "tool_not_exposed"


@dataclass(frozen=True)
class McpInvocation:
    tool: str
    user_id: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    duration_ms: int = 0
    ok: bool = True
    error: str | None = None


class McpAuditLog:
    """调用审计。只记工具名/用户/时长/结果，**绝不记录凭证**。"""

    def __init__(self) -> None:
        self._entries: list[McpInvocation] = []

    def record(self, invocation: McpInvocation) -> None:
        self._entries.append(invocation)
        logger.info(
            "mcp tool=%s user=%s ok=%s duration_ms=%s",
            invocation.tool,
            invocation.user_id,
            invocation.ok,
            invocation.duration_ms,
        )

    def entries(self) -> tuple[McpInvocation, ...]:
        return tuple(self._entries)

    def usage(self) -> dict[str, dict[str, Any]]:
        """按工具聚合的调用量与延迟分布，便于容量分析。"""
        summary: dict[str, dict[str, Any]] = {}
        for entry in self._entries:
            bucket = summary.setdefault(entry.tool, {"calls": 0, "failures": 0, "latencies": []})
            bucket["calls"] += 1
            bucket["latencies"].append(entry.duration_ms)
            if not entry.ok:
                bucket["failures"] += 1
        for bucket in summary.values():
            latencies = sorted(bucket.pop("latencies"))
            if latencies:
                bucket["p50_ms"] = latencies[len(latencies) // 2]
                bucket["max_ms"] = latencies[-1]
        return summary


class McpServerCore:
    """MCP 服务的传输中立核心。

    职责：工具白名单、调用分发、鉴权前置校验、审计。
    不负责传输（HTTP/stdio）与 lifespans——那些交给 SDK 适配层。
    """

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        audit: McpAuditLog | None = None,
        exposes: Sequence[str] | None = None,
        never_exposed: frozenset[str] = NEVER_EXPOSED,
    ) -> None:
        self._registry = registry
        self._audit = audit or McpAuditLog()
        self._never_exposed = never_exposed
        self._explicit = set(exposes) if exposes is not None else None

    # ── 白名单 ────────────────────────────────────────────────────────────

    def is_exposed(self, name: str) -> bool:
        spec = self._registry.get(name)
        if spec is None:
            return False
        if name in self._never_exposed:
            return False
        if not spec.is_synchronous:
            return False
        if self._explicit is not None and name not in self._explicit:
            return False
        return True

    def list_tools(self) -> list[dict[str, Any]]:
        """MCP 工具列表。名称即注册表名，无需 sanitize 映射。"""
        return [to_mcp_tool(spec) for spec in self._registry if self.is_exposed(spec.name)]

    @property
    def audit(self) -> McpAuditLog:
        return self._audit

    # ── 分发 ──────────────────────────────────────────────────────────────

    def invoke(
        self,
        *,
        name: str,
        arguments: Mapping[str, Any],
        user_id: str | None,
    ) -> ToolResult:
        """执行一次 MCP 调用。

        鉴权在传输层完成（复用既有 API Token），这里只做**兜底断言**：
        没有解析出用户就不允许执行。缺了这条，任何一处忘记加依赖的挂载
        都会变成匿名读路径。
        """
        if not user_id:
            raise PermissionError("mcp invocation requires an authenticated user")

        started = time.monotonic()
        if not self.is_exposed(name):
            duration = int((time.monotonic() - started) * 1000)
            self._audit.record(
                McpInvocation(tool=name, user_id=user_id, arguments=dict(arguments),
                              duration_ms=duration, ok=False, error=EXPOSURE_DENIED)
            )
            raise PermissionError(f"{EXPOSURE_DENIED}: {name}")

        try:
            result = self._registry.invoke(name, **dict(arguments))
        except Exception as exc:  # noqa: BLE001
            duration = int((time.monotonic() - started) * 1000)
            self._audit.record(
                McpInvocation(tool=name, user_id=user_id, arguments=dict(arguments),
                              duration_ms=duration, ok=False, error=f"{type(exc).__name__}: {exc}")
            )
            raise
        duration = int((time.monotonic() - started) * 1000)
        self._audit.record(
            McpInvocation(tool=name, user_id=user_id, arguments=dict(arguments),
                          duration_ms=duration, ok=True)
        )
        return result

    def exposed_names(self) -> tuple[str, ...]:
        """对外暴露的工具名（与 `list_tools()` 同一判断，避免两处定义漂移）。"""
        return tuple(spec.name for spec in self._registry if self.is_exposed(spec.name))

    def describe(self) -> dict[str, Any]:
        return {
            "server": SERVER_NAME,
            "version": SERVER_VERSION,
            "capabilities": {"tools": {}},
            "tools": self.list_tools(),
        }


def build_core(
    *,
    ports_factory: Callable[[str, str], Any] | None = None,
    audit: McpAuditLog | None = None,
) -> McpServerCore:
    """按组合根装配 MCP 核心。未注入端口时不装配信号工具。"""
    from tradingagents.tools import build_registry

    return McpServerCore(build_registry(ports_factory=ports_factory), audit=audit)


def mount_instructions() -> str:
    """挂载说明。写成代码而不是注释，便于健康检查端点把它暴露出来。"""
    return (
        "MCP 传输层需要安装官方 SDK（pip install mcp）后挂载到 FastAPI。\n"
        "关键约束：必须把 MCP 子应用的 lifespan 与主应用合并——"
        "FastMCP 的 lifespan 与 app.mount 组合有已知陷阱"
        "（python-sdk#1712 / fastmcp#1176），裸挂会导致子应用不初始化也不关闭。\n"
        "鉴权复用既有 API Token（RequireUser(allow_api_token=True)），不新建凭证体系。"
    )


# ---------------------------------------------------------------------------
# 传输层：官方 MCP SDK（streamable-http）
#
# ## 实测到的 SDK 现实
#
# 设计文档写的是 v1 的 `FastMCP`；实际安装的是 **mcp 2.2.0**，`FastMCP` 已改名
# `MCPServer`（`mcp.server.mcpserver`）。本段按 v2 API 实现，并把这个偏差
# 记入 design.md 的实施记录。
#
# ## 必须合并 lifespan（本模块最容易踩的坑）
#
# `MCPServer.streamable_http_app()` 返回的 Starlette 应用自带
# `lifespan=lambda app: session_manager.run()`，而 **FastAPI 不会运行被挂载
# 子应用的 lifespan**。裸 `app.mount(...)` 的结果是 session manager 永不启动：
# MCP 端点能连通，但任何会话初始化都会失败——这类"连得上、用不了"的故障
# 排查成本极高。因此本模块显式提供 `run_mcp_lifespan()`，由主应用在 lifespan
# 里进入。
# ---------------------------------------------------------------------------

import contextvars

# 挂载点。客户端应使用带尾斜杠的 `/mcp/`：FastAPI 对挂载点的**无尾斜杠 POST**
# 返回 405（请求不会进入子应用），而 `/mcp/` 正常。为减少踩坑，另有一条
# 308 重定向把 `/mcp` 引到 `/mcp/`（见 api/routers/mcp.py）。
MCP_PATH = "/mcp"
MCP_ENDPOINT = "/mcp/"

# 当前请求的已解析用户。工具在调用时读取它，因此鉴权失败不可能"漏过"到工具层。
_current_user: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mcp_current_user", default=None
)


def current_mcp_user() -> str | None:
    return _current_user.get()


def resolve_api_token(token: str | None) -> str | None:
    """用既有 API Token 解析用户。**不新建凭证体系**（design D10）。

    复用 `UserTokenDB` 与 `token_service.verify_token`，因此吊销、过期、
    用户隔离的语义与网页端完全一致。
    """
    if not token:
        return None
    try:
        from api.database import get_db_ctx
        from api.services import token_service

        prefix = getattr(token_service, "TOKEN_PREFIX", "ap_")
        if not token.startswith(prefix):
            return None
        with get_db_ctx() as db:
            user = token_service.verify_token(db, token)
            if user is None or not getattr(user, "is_active", True):
                return None
            return str(user.id)
    except Exception as exc:  # noqa: BLE001 — 鉴权失败一律视为未认证
        logger.info("mcp token resolution failed: %s: %s", type(exc).__name__, exc)
        return None


class McpAuthMiddleware:
    """ASGI 层鉴权：未认证的请求在进入 MCP 应用之前就被拒绝。

    放在 ASGI 层而不是工具内部，是为了保证**没有匿名读路径**——工具根本无法
    在未认证的情况下被到达，而不是"每个工具都记得检查"。
    """

    def __init__(self, app: Any, *, resolver: Callable[[str | None], str | None] | None = None) -> None:
        self.app = app
        self._resolve = resolver or resolve_api_token

    async def __call__(self, scope: Mapping[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        token: str | None = None
        for key, value in scope.get("headers") or []:
            if key == b"authorization":
                raw = value.decode("latin-1")
                if raw.lower().startswith("bearer "):
                    token = raw[7:].strip()
                break

        user_id = self._resolve(token)
        if not user_id:
            await _send_unauthorized(send)
            return

        reset = _current_user.set(user_id)
        try:
            await self.app(scope, receive, send)
        finally:
            _current_user.reset(reset)


async def _send_unauthorized(send: Any) -> None:
    body = json.dumps({"error": "unauthorized", "detail": "需要有效的 API Token"}).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


_PY_ANNOTATIONS = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "array": "list",
    "object": "dict",
}


def _make_handler(spec: ToolSpec, dispatch: Callable[[str, Mapping[str, Any]], Any]) -> Callable[..., Any]:
    """按 ToolSpec 的 JSON Schema 生成一个**显式签名**的处理函数。

    MCP SDK 从函数签名推导工具的输入 schema；写成 `def handler(**kwargs)`
    会让它推导出一个无参数的工具，于是任何调用都返回参数校验错误。
    因此这里按声明生成参数列表——schema 仍然只有一份来源（ToolSpec），
    生成出来的签名只是它在 Python 侧的投影。
    """
    properties: Mapping[str, Any] = spec.input_schema.get("properties", {})
    required = set(spec.input_schema.get("required", []))

    parameters: list[str] = []
    for arg_name, prop in properties.items():
        annotation = _PY_ANNOTATIONS.get(prop.get("type", "string"), "str")
        if arg_name in required:
            parameters.append(f"{arg_name}: {annotation}")
        else:
            parameters.append(f"{arg_name}: {annotation} = {prop.get('default')!r}")

    source = (
        f"def {spec.name}({', '.join(parameters)}):\n"
        f"    return __dispatch({spec.name!r}, dict(locals()))\n"
    )
    namespace: dict[str, Any] = {"__dispatch": dispatch}
    exec(compile(source, f"<mcptool:{spec.name}>", "exec"), namespace)
    handler = namespace[spec.name]
    handler.__doc__ = spec.description
    return handler


def build_mcp_server(
    *,
    registry_provider: Callable[[], Any] | None = None,
    ports_factory: Callable[[str, str], Any] | None = None,
    news_fetcher: Callable[..., Any] | None = None,
    audit: McpAuditLog | None = None,
) -> tuple[Any, McpServerCore]:
    """构造 MCP 服务器与其核心。

    只注册同步只读工具（与 `McpServerCore.list_tools()` 同一判断），因此
    「哪些能力对外可见」只有一处定义。
    """
    from mcp.server.mcpserver import MCPServer

    from tradingagents.tools import build_registry
    from tradingagents.tools.evidence import default_market_fetchers

    # 注册表在**调用时**才构造：主应用在 import 期挂载，而端口工厂需要应用层
    # 配置；惰性解析让注入（以及测试替换）都能生效，也让 import 期不必触网。
    #
    # L2 市场证据工具（筹码/分时/资金流）在这里显式注入**引擎既有取数实现**：
    # 它们不依赖用户维度，MCP 面可以直接用。自选清单与时效探针需要 user_id 闭包
    # 绑定（MCP 的用户要到调用时才解析），这里刻意不注入——「未注入就不注册」
    # 由 build_registry 保证，不留一调就失败的条目。
    if registry_provider is not None:
        registry = registry_provider()
    else:
        registry = build_registry(
            ports_factory=ports_factory,
            news_fetcher=news_fetcher,
            evidence_fetchers=default_market_fetchers(),
        )
    core = McpServerCore(registry, audit=audit)

    server = MCPServer(
        name=SERVER_NAME,
        version=SERVER_VERSION,
        instructions=(
            "AlphaPilot A-Share 的确定性研判能力。所有结果由信号层计算，"
            "不接受模型改写；数据未覆盖的维度会显式列出。研究结论不构成投资建议。"
        ),
    )

    def _dispatch(name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        user_id = current_mcp_user()
        result = core.invoke(name=name, arguments=arguments, user_id=user_id)
        return {
            "data": dict(result.data),
            "evidence": [dict(e.__dict__) for e in result.evidence],
            "coverage": {
                "available": list(result.coverage.available),
                "unavailable": list(result.coverage.unavailable),
            },
            "freshness": dict(result.freshness),
            "degraded": result.degraded,
            "degraded_reason": result.degraded_reason,
            "as_of": result.as_of,
            "disclaimer": result.disclaimer,
        }

    for name in core.exposed_names():
        spec = registry.require(name)
        server.add_tool(
            _make_handler(spec, _dispatch),
            name=spec.name,
            title=spec.title,
            description=spec.description,
        )

    return server, core


def build_mcp_asgi(*, resolver: Callable[[str | None], str | None] | None = None, **kw: Any) -> Any:
    """构造可直接挂载的 ASGI 应用（已包鉴权中间件）。"""
    server, _core = build_mcp_server(**kw)
    # 子应用内部路径设为 "/"：它会被挂载到 MCP_PATH 之下，若内部也用 "/mcp"
    # 就会出现双重前缀（实际端点变成 /mcp/mcp），客户端拿到 405 而非预期响应。
    app = server.streamable_http_app(streamable_http_path="/")
    wrapped = McpAuthMiddleware(app, resolver=resolver)
    wrapped.mcp_server = server  # 供 lifespan 取 session_manager
    return wrapped


@contextlib.asynccontextmanager
async def run_mcp_lifespan(asgi_app: Any):
    """进入 MCP 的 session manager。

    **必须**由主应用在 lifespan 里调用：FastAPI 不运行被挂载子应用的 lifespan，
    而 MCP 的 streamable-http 会话依赖 `session_manager.run()`。缺了它，
    端点能连通但会话初始化失败——"连得上、用不了"是最难排查的一类故障。

    MCP 不可用时（未安装 SDK、装饰失败等）只记录日志并让主应用继续启动：
    研判能力不应因为一个可选出口而不可用。
    """
    server = getattr(asgi_app, "mcp_server", None)
    if server is None:
        yield
        return
    try:
        async with server.session_manager.run():
            yield
    except Exception as exc:  # noqa: BLE001
        logger.warning("MCP session manager unavailable, continuing without MCP: %s", exc)
        yield


def mount_mcp(app: Any, *, asgi_app: Any | None = None, **kw: Any) -> Any | None:
    """把 MCP 出口挂到 FastAPI 应用上。

    返回可挂载的 ASGI 应用（供主应用在 lifespan 里使用）；失败时返回 None
    并记录原因，绝不让可选出口阻断主应用启动。
    """
    try:
        mounted = asgi_app or build_mcp_asgi(**kw)
        app.mount(MCP_PATH, mounted)
        logger.info("MCP endpoint mounted at %s", MCP_PATH)
        return mounted
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to mount MCP endpoint: %s: %s", type(exc).__name__, exc)
        return None
