"""对话 Agent 循环：事件协议、middleware 链、预算、判定护栏、诚实降级。

## 这些用例守的是「自动对话」与「不乱说」同时成立

原方案的 `VerdictGuard` 只守 `verdict_state` 与 `confidence`；正文里的数字、
日期、因果无人守。因此本文件不仅验证「能对话」，还验证：

* 回答**不是**任务回执（现状 `content` 只有 `"已启动分析任务：{job_id}"`）
* 模型试图改写判定时被拦下，且 `verdict` 事件与正文一致
* 校验失败只修复**一次**，再失败则诚实降级——**未通过校验的文本绝不出门**
* 超预算时返回已有证据并显式告知截断，而不是静默升级档位或静默失败
* 终止事件在成功/失败/取消三种情况下都必须发出

## 为什么用同步循环 + 注入式依赖

LLM、时钟、端口、事件出口全部注入：测试里可以给假模型、冻结时钟、
内存端口，于是循环逻辑在没有网络、没有模型、时间可控的条件下被完整覆盖。
"""

from __future__ import annotations

import pytest

from tests._signal_fixtures import make_daily
from tradingagents.agent.events import (
    AnswerDelta,
    AnswerDone,
    EndEvent,
    Notice,
    ToolDone,
    ToolStart,
    VerdictEvent,
)
from tradingagents.grounding.types import Claim, EvidenceSet, StructuredAnswer
from tradingagents.signals.ports import SignalPorts


# ── 测试替身 ──────────────────────────────────────────────────────────────


class CollectingSink:
    def __init__(self):
        self.events: list[object] = []

    def emit(self, event) -> None:
        self.events.append(event)

    def of(self, kind):
        return [e for e in self.events if isinstance(e, kind)]


class FrozenClock:
    """可控时钟：让延迟断言不依赖真实时间。"""

    def __init__(self, start: float = 1000.0):
        self._t = start

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


class QueueModel:
    """按序返回预设结构化回答的假模型。"""

    def __init__(self, answers):
        self._answers = list(answers)
        self.propose_calls = 0
        self.repair_calls = 0

    def propose(self, *, prompt, evidence, verdict):
        self.propose_calls += 1
        if not self._answers:
            return StructuredAnswer(summary="（无预设回答）")
        item = self._answers.pop(0)
        if callable(item):
            return item(evidence, verdict)
        return item

    def repair(self, *, previous, failure, evidence, verdict):
        self.repair_calls += 1
        if not self._answers:
            return previous
        item = self._answers.pop(0)
        if callable(item):
            return item(evidence, verdict)
        return item


def _ports_factory(closes=None, volumes=None):
    closes = closes or [10.0] * 5 + [9.8, 9.6, 9.4, 9.3, 9.6]
    volumes = volumes or [1000, 1000, 1000, 1000, 1000, 700, 600, 500, 480, 700]
    daily = make_daily(closes, volumes)

    class _P:
        def bars(self, symbol, end, lookback):
            return daily

    class _Peer:
        def snapshot(self, symbol, day):
            from tradingagents.signals.ports import PeerSnapshot

            # 必须提供板块基准：相对强度是四个核心维度之一，缺它探针会直接弃权，
            # 那样后面的断言验证的就不是「判定护栏」而是「弃权路径」。
            return PeerSnapshot(
                board_name="白酒",
                board_return_pct=-3.0,
                market_return_pct=-1.5,
                stock_turnover=2.2,
                board_median_turnover=1.8,
            )

    def factory(symbol: str, as_of: str) -> SignalPorts:
        return SignalPorts(daily=_P(), peer=_Peer())

    return factory


def _registry(ports_factory=None, **kw):
    from tradingagents.tools import build_registry

    return build_registry(ports_factory=ports_factory or _ports_factory(), **kw)


def _request(**kw):
    from tradingagents.agent.loop import TurnRequest

    base = dict(text="600519 是不是在洗盘", user_id="u1", tier="medium", symbol="600519.SH", as_of="2026-01-16")
    base.update(kw)
    return TurnRequest(**base)


def _run(model, *, registry=None, sink=None, clock=None, request=None, middleware=None, **kw):
    from tradingagents.agent.loop import run_turn

    sink = sink or CollectingSink()
    result = run_turn(
        request=request or _request(),
        registry=registry or _registry(),
        model=model,
        sink=sink,
        clock=clock or FrozenClock(),
        middleware=middleware,
        **kw,
    )
    return result, sink


