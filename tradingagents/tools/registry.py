"""工具注册表：能力定义的单一来源。

## 为什么需要一个注册表而不是散落的 @tool

仓库现有 19 个 `@tool` 分散在 `agents/utils/*_tools.py`，通过 `ToolNode`
硬绑定给固定分析角色。这对「角色固定、提示词固定」的管道是够用的，但一旦
要让对话 Agent、MCP 外部调用、HTTP 直取共享能力，就会出现三套并行的定义，
必然漂移。

注册表把「有哪些能力」收敛成一个可枚举、可导出 schema、可校验参数的集合，
三个消费者各自用自己的适配器从它派生。

## 参数校验为什么必须在这里做

若把校验交给 handler，失败点会从「参数错误」变成「运行时异常」，
错误信息对模型完全不可用（模型无法据此修正调用）。注册表在 handler
之前用**导出时同一份 schema** 校验，保证「导出的契约」与「执行的契约」不分离。
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Any, Iterator

from .freshness import evaluate_tool_sources
from .grounding_compat import degraded_tool_result
from .spec import ToolResult, ToolSpec

logger = logging.getLogger(__name__)

# JSON Schema 基础类型 → Python 类型。只覆盖工具实际会用到的子集，
# 保持校验确定性、零外部依赖。
_TYPE_MAP: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list, tuple),
    "object": (dict,),
}


class ToolRegistry:
    """能力注册表。刻意做成显式组装（见 `build_registry`），不用装饰器自动注册。

    自动注册依赖 import 顺序，且在测试里难以构造干净实例——一个全局可变
    注册表会在 pytest 并发与乱序下互相污染，这正是本仓库已经踩过的坑
    （`dataflows/config.py` 的 `global _config`）。
    """

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(
                f"duplicate tool registration: {spec.name!r} "
                f"(existing={self._specs[spec.name].output_contract}, new={spec.output_contract})"
            )
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def require(self, name: str) -> ToolSpec:
        spec = self.get(name)
        if spec is None:
            raise KeyError(f"unknown tool: {name!r}")
        return spec

    def names_by_layer(self, layer: str) -> tuple[str, ...]:
        """按工具面分层取清单。

        规划层据此决定注入哪些工具：L1 常驻（数量少、语义强），L2 按意图检索
        （数量多，全量注入会撑爆上下文并降低选择准确率）。
        """
        return tuple(
            spec.name for spec in self._specs.values()
            if getattr(spec, "plan_layer", "L2") == layer
        )

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._specs))

    def __iter__(self) -> Iterator[ToolSpec]:
        return iter(self._specs[name] for name in self.names())

    def __len__(self) -> int:
        return len(self._specs)

    def synchronous(self) -> tuple[ToolSpec, ...]:
        """同步面上可用的工具。`expensive` 工具被排除。"""
        return tuple(spec for spec in self if spec.is_synchronous)

    def schemas(self) -> list[dict[str, Any]]:
        """导出同步工具及其输入 schema（与调用校验使用同一份）。"""
        return [spec.as_descriptor() for spec in self.synchronous()]

    def validate(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """按声明 schema 校验并填充默认值。"""
        spec = self.require(name)
        schema = spec.input_schema
        properties: dict[str, Any] = schema.get("properties", {})
        required: list[str] = list(schema.get("required", []))

        unknown = [key for key in arguments if key not in properties]
        if unknown:
            raise ValueError(f"unknown arguments for {name}: {unknown}")

        missing = [key for key in required if key not in arguments]
        if missing:
            raise ValueError(f"missing required arguments for {name}: {missing}")

        resolved: dict[str, Any] = {}
        for key, prop in properties.items():
            if key not in arguments:
                if "default" in prop:
                    resolved[key] = prop["default"]
                continue
            value = arguments[key]
            expected = prop.get("type")
            if expected in _TYPE_MAP:
                types = _TYPE_MAP[expected]
                # bool 是 int 的子类，需排除以免 True 通过 integer 校验
                if expected in {"integer", "number"} and isinstance(value, bool):
                    raise ValueError(f"argument {key!r} for {name} must be {expected}, got boolean")
                if not isinstance(value, types):
                    raise ValueError(
                        f"argument {key!r} for {name} must be {expected}, got {type(value).__name__}"
                    )
            if "enum" in prop and value not in prop["enum"]:
                raise ValueError(f"argument {key!r} for {name} must be one of {prop['enum']}")
            resolved[key] = value
        return resolved

    def invoke(self, name: str, **arguments: Any):
        """校验后执行 handler，并**执行延迟预算**。

        修复审计 B1：`latency_budget_ms` 此前只被赋值、从未被读取——声明了超时
        预算却没有任何超时行为。

        超时转成**结构化结果**而不是异常：异常会让整轮对话失败，而「某个源慢」
        只应让该维度降级。耗时超限的线程会被放弃（Python 无法抢占），这一点
        在 `degraded_reason` 中如实说明。
        """
        spec = self.require(name)
        resolved = self.validate(name, arguments)
        budget_ms = int(getattr(spec, "latency_budget_ms", 0) or 0)

        if budget_ms <= 0:
            return spec.handler(**resolved)

        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"tool-{spec.name}")
        try:
            future = pool.submit(spec.handler, **resolved)
            try:
                return future.result(timeout=budget_ms / 1000.0)
            except FutureTimeout:
                sources = ", ".join(spec.freshness_sources) or spec.name
                logger.warning("tool %s exceeded latency budget %sms", spec.name, budget_ms)
                return degraded_tool_result(
                    tool_name=spec.name,
                    reason=(
                        f"工具 {spec.name} 超过延迟预算 {budget_ms}ms（慢源：{sources}）；"
                        "已放弃等待并降级，该维度本轮不可用"
                    ),
                    unavailable=tuple(spec.freshness_sources) or (spec.name,),
                )
        finally:
            # 不等待被放弃的调用：它可能仍在阻塞于不响应的数据源
            pool.shutdown(wait=False)
