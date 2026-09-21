"""L1 决策工具：平台结论的 T+1 质量（命中率与其**统计效力**）。

## 为什么"准不准"必须由平台回答，而且必须带样本效力

用户问「平台推荐得准吗」「你这些判断靠谱吗」。回答这个问题需要三个量：
命中率、**置信区间**、以及**达到 80% 功效需要多少独立样本**。平台已经算好了
（`insights_t1_service.report_accuracy_t1_summary`），其中：

* `beats_coin_flip` —— 区间下界 > 50%，**有统计证据**优于掷硬币；
* `not_significant` —— 区间跨过 50%，现有样本**无法区分**于掷硬币；
* `required_n` / `underpowered` —— 样本远不够时，"命中率 53%"不该被当结论。

后两者是本工具存在的理由：只报一个 53% 而不报它的效力，就是在制造一个
看起来有信息量、实际无法区分的数字——这正是本平台反复强调的"不得伪装"。

## 弃权与降级

* 无已评估样本 → `unavailable`（服务会返回 `note="无已评估样本"` 且
  `accuracy_pct=None`；这不是"命中率 0%"）；
* 取数失败 → `degraded=True`，**不外传上游原文**；
* `days` / `scope` 一律 clamp，且不写 `x or default`（0 会被 falsy 换掉）；
* 用户维度在端口闭包里绑定，工具参数里没有 user_id。
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol

from tradingagents.grounding.types import Coverage, EvidenceItem

from .spec import PLAN_LAYER_DECISION, RESEARCH_DISCLAIMER, ToolResult, ToolSpec

T1_QUALITY_CONTRACT = "T1_QUALITY"
_SOURCE_KEY = "t1_quality"
DEFAULT_DAYS = 90
MIN_DAYS = 7
MAX_DAYS = 400
SCOPES = ("all", "portfolio")
DEFAULT_SCOPE = "all"

_VERDICT_LABELS = {
    "beats_coin_flip": "有统计证据优于掷硬币",
    "not_significant": "现有样本无法区分于掷硬币（不得当成「有效」）",
    "insufficient_data": "样本不足，无法判定",
}


class T1QualityPort(Protocol):
    """读取当前用户的 T+1 质量区间汇总（实现由应用层提供，闭包内绑定 user_id）。

    返回 `insights_t1_service.report_accuracy_t1_summary` 的结构：
    `accuracy_pct / ci_low_pct / ci_high_pct / effective_n / n_days / hits /
    required_n / underpowered / verdict / clustered / abstain_count /
    coverage_pct / unique_symbols / top_symbol_share / conflict_n / excess /
    window_start / window_end / note`。
    """

    def t1_quality(self, *, days: int, scope: str) -> Mapping[str, Any]: ...


def _fmt(value: Any, *, digits: int = 2, suffix: str = "") -> str:
    if value in (None, ""):
        return ""
    try:
        return f"{float(value):.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return str(value)


def make_quality_tools(quality: T1QualityPort) -> tuple[ToolSpec, ...]:
    def t1_quality_read(
        *, days: int = DEFAULT_DAYS, scope: str = DEFAULT_SCOPE, as_of: str = ""
    ) -> ToolResult:
        # 两个都不能写 `x or default`：days=0 会被 falsy 换成默认值
        requested_days = DEFAULT_DAYS if days is None else int(days)
        window_days = max(MIN_DAYS, min(requested_days, MAX_DAYS))
        resolved_scope = str(scope or "").strip().lower()
        if resolved_scope not in SCOPES:
            resolved_scope = DEFAULT_SCOPE
        try:
            payload = quality.t1_quality(days=window_days, scope=resolved_scope)
        except Exception as exc:  # noqa: BLE001 — 取不到就如实声明，不抛给模型
            return ToolResult(
                data={"quality": {}, "error": type(exc).__name__},
                evidence=(),
                coverage=Coverage(available=(), unavailable=(_SOURCE_KEY,)),
                degraded=True,
                degraded_reason=f"T+1 质量读取失败（{type(exc).__name__}）",
                as_of=as_of,
                disclaimer=RESEARCH_DISCLAIMER,
            )

        if not isinstance(payload, Mapping):
            return ToolResult(
                data={"quality": {}, "error": "unparsable_payload"},
                evidence=(),
                coverage=Coverage(available=(), unavailable=(_SOURCE_KEY,)),
                degraded=True,
                degraded_reason="T+1 质量结构无法解析（不是映射）",
                as_of=as_of,
                disclaimer=RESEARCH_DISCLAIMER,
            )

        accuracy = payload.get("accuracy_pct")
        effective_n = payload.get("effective_n")
        if accuracy is None or not effective_n:
            # 没有已评估样本 ≠ 命中率 0%：必须显式声明未覆盖
            note = str(payload.get("note") or "无已评估样本")
            return ToolResult(
                data={"quality": dict(payload), "count": 0},
                evidence=(),
                coverage=Coverage(
                    available=(_SOURCE_KEY,),
                    unavailable=(f"t1_quality（{note}：区间内没有已评估的 T+1 结果）",),
                ),
                as_of=as_of,
                disclaimer=RESEARCH_DISCLAIMER,
            )

        window_start = str(payload.get("window_start") or "").strip()
        window_end = str(payload.get("window_end") or "").strip()
        head_parts = [
            f"{window_start} 至 {window_end}" if window_start or window_end else "",
            f"口径 {payload.get('scope') or resolved_scope}",
            f"有效样本 {int(effective_n)}",
            f"命中率 {_fmt(accuracy, suffix='%')}",
        ]
        ci_low = _fmt(payload.get("ci_low_pct"), suffix="%")
        ci_high = _fmt(payload.get("ci_high_pct"), suffix="%")
        if ci_low and ci_high:
            head_parts.append(f"95% 置信区间 {ci_low}–{ci_high}")

        verdict = str(payload.get("verdict") or "").strip()
        verdict_text = _VERDICT_LABELS.get(verdict, verdict or "未判定")
        head_parts.append(f"结论 {verdict_text}")

        required_n = payload.get("required_n")
        if required_n not in (None, ""):
            # 样本效力必须与命中率同时出现，否则 53% 会被当成结论
            head_parts.append(f"80% 功效所需样本 {int(required_n)}")
        if payload.get("underpowered"):
            head_parts.append("当前样本效力不足（underpowered）")

        evidence: list[EvidenceItem] = [
            EvidenceItem(
                id="E1",
                field="T+1 命中率（含效力）",
                value="｜".join(part for part in head_parts if part),
                unit="",
                source_key=_SOURCE_KEY,
                as_of=window_end or as_of,
            )
        ]

        detail_parts = [
            f"命中 {int(payload.get('hits') or 0)}／样本 {int(effective_n)}",
            f"覆盖交易日 {int(payload.get('n_days') or 0)}",
            f"弃权 {int(payload.get('abstain_count') or 0)}",
        ]
        coverage_pct = _fmt(payload.get("coverage_pct"), suffix="%")
        if coverage_pct:
            detail_parts.append(f"可评估覆盖率 {coverage_pct}")
        unique_symbols = payload.get("unique_symbols")
        if unique_symbols not in (None, ""):
            detail_parts.append(f"涉及标的 {int(unique_symbols)}")
        top_share = _fmt(payload.get("top_symbol_share"))
        if top_share:
            # 集中度必须可见：一个标的贡献了大部分样本时，命中率不是"广度"的证据
            detail_parts.append(f"最大标的样本占比 {top_share}")
        conflict_n = payload.get("conflict_n")
        if conflict_n not in (None, ""):
            detail_parts.append(f"与计划周期冲突 {int(conflict_n)}")
        if payload.get("clustered"):
            detail_parts.append("样本存在聚集（clustered），显著性判定已按聚集修正")
        evidence.append(
            EvidenceItem(
                id="E2",
                field="样本构成",
                value="｜".join(detail_parts),
                unit="",
                source_key=_SOURCE_KEY,
                as_of=window_end or as_of,
            )
        )

        excess = payload.get("excess")
        if isinstance(excess, Mapping) and excess:
            excess_parts = [
                f"超额命中率 {_fmt(excess.get('accuracy_pct'), suffix='%')}"
                if excess.get("accuracy_pct") is not None
                else "",
                f"超额有效样本 {int(excess['effective_n'])}"
                if excess.get("effective_n") not in (None, "")
                else "",
            ]
            # `excess_spread` 是**结构化块**（多空超额与是否覆盖交易成本），
            # 不是标量：直接 str() 会把整个 dict 塞进证据文本，等于没给可引用数值。
            spread = payload.get("excess_spread")
            if isinstance(spread, Mapping) and spread:
                bull = _fmt(spread.get("bullish_mean_excess_pct"))
                bear = _fmt(spread.get("bearish_mean_excess_pct"))
                if bull:
                    excess_parts.append(f"看多样本 {int(spread.get('bullish_n') or 0)} 个平均超额 {bull}")
                if bear:
                    excess_parts.append(f"看空样本 {int(spread.get('bearish_n') or 0)} 个平均超额 {bear}")
                ls = _fmt(spread.get("long_short_spread_pct"))
                if ls:
                    excess_parts.append(f"多空价差 {ls}")
                if "covers_cost" in spread:
                    cost = _fmt(spread.get("cost_round_trip_pct"))
                    excess_parts.append(
                        "多空价差" + ("覆盖" if spread.get("covers_cost") else "未覆盖")
                        + f"往返成本（{cost}%）"
                    )
            joined = "｜".join(part for part in excess_parts if part)
            if joined:
                # 绝对口径会被 beta 抬高，相对口径才是能力体现——两个都要给
                evidence.append(
                    EvidenceItem(
                        id="E3",
                        field="去 beta 口径",
                        value=joined,
                        unit="",
                        source_key=_SOURCE_KEY,
                        as_of=window_end or as_of,
                    )
                )

        return ToolResult(
            data={"quality": dict(payload), "count": 1},
            evidence=tuple(evidence),
            coverage=Coverage(available=(_SOURCE_KEY,), unavailable=()),
            as_of=as_of,
            disclaimer=RESEARCH_DISCLAIMER,
        )

    spec = ToolSpec(
        name="t1_quality_read",
        title="平台结论的 T+1 质量（命中率 + 统计效力）",
        description=(
            "读取平台自身结论的 **T+1 方向命中率**：区间命中率、95% 置信区间、"
            "有效样本量、达到 80% 功效所需样本、样本集中度、弃权数与可评估覆盖率，"
            "以及去 beta 的超额口径。用于回答「平台推荐得准吗」「这些判断靠谱吗」。"
            "命中率必须与样本效力一起解读：区间跨过 50% 时现有样本**无法区分**于掷硬币，"
            "不得当作「有效」。没有已评估样本时会显式声明（不是 0% 命中率）。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": f"统计窗口天数，{MIN_DAYS}-{MAX_DAYS}，默认 {DEFAULT_DAYS}",
                },
                "scope": {
                    "type": "string",
                    "enum": list(SCOPES),
                    "description": f"统计口径，默认 {DEFAULT_SCOPE}（all=全部报告，portfolio=只看组合快照口径）",
                },
                "as_of": {"type": "string", "description": "基准日 YYYY-MM-DD"},
            },
            "required": [],
        },
        output_contract=T1_QUALITY_CONTRACT,
        handler=t1_quality_read,
        cost="cheap",
        plan_layer=PLAN_LAYER_DECISION,
        latency_budget_ms=8000,
        freshness_sources=(_SOURCE_KEY,),
    )
    return (spec,)