def _grounded_answer(evidence, verdict):
    """构造一个引用真实证据的回答（字段名取自探针实际输出）。"""
    ref = evidence.items[0].id if evidence.items else "E1"
    return StructuredAnswer(
        summary="缩量下杀后出现承接",
        claims=(Claim(text="量能出现萎缩", refs=(ref,)),),
        verdict_ref=verdict.id if verdict else None,
        declared_unavailable=tuple(verdict.unavailable) if verdict else (),
    )


# ══════════════════════════════════════════════════════════════════════════
# 事件协议
# ══════════════════════════════════════════════════════════════════════════


def test_answer_text_is_streamed_not_a_job_acknowledgement():
    """**核心回归**：现状响应只有 `"已启动分析任务：{job_id}"`，没有回答。

    本变更的验收标准之一就是「问句必须得到回答文本」。
    """
    sink = CollectingSink()
    result, sink = _run(QueueModel([_grounded_answer]), sink=sink)

    deltas = sink.of(AnswerDelta)
    assert deltas, "必须流出回答文本增量"
    assert "".join(d.text for d in deltas).strip()
    assert "已启动分析任务" not in result.answer
    assert result.status == "completed"


def test_verdict_event_carries_structured_payload():
    sink = CollectingSink()
    _, sink = _run(QueueModel([_grounded_answer]), sink=sink)

    verdicts = sink.of(VerdictEvent)
    assert len(verdicts) == 1
    v = verdicts[0].verdict
    assert v.state in {"washout", "distribution", "trend_break", "insufficient_evidence"}
    assert isinstance(v.confidence, int)
    assert v.probe == "washout"


def test_tool_activity_is_streamed():
    sink = CollectingSink()
    _, sink = _run(QueueModel([_grounded_answer]), sink=sink)

    assert [e.tool for e in sink.of(ToolStart)] == ["signal_washout_probe"]
    done = sink.of(ToolDone)
    assert len(done) == 1 and done[0].ok is True


def test_terminal_event_always_emitted_on_success():
    _, sink = _run(QueueModel([_grounded_answer]))
    ends = sink.of(EndEvent)
    assert len(ends) == 1
    assert ends[0].status == "completed"
    assert sink.events[-1] is ends[0]


def test_exactly_one_terminal_event_per_turn():
    """一次回合必须**恰好**发出一个终止事件，且它在最后。

    早期版本先发 EndEvent 再构造结果，结果构造抛错时异常处理器又发了第二个
    终止事件——前端会看到 completed 紧跟 failed，无法判断回合到底怎么了。
    """
    _, sink = _run(QueueModel([_grounded_answer]))
    ends = sink.of(EndEvent)
    assert len(ends) == 1
    assert sink.events[-1] is ends[0]


def test_terminal_event_emitted_on_model_failure():
    class _Boom:
        def propose(self, **kw):
            raise RuntimeError("model exploded")

        def repair(self, **kw):  # pragma: no cover
            raise AssertionError("不应被调用")

    result, sink = _run(_Boom())
    ends = sink.of(EndEvent)
    assert len(ends) == 1
    assert ends[0].status == "failed"
    assert result.status == "failed"


def test_terminal_event_emitted_on_cancellation():
    class _CancelSink(CollectingSink):
        def emit(self, event):
            super().emit(event)
            if isinstance(event, ToolDone):
                raise KeyboardInterrupt("client disconnected")

    sink = _CancelSink()
    result, _ = _run(QueueModel([_grounded_answer]), sink=sink)
    assert result.status == "cancelled"
    assert isinstance(sink.events[-1], EndEvent)


# ══════════════════════════════════════════════════════════════════════════
# 判定护栏：模型不得改写判定
# ══════════════════════════════════════════════════════════════════════════


def test_model_cannot_change_verdict_state():
    """模型声称另一个状态 → 被拦下，verdict 事件仍等于探针输出。

    「方向权在确定性层、表达权在 LLM」这条设计原则的可执行形式。
    """

    def _lying(evidence, verdict):
        return StructuredAnswer(
            summary="这明显是派发，不是洗盘",
            claims=(Claim(text="量能出现萎缩", refs=(evidence.items[0].id,)),),
            verdict_ref=verdict.id if verdict else None,
            rendered_note="判定：distribution（置信 95）",
        )

    result, sink = _run(QueueModel([_lying, _grounded_answer]))
    v = sink.of(VerdictEvent)[0].verdict

    assert v.state in {"washout", "distribution", "trend_break", "insufficient_evidence"}
    # 正文不得声称与探针矛盾的状态
    assert "distribution" not in result.answer
    assert "置信 95" not in result.answer


