"""L1 决策工具：把平台**已有结论**接进工具面（只读、不改写）。

## 为什么这是 L1（决策层）而不是 L2（证据层）

「我最近的判定是什么」「上次那个判定兑现了吗」——问的是一个**判断的结论**，
不是一条数据。平台已经把它算好并落库（`signal_verdicts` + T+3/T+5 回填），
Agent 的职责是**读取与解释**，而不是重新生成一个结论。

## 为什么必须走端口注入

这些数据属于**某个用户**且来自应用层（要读库）。工具层不得 import `api/`
（AST 边界测试强制），所以端口在工具层声明、由应用层实现并在装配处注入——
与 `signal_ports` 同一套做法。用户维度在端口的闭包里绑定，工具参数里
**不出现 user_id**：让模型有机会传 user_id 就等于给它越权的可能。

## 弃权

没有任何判定时**不返回空列表当成功**：返回 `unavailable=("verdict_history",)`
与一句可读说明，要求回答声明"还没有记录"。空结果被当成"查过了、没有风险"
是本平台最不能接受的失败形态。
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence

from tradingagents.grounding.types import Coverage, EvidenceItem

from .spec import PLAN_LAYER_DECISION, RESEARCH_DISCLAIMER, ToolResult, ToolSpec

HISTORY_CONTRACT = "VERDICT_HISTORY"
_SOURCE_KEY = "verdict_history"


class VerdictHistoryPort(Protocol):
    """读取当前用户的判定历史。实现由应用层提供（闭包内绑定 user_id）。"""

    def recent_verdicts(self, *, limit: int, symbol: str | None = None) -> Sequence[Mapping[str, Any]]: ...


def _fmt(value: Any) -> str:
    if value in (None, ""):
        return "未提供"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def make_history_tools(history: VerdictHistoryPort) -> tuple[ToolSpec, ...]:
    def verdict_history_read(
        *, limit: int = 10, symbol: str | None = None, as_of: str = ""
    ) -> ToolResult:
        # 注意不能写 `limit or 10`：limit=0 会被 falsy 换成 10（本项目已第二次
        # 踩这类坑，前一次是 `code or -1` 把成功的 code=0 判成失败）
        requested = 10 if limit is None else int(limit)
        capped = max(1, min(requested, 50))
        try:
            rows = list(history.recent_verdicts(limit=capped, symbol=symbol) or [])
        except Exception as exc:  # noqa: BLE001 — 取不到就如实声明，不抛给模型
            return ToolResult(
                data={"verdicts": [], "error": type(exc).__name__},
                evidence=(),
                coverage=Coverage(available=(), unavailable=("verdict_history",)),
                degraded=True,
                degraded_reason=f"判定历史读取失败（{type(exc).__name__}）",
                as_of=as_of,
                disclaimer=RESEARCH_DISCLAIMER,
            )

        if not rows:
            return ToolResult(
                data={"verdicts": [], "count": 0},
                evidence=(),
                coverage=Coverage(
                    available=(_SOURCE_KEY,), unavailable=("尚未落库的判定记录",)
                ),
                as_of=as_of,
                disclaimer=RESEARCH_DISCLAIMER,
            )

        evidence: list[EvidenceItem] = []
        for index, row in enumerate(rows, start=1):
            parts = [
                f"{_fmt(row.get('symbol'))}（{_fmt(row.get('as_of'))}）",
                f"判定 {_fmt(row.get('state'))}",
                f"置信度 {_fmt(row.get('confidence'))}",
            ]
            outcome = row.get("outcome") or {}
            if isinstance(outcome, Mapping) and outcome:
                parts.append(
                    "兑现：" + "；".join(f"{key} {_fmt(value)}" for key, value in outcome.items())
                )
            evidence.append(
                EvidenceItem(
                    id=f"E{index}",
                    field=f"历史判定{index}",
                    value="｜".join(parts),
                    unit="",
                    source_key=_SOURCE_KEY,
                    as_of=str(row.get("as_of") or as_of),
                )
            )

        return ToolResult(
            data={"verdicts": [dict(row) for row in rows], "count": len(rows)},
            evidence=tuple(evidence),
            coverage=Coverage(available=(_SOURCE_KEY,), unavailable=()),
            as_of=as_of,
            disclaimer=RESEARCH_DISCLAIMER,
        )

    spec = ToolSpec(
        name="verdict_history_read",
        title="判定历史（含兑现）",
        description=(
            "读取当前用户**已落库的洗盘/派发判定历史**，以及判定后 T+3/T+5 的实际兑现情况。"
            "用于回答「我最近的判定是什么」「上次那个判定兑现了吗」。"
            "结论来自平台确定性信号层的历史记录，不接受模型改写；没有记录时会显式声明。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "返回条数，1-50，默认 10"},
                "symbol": {"type": "string", "description": "只看某只标的（可选）"},
                "as_of": {"type": "string", "description": "基准日 YYYY-MM-DD"},
            },
            "required": [],
        },
        output_contract=HISTORY_CONTRACT,
        handler=verdict_history_read,
        cost="cheap",
        plan_layer=PLAN_LAYER_DECISION,
        latency_budget_ms=8000,
        freshness_sources=(_SOURCE_KEY,),
    )
    return (spec,)


# ── 校准权重版本（L1 决策）────────────────────────────────────────────────
#
# 问「当前洗盘判定用的是哪一版权重」问的是**平台的可追溯性**：判定的结论依赖
# 权重，而权重可被数据校准。答不出用哪一版，命中率就无法归因。


class WeightVersionPort(Protocol):
    def active_version(self) -> str | None: ...

    def list_versions(self) -> Sequence[Mapping[str, Any]]: ...


def make_weight_tools(weights: WeightVersionPort) -> tuple[ToolSpec, ...]:
    def signal_weight_read(*, as_of: str = "") -> ToolResult:
        try:
            active = weights.active_version()
            versions = list(weights.list_versions() or [])
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                data={"active": None, "error": type(exc).__name__},
                evidence=(),
                coverage=Coverage(available=(), unavailable=("signal_weights",)),
                degraded=True,
                degraded_reason=f"权重版本读取失败（{type(exc).__name__}）",
                as_of=as_of,
                disclaimer=RESEARCH_DISCLAIMER,
            )

        # 「没有激活版本」是可回答的**事实**（此刻用的是内置先验），不是"未覆盖"。
        # 把它声明成 unavailable 会让回答凭空多出一条"未覆盖"噪音，而用户真正
        # 需要知道的是"现在用的是先验权重"。所以永远产出一条事实证据。
        evidence: list[EvidenceItem] = []
        if active:
            evidence.append(
                EvidenceItem(
                    id="E1", field="当前生效权重版本", value=str(active), unit="",
                    source_key="signal_weights", as_of=as_of,
                )
            )
        else:
            # 「没有激活版本」本身是一条事实：此时判定用内置先验，必须说出来
            evidence.append(
                EvidenceItem(
                    id="E1", field="当前生效权重版本",
                    value="未激活任何校准版本，判定使用内置先验权重", unit="",
                    source_key="signal_weights", as_of=as_of,
                )
            )
        for index, row in enumerate(versions[:10], start=2):
            parts = [
                f"版本 {_fmt(row.get('version'))}",
                f"激活于 {_fmt(row.get('activated_at'))}",
                f"来源 {_fmt(row.get('source') or row.get('reason'))}",
            ]
            evidence.append(
                EvidenceItem(
                    id=f"E{index}", field=f"权重版本{index - 1}", value="｜".join(parts),
                    unit="", source_key="signal_weights", as_of=as_of,
                )
            )

        return ToolResult(
            data={"active": active, "versions": [dict(row) for row in versions[:10]]},
            evidence=tuple(evidence),
            coverage=Coverage(available=("signal_weights",), unavailable=()),
            as_of=as_of,
            disclaimer=RESEARCH_DISCLAIMER,
        )

    spec = ToolSpec(
        name="signal_weight_read",
        title="当前判定权重版本",
        description=(
            "读取洗盘/派发判定当前生效的**权重校准版本**及其来源。"
            "用于回答「当前判定用的是哪一版权重」。权重可被历史数据校准，"
            "答不出用哪一版，命中率就无法归因。"
        ),
        input_schema={
            "type": "object",
            "properties": {"as_of": {"type": "string", "description": "基准日 YYYY-MM-DD"}},
            "required": [],
        },
        output_contract="SIGNAL_WEIGHTS",
        handler=signal_weight_read,
        cost="cheap",
        plan_layer=PLAN_LAYER_DECISION,
        latency_budget_ms=5000,
        freshness_sources=("signal_weights",),
    )
    return (spec,)
