"""Single source of truth for direction / VERDICT parsing.

Why this module exists
----------------------
Before this module the codebase had six independent direction parsers, all
fail-open:

1. ``graph/signal_processing.py::_extract_decision_keyword``
2. ``agents/utils/agent_states.py::extract_verdict``
3. ``api/services/report_service.py::_extract_verdict``
4. ``api/services/report_quality_service.py::reconcile_verdict_with_consensus``
5. ``api/services/email_report_service.py::_extract_verdict``
6. ``frontend/src/utils/reportText.ts``

Measured consequences of that design (see ``docs/deep-analysis-efficacy-audit.md``):

* ``48.6%`` of identical (symbol, day, window, mode) inputs produced **different**
  directions across runs, and ``20.5%`` emitted **two directions at once**, at
  ``llm_temperature=0.0``.
* The keyword classifier in (1) tested buy keywords *before* sell keywords, so a
  paragraph containing both resolved to BUY. Together with a final fallback that
  scanned the *entire* document, this is one of the sources of the observed
  ``74%`` long bias.
* Ambiguous or absent evidence silently became ``UNKNOWN`` (truthy, therefore
  persisted) or ``"中性"`` (a real opinion the model never expressed).

Design rules
------------
1. **Fail-closed.** Contradictory or absent evidence yields ``None``, meaning
   "不可研判" (cannot judge). Callers must handle ``None`` explicitly. Guessing a
   direction is never allowed: a wrong confident call is worse than an abstention.
2. **Normalized.** Only five canonical directions are ever produced, matching
   ``api/services/consensus_service.py``:
   ``看多 / 偏多 / 中性 / 偏空 / 看空``.
3. **Bounded scope.** Only the structured ``VERDICT`` block and explicitly
   labelled decision lines are read. Free prose is never scanned.

The math behind rule 1: for a signal with information coefficient ``IC``, the
best achievable directional hit rate is ``0.5 + asin(IC)/pi``, so ``IC = 0.10``
caps accuracy at ``53.19%``. Abstaining costs nothing relative to that ceiling;
inventing a direction to avoid ``None`` costs the whole ceiling.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

__all__ = [
    "BULLISH",
    "LEAN_BULLISH",
    "NEUTRAL",
    "LEAN_BEARISH",
    "BEARISH",
    "CANONICAL_DIRECTIONS",
    "TRADING_DECISIONS",
    "ABSTAIN",
    "normalize_direction",
    "classify_direction_phrase",
    "direction_from_score",
    "to_trading_decision",
    "coerce_confidence",
    "parse_stated_confidence",
    "extract_tagged_json",
    "DirectionResult",
    "extract_direction_result",
    "coerce_persistable_decision",
]

# --- canonical directions (must stay in sync with consensus_service) ---------

BULLISH = "看多"
LEAN_BULLISH = "偏多"
NEUTRAL = "中性"
LEAN_BEARISH = "偏空"
BEARISH = "看空"

CANONICAL_DIRECTIONS = (BULLISH, LEAN_BULLISH, NEUTRAL, LEAN_BEARISH, BEARISH)

# --- trading decisions -------------------------------------------------------

BUY = "BUY"
SELL = "SELL"
HOLD = "HOLD"

#: Explicit abstention. Distinct from HOLD: HOLD is an opinion ("no action"),
#: NA is the absence of an opinion ("cannot judge"). Conflating the two is what
#: made the old system appear to always have a view.
ABSTAIN = "NA"

TRADING_DECISIONS = (BUY, SELL, HOLD, ABSTAIN)

_DIRECTION_TO_DECISION = {
    BULLISH: BUY,
    LEAN_BULLISH: BUY,
    NEUTRAL: HOLD,
    LEAN_BEARISH: SELL,
    BEARISH: SELL,
}

#: Values that older code paths wrote as if they were directions. They are not
#: opinions and must never be persisted as one.
NON_DIRECTION_VALUES = frozenset(
    {"UNKNOWN", "DRY_RUN", "NONE", "NULL", "N/A", "NA", "", "-", "TBD", "PENDING"}
)


# --- phrase table ------------------------------------------------------------
#
# Longest-match-first consumption is used when scanning a labelled snippet, so
# the table is pre-sorted by pattern length (descending). The sort is what makes
# "谨慎看多" resolve to LEAN_BULLISH rather than to a conflict between "谨慎"
# (NEUTRAL) and "看多" (BULLISH), and what keeps "strong_buy" from matching the
# inner "buy" separately.

_PHRASE_TABLE: Dict[str, str] = {
    # --- 看多 ---
    "强烈看多": BULLISH,
    "非常看多": BULLISH,
    "看多": BULLISH,
    "看涨": BULLISH,
    "做多": BULLISH,
    "买入": BULLISH,
    "增持": BULLISH,
    "建仓": BULLISH,
    "加仓": BULLISH,
    "strong_buy": BULLISH,
    "bullish": BULLISH,
    "buy": BULLISH,
    "positive": BULLISH,
    # --- 偏多 ---
    "谨慎看多": LEAN_BULLISH,
    "中性偏多": LEAN_BULLISH,
    "有条件建仓": LEAN_BULLISH,
    "条件建仓": LEAN_BULLISH,
    "谨慎乐观": LEAN_BULLISH,
    "偏多": LEAN_BULLISH,
    "lean_bullish": LEAN_BULLISH,
    "weak_buy": LEAN_BULLISH,
    # --- 中性 ---
    "中性": NEUTRAL,
    "观望": NEUTRAL,
    "持有": NEUTRAL,
    "谨慎": NEUTRAL,
    "等待": NEUTRAL,
    "中性观望": NEUTRAL,
    "neutral": NEUTRAL,
    "hold": NEUTRAL,
    "cautious": NEUTRAL,
    # --- 偏空 ---
    "谨慎看空": LEAN_BEARISH,
    "中性偏空": LEAN_BEARISH,
    "谨慎悲观": LEAN_BEARISH,
    "偏空": LEAN_BEARISH,
    "lean_bearish": LEAN_BEARISH,
    "weak_sell": LEAN_BEARISH,
    # --- 看空 ---
    "强烈看空": BEARISH,
    "非常看空": BEARISH,
    "看空": BEARISH,
    "看跌": BEARISH,
    "做空": BEARISH,
    "卖出": BEARISH,
    "减持": BEARISH,
    "清仓": BEARISH,
    "空仓": BEARISH,
    "回避": BEARISH,
    "strong_sell": BEARISH,
    "bearish": BEARISH,
    "sell": BEARISH,
    "negative": BEARISH,
}

#: (pattern, canonical) sorted by pattern length descending, so that at any
#: position the longest available pattern is consumed first.
_PHRASES: List[tuple] = sorted(
    _PHRASE_TABLE.items(), key=lambda item: len(item[0]), reverse=True
)

#: Exact-match aliases that are not substring-scannable (too short / generic).
_EXACT: Dict[str, str] = {
    "多": BULLISH,
    "空": BEARISH,
    "long": BULLISH,
    "short": BEARISH,
}

_STRIP_CHARS = " \t\r\n*_`'\"　:：,，.。;；()（）[]【】<>《》"


def normalize_direction(value: Any) -> Optional[str]:
    """Map any known direction representation to a canonical direction.

    Returns one of ``CANONICAL_DIRECTIONS``, or ``None`` when the value is not a
    recognizable direction (including explicit non-answers such as ``UNKNOWN``).

    A single short value is expected. Use :func:`classify_direction_phrase` for
    snippets that may contain more than one opinion.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None

    text = str(value).strip().strip(_STRIP_CHARS)
    if not text:
        return None

    upper = text.upper()
    if upper in NON_DIRECTION_VALUES:
        return None

    # Canonical passthrough.
    if text in _DIRECTION_TO_DECISION:
        return text

    lowered = text.lower()
    if lowered in _PHRASE_TABLE:
        return _PHRASE_TABLE[lowered]
    if lowered in _EXACT:
        return _EXACT[lowered]

    # Longest-match-first scan: consume each match so nested or partially
    # overlapping patterns cannot each contribute a separate opinion.
    direction, _ = _resolve_directions(_scan_phrases(text))
    return direction


