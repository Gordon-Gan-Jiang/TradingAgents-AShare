"""HTTP 路由分层。

`api/main.py` 已有 9591 行且零 `APIRouter` 使用；本变更新增的端点一律走这里，
由入口模块 `include_router` 挂载（由 `tests/test_architecture_boundaries.py`
强制）。把新端点继续写进入口模块，就是第三次重复同一个错误。
"""

from __future__ import annotations

__all__ = ["chat", "data_sources", "signals"]
