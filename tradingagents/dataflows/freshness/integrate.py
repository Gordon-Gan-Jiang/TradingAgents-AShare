from __future__ import annotations

from typing import Any

from .access import build_degraded_freshness_pool
from .aggregator import build_freshness_summary, clamp_confidence


def attach_freshness_to_result(
    result: dict[str, Any],
    freshness_pool: dict[str, dict[str, Any]] | None,
    trade_date: str,
    symbol: str,
) -> dict[str, Any]:
    """Build freshness_summary, clamp confidence, and attach to analysis result dict."""
    if not freshness_pool:
        freshness_pool = build_degraded_freshness_pool(trade_date, symbol=symbol)
    summary = build_freshness_summary(
        freshness_pool,
        trade_date,
        symbol=symbol,
    )
    result["freshness_pool"] = freshness_pool
    result["freshness_summary"] = summary
    result["freshness_status"] = summary["overall_status"]
    if "confidence" in result and result["confidence"] is not None:
        result["confidence"] = clamp_confidence(
            int(result["confidence"]), summary["overall_status"]
        )
    return result
