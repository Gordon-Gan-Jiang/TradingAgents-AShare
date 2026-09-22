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


def _edit_distance_within(a: str, b: str, limit: int = 2) -> bool:
    """编辑距离是否 ≤ limit（带上限的 Levenshtein，够用且不必引入依赖）。"""
    if abs(len(a) - len(b)) > limit:
        return False
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(
                min(
                    previous[j] + 1,  # 删
                    current[j - 1] + 1,  # 增
                    previous[j - 1] + (ca != cb),  # 改
                )
            )
        if min(current) > limit:
            return False
        previous = current
    return previous[-1] <= limit


def _looks_like_a_typo_of(key: str, properties: Any) -> bool:
    """`key` 是否像某个已声明参数的拼写错误（而不是一个无关的外来键）。

    分辨这两者是有意义的，因为它们的正确处理方式相反：

    * `look_back` 想写 `look_back_days` → **拒绝**。静默丢弃会让工具退回默认值，
      于是默认的"7 天"冒充了模型想表达的"5 天"——不报错、也没痕迹。
    * 问市盈率时多带的 `symbol`（工具要的是 `ticker`）→ **丢弃**。它与任何声明
      参数都不同源，丢掉不改变这次调用的语义；而硬拒会让整次调用失败、
      用户只看到「本轮无可用证据」。

    判据：前缀关系（含互为前缀）或编辑距离 ≤ 2。
    """
    lowered = key.lower()
    for declared in properties:
        other = str(declared).lower()
        if lowered == other:
            continue
        if lowered.startswith(other) or other.startswith(lowered):
            # 前缀关系：`look_back` / `look_back_days`、`date` / `date_range`。
            # 太短的公共前缀不算（`date` 与 `data` 不是同一个东西）。
            if min(len(lowered), len(other)) >= 4:
                return True
        if _edit_distance_within(lowered, other, limit=2):
            return True
    return False

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
        # 最近一次 `validate` 丢弃的未声明参数。留着是为了让"提示词里出现了假参数名"
        # 这类回归仍然可观测——丢弃它本身是对的，静默丢弃才是问题。
        self.last_dropped_arguments: tuple[str, ...] = ()

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
        """按声明 schema 校验并填充默认值。

        **多给的参数被丢弃，而不是让整次调用失败。**

        原本这里是硬拒：`unknown = [...] ; if unknown: raise`。它抓到过一个真 bug
        （`routing.py:194` 记着：曾经按 `symbol`/`as_of` 硬猜参数名，于是每次调用
        都被拒），所以这个拒绝本身有价值，**不能静默取消**。

        但它的代价与收益不成比例。实测（2026-09-22，问题「贵州茅台市盈率多少」）
        规划器给出的参数是：

            {"ticker": "600519.SH", "curr_date": "2026-09-22", "symbol": "600519.SH"}
                                    ↑ 必需参数齐全且正确            ↑ 多给的一个等价别名

        结果整次调用抛 `ValueError: unknown arguments: ['symbol']` → 工具 0 条证据
        → 用户看到「本轮无可用证据」。**模型因为多给了一个信息而受罚**，而且是一次
        必然重演的行为（LLM 天然会把上下文里的 `symbol` 一并带上）。

        为什么丢弃它不会掩盖错标的风险：所有**标识性**参数（`ticker` / `symbols` /
        `board_name` / `date` / `curr_date`）在各自 schema 里都是 `required` 或没有
        可静默替换的默认值；带默认值的只有 `freq` / `sector_type` / `period` /
        `limit` 这类范围旋钮。所以"丢掉了想表达标的的那个键"不可能静默发生——
        它必然表现为 `missing required arguments`，那正是准确的诊断。
        """
        spec = self.require(name)
        self.last_dropped_arguments = ()  # 每次校验都重置，避免读到上一次的残留
        schema = spec.input_schema
        properties: dict[str, Any] = schema.get("properties", {})
        required: list[str] = list(schema.get("required", []))

        unknown = [key for key in arguments if key not in properties]
        missing = [key for key in required if key not in arguments]
        # 必需项缺失时先报缺失：它比"有个参数不认识"更接近用户要修的东西。
        if missing:
            detail = f"missing required arguments for {name}: {missing}"
            if unknown:
                detail += f"（另有未声明的参数被忽略：{unknown}）"
            raise ValueError(detail)

        # 区分两种"多给的参数"——它们的正确处理方式相反：
        #
        #   * **拼错**（`look_back` 想写 `look_back_days`）：必须拒绝。
        #     静默丢弃会让工具退回默认值，于是"7 天"冒充了模型想表达的"5 天"——
        #     一个不会报错、也没有痕迹的错误结果。
        #   * **外来键**（问市盈率时把上下文里的 `symbol` 一并带上）：应当丢弃。
        #     它与任何已声明参数都不同源，丢掉不改变这次调用的语义。
        #
        # 判据用"名字近似"来区分：前缀关系或编辑距离很小 = 像拼错。
        typo = [key for key in unknown if _looks_like_a_typo_of(key, properties)]
        if typo:
            candidates = [
                declared
                for declared in properties
                if _looks_like_a_typo_of(typo[0], {declared: properties[declared]})
            ]
            raise ValueError(
                f"unknown arguments for {name}: {typo}"
                f"（像已声明参数的拼写错误，应改为：{candidates}）"
            )

        if unknown:
            # 保住诊断信号：丢弃这件事必须在 trace / 日志里看得见，
            # 否则"提示词里出现了假参数名"这类回归又变成不可观测。
            self.last_dropped_arguments = tuple(unknown)
            logger.warning(
                "工具 %s 收到未声明的参数 %s，已忽略（必需参数齐全、且不像拼写错误）",
                name,
                unknown,
            )

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
