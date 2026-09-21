"""回合中间件：把横切关注点从循环体里拿出来。

## 为什么必须是中间件链

一个完整的对话回合有六件事与「取数→归纳」的主流程无关：预算控制、
拒绝昂贵工具、防止原地打转、判定一致性、免责声明、记账。把它们内联进
循环体，`loop.py` 会变成第二个 9591 行的 `api/main.py`——那是本仓库
已经付出过代价的错误。

每个中间件都是独立的、可单测的、可通过组合根增删的单元。新增一个关注点
（例如 PII 脱敏、限流）= 新文件 + 组合处加一行，不碰循环。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Protocol, Sequence, runtime_checkable

from tradingagents.tools.spec import RESEARCH_DISCLAIMER, ToolResult

from .events import Notice


@dataclass
class ToolCall:
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    synthetic: bool = False  # 由编排层（而非模型）追加的调用


@dataclass(frozen=True)
class Rejection:
    """中间件拒绝一次工具调用。`notice` 会作为 Notice 事件告知用户。"""

    reason: str
    notice: str | None = None


@dataclass
class TurnContext:
    """回合内的可变状态。中间件读写它，循环在边界处消费。"""

    user_id: str
    tier: str
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    # attempted 与 executed 必须分开：预算要按**尝试**计账，否则一个总是失败的
    # 工具调用可以无限绕过预算（失败路径不计入 executed）。这是测试暴露出的
    # 真实缺陷——最初只统计成功执行，9 次尝试里 8 次失败却一次都没触发预算。
    attempted: list[str] = field(default_factory=list)
    executed: list[str] = field(default_factory=list)
    evidence_versions: list[int] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    # 本轮**计划**（Phase 0 起记录，Phase 1 由 LLM 规划层填充）：让"这一轮打算
    # 做什么、为什么"可事后复盘，而不是只有一串工具名。
    plan: dict[str, Any] = field(default_factory=dict)
    # 每步执行观测：工具、参数、是否成功、耗时、证据条数。存在 ctx 上而不是
    # 逐个改 `_finish` 调用点——那有 5 处，漏一处就会有一类回合没有 trace。
    steps: list[dict[str, Any]] = field(default_factory=list)
    tokens: int = 0
    model_calls: int = 0
    verdict: Any | None = None
    started_at: float = 0.0
    # 注册表由循环注入：中间件通过它查询工具元信息（例如 cost），
    # 从而不必在构造时绑定具体实例——否则测试里就得先造注册表再造中间件，
    # 装配顺序变成隐式契约。
    registry: Any | None = None

    @property
    def evidence_signature(self) -> int:
        """证据规模指纹：用于判断一次调用是否带来新信息。"""
        return sum(self.evidence_versions)


@runtime_checkable
class TurnMiddleware(Protocol):
    """回合中间件。

    三个钩子对应三个决策点：调用前（可否）、调用后（结果变换）、
    成文前（文本变换）。刻意保持窄接口——中间件不该有权改写判定，
    否则 `VerdictGuard` 这样的护栏可以被另一个中间件绕过。
    """

    def before_tool(self, ctx: TurnContext, call: ToolCall) -> ToolCall | Rejection:
        return call

    def after_tool(self, ctx: TurnContext, call: ToolCall, result: ToolResult) -> ToolResult:
        return result

    def before_answer(self, ctx: TurnContext, text: str) -> str:
        return text


class BaseMiddleware:
    """中间件基类：提供三个钩子的空实现。

    `TurnMiddleware` 是 Protocol，只声明接口——**不会**给实现类提供方法体。
    若不加这个基类，凡只关心一个钩子的中间件（例如只做免责声明的）在另一个
    钩子上就会 AttributeError，而失败点会出现在循环深处、表现为「回合失败」，
    排查成本很高。基类把「未实现即透传」变成显式契约。
    """

    name = "base"

    def before_tool(self, ctx: TurnContext, call: ToolCall) -> ToolCall | Rejection:
        return call

    def after_tool(self, ctx: TurnContext, call: ToolCall, result: ToolResult) -> ToolResult:
        return result

    def before_answer(self, ctx: TurnContext, text: str) -> str:
        return text


# ── 内置中间件 ────────────────────────────────────────────────────────────


class BudgetGuard(BaseMiddleware):
    """轮数与 token 预算。

    超限时的行为必须是「返回已获证据 + 明示截断」，而不是静默升级档位或
    静默失败——静默升级会绕过 D3 的向下降级规则，静默失败会让用户以为
    问题无法回答。
    """

    name = "budget_guard"

    def __init__(self, *, max_tool_calls: int | None = None, max_model_calls: int | None = None) -> None:
        self._max_tool_calls = max_tool_calls
        self._max_model_calls = max_model_calls

    def before_tool(self, ctx: TurnContext, call: ToolCall):
        from .routing import TIER_BUDGETS

        budget = TIER_BUDGETS[ctx.tier]
        limit = self._max_tool_calls if self._max_tool_calls is not None else budget.max_tool_calls
        if len(ctx.attempted) >= limit:
            return Rejection(
                reason="tool_budget_exhausted",
                notice=f"已达本档工具调用预算（{limit} 次），剩余请求被截断；以下结论仅基于已获取的证据。",
            )
        return call

    def after_tool(self, ctx: TurnContext, call: ToolCall, result: ToolResult) -> ToolResult:
        return result


class ExpensiveToolGuard(BaseMiddleware):
    """拒绝在循环内调用 `expensive` 工具（分钟级管道）。

    正确行为是**提议**升级并等待用户确认（D21），而不是静默升级，
    也不是永久拒绝——后者会让需要深度研究的问题无处可去。
    """

    name = "expensive_tool_guard"

    def __init__(self, registry=None) -> None:
        self._registry = registry

    def before_tool(self, ctx: TurnContext, call: ToolCall):
        registry = self._registry if self._registry is not None else ctx.registry
        spec = registry.get(call.tool) if registry is not None else None
        is_expensive = bool(spec is not None and spec.cost == "expensive")
        if is_expensive:
            return Rejection(
                reason="expensive_tool_not_allowed_in_loop",
                notice=(
                    "该问题需要完整的多分析师研判（约 3-5 分钟）。"
                    "如需启动，请明确回复「启动深度分析」。"
                ),
            )
        return call


class NoProgressGuard(BaseMiddleware):
    """第 2 轮之后若没有产生新证据，强制终止。

    挡的是模型反复查同一份数据——那既烧 token 又不产生新信息。判据是
    「已执行次数」与「证据指纹是否变化」，而不是猜测模型意图。
    """

    name = "no_progress_guard"

    def __init__(self, *, max_identical: int = 2) -> None:
        self._max_identical = max_identical

    def before_tool(self, ctx: TurnContext, call: ToolCall):
        if len(ctx.attempted) < self._max_identical:
            return call
        sigs = ctx.evidence_versions
        # 最近一次调用带来了新证据 → 放行；证据规模没变 → 判定为原地打转
        if len(sigs) >= 2 and sigs[-1] != sigs[-2]:
            return call
        return Rejection(
            reason="no_new_evidence",
            notice="连续多次调用未产生新证据，已提前结束取数，避免重复查询。",
        )


class VerdictGuard(BaseMiddleware):
    """判定一致性护栏：正文不得声称与探针不同的状态或更高的置信度。

    这是「方向权在确定性层、表达权在 LLM」的落地。与仓库既有护栏同构：
    `RISK_JUDGE` 解析失败强制按 reject 处理、`signal_processing` 无
    `VERDICT` 块时返回 None 计入弃权率——都是「宁可少说，不可乱说」。
    """

    name = "verdict_guard"

    # 其他状态的同义表述，出现即视为模型在改写判定
    _STATE_WORDS = {
        "washout": ("洗盘", "震仓"),
        "distribution": ("派发", "出货"),
        "trend_break": ("趋势破坏", "破位下跌"),
    }

    def before_answer(self, ctx: TurnContext, text: str) -> str:
        verdict = ctx.verdict
        if verdict is None:
            return text

        forbidden: list[str] = []
        for state, words in self._STATE_WORDS.items():
            if state != verdict.state:
                forbidden.extend(words)
                # 英文状态词同样要拦：中文被过滤而英文漏网，等于护栏没生效
                forbidden.append(state)
        for word in forbidden:
            if word in text:
                text = text.replace(word, _neutralize(word))
                ctx.notices.append("verdict_guard: 已移除与探针判定冲突的措辞")

        # 置信度不得高于探针报告值
        inflated = _inflated_confidence(text, verdict.confidence)
        if inflated is not None:
            text = text.replace(f"置信 {inflated}", f"置信 {verdict.confidence}")
            text = text.replace(f"置信度 {inflated}", f"置信度 {verdict.confidence}")
            ctx.notices.append("verdict_guard: 已把置信度收敛到探针报告值")
        return text


class DisclaimerInjector(BaseMiddleware):
    """在传达判断的回答末尾追加免责声明。"""

    name = "disclaimer_injector"

    _JUDGEMENT_STATES = ("washout", "distribution", "trend_break")

    def before_answer(self, ctx: TurnContext, text: str) -> str:
        verdict = ctx.verdict
        needs = verdict is not None and verdict.state in self._JUDGEMENT_STATES
        if not needs or RESEARCH_DISCLAIMER in text:
            return text
        return f"{text}\n\n> {RESEARCH_DISCLAIMER}"


class DirectiveLanguageGuard(BaseMiddleware):
    """拦截指令式买卖措辞（合规要求）。

    研究结论允许是「条件化观察」，不允许是「立即清仓」这类祈使句。
    """

    name = "directive_language_guard"

    _DIRECTIVES = ("立即清仓", "马上买入", "立刻卖出", "全仓买入", "无条件卖出", "抓紧买入")

    def before_answer(self, ctx: TurnContext, text: str) -> str:
        for phrase in self._DIRECTIVES:
            if phrase in text:
                text = text.replace(phrase, "（该措辞已被合规过滤）")
                ctx.notices.append("directive_language_guard: 已过滤指令式措辞")
        return text


class TelemetrySink(BaseMiddleware):
    """把回合指标写入上下文，供落库与成本归因。"""

    name = "telemetry"

    def after_tool(self, ctx: TurnContext, call: ToolCall, result: ToolResult) -> ToolResult:
        ctx.evidence_versions.append(len(result.evidence))
        return result


DEFAULT_MIDDLEWARE: tuple[Any, ...] = (
    BudgetGuard(),
    ExpensiveToolGuard(),
    NoProgressGuard(),
    VerdictGuard(),
    DirectiveLanguageGuard(),
    DisclaimerInjector(),
    TelemetrySink(),
)


def _neutralize(word: str) -> str:
    """把与判定冲突的状态词替换为中性表述，而不是简单删除。

    直接删除会让句子变得不通顺（「这明显是，不是洗盘」），从而把一次护栏
    拦截变成一次可读性事故；替换为中性词保留句子结构，也不引入新断言。
    """
    return "非本研究结论所述状态"


def _inflated_confidence(text: str, ceiling: int) -> int | None:
    """探测正文中高于探针值的置信度数字。"""
    import re

    for match in re.finditer(r"置信(?:度)?\s*[:：]?\s*(\d{1,3})", text):
        value = int(match.group(1))
        if value > ceiling:
            return value
    return None
