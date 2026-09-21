"""LangChain 适配器：ToolSpec → StructuredTool。

只依赖 `ToolSpec`，**不 import** `adapters/mcp.py`。两个适配器互不知道对方
存在，是「工具定义单一来源」的结构保证：任何一方想偷偷改描述或加字段，
都必须先改 `ToolSpec`，从而同时影响所有消费者。
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import Field, create_model

from ..spec import ToolSpec

_PY_TYPES: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _args_model(spec: ToolSpec):
    """由 JSON Schema 生成 pydantic 参数模型。

    LangChain 需要真实的 pydantic 模型做参数约束与工具调用解析；由 schema
    派生（而不是各写一份）保证「模型看到的参数」与「注册表校验的参数」一致。
    """
    schema = spec.input_schema
    properties: dict[str, Any] = schema.get("properties", {})
    required = set(schema.get("required", []))

    fields: dict[str, Any] = {}
    for name, prop in properties.items():
        py_type = _PY_TYPES.get(prop.get("type", "string"), str)
        description = prop.get("description", "")
        if name in required:
            fields[name] = (py_type, Field(..., description=description))
        else:
            fields[name] = (py_type, Field(default=prop.get("default"), description=description))

    return create_model(f"{spec.name}_args", **fields)


def to_langchain_tool(spec: ToolSpec) -> StructuredTool:
    """把 ToolSpec 物化成 LangChain 工具。"""
    return StructuredTool.from_function(
        func=spec.handler,
        name=spec.name,
        description=spec.description,
        args_schema=_args_model(spec),
    )
