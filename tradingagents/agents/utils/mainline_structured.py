"""结构化主线输出：MAINLINE_JSON / SELECTOR_JSON 的指令、提取与规范化。

与 analyst_structured.py 同风格：LLM 在报告末尾追加一行机读 JSON，
此处负责提取、类型规范化与轻量校验（防幻觉的机器侧闸门）。
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

_MAINLINE_JSON_RE = re.compile(
    r"<!--\s*MAINLINE_JSON:\s*(\{.*?\})\s*-->",
    re.IGNORECASE | re.DOTALL,
)
_SELECTOR_JSON_RE = re.compile(
    r"<!--\s*SELECTOR_JSON:\s*(\{.*?\})\s*-->",
    re.IGNORECASE | re.DOTALL,
)

_PHASES = ("发酵", "主升", "高位分歧", "退潮")
_STATUSES = ("延续", "扩散", "退潮", "新发")
_TIERS = ("龙头", "中军", "补涨")


def _norm_conf(v: Any) -> int:
    """置信度归一化到 0-100 整数（支持 0-1 与 0-100 两种口径）。"""
    try:
        f = float(v)
        if 0.0 <= f <= 1.0:
            return int(max(0, min(100, round(f * 100))))
        return int(max(0, min(100, round(f))))
    except (TypeError, ValueError):
        return 0


def _norm_str(v: Any, default: str = "") -> str:
    if v is None:
        return default
    s = str(v).strip()
    return s


def get_mainline_json_instruction() -> str:
    """Footer forcing a single-line MAINLINE_JSON with the mainline schema."""
    return (
        "\n\n---\n"
        "在报告正文结束后，**另起一段**追加且仅追加一行机读摘要（不得省略，键名勿改）：\n"
        '<!-- MAINLINE_JSON: {"market_reading":"1-2句整体盘面解读","mainlines":[{"name":"主线名",'
        '"type":"concept|industry","phase":"发酵|主升|高位分歧|退潮","confidence":0-100整数,'
        '"status_vs_yesterday":"延续|扩散|退潮|新发","logic":"核心逻辑2-3句",'
        '"drivers":["驱动事件1","驱动事件2"],'
        '"representative_boards":[{"board":"板块名","chg_1d":数值}],'
        '"leading_stocks":["领涨股1"],"verify_conditions":["验证条件"],"risks":["风险"],'
        '"evidence":"引用自输入数据的真实事实"}],"watchlist":["观察方向"],"data_gaps":[]} -->\n'
        "规则：1~3 条真主线（宁缺毋滥）；confidence 只可填 0-100 整数；"
        "phase 与 status_vs_yesterday 只可填括号内枚举；"
        "evidence 必须逐字引用输入中的板块名/涨幅/资金数字，禁止编造。"
    )


def get_selector_json_instruction() -> str:
    """Footer forcing a single-line SELECTOR_JSON with the candidate schema."""
    return (
        "\n\n---\n"
        "在正文结束后，**另起一段**追加且仅追加一行机读摘要（不得省略，键名勿改）：\n"
        '<!-- SELECTOR_JSON: {"candidates":[{"symbol":"600111.SH","name":"北方稀土",'
        '"mainline":"主线名","tier":"龙头|中军|补涨","score":0-100整数,'
        '"reasons":["入选理由，引用池中真实数据"],"entry_hint":"介入位置建议","risk":"风险"}],'
        '"data_gaps":[]} -->\n'
        "规则：symbol 格式如 600111.SH / 300308.SZ，必须来自候选股池；每主线最多 5 只；"
        "tier 只可填 龙头/中军/补涨；禁止编造池外的代码/名称/数据。"
    )


def extract_mainline_json(text: str) -> Optional[dict[str, Any]]:
    m = _MAINLINE_JSON_RE.search(text or "")
    if not m:
        return None
    try:
        raw = m.group(1).strip().replace("\n", " ").replace("\r", " ")
        return json.loads(raw)
    except Exception:
        return None


def extract_selector_json(text: str) -> Optional[dict[str, Any]]:
    m = _SELECTOR_JSON_RE.search(text or "")
    if not m:
        return None
    try:
        raw = m.group(1).strip().replace("\n", " ").replace("\r", " ")
        return json.loads(raw)
    except Exception:
        return None


def normalize_mainlines(data: Optional[dict]) -> dict:
    """规范化主线输出：类型收窄、枚举校验、缺失字段补默认。"""
    if not isinstance(data, dict):
        return {"market_reading": "", "mainlines": [], "watchlist": [], "data_gaps": []}
    mainlines: list[dict] = []
    for m in data.get("mainlines") or []:
        if not isinstance(m, dict):
            continue
        name = _norm_str(m.get("name"))
        if not name:
            continue
        phase = _norm_str(m.get("phase"))
        if phase not in _PHASES:
            phase = ""
        status = _norm_str(m.get("status_vs_yesterday"))
        if status not in _STATUSES:
            status = ""
        boards = []
        for b in m.get("representative_boards") or []:
            if isinstance(b, dict):
                boards.append({"board": _norm_str(b.get("board")), "chg_1d": b.get("chg_1d")})
            elif isinstance(b, str):
                boards.append({"board": b, "chg_1d": None})
        mainlines.append(
            {
                "name": name,
                "type": _norm_str(m.get("type"), "concept") if m.get("type") in ("concept", "industry") else "concept",
                "phase": phase,
                "confidence": _norm_conf(m.get("confidence")),
                "status_vs_yesterday": status,
                "logic": _norm_str(m.get("logic")),
                "drivers": [str(x) for x in (m.get("drivers") or [])][:6],
                "representative_boards": boards[:4],
                "leading_stocks": [str(x) for x in (m.get("leading_stocks") or [])][:8],
                "verify_conditions": [str(x) for x in (m.get("verify_conditions") or [])][:4],
                "risks": [str(x) for x in (m.get("risks") or [])][:4],
                "evidence": _norm_str(m.get("evidence")),
            }
        )
    return {
        "market_reading": _norm_str(data.get("market_reading")),
        "mainlines": mainlines[:3],
        "watchlist": [str(x) for x in (data.get("watchlist") or [])][:6],
        "data_gaps": [str(x) for x in (data.get("data_gaps") or [])][:6],
    }


def normalize_candidates(data: Optional[dict]) -> list[dict]:
    """规范化选股输出：类型收窄、tier 校验、剔除非池字段。"""
    if not isinstance(data, dict):
        return []
    out: list[dict] = []
    for c in data.get("candidates") or []:
        if not isinstance(c, dict):
            continue
        symbol = _norm_str(c.get("symbol"))
        name = _norm_str(c.get("name"))
        if not symbol or not name:
            continue
        tier = _norm_str(c.get("tier"))
        if tier not in _TIERS:
            tier = ""
        out.append(
            {
                "symbol": symbol,
                "name": name,
                "mainline": _norm_str(c.get("mainline")),
                "tier": tier,
                "score": _norm_conf(c.get("score")),
                "reasons": [str(x) for x in (c.get("reasons") or [])][:5],
                "entry_hint": _norm_str(c.get("entry_hint")),
                "risk": _norm_str(c.get("risk")),
            }
        )
    return out
