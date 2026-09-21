from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Iterable, Literal

from tradingagents.dataflows.interface import route_to_vendor

_CODE_RE = re.compile(r"^(\d{6})\.(SH|SZ|BJ)$")
_PROFILE_WEIGHTS: dict[str, dict[str, float]] = {
    "ashare_balanced": {"momentum": 0.40, "activity": 0.35, "near_high": 0.25},
    "ashare_aggressive": {"momentum": 0.50, "activity": 0.30, "near_high": 0.20},
    "us_balanced": {"momentum": 0.45, "activity": 0.25, "near_high": 0.30},
}


@dataclass
class DailyStockCandidate:
    symbol: str
    name: str
    score: float
    reasons: list[str]
    quote: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "code": self.symbol,
            "name": self.name,
            "score": self.score,
            "reasons": self.reasons,
            "live_price": _to_float(self.quote.get("price")),
            "price_change_pct": _to_float(self.quote.get("change_pct")),
            "day_high": _to_float(self.quote.get("high")),
            "amount": _to_float(self.quote.get("amount")),
            "volume": _to_float(self.quote.get("volume")),
            "quote_time": self.quote.get("quote_time"),
            "quote_source": self.quote.get("source"),
        }


def analyze_daily_candidates(
    *,
    symbol_to_name: dict[str, str],
    top_k: int,
    min_change_pct: float,
    market: Literal["cn", "us"] = "cn",
    profile: str | None = None,
    momentum_weight: float | None = None,
    activity_weight: float | None = None,
    near_high_weight: float | None = None,
) -> dict[str, Any]:
    """统一的日度选股逻辑（推荐页、定时推荐、后续策略任务共用）。"""
    top_k = max(1, min(int(top_k), 20))
    min_change_pct = float(min_change_pct)
    resolved_profile = profile or ("ashare_balanced" if market == "cn" else "us_balanced")
    weights = resolve_score_weights(
        profile=resolved_profile,
        momentum_weight=momentum_weight,
        activity_weight=activity_weight,
        near_high_weight=near_high_weight,
    )

    symbols = list(symbol_to_name.keys())
    quotes = _fetch_live_quotes(symbols)

    ranked: list[DailyStockCandidate] = []
    for symbol in symbols:
        quote = quotes.get(symbol) or {}
        change_pct = _to_float(quote.get("change_pct"))
        if change_pct is None or change_pct < min_change_pct:
            continue

        price = _to_float(quote.get("price"))
        day_high = _to_float(quote.get("high"))
        amount = _to_float(quote.get("amount"))
        volume = _to_float(quote.get("volume"))

        score = 0.0
        reasons: list[str] = []

        # 1) 动量分：[-10,+10] -> [0,100]
        momentum = max(-10.0, min(10.0, change_pct))
        momentum_score = (momentum + 10.0) * 5.0
        score += momentum_score * weights["momentum"]
        if change_pct >= 3:
            reasons.append(f"当日涨幅较强（{change_pct:+.2f}%）")
        elif change_pct >= 0:
            reasons.append(f"当日保持上涨（{change_pct:+.2f}%）")
        else:
            reasons.append(f"当日回调（{change_pct:+.2f}%）")

        # 2) 活跃度分：成交额对数量级进行对数压缩 -> [0,100]
        if amount and amount > 0:
            activity_raw = max(0.0, min(100.0, (math.log10(amount + 1.0) - 5.0) * 28.0))
            score += activity_raw * weights["activity"]
            if amount >= 1e9:
                reasons.append("成交额活跃（>=10亿）")
            elif amount >= 3e8:
                reasons.append("成交额较活跃")

        # 3) 强势分：逼近日高 -> [0,100]
        if price and day_high and day_high > 0:
            dist = abs(day_high - price) / day_high
            near_high_raw = max(0.0, 100.0 - dist * 800.0)
            score += near_high_raw * weights["near_high"]
            if dist <= 0.008:
                reasons.append("价格逼近日内高点")

        if volume and volume > 0 and not any("成交额" in r for r in reasons):
            reasons.append("成交量有支撑")

        ranked.append(
            DailyStockCandidate(
                symbol=symbol,
                name=symbol_to_name.get(symbol) or symbol,
                score=round(score, 2),
                reasons=reasons[:3],
                quote=quote,
            )
        )

    ranked.sort(key=lambda x: x.score, reverse=True)
    return {
        "pool_size": len(symbols),
        "scored_size": len(ranked),
        "items": [x.to_dict() for x in ranked[:top_k]],
        "scoring_model": {
            "market": market,
            "profile": resolved_profile,
            "weights": weights,
        },
    }


def resolve_score_weights(
    *,
    profile: str,
    momentum_weight: float | None,
    activity_weight: float | None,
    near_high_weight: float | None,
) -> dict[str, float]:
    base = dict(_PROFILE_WEIGHTS.get(profile, _PROFILE_WEIGHTS["ashare_balanced"]))
    custom_vals = [momentum_weight, activity_weight, near_high_weight]
    if any(v is not None for v in custom_vals):
        m = _clean_weight(momentum_weight, base["momentum"])
        a = _clean_weight(activity_weight, base["activity"])
        n = _clean_weight(near_high_weight, base["near_high"])
        total = m + a + n
        if total <= 0:
            return base
        return {
            "momentum": round(m / total, 4),
            "activity": round(a / total, 4),
            "near_high": round(n / total, 4),
        }
    return base


def _clean_weight(value: float | None, fallback: float) -> float:
    if value is None:
        return float(fallback)
    try:
        x = float(value)
    except Exception:
        return float(fallback)
    return max(0.0, min(10.0, x))


def normalize_symbol(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    if not text:
        return None
    if _CODE_RE.match(text):
        return text
    if re.match(r"^\d{6}$", text):
        if text.startswith("6"):
            return f"{text}.SH"
        if text.startswith(("0", "3")):
            return f"{text}.SZ"
        if text.startswith(("4", "8")):
            return f"{text}.BJ"
    return None


def merge_seed_symbols(symbol_to_name: dict[str, str], seeds: Iterable[str] | None) -> None:
    for s in seeds or []:
        norm = normalize_symbol(s)
        if norm and norm not in symbol_to_name:
            symbol_to_name[norm] = norm


def _fetch_live_quotes(symbols: list[str]) -> dict[str, dict[str, Any]]:
    if not symbols:
        return {}
    try:
        result_json = route_to_vendor("get_realtime_quotes", symbols)
        return json.loads(result_json)
    except Exception:
        return {}


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), 4)
    except Exception:
        return None