def _scan_phrases(text: str) -> List[str]:
    """Longest-match-first phrase scan that consumes matched spans."""
    lowered = text.lower()
    found: List[str] = []
    index = 0
    length = len(lowered)
    while index < length:
        for pattern, canonical in _PHRASES:
            if lowered.startswith(pattern, index):
                found.append(canonical)
                index += len(pattern)
                break
        else:
            index += 1
    return found


#: Signed strength of each canonical direction. Used to pick the dominant
#: opinion when one side is expressed more than once (e.g. "看多" + "偏多").
_DIRECTION_STRENGTH = {
    BULLISH: 2,
    LEAN_BULLISH: 1,
    NEUTRAL: 0,
    LEAN_BEARISH: -1,
    BEARISH: -2,
}


def _resolve_directions(found: List[str]) -> tuple:
    """Resolve the opinions found in a snippet into ``(direction, conflict)``.

    Conflict means **directional disagreement** — bullish evidence and bearish
    evidence in the same snippet. A neutral marker alongside a direction is not a
    conflict: phrases like "减持，等待企稳" or "可以建仓，但需耐心" express one
    opinion plus a timing caveat, and treating the hedge as a rival opinion would
    discard a great deal of legitimate signal.

    The laterality rule is what keeps this fail-closed where it matters: bull and
    bear in the same sentence always yields abstention, regardless of order.
    """
    positives = [item for item in found if _DIRECTION_STRENGTH[item] > 0]
    negatives = [item for item in found if _DIRECTION_STRENGTH[item] < 0]

    if positives and negatives:
        return None, True
    if positives:
        return max(positives, key=lambda item: _DIRECTION_STRENGTH[item]), False
    if negatives:
        return min(negatives, key=lambda item: _DIRECTION_STRENGTH[item]), False
    if found:
        # Only neutral markers were present: that is a real (if weak) opinion.
        return NEUTRAL, False
    return None, False


