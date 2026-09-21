"""Full-market A-share scanner — v3 (six-factor model with evidence-based design).

Factor design principles (A-share empirical research 2015-2025):
─────────────────────────────────────────────────────────────────
 Factor          Direction  Evidence
 ──────          ─────────  ────────
 momentum        +/-        Short-term (1-3d) continuation for extreme moves (涨停);
                             medium-term (5-7d) shows reversal. Direction is profile-dependent.
 activity        +          Log-scaled amount; higher = more discoverable.
 near_high       +          Stocks near intraday high show short-term control.
 sector          +          Board fund-flow rank; top sectors attract follow-through.
 volume_ratio    +          放量 confirms real buying; 量比≥2 meaningful.
 volatility      -          Low amplitude / low intraday range = stable quality.
                             2024 top factor: low-volatility beats market on risk-adj return.
 turnover        bell       Optimal range 2-8%. Too low = illiquid; too HIGH = hot-money top.
                             Research: low turnover has ICIR=-2.04; factor used in NEGATIVE direction
                             for medium-term, but integrated here as a bell-curve quality score.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any

from tradingagents.dataflows.interface import route_to_vendor

from . import daily_stock_analysis_service

logger = logging.getLogger(__name__)

# ── Universe cache ────────────────────────────────────────────────────────────
_CN_UNIVERSE_CACHE: dict[str, str] | None = None
_CN_UNIVERSE_LOADED_AT: float = 0.0
_CN_UNIVERSE_TTL = 6 * 3600

# ── Sector / board fund-flow cache ────────────────────────────────────────────
_SECTOR_CACHE: dict[str, float] | None = None
_SECTOR_MEMBERSHIP_CACHE: dict[str, str] | None = None
_SECTOR_CACHE_TS: float = 0.0
_SECTOR_CACHE_TTL: float = 1800.0

# ── Enhanced spot-data cache ──────────────────────────────────────────────────
_SPOT_ENHANCED_CACHE: dict[str, dict[str, Any]] | None = None
_SPOT_ENHANCED_TS: float = 0.0
_SPOT_ENHANCED_TTL: float = 60.0

# ── Per-strategy recommended hold days ───────────────────────────────────────
STRATEGY_HOLD_DAYS: dict[str, int] = {
    "strong_momentum":    3,
    "trend_up":           5,
    "breakout_near_high": 5,
    "high_tight_range":   5,
    "active_liquidity":   3,
    "sector_rotation":    7,
    "volume_surge":       5,
    "turnover_quality":   7,   # medium turnover = quality hold
    "intraday_bull_body": 3,
    "low_volatility":     7,   # stable stocks: longer horizon
    "reversal_dip":       5,   # buy-the-dip: wait for recovery
    "52w_middle":         7,   # mid-range in 52w band
}

# ── Factor metadata for UI / learning loop ───────────────────────────────────
FACTOR_META: dict[str, dict[str, Any]] = {
    "momentum":     {"label": "动量",   "direction": +1, "description": "当日涨跌幅强度"},
    "activity":     {"label": "活跃",   "direction": +1, "description": "对数成交额（流动性）"},
    "near_high":    {"label": "逼高",   "direction": +1, "description": "价格逼近日内高点"},
    "sector":       {"label": "板块",   "direction": +1, "description": "行业资金流入分位"},
    "volume_ratio": {"label": "量比",   "direction": +1, "description": "量比（放量确认）"},
    "volatility":   {"label": "低波动", "direction": -1, "description": "低振幅 = 低波动率（越低越好）"},
}

# ── Scoring profiles (six factors) ───────────────────────────────────────────
_PROFILE_WEIGHTS: dict[str, dict[str, Any]] = {
    # 稳健：兼顾上涨概率与可交易性（默认）
    "ashare_robust": {
        "weights": {"momentum": 0.18, "activity": 0.22, "near_high": 0.10,
                    "sector": 0.20, "volume_ratio": 0.12, "volatility": 0.18},
        "objective_weights": {"return": 0.40, "quality": 0.30, "tradability": 0.30},
        "momentum_direction": +1,
    },
    # 均衡：价量质综合
    "ashare_balanced": {
        "weights": {"momentum": 0.25, "activity": 0.20, "near_high": 0.15,
                    "sector": 0.20, "volume_ratio": 0.10, "volatility": 0.10},
        "objective_weights": {"return": 0.45, "quality": 0.25, "tradability": 0.30},
        "momentum_direction": +1,
    },
    # 进攻：强调涨幅与放量
    "ashare_aggressive": {
        "weights": {"momentum": 0.35, "activity": 0.20, "near_high": 0.15,
                    "sector": 0.15, "volume_ratio": 0.15, "volatility": 0.00},
        "objective_weights": {"return": 0.62, "quality": 0.10, "tradability": 0.28},
        "momentum_direction": +1,
    },
    # 行业轮动：板块优先，其次量质
    "sector_rotation": {
        "weights": {"momentum": 0.15, "activity": 0.15, "near_high": 0.10,
                    "sector": 0.40, "volume_ratio": 0.10, "volatility": 0.10},
        "objective_weights": {"return": 0.45, "quality": 0.20, "tradability": 0.35},
        "momentum_direction": +1,
    },
    # 突破：量比 + 逼近日高双重确认
    "breakout": {
        "weights": {"momentum": 0.20, "activity": 0.10, "near_high": 0.30,
                    "sector": 0.10, "volume_ratio": 0.25, "volatility": 0.05},
        "objective_weights": {"return": 0.58, "quality": 0.12, "tradability": 0.30},
        "momentum_direction": +1,
    },
    # 缩量反转：买回调中的低波动股（研究支持最强）
    "reversal": {
        "weights": {"momentum": 0.20, "activity": 0.10, "near_high": 0.05,
                    "sector": 0.30, "volume_ratio": 0.05, "volatility": 0.30},
        "objective_weights": {"return": 0.40, "quality": 0.38, "tradability": 0.22},
        "momentum_direction": -1,   # 负向: 近期弱势股得高分
    },
    # 低波动价值：2024年最佳因子组合
    "low_volatility_value": {
        "weights": {"momentum": 0.05, "activity": 0.15, "near_high": 0.10,
                    "sector": 0.25, "volume_ratio": 0.05, "volatility": 0.40},
        "objective_weights": {"return": 0.28, "quality": 0.48, "tradability": 0.24},
        "momentum_direction": +1,
    },
    # 美股
    "us_balanced": {
        "weights": {"momentum": 0.35, "activity": 0.25, "near_high": 0.20,
                    "sector": 0.00, "volume_ratio": 0.15, "volatility": 0.05},
        "objective_weights": {"return": 0.55, "quality": 0.15, "tradability": 0.30},
        "momentum_direction": +1,
    },
}


@dataclass
class MarketScanCandidate:
    symbol: str
    name: str
    score: float
    reasons: list[str]
    strategy_hits: list[str]
    risk_flags: list[str]
    score_breakdown: dict[str, float]
    quote: dict[str, Any]
    sector: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "code": self.symbol,
            "name": self.name,
            "score": self.score,
            "reasons": self.reasons,
            "strategy_hits": self.strategy_hits,
            "risk_flags": self.risk_flags,
            "score_breakdown": self.score_breakdown,
            "sector": self.sector,
            "live_price": _to_float(self.quote.get("price")),
            "price_change_pct": _to_float(self.quote.get("change_pct")),
            "day_high": _to_float(self.quote.get("high")),
            "day_open": _to_float(self.quote.get("open")),
            "day_low": _to_float(self.quote.get("low")),
            "amplitude": _to_float(self.quote.get("amplitude")),
            "amount": _to_float(self.quote.get("amount")),
            "volume": _to_float(self.quote.get("volume")),
            "volume_ratio": _to_float(self.quote.get("volume_ratio")),
            "turnover_rate": _to_float(self.quote.get("turnover_rate")),
            "week52_high": _to_float(self.quote.get("week52_high")),
            "week52_low": _to_float(self.quote.get("week52_low")),
            "quote_time": self.quote.get("quote_time"),
            "quote_source": self.quote.get("source"),
        }


def resolve_weights(profile: str) -> dict[str, float]:
    cfg = _PROFILE_WEIGHTS.get(profile) or _PROFILE_WEIGHTS["ashare_balanced"]
    base = dict(cfg["weights"])
    total = sum(base.values())
    if total <= 0:
        return base
    return {k: round(v / total, 4) for k, v in base.items()}


def list_profiles() -> list[dict[str, Any]]:
    return [
        {"id": "ashare_robust",        "name": "A股稳健",    "description": "可买入优先的稳健档位，默认推荐使用"},
        {"id": "ashare_balanced",      "name": "A股均衡",    "description": "六因子均衡打分，适合日常使用"},
        {"id": "ashare_aggressive",    "name": "A股进攻",    "description": "强动量+放量优先，适合强势行情"},
        {"id": "sector_rotation",      "name": "行业轮动",   "description": "板块资金流向主导，跟随主力热点"},
        {"id": "breakout",             "name": "突破策略",   "description": "量比+逼近日高双重确认，捕捉突破"},
        {"id": "reversal",             "name": "回调买入",   "description": "买近期弱势低波动票，实证ICIR领先"},
        {"id": "low_volatility_value", "name": "低波动价值", "description": "2024年最佳因子组合，低振幅+强板块"},
        {"id": "us_balanced",          "name": "美股均衡",   "description": "适用于美股，动量主导"},
    ]


def scan_market_candidates(
    *,
    top_k: int,
    min_change_pct: float,
    profile: str | None = None,
    momentum_weight: float | None = None,
    activity_weight: float | None = None,
    near_high_weight: float | None = None,
    scan_limit: int | None = None,
    min_price: float = 2.0,
    max_price: float = 10000.0,
    min_amount: float = 2e8,
    min_turnover_rate: float = 0.8,
    min_volume_ratio: float = 0.6,
    limit_up_threshold_pct: float = 9.6,
    limit_down_threshold_pct: float = -9.6,
    enforce_tradability: bool = True,
    enable_sector_filter: bool = True,
) -> dict[str, Any]:
    """Full-market A-share scan — six-factor model with evidence-based tuning.

    Factors:
      1. momentum      — 当日涨跌幅（方向依 profile，reversal 取负）
      2. activity      — 对数成交额（流动性）
      3. near_high     — 价格逼近日内高点
      4. sector        — 行业资金流入分位
      5. volume_ratio  — 量比（放量有效性确认）
      6. volatility    — 低振幅代理低波动（越低越好，负方向）

    Turnover:
      使用钟形曲线评分：3-6%为最优；过低（<1%）流动性差；过高（>12%）热钱风险。
      不再简单正向加分，修复了之前高换手=好的方向性错误。
    """
    top_k = max(1, min(int(top_k), 20))
    min_change_pct = float(min_change_pct)
    resolved_profile = profile or "ashare_robust"

    profile_cfg = _PROFILE_WEIGHTS.get(resolved_profile) or _PROFILE_WEIGHTS["ashare_balanced"]
    weights = _resolve_weights(profile_cfg, momentum_weight, activity_weight, near_high_weight)
    objective_weights = _resolve_objective_weights(profile_cfg)
    momentum_direction = int(profile_cfg.get("momentum_direction", +1))

    universe = _load_cn_universe()
    symbols = list(universe.keys())
    if scan_limit is not None and int(scan_limit) > 0:
        symbols = symbols[: max(top_k, int(scan_limit))]

    # ── Layer 1: enhanced real-time spot data ─────────────────────────────────
    quotes = _fetch_enhanced_spot_data(symbols)

    # ── Layer 2: sector strength map ─────────────────────────────────────────
    sector_score_map: dict[str, float] = {}
    sector_membership: dict[str, str] = {}
    if enable_sector_filter and weights.get("sector", 0.0) > 0.0:
        sector_score_map, sector_membership = _load_sector_context()

    ranked: list[MarketScanCandidate] = []

    for symbol in symbols:
        quote = quotes.get(symbol) or {}
        price = _to_float(quote.get("price"))
        change_pct = _to_float(quote.get("change_pct"))
        day_high = _to_float(quote.get("high"))
        day_open = _to_float(quote.get("open"))
        day_low = _to_float(quote.get("low"))
        amount = _to_float(quote.get("amount"))
        volume = _to_float(quote.get("volume"))
        volume_ratio = _to_float(quote.get("volume_ratio"))
        turnover_rate = _to_float(quote.get("turnover_rate"))
        amplitude = _to_float(quote.get("amplitude"))  # 振幅 %
        week52_high = _to_float(quote.get("week52_high"))
        week52_low = _to_float(quote.get("week52_low"))

        # ── Hard filters ──────────────────────────────────────────────────────
        if price is None or change_pct is None or amount is None:
            continue
        if price < min_price or price > max_price:
            continue
        if amount < min_amount:
            continue
        if enforce_tradability and change_pct >= float(limit_up_threshold_pct):
            continue
        if enforce_tradability and change_pct <= float(limit_down_threshold_pct):
            continue
        if enforce_tradability and turnover_rate is not None and turnover_rate < float(min_turnover_rate):
            continue
        if enforce_tradability and volume_ratio is not None and volume_ratio < float(min_volume_ratio):
            continue
        # For reversal profile, we want to scan negative change_pct stocks too
        effective_min_change = -10.0 if momentum_direction < 0 else min_change_pct
        if change_pct < effective_min_change:
            continue

        # ── Derived signals ───────────────────────────────────────────────────
        near_high_dist: float | None = None
        if price and day_high and day_high > 0:
            near_high_dist = abs(day_high - price) / day_high

        intraday_gain: float | None = None
        if price and day_open and day_open > 0:
            intraday_gain = (price - day_open) / day_open * 100.0

        week52_position: float | None = None
        if week52_high and week52_low and (week52_high - week52_low) > 0 and price:
            week52_position = (price - week52_low) / (week52_high - week52_low)

        # ── Factor 1: Momentum (direction is profile-dependent) ───────────────
        if momentum_direction >= 0:
            momentum_score = _normalize_to_100(change_pct, low=-3.0, high=9.0)
        else:
            # Reversal: stocks DOWN 5% score 100; stocks UP 2% score 0
            momentum_score = _normalize_to_100(-change_pct, low=-2.0, high=5.0)

        # ── Factor 2: Activity (log-scaled amount) ────────────────────────────
        activity_score = max(0.0, min(100.0, (math.log10(amount + 1.0) - 7.0) * 26.0))

        # ── Factor 3: Near intraday high ──────────────────────────────────────
        near_high_score = 0.0
        if near_high_dist is not None:
            near_high_score = max(0.0, 100.0 - near_high_dist * 900.0)

        # ── Factor 4: Sector strength (0-100 from fund-flow rank) ─────────────
        symbol_sector = sector_membership.get(symbol)
        sector_raw_score = sector_score_map.get(symbol_sector, 0.0) if symbol_sector else 0.0
        sector_score = sector_raw_score * 100.0

        # ── Factor 5: Volume ratio ─────────────────────────────────────────────
        vr_score = 0.0
        if volume_ratio is not None and volume_ratio > 0:
            vr_score = _normalize_to_100(volume_ratio, low=0.5, high=3.0)

        # ── Factor 6: Low volatility (amplitude as proxy) ─────────────────────
        # Lower amplitude → higher score.  Amplitude 0%→100, 10%→0.
        volatility_score = 0.0
        if amplitude is not None and amplitude >= 0:
            volatility_score = max(0.0, 100.0 - amplitude * 10.0)
        elif day_high and day_low and day_high > 0:
            # Fallback: compute from high/low if amplitude not available
            intraday_range_pct = (day_high - day_low) / day_high * 100.0
            volatility_score = max(0.0, 100.0 - intraday_range_pct * 10.0)

        # ── Turnover quality: bell-curve score (peak at 4%, punish extremes) ──
        turnover_quality = 0.0
        if turnover_rate is not None and turnover_rate > 0:
            turnover_quality = _bell_score(turnover_rate, peak=4.0, width=3.5)

        # ── Composite base factor score ───────────────────────────────────────
        factor_score = (
            momentum_score  * weights.get("momentum",     0.25)
            + activity_score  * weights.get("activity",     0.20)
            + near_high_score * weights.get("near_high",    0.15)
            + sector_score    * weights.get("sector",       0.20)
            + vr_score        * weights.get("volume_ratio", 0.10)
            + volatility_score* weights.get("volatility",   0.10)
        )
        liquidity_amount_score = max(0.0, min(100.0, (math.log10(amount + 1.0) - 7.5) * 33.0))
        turnover_trade_score = 0.0
        if turnover_rate is not None and turnover_rate > 0:
            turnover_trade_score = _normalize_to_100(turnover_rate, low=0.8, high=8.0)
        tradability_score = (
            liquidity_amount_score * 0.45
            + turnover_trade_score * 0.30
            + vr_score * 0.25
        )
        return_score = (
            momentum_score * 0.40
            + near_high_score * 0.20
            + sector_score * 0.20
            + vr_score * 0.20
        )
        quality_score = (
            volatility_score * 0.50
            + turnover_quality * 0.30
            + sector_score * 0.20
        )
        score = (
            return_score * objective_weights["return"]
            + quality_score * objective_weights["quality"]
            + tradability_score * objective_weights["tradability"]
        )

        # ── Strategy hits, bonuses, penalties ────────────────────────────────
        strategy_hits: list[str] = []
        reasons: list[str] = []
        risk_flags: list[str] = []
        bonus = 0.0
        penalty = 0.0

        # Momentum signals
        if momentum_direction >= 0:
            if change_pct >= 5.0:
                strategy_hits.append("strong_momentum")
                reasons.append(f"涨幅强势（{change_pct:+.2f}%）")
            elif change_pct >= 2.0:
                strategy_hits.append("trend_up")
                reasons.append(f"趋势上行（{change_pct:+.2f}%）")
            elif change_pct >= 0.0:
                reasons.append(f"小幅上涨（{change_pct:+.2f}%）")
            else:
                reasons.append(f"微幅震荡（{change_pct:+.2f}%）")
        else:
            # Reversal profile: negative change_pct is the signal
            if change_pct <= -3.0:
                strategy_hits.append("reversal_dip")
                reasons.append(f"明显回调，具备反转潜力（{change_pct:+.2f}%）")
            elif change_pct <= -1.0:
                strategy_hits.append("reversal_dip")
                reasons.append(f"回调中（{change_pct:+.2f}%）")

        # Near intraday high
        if near_high_dist is not None and near_high_dist <= 0.012:
            bonus += 8.0
            strategy_hits.append("breakout_near_high")
            reasons.append("价格逼近日内高点")
        elif near_high_dist is not None and near_high_dist <= 0.025:
            strategy_hits.append("high_tight_range")

        # Activity
        if amount >= 1e9:
            strategy_hits.append("active_liquidity")
            reasons.append("成交额活跃（≥10亿）")
        elif amount >= 4e8:
            reasons.append("成交额较活跃")

        # Volume ratio —放量确认
        if volume_ratio is not None and volume_ratio >= 2.5:
            strategy_hits.append("volume_surge")
            bonus += 7.0
            reasons.append(f"明显放量（量比{volume_ratio:.1f}）")
        elif volume_ratio is not None and volume_ratio >= 1.5:
            strategy_hits.append("volume_surge")
            bonus += 3.0

        # Intraday bullish body
        if intraday_gain is not None and intraday_gain >= 1.5:
            strategy_hits.append("intraday_bull_body")
            bonus += 4.0

        # FIXED: Turnover quality bonus (bell curve; old code incorrectly gave +3 for ANY high turnover)
        if turnover_rate is not None:
            if 2.0 <= turnover_rate <= 8.0:
                strategy_hits.append("turnover_quality")
                bonus += round(turnover_quality * 0.08, 1)  # max ~+8
                if 3.0 <= turnover_rate <= 6.0:
                    reasons.append(f"换手率健康（{turnover_rate:.1f}%）")
            elif turnover_rate < 0.5:
                penalty += 5.0
                risk_flags.append("illiquid_turnover")
            elif turnover_rate > 15.0:
                penalty += 6.0
                risk_flags.append("hot_money_risk")
            elif turnover_rate > 10.0:
                penalty += 3.0
                risk_flags.append("elevated_turnover")

        # Low volatility bonus
        if amplitude is not None:
            if amplitude < 3.0:
                strategy_hits.append("low_volatility")
                bonus += 5.0
                reasons.append(f"低振幅稳定（{amplitude:.1f}%）")
            elif amplitude > 8.0:
                penalty += 4.0
                risk_flags.append("high_volatility")

        # Sector rotation bonus
        if symbol_sector and sector_raw_score >= 0.70:
            strategy_hits.append("sector_rotation")
            bonus += 7.0
            reasons.append(f"板块资金流强（{symbol_sector}）")
        elif symbol_sector and sector_raw_score >= 0.50:
            bonus += 3.0

        # 52-week position
        if week52_position is not None:
            if week52_position >= 0.85:
                strategy_hits.append("52w_near_high")
                bonus += 4.0
                reasons.append("接近52周高点")
            elif 0.30 <= week52_position <= 0.60:
                strategy_hits.append("52w_middle")
                bonus += 2.0  # recovery zone

        # ── Risk penalties ────────────────────────────────────────────────────
        if momentum_direction >= 0 and change_pct < 0.3:
            penalty += 5.0
            risk_flags.append("weak_momentum")
        if change_pct >= (limit_up_threshold_pct - 0.6):
            penalty += 6.0
            risk_flags.append("near_limit_up")
        if change_pct <= (limit_down_threshold_pct + 0.6):
            penalty += 4.0
            risk_flags.append("near_limit_down")
        if near_high_dist is not None and near_high_dist > 0.06 and momentum_direction >= 0:
            penalty += 4.0
            risk_flags.append("far_from_intraday_high")
        if amount < 3e8:
            penalty += 4.0
            risk_flags.append("borderline_liquidity")
        if volume_ratio is not None and volume_ratio < 0.5:
            penalty += 3.0
            risk_flags.append("volume_dry_up")

        final_score = round(score * 100.0 + bonus - penalty, 2)

        ranked.append(
            MarketScanCandidate(
                symbol=symbol,
                name=universe.get(symbol) or symbol,
                score=final_score,
                reasons=reasons[:4],
                strategy_hits=strategy_hits[:6],
                risk_flags=risk_flags[:4],
                score_breakdown={
                    "momentum":     round(momentum_score,  2),
                    "activity":     round(activity_score,  2),
                    "near_high":    round(near_high_score, 2),
                    "sector":       round(sector_score,    2),
                    "volume_ratio": round(vr_score,        2),
                    "volatility":   round(volatility_score,2),
                    "turnover_q":   round(turnover_quality,2),
                    "return_score": round(return_score,   2),
                    "quality_score": round(quality_score, 2),
                    "tradability_score": round(tradability_score, 2),
                    "factor_score": round(factor_score, 2),
                    "bonus":        round(bonus,           2),
                    "penalty":      round(penalty,         2),
                },
                quote=quote,
                sector=symbol_sector,
            )
        )

    ranked.sort(key=lambda x: x.score, reverse=True)
    return {
        "pool_size": len(symbols),
        "scored_size": len(ranked),
        "items": [x.to_dict() for x in ranked[:top_k]],
        "scoring_model": {
            "market": "cn",
            "profile": resolved_profile,
            "weights": weights,
            "objective_weights": objective_weights,
            "momentum_direction": momentum_direction,
            "engine": "market_scan_v3",
            "factors": list(FACTOR_META.keys()),
            "filters": {
                "min_change_pct": min_change_pct,
                "scan_limit": None if scan_limit in (None, 0) else int(scan_limit),
                "min_price": min_price,
                "max_price": max_price,
                "min_amount": min_amount,
                "min_turnover_rate": min_turnover_rate,
                "min_volume_ratio": min_volume_ratio,
                "limit_up_threshold_pct": limit_up_threshold_pct,
                "limit_down_threshold_pct": limit_down_threshold_pct,
                "enforce_tradability": bool(enforce_tradability),
            },
        },
    }


# ── Weight resolution ─────────────────────────────────────────────────────────

def _resolve_weights(
    profile_cfg: dict[str, Any],
    momentum_override: float | None,
    activity_override: float | None,
    near_high_override: float | None,
) -> dict[str, float]:
    base = dict(profile_cfg.get("weights") or {})
    if any(v is not None for v in (momentum_override, activity_override, near_high_override)):
        if momentum_override is not None:
            base["momentum"]  = max(0.0, min(10.0, float(momentum_override)))
        if activity_override is not None:
            base["activity"]  = max(0.0, min(10.0, float(activity_override)))
        if near_high_override is not None:
            base["near_high"] = max(0.0, min(10.0, float(near_high_override)))
    total = sum(base.values())
    if total <= 0:
        return base
    return {k: round(v / total, 4) for k, v in base.items()}


def _resolve_objective_weights(profile_cfg: dict[str, Any]) -> dict[str, float]:
    base = dict(profile_cfg.get("objective_weights") or {})
    if not base:
        base = {"return": 0.45, "quality": 0.25, "tradability": 0.30}
    for key in ("return", "quality", "tradability"):
        base[key] = max(0.0, float(base.get(key, 0.0)))
    total = sum(base.values())
    if total <= 0:
        return {"return": 0.45, "quality": 0.25, "tradability": 0.30}
    return {k: round(v / total, 4) for k, v in base.items()}


# ── Sector context (board fund-flow) ──────────────────────────────────────────

def _load_sector_context(*, force: bool = False) -> tuple[dict[str, float], dict[str, str]]:
    global _SECTOR_CACHE, _SECTOR_MEMBERSHIP_CACHE, _SECTOR_CACHE_TS
    now = time.time()
    if (
        not force
        and _SECTOR_CACHE is not None
        and _SECTOR_MEMBERSHIP_CACHE is not None
        and (now - _SECTOR_CACHE_TS) < _SECTOR_CACHE_TTL
    ):
        return dict(_SECTOR_CACHE), dict(_SECTOR_MEMBERSHIP_CACHE)

    score_map: dict[str, float] = {}
    membership: dict[str, str] = {}

    try:
        import akshare as ak
        from tradingagents.dataflows.providers.cn_akshare_provider import fetch_board_fund_flow_df

        flow_df = fetch_board_fund_flow_df(ak_module=ak)
        if flow_df is not None and not flow_df.empty:
            sort_col = next(
                (c for c in flow_df.columns if "主力" in c and "净" in c and "额" in c),
                None,
            )
            if sort_col:
                flow_df = flow_df.sort_values(sort_col, ascending=False).reset_index(drop=True)
            n = len(flow_df)
            name_col = next((c for c in flow_df.columns if "板块" in c or "名称" in c), None)
            if name_col is None and len(flow_df.columns) > 0:
                name_col = flow_df.columns[0]
            if name_col:
                for idx, row in flow_df.iterrows():
                    sector_name = str(row.get(name_col, "")).strip()
                    if not sector_name:
                        continue
                    rank_pct = 1.0 - (int(idx) / max(1, n - 1))
                    score_map[sector_name] = round(rank_pct, 4)

        if score_map:
            top_sectors = [s for s, sc in score_map.items() if sc >= 0.70][:8]
            for sector in top_sectors:
                try:
                    cons_df = ak.stock_board_industry_cons_em(symbol=sector)
                    if cons_df is None or cons_df.empty:
                        continue
                    code_col = next((c for c in cons_df.columns if "代码" in c or "code" in c.lower()), None)
                    if not code_col and len(cons_df.columns) > 0:
                        code_col = cons_df.columns[0]
                    if not code_col:
                        continue
                    for _, row in cons_df.iterrows():
                        raw = str(row.get(code_col, "")).strip()
                        sym = daily_stock_analysis_service.normalize_symbol(raw)
                        if sym and sym not in membership:
                            membership[sym] = sector
                except Exception:
                    continue
    except Exception as exc:
        logger.debug("[Scanner] sector context fetch failed: %s", exc)

    _SECTOR_CACHE = score_map
    _SECTOR_MEMBERSHIP_CACHE = membership
    _SECTOR_CACHE_TS = now
    return dict(score_map), dict(membership)


# ── Enhanced spot data ────────────────────────────────────────────────────────

def _fetch_enhanced_spot_data(symbols: list[str]) -> dict[str, dict[str, Any]]:
    global _SPOT_ENHANCED_CACHE, _SPOT_ENHANCED_TS
    now = time.time()
    if _SPOT_ENHANCED_CACHE is not None and (now - _SPOT_ENHANCED_TS) < _SPOT_ENHANCED_TTL:
        base = _SPOT_ENHANCED_CACHE
    else:
        base = _try_load_full_spot_data()
        if base:
            _SPOT_ENHANCED_CACHE = base
            _SPOT_ENHANCED_TS = now

    missing = [s for s in symbols if s not in base]
    if missing:
        basic = _fetch_live_quotes_chunked(missing)
        merged = dict(base)
        merged.update(basic)
        return {s: merged[s] for s in symbols if s in merged}
    return {s: base[s] for s in symbols if s in base}


def _try_load_full_spot_data() -> dict[str, dict[str, Any]]:
    """Parse stock_zh_a_spot_em() enriching with amplitude, 52-week band."""
    result: dict[str, dict[str, Any]] = {}
    try:
        import akshare as ak
        from api.services.daily_stock_analysis_service import normalize_symbol

        df = ak.stock_zh_a_spot_em()
        if df is None or df.empty:
            return result

        for _, row in df.iterrows():
            code = str(row.get("代码", "")).strip()
            sym = normalize_symbol(code)
            if not sym:
                continue
            price = _safe_float(row.get("最新价"))
            prev_close = _safe_float(row.get("昨收"))
            change = None
            change_pct = None
            if price is not None and prev_close and prev_close > 0:
                change = round(price - prev_close, 4)
                change_pct = round(change / prev_close * 100.0, 4)
            result[sym] = {
                "price": price,
                "open": _safe_float(row.get("今开")),
                "high": _safe_float(row.get("最高")),
                "low": _safe_float(row.get("最低")),
                "previous_close": prev_close,
                "change": change,
                "change_pct": change_pct,
                "volume": _safe_float(row.get("成交量")),
                "amount": _safe_float(row.get("成交额")),
                "volume_ratio": _safe_float(row.get("量比")),
                "turnover_rate": _safe_float(row.get("换手率")),
                "amplitude": _safe_float(row.get("振幅")),         # 振幅 %
                "week52_high": _safe_float(row.get("52周最高")),    # 52周最高
                "week52_low": _safe_float(row.get("52周最低")),     # 52周最低
                "source": "eastmoney_spot",
            }
    except Exception as exc:
        logger.debug("[Scanner] full spot fetch failed: %s", exc)
    return result


# ── Universe ──────────────────────────────────────────────────────────────────

def _load_cn_universe() -> dict[str, str]:
    global _CN_UNIVERSE_CACHE, _CN_UNIVERSE_LOADED_AT
    now = time.time()
    if _CN_UNIVERSE_CACHE is not None and (now - _CN_UNIVERSE_LOADED_AT) <= _CN_UNIVERSE_TTL:
        return dict(_CN_UNIVERSE_CACHE)

    result: dict[str, str] = {}
    try:
        import akshare as ak
        df = ak.stock_info_a_code_name()
        for _, row in df.iterrows():
            raw_code = str(row.get("code") or "").strip()
            raw_name = str(row.get("name") or "").strip()
            symbol = daily_stock_analysis_service.normalize_symbol(raw_code)
            if not symbol or not raw_name:
                continue
            if _is_excluded_name(raw_name):
                continue
            result[symbol] = raw_name
    except Exception:
        result = {}

    _CN_UNIVERSE_CACHE = result
    _CN_UNIVERSE_LOADED_AT = now
    return dict(result)


def _is_excluded_name(name: str) -> bool:
    text = str(name or "").strip().upper()
    if not text:
        return True
    return "ST" in text or "退" in text


def _fetch_live_quotes_chunked(symbols: list[str], chunk_size: int = 180) -> dict[str, dict[str, Any]]:
    import json
    merged: dict[str, dict[str, Any]] = {}
    if not symbols:
        return merged
    for idx in range(0, len(symbols), chunk_size):
        batch = symbols[idx : idx + chunk_size]
        try:
            payload = route_to_vendor("get_realtime_quotes", batch)
            data = json.loads(payload)
            if isinstance(data, dict):
                merged.update(data)
        except Exception:
            continue
    return merged


# ── Math helpers ──────────────────────────────────────────────────────────────

def _normalize_to_100(value: float, *, low: float, high: float) -> float:
    clipped = max(low, min(high, value))
    return (clipped - low) / max(1e-6, high - low) * 100.0


def _bell_score(value: float, *, peak: float, width: float) -> float:
    """Gaussian-like score: maximum (100) at peak, falls toward 0 as value deviates.

    Used for turnover_rate: optimal zone ~3-6%, punish both extremes.
    """
    return 100.0 * math.exp(-0.5 * ((value - peak) / max(width, 1e-6)) ** 2)


def _safe_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        import pandas as pd
        f = float(val)
        if pd.isna(f):
            return None
        return f
    except Exception:
        return None


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), 4)
    except Exception:
        return None