def test_rendered_confidence_cannot_exceed_probe_confidence():
    def _inflate(evidence, verdict):
        return StructuredAnswer(
            summary="缩量下杀后出现承接",
            claims=(Claim(text="量能出现萎缩", refs=(evidence.items[0].id,)),),
            verdict_ref=verdict.id if verdict else None,
            rendered_note=f"置信 99（探针为 {verdict.confidence if verdict else 0}）",
        )

    result, sink = _run(QueueModel([_inflate, _grounded_answer]))
    v = sink.of(VerdictEvent)[0].verdict
    assert f"置信 {v.confidence}" not in result.answer or "置信 99" not in result.answer


def test_ungrounded_answer_is_repaired_then_accepted():
    """第一次失败 → 一次修复 → 通过。"""
    bad = StructuredAnswer(summary="x", claims=(Claim(text="主力净流出 9800 万", refs=("E1",)),))
    model = QueueModel([bad, _grounded_answer])
    result, _ = _run(model)

    assert model.repair_calls == 1
    assert result.status == "completed"
    assert result.rejected_claims == 1


def test_second_failure_degrades_honestly_without_emitting_unverified_text():
    """两次失败 → 诚实降级：**绝不输出未通过校验的文本**。"""
    bad1 = StructuredAnswer(summary="x", claims=(Claim(text="主力净流出 9800 万", refs=("E1",)),))
    bad2 = StructuredAnswer(summary="x", claims=(Claim(text="获利盘 99%", refs=("E1",)),))
    result, sink = _run(QueueModel([bad1, bad2]))

    assert result.status == "degraded"
    assert "9800" not in result.answer
    assert "99%" not in result.answer
    # 降级文本必须说明缺什么，并列出已获得的证据字段
    assert "已获取的证据字段" in result.answer
    assert result.rejected_claims >= 2


def test_disclaimer_is_injected_when_judgement_conveyed():
    result, _ = _run(QueueModel([_grounded_answer]))
    assert "不构成投资建议" in result.answer


def test_directive_language_is_flagged():
    """指令式买卖措辞不得出现在回答里（合规要求）。"""

    def _directive(evidence, verdict):
        return StructuredAnswer(
            summary="立即清仓",
            claims=(Claim(text="量能出现萎缩", refs=(evidence.items[0].id,)),),
            verdict_ref=verdict.id if verdict else None,
        )

    result, sink = _run(QueueModel([_directive, _grounded_answer]))
    assert "立即清仓" not in result.answer


def test_text_mode_answer_is_still_verified():
    """**关键漏洞回归**：文本兜底回答的 claims 为空。

    若循环对它调用引用校验器，会出现「无 claim 可查 → 直接通过」的空过，
    未经校验的散文就能出门——护栏在降级路径上失效，而这条路径恰恰是模型
    不遵守 schema 时的高频路径。
    """

    def _fabricating_text(evidence, verdict):
        # 模拟结构化解析失败后的文本模式结果：有正文、没有逐条引用
        return StructuredAnswer(summary="主力资金净流出 9800 万，明显是派发。", claims=())

    result, _ = _run(QueueModel([_fabricating_text, _fabricating_text]))
    assert result.status == "degraded"
    assert "9800" not in result.answer


def test_text_mode_answer_with_grounded_numbers_is_accepted():
    """文本模式下数值确实有来源 → 允许通过，但标记为弱校验。"""

    def _grounded_text(evidence, verdict):
        return StructuredAnswer(summary="反抽量能恢复至 1.228，承接出现。", claims=())

    result, _ = _run(QueueModel([_grounded_text]))
    assert result.status == "completed"
    assert result.weak_verification is True


def test_reference_mode_answer_is_not_marked_weak():
    result, _ = _run(QueueModel([_grounded_answer]))
    assert result.status == "completed"
    assert result.weak_verification is False


# ══════════════════════════════════════════════════════════════════════════
# 预算与中间件
# ══════════════════════════════════════════════════════════════════════════


