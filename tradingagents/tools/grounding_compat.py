"""把超时/失败统一转成 `ToolResult` 的小工具。

单独成模块是为了让 `registry.py` 不直接依赖 `grounding` 的细节，也避免
「超时结果」与「正常结果」的构造逻辑散落两处。
"""

from __future__ import annotations

from typing import Any

from tradingagents.grounding.types import Coverage


def degraded_tool_result(*, tool_name: str, reason: str, unavailable: tuple[str, ...] = ()) -> Any:
    """构造一个「结构化失败」结果。

    刻意返回 `ToolResult` 而不是抛异常：单个源慢/不可用只应让该维度降级，
    不应让整轮对话失败。
    """
    from .spec import ToolResult

    return ToolResult(
        data={"timeout": True, "tool": tool_name, "reason": reason},
        evidence=(),
        coverage=Coverage(available=(), unavailable=unavailable or (tool_name,)),
        degraded=True,
        degraded_reason=reason,
        freshness={key: {"source_key": key, "status": "error", "issue_type": "fetch_error"}
                   for key in (unavailable or (tool_name,))},
    )