def classify_direction_phrase(text: Any) -> tuple:
    """Classify a snippet that may contain several opinions.

    Returns ``(direction, conflict)`` where ``direction`` is a canonical
    direction or ``None``. When the snippet contains two or more *different*
    directions the result is ``(None, True)`` — contradictory evidence is an
    abstention, not a vote to be broken by keyword order.
    """
    if text is None:
        return None, False
    snippet = str(text).strip().strip(_STRIP_CHARS)
    if not snippet:
        return None, False

    upper = snippet.upper()
    if upper in NON_DIRECTION_VALUES:
        return None, False

    if snippet in _DIRECTION_TO_DECISION:
        return snippet, False

    lowered = snippet.lower()
    if lowered in _EXACT:
        return _EXACT[lowered], False

    return _resolve_directions(_scan_phrases(snippet))


def direction_from_score(score: float) -> str:
    """Map a numeric stance in ``[-1, 1]`` to a canonical direction.

    Mirrors the thresholds already used by ``consensus_service`` so the
    deterministic layer and the parsing layer agree on what "偏多" means.
    """
    if score >= 0.75:
        return BULLISH
    if score >= 0.2:
        return LEAN_BULLISH
    if score <= -0.75:
        return BEARISH
    if score <= -0.2:
        return LEAN_BEARISH
    return NEUTRAL


