from __future__ import annotations

import re
from typing import Any

from .access import build_degraded_freshness_pool
from .aggregator import build_freshness_summary, clamp_confidence

# 报告级置信度的来源文档（`api/services/report_service.py:_resolve_confidence`
# 只从这两份里读）。**只有**这两份的正文需要跟着上限走。
_DECISION_DOC_KEYS = ("final_trade_decision", "trader_investment_plan")

# 与 `report_service._extract_confidence_regex` 保持同一套形态。
_VERDICT_JSON_CONF_RE = re.compile(r'("confidence"\s*:\s*)(\d+)')
_PLAIN_CONF_RE = re.compile(
    r"((?:置信度|confidence)[:：]\s*)(\d+)(\s*%)", re.IGNORECASE
)


def _clamp_prose_confidence(text: Any, cap: int) -> Any:
    """把决策文档正文里声明的置信度压到不超过 `cap`。

    只改数字、不新增数字：文中本来没写置信度就保持不写（"未声明"不得被伪造成
    一个具体值）。已在 `cap` 以内的值原样保留（`min`），所以不需要封顶时正文
    **逐字节不变**。
    """
    if not isinstance(text, str) or not text:
        return text

    def _min_of(match: re.Match[str]) -> str:
        return f"{match.group(1)}{min(int(match.group(2)), cap)}"

    out = _VERDICT_JSON_CONF_RE.sub(_min_of, text)
    return _PLAIN_CONF_RE.sub(
        lambda m: f"{m.group(1)}{min(int(m.group(2)), cap)}{m.group(3)}", out
    )


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
        capped = clamp_confidence(
            int(result["confidence"]), summary["overall_status"]
        )
        result["confidence"] = capped
        # 上限必须**同时**落到正文（审计 §4.17）：旧实现只压列，于是同一份报告里
        # 列写 40、正文 VERDICT 写 70，任何从正文重算的消费者都看到未封顶值。
        # 各分析师报告的自评置信度是另一个量，不动。
        if capped is not None:
            for key in _DECISION_DOC_KEYS:
                if key in result:
                    result[key] = _clamp_prose_confidence(result[key], capped)
    return result
