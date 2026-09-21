"""共享的 FastAPI 依赖。

## 为什么单独一个模块

`api/main.py` 已有 9591 行、零 `APIRouter`。新端点必须走 `api/routers/*`，
但它们需要与既有端点**同一套**鉴权语义（JWT 优先、API Token 兜底、用户隔离）。

如果路由模块直接 `from api.main import _require_api_user`，会形成
main → routers → main 的循环导入。这里用**函数内延迟导入**打破循环：
模块加载时只注册一个依赖函数，真正取鉴权实现是在请求到来时。

这样做的收益是：不搬迁 `RequireUser`（避免改动既有鉴权路径、扩大回归面），
同时新路由与老端点复用同一个实例，语义不会分叉。
"""

from __future__ import annotations

from typing import Optional

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

# 与 api/main.py:1894 的 _auth_scheme 等价（auto_error=False → 缺失凭证时
# 由业务层返回统一的 401 文案，而不是 FastAPI 的默认 403）
_bearer = HTTPBearer(auto_error=False)


def current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
):
    """解析当前用户。委托给既有的 `_require_api_user`，保持语义一致。"""
    from api.main import _require_api_user

    return _require_api_user(credentials)
