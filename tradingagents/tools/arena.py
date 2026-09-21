"""L1 决策工具：模型对比榜（model arena）。

## 「哪个模型更准」不能由模型自述

问一个模型"你和其他模型比谁准"，它只能编。平台有一条评测链路：报告落库时记录
模型身份（provider / quick_think_llm / deep_think_llm / profile），T+1 结果回填后
按模型聚合出准确率、胜率、平均收益、波动与综合分，并**显式剔除日期漂移
（date drift）记录**——那些行的价格不可靠，会把统计带偏。

因此这个工具只做一件事：把平台的评测产出读出来。结论（排名、准确率）来自
`model_arena_service.build_model_leaderboard`，不是模型的自我评价。

## 证据里为什么必须有样本量与窗口

一条 61.9% 的准确率，在 24 个样本上和 3 个样本上完全不是一回事；不写窗口，
结论就无法复核（"这三个月"到底是哪三个月）。所以证据里同时给：窗口起止、
口径（portfolio / all）、总样本量、每个模型的样本量。这些是**可复核性**的
最低要求，不是可选的装饰字段。

## 弃权与降级

* 窗口内没有可比样本 → `unavailable`，不编排名；
* 取数失败 → `degraded=True` + 可读原因，**不外传上游原文**；
* 用户维度（策略口径按用户过滤）在端口的闭包里绑定，工具参数里没有 user_id。
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol

from tradingagents.grounding.types import Coverage, EvidenceItem

from .spec import PLAN_LAYER_DECISION, RESEARCH_DISCLAIMER, ToolResult, ToolSpec

MODEL_ARENA_CONTRACT = "MODEL_ARENA_LEADERBOARD"
_SOURCE_KEY = "model_arena"
MAX_EVIDENCE = 20
DEFAULT_DAYS = 90
MIN_DAYS = 7
MAX_DAYS = 400

# 评测口径。**默认 "all"**：实测某真实用户 scope="portfolio"（只看组合快照报告口径）
# 只有 16 个样本、榜上 0 行，而 scope="all" 有 2238 个样本、5 个模型——用前者回答
# 「哪个模型更准」几乎总是"没有样本"，那是把"口径太窄"说成"平台没评测过"。
# 无论用哪个口径，实际口径都会写进证据，回答层看得到自己拿到的是什么。
DEFAULT_SCOPE = "all"
SCOPES = ("portfolio", "all")


class ModelArenaPort(Protocol):
    """读取模型评测榜（实现由应用层提供，闭包内绑定 user_id）。

    返回 `model_arena_service.build_model_leaderboard` 的结构：
    `{"window_start", "window_end", "scope", "total_samples", "leaderboard": [...],
    "generated_at"}`，榜内每行含 `rank / model_key / model_display_name /
    llm_provider / sample_count / symbol_count / date_count / accuracy_pct /
    win_rate_pct / avg_return_t1_pct / return_volatility / composite_score`。
    """

    def leaderboard(self, *, limit: int, days: int, scope: str) -> Mapping[str, Any]: ...


def _fmt(value: Any, *, digits: int = 2, suffix: str = "") -> str:
    if value in (None, ""):
        return ""
    try:
        return f"{float(value):.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return str(value)


def _model_text(row: Mapping[str, Any], fallback_rank: int) -> str:
    rank = row.get("rank") or fallback_rank
    name = str(
        row.get("model_display_name") or row.get("model_key") or "未命名模型"
    ).strip()
    provider = str(row.get("llm_provider") or "").strip()
    parts = [f"第{rank}名 {name}" + (f"（{provider}）" if provider else "")]

    samples = row.get("sample_count")
    if samples not in (None, ""):
        # 样本量必须与准确率同时出现：没有样本量的准确率不可信
        parts.append(f"样本 {int(samples)}")
    accuracy = _fmt(row.get("accuracy_pct"), suffix="%")
    if accuracy:
        parts.append(f"准确率 {accuracy}")
    win_rate = _fmt(row.get("win_rate_pct"), suffix="%")
    if win_rate:
        parts.append(f"胜率 {win_rate}")
    avg_return = _fmt(row.get("avg_return_t1_pct"), suffix="%")
    if avg_return:
        parts.append(f"平均T+1 {avg_return}")
    volatility = _fmt(row.get("return_volatility"))
    if volatility:
        parts.append(f"收益波动 {volatility}")
    score = _fmt(row.get("composite_score"))
    if score:
        parts.append(f"综合分 {score}")
    symbols = row.get("symbol_count")
    if symbols not in (None, ""):
        parts.append(f"覆盖标的 {int(symbols)}")
    return "｜".join(parts)


def make_arena_tools(arena: ModelArenaPort) -> tuple[ToolSpec, ...]:
    def model_arena_leaderboard(
        *, limit: int = 5, days: int = DEFAULT_DAYS, scope: str = DEFAULT_SCOPE,
        as_of: str = "",
    ) -> ToolResult:
        # 两个都不能写 `x or default`：limit=0 / days=0 会被 falsy 换成默认值
        requested_limit = 5 if limit is None else int(limit)
        capped = max(1, min(requested_limit, MAX_EVIDENCE))
        requested_days = DEFAULT_DAYS if days is None else int(days)
        window_days = max(MIN_DAYS, min(requested_days, MAX_DAYS))
        # 口径只接受平台支持的取值；非法值收敛到默认值（而不是抛给模型），
        # 因为实际口径会写进证据——回答层不会误以为拿的是别的口径
        resolved_scope = str(scope or "").strip().lower()
        if resolved_scope not in SCOPES:
            resolved_scope = DEFAULT_SCOPE
        try:
            payload = arena.leaderboard(limit=capped, days=window_days, scope=resolved_scope)
        except Exception as exc:  # noqa: BLE001 — 取不到就如实声明，不抛给模型
            return ToolResult(
                data={"leaderboard": [], "error": type(exc).__name__},
                evidence=(),
                coverage=Coverage(available=(), unavailable=(_SOURCE_KEY,)),
                degraded=True,
                degraded_reason=f"模型评测榜读取失败（{type(exc).__name__}）",
                as_of=as_of,
                disclaimer=RESEARCH_DISCLAIMER,
            )

        if not isinstance(payload, Mapping):
            return ToolResult(
                data={"leaderboard": [], "error": "unparsable_payload"},
                evidence=(),
                coverage=Coverage(available=(), unavailable=(_SOURCE_KEY,)),
                degraded=True,
                degraded_reason="模型评测榜结构无法解析（不是映射）",
                as_of=as_of,
                disclaimer=RESEARCH_DISCLAIMER,
            )

        rows = payload.get("leaderboard")
        if not isinstance(rows, (list, tuple)):
            return ToolResult(
                data={"leaderboard": [], "error": "missing_leaderboard"},
                evidence=(),
                coverage=Coverage(available=(), unavailable=(_SOURCE_KEY,)),
                degraded=True,
                degraded_reason="模型评测榜缺少 leaderboard 列表，结构无法解析",
                as_of=as_of,
                disclaimer=RESEARCH_DISCLAIMER,
            )

        if not rows:
            # 窗口内没有可比样本是**事实**，但它是未覆盖：不能编一个排名出来
            return ToolResult(
                data={"leaderboard": [], "count": 0, "total_samples": payload.get("total_samples")},
                evidence=(),
                coverage=Coverage(
                    available=(_SOURCE_KEY,),
                    unavailable=("model_arena（该窗口内没有可比较的模型样本）",),
                ),
                as_of=as_of,
                disclaimer=RESEARCH_DISCLAIMER,
            )

        evidence: list[EvidenceItem] = []
        for index, row in enumerate(
            [r for r in rows if isinstance(r, Mapping)][:capped], start=1
        ):
            evidence.append(
                EvidenceItem(
                    id=f"E{index}",
                    field=f"模型{index}",
                    value=_model_text(row, index),
                    unit="",
                    source_key=_SOURCE_KEY,
                    as_of=str(payload.get("generated_at") or as_of),
                )
            )
        # 窗口与口径必须可引用：不说窗口的准确率无法复核
        window_start = str(payload.get("window_start") or "").strip()
        window_end = str(payload.get("window_end") or "").strip()
        total = payload.get("total_samples")
        meta_parts = [
            part
            for part in (
                f"{window_start} 至 {window_end}" if window_start or window_end else "",
                f"口径 {payload.get('scope') or resolved_scope}",
                f"总样本 {int(total)}" if isinstance(total, (int, float)) else "",
                f"生成于 {payload.get('generated_at')}" if payload.get("generated_at") else "",
            )
            if part
        ]
        if meta_parts:
            evidence.append(
                EvidenceItem(
                    id=f"E{len(evidence) + 1}",
                    field="评测窗口与口径",
                    value="｜".join(meta_parts),
                    unit="",
                    source_key=_SOURCE_KEY,
                    as_of=as_of,
                )
            )

        return ToolResult(
            data={
                "leaderboard": [dict(row) for row in rows[:capped]],
                "count": len(rows[:capped]),
                "window_start": payload.get("window_start"),
                "window_end": payload.get("window_end"),
                "scope": payload.get("scope"),
                "total_samples": payload.get("total_samples"),
            },
            evidence=tuple(evidence),
            coverage=Coverage(available=(_SOURCE_KEY,), unavailable=()),
            as_of=as_of,
            disclaimer=RESEARCH_DISCLAIMER,
        )

    spec = ToolSpec(
        name="model_arena_leaderboard",
        title="模型对比榜（准确率与兑现）",
        description=(
            "读取**平台的模型评测榜单**：按模型聚合的样本量、准确率、胜率、"
            "平均 T+1 收益、收益波动与综合分，并给出评测窗口与口径。"
            "用于回答「哪个模型在这些标的上更准」「换模型有没有用」。"
            "排名与准确率来自平台落库的评测产出（已剔除日期漂移样本），"
            "**不能由模型自述**；窗口内没有样本时会显式声明。"
            "scope 决定评测口径：all=该用户全部报告（默认，样本多），"
            "portfolio=只看组合快照报告口径（样本少，容易无样本）。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": f"返回前几名，1-{MAX_EVIDENCE}，默认 5"},
                "days": {
                    "type": "integer",
                    "description": f"评测窗口天数，{MIN_DAYS}-{MAX_DAYS}，默认 {DEFAULT_DAYS}",
                },
                "scope": {
                    "type": "string",
                    "enum": list(SCOPES),
                    "description": f"评测口径，默认 {DEFAULT_SCOPE}",
                },
                "as_of": {"type": "string", "description": "基准日 YYYY-MM-DD"},
            },
            "required": [],
        },
        output_contract=MODEL_ARENA_CONTRACT,
        handler=model_arena_leaderboard,
        cost="cheap",
        plan_layer=PLAN_LAYER_DECISION,
        latency_budget_ms=10000,
        freshness_sources=(_SOURCE_KEY,),
    )
    return (spec,)