def to_trading_decision(direction: Any) -> Optional[str]:
    """Map a direction to ``BUY``/``SELL``/``HOLD``, or ``None`` when unknown.

    ``None`` propagates as abstention (``ABSTAIN`` at the persistence boundary);
    it never silently becomes ``HOLD``, because "no view" and "hold" are
    different claims and only one of them is an opinion.
    """
    canonical = normalize_direction(direction)
    if canonical is None:
        return None
    return _DIRECTION_TO_DECISION[canonical]


def coerce_persistable_decision(value: Any) -> str:
    """Coerce a value into a persistable trading decision.

    Anything that is not ``BUY``/``SELL``/``HOLD`` becomes ``ABSTAIN``. In
    particular ``UNKNOWN`` and ``DRY_RUN`` — which the old code path persisted
    because ``"UNKNOWN"`` is truthy — are mapped to an explicit abstention.

    Accepts canonical directions (``看多`` …) as input for convenience.
    """
    text = str(value or "").strip().upper().strip(_STRIP_CHARS)
    if text in (BUY, SELL, HOLD):
        return text
    canonical = normalize_direction(value)
    if canonical is not None:
        return _DIRECTION_TO_DECISION[canonical]
    return ABSTAIN


# --- confidence --------------------------------------------------------------


def _fallback_confidence_from_direction(direction: Optional[str]) -> int:
    """Deterministic prior when the model omits confidence.

    Preserves the historical values (68 / 58 / 48 / 45) so downstream calibration
    comparisons remain valid across the change.
    """
    if direction in (BULLISH, BEARISH):
        return 68
    if direction in (LEAN_BULLISH, LEAN_BEARISH):
        return 58
    if direction == NEUTRAL:
        return 48
    return 45


def coerce_confidence(raw: Any, direction: Optional[str]) -> int:
    """Normalize a model-supplied confidence to an integer in ``[0, 100]``.

    Note: this only *normalizes* the number. The audit found LLM self-reported
    confidence to be uncalibrated (``r = +0.033``, and the ``[80, 101)`` bucket
    was correct only ``46.7%`` of the time, i.e. non-monotonic). Consumers that
    need calibrated confidence must derive it from measurable inputs
    (see ``api/services/confidence_service.py``); this function is retained for
    parsing fidelity and for the analysts' internal trace only.
    """
    if raw is None or raw == "":
        return _fallback_confidence_from_direction(direction)
    if isinstance(raw, bool):
        return _fallback_confidence_from_direction(direction)

    if isinstance(raw, (int, float)):
        value = float(raw)
        if 0.0 <= value <= 1.0:
            return int(max(0, min(100, round(value * 100))))
        return int(max(0, min(100, round(value))))

    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped.isdigit():
            return int(max(0, min(100, int(stripped))))
        try:
            value = float(stripped)
            if 0.0 <= value <= 1.0:
                return int(max(0, min(100, round(value * 100))))
            if value == value:  # not NaN
                return int(max(0, min(100, round(value))))
        except ValueError:
            pass

    text = str(raw).strip().lower()
    bucket = {
        "高": 82,
        "中": 62,
        "低": 42,
        "high": 82,
        "medium": 62,
        "mid": 62,
        "low": 42,
    }
    if text in bucket:
        return bucket[text]
    return _fallback_confidence_from_direction(direction)


