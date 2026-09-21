"""Structured analyst outputs: evidence anchors + machine-readable JSON (self-correction / manager fusion)."""

from __future__ import annotations

import json
import re
from typing import Any, Mapping, Optional

from tradingagents.dataflows.config import get_config
from tradingagents.prompts.catalog import resolve_prompt_language

_ANALYST_JSON_RE = re.compile(
    r"<!--\s*ANALYST_JSON:\s*(\{.*?\})\s*-->",
    re.IGNORECASE | re.DOTALL,
)


def get_analyst_json_instruction(*, agent_role: str, config: Mapping[str, Any] | None = None) -> str:
    """Footer block forcing one-line ANALYST_JSON; language follows prompt pack."""
    cfg = dict(config or get_config())
    lang = resolve_prompt_language(cfg)
    ar = str(agent_role or "analyst").strip()
    if lang == "zh":
        return (
            "\n\n---\n"
            "在报告正文结束后，**另起一段**追加且仅追加一行机读摘要（不得省略，键名勿改）：\n"
            f"<!-- ANALYST_JSON: {{\"agent_role\":\"{ar}\",\"direction\":\"看多|偏多|中性|偏空|看空\","
            '"confidence":0-100整数,"evidence_anchors":[{"field":"指标或字段名","value":"必须逐字拷贝自上文工具数据/引文中已出现的片段（禁止编造数字）","source":"工具或段落名如get_stock_data"}],'
            '"data_gaps":["若缺数据写此处，否则[]]} -->\n'
            "规则：direction 与正文结论一致；最多 4 条 evidence_anchors；value 必须能在上文检索到。"
        )
    return (
        "\n\n---\n"
        "After the report, append exactly ONE machine-readable line (do not omit; keep keys):\n"
        f'<!-- ANALYST_JSON: {{"agent_role":"{ar}","direction":"BULLISH|LEAN_BULLISH|NEUTRAL|LEAN_BEARISH|BEARISH",'
        '"confidence":0-100,"evidence_anchors":[{"field":"name","value":"verbatim substring from data above","source":"tool"}],'
        '"data_gaps":[]}} -->\n'
        "Rules: direction matches narrative; max 4 anchors; values must be copy-pasted from supplied data."
    )


def extract_analyst_structured_json(text: str) -> Optional[dict[str, Any]]:
    m = _ANALYST_JSON_RE.search(text or "")
    if not m:
        return None
    try:
        raw = m.group(1).strip().replace("\n", " ").replace("\r", " ")
        return json.loads(raw)
    except Exception:
        return None


def build_structured_brief_for_research_manager(traces: list[Any]) -> str:
    """Compact bullet list for Research Manager (fusion from structured layer first)."""
    if not traces:
        return "（尚无分析师结构化输出。）"
    lines: list[str] = []
    for t in traces:
        if not isinstance(t, dict):
            continue
        s = t.get("structured")
        if not isinstance(s, dict):
            continue
        agent = str(t.get("agent") or s.get("agent_role") or "?")
        direction = str(s.get("direction") or t.get("verdict") or "")
        conf = s.get("confidence", t.get("confidence", ""))
        anchors = s.get("evidence_anchors") or []
        gap = s.get("data_gaps") or []
        a_parts: list[str] = []
        if isinstance(anchors, list):
            for it in anchors[:4]:
                if not isinstance(it, dict):
                    continue
                a_parts.append(
                    f"{it.get('field','')}={str(it.get('value',''))[:80]}"
                )
        anchor_txt = "；".join(a_parts) if a_parts else "（无锚点）"
        gap_txt = ""
        if isinstance(gap, list) and gap:
            gap_txt = f" | 数据缺口: {', '.join(str(x) for x in gap[:3])}"
        lines.append(f"- **{agent}** → {direction} (置信 {conf}) | 锚点: {anchor_txt}{gap_txt}")
    if not lines:
        return "（分析师未输出 ANALYST_JSON，请依据正文与 VERDICT；不得杜撰锚点。）"
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
