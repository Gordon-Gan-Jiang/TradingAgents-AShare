"""黄金集 harness：加载、探测当前实现、按指标打分。

## 为什么需要它

Phase 0 的全部意义是**先有度量，再改行为**。没有基线就无法回答
"改了之后是不是更好了"——那正是上一轮讨论里最容易糊弄过去的地方。

本模块刻意把三件事分开：

1. `load_golden()` —— 读数据集（期望行为，不含实现细节）
2. `probe(text)` —— **探测当前实现**会怎么处理这句话（不执行真实工具、不联网）
3. `score(items, probes)` —— 纯函数打分

分开的好处：数据集与实现解耦，任何一版实现都能拿同一套尺子量；
`probe` 只做"规划"这一层的观测（意图/标的/计划工具/是否追问），
因此**不需要行情源**，在离线环境也能跑分——否则基线报告永远出不来。

## 指标口径

| 指标 | 定义 | 为什么这么定 |
|---|---|---|
| `coverage` | 产出可服务回合（而非 400 追问）的比例 | 用户能感知的第一道门槛 |
| `planning_accuracy` | 意图 **且** 标的集合都对的占比 | 规划层的核心质量 |
| `tool_recall` | 必需工具被计划到的比例 | 漏工具 = 答案缺依据 |
| `tool_precision` | 计划到的工具里属于必需/允许的比例 | 乱调工具 = 成本与噪声 |
| `clarify_accuracy` | 该追问的追问了、不该追问的没追问 | 追问过度会让人烦，不足会答错标的 |
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

GOLDEN_PATH = Path(__file__).resolve().parent / "questions.jsonl"

INTENTS: tuple[str, ...] = (
    "single_symbol_judgement",
    "factual_lookup",
    "portfolio_review",
    "market_screen",
    "multi_symbol_compare",
    "market_view",
    "history_lookup",
    "concept_qa",
    "action_request",
)

# 不需要标的的问句类别：它们没有标的**是正常的**，不该因此被追问
_SYMBOL_OPTIONAL_INTENTS = {
    "market_screen",
    "market_view",
    "portfolio_review",
    "history_lookup",
    "concept_qa",
}


@dataclass(frozen=True)
class GoldenItem:
    id: str
    text: str
    intent: str
    symbols: tuple[str, ...] = ()
    required_tools: tuple[str, ...] = ()
    optional_tools: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    must_declare: tuple[str, ...] = ()
    expect_clarify: bool | None = None
    note: str = ""


@dataclass(frozen=True)
class Probe:
    """当前实现对一句话的规划结果（观测值）。"""

    servable: bool               # 是否构造出了回合（False = 400 追问）
    intent: str | None = None    # 观测到的意图（由档位/筛选/多标的等推导，未必精确）
    symbols: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    clarifying: bool = False
    reason: str = ""


@dataclass
class Metrics:
    total: int = 0
    coverage: float = 0.0
    planning_accuracy: float = 0.0
    tool_recall: float = 0.0
    tool_precision: float = 0.0
    clarify_accuracy: float = 0.0
    failures: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "coverage": round(self.coverage, 4),
            "planning_accuracy": round(self.planning_accuracy, 4),
            "tool_recall": round(self.tool_recall, 4),
            "tool_precision": round(self.tool_precision, 4),
            "clarify_accuracy": round(self.clarify_accuracy, 4),
            "failures": self.failures,
        }


def load_golden(path: Path | None = None) -> tuple[GoldenItem, ...]:
    """读黄金集。字段缺失按空值处理，多余字段忽略。"""
    target = Path(path) if path is not None else GOLDEN_PATH
    items: list[GoldenItem] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        raw = json.loads(line)
        items.append(
            GoldenItem(
                id=str(raw["id"]),
                text=str(raw["text"]),
                intent=str(raw["intent"]),
                symbols=tuple(raw.get("symbols") or ()),
                required_tools=tuple(raw.get("required_tools") or ()),
                optional_tools=tuple(raw.get("optional_tools") or ()),
                required_capabilities=tuple(raw.get("required_capabilities") or ()),
                must_declare=tuple(raw.get("must_declare") or ()),
                expect_clarify=raw.get("expect_clarify"),
                note=str(raw.get("note") or ""),
            )
        )
    return tuple(items)


def _observed_intent(*, screen: bool, symbols: Sequence[str], symbol: str | None,
                     candidates: Sequence[str], planned: Sequence[str]) -> str | None:
    """从当前实现的可观测产物**反推**意图。

    反推而不直接读"意图"字段，是因为今天的实现根本没有意图概念（只有档位与
    工具计划）。反推口径要写死在这里，否则基线会随实现漂移。
    """
    if screen:
        return "market_screen"
    if len(symbols) >= 2:
        return "multi_symbol_compare"
    if symbol or candidates:
        if "signal_washout_probe" in planned:
            return "single_symbol_judgement"
        return "factual_lookup"
    return None


_NAME_MAP_WARMED = False
_REGISTRY: Any = None


def warm_registry() -> Any:
    """装配**完整**工具面的注册表并缓存。

    计划参数必须按工具 schema 生成，所以规划路径需要真实注册表；用哑依赖装配
    是为了把"工具存在"与"依赖可用"分开——跑分测的是规划，不该被网络卡住。
    """
    global _REGISTRY
    if _REGISTRY is None:
        from tradingagents.tools import build_registry

        _REGISTRY = build_registry(
            ports_factory=lambda *a: object(),
            news_fetcher=lambda *a: [],
            market_screener=object(),
        )
    return _REGISTRY


_NAME_MAP_WARMED = False


def warm_name_map() -> int:
    """把磁盘上的名称表装载进内存（与服务进程启动时同一条路径）。

    **这是测量有效性的前提**：名称表是分批后台加载的，冷进程里所有"用公司名
    提问"的用例都会失败。若不预热，基线分数反映的是"缓存冷不冷"而不是实现好坏
    ——第一次跑分就踩到了这个坑（覆盖率 46%，其中相当一部分是名称表没加载）。
    读磁盘是本地操作、不触网，因此跑分可以离线且可重复。
    """
    global _NAME_MAP_WARMED
    import api.main as main

    if _NAME_MAP_WARMED:
        return len(main._get_stock_name_map_cached_only() or {})
    try:
        stocks, funds, loaded_at = main._read_stock_map_from_disk()
        if stocks or funds:
            main._publish_stock_maps(stocks, funds, loaded_at)
    except Exception:  # noqa: BLE001 — 取不到就照常跑，但调用方应知道分数不可比
        pass
    _NAME_MAP_WARMED = True
    return len(main._get_stock_name_map_cached_only() or {})


def probe(text: str, *, registry_names: Iterable[str] | None = None) -> Probe:
    """探测当前实现如何规划这句话。

    **只走规划层**（解析标的 → 构造回合 → 计划工具），不执行工具、不调模型、
    不联网。这样基线报告在离线机器上也能跑——否则它永远只是一句承诺。
    """
    from api.services import chat_service
    from tradingagents.agent.loop import _plan_tools

    from tradingagents.agent.routing import looks_like_market_screen

    warm_name_map()
    registry = warm_registry()
    body = chat_service.ChatRequest(messages=[{"role": "user", "content": text}])
    try:
        request = chat_service.build_turn_request(body, user_id="golden")
    except Exception as exc:  # noqa: BLE001 — 探测不该因实现异常而中断整轮跑分
        return Probe(servable=False, clarifying=True, reason=f"resolve error: {exc}")

    if request is None:
        # 当前实现返回 None → 路由转 400 追问
        return Probe(
            servable=False,
            intent=("market_screen" if looks_like_market_screen(text) else None),
            clarifying=True,
            reason="no servable turn (HTTP 400 clarify)",
        )

    planned = _plan_tools(request, registry=registry)
    # `_plan_tools` 内部已经把因果问句要追加的新闻工具算进去了
    tools = tuple(call.tool for call in planned)
    symbols = tuple(request.symbols) or ((request.symbol,) if request.symbol else ())
    clarifying = bool(request.candidates) and not symbols and not request.screen
    return Probe(
        servable=True,
        intent=_observed_intent(
            screen=request.screen, symbols=request.symbols, symbol=request.symbol,
            candidates=request.candidates, planned=tools,
        ),
        symbols=symbols,
        tools=tools,
        clarifying=clarifying,
    )


def score(items: Sequence[GoldenItem], probes: Mapping[str, Probe] | Sequence[Probe]) -> Metrics:
    """纯函数打分。

    `probes` 可以是 {id: Probe} 或与 items 同序的序列。
    """
    if isinstance(probes, Mapping):
        pairs = [(item, probes.get(item.id)) for item in items]
    else:
        pairs = list(zip(items, probes))

    metrics = Metrics(total=len(pairs))
    if not pairs:
        return metrics

    servable = accurate = 0
    clarify_hits = clarify_total = 0
    recall_parts: list[float] = []
    precision_parts: list[float] = []

    for item, probe_ in pairs:
        if probe_ is None:
            metrics.failures.append({"id": item.id, "why": "no probe"})
            continue

        if probe_.servable:
            servable += 1

        intent_ok = probe_.intent == item.intent
        # 标的集合比对：期望为空的类别不参与（那些问句本来就没有标的）
        symbols_ok = set(probe_.symbols) == set(item.symbols)

        if intent_ok and symbols_ok:
            accurate += 1
        else:
            metrics.failures.append(
                {
                    "id": item.id,
                    "text": item.text,
                    "why": "planning mismatch",
                    "expected_intent": item.intent,
                    "observed_intent": probe_.intent,
                    "expected_symbols": list(item.symbols),
                    "observed_symbols": list(probe_.symbols),
                }
            )

        required = set(item.required_tools)
        allowed = required | set(item.optional_tools)
        observed = set(probe_.tools)
        if required:
            recall_parts.append(len(required & observed) / len(required))
        if observed:
            precision_parts.append(len(observed & allowed) / len(observed))
        missing = required - observed
        if missing:
            metrics.failures.append(
                {
                    "id": item.id,
                    "text": item.text,
                    "why": "missing required tools",
                    "missing": sorted(missing),
                    "observed": sorted(observed),
                }
            )

        if item.expect_clarify is not None:
            clarify_total += 1
            if bool(probe_.clarifying) == bool(item.expect_clarify):
                clarify_hits += 1
            else:
                metrics.failures.append(
                    {
                        "id": item.id,
                        "text": item.text,
                        "why": "clarify expectation mismatch",
                        "expected": item.expect_clarify,
                        "observed": probe_.clarifying,
                    }
                )

    metrics.coverage = servable / len(pairs)
    metrics.planning_accuracy = accurate / len(pairs)
    metrics.tool_recall = (sum(recall_parts) / len(recall_parts)) if recall_parts else 1.0
    metrics.tool_precision = (
        (sum(precision_parts) / len(precision_parts)) if precision_parts else 1.0
    )
    metrics.clarify_accuracy = (clarify_hits / clarify_total) if clarify_total else 1.0
    return metrics


def run(registry_names: Iterable[str] | None = None) -> Metrics:
    """端到端跑一遍当前实现。"""
    items = load_golden()
    probes = {item.id: probe(item.text, registry_names=registry_names) for item in items}
    return score(items, probes)
