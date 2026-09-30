"""Structured analyst outputs: evidence anchors + machine-readable JSON (self-correction / manager fusion)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from tradingagents.agents.utils.direction import normalize_direction
from tradingagents.dataflows.config import get_config
from tradingagents.prompts.catalog import resolve_prompt_language

_ANALYST_JSON_RE = re.compile(
    r"<!--\s*ANALYST_JSON:\s*(\{.*?\})\s*-->",
    re.IGNORECASE | re.DOTALL,
)

# 裸写形态：模型偶尔漏掉 `<!-- -->` 注释包裹，只写 `ANALYST_JSON: {...}`。
# 实测（600699.SH 2026-09-29）：7 个维度里 6 个写了包裹形态，smart_money 漏了包裹，
# JSON 本身完整合法且明确写了 "direction":"偏多"，却因正则要求 `<!--`/`-->` 而被丢弃，
# 该分析师于是被排除在共识投票之外——UI 上显示为「N 份报告未计入共识（未给出方向）」。
# 修复后前四份报告里这类漏写占 3/28 个分析师位（10.7%）。
_ANALYST_JSON_MARKER_RE = re.compile(r"ANALYST_JSON\s*:\s*", re.IGNORECASE)


def _has_stated_direction(obj: Mapping[str, Any]) -> bool:
    """裸写块必须自带一个真正写出来的方向，才值得我们接受它。

    裸写形态没有 `-->` 作定界符，证据强度弱于包裹形态；要求 `direction` 是**非空
    字符串**是这个弱形态的自我证明。这既挡住"正文里提到 ANALYST_JSON 字样"，也守住
    fail-closed：绝不凭一个没有方向的块去造一个方向。包裹形态不做此要求（它的边界由
    `-->` 确定），是否"无方向"仍由 resolve_analyst_direction 判定。
    """
    value = obj.get("direction")
    return isinstance(value, str) and bool(value.strip())


def _scan_json_object(text: str, start: int) -> Optional[str]:
    """从 ``text[start]``（应为 ``{``）起按大括号配平扫出一个完整 JSON 对象。

    这里不能用正则：裸写形态没有 `-->` 终止符，非贪婪 `\\{.*?\\}` 会截在半路，贪婪则
    会越过对象末尾。按深度配平，并跳过字符串内部的括号、正确处理反斜杠转义。
    """
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _load_json_lenient(raw: str) -> Optional[dict[str, Any]]:
    """解析一个机读块；块内的字面换行按既有做法折成空格（JSON 字符串不允许裸换行）。"""
    try:
        obj = json.loads(raw.strip().replace("\n", " ").replace("\r", " "))
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _extract_bare_json(text: str) -> Optional[dict[str, Any]]:
    """接受没有 `<!-- -->` 包裹的 `ANALYST_JSON: {...}`（见文件末尾测试的说明）。"""
    for marker in _ANALYST_JSON_MARKER_RE.finditer(text):
        brace = text.find("{", marker.end())
        if brace == -1:
            continue
        raw = _scan_json_object(text, brace)
        if raw is None:
            continue
        obj = _load_json_lenient(raw)
        if obj is not None and _has_stated_direction(obj):
            return obj
    return None


@dataclass(frozen=True)
class AnalystDirection:
    """The direction one analyst actually stated, and which block it came from.

    ``direction is None`` is the **only** condition that means "this analyst
    stated nothing verifiable", and such a trace must be excluded from the
    consensus vote. A non-``None`` direction is never invented here: it is
    verbatim a direction one of the two machine-readable blocks carried, both of
    which the model wrote itself.

    ``source`` is ``"verdict"``, ``"analyst_json"`` or ``"none"``.
    """

    direction: Optional[str]
    confidence: Optional[int]
    source: str

    @property
    def parsed(self) -> bool:
        return self.direction is not None


def _int_confidence(raw: Any) -> Optional[int]:
    """0-100 integer, or ``None`` when absent/not a number.

    Never fabricates a value: the display must not show a confidence the model
    did not state.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return int(max(0, min(100, round(float(raw)))))
    text = str(raw).strip()
    try:
        return int(max(0, min(100, round(float(text)))))
    except (TypeError, ValueError):
        return None


def resolve_analyst_direction(
    *,
    verdict: Any = None,
    confidence: Any = None,
    verdict_parsed: Any = None,
    structured: Any = None,
) -> AnalystDirection:
    """Resolve one analyst's direction from the machine-readable blocks it wrote.

    Precedence is deliberate, and this function is its **single definition** —
    previously the three consumers each had their own rule and therefore
    disagreed about the same report:

    * ``consensus_service`` read only ``verdict`` + ``verdict_parsed``;
    * ``build_structured_brief_for_research_manager`` preferred
      ``structured.direction`` over ``verdict``.

    so one stored report could be 偏多 to the research manager and
    中性-or-dropped to the consensus card beside it. Now both call this.

    1. the ``VERDICT`` block, when it parsed (``verdict_parsed is not False``).
       Traces written before that flag existed carry no value and are treated as
       parsed, so historical rows keep counting instead of being silently dropped;
    2. the ``ANALYST_JSON`` block's ``direction`` — the block the model actually
       emits, and the whole reason a report with a perfectly good stated direction
       used to be reported to the user as "解析失败";
    3. otherwise ``None``: no opinion. Exclude from the vote, never guess.
    """
    if verdict_parsed is not False:
        normalized = normalize_direction(verdict)
        if normalized is not None:
            return AnalystDirection(normalized, _int_confidence(confidence), "verdict")

    payload = structured if isinstance(structured, Mapping) else None
    if payload:
        normalized = normalize_direction(payload.get("direction"))
        if normalized is not None:
            return AnalystDirection(
                normalized, _int_confidence(payload.get("confidence")), "analyst_json"
            )

    return AnalystDirection(None, None, "none")


def resolve_trace_direction(trace: Any) -> AnalystDirection:
    """Same as :func:`resolve_analyst_direction`, reading a stored trace mapping.

    Every consumer of ``analyst_traces`` should go through this so they cannot
    drift apart again.
    """
    if not isinstance(trace, Mapping):
        return AnalystDirection(None, None, "none")
    structured = trace.get("structured")
    return resolve_analyst_direction(
        verdict=trace.get("verdict"),
        confidence=trace.get("confidence"),
        verdict_parsed=trace.get("verdict_parsed"),
        structured=structured if isinstance(structured, Mapping) else None,
    )


def get_analyst_json_instruction(*, agent_role: str, config: Mapping[str, Any] | None = None) -> str:
    """Footer block forcing one-line ANALYST_JSON; language follows prompt pack."""
    cfg = dict(config or get_config())
    lang = resolve_prompt_language(cfg)
    ar = str(agent_role or "analyst").strip()
    if lang == "zh":
        return (
            "\n\n---\n"
            "在报告正文结束后，**另起一段**追加机读摘要块（不得省略，键名勿改）：\n"
            f"<!-- ANALYST_JSON: {{\"agent_role\":\"{ar}\",\"direction\":\"看多|偏多|中性|偏空|看空\","
            '"confidence":0-100整数,"evidence_anchors":[{"field":"指标或字段名","value":"必须逐字拷贝自上文工具数据/引文中已出现的片段（禁止编造数字）","source":"工具或段落名如get_stock_data"}],'
            '"data_gaps":["若缺数据写此处，否则[]]} -->\n'
            "规则：这是本报告**唯一**的机读块，你的方向结论就写在这里的 direction 字段；"
            "正文中不要再另写 VERDICT 等其他机读块（重复的机读块会互相冲突）。\n"
            "direction 只可填：看多 / 偏多 / 中性 / 偏空 / 看空（本维度对次日涨跌无明确指向时请选「中性」；"
            "证据不足、证据彼此冲突、或证据只支撑更长期限，都是选「中性」的正当理由，不算回避）\n"
            "confidence：填 0-100 的整数，表示你对本方向判断的主观置信度"
            "（50=证据弱或矛盾，65-75=多数证据支持，85+ 为多维度强一致）。\n"
            "direction 与正文结论一致；最多 4 条 evidence_anchors；value 必须能在上文检索到。"
        )
    return (
        "\n\n---\n"
        "After the report, append the machine-readable summary block (do not omit; keep keys):\n"
        f'<!-- ANALYST_JSON: {{"agent_role":"{ar}","direction":"BULLISH|LEAN_BULLISH|NEUTRAL|LEAN_BEARISH|BEARISH",'
        '"confidence":0-100,"evidence_anchors":[{"field":"name","value":"verbatim substring from data above","source":"tool"}],'
        '"data_gaps":[]}} -->\n'
        "Rules: this is the report's ONLY machine-readable block and it carries your direction "
        "conclusion in its direction field -- do not emit a separate VERDICT block as well "
        "(duplicate machine-readable blocks contradict each other).\n"
        "direction must be one of: BULLISH / LEAN_BULLISH / NEUTRAL / LEAN_BEARISH / BEARISH "
        "(use LEAN_BULLISH or LEAN_BEARISH when data leans directionally but lacks full "
        "confirmation; choose NEUTRAL whenever this dimension does not point clearly at the "
        "next-day move -- insufficient, conflicting, or longer-horizon-only evidence are all "
        "valid reasons, and NEUTRAL is not a cop-out).\n"
        "confidence must be an integer 0-100 (50=weak or conflicting evidence, 65-75=most "
        "evidence agrees, 85+=strong multi-factor alignment).\n"
        "direction matches narrative; max 4 anchors; values must be copy-pasted from supplied data."
    )


def extract_analyst_structured_json(text: str) -> Optional[dict[str, Any]]:
    """取出分析师写的 ANALYST_JSON 块。

    两条路径，顺序有意为之：

    1. **包裹形态** ``<!-- ANALYST_JSON: {...} -->`` —— 有 `-->` 定界，最可靠，优先；
    2. **裸写形态** ``ANALYST_JSON: {...}`` —— 模型漏写包裹时的回退，要求 JSON 合法
       且自带非空 ``direction``。

    只做"读出模型自己写的东西"，不推断、不补全：两条路径都失败就返回 None，由
    :func:`resolve_analyst_direction` 判定该分析师"未给出方向"并被排除出共识。
    """
    text = text or ""
    m = _ANALYST_JSON_RE.search(text)
    if m:
        obj = _load_json_lenient(m.group(1))
        if obj is not None:
            return obj
    return _extract_bare_json(text)


def _is_failure_anchor(value: Any) -> bool:
    """锚点的 value 是否为**取数失败文案**（而非事实片段）。

    判据复用引擎层唯一权威处 `dataflows/vendor_text.py`，以免这里又长出一份
    关键词表（本次审计的成因之一就是同一判断有两份实现）。
    """
    if not isinstance(value, str) or not value.strip():
        return False
    from tradingagents.dataflows.vendor_text import (
        looks_like_empty_text,
        looks_like_failure_text,
    )

    return looks_like_failure_text(value) or looks_like_empty_text(value)


def build_structured_brief_for_research_manager(traces: list[Any]) -> str:
    """Compact bullet list for Research Manager (fusion from the structured layer first).

    The direction comes from :func:`resolve_trace_direction`, the same resolver the
    consensus layer uses, so the manager and the consensus card can no longer
    disagree about one report.

    An analyst is only skipped when **no** machine-readable block stated a
    direction. Previously the loop required ``structured`` to exist and dropped
    the trace otherwise -- the mirror image of the consensus bug, where a report
    whose VERDICT parsed but whose ANALYST_JSON was missing silently vanished from
    the manager's input.
    """
    if not traces:
        return "（尚无分析师结构化输出。）"
    lines: list[str] = []
    for t in traces:
        if not isinstance(t, dict):
            continue
        s = t.get("structured")
        s = s if isinstance(s, dict) else {}
        decision = resolve_trace_direction(t)
        if decision.direction is None:
            continue
        agent = str(t.get("agent") or s.get("agent_role") or "?")
        direction = decision.direction
        conf = decision.confidence if decision.confidence is not None else ""
        anchors = s.get("evidence_anchors") or []
        gap = s.get("data_gaps") or []
        a_parts: list[str] = []
        dropped_failure_anchors = 0
        if isinstance(anchors, list):
            for it in anchors:
                if not isinstance(it, dict):
                    continue
                # **失败文案不得充当证据锚点。**
                #
                # 提示词要求 anchor 的 value「必须逐字拷贝自上文工具数据」，所以只要
                # 取数失败文案出现在上文，模型照抄它就是"合规"的——实测 59422 条锚点
                # 里 1222 条（2.1%）正是失败文案。指望提示词拦住是不可靠的，
                # 必须在**我们自己的渲染出口**剔除。
                #
                # 剔除必须**说出来**（计入下方 `失败锚点` 提示），静默丢弃等于把
                # 数据质量问题藏起来。
                if _is_failure_anchor(it.get("value")):
                    dropped_failure_anchors += 1
                    continue
                if len(a_parts) >= 4:
                    continue
                a_parts.append(
                    f"{it.get('field','')}={str(it.get('value',''))[:80]}"
                )
        anchor_txt = "；".join(a_parts) if a_parts else "（无锚点）"
        gap_txt = ""
        if isinstance(gap, list) and gap:
            gap_txt = f" | 数据缺口: {', '.join(str(x) for x in gap[:3])}"
        if dropped_failure_anchors:
            gap_txt += f" | 已剔除 {dropped_failure_anchors} 条失败锚点（取数失败不可作为证据）"
        lines.append(f"- **{agent}** → {direction} (置信 {conf}) | 锚点: {anchor_txt}{gap_txt}")
    if not lines:
        return "（分析师未输出可解析的方向，请依据正文；不得杜撰锚点。）"
    return "\n".join(lines)


def format_traces_for_critic(traces: list[Any], max_chars: int = 6000) -> str:
    """Narrow view for Decision Critic."""
    lines: list[str] = []
    for t in traces or []:
        if not isinstance(t, dict):
            continue
        s = t.get("structured") if isinstance(t.get("structured"), dict) else {}
        agent = str(t.get("agent", ""))
        verdict = str(t.get("verdict", ""))
        lines.append(f"{agent}: verdict={verdict} structured={json.dumps(s, ensure_ascii=False)[:800]}")
    body = "\n".join(lines)
    return body[:max_chars]
