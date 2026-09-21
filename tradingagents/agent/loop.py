"""对话回合循环。

## 循环只做四件事

    ① 决定调用哪些工具（可插拔 planner，默认按档位）
    ② 过中间件 → 执行工具 → 收集证据
    ③ 让模型产出**结构化**回答 → 机械校验 → 有界修复 → 诚实降级
    ④ 发出事件并返回记账结果

预算、拒绝昂贵工具、防原地打转、判定一致性、免责声明、合规过滤全部在
`middleware.py` 里，**不在这个文件**。这个文件一旦开始内联这些逻辑，
就会长成第二个 9591 行的 `api/main.py`。

## 为什么是同步函数

LLM、时钟、端口、事件出口全部由参数注入，于是循环可以在没有网络、没有模型、
时间冻结的条件下被完整测试。API 层用 `asyncio.to_thread` 调用它——与仓库
既有做法一致（`await asyncio.to_thread(_build_runtime_config, ...)`）。

## 降级与失败的区别

* `degraded`：**能回答但没有依据** —— 校验两次未通过，改输出「缺什么」，
  绝不输出未通过校验的文本
* `failed`：模型或编排层抛异常
* `cancelled`：客户端断开
* `completed`：正常完成

四者在事件流里由终止事件的 status 区分，前端据此决定渲染方式。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Protocol, Sequence

from tradingagents.grounding.renderer import render, render_insufficient
from tradingagents.grounding.types import (
    CheckResult,
    EvidenceItem,
    EvidenceSet,
    StructuredAnswer,
    TurnVerdict,
)
from tradingagents.grounding.fallback import verify_text
from tradingagents.grounding.verifier import coverage_unavailable_of, verify
from tradingagents.tools.spec import ToolResult
from tradingagents.tools.registry import ToolRegistry

from .actions import ActionProposal, can_execute_actions, detect_action_proposal, is_confirmation, is_rejection
import logging

from .events import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_DEGRADED,
    STATUS_FAILED,
    AnswerDelta,
    AnswerDone,
    EndEvent,
    ErrorEvent,
    EventSink,
    ActionExecutedEvent,
    ClarificationNeeded,
    SessionEvent,
    Notice,
    ProposalEvent,
    ToolDone,
    ToolStart,
    VerdictEvent,
)
from .failures import describe_turn_failure, failure_reason
from .middleware import DEFAULT_MIDDLEWARE, Rejection, ToolCall, TurnContext
from .routing import TIER_BUDGETS, TIER_HEAVY, TIER_LIGHT, TIER_MEDIUM, TierBudget

# 会话内判定编号。同一回合最多一个判定（批量探针按标的逐条列出）。
VERDICT_ID = "V1"

# 注入提示词的历史轮数上限。不做限制会让长会话的提示词无界增长——
# 那既是成本问题，也会让模型注意力被早期无关内容稀释。
MAX_HISTORY_TURNS = 6


class AnswerModel(Protocol):
    """结构化回答模型。

    `propose` 产出带引用的 claims；`repair` 在**一次**校验失败后被调用。
    把「生成」与「修复」分成两个方法，是为了让「最多修复一次」这条约束
    由循环结构保证，而不是靠提示词自律。
    """

    def propose(
        self,
        *,
        prompt: str,
        evidence: EvidenceSet,
        verdict: TurnVerdict | None,
    ) -> StructuredAnswer: ...

    def repair(
        self,
        *,
        previous: StructuredAnswer,
        failure: CheckResult,
        evidence: EvidenceSet,
        verdict: TurnVerdict | None,
    ) -> StructuredAnswer: ...


@dataclass(frozen=True)
class TurnRequest:
    text: str
    user_id: str
    tier: str = TIER_MEDIUM
    symbol: str | None = None
    symbols: tuple[str, ...] = ()
    as_of: str = ""
    conversation_id: str | None = None
    # 上一轮留下的待确认提议（由服务层从会话里读出）
    pending_proposal: Mapping[str, Any] | None = None
    # 执行面：只有产品内对话可以确认执行动作；MCP 等外部面 V1 只读
    surface: str = "product_chat"
    # 标的解析出的多个候选（需要用户选择）
    candidates: tuple[str, ...] = ()
    # 本会话此前的问答（按时间升序）。注入提示词才能消解「它」「这只」这类指代。
    history: tuple[Mapping[str, Any], ...] = ()
    # 无标的的全市场筛选回合（「放量票找十个给我」）。显式字段而不是靠文本
    # 在循环里二次猜测：意图判定只应有一处实现（routing 层）。
    screen: bool = False
    # 本回合的计划来源（rules / llm）。落进 trace 才能回答"这轮为什么这么走"，
    # 也是灰度期间对比两条路径的依据。
    plan_source: str = "rules"
    # 市场级**事实**问句（「今天涨停板有多少只」）：本来就不需要标的。
    # 没有这个标记的话，澄清分支会把它当成"用户没给代码"而追问——那是把
    # 平台已有的市场级工具藏起来（实测踩到）。
    market_level: bool = False


@dataclass(frozen=True)
class TurnResult:
    tier: str
    answer: str
    status: str
    state: str | None = None
    confidence: int | None = None
    verdict_id: str | None = None
    tool_calls: tuple[str, ...] = ()
    notices: tuple[str, ...] = ()
    tokens: int = 0
    model: str = ""
    model_calls: int = 0
    latency_ms: int = 0
    rejected_claims: int = 0
    # 本轮走的是弱校验（文本数值后置校验）而非引用校验。
    # 统计必须区分二者，否则强弱两条路径的准确率会被混为一谈。
    weak_verification: bool = False
    proposal: Mapping[str, Any] | None = None
    action_executed: bool = False
    # 本轮的**计划**与**逐步观测**：Phase 0 的交付物（可审计/可评测），
    # Phase 1 起由 LLM 规划层填充同一字段。
    plan: Mapping[str, Any] = field(default_factory=dict)
    steps: tuple[Mapping[str, Any], ...] = ()
    # None = 本回合未解析持仓；True/False = 已解析且已知/未知
    position_known: bool | None = None
    unavailable_declared: tuple[str, ...] = ()
    conversation_id: str | None = None
    budget: TierBudget = field(default_factory=lambda: TIER_BUDGETS[TIER_MEDIUM])


def _prequel(
    request: TurnRequest,
    sink: EventSink,
    clock: Any,
    started: float,
    action_executor: Any | None = None,
) -> TurnResult | None:
    """处理澄清与两步确认。返回 None 表示继续正常流程。

    这两件事都必须在取数与模型**之前**决定：追问不该付模型成本，确认不该
    重新跑一遍分析——后者既慢又会让用户以为要重新问一次。
    """
    budget = TIER_BUDGETS[request.tier]

    # ── 澄清：没有标的就追问，不猜 ──────────────────────────────────────
    # 全市场筛选回合**本来就没有标的**，追问「请告诉我代码」是把一个已被正确
    # 理解的需求又推回给用户。筛选回合跳过这一段。
    if (
        request.symbol is None
        and not request.symbols
        and not request.screen
        and not request.market_level
    ):
        if request.candidates:
            listed = "、".join(request.candidates)
            question = f"你说的可能是以下哪一只？请回复代码：{listed}"
        else:
            question = "请告诉我具体的股票代码（如 600519.SH）或公司名称，我再帮你判断。"
        sink.emit(ClarificationNeeded(question=question, candidates=tuple(request.candidates)))
        sink.emit(AnswerDelta(text=question + "\n"))
        sink.emit(AnswerDone(text=question))
        sink.emit(EndEvent(status=STATUS_COMPLETED))
        return TurnResult(
            tier=request.tier, answer=question, status=STATUS_COMPLETED, budget=budget
        )

    pending = request.pending_proposal

    # ── 拒绝：撤销提议，不执行 ──────────────────────────────────────────
    if pending is not None and is_rejection(request.text):
        text = "已取消该操作，未做任何改动。"
        sink.emit(Notice(text=text))
        sink.emit(AnswerDelta(text=text + "\n"))
        sink.emit(AnswerDone(text=text))
        sink.emit(EndEvent(status=STATUS_COMPLETED))
        return TurnResult(
            tier=request.tier, answer=text, status=STATUS_COMPLETED, budget=budget
        )

    # ── 确认：仅在允许执行的面，且必须真的调用执行器 ─────────────────────
    if pending is not None and is_confirmation(request.text):
        if not can_execute_actions(request.surface):
            text = (
                "当前入口不支持执行状态变更操作（只读）。"
                "请在产品界面的对话里确认该操作。"
            )
            sink.emit(Notice(text=text))
            sink.emit(AnswerDelta(text=text + "\n"))
            sink.emit(AnswerDone(text=text))
            sink.emit(EndEvent(status=STATUS_COMPLETED))
            return TurnResult(
                tier=request.tier, answer=text, status=STATUS_COMPLETED, budget=budget
            )

        executor = action_executor
        if executor is None:
            text = "当前环境未配置动作执行器，操作未执行。"
            sink.emit(Notice(text=text))
            sink.emit(AnswerDelta(text=text + "\n"))
            sink.emit(AnswerDone(text=text))
            sink.emit(EndEvent(status=STATUS_COMPLETED))
            return TurnResult(
                tier=request.tier, answer=text, status=STATUS_COMPLETED, budget=budget
            )

        try:
            summary = str(executor(pending, request.user_id))
            ok = True
        except Exception as exc:  # noqa: BLE001 — 执行失败必须如实告知
            summary = f"操作执行失败：{type(exc).__name__}: {exc}。未做任何改动。"
            ok = False

        sink.emit(
            ActionExecutedEvent(
                kind=str(pending.get("kind") or ""), summary=summary, ok=ok
            )
        )
        sink.emit(AnswerDelta(text=summary + "\n"))
        sink.emit(AnswerDone(text=summary))
        sink.emit(EndEvent(status=STATUS_COMPLETED))
        return TurnResult(
            tier=request.tier,
            answer=summary,
            status=STATUS_COMPLETED,
            action_executed=ok,
            budget=budget,
        )

    return None


# 执行器通过参数显式传入（审计 C2 修复）。
#
# 原实现用模块级全局 `_EXECUTOR: dict[str, Any]` 按用户注册。那是与
# `dataflows/config.py` 的 `global _config` 同一类反模式——我在别处批评过它，
# 却在这里复现了：同一用户并发两个回合会互相覆盖执行器，而且测试之间会串味。
# 因果类问句的标记：问到「为什么」时，只有量价资金数据会让模型从价格行为
# 倒推原因——那是编造。必须同时取事实来源（新闻/公告），并允许它声明未覆盖。
_CAUSAL_MARKERS = (
    "为什么",
    "什么原因",
    "啥原因",
    "怎么回事",
    "为何",
    "利空",
    "利好",
    "什么消息",
    "出了什么事",
)


def _asks_for_cause(text: str) -> bool:
    return any(marker in (text or "") for marker in _CAUSAL_MARKERS)


def _is_text_mode(answer: StructuredAnswer) -> bool:
    """文本兜底模式：有正文但没有逐条引用。

    这种回答不能走引用校验器——没有 claim 可查会让校验**空过**。
    """
    return not answer.claims and bool(answer.summary.strip())


def _tokens_of(model: Any) -> int:
    """取上次补全的 token 用量。

    优先用模型显式暴露的增量（`last_usage`），否则退化为 `total_tokens` 差值；
    两者都没有时记 0 而**不估算**——估算会把成本归因带偏。
    """
    usage = getattr(model, "last_usage", None)
    if usage is not None:
        try:
            return int(getattr(usage, "total_tokens", 0) or 0)
        except Exception:  # noqa: BLE001
            return 0
    return 0


def _missing_required_context(
    spec: Any,
    *,
    position_context: Any | None,
) -> tuple[str, ...]:
    """校验 `ToolSpec.requires` 声明的上下文是否已由编排层解析。

    只覆盖已知的上下文键；未知键按「已满足」处理并留日志——把未知键当成未满足
    会让新增声明直接静默停掉工具，那是更糟的失败模式。
    """
    missing: list[str] = []
    for key in getattr(spec, "requires", ()) or ():
        if key == "position":
            if position_context is None:
                missing.append("position")
        elif key not in ("", None):
            logging.getLogger(__name__).info(
                "unknown required context key %r on tool %s", key, getattr(spec, "name", "?")
            )
    return tuple(missing)


def _plan_tools(request: TurnRequest, *, registry: Any = None) -> list[ToolCall]:
    """默认工具计划：按档位与标的机械决定。

    轻档**不调用探针**——事实型问句付 5× 成本是纯浪费。
    重档在本循环内不执行（由既有深度分析管道负责），因此也不计划工具。
    """
    if request.tier == TIER_HEAVY:
        return []
    as_of = request.as_of or ""
    if request.tier == TIER_LIGHT:
        # 事实型问句：取**一个**便宜的数据工具。此前这里直接返回空计划，
        # 而档位表承诺的是"1 个数据工具"——承诺没落地，于是"现在多少钱"
        # 这类最高频的问题永远答"无可用证据"。探针仍然不碰（5× 成本不该付）。
        from .routing import (
            DEFAULT_FACTUAL_TOOL,
            DEFAULT_MARKET_TOOL,
            build_factual_arguments,
            plan_factual_tool,
        )

        candidates = [plan_factual_tool(request.text, has_symbol=bool(request.symbol))]
        for fallback in (DEFAULT_FACTUAL_TOOL, DEFAULT_MARKET_TOOL):
            if fallback not in candidates:
                candidates.append(fallback)

        for name in candidates:
            spec = registry.get(name) if registry is not None and hasattr(registry, "get") else None
            if spec is None:
                continue
            arguments = build_factual_arguments(
                spec, symbol=request.symbol, as_of=as_of, text=request.text
            )
            if arguments is None:
                # 参数填不满必填项 → 换下一个候选，而不是发一次注定失败的调用
                continue
            return [ToolCall(tool=name, arguments=arguments, reason="factual lookup")]
        return []
    if request.screen:
        # 全市场筛选：无标的，候选与理由来自扫描器。参数由纯函数从问句解析。
        from .routing import parse_screen_request

        parsed = parse_screen_request(request.text)
        return [
            ToolCall(
                tool="market_screen",
                arguments={
                    "top_k": parsed.top_k,
                    "profile": parsed.profile,
                    "min_change_pct": parsed.min_change_pct,
                    "as_of": as_of,
                },
                reason="market-wide screening",
            )
        ]
    if request.symbols:
        return [
            ToolCall(
                tool="signal_washout_probe_batch",
                arguments={"symbols": list(request.symbols), "as_of": as_of},
                reason="multi-symbol judgement",
            )
        ]
    if request.symbol:
        planned = [
            ToolCall(
                tool="signal_washout_probe",
                arguments={"symbol": request.symbol, "as_of": as_of},
                reason="single-symbol judgement",
            )
        ]
        if _asks_for_cause(request.text):
            # 因果问句追加事实来源。若该工具未注册，循环会以「未知工具」记一条
            # 失败活动并继续——不会因为环境没配新闻源就整轮失败。
            planned.append(
                ToolCall(
                    tool="data_symbol_news",
                    arguments={"symbol": request.symbol, "as_of": as_of, "lookback_days": 3},
                    reason="causal question needs a factual source",
                )
            )
        return planned
    return []



def _declare_computed_coverage(
    answer: StructuredAnswer, coverage_unavailable: Sequence[str]
) -> StructuredAnswer:
    """把**代码已算出**的未覆盖维度并入回答的声明。

    为什么不让模型复述：未覆盖维度是编排层取数后**确定知道**的事实——它正是
    这么算出来的。要求模型逐字复述，等于把"陈述已知事实"变成一次随机通过的语言
    考试。实测 deepseek-v4-flash 连续两次没照抄，整轮被判
    `coverage_unavailable_not_disclosed` 并降级成"什么都没有"——用户拿到的是
    最差的结果（既没有答案，也不知道缺什么）。

    并入之后，校验器的这条检查由**构造**满足，而渲染器本来就会把
    `declared_unavailable` 写成「数据未覆盖」一行——所以用户该看到的声明一条不少。
    """
    declared = tuple(getattr(answer, "declared_unavailable", ()) or ())
    if not coverage_unavailable:
        return answer
    merged = tuple(dict.fromkeys(declared + tuple(coverage_unavailable)))
    if merged == declared:
        return answer
    return replace(answer, declared_unavailable=merged)


def _estimated_disclosure(data: Mapping[str, Any]) -> str:
    """把"模型估计"口径渲染成一行——由**代码**写，不靠模型自觉。"""
    estimated = [str(item) for item in (data.get("estimated_dimensions") or [])]
    if not estimated:
        return ""
    return (
        f"**数据口径**：{'、'.join(estimated)} 为**模型估计**"
        "（由日线与换手率推算），不是交易所原始数据。"
    )


def _with_screen_block(text: str, request: TurnRequest, merged: ToolResult) -> str:
    """筛选回合：把**代码渲染**的候选表放到回答最前。

    降级路径也要走这里。理由：候选表不是"方向性判断"，而是扫描器的原始清单
    （代码渲染、数字与证据同源，无编造风险）。模型没能给出可溯源的解释时，
    把用户真正要的清单一起丢掉，是拿诚实当借口交出无用的回答。
    """
    if not request.screen:
        return text
    block = _render_screen_block(merged.data)
    if not block:
        return text
    return f"{block}\n\n{text}"


def _render_screen_block(data: Mapping[str, Any]) -> str:
    """把筛选结果渲染成 Markdown 表格。

    **由代码渲染，不由模型写**。原因：用户要的是"哪十只"，这属于结构化数据
    （与判定卡片同理）。交给模型复述会同时引入两个风险——候选被写错或被漏写，
    以及"量比缺失"这类未覆盖声明被忘掉。代码渲染让这两件事变成必然，而不是
    依赖模型守规矩。
    """
    items = [item for item in (data.get("items") or []) if isinstance(item, Mapping)]
    if not items:
        return ""

    def _num(value: Any, *, suffix: str = "") -> str:
        if value in (None, ""):
            return "未提供"
        if isinstance(value, (int, float)):
            return f"{float(value):.2f}{suffix}"
        return f"{value}{suffix}"

    lines = ["### 筛选结果", ""]
    scope = (
        f"扫描全市场 **{data.get('pool_size', '未提供')}** 只，"
        f"评分 **{data.get('scored_size', '未提供')}** 只，"
        f"档案 `{data.get('profile') or '默认'}`，取前 {len(items)} 只。"
    )
    lines.append(scope)
    lines.append("")
    lines.append("| # | 代码 | 名称 | 得分 | 涨幅 | 入选理由 |")
    lines.append("|---|---|---|---|---|---|")
    for index, item in enumerate(items, start=1):
        reasons = item.get("reasons") or []
        if isinstance(reasons, str):
            reasons = [reasons]
        lines.append(
            f"| {index} | {item.get('symbol') or '未提供'} | {item.get('name') or '未提供'} "
            f"| {_num(item.get('score'))} | {_num(item.get('price_change_pct'), suffix='%')} "
            f"| {'；'.join(str(r) for r in reasons) or '未提供'} |"
        )
    lines.append("")

    first = items[0]
    lines.append(
        f"数据时点：{first.get('quote_time') or '未提供'}"
        f"（来源 {first.get('quote_source') or '未提供'}）"
    )

    missing = [str(name) for name in (data.get("missing_fields") or [])]
    if missing:
        # 这一行是**代码保证**的：用户问「放量票」而量比拿不到时，答复里必然
        # 出现这句，不依赖模型是否记得声明。
        labels = "、".join(
            name.split("（")[-1].rstrip("）") if "（" in name else name for name in missing
        )
        lines.append(f"⚠️ 数据源未提供、本轮**无法确认**：{labels}。")
    flags = sorted({str(f) for item in items for f in (item.get("risk_flags") or [])})
    if flags:
        lines.append(f"⚠️ 风险标记：{'、'.join(flags)}。")
    return "\n".join(lines)


def _verdict_from_tool_result(result: ToolResult, request: TurnRequest) -> TurnVerdict | None:
    """把探针的 `ToolResult.data` 转成会话判定。

    只认结构化字段；`ToolResult` 里没有散文，因此不存在「从文本里解析判定」
    这种脆弱路径。
    """
    data = dict(result.data)
    if "state" in data:  # 单标的
        payloads = [data]
    elif isinstance(data.get("results"), list):  # 批量：取证据最强者作为主判定
        payloads = data["results"][:1]
    else:
        return None
    if not payloads:
        return None

    row = payloads[0]
    return TurnVerdict(
        id=VERDICT_ID,
        probe=str(row.get("probe", "washout")),
        symbol=str(row.get("symbol", request.symbol or "")),
        as_of=str(row.get("as_of", request.as_of)),
        state=str(row.get("state", "")),
        confidence=int(row.get("confidence", 0)),
        falsifiers=tuple(
            f.get("description", "") if isinstance(f, dict) else str(f)
            for f in (row.get("falsifiers") or [])
        ),
        unavailable=tuple(row.get("unavailable") or ()),
        daily_only=bool(row.get("daily_only", False)),
        high_confidence=bool(row.get("high_confidence", False)),
        signal_version=str(row.get("signal_version", "")),
        payload=row,
    )


def _build_prompt(
    request: TurnRequest,
    evidence: EvidenceSet,
    ctx: TurnContext,
    coverage_unavailable: Sequence[str] = (),
    history: Sequence[Mapping[str, Any]] = (),
) -> str:
    """构造模型提示。

    刻意只给**证据编号与字段名**，不给数值的散文描述：数值由模型从证据里
    引用，而不是被复述一遍（复述是数值错误的来源）。

    `history` 注入最近若干轮问答，用于消解「它」「这只」这类指代——没有它，
    所谓多轮对话退化成互不相干的单轮问答（审计 A1）。
    """
    if request.screen:
        # 筛选回合把**数值**一并给出。实测：只给字段名时真实模型写出的解读
        # 两次被校验器拒（unsourced_number）——它看不到数值，只能凭空写数字。
        # 原设计「只给字段名」是为判定解释服务的（防止模型复述数值引入错误）；
        # 而筛选任务的数字本身就是内容，且候选表已由代码渲染，喂给模型只是
        # 让它能正确引用。校验器照旧逐条核对，安全性不变。
        fields = (
            ", ".join(f"[{item.id}] {item.field}={item.value}" for item in evidence.items)
            or "（无）"
        )
    else:
        fields = ", ".join(f"[{item.id}] {item.field}" for item in evidence.items) or "（无）"
    verdict_line = (
        f"探针判定：{ctx.verdict.state}（置信 {ctx.verdict.confidence}），"
        "你只能解释它、不能改写它。"
        if ctx.verdict is not None
        else "本轮无探针判定。"
    )
    must_declare = "、".join(coverage_unavailable) or "（无）"

    sections = []
    history_block = _format_history(history)
    if history_block:
        sections.append(history_block)

    # 多标的问句最现实的失败是"模型只谈其中一只就交卷"——静态约束必须说出来。
    symbols_line = (
        f"本轮涉及多只标的：{'、'.join(request.symbols)}。"
        "必须**逐只**给出各自结论再作比较，不得只谈其中一只，也不得把某一只的结论"
        "套到另一只上。\n"
        if request.symbols
        else ""
    )
    screen_line = (
        "本轮为全市场筛选：候选代码与数值只能逐个来自证据；"
        "证据中未出现的标的**不得**出现在回答里，也不得补充扫描器未提供的因子。\n"
        if request.screen
        else ""
    )
    sections.append(
        "## 本轮\n"
        f"用户问题：{request.text}\n"
        f"档位：{request.tier}\n"
        f"{symbols_line}"
        f"{screen_line}"
        f"{verdict_line}\n"
        f"可用证据：{fields}\n"
        f"必须在 unavailable 中声明的未覆盖维度：{must_declare}\n"
        "要求：每条断言必须引用证据编号；上列未覆盖维度必须逐项声明，不得描述为「无影响」。"
        "若证据里出现「数据口径」字段，回答**必须**说明相应维度为模型估计，"
        "不得表述为交易所原始数据。"
    )
    return "\n\n".join(sections)


def _format_history(history: Sequence[Mapping[str, Any]] | None) -> str:
    """把历史问答渲染成提示词段落。

    空内容会被跳过：取消或失败的回合会留下空消息，渲染成空行只会白占预算。
    """
    if not history:
        return ""
    recent = list(history)[-MAX_HISTORY_TURNS:]
    lines: list[str] = []
    for item in recent:
        content = str((item or {}).get("content") or "").strip()
        if not content:
            continue
        role = "用户" if str((item or {}).get("role")) == "user" else "助手"
        lines.append(f"- {role}：{content}")
    if not lines:
        return ""
    return "## 本会话此前的问答（用于消解指代，不得作为证据引用）\n" + "\n".join(lines)


def _apply_before_tool(
    middleware: Sequence[Any], ctx: TurnContext, call: ToolCall
) -> ToolCall | Rejection:
    for mw in middleware:
        hook = getattr(mw, "before_tool", None)
        if hook is None:
            continue
        outcome = hook(ctx, call)
        if isinstance(outcome, Rejection):
            return outcome
        call = outcome
    return call


def _apply_after_tool(middleware: Sequence[Any], ctx: TurnContext, call: ToolCall, result: ToolResult) -> ToolResult:
    for mw in middleware:
        hook = getattr(mw, "after_tool", None)
        if hook is not None:
            result = hook(ctx, call, result)
    return result


def _apply_before_answer(middleware: Sequence[Any], ctx: TurnContext, text: str) -> str:
    for mw in middleware:
        hook = getattr(mw, "before_answer", None)
        if hook is not None:
            text = hook(ctx, text)
    return text


def _stream_answer(sink: EventSink, text: str) -> None:
    """逐行下发回答增量。

    真实实现会按模型的 token 流下发；这里按已校验的成文分行下发，保证
    **流出去的内容一定通过了校验**——先流后校验会让未校验文本短暂可见。
    """
    for line in text.splitlines() or [""]:
        sink.emit(AnswerDelta(text=line + "\n"))


def run_turn(
    *,
    request: TurnRequest,
    registry: ToolRegistry,
    model: AnswerModel,
    sink: EventSink,
    clock: Any,
    middleware: Sequence[Any] | None = None,
    extra_tool_calls: Sequence[str] = (),
    reference_date: str | None = None,
    previous_trading_day: str | None = None,
    action_executor: Any | None = None,
    position_context: Any | None = None,
    model_label: str = "",
) -> TurnResult:
    """执行一个对话回合。所有外部依赖由参数注入。"""
    chain = DEFAULT_MIDDLEWARE if middleware is None else tuple(middleware)
    started = clock.now()
    ctx = TurnContext(
        user_id=request.user_id,
        tier=request.tier,
        text=request.text,
        started_at=started,
        registry=registry,
    )
    budget = TIER_BUDGETS[request.tier]

    tool_calls: list[str] = []
    tool_results: list[ToolResult] = []
    # 在 try 之前初始化：异常路径要判断"扫描是否已经完成"，从而决定能不能
    # 把候选清单交付出去。
    merged: ToolResult | None = None
    rejected_claims = 0
    weak_verification = False
    status = STATUS_COMPLETED

    # 会话 id 尽早回传：前端据此建立会话状态并在下一轮续接。
    # 放在任何工作之前——用户等待回答时就该能续接。
    if request.conversation_id:
        sink.emit(SessionEvent(conversation_id=request.conversation_id))

    try:
        # ── 前置分支：澄清与两步确认 ─────────────────────────────────────
        # 这两个分支在取数与模型之前处理：追问不该付模型成本，确认不该重新跑一遍分析
        pre = _prequel(request, sink, clock, started, action_executor)
        if pre is not None:
            return pre

        planned = _plan_tools(request, registry=registry)
        planned += [ToolCall(tool=name, arguments={}, reason="requested") for name in extra_tool_calls]
        ctx.plan = {
            "tier": request.tier,
            "symbol": request.symbol,
            "symbols": list(request.symbols),
            "screen": bool(request.screen),
            "intent_source": request.plan_source,
            "planned": [
                {"tool": call.tool, "arguments": dict(call.arguments), "reason": call.reason}
                for call in planned
            ],
        }

        if any(call.tool == "market_screen" for call in planned):
            # 60s 量级的静默等待会被用户当成卡死。实测全市场 5020 只：冷启动
            # 61.5s、热启动 10.0s，所以给的是量级而不是精确值。
            notice = "正在进行全市场扫描（约 10–60 秒，取决于行情源响应速度）…"
            sink.emit(Notice(text=notice))
            ctx.notices.append(notice)

        for call in planned:
            outcome = _apply_before_tool(chain, ctx, call)
            if isinstance(outcome, Rejection):
                if outcome.notice:
                    sink.emit(Notice(text=outcome.notice))
                    ctx.notices.append(outcome.notice)
                continue

            # 计账在尝试之前：失败与未知工具同样消耗预算，否则模型可以用
            # 一个永远失败的调用绕过预算（见 middleware 里 attempted 的说明）
            ctx.attempted.append(call.tool)
            tool_calls.append(call.tool)

            spec = registry.get(call.tool)
            if spec is None:
                sink.emit(ToolDone(tool=call.tool, ok=False, detail="unknown tool"))
                ctx.steps.append(
                    {"tool": call.tool, "arguments": dict(call.arguments),
                     "ok": False, "elapsed_ms": 0, "evidence_count": 0,
                     "detail": "unknown tool"}
                )
                continue

            # B3 修复：`requires` 此前只是元数据，从未被编排层读取。
            # 语义是「编排层必须先尝试解析该上下文」，未满足时**告知用户**而不是
            # 静默执行——静默会让回答看起来基于持仓，实际没有。
            missing = _missing_required_context(spec, position_context=position_context)
            if missing:
                notice = (
                    f"本轮未解析到所需上下文（{'、'.join(missing)}），"
                    f"{spec.name} 的相应结论将标注为未覆盖。"
                )
                sink.emit(Notice(text=notice))
                ctx.notices.append(notice)

            sink.emit(ToolStart(tool=call.tool, arguments=dict(call.arguments)))
            try:
                result = registry.invoke(call.tool, **call.arguments)
            except Exception as exc:  # noqa: BLE001 — 单工具失败不应终止回合
                sink.emit(ToolDone(tool=call.tool, ok=False, detail=f"{type(exc).__name__}: {exc}"))
                # 失败步骤也要记真实耗时：记 0 会读成"瞬时失败"，而实际上
                # 失败前往往已经把超时预算耗掉了——那正是排查时最需要看到的信息。
                ctx.steps.append(
                    {
                        "tool": call.tool,
                        "arguments": dict(call.arguments),
                        "ok": False,
                        "elapsed_ms": int((clock.now() - started) * 1000),
                        "evidence_count": 0,
                        "detail": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue

            if not isinstance(result, ToolResult):
                sink.emit(ToolDone(tool=call.tool, ok=False, detail="tool returned non-ToolResult"))
                continue

            result = _apply_after_tool(chain, ctx, call, result)
            tool_results.append(result)
            ctx.executed.append(call.tool)
            elapsed_step_ms = int((clock.now() - started) * 1000)
            sink.emit(
                ToolDone(
                    tool=call.tool,
                    ok=True,
                    elapsed_ms=elapsed_step_ms,
                    evidence_count=len(result.evidence),
                )
            )
            ctx.steps.append(
                {
                    "tool": call.tool,
                    "arguments": dict(call.arguments),
                    "ok": True,
                    "elapsed_ms": elapsed_step_ms,
                    "evidence_count": len(result.evidence),
                    "degraded": bool(getattr(result, "degraded", False)),
                }
            )

        # ── 合并证据与覆盖声明 ────────────────────────────────────────────
        merged = ToolResult(data={}, as_of=request.as_of)
        for result in tool_results:
            merged = merged.merged(result)
        evidence = EvidenceSet(items=merged.evidence)
        coverage_list = [merged.coverage]

        # 持仓上下文接入：已知则产出可引用证据（成本/数量），未知则**强制**
        # 进入覆盖声明——回答必须说出「未提供持仓」，而不是按空仓给建议。
        position_known: bool | None = None
        if position_context is not None:
            position_known = bool(getattr(position_context, "known", False))
            position_evidence = tuple(position_context.to_evidence())
            if position_evidence:
                offset = len(evidence.items)
                renumbered = tuple(
                    EvidenceItem(
                        id=f"E{offset + i + 1}", field=item.field, value=item.value,
                        unit=item.unit, source_key=item.source_key, as_of=item.as_of,
                    )
                    for i, item in enumerate(position_evidence)
                )
                evidence = EvidenceSet(items=evidence.items + renumbered)
            if not position_known:
                from tradingagents.grounding.types import Coverage

                coverage_list.append(Coverage(available=(), unavailable=("position",)))

        coverage_unavailable = coverage_unavailable_of(coverage_list)

        if tool_results:
            verdict = _verdict_from_tool_result(tool_results[0], request)
            ctx.verdict = verdict
            if verdict is not None:
                sink.emit(VerdictEvent(verdict=verdict))

        # ── 结构化回答 → 校验 → 有界修复 → 诚实降级 ──────────────────────
        prompt = _build_prompt(
            request, evidence, ctx, coverage_unavailable, history=request.history
        )
        answer = model.propose(prompt=prompt, evidence=evidence, verdict=ctx.verdict)
        # 声明由代码补齐（见 `_declare_computed_coverage`）：这是事实，不是模型的观点
        answer = _declare_computed_coverage(answer, coverage_unavailable)
        ctx.model_calls += 1
        ctx.tokens += _tokens_of(model)

        verdict_ids = frozenset({ctx.verdict.id}) if ctx.verdict is not None else frozenset()

        def _check(candidate: StructuredAnswer) -> CheckResult:
            """按回答形态选择校验强度。

            文本模式（claims 为空、只有正文）必须走**弱校验**。若对它调用
            引用校验器，会因为「没有 claim 可查」而**空过**——未校验的散文
            直接出门。这条降级路径恰恰是模型不遵守 schema 时的高频路径。
            """
            if _is_text_mode(candidate):
                return verify_text(
                    candidate.summary,
                    evidence,
                    reference_date=reference_date,
                    previous_trading_day=previous_trading_day,
                )
            # 本轮标的一并传入：断言里出现的**其他**代码必须能在被引证据里找到，
            # 否则就是编造（实测漏洞：代码被排除在数值检查之外，编造的候选反而通过）
            allowed = tuple(request.symbols) or ((request.symbol,) if request.symbol else ())
            return verify(
                candidate,
                evidence,
                verdict_ids=verdict_ids,
                allowed_symbols=allowed,
                coverage_unavailable=coverage_unavailable,
                reference_date=reference_date,
                previous_trading_day=previous_trading_day,
            )

        check = _check(answer)
        weak_verification = check.weak

        if not check.ok:
            rejected_claims += 1
            # B2 修复：模型调用预算此前只被存下来、从未被引用。
            # 预算耗尽时**不再发起修复**，直接走诚实降级——否则档位声明的
            # 「1 次归纳」没有任何约束力。
            if ctx.model_calls >= budget.max_model_calls:
                status = STATUS_DEGRADED
                text = render_insufficient(
                    evidence,
                    f"模型调用预算已用尽（{ctx.model_calls}/{budget.max_model_calls}），"
                    f"未能修复未通过校验的回答（{check.reason}）。",
                )
                text = _with_screen_block(text, request, merged)
                text = _apply_before_answer(chain, ctx, text)
                _stream_answer(sink, text)
                sink.emit(AnswerDone(text=text))
                result = _finish(
                    ctx, request, text, status, tool_calls, rejected_claims, clock, started,
                    budget, weak_verification,
                    model_label=model_label,
                    position_known=position_known,
                    unavailable_declared=coverage_unavailable,
                )
                sink.emit(EndEvent(status=STATUS_DEGRADED, reason="model_budget_exhausted"))
                return result

            repaired = model.repair(
                previous=answer, failure=check, evidence=evidence, verdict=ctx.verdict
            )
            repaired = _declare_computed_coverage(repaired, coverage_unavailable)
            ctx.model_calls += 1
            ctx.tokens += _tokens_of(model)
            recheck = _check(repaired)
            if recheck.ok:
                answer = repaired
                weak_verification = recheck.weak
            else:
                rejected_claims += 1
                status = STATUS_DEGRADED
                text = render_insufficient(
                    evidence,
                    f"两次生成均未能给出可溯源的回答（{recheck.reason}）。",
                )
                text = _with_screen_block(text, request, merged)
                text = _apply_before_answer(chain, ctx, text)
                _stream_answer(sink, text)
                sink.emit(AnswerDone(text=text))
                # 先构造结果再发终止事件：终止事件必须**恰好一次且最后**。
                # 早期版本先发 EndEvent 再调 _finish，_finish 一抛错就会发出
                # 第二个终止事件（failed），一次回合出现两个终态。
                result = _finish(
                    ctx, request, text, status, tool_calls, rejected_claims, clock, started,
                    budget, weak_verification,
                    model_label=model_label,
                    position_known=position_known,
                    unavailable_declared=coverage_unavailable,
                )
                sink.emit(EndEvent(status=STATUS_DEGRADED, reason=recheck.reason))
                return result

        text = render(answer, evidence)
        text = _with_screen_block(text, request, merged)
        disclosure = _estimated_disclosure(merged.data)
        if disclosure:
            # 代码渲染，不由模型复述：估计口径必须**必然**出现在回答里
            text = f"{text}\n\n{disclosure}"
        text = _apply_before_answer(chain, ctx, text)

        # 行动提议：Agent 只提议，用户确认后才执行。
        # 把确认提示拼进正文，用户才不会以为动作已经生效。
        proposal = detect_action_proposal(request.text, symbol=request.symbol)
        if proposal is not None:
            text = f"{text}\n\n**待确认**：{proposal.summary}\n{proposal.confirmation_prompt}"
            sink.emit(ProposalEvent(proposal=proposal.as_event_payload()))

        _stream_answer(sink, text)
        sink.emit(AnswerDone(text=text))

        for note in ctx.notices:
            sink.emit(Notice(text=note))

        # `TierBudget.target_latency_ms` 此前只赋值、从未被读取。全市场扫描必然
        # 超出中档 30s，静默超出会让「档位预算」这条声明变成空话——如实说出实际耗时。
        elapsed_ms = int((clock.now() - started) * 1000)
        if elapsed_ms > budget.target_latency_ms:
            sink.emit(
                Notice(
                    text=(
                        f"本轮实际耗时 {elapsed_ms / 1000:.1f}s，超过该档目标延迟 "
                        f"{budget.target_latency_ms / 1000:.0f}s。"
                    )
                )
            )

        result = _finish(
            ctx, request, text, status, tool_calls, rejected_claims, clock, started, budget,
            weak_verification,
            model_label=model_label,
            proposal=proposal.as_event_payload() if proposal is not None else None,
            position_known=position_known,
            unavailable_declared=coverage_unavailable,
        )
        sink.emit(EndEvent(status=status))
        return result

    except KeyboardInterrupt:
        # 客户端断开：释放工作、按 cancelled 记账，**不**落为 completed
        text = "（回合已取消）"
        result = _finish(
            ctx, request, text, STATUS_CANCELLED, tool_calls, rejected_claims, clock, started,
            budget, weak_verification,
            model_label=model_label,
        )
        sink.emit(EndEvent(status=STATUS_CANCELLED, reason="client disconnected"))
        return result
    except Exception as exc:  # noqa: BLE001 — 任何失败都必须有终止事件
        # 不回传异常原文：上游报错里可能带 request id、内网 URL、疑似密钥片段
        # （实测模型 401 会带 Alibaba 风格 request id）。统一走分类器给出
        # "是什么问题、该做什么"，原文只进服务端日志。
        sink.emit(ErrorEvent(message=describe_turn_failure(exc)))

        # 扫描已经跑完（实测 61.5s）而模型失败时，候选表仍要交付：它是**代码渲染
        # 的扫描器原始结果**，不需要模型参与。把用户白等一分钟换来的清单丢掉，
        # 是拿诚实当借口交出无用的回答。
        fallback = "（回合失败）"
        status = STATUS_FAILED
        if request.screen and merged is not None:
            block = _render_screen_block(merged.data)
            if block:
                fallback = f"{block}\n\n（本轮未能生成解释：{describe_turn_failure(exc)}）"
                status = STATUS_DEGRADED
                _stream_answer(sink, fallback)
                sink.emit(AnswerDone(text=fallback))

        result = _finish(
            ctx, request, fallback, status, tool_calls, rejected_claims, clock,
            started, budget, weak_verification,
            model_label=model_label,
        )
        sink.emit(EndEvent(status=status, reason=failure_reason(exc)))
        return result


def _finish(
    ctx: TurnContext,
    request: TurnRequest,
    text: str,
    status: str,
    tool_calls: list[str],
    rejected_claims: int,
    clock: Any,
    started: float,
    budget: TierBudget,
    weak_verification: bool = False,
    model_label: str = "",
    proposal: Mapping[str, Any] | None = None,
    action_executed: bool = False,
    position_known: bool | None = None,
    unavailable_declared: tuple[str, ...] = (),
) -> TurnResult:
    verdict = ctx.verdict
    return TurnResult(
        tier=request.tier,
        answer=text,
        status=status,
        state=verdict.state if verdict is not None else None,
        confidence=verdict.confidence if verdict is not None else None,
        verdict_id=verdict.id if verdict is not None else None,
        tool_calls=tuple(tool_calls),
        notices=tuple(ctx.notices),
        tokens=ctx.tokens,
        model=model_label,
        model_calls=ctx.model_calls,
        latency_ms=int((clock.now() - started) * 1000),
        rejected_claims=rejected_claims,
        weak_verification=weak_verification,
        proposal=proposal,
        action_executed=action_executed,
        position_known=position_known,
        unavailable_declared=tuple(unavailable_declared),
        conversation_id=request.conversation_id,
        budget=budget,
        # 计划与逐步观测：从 ctx 读出而不是逐个调用点传参——`_finish` 有 5 处
        # 调用，漏一处就会有一类回合没有 trace（而恰好漏掉的那类最难排查）。
        plan=dict(ctx.plan),
        steps=tuple(ctx.steps),
    )
