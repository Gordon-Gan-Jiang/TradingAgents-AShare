"""MCP 适配器：ToolSpec → MCP 工具描述。

## 为什么返回 dict 而不是 `mcp.types.Tool`

当前环境未安装官方 `mcp` SDK，而 MCP 的工具描述是一个稳定的小结构
（`name` / `description` / `inputSchema`）。返回等价的 dict 让适配器可以
在**没有 SDK 的情况下被单测覆盖**；引入 SDK 后只需 `mcp.types.Tool(**d)` 包装，
不需要改动本模块的逻辑与测试。

## 命名约定

工具名直接使用注册表名，**不做任何 sanitize 映射**。MCP 客户端对工具名有
字符集约束（`[a-zA-Z0-9_-]`），因此注册表本身就用下划线命名
（`signal_washout_probe`）。若允许点号命名空间，就需要一层「注册表名 → MCP 名」
的映射表——那正是「工具定义单一来源」想排除的漂移，只是换了个形式。
"""

from __future__ import annotations

from typing import Any

from ..spec import ToolSpec


def to_mcp_tool(spec: ToolSpec) -> dict[str, Any]:
    """把 ToolSpec 物化成 MCP 工具描述。"""
    return {
        "name": spec.name,
        "description": spec.description,
        "inputSchema": dict(spec.input_schema),
    }


def to_mcp_tools(specs) -> list[dict[str, Any]]:
    return [to_mcp_tool(spec) for spec in specs]
