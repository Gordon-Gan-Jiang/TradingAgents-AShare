import contextvars
import operator
from typing import Annotated, Any, List, Tuple, Union

from typing_extensions import Optional, TypedDict
from langgraph.graph import MessagesState

from tradingagents.agents.utils.direction import (
    NEUTRAL as _NEUTRAL,
    coerce_confidence,
    extract_direction_result,
    normalize_direction,
)

# ContextVar used to pass the AgentProgressTracker into async graph nodes
# without putting it in the LangGraph state (which would require serialization).
# Set by the API layer before each graph.astream() call; read by analyst nodes.
current_tracker_var: contextvars.ContextVar = contextvars.ContextVar(
    "current_tracker", default=None
)


def _fallback_confidence_from_direction(direction: str) -> int:
    """Integer 0-100 when the model omits confidence (deterministic prior).

    Delegates to the canonical parser so the prior lives in exactly one place.
    """
    canonical = normalize_direction(direction) or _NEUTRAL
    return coerce_confidence(None, canonical)


def _coerce_verdict_confidence(raw: Any, direction: str) -> int:
    """Normalize model-supplied confidence to a 0-100 integer.

    Delegates to the canonical parser. Retained as a module-level name because
    callers and tests reference it.
    """
    canonical = normalize_direction(direction) or _NEUTRAL
    return coerce_confidence(raw, canonical)


def extract_verdict(text: str) -> Tuple[str, int]:
    """Extract the VERDICT block from analyst output.

    Returns ``(direction, confidence)`` where ``direction`` is a **canonical**
    direction (看多/偏多/中性/偏空/看空) and ``confidence`` is an integer 0-100.

    Two intentional behaviour changes versus the previous implementation:

    * The direction is normalized, so an analyst emitting ``"NEUTRAL"`` or
      ``"bullish"`` no longer leaks a non-canonical value into
      ``analyst_traces.verdict`` and therefore into the consensus vote.
    * Parsing delegates to :mod:`tradingagents.agents.utils.direction`, which
      fixes the nested-brace JSON truncation and drops the whole-document
      keyword scan.

    Analysts deliberately keep a neutral fallback: the LLM trace always renders a
    value, and the analyst layer is descriptive input rather than the trading
    decision. The trading-decision path does **not** use this fallback; it goes
    through ``extract_direction_result`` and abstains when evidence is absent.
    """
    direction, confidence, _parsed = extract_verdict_with_flag(text)
    return direction, confidence


def extract_verdict_with_flag(text: str) -> Tuple[str, int, bool]:
    """Same as :func:`extract_verdict`, but also reports whether it *parsed*.

    The third element is the whole point. ``extract_verdict`` must always render
    something for the trace UI, so an unparseable report yields 中性. But
    ``consensus_service`` reads ``analyst_traces[*].verdict`` and feeds it into the
    weighted direction score — so without this flag a **parse failure is
    indistinguishable from a genuine neutral opinion**, and a broken analyst report
    silently votes "中性" with full weight.

    ``False`` means no verdict was parsed: the caller must treat the analyst as
    having said nothing, not as having said 中性.
    """
    result = extract_direction_result(text, allow_labelled_lines=False)
    if result.direction is None:
        return _NEUTRAL, _fallback_confidence_from_direction(_NEUTRAL), False
    confidence = result.confidence
    if confidence is None:
        confidence = _fallback_confidence_from_direction(result.direction)
    return result.direction, confidence, True


class UserIntent(TypedDict, total=False):
    raw_query: str
    ticker: str
    horizons: List[str]
    focus_areas: List[str]
    specific_questions: List[str]
    user_context: "UserContext"


class TraceItem(TypedDict, total=False):
    agent: str
    horizon: str
    data_window: str
    key_finding: str
    verdict: str
    confidence: Union[int, float, str]
    structured: dict[str, Any]


