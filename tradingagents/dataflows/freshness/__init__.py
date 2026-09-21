"""Data freshness contract: validate, aggregate, and disclose source freshness."""

from .access import (
    build_degraded_freshness_pool,
    inject_freshness_into_state,
    resolve_freshness_pool,
    section_freshness_tooltip,
)
from .aggregator import build_freshness_pool, build_freshness_summary, clamp_confidence
from .integrate import attach_freshness_to_result
from .prompt import format_freshness_context_for_sources, format_freshness_context_summary

__all__ = [
    "attach_freshness_to_result",
    "build_degraded_freshness_pool",
    "build_freshness_pool",
    "build_freshness_summary",
    "clamp_confidence",
    "format_freshness_context_for_sources",
    "format_freshness_context_summary",
    "inject_freshness_into_state",
    "resolve_freshness_pool",
    "section_freshness_tooltip",
]
