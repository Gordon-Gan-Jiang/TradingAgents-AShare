"""Second-pass decision critic (Reflection-style) after Risk Judge — consistency, no new data.

The critic is **advisory**. It reviews the risk judge's final decision and records
its findings, but it never rewrites the decision. Previously, when it set
``revision_required`` with severity above the threshold, it replaced
``final_trade_decision`` wholesale with a freshly generated draft. That had two
consequences the audit objected to:

* the stored direction could be authored by the critic rather than by the risk
  judge, and nothing in the report recorded the substitution, so the measured
  signal was not the signal the pipeline claims to produce;
* it added a second full-length generation per analysis whose only effect on a
  quantity with no measured predictive content was extra variance and cost.
"""

from __future__ import annotations

import json
from typing import Any

from tradingagents.dataflows.config import get_config
from tradingagents.prompts import get_prompt
from tradingagents.agents.utils.analyst_structured import format_traces_for_critic
from tradingagents.agents.utils.critic_config import (
    decision_critic_revision_threshold,
    is_decision_critic_enabled,
)
from tradingagents.agents.utils.direction import extract_tagged_json


def _parse_critic(text: str) -> dict[str, Any]:
    """Parse the CRITIC_RESULT block, defaulting to a clean review.

    Uses the shared tag extractor, so a nested object in the payload no longer
    truncates the JSON the way the old non-greedy ``\\{.*?\\}`` pattern did.
    """
    payload = extract_tagged_json(text or "", "CRITIC_RESULT")
    if not isinstance(payload, dict):
        return {"severity": 0, "issues": [], "revision_required": False}
    return payload


def create_decision_critic(llm):
    async def decision_critic_node(state) -> dict[str, Any]:
        if not is_decision_critic_enabled():
            return {}

        config = get_config()
        ic = json.dumps(state.get("instrument_context") or {}, ensure_ascii=False)[:8000]
        mc = json.dumps(state.get("market_context") or {}, ensure_ascii=False)[:8000]
        traces_txt = format_traces_for_critic(list(state.get("analyst_traces") or []))
        trader_plan = str(state.get("trader_investment_plan") or "")[:6000]
        final_td = str(state.get("final_trade_decision") or "")

        prompt = get_prompt("decision_critic_prompt", config=config).format(
            instrument_context_json=ic,
            market_context_json=mc,
            analyst_traces_summary=traces_txt,
            trader_plan_excerpt=trader_plan,
            final_trade_decision=final_td[:24000],
        )
        review_text = ""
        async for chunk in llm.astream(prompt):
            content = chunk.content if hasattr(chunk, "content") else str(chunk)
            review_text += content

        parsed = _parse_critic(review_text)
        meta = dict(state.get("metadata") or {})
        severity = float(parsed.get("severity") or 0)
        revision_recommended = bool(parsed.get("revision_required")) and severity >= (
            decision_critic_revision_threshold(config)
        )

        meta["decision_critic"] = {
            "result": parsed,
            "raw_review": review_text[:12000],
            # Advisory only. Recording the recommendation keeps the diagnostic
            # signal (and makes the critic's own precision measurable later)
            # without letting it author the decision it is reviewing.
            "revision_recommended": revision_recommended,
            "revision_applied": False,
        }
        return {"metadata": meta}

    return decision_critic_node