def parse_stated_confidence(raw: Any) -> Optional[int]:
    """Parse a model-*stated* confidence, or ``None`` when nothing was stated.

    Deliberately distinct from :func:`coerce_confidence`, which synthesizes a
    direction-dependent prior (68/58/48/45) whenever the value is missing or
    unreadable. That prior is appropriate for a UI trace, but it must never reach
    ``reports.confidence``: a report that stated nothing would then be stored as if
    it had stated 68, which is indistinguishable from a genuine 68 and silently
    poisons every confidence-bucketed statistic.

    ``coerce_confidence`` accepted ``""``, ``"unknown"`` and ``True`` through its
    ``is not None``-style guards and returned the prior for all three, so callers
    that meant "only persist what was actually written" needed this stricter entry
    point. Here, unreadable input yields ``None`` — an absent confidence stays
    visibly absent.
    """
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        return None

    if isinstance(raw, (int, float)):
        value = float(raw)
        if value != value:  # NaN
            return None
        if 0.0 <= value <= 1.0:
            return int(max(0, min(100, round(value * 100))))
        return int(max(0, min(100, round(value))))

    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped.isdigit():
            return int(max(0, min(100, int(stripped))))
        try:
            value = float(stripped)
        except ValueError:
            value = None
        if value is not None:
            if value != value:  # NaN
                return None
            if 0.0 <= value <= 1.0:
                return int(max(0, min(100, round(value * 100))))
            return int(max(0, min(100, round(value))))

    # Only an explicit qualitative word counts; anything else was not a statement.
    bucket = {"高": 82, "中": 62, "低": 42, "high": 82, "medium": 62, "mid": 62, "low": 42}
    return bucket.get(str(raw).strip().lower())


# --- structured block extraction --------------------------------------------

_TAG_RE_CACHE: Dict[str, "re.Pattern[str]"] = {}


def _tag_pattern(tag: str) -> "re.Pattern[str]":
    pattern = _TAG_RE_CACHE.get(tag)
    if pattern is None:
        pattern = re.compile(
            r"<!--\s*" + re.escape(tag) + r"\s*[:：]?\s*", re.IGNORECASE
        )
        _TAG_RE_CACHE[tag] = pattern
    return pattern


def _find_balanced_object(text: str, start: int) -> Optional[str]:
    """Return the balanced ``{...}`` substring beginning at ``start``.

    Tracks string literals and escapes so braces inside strings do not confuse
    the depth count. The previous implementation used the non-greedy
    ``\\{.*?\\}`` regex, which truncates at the first ``}`` and therefore fails
    on any nested object.
    """
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _iter_tag_candidates(text: str, tag: str):
    """Yield ``(match, object_start)`` for every occurrence of ``tag``, in order.

    Iterating matters: a malformed block (unbalanced braces, or an unclosed tag
    whose payload is cut off) must not mask a well-formed block later in the
    document. Using a single ``search()`` made the whole extraction depend on the
    first occurrence being valid.
    """
    blob = str(text)
    for match in _tag_pattern(tag).finditer(blob):
        start = blob.find("{", match.end())
        if start == -1:
            continue
        yield match, start


def extract_tagged_json(text: Any, tag: str) -> Optional[Dict[str, Any]]:
    """Extract a ``<!-- TAG: {...} -->`` payload as a dict.

    Tolerates a missing ``-->``, nested objects, and a malformed occurrence
    followed by a valid one. Returns ``None`` when no occurrence yields a JSON
    object — never a partially-guessed dict.
    """
    if not text:
        return None
    blob = str(text)
    for _match, start in _iter_tag_candidates(blob, tag):
        raw = _find_balanced_object(blob, start)
        if raw is None:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def find_tagged_json_span(text: Any, tag: str) -> Optional[Tuple[int, int]]:
    """Return the ``(start, end)`` span of a whole ``<!-- TAG: {...} -->`` block.

    ``end`` is exclusive. When the closing ``-->`` is missing the span still ends
    after the balanced object, so callers can replace a malformed block instead of
    appending a second one next to it.
    """
    if not text:
        return None
    blob = str(text)
    for match, start in _iter_tag_candidates(blob, tag):
        raw = _find_balanced_object(blob, start)
        if raw is None:
            continue
        end = start + len(raw)
        closer = blob.find("-->", end)
        if closer != -1 and not blob[end:closer].strip():
            end = closer + len("-->")
        return (match.start(), end)
    return None


