"""市场主线规则层（M1）：板块硬过滤、双维度评分、情绪温度 gating。

设计原则（见 docs/mainline-market-design.md v1.1）：
- 主线 ≠ 当日涨幅榜：用 超额收益 + 5/20 日斜率 + 资金流持续性 + 量能 判定中期确定性；
- heat_score（短期情绪）+ strength_score（中期确定性）双维度，阶段由组合判定（M3 LLM 层使用）；
- 先规则后 LLM：本模块只做确定性计算，产出候选板块特征表，LLM 层负责归并/叙事/风险；
- 按需拉取：先榜单后 top N，避免对全部板块拉历史/成分股触发反爬。

本模块不调用 LLM，所有 fetch 依赖可注入（便于单测 mock）。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import Callable, Optional

import numpy as np
import pandas as pd

# ── 可注入的取数依赖签名 ─────────────────────────────────────────
# sector_type: "industry" | "concept"
SpotFetcher = Callable[[str], pd.DataFrame]              # -> 板块涨幅榜(已归一化)
HistFetcher = Callable[[str, str], pd.DataFrame]         # (board_name, sector_type) -> 板块历史
FlowFetcher = Callable[[str, str], pd.DataFrame]         # (sector_type, period) -> 资金流排行
ZtFetcher = Callable[[str], pd.DataFrame]                # (date) -> 涨停池
IndexFetcher = Callable[[], pd.DataFrame]                # () -> 基准指数历史(沪深300)

# 双维度评分在 composite 中的权重（按视角）
_PERSPECTIVE_WEIGHTS = {
    "short": {"heat": 0.60, "strength": 0.40},   # 短线题材：情绪为主
    "medium": {"heat": 0.35, "strength": 0.65},  # 中期行业：确定性为主
}

# 情绪温度 regime 阈值
_EMOTION_REGIMES = [
    (25, "冰点"),
    (45, "低迷"),
    (65, "中性"),
    (85, "活跃"),
    (101, "亢奋"),
]


@dataclass
class BoardFeatures:
    """单个板块的规则层特征与评分（v2：金融理论驱动的多周期动量结构）。"""

    sector_type: str
    code: str
    name: str
    # 基础行情
    chg_1d: float
    chg_5d: Optional[float]
    chg_20d: Optional[float]
    turnover: float
    up_ratio: Optional[float]
    leader: str
    leader_chg: Optional[float]
    # 相对强弱（超额收益）
    excess_ret_1d: float            # chg_1d - 市场板块中位数涨幅
    excess_ret_5d: Optional[float]  # chg_5d - 基准 5 日涨幅
    rs20: Optional[float]           # 20 日超额收益（相对强弱，Levy 1967）— 主线核心指标
    # 趋势与量能
    slope_5d: Optional[float]       # 近5日收盘线性斜率（%/日）
    slope_20: Optional[float]       # 近20日收盘线性斜率（%/日）— 趋势强度
    ma_bullish: bool                # MA20 > MA60 多头排列（道氏趋势确认）
    new_high_gap: Optional[float]   # 收盘距 20 日高点距离（0=新高，负=回落）
    rsi14: Optional[float]          # 14 日 RSI（拥挤度，行为金融）
    turnover_slope: Optional[float]  # 近5日换手均值 / 前15日换手均值
    turnover_slope20: Optional[float]  # 近20日换手均值 / 前20日换手均值（量能确认）
    # 资金流（亿元）
    net_inflow_1d: Optional[float]
    net_inflow_5d: Optional[float]
    net_inflow_10d: Optional[float]
    inflow_persistent: bool         # 今日与5日均为净流入
    inflow_persistent_10: bool      # 5日与10日均为净流入（资金合力，双周期确认）
    flow_data_missing: bool         # 资金流数据不可用（硬门槛豁免标记）
    # 评分
    heat_score: float               # 短期情绪 0-100
    strength_score: float           # 中期确定性 0-100
    composite_score: float          # 按视角加权
    hist_available: bool            # 是否拉取到历史（决定 strength 的可信度）
    # v2 硬门槛与阶段提示（规则层判定，LLM 层最终确认）
    passes_gate: bool               # 是否通过真主线硬门槛
    pulse: bool                     # 短期脉冲（heat 高但未过门槛）
    phase_hint: str                 # 发酵 | 主升 | 高位分歧 | 退潮 | 脉冲 | 数据不足

    def to_dict(self) -> dict:
        return asdict(self)


# ── 工具函数 ─────────────────────────────────────────────────────

def _pct(series: pd.Series) -> pd.Series:
    """0-1 分位（缺失值单独处理，不参与排名，返回 NaN）。"""
    valid = series.dropna()
    if valid.empty:
        return pd.Series(np.nan, index=series.index)
    ranks = series.rank(pct=True, na_option="keep")
    return ranks


def _rsi(closes: pd.Series, period: int = 14) -> Optional[float]:
    """Wilder RSI（相对强弱指标，衡量拥挤度）。边界：全涨→100，全跌→0。"""
    c = closes.dropna().astype(float)
    if len(c) < period + 2:
        return None
    delta = c.diff()
    gain = delta.clip(lower=0).ewm(alpha=1.0 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1.0 / period, adjust=False).mean()
    g, l = float(gain.iloc[-1]), float(loss.iloc[-1])
    if l == 0 and g > 0:
        return 100.0
    if g == 0 and l > 0:
        return 0.0
    if l == 0:
        return None
    rs = g / l
    return float(100 - 100 / (1 + rs))


def _clip01(x: float | None) -> float:
    if x is None or np.isnan(x):
        return 0.0
    return float(min(max(x, 0.0), 1.0))


def _linear_slope_pct(closes: pd.Series) -> float | None:
    """近 N 日收盘价的线性斜率（%/日）。"""
    y = closes.dropna().astype(float).values
    if len(y) < 3:
        return None
    x = np.arange(len(y))
    slope = np.polyfit(x, y, 1)[0]
    base = float(np.mean(y))
    if base == 0:
        return None
    return slope / base * 100.0


def hard_filter_spot(spot: pd.DataFrame) -> pd.DataFrame:
    """板块涨幅榜硬过滤：剔除无效行/缺失涨跌幅，按名称去重。"""
    if spot is None or spot.empty:
        return pd.DataFrame()
    df = spot.copy()
    if "name" not in df.columns:
        return pd.DataFrame()
    df = df[df["name"].notna() & (df["name"].astype(str).str.strip() != "")]
    if "chg_1d" in df.columns:
        df = df[df["chg_1d"].notna()]
    if "code" in df.columns:
        df = df.drop_duplicates(subset=["code"], keep="first")
    else:
        df = df.drop_duplicates(subset=["name"], keep="first")
    return df.reset_index(drop=True)


def compute_emotion_temperature(zt_df: Optional[pd.DataFrame]) -> dict:
    """从涨停池计算市场情绪温度（0-100）与 regime，作为主线 gating。

    M1 近似口径（不依赖昨日涨停数据）：
      temperature = f(涨停家数, 最高连板, 连板>=3 家数)
    gate：冰点(<25) 或 亢奋(>85) 时标记"不追主线"或降级输出。
    """
    if zt_df is None or zt_df.empty:
        return {
            "temperature": None,
            "regime": "未知",
            "zt_count": 0,
            "max_lianban": 0,
            "lianban_ge3": 0,
            "gate": "normal",
            "gate_reason": "涨停池数据不可用，不做情绪 gating",
        }
    zt_count = int(len(zt_df))
    lianban_col = next((c for c in ("连板数", "连板", "连续涨停") if c in zt_df.columns), None)
    if lianban_col is None:
        max_lianban = 0
        lianban_ge3 = 0
    else:
        s = pd.to_numeric(zt_df[lianban_col], errors="coerce").dropna()
        max_lianban = int(s.max()) if not s.empty else 0
        lianban_ge3 = int((s >= 3).sum())
    temperature = round(
        20.0
        + min(zt_count, 120) * 0.5
        + min(max_lianban, 8) * 3.0
        + min(lianban_ge3, 10) * 1.5
    )
    temperature = int(min(temperature, 100))
    regime = next((label for th, label in _EMOTION_REGIMES if temperature < th), "亢奋")
    if temperature < 25:
        gate, gate_reason = "ice", "市场情绪冰点，主线缺失概率高，建议只输出观察方向不出股"
    elif temperature > 85:
        gate, gate_reason = "euphoria", "市场情绪亢奋，谨防高潮接盘，主线需更严格验证"
    else:
        gate, gate_reason = "normal", ""
    return {
        "temperature": temperature,
        "regime": regime,
        "zt_count": zt_count,
        "max_lianban": max_lianban,
        "lianban_ge3": lianban_ge3,
        "gate": gate,
        "gate_reason": gate_reason,
    }


def score_boards(
    spot: pd.DataFrame,
    *,
    sector_type: str,
    hist_map: dict[str, pd.DataFrame],
    flow_1d: Optional[pd.DataFrame],
    flow_5d: Optional[pd.DataFrame],
    flow_10d: Optional[pd.DataFrame] = None,
    benchmark_chg_5d: Optional[float] = None,
    benchmark_chg_20d: Optional[float] = None,
    market_median_chg_1d: float,
    perspective: str = "short",
) -> list[BoardFeatures]:
    """对一个板块池（行业或概念）做 v2 评分，返回按 composite 降序的 BoardFeatures。

    v2（金融理论版，见 docs 7.3）：
    - heat_score 组件：当日涨幅分位、上涨占比分位、换手分位、领涨股涨幅分位（短期情绪）
    - strength_score 组件（中期确定性）：
        30% RS20（20日超额，相对强弱 Levy） + 20% 20日斜率（趋势强度）
      + 15% 上涨占比（广度） + 15% 资金双周期持续（5日&10日净流入）
      + 10% 20日量能趋势 + 10% 距20日高点近度
    - 硬门槛（真主线候选资格）：RS20>0 & MA20>MA60 & 广度≥0.5 & RSI<85 & 资金双周期（数据缺失豁免）
    - phase_hint：发酵/主升/高位分歧/退潮/脉冲/数据不足（规则层判定，LLM 层最终确认）
    - 历史缺失的板块用当日信号回退填充，并标记 hist_available=False
    """
    if spot is None or spot.empty:
        return []
    weights = _PERSPECTIVE_WEIGHTS.get(perspective, _PERSPECTIVE_WEIGHTS["short"])
    df = hard_filter_spot(spot).copy()

    # ── 基础特征 ──
    df["excess_ret_1d"] = df.get("chg_1d", pd.Series(dtype=float)) - market_median_chg_1d
    if "up_count" in df.columns and "down_count" in df.columns:
        total = df["up_count"] + df["down_count"]
        df["up_ratio"] = df["up_count"] / total.replace(0, np.nan)
    else:
        df["up_ratio"] = np.nan

    # ── 历史特征（按需拉取的历史，v2 多周期窗口） ──
    df["chg_5d"] = np.nan
    df["chg_20d"] = np.nan
    df["slope_5d"] = np.nan
    df["slope_20"] = np.nan
    df["ma_bullish"] = False
    df["new_high_gap"] = np.nan
    df["rsi14"] = np.nan
    df["turnover_slope"] = np.nan
    df["turnover_slope20"] = np.nan
    df["hist_available"] = False
    for idx, row in df.iterrows():
        name = str(row["name"])
        hist = hist_map.get(name)
        if hist is None or hist.empty or "close" not in hist.columns:
            continue
        closes = pd.to_numeric(hist["close"], errors="coerce").dropna()
        if len(closes) < 6:
            continue
        df.at[idx, "hist_available"] = True
        df.at[idx, "chg_5d"] = closes.iloc[-1] / closes.iloc[-6] - 1
        if len(closes) >= 21:
            df.at[idx, "chg_20d"] = closes.iloc[-1] / closes.iloc[-21] - 1
        df.at[idx, "slope_5d"] = _linear_slope_pct(closes.tail(5))
        if len(closes) >= 21:
            df.at[idx, "slope_20"] = _linear_slope_pct(closes.tail(20))
        if len(closes) >= 60:
            ma20 = closes.rolling(20).mean().iloc[-1]
            ma60 = closes.rolling(60).mean().iloc[-1]
            if pd.notna(ma20) and pd.notna(ma60):
                df.at[idx, "ma_bullish"] = bool(ma20 > ma60)
        roll_max20 = closes.rolling(20).max()
        if pd.notna(roll_max20.iloc[-1]) and roll_max20.iloc[-1] > 0:
            df.at[idx, "new_high_gap"] = closes.iloc[-1] / roll_max20.iloc[-1] - 1
        df.at[idx, "rsi14"] = _rsi(closes, 14)
        if "turnover" in hist.columns:
            to = pd.to_numeric(hist["turnover"], errors="coerce").dropna()
            if len(to) >= 20:
                recent5 = to.tail(5).mean()
                prior15 = to.iloc[-20:-5].mean()
                if prior15 and prior15 > 0:
                    df.at[idx, "turnover_slope"] = recent5 / prior15
            if len(to) >= 40:
                recent20 = to.tail(20).mean()
                prior20 = to.iloc[-40:-20].mean()
                if prior20 and prior20 > 0:
                    df.at[idx, "turnover_slope20"] = recent20 / prior20

    # ── 资金流特征（双周期：1日/5日/10日） ──
    flow_by_name: dict[int, dict] = {1: {}, 5: {}, 10: {}}
    for period, src in ((1, flow_1d), (5, flow_5d), (10, flow_10d)):
        if src is not None and not src.empty and "name" in src.columns:
            flow_by_name[period] = dict(zip(src["name"].astype(str), src.get("net_inflow", pd.Series(dtype=float))))
    df["net_inflow_1d"] = df["name"].astype(str).map(flow_by_name[1])
    df["net_inflow_5d"] = df["name"].astype(str).map(flow_by_name[5])
    df["net_inflow_10d"] = df["name"].astype(str).map(flow_by_name[10])
    df["inflow_persistent"] = (
        pd.to_numeric(df["net_inflow_1d"], errors="coerce").fillna(0) > 0
    ) & (pd.to_numeric(df["net_inflow_5d"], errors="coerce").fillna(0) > 0)
    # 资金合力：5日与10日双周期净流入；数据整体缺失时豁免（flow_data_missing=True）
    n5 = pd.to_numeric(df["net_inflow_5d"], errors="coerce")
    n10 = pd.to_numeric(df["net_inflow_10d"], errors="coerce")
    df["flow_data_missing"] = n5.isna() & n10.isna()
    df["inflow_persistent_10"] = (n5.fillna(0) > 0) & (n10.fillna(0) > 0)

    # ── 相对强弱（5日/20日超额收益） ──
    if benchmark_chg_5d is not None:
        df["excess_ret_5d"] = df["chg_5d"] - benchmark_chg_5d
    else:
        df["excess_ret_5d"] = df["chg_5d"]
    if benchmark_chg_20d is not None:
        df["rs20"] = df["chg_20d"] - benchmark_chg_20d
    else:
        df["rs20"] = df["chg_20d"]

    # ── 双维度评分（池内分位归一化，v2 权重） ──
    pct_chg = _pct(df["chg_1d"]).fillna(0.5)
    pct_up = _pct(df["up_ratio"]).fillna(0.5)
    pct_turnover = _pct(df["turnover"]).fillna(0.5)
    pct_leader = _pct(df.get("leader_chg", pd.Series(np.nan, index=df.index))).fillna(0.5)
    # 历史缺失时用当日信号回退：rs20 <- excess_ret_1d, slope_20 <- chg_1d
    eff_rs20 = df["rs20"].where(df["hist_available"], df["excess_ret_1d"])
    eff_slope20 = df["slope_20"].where(df["hist_available"], df["chg_1d"])
    pct_rs20 = _pct(eff_rs20).fillna(0.5)
    pct_slope20 = _pct(eff_slope20).fillna(0.5)
    pct_vol20 = _pct(df["turnover_slope20"]).fillna(0.5)
    pct_newhigh = _pct(df["new_high_gap"]).fillna(0.5)
    turnover_slope_norm = df["turnover_slope"].map(
        lambda v: 0.5 if (v is None or pd.isna(v)) else _clip01((v - 0.8) / 0.6)
    ).fillna(0.5)

    df["heat_score"] = 100.0 * (
        0.50 * pct_chg + 0.25 * pct_up + 0.15 * pct_turnover + 0.10 * pct_leader
    )
    df["strength_score"] = 100.0 * (
        0.30 * pct_rs20
        + 0.20 * pct_slope20
        + 0.15 * pct_up
        + 0.15 * df["inflow_persistent_10"].astype(float)
        + 0.10 * pct_vol20
        + 0.10 * pct_newhigh
    )
    df["composite_score"] = weights["heat"] * df["heat_score"] + weights["strength"] * df["strength_score"]

    # ── v2 硬门槛 + 阶段提示（规则层判定，LLM 层最终确认） ──
    rs20_ok = df["rs20"].fillna(-np.inf) > 0
    ma_ok = df["ma_bullish"].fillna(False)
    breadth_ok = df["up_ratio"].fillna(0) >= 0.5
    rsi_ok = df["rsi14"].isna() | (df["rsi14"] < 85)
    flow_ok = df["inflow_persistent_10"] | df["flow_data_missing"]
    df["passes_gate"] = rs20_ok & ma_ok & breadth_ok & rsi_ok & flow_ok

    df["pulse"] = ~df["passes_gate"] & (df["heat_score"] >= 55)
    rsi_high = df["rsi14"].fillna(0) > 75

    def _phase_hint(row) -> str:
        if not row["hist_available"]:
            return "数据不足"
        if not row["passes_gate"]:
            return "脉冲" if row["pulse"] else "退潮"
        if rsi_high[row.name]:
            return "高位分歧"
        if row["heat_score"] > 60 and row["strength_score"] > 60:
            return "主升"
        if row["heat_score"] > 60:
            return "发酵"  # 热度先行、中期动量刚确认
        return "主升"  # 中期趋势强但情绪未过热（趋势中继）

    df["phase_hint"] = [""] * len(df)
    for idx, row in df.iterrows():
        df.at[idx, "phase_hint"] = _phase_hint(row)

    # ── 产出 ──
    out: list[BoardFeatures] = []
    for _, row in df.sort_values("composite_score", ascending=False).iterrows():
        out.append(
            BoardFeatures(
                sector_type=sector_type,
                code=str(row.get("code", "") or ""),
                name=str(row["name"]),
                chg_1d=float(row.get("chg_1d", 0) or 0),
                chg_5d=_opt_float(row.get("chg_5d")),
                chg_20d=_opt_float(row.get("chg_20d")),
                turnover=_opt_float(row.get("turnover")) or 0.0,
                up_ratio=_opt_float(row.get("up_ratio")),
                leader=str(row.get("leader", "") or ""),
                leader_chg=_opt_float(row.get("leader_chg")),
                excess_ret_1d=float(row.get("excess_ret_1d", 0) or 0),
                excess_ret_5d=_opt_float(row.get("excess_ret_5d")),
                rs20=_opt_float(row.get("rs20")),
                slope_5d=_opt_float(row.get("slope_5d")),
                slope_20=_opt_float(row.get("slope_20")),
                ma_bullish=bool(row.get("ma_bullish", False)),
                new_high_gap=_opt_float(row.get("new_high_gap")),
                rsi14=_opt_float(row.get("rsi14")),
                turnover_slope=_opt_float(row.get("turnover_slope")),
                turnover_slope20=_opt_float(row.get("turnover_slope20")),
                net_inflow_1d=_opt_float(row.get("net_inflow_1d")),
                net_inflow_5d=_opt_float(row.get("net_inflow_5d")),
                net_inflow_10d=_opt_float(row.get("net_inflow_10d")),
                inflow_persistent=bool(row.get("inflow_persistent", False)),
                inflow_persistent_10=bool(row.get("inflow_persistent_10", False)),
                flow_data_missing=bool(row.get("flow_data_missing", False)),
                heat_score=round(float(row["heat_score"]), 1),
                strength_score=round(float(row["strength_score"]), 1),
                composite_score=round(float(row["composite_score"]), 1),
                hist_available=bool(row.get("hist_available", False)),
                passes_gate=bool(row.get("passes_gate", False)),
                pulse=bool(row.get("pulse", False)),
                phase_hint=str(row.get("phase_hint", "")),
            )
        )
    return out


def _opt_float(val) -> Optional[float]:
    if val is None:
        return None
    try:
        f = float(val)
        return None if pd.isna(f) else round(f, 4)
    except (ValueError, TypeError):
        return None


def _default_ak():
    import akshare as ak  # type: ignore

    return ak


def _default_fetch_spot(ak_module) -> SpotFetcher:
    from .providers.cn_akshare_provider import fetch_board_spot_df

    def _fetch(sector_type: str) -> pd.DataFrame:
        return fetch_board_spot_df(ak_module, sector_type)

    return _fetch


def _default_fetch_spot_chain(ak_module):
    """多源板块涨幅榜链：东财 → 同花顺(行业) → 新浪(行业)，返回 (df, source)。"""
    from .providers.cn_akshare_provider import fetch_board_spot_df_chain

    def _fetch(sector_type: str) -> tuple[pd.DataFrame, str]:
        return fetch_board_spot_df_chain(ak_module, sector_type)

    return _fetch


def _default_fetch_hist(ak_module) -> HistFetcher:
    from .providers.cn_akshare_provider import fetch_board_hist_df

    def _fetch(board_name: str, sector_type: str) -> pd.DataFrame:
        return fetch_board_hist_df(ak_module, board_name, sector_type)

    return _fetch


def is_hist_fresh(hist: Optional[pd.DataFrame], trade_date: str, max_gap_days: int = 15) -> bool:
    """板块历史必须覆盖到 trade_date 附近，否则视为陈旧数据，禁止用于近期动量计算。

    实测：同花顺板块指数历史停更于 2024-01（stock_board_*_index_ths），
    若不经校验会被当成"近 5 日"数据使用 → 5 日动量/超额收益完全错位（假数据）。
    """
    if hist is None or hist.empty or "date" not in hist.columns:
        return False
    try:
        last = pd.to_datetime(hist["date"]).max()
        ref = pd.to_datetime(trade_date)
        return (ref - last).days <= max_gap_days
    except Exception:
        return False


def _default_fetch_hist_chain(ak_module, spot_sources: dict[str, str]) -> HistFetcher:
    """板块历史：仅用东方财富（近期数据），并接受调用方的新鲜度校验。

    注意：不再以 THS 板块指数历史兜底——该源停更于 2024-01，属于陈旧数据。
    新鲜度校验在 build_mainline_candidates._load_hist 中执行。
    """
    from .providers.cn_akshare_provider import fetch_board_hist_df

    def _fetch(board_name: str, sector_type: str) -> pd.DataFrame:
        return fetch_board_hist_df(ak_module, board_name, sector_type)

    return _fetch


def _default_fetch_flow(ak_module) -> FlowFetcher:
    from .providers.cn_akshare_provider import fetch_sector_fund_flow_rank_df

    def _fetch(sector_type: str, period: str) -> pd.DataFrame:
        return fetch_sector_fund_flow_rank_df(ak_module, sector_type, period)

    return _fetch


def _default_fetch_flow_chain(ak_module) -> FlowFetcher:
    """资金流链：东财排行 → 新浪行业资金流（行业）。概念资金流无新浪源时降级为空。"""
    from .providers.cn_akshare_provider import (
        _normalize_sina_flow_df,
        fetch_sector_fund_flow_rank_df,
    )

    def _fetch(sector_type: str, period: str) -> pd.DataFrame:
        try:
            return fetch_sector_fund_flow_rank_df(ak_module, sector_type, period)
        except Exception:
            if sector_type != "industry":
                return pd.DataFrame()
            try:
                from .providers.cn_sina_moneyflow_provider import (
                    fetch_sina_industry_board_fund_flow_df,
                )

                return _normalize_sina_flow_df(fetch_sina_industry_board_fund_flow_df())
            except Exception:
                return pd.DataFrame()

    return _fetch


def _default_fetch_zt(ak_module) -> ZtFetcher:
    from .providers.cn_akshare_provider import _call_akshare_retry

    def _fetch(trade_date: str) -> pd.DataFrame:
        func = getattr(ak_module, "stock_zt_pool_em", None)
        if func is None:
            return pd.DataFrame()
        return _call_akshare_retry(
            lambda: func(date=trade_date.replace("-", "")), api_name="stock_zt_pool_em"
        )

    return _fetch


def _default_fetch_benchmark(ak_module) -> IndexFetcher:
    def _fetch() -> pd.DataFrame:
        last_exc: Exception | None = None
        for api_name in ("stock_zh_index_daily_em", "stock_zh_index_daily"):
            func = getattr(ak_module, api_name, None)
            if func is None:
                continue
            try:
                from .providers.cn_akshare_provider import _call_akshare_retry

                raw = _call_akshare_retry(
                    lambda f=func: f(symbol="sh000300"), api_name=api_name
                )
                if raw is None or raw.empty:
                    continue
                date_col = next((c for c in ("日期", "date") if c in raw.columns), None)
                close_col = next((c for c in ("收盘", "close") if c in raw.columns), None)
                if close_col is None:
                    continue
                out = pd.DataFrame()
                out["close"] = pd.to_numeric(raw[close_col], errors="coerce")
                if date_col is not None:
                    out["date"] = raw[date_col]
                return out
            except Exception as exc:
                last_exc = exc
                continue
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("akshare has no benchmark index API (sh000300)")

    return _fetch


def build_mainline_candidates(
    trade_date: str,
    *,
    perspective: str = "short",
    top_concept: int = 20,
    top_industry: int = 10,
    hist_days: int = 60,
    ak_module=None,
    fetch_spot: Optional[SpotFetcher] = None,
    fetch_hist: Optional[HistFetcher] = None,
    fetch_flow: Optional[FlowFetcher] = None,
    fetch_zt: Optional[ZtFetcher] = None,
    fetch_benchmark: Optional[IndexFetcher] = None,
) -> dict:
    """M1 规则层主入口：按需拉取 → 硬过滤 → 双维度评分 → top N 候选 + 情绪温度。

    返回 dict：
      {
        "trade_date", "perspective",
        "emotion": {...},                       # compute_emotion_temperature 结果
        "market_median_chg_1d": float|None,
        "benchmark_chg_5d": float|None,
        "boards": [BoardFeatures.to_dict(), ...],  # 行业+概念合并，按 composite 降序
        "pool_sizes": {"industry": n, "concept": n},
        "hist_fetched": n,                       # 实际拉取历史的板块数（按需拉取）
        "spot_sources": {"industry": "em|ths|sina|custom|None", "concept": ...},
        "warnings": [str, ...],                  # 降级/不可用说明（数据稳定性）
      }
    """
    ak = ak_module if ak_module is not None else _default_ak()
    f_zt = fetch_zt or _default_fetch_zt(ak)
    f_bench = fetch_benchmark or _default_fetch_benchmark(ak)
    warnings: list[str] = []

    # ── 1. 榜单（多源链，任何单源失败不阻塞整体） + 市场基准 ──
    if fetch_spot is not None:
        spot_industry = fetch_spot("industry")
        spot_concept = fetch_spot("concept")
        spot_sources = {"industry": "custom", "concept": "custom"}
    else:
        f_spot_chain = _default_fetch_spot_chain(ak)
        try:
            spot_industry, src_ind = f_spot_chain("industry")
            spot_sources = {"industry": src_ind}
        except Exception as exc:
            spot_industry = pd.DataFrame()
            spot_sources = {"industry": None}
            warnings.append(f"行业板块涨幅榜不可用：{type(exc).__name__}: {exc}")
        try:
            spot_concept, src_con = f_spot_chain("concept")
            spot_sources["concept"] = src_con
        except Exception as exc:
            spot_concept = pd.DataFrame()
            spot_sources["concept"] = None
            warnings.append(f"概念板块涨幅榜不可用（无可用降级源）：{type(exc).__name__}: {exc}")

    chg_series = []
    for s in (spot_industry, spot_concept):
        if s is not None and not s.empty and "chg_1d" in s.columns:
            chg_series.append(s["chg_1d"])
    all_chg = pd.concat(chg_series) if chg_series else pd.Series(dtype=float)
    market_median_chg_1d = float(all_chg.median()) if not all_chg.empty else None

    benchmark_chg_5d: Optional[float] = None
    benchmark_chg_20d: Optional[float] = None
    try:
        bench_df = f_bench()
        if bench_df is not None and not bench_df.empty and "close" in bench_df.columns:
            closes = bench_df["close"].dropna().astype(float)
            if len(closes) >= 6:
                benchmark_chg_5d = float(closes.iloc[-1] / closes.iloc[-6] - 1)
            if len(closes) >= 21:
                benchmark_chg_20d = float(closes.iloc[-1] / closes.iloc[-21] - 1)
    except Exception:
        benchmark_chg_5d = None
        benchmark_chg_20d = None

    # ── 2. 硬过滤 + 预选（按需拉取的候选池） ──
    pools = {
        "industry": (hard_filter_spot(spot_industry), top_industry),
        "concept": (hard_filter_spot(spot_concept), top_concept),
    }
    pre_candidates: dict[str, pd.DataFrame] = {}
    pool_sizes: dict[str, int] = {}
    for sector_type, (pool, top_n) in pools.items():
        if pool.empty:
            pool_sizes[sector_type] = 0
            continue
        pool_sizes[sector_type] = len(pool)
        # 预排序：当日涨幅分位 70% + 换手分位 30%，取 top N 进入历史拉取
        pre = pool.copy()
        pre["_pre"] = (
            0.7 * _pct(pre["chg_1d"]).fillna(0.5)
            + 0.3 * _pct(pre["turnover"]).fillna(0.5)
        )
        pre = pre.sort_values("_pre", ascending=False).head(top_n)
        pre_candidates[sector_type] = pre

    # ── 3. 按需拉历史（只对预选池，按 spot 来源选源；并发加速，单源失败不阻塞） ──
    f_hist = fetch_hist or _default_fetch_hist_chain(ak, spot_sources)
    hist_map: dict[str, pd.DataFrame] = {}

    def _load_hist(name_sector: tuple[str, str]):
        name, sector_type = name_sector
        try:
            h = f_hist(name, sector_type)
            if h is not None and not h.empty and is_hist_fresh(h, trade_date):
                return (name, h)
            return (name, None)
        except Exception:
            return (name, None)

    hist_jobs = [
        (name, sector_type)
        for sector_type, pre in pre_candidates.items()
        for name in pre["name"].astype(str)
    ]
    if hist_jobs:
        with ThreadPoolExecutor(max_workers=min(5, len(hist_jobs))) as executor:
            for name, h in executor.map(_load_hist, hist_jobs):
                if h is not None:
                    hist_map[name] = h

    # ── 4. 资金流（整池，今日 / 5日 / 10日，多源链） ──
    f_flow = fetch_flow or _default_fetch_flow_chain(ak)
    flow_1d: dict[str, pd.DataFrame] = {}
    flow_5d: dict[str, pd.DataFrame] = {}
    flow_10d: dict[str, pd.DataFrame] = {}
    for sector_type in ("industry", "concept"):
        for period, store in (("1d", flow_1d), ("5d", flow_5d), ("10d", flow_10d)):
            try:
                store[sector_type] = f_flow(sector_type, period)
            except Exception:
                store[sector_type] = pd.DataFrame()

    # ── 5. 评分（池内分位，v2 多周期动量结构） ──
    all_boards: list[BoardFeatures] = []
    for sector_type in ("industry", "concept"):
        pre = pre_candidates.get(sector_type)
        if pre is None or pre.empty:
            continue
        scored = score_boards(
            pre,
            sector_type=sector_type,
            hist_map=hist_map,
            flow_1d=flow_1d.get(sector_type),
            flow_5d=flow_5d.get(sector_type),
            flow_10d=flow_10d.get(sector_type),
            benchmark_chg_5d=benchmark_chg_5d,
            benchmark_chg_20d=benchmark_chg_20d,
            market_median_chg_1d=market_median_chg_1d if market_median_chg_1d is not None else 0.0,
            perspective=perspective,
        )
        all_boards.extend(scored)

    all_boards.sort(key=lambda b: b.composite_score, reverse=True)

    # ── 6. 情绪温度 gating ──
    try:
        zt_df = f_zt(trade_date)
    except Exception:
        zt_df = pd.DataFrame()
    emotion = compute_emotion_temperature(zt_df if zt_df is not None else None)

    return {
        "trade_date": trade_date,
        "perspective": perspective,
        "emotion": emotion,
        "market_median_chg_1d": market_median_chg_1d,
        "benchmark_chg_5d": benchmark_chg_5d,
        "benchmark_chg_20d": benchmark_chg_20d,
        "boards": [b.to_dict() for b in all_boards],
        "pool_sizes": pool_sizes,
        "hist_fetched": len(hist_map),
        "spot_sources": spot_sources,
        "warnings": warnings,
    }
