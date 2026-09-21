"""全市场筛选工具：把既有扫描能力接进对话工具面。

## 为什么是「接入」而不是「新写」

`api/services/market_scanner_service.py` 已是一台成熟的全市场扫描器（六因子模型
+ 8 个评分档案，其中 `breakout` 档的定位就是「量比 + 逼近日高双重确认」）。
本模块**不重写因子**，只做两件事：

1. 把端口（`MarketScreener`）注入进来，使工具层不必 import 应用层
   （分层方向 `api → agent → tools`，由 AST 边界测试强制）。
2. 把扫描结果转成**可引用的证据**，让「候选来自扫描器」成为可校验的事实：
   零 LLM 校验器逐条检查回答里的引用与数字，模型无法编造候选或得分。

## 诚实性：缺失因子不得写成 0

实测（sina 源）`volume_ratio` 与 `turnover_rate` 返回 `null`，于是 `breakout` 档
名义上的「量比确认」实际贡献 `0.0`。用户要「放量票」时，如果把缺失当作 0 或
干脆不提，就等于把「按突破档排序」讲成「已确认放量」——这是**编造**，只是
编造发生在措辞层。

因此：缺失字段一律进 `Coverage.unavailable`（`volume_ratio（量比）`），
由既有的「未覆盖维度必须逐项声明」机制强制回答说出来。
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Protocol

from tradingagents.grounding.types import Coverage, EvidenceItem

from .spec import PLAN_LAYER_DECISION, RESEARCH_DISCLAIMER, ToolResult, ToolSpec

SCREEN_CONTRACT = "MARKET_SCREEN"

DEFAULT_TOP_K = 10
MAX_TOP_K = 20

# 缺失字段 → 对外可读名。用「键（中文名）」而不是只写键：
# 回答里要逐项声明，用户读到的是中文维度名。
_MISSING_FIELDS: tuple[tuple[str, str], ...] = (
    ("volume_ratio", "volume_ratio（量比）"),
    ("turnover_rate", "turnover_rate（换手率）"),
)

# 「放量」相关的字段：这些缺失直接影响用户措辞能否成立，单独强调
_VOLUME_FIELDS = {"volume_ratio"}


class MarketScreener(Protocol):
    """全市场扫描端口。实现由应用层提供（与 `signal_ports.py` 同一套做法）。"""

    def screen(
        self,
        *,
        top_k: int,
        profile: str | None = None,
        min_change_pct: float = 0.0,
        scan_limit: int | None = None,
    ) -> Mapping[str, Any]: ...


def _fmt(value: Any) -> str:
    """数值尽量保留 2 位小数（校验器按被引用证据比对数字）。"""
    if value is None:
        return "未提供"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return f"{float(value):.2f}"
    return str(value)


def _candidate_value(item: Mapping[str, Any]) -> str:
    """一只候选的证据正文：代码、名称、得分、入选理由、涨幅。

    理由（`reasons`）来自扫描器原生输出，因此「为什么入选」也是可校验的，
    而不是让模型自由发挥。
    """
    reasons = item.get("reasons") or []
    if isinstance(reasons, str):
        reasons = [reasons]
    parts = [
        f"{item.get('symbol')} {item.get('name')}",
        f"得分 {_fmt(item.get('score'))}",
        f"涨幅 {_fmt(item.get('price_change_pct'))}%",
    ]
    if reasons:
        parts.append("入选理由：" + "；".join(str(r) for r in reasons))
    # 量比/换手是「放量」这类措辞的直接依据，有值时必须可引用：
    # 拿不到时 `_missing_across` 会把它列进未覆盖，两条路径都不留含糊。
    for key, label in (("volume_ratio", "量比"), ("turnover_rate", "换手率")):
        value = item.get(key)
        if value not in (None, ""):
            parts.append(f"{label} {_fmt(value)}")
    flags = item.get("risk_flags") or []
    if flags:
        parts.append("风险标记：" + "、".join(str(f) for f in flags))
    hits = item.get("strategy_hits") or []
    if hits:
        parts.append("命中策略：" + "、".join(str(h) for h in hits))
    return "｜".join(parts)


def _missing_across(items: list[Mapping[str, Any]]) -> tuple[str, ...]:
    """所有候选都缺的字段才算缺失（部分缺失会让维度名有歧义）。"""
    missing: list[str] = []
    for key, label in _MISSING_FIELDS:
        if all(item.get(key) in (None, "") for item in items):
            missing.append(label)
    return tuple(missing)


def make_screen_tools(screener: MarketScreener) -> tuple[ToolSpec, ...]:
    """构造筛选工具。`screener` 由应用层注入。"""

    def market_screen(
        *,
        top_k: int = DEFAULT_TOP_K,
        profile: str | None = None,
        min_change_pct: float = 0.0,
        as_of: str = "",
        scan_limit: int | None = None,
    ) -> ToolResult:
        try:
            raw = screener.screen(
                top_k=top_k,
                profile=profile,
                min_change_pct=min_change_pct,
                scan_limit=scan_limit,
            )
        except Exception as exc:  # noqa: BLE001
            # 结构化降级而不是抛错：抛错后循环会记一条工具失败并继续，模型将
            # 面对**空证据**去回答一个筛选问题——那正是编造的温床。
            return ToolResult(
                data={
                    "items": [],
                    "error": type(exc).__name__,
                    "profile": profile,
                    "top_k": top_k,
                },
                evidence=(),
                coverage=Coverage(available=(), unavailable=("market_screen",)),
                degraded=True,
                degraded_reason=f"全市场扫描失败（{type(exc).__name__}）",
                as_of=as_of,
                disclaimer=RESEARCH_DISCLAIMER,
            )

        payload = dict(raw or {})
        items = [
            item for item in (payload.get("items") or []) if isinstance(item, Mapping)
        ][: max(1, min(int(top_k), MAX_TOP_K))]

        if not items:
            # 空结果**不产出任何候选证据**：没有证据，模型就没有可引用的标的。
            return ToolResult(
                data={
                    "items": [],
                    "pool_size": payload.get("pool_size"),
                    "scored_size": payload.get("scored_size"),
                    "profile": profile,
                    "top_k": top_k,
                },
                evidence=(),
                coverage=Coverage(
                    available=("market_screen",), unavailable=("market_screen：未取得候选",)
                ),
                as_of=as_of,
                disclaimer=RESEARCH_DISCLAIMER,
            )

        evidence: list[EvidenceItem] = [
            EvidenceItem(
                id=f"E{i}",
                field=f"候选{i}",
                value=_candidate_value(item),
                unit="",
                source_key="realtime_quotes",
                as_of=as_of,
            )
            for i, item in enumerate(items, start=1)
        ]

        scope = (
            f"扫描全市场 {payload.get('pool_size')} 只，评分 {payload.get('scored_size')} 只，"
            f"取前 {len(items)} 只；档案 {profile or '默认'}，"
            f"最低涨幅 {_fmt(min_change_pct)}%"
        )
        first = items[0]
        timing = (
            f"数据时点 {first.get('quote_time') or '未提供'}，"
            f"来源 {first.get('quote_source') or '未提供'}，基准日 {as_of or '未提供'}"
        )
        scope_id = f"E{len(items) + 1}"
        timing_id = f"E{len(items) + 2}"
        evidence.append(
            EvidenceItem(
                id=scope_id, field="扫描范围与条件", value=scope, unit="",
                source_key="realtime_quotes", as_of=as_of,
            )
        )
        evidence.append(
            EvidenceItem(
                id=timing_id, field="数据时点与来源", value=timing, unit="",
                source_key="realtime_quotes", as_of=as_of,
            )
        )

        missing = _missing_across(items)
        unavailable = list(missing)
        # 渲染层要的是短名（「量比」而不是「volume_ratio（量比）」）
        missing_short = [
            label.split("（")[-1].rstrip("）") if "（" in label else label
            for label in missing
        ]
        # 用户要「放量」而这个字段拿不到时，措辞会直接失真，单独说得更明确
        if any(m.startswith("volume_ratio") for m in missing):
            # 短名：校验器按**逐字相等**比对 `declared_unavailable`，长句会让
            # 模型几乎不可能复述正确——那会把诚实声明变成必然失败。
            unavailable.append("放量确认（量比缺失）")

        return ToolResult(
            data={
                "items": [dict(item) for item in items],
                "pool_size": payload.get("pool_size"),
                "scored_size": payload.get("scored_size"),
                "profile": profile,
                "top_k": top_k,
                "missing_fields": list(missing),
                "missing_fields_short": missing_short,
                "scope_ref": scope_id,
                "timing_ref": timing_id,
            },
            evidence=tuple(evidence),
            coverage=Coverage(
                available=("market_screen", "realtime_quotes"), unavailable=tuple(unavailable)
            ),
            as_of=as_of,
            disclaimer=RESEARCH_DISCLAIMER,
        )

    spec = ToolSpec(
        name="market_screen",
        title="全市场筛选",
        description=(
            "按评分档案从全市场 A 股中筛选候选（六因子：动量/活跃度/逼近日高/行业资金/"
            "量比/振幅），返回候选代码、名称、得分与入选理由。用于回答"
            "「今天有哪些放量票」「帮我找十只强势股」这类**无标的**的筛选问题。"
            "候选与理由均来自扫描器，不接受模型改写；数据源未提供的因子会显式列为未覆盖。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "top_k": {
                    "type": "integer",
                    "description": f"候选数量，1-{MAX_TOP_K}，默认 {DEFAULT_TOP_K}",
                },
                "profile": {
                    "type": "string",
                    "description": "评分档案：breakout（放量突破）/ashare_aggressive（进攻）/reversal（回调低吸）/sector_rotation（行业轮动）等，留空用默认档",
                },
                "min_change_pct": {
                    "type": "number",
                    "description": "最低当日涨跌幅过滤（%）",
                },
                "as_of": {"type": "string", "description": "基准日 YYYY-MM-DD"},
            },
            "required": [],
        },
        output_contract=SCREEN_CONTRACT,
        plan_layer=PLAN_LAYER_DECISION,
        handler=market_screen,
        cost="medium",
        # 实测：全市场 5020 只冷启动 61.5s、热启动 10.0s。预算必须覆盖冷启动，
        # 否则一次正常的全市场扫描会被自己的超时保护判成失败。
        latency_budget_ms=120_000,
        freshness_sources=("realtime_quotes",),
    )
    return (spec,)