class InstrumentContext(TypedDict):
    symbol: Annotated[str, "Normalized symbol"]
    security_name: Annotated[str, "Display name or fallback symbol"]
    market_country: Annotated[str, "Market country such as CN or US"]
    exchange: Annotated[str, "Exchange code"]
    currency: Annotated[str, "Trading currency"]
    asset_type: Annotated[str, "Asset type"]


class MarketContext(TypedDict):
    trade_date: Annotated[str, "Requested trade date"]
    timezone: Annotated[str, "Market timezone"]
    market_country: Annotated[str, "Market country"]
    exchange: Annotated[str, "Exchange code"]
    market_session: Annotated[str, "Current session for the requested trade date"]
    market_is_open: Annotated[bool, "Whether the market is currently open"]
    analysis_mode: Annotated[str, "Analysis mode such as pre_market, intraday, post_market, t_plus_1"]
    data_as_of: Annotated[str, "Latest date the analysis should treat as confirmed data"]
    session_note: Annotated[str, "Explanation for the current session inference"]


class UserContext(TypedDict, total=False):
    objective: Annotated[str, "User's desired action"]
    risk_profile: Annotated[str, "User's risk profile"]
    investment_horizon: Annotated[str, "User's intended holding horizon"]
    cash_available: Annotated[float, "Available cash"]
    current_position: Annotated[float, "Current position size"]
    current_position_pct: Annotated[float, "Current position percentage"]
    average_cost: Annotated[float, "Average holding cost"]
    max_loss_pct: Annotated[float, "Maximum tolerated loss percentage"]
    constraints: Annotated[list[str], "Hard trading constraints"]
    user_notes: Annotated[str, "Additional user notes"]


class WorkflowContext(TypedDict):
    context_version: Annotated[str, "Workflow context version"]
    request_source: Annotated[str, "Request origin such as api or chat"]
    selected_analysts: Annotated[list[str], "Requested analyst roster"]


class InvestDebateState(TypedDict):
    bull_history: Annotated[str, "Bullish conversation history"]
    bear_history: Annotated[str, "Bearish conversation history"]
    history: Annotated[str, "Conversation history"]
    current_speaker: Annotated[str, "Speaker that spoke last"]
    current_response: Annotated[str, "Latest response"]
    
    # ── Parallel Rebuttal Fields ──────────────────────────────────────
    bull_initial: Annotated[str, "Bull's initial opening statement"]
    bear_initial: Annotated[str, "Bear's initial opening statement"]
    bull_rebuttal: Annotated[str, "Bull's rebuttal to Bear's initial"]
    bear_rebuttal: Annotated[str, "Bear's rebuttal to Bull's initial"]
    # ──────────────────────────────────────────────────────────────────

    judge_decision: Annotated[str, "Final judge decision"]
    count: Annotated[int, "Length of the current conversation"]
    claims: Annotated[list[dict[str, Any]], "Tracked research claims"]
    focus_claim_ids: Annotated[list[str], "Claim ids that must be answered in the next round"]
    open_claim_ids: Annotated[list[str], "Claim ids still open"]
    resolved_claim_ids: Annotated[list[str], "Claim ids considered resolved"]
    unresolved_claim_ids: Annotated[list[str], "Claim ids still materially disputed"]
    round_summary: Annotated[str, "Summary of the latest debate round"]
    round_goal: Annotated[str, "Current round objective"]
    claim_counter: Annotated[int, "Claim counter for unique ids"]


