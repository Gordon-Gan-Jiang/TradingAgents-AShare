"""MCP 挂载点的无尾斜杠重定向。

## 为什么需要它

FastAPI 对**挂载点**的无尾斜杠 POST 返回 405（请求不会进入子应用），而
`/mcp/` 正常。真实 MCP 客户端通常不会自动跟随重定向，因此正式端点以
`/mcp/` 为准；这里的 308 只是让浏览器/curl 用户不被 405 迷惑。

308 会保留方法与请求体，因此即使某个客户端跟随它也仍然可用。
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import RedirectResponse

from api.mcp_server import MCP_ENDPOINT, MCP_PATH

router = APIRouter(tags=["mcp"], include_in_schema=False)


@router.api_route(MCP_PATH, methods=["GET", "POST", "DELETE", "OPTIONS"])
async def mcp_trailing_slash_redirect() -> RedirectResponse:
    """把 `/mcp` 引到 `/mcp/`（308 保留方法与请求体）。"""
    return RedirectResponse(url=MCP_ENDPOINT, status_code=308)