def replace_tagged_json(
    text: Any,
    tag: str,
    payload: Dict[str, Any],
    *,
    append_if_missing: bool = True,
) -> Tuple[str, bool]:
    """Replace a tagged block's payload, or append a fresh block.

    Returns ``(new_text, replaced)``; ``replaced`` is ``False`` when the tag was
    absent and a new block was appended (or when ``append_if_missing`` is off and
    nothing was done).
    """
    block = f"<!-- {tag}: {json.dumps(payload, ensure_ascii=False)} -->"
    blob = str(text or "")
    span = find_tagged_json_span(blob, tag)
    if span is not None:
        return blob[: span[0]] + block + blob[span[1] :], True
    if not append_if_missing:
        return blob, False
    base = blob.rstrip()
    return (f"{base}\n\n{block}" if base else block), False


# --- the single extraction entry point --------------------------------------

#: Explicitly labelled decision lines. Deliberately narrow: only a labelled line
#: is read, and only the text after the label. Free prose is never scanned.
_LABELLED_PATTERNS = (
    r"风控委员会最终裁决[:：]\s*([^\n*]+)",
    r"最终交易建议[:：]\s*([^\n*]+)",
    r"最终裁决[:：]\s*([^\n*]+)",
    r"最终建议[:：]\s*([^\n*]+)",
    r"最终结论[:：]\s*([^\n*]+)",
    r"核心定性[:：]\s*([^\n*]+)",
    r"^方向[:：]\s*([^\n*]+)",
    r"建议方向[:：]\s*([^\n*]+)",
)

_VERDICT_DIRECTION_KEYS = ("direction", "verdict", "stance", "bias")


@dataclass(frozen=True)
class DirectionResult:
    """Outcome of parsing a document for a direction.

    Attributes
    ----------
    direction:
        Canonical direction, or ``None`` for 不可研判.
    confidence:
        Normalized ``0-100`` model-supplied confidence, or ``None`` when the
        direction is unknown or no confidence was supplied *and* the caller
        should not invent one.
    source:
        Which rule produced the result: ``"verdict_block"``,
        ``"labelled_line"``, ``"verdict_conflict"``, ``"labelled_conflict"``,
        ``"verdict_invalid"`` or ``"none"``. Persisted for observability so the
        abstention rate can be broken down by cause.
    conflict:
        ``True`` when contradictory evidence was found. Such a result always has
        ``direction is None``.
    """

    direction: Optional[str]
    confidence: Optional[int]
    source: str
    conflict: bool = False

    @property
    def is_abstain(self) -> bool:
        return self.direction is None


def extract_direction_result(
    text: Any, *, allow_labelled_lines: bool = True
) -> DirectionResult:
    """Parse a document for a direction, fail-closed.

    Precedence: the structured ``VERDICT`` block, then — when enabled — a single
    explicitly labelled decision line. A ``VERDICT`` block always wins, so a
    later labelled line cannot override or blur it.

    Returns a result whose ``direction`` is ``None`` when the evidence is absent,
    invalid, or contradictory.
    """
    if not text:
        return DirectionResult(None, None, "none", False)

    payload = extract_tagged_json(text, "VERDICT")
    if payload is not None:
        raw_direction = None
        for key in _VERDICT_DIRECTION_KEYS:
            if payload.get(key) not in (None, ""):
                raw_direction = payload.get(key)
                break

        direction, conflict = classify_direction_phrase(raw_direction)
        if conflict:
            return DirectionResult(None, None, "verdict_conflict", True)
        if direction is None:
            return DirectionResult(None, None, "verdict_invalid", False)
        confidence = coerce_confidence(payload.get("confidence"), direction)
        return DirectionResult(direction, confidence, "verdict_block", False)

    if allow_labelled_lines:
        blob = str(text)
        for pattern in _LABELLED_PATTERNS:
            match = re.search(pattern, blob, re.IGNORECASE | re.MULTILINE)
            if not match:
                continue
            direction, conflict = classify_direction_phrase(match.group(1))
            if conflict:
                return DirectionResult(None, None, "labelled_conflict", True)
            if direction is not None:
                return DirectionResult(direction, None, "labelled_line", False)

    return DirectionResult(None, None, "none", False)
