"""工具契约：ToolSpec 与 ToolResult。

## 为什么 ToolResult 的 data 必须是结构化映射

仓库现有 19 个 `@tool` 返回**预格式化的中文字符串表格**（例如
`core_stock_tools.py` 的 `get_stock_data` 返回一张 markdown 表）。这对
「喂给提示词的 Agent」是合适的，但对任何需要做数值推理的消费者都不合适：
外部 Agent 拿到的是别人咀嚼过的文本，洗盘探针的确定性层也无法在其上工作。

因此新工具的返回值是 `ToolResult.data`（可直接参与算术）+ `evidence`
（可被回答层引用）+ `coverage`（声明未覆盖维度）。文本渲染由独立
renderer 负责，**渲染不是契约**。

## 为什么 requires 要显式声明

`requires=("position",)` 让编排层在调用前知道需要先解析用户持仓。把这种
依赖藏在 handler 内部，会导致「以为传了持仓其实没有」，进而重演路线图里
记录的 99.5% 盲评问题。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Mapping

from tradingagents.grounding.types import Coverage, EvidenceItem

CostClass = Literal["cheap", "medium", "expensive"]

# 判定类输出的统一免责声明：洗盘/派发是对他人意图的概率化推断
RESEARCH_DISCLAIMER = (
    "本结论为基于可观测数据的概率化研究判断，不构成投资建议。"
    "判定附带失效条件，请在条件触发时重新评估。"
)


@dataclass(frozen=True)
class ToolResult:
    """一次工具调用的结构化结果。"""

    data: Mapping[str, Any]
    evidence: tuple[EvidenceItem, ...] = ()
    coverage: Coverage = field(default_factory=Coverage)
    freshness: Mapping[str, Any] = field(default_factory=dict)
    degraded: bool = False
    degraded_reason: str | None = None
    as_of: str = ""
    disclaimer: str | None = None

    def evidence_by_id(self) -> dict[str, EvidenceItem]:
        return {item.id: item for item in self.evidence}

    def merged(self, other: "ToolResult") -> "ToolResult":
        """合并多个工具结果（批量调用与多工具轮次会用到）。

        证据编号保持全局唯一：合并时对后者重新编号，避免 E1 指两条事实。
        """
        offset = len(self.evidence)
        renumbered = tuple(
            EvidenceItem(
                id=f"E{offset + i + 1}",
                field=item.field,
                value=item.value,
                unit=item.unit,
                source_key=item.source_key,
                as_of=item.as_of,
            )
            for i, item in enumerate(other.evidence)
        )
        return ToolResult(
            data={**self.data, **other.data},
            evidence=self.evidence + renumbered,
            coverage=self.coverage.merged(other.coverage),
            freshness={**self.freshness, **other.freshness},
            degraded=self.degraded or other.degraded,
            degraded_reason=self.degraded_reason or other.degraded_reason,
            as_of=max(self.as_of, other.as_of) if self.as_of and other.as_of else (self.as_of or other.as_of),
            disclaimer=self.disclaimer or other.disclaimer,
        )


PLAN_LAYER_DECISION = "L1"
PLAN_LAYER_EVIDENCE = "L2"
PLAN_LAYER_TASK = "L3"
PLAN_LAYERS: tuple[str, ...] = (PLAN_LAYER_DECISION, PLAN_LAYER_EVIDENCE, PLAN_LAYER_TASK)


@dataclass(frozen=True)
class ToolSpec:
    """一个能力的唯一权威定义。

    所有消费者（对话 Agent / LangGraph 分析师 / MCP 外部调用 / HTTP 直取）
    都从它派生各自的传输形态，因此不存在「第二份定义」可以漂移。
    """

    name: str
    title: str
    description: str
    input_schema: Mapping[str, Any]
    output_contract: str
    handler: Callable[..., ToolResult]
    cost: CostClass = "cheap"
    latency_budget_ms: int = 3000
    freshness_sources: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    # 工具面分层（见 docs/agent-tool-inventory.md）：
    #   L1 决策——一个工具回答一个**判断问题**（含结论/依据/弃权），提示词常驻
    #   L2 证据——只给事实、不做判断，按意图检索注入
    #   L3 任务——分钟级能力，**必须异步**（同步工具面里放分钟级管道会让客户端
    #      超时重试、循环卡住、预算记账失真）
    # 默认 L2：绝大多数工具是取数原语，分层信息不是可选的元数据——规划层要靠它
    # 决定注入哪些工具，错了会导致"该调的不在册"。
    plan_layer: str = PLAN_LAYER_EVIDENCE
    # 异步工具：调用立即返回任务号，由 poll 取终态。分钟级能力必须是 True。
    asynchronous: bool = False

    def __post_init__(self) -> None:
        if self.plan_layer not in PLAN_LAYERS:
            raise ValueError(
                f"工具 {self.name} 的 plan_layer={self.plan_layer!r} 非法，"
                f"只能是 {PLAN_LAYERS}"
            )
        if self.plan_layer == PLAN_LAYER_TASK and not self.asynchronous:
            # 任务层工具同步执行 = 客户端必然超时。这条不是风格问题，是协议问题。
            raise ValueError(f"L3 工具 {self.name} 必须 asynchronous=True")

    @property
    def is_synchronous(self) -> bool:
        """分钟级管道不进同步工具面（MCP 客户端会超时重试）。"""
        return self.cost != "expensive"

    def as_descriptor(self) -> dict[str, Any]:
        """传输中立的能力描述，供适配器与测试共同使用。"""
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "input_schema": dict(self.input_schema),
            "output_contract": self.output_contract,
            "cost": self.cost,
            "latency_budget_ms": self.latency_budget_ms,
            "plan_layer": self.plan_layer,
            "asynchronous": self.asynchronous,
        }
