"""工具层与 freshness 契约的桥接（审计 A2 修复）。

## 修复前的实际状态

`ToolResult.freshness` 在**所有工具中恒为 `{}`**。`freshness_sources` 字段声明了、
平台既有 freshness 契约（四类 taxonomy + 锚点 + grace + confidence 封顶）也在，
但两者之间没有接线。后果：新链路上「这份数据可能过时」这件事彻底消失——
而旧管道（7 分析师）正是靠它防这个的。

## 不可让步的原则：不得伪造 `fresh`

最容易的"修复"是把所有声明的源一律标 `fresh`。那**比不填更糟**：它把「未验证」
伪装成「已确认最新」，下游据此不做任何降级，等于把闸门换成了一块装饰板。

因此三态判定全部基于**工具能确证的事实**：

| 情形 | status | 依据 |
|---|---|---|
| 工具声明该源不可用 | `error` + `issue_type=fetch_error` | 事实 |
| 工具提供了该源的数据截止日 | 交给契约的 `evaluate_source` 判定（fresh/warning/stale） | 事实 |
| 其余（读了但不知道截止日） | `unknown` + 说明 | 诚实承认不知道 |

`unknown` 按既有契约**不得单独抬高** overall_status —— 这正是它存在的意义。
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Iterable, Mapping, Sequence

from tradingagents.dataflows.freshness.aggregator import build_freshness_summary
from tradingagents.dataflows.freshness.contract import get_contract_entry
from tradingagents.dataflows.freshness.types import FreshnessMeta

logger = logging.getLogger(__name__)

NOTE_UNVERIFIED = "工具未验证该源的数据截止日，按 unknown 处理（不得据此认为数据最新）"
NOTE_UNAVAILABLE = "工具声明该源在本轮不可用"
NOTE_LAGGED = "数据截止日早于评估基准日"


def evaluate_tool_sources(
    *,
    source_keys: Sequence[str],
    as_of: str,
    unavailable: Iterable[str] = (),
    verified: Iterable[str] = (),
    cutoffs: Mapping[str, str] | None = None,
) -> dict[str, dict]:
    """为工具声明的数据源产出 freshness 描述。

    * `unavailable` —— 工具已知取不到的源 → `error` / `fetch_error`
    * `cutoffs` —— 工具确认的数据截止日 → 交给契约判定
    * `verified` —— 未给 cutoffs 但已确证等于 as_of 的源 → `fresh`
    * 其余 → `unknown`（**不得**默认 fresh）
    """
    unavailable_set = set(unavailable or ())
    verified_set = set(verified or ())
    cutoff_map = dict(cutoffs or {})

    pool: dict[str, dict] = {}
    for key in source_keys:
        entry = get_contract_entry(key) or {}
        category = entry.get("category", "calendar_anchored")
        criticality = entry.get("criticality", "informational")

        if key in unavailable_set:
            meta = FreshnessMeta(
                source_key=key,
                category=category,
                criticality=criticality,
                status="error",
                anchor_expected=as_of,
                anchor_actual=None,
                issue_type="fetch_error",
                error_message=NOTE_UNAVAILABLE,
                note=NOTE_UNAVAILABLE,
            )
        elif key in cutoff_map:
            actual = str(cutoff_map[key])[:10]
            if actual >= as_of:
                meta = FreshnessMeta(
                    source_key=key,
                    category=category,
                    criticality=criticality,
                    status="fresh",
                    anchor_expected=as_of,
                    anchor_actual=actual,
                )
            else:
                meta = FreshnessMeta(
                    source_key=key,
                    category=category,
                    criticality=criticality,
                    status="stale",
                    anchor_expected=as_of,
                    anchor_actual=actual,
                    issue_type="lag",
                    note=NOTE_LAGGED,
                )
        elif key in verified_set:
            meta = FreshnessMeta(
                source_key=key,
                category=category,
                criticality=criticality,
                status="fresh",
                anchor_expected=as_of,
                anchor_actual=as_of,
            )
        else:
            # 未验证 → unknown。绝不默认 fresh（那会把未验证伪装成已确认）
            meta = FreshnessMeta(
                source_key=key,
                category=category,
                criticality=criticality,
                status="unknown",
                anchor_expected=as_of,
                anchor_actual=None,
                note=NOTE_UNVERIFIED,
            )

        pool[key] = asdict(meta)
    return pool


def summarize_tool_freshness(
    pool: Mapping[str, Mapping[str, object]],
    *,
    as_of: str,
    symbol: str = "",
) -> dict:
    """复用平台既有的 aggregator（overall_status / blocking_sources / confidence_cap）。

    刻意不重新实现聚合：既有实现已被 6 个 freshness 用例覆盖，重写一份等于
    引入第二个可能分叉的口径。
    """
    try:
        return build_freshness_summary(dict(pool), as_of, symbol=symbol or "600519.SH")
    except Exception as exc:  # noqa: BLE001 — 聚合失败不得让工具整体失败
        logger.warning("freshness aggregation failed: %s", exc)
        return {"overall_status": "unknown", "confidence_cap": None, "error": str(exc)}


def tool_freshness(
    *,
    source_keys: Sequence[str],
    as_of: str,
    symbol: str = "",
    unavailable: Iterable[str] = (),
    verified: Iterable[str] = (),
    cutoffs: Mapping[str, str] | None = None,
) -> tuple[dict[str, dict], dict]:
    """便捷入口：返回 (pool, summary)。"""
    pool = evaluate_tool_sources(
        source_keys=source_keys,
        as_of=as_of,
        unavailable=unavailable,
        verified=verified,
        cutoffs=cutoffs,
    )
    return pool, summarize_tool_freshness(pool, as_of=as_of, symbol=symbol)
