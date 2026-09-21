"""Post-processing for multi-agent reports: trace horizons, verdict reconciliation, methodology provenance."""

from __future__ import annotations

from typing import Any

from tradingagents.agents.utils.direction import (
    extract_direction_result,
    find_tagged_json_span,
    replace_tagged_json,
)
from tradingagents.methodology.loader import get_methodology_snapshot

from . import consensus_service


def merge_dual_horizon_traces(
    short_traces: list[dict[str, Any]] | None,
    medium_traces: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Concatenate short/medium analyst traces with explicit horizon labels."""
    out: list[dict[str, Any]] = []
    for t in short_traces or []:
        if isinstance(t, dict):
            row = dict(t)
            row["horizon"] = "short"
            out.append(row)
    for t in medium_traces or []:
        if isinstance(t, dict):
            row = dict(t)
            row["horizon"] = "medium"
            out.append(row)
    return out


def _append_quality_flag(flags: list[str], name: str) -> None:
    if name not in flags:
        flags.append(name)


def _write_verdict(text: str, direction: str, confidence: int, reason: str) -> str:
    """Replace, or append, the VERDICT block.

    Delegates to the canonical tag surgery in
    :mod:`tradingagents.agents.utils.direction` so the block format is identical to
    every other writer, and so an existing malformed block is corrected in place
    rather than being left next to a second, valid one.
    """
    payload = {
        "direction": direction,
        "reason": reason,
        "confidence": int(max(0, min(100, confidence))),
    }
    updated, _replaced = replace_tagged_json(text, "VERDICT", payload)
    return updated


def _tag_consensus_opposes_extracted(
    consensus_direction: str,
    extracted_direction: str,
    *,
    has_verdict_tag: bool,
) -> bool:
    if not has_verdict_tag:
        return False
    cs = consensus_service._direction_score(consensus_direction)
    ts = consensus_service._direction_score(
        consensus_service._normalize_direction(extracted_direction) or extracted_direction
    )
    return ts * cs < 0 and abs(cs) >= 0.2 and abs(ts) >= 0.2


def reconcile_verdict_with_consensus(result: dict[str, Any]) -> None:
    """Align the final_trade_decision VERDICT block with deterministic analyst consensus.

    Three cases are handled, and each one is recorded in ``quality_flags`` so a
    stored direction can always be traced back to its origin:

    ``verdict_synthesized_from_consensus``
        the document carried no VERDICT block;
    ``verdict_direction_invalid``
        a block existed but stated no recognisable direction (fail-closed), so it
        is treated as missing rather than being silently read as neutral;
    ``verdict_realigned_with_consensus``
        a block existed but contradicted the weighted analyst consensus.

    Consensus is used as a replacement only because the alternatives are worse:
    leaving the block absent makes the stored direction null, and leaving a
    self-contradictory block makes the stored direction disagree with the very
    analyst traces displayed beside it. Because every substitution is flagged, the
    measurement layer can separate judge-stated directions from consensus-derived
    ones instead of treating them as the same signal.
    """
    traces = list(result.get("analyst_traces") or [])
    summary = consensus_service.build_consensus_summary(result_data={"analyst_traces": traces})
    consensus_dir = summary["consensus_direction"] if summary else "中性"
    consensus_conf = int(summary.get("consensus_strength", 55)) if summary else 55

    flags = list(result.get("quality_flags") or [])
    text = str(result.get("final_trade_decision") or "")

    has_tag = find_tagged_json_span(text, "VERDICT") is not None
    extracted_dir = extract_direction_result(
        text, allow_labelled_lines=False
    ).direction

    reason_consensus = "基于分析师轨迹加权共识"

    if not has_tag:
        result["final_trade_decision"] = _write_verdict(
            text, consensus_dir, consensus_conf, reason_consensus
        )
        _append_quality_flag(flags, "verdict_synthesized_from_consensus")
    elif extracted_dir is None:
        result["final_trade_decision"] = _write_verdict(
            text, consensus_dir, consensus_conf, reason_consensus
        )
        _append_quality_flag(flags, "verdict_direction_invalid")
    elif _tag_consensus_opposes_extracted(
        consensus_dir, extracted_dir, has_verdict_tag=True
    ):
        result["final_trade_decision"] = _write_verdict(
            text,
            consensus_dir,
            consensus_conf,
            reason_consensus + "（已按共识校准原文不一致的裁决标签）",
        )
        _append_quality_flag(flags, "verdict_realigned_with_consensus")

    result["quality_flags"] = flags


def attach_methodology_snapshot(result: dict[str, Any], config: dict[str, Any] | None) -> None:
    md = result.setdefault("metadata", {})
    if not isinstance(md, dict):
        return
    md["methodology_snapshot"] = get_methodology_snapshot(config)


def apply_result_quality_pass(
    result: dict[str, Any],
    *,
    config: dict[str, Any] | None = None,
    default_trace_horizon: str = "short",
) -> None:
    hz = str(default_trace_horizon or "short").strip().lower() or "short"
    for raw in result.get("analyst_traces") or []:
        if isinstance(raw, dict) and not str(raw.get("horizon") or "").strip():
            raw["horizon"] = hz
    reconcile_verdict_with_consensus(result)
    attach_methodology_snapshot(result, config)