class RiskDebateState(TypedDict):
    aggressive_history: Annotated[str, "Aggressive analyst history"]
    conservative_history: Annotated[str, "Conservative analyst history"]
    neutral_history: Annotated[str, "Neutral analyst history"]
    history: Annotated[str, "Conversation history"]
    latest_speaker: Annotated[str, "Analyst that spoke last"]
    current_aggressive_response: Annotated[str, "Latest response by the aggressive analyst"]
    current_conservative_response: Annotated[str, "Latest response by the conservative analyst"]
    current_neutral_response: Annotated[str, "Latest response by the neutral analyst"]
    judge_decision: Annotated[str, "Judge decision"]
    count: Annotated[int, "Length of the current conversation"]
    claims: Annotated[list[dict[str, Any]], "Tracked risk claims"]
    focus_claim_ids: Annotated[list[str], "Risk claim ids that must be answered next"]
    open_claim_ids: Annotated[list[str], "Risk claim ids still open"]
    resolved_claim_ids: Annotated[list[str], "Risk claim ids considered resolved"]
    unresolved_claim_ids: Annotated[list[str], "Risk claim ids still materially disputed"]
    round_summary: Annotated[str, "Summary of the latest debate round"]
    round_goal: Annotated[str, "Current round objective"]
    claim_counter: Annotated[int, "Claim counter for unique ids"]


class RiskFeedbackState(TypedDict):
    retry_count: Annotated[int, "How many times the trader has been sent back for revision"]
    max_retries: Annotated[int, "Maximum number of allowed revisions"]
    revision_required: Annotated[bool, "Whether the trader must revise the plan"]
    latest_risk_verdict: Annotated[str, "Risk judge verdict such as pass, revise, reject"]
    hard_constraints: Annotated[list[str], "Non-negotiable constraints from the risk judge"]
    soft_constraints: Annotated[list[str], "Advisory constraints from the risk judge"]
    execution_preconditions: Annotated[list[str], "Conditions that must hold before execution"]
    de_risk_triggers: Annotated[list[str], "Triggers that require immediate de-risking"]
    revision_reason: Annotated[str, "Why the plan was sent back"]


class AgentState(MessagesState):
    company_of_interest: Annotated[str, "Company that we are interested in trading"]
    trade_date: Annotated[str, "What date we are trading at"]
    sender: Annotated[str, "Agent that sent this message"]

    instrument_context: Annotated[InstrumentContext, "Normalized instrument context"]
    market_context: Annotated[MarketContext, "Market session and timing context"]
    user_context: Annotated[UserContext, "User-specific holdings and constraints"]
    workflow_context: Annotated[WorkflowContext, "Workflow metadata for the current run"]

    market_report: Annotated[str, "Report from the Market Analyst"]
    sentiment_report: Annotated[str, "Report from the Social Media Analyst"]
    news_report: Annotated[str, "Report from the News Researcher of current world affairs"]
    fundamentals_report: Annotated[str, "Report from the Fundamentals Researcher"]

    investment_debate_state: Annotated[
        InvestDebateState, "Current state of the debate on if to invest or not"
    ]
    investment_plan: Annotated[str, "Plan generated by the Analyst"]
    trader_investment_plan: Annotated[str, "Plan generated by the Trader"]

    risk_debate_state: Annotated[
        RiskDebateState, "Current state of the debate on evaluating risk"
    ]
    risk_feedback_state: Annotated[
        RiskFeedbackState, "Risk-judge feedback used for trader revision"
    ]
    final_trade_decision: Annotated[str, "Final decision made by the Risk Analysts"]

    macro_report: Annotated[str, "Report from the Macro/Sector Analyst"]
    smart_money_report: Annotated[str, "Report from the Smart Money Analyst"]
    volume_price_report: Annotated[str, "Report from the Volume Price Analyst"]
    user_intent: Annotated[Optional[UserIntent], "Parsed user intent from natural language"]
    horizon: Annotated[str, "Current analysis horizon: short or medium"]
    analyst_traces: Annotated[List[TraceItem], operator.add]
    short_term_result: Annotated[Optional[dict], "Final short-term analysis result"]
    medium_term_result: Annotated[Optional[dict], "Final medium-term analysis result"]
    freshness_pool: Annotated[Optional[dict[str, Any]], "Per-source freshness metadata keyed by source_key"]
    metadata: Annotated[dict[str, Any], "Optional runtime metadata"]