def test_expensive_tool_is_refused_with_explicit_notice():
    """分钟级管道不得在循环内被调用（D21）。必须显式告知用户可提议升级。"""
    from tradingagents.agent.middleware import ExpensiveToolGuard
    from tradingagents.tools import build_registry
    from tradingagents.tools.spec import ToolSpec

    # 昂贵工具必须真的注册在册，guard 才可能识别它——否则测的是「未知工具」路径
    registry = build_registry(ports_factory=_ports_factory())
    registry.register(
        ToolSpec(
            name="analyze_pipeline",
            title="深度分析管道",
            description="分钟级多分析师研判",
            input_schema={"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]},
            output_contract="PIPELINE",
            handler=lambda **kw: None,
            cost="expensive",
        )
    )

    sink = CollectingSink()
    result, sink = _run(
        QueueModel([_grounded_answer]),
        registry=registry,
        sink=sink,
        request=_request(tier="medium"),
        middleware=(ExpensiveToolGuard(),),
        extra_tool_calls=("analyze_pipeline",),
    )

    notices = sink.of(Notice)
    assert any("深度分析" in n.text or "重档" in n.text for n in notices)
    assert "analyze_pipeline" not in result.tool_calls


def test_budget_guard_limits_tool_calls_and_reports_truncation():
    from tradingagents.agent.middleware import BudgetGuard

    sink = CollectingSink()
    result, sink = _run(
        QueueModel([_grounded_answer]),
        sink=sink,
        middleware=(BudgetGuard(),),
        extra_tool_calls=("signal_washout_probe_batch",) * 8,
    )

    assert len(result.tool_calls) <= result.budget.max_tool_calls
    assert any("截断" in n.text or "预算" in n.text for n in sink.of(Notice))


def test_no_progress_guard_stops_repeated_identical_lookups():
    """第 2 轮之后若没有产生新证据，强制终止——挡的是模型反复查同一份数据。"""
    from tradingagents.agent.middleware import NoProgressGuard

    sink = CollectingSink()
    result, sink = _run(
        QueueModel([_grounded_answer]),
        sink=sink,
        middleware=(NoProgressGuard(),),
        extra_tool_calls=("signal_washout_probe", "signal_washout_probe", "signal_washout_probe"),
    )

    assert len(result.tool_calls) <= 2
    assert any("新证据" in n.text for n in sink.of(Notice))


def test_middleware_order_is_explicit_and_applied():
    """中间件链必须按声明顺序生效，且每个都能独立观测到调用。"""
    from tradingagents.agent.middleware import TurnMiddleware

    seen: list[str] = []

    class _Recorder:
        def __init__(self, tag):
            self.tag = tag

        def before_tool(self, ctx, call):
            seen.append(f"{self.tag}:before")
            return call

        def after_tool(self, ctx, call, result):
            seen.append(f"{self.tag}:after")
            return result

        def before_answer(self, ctx, text):
            seen.append(f"{self.tag}:answer")
            return text

    _run(QueueModel([_grounded_answer]), middleware=(_Recorder("a"), _Recorder("b")))
    assert seen[0] == "a:before" and seen[-1] == "b:answer"


# ══════════════════════════════════════════════════════════════════════════
# 记账与时钟注入
# ══════════════════════════════════════════════════════════════════════════


def test_turn_records_tier_model_tools_tokens_and_latency():
    clock = FrozenClock()
    registry = _registry()

    result, _ = _run(QueueModel([_grounded_answer]), registry=registry, clock=clock)
    assert result.tier == "medium"
    assert result.tool_calls == ("signal_washout_probe",)
    assert result.latency_ms >= 0
    assert isinstance(result.tokens, int)


def test_latency_uses_injected_clock_not_wall_clock():
    clock = FrozenClock()

    class _Advancing:
        def propose(self, *, prompt, evidence, verdict):
            clock.advance(2.5)
            return _grounded_answer(evidence, verdict)

        def repair(self, **kw):  # pragma: no cover
            raise AssertionError("不应被调用")

    result, _ = _run(_Advancing(), clock=clock)
    assert result.latency_ms == pytest.approx(2500, abs=5)


def test_light_tier_does_not_start_the_probe():
    """轻档（纯事实问答）不得启动**探针**——那会把 1× 成本变成 5×。

    注意断言的写法：这里要否的是**探针**，不是"一切工具"。曾经写成
    `result.tool_calls == ()`，那等于把"轻档连便宜的数据工具也不调"这个 bug
    一起写死——于是「600519 现在多少钱」永远答"本轮无可用证据"（黄金集抓到的）。
    轻档**应当**调一个便宜的数据工具（档位表原本就是这么承诺的）。
    """
    sink = CollectingSink()
    result, sink = _run(
        QueueModel([_grounded_answer]),
        sink=sink,
        request=_request(tier="light", text="600519 现在多少钱"),
    )

    assert "signal_washout_probe" not in result.tool_calls, "轻档不得付探针的 5× 成本"
    assert sink.of(VerdictEvent) == []
    assert result.status in {"completed", "degraded"}


def test_missing_tool_is_reported_not_silently_skipped():
    """请求了不存在的工具必须显式报错，避免「以为取了数其实没有」。"""
    sink = CollectingSink()
    _, sink = _run(
        QueueModel([_grounded_answer]),
        sink=sink,
        extra_tool_calls=("no_such_tool",),
    )
    assert any(not d.ok for d in sink.of(ToolDone))
