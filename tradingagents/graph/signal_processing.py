# TradingAgents/graph/signal_processing.py
"""Deterministic extraction of the final trading decision.

This module used to combine a loose keyword scan with an LLM fallback:

* the keyword scan tested **buy keywords before sell keywords**, so a paragraph
  containing both resolved to BUY;
* if the scan found nothing it scanned the **entire document**, then
* if that still found nothing it called an LLM with the prompt "read the report
  and output only one token: BUY, SELL, or HOLD" — a second, independent
  inference over free prose.

Measured effect: 48.6% of identical (symbol, day, window, mode) inputs produced
different directions and 20.5% produced two directions at once, at
``llm_temperature=0.0``; the bias was strongly long (74% of shipped signals).

Both the trader prompt and the risk-judge prompt mandate a machine-readable
direction (``<!-- VERDICT: ... -->`` and a final ``最终交易建议：`` line). A document
without one is a prompt-compliance failure, not an invitation to guess. It now
yields ``None`` — explicit abstention — which surfaces in the abstention-rate
metric instead of being silently converted into an opinion.

All parsing lives in :mod:`tradingagents.agents.utils.direction`.
"""

from __future__ import annotations

from typing import Optional

from tradingagents.agents.utils.direction import (
    extract_direction_result,
    to_trading_decision,
)


class SignalProcessor:
    """Extracts the actionable decision from a completed analysis document."""

    def process_signal(self, full_signal: str) -> Optional[str]:
        """Extract the decision from a full trading signal.

        Args:
            full_signal: Complete trading signal text.

        Returns:
            ``"BUY"``, ``"SELL"`` or ``"HOLD"``, or ``None`` when the document
            does not state an unambiguous direction. ``None`` means abstention:
            callers must decide how to record it, and must not substitute an
            opinion of their own.
        """
        return extract_trading_decision(full_signal)


def extract_trading_decision(text: Optional[str]) -> Optional[str]:
    """Map a document to ``BUY``/``SELL``/``HOLD``, or ``None`` to abstain.

    A ``VERDICT`` block takes precedence; otherwise a single explicitly labelled
    decision line is read. Contradictory evidence abstains rather than being
    resolved by keyword order.
    """
    if not text:
        return None

    result = extract_direction_result(text)
    if result.conflict:
        # The document argues both ways. Breaking that tie by keyword order is
        # exactly the failure this module removes, so abstain instead.
        return None
    return to_trading_decision(result.direction)
