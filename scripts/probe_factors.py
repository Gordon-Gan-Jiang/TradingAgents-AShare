"""One-off probe: does ANY simple, cheap factor predict short-horizon A-share
cross-sectional excess returns in the analyzed universe?

Not part of the app. Reads the panel written by probe_fetch_panel.py plus the
DB signals, and prints IC / t / quintile-spread tables.

Method notes
------------
* Factors at date t use only data <= t (no lookahead).
* Target = forward return from close(t) to close(t+h), demeaned
  cross-sectionally on the same date (equal-weight market-neutral), which is
  the right target for "stock selection skill" as opposed to beta.
* IC is the per-date Spearman rank correlation between factor and target;
  the reported t-stat is computed from the across-date distribution of IC
  (i.e. clustered by date), so it does not assume independent observations.
"""
from __future__ import annotations

import math
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "tradingagents.db"
PANEL = ROOT / "tradingagents" / "dataflows" / "data_cache" / "_probe" / "panel.csv"
HORIZONS = (1, 3, 5, 10)


def implied_accuracy(rho: float) -> float:
    """P(sign correct) for bivariate normal with correlation rho."""
    return 0.5 + math.asin(max(-1.0, min(1.0, rho))) / math.pi


def tstat(x: pd.Series) -> tuple[float, float, int]:
    x = x.dropna()
    n = len(x)
    if n < 3:
        return (np.nan, np.nan, n)
    se = x.std(ddof=1) / math.sqrt(n)
    if not se or np.isnan(se):
        return (x.mean(), np.nan, n)
    return (x.mean(), x.mean() / se, n)


def spearman(a: pd.Series, b: pd.Series) -> float:
    """Spearman without scipy: Pearson on ranks."""
    d = pd.concat([a, b], axis=1).dropna()
    if len(d) < 10:
        return np.nan
    return float(d.iloc[:, 0].rank().corr(d.iloc[:, 1].rank()))


def load_panel() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    df = pd.read_csv(PANEL)
    df["date"] = pd.to_datetime(df["date"])
    df = df.drop_duplicates(["symbol", "date"]).sort_values(["symbol", "date"])
    C = df.pivot(index="date", columns="symbol", values="close").astype(float)
    V = df.pivot(index="date", columns="symbol", values="volume").astype(float)
    H = df.pivot(index="date", columns="symbol", values="high").astype(float)
    L = df.pivot(index="date", columns="symbol", values="low").astype(float)
    # keep symbols with a reasonable history
    keep = C.notna().sum() >= 120
    cols = keep[keep].index
    return C[cols], V[cols], H[cols], L[cols]


def build_factors(C, V, H, L) -> dict[str, pd.DataFrame]:
    ret1 = C / C.shift(1) - 1.0
    f: dict[str, pd.DataFrame] = {}
    f["mom_5"] = C / C.shift(5) - 1.0
    f["mom_20"] = C / C.shift(20) - 1.0
    f["mom_60"] = C / C.shift(60) - 1.0
    f["rev_1"] = -ret1                      # short-term reversal
    f["rev_5"] = -(C / C.shift(5) - 1.0)    # 5d reversal
    f["vol_20"] = ret1.rolling(20).std()
    f["vol_shock"] = V / V.rolling(20).mean()
    f["dist_ma20"] = C / C.rolling(20).mean() - 1.0
    f["dist_ma60"] = C / C.rolling(60).mean() - 1.0
    f["range_10"] = ((H - L) / C).rolling(10).mean()
    f["illiq_20"] = (ret1.abs() / (V * C)).rolling(20).mean()
    return f


def rank_ic(fac: pd.DataFrame, tgt: pd.DataFrame) -> pd.Series:
    """Per-date Spearman IC between factor and target."""
    out = {}
    common = fac.index.intersection(tgt.index)
    rf = fac.loc[common].rank(axis=1)
    rt = tgt.loc[common].rank(axis=1)
    rf = rf.sub(rf.mean(axis=1), axis=0)
    rt = rt.sub(rt.mean(axis=1), axis=0)
    num = (rf * rt).sum(axis=1)
    den = np.sqrt((rf**2).sum(axis=1) * (rt**2).sum(axis=1))
    ic = num / den.replace(0, np.nan)
    mask = fac.loc[common].notna().sum(axis=1) >= 20
    return ic[mask]


def main() -> int:
    C, V, H, L = load_panel()
    print(f"panel: {C.shape[0]} dates x {C.shape[1]} symbols "
          f"({C.index.min().date()} -> {C.index.max().date()})")
    factors = build_factors(C, V, H, L)

    fwd = {h: C.shift(-h) / C - 1.0 for h in HORIZONS}
    excess = {h: fwd[h].sub(fwd[h].mean(axis=1), axis=0) for h in HORIZONS}

    # ---------- Table A: IC of each candidate factor ----------
    print("\n=== A. 因子 IC（对横截面超额收益，按日 Spearman）===")
    hdr = f"{'factor':<12}" + "".join(
        f"| h={h}: IC      t      n  " for h in HORIZONS
    )
    print(hdr)
    print("-" * len(hdr))
    rows = []
    for name, fac in factors.items():
        line = f"{name:<12}"
        for h in HORIZONS:
            ic = rank_ic(fac, excess[h])
            m, t, n = tstat(ic)
            line += f"| {m:+.4f} {t:+6.2f} {n:4d}  "
            rows.append((name, h, m, t, n))
        print(line)

    # ---------- Table B: quintile spread (MEDIAN-based: robust to outliers) ----------
    print("\n=== B. 五分位超额收益 Q5-Q1（中位数口径, 稳健；括号内为 t）===")
    print(f"{'factor':<12}" + "".join(f"| h={h:<12}" for h in HORIZONS))
    for name, fac in factors.items():
        line = f"{name:<12}"
        for h in HORIZONS:
            q = fac.rank(axis=1, pct=True)
            top = excess[h].where(q >= 0.8).median(axis=1)
            bot = excess[h].where(q <= 0.2).median(axis=1)
            spread = (top - bot).dropna()
            m, t, n = tstat(spread)
            line += f"| {100*m:+6.3f}({t:+5.2f})"
        print(line)

    # ---------- Table C: market timing (equal-weight market) ----------
    print("\n=== C. 市场择时：指数层面信号 vs 后续市场收益 ===")
    mkt = fwd[1].mean(axis=1)  # daily equal-weight market return
    for h in HORIZONS:
        mfwd = C.shift(-h).mean(axis=1) / C.mean(axis=1) - 1.0
        sigs = {
            "mkt_mom_5": C.mean(axis=1) / C.mean(axis=1).shift(5) - 1.0,
            "mkt_mom_20": C.mean(axis=1) / C.mean(axis=1).shift(20) - 1.0,
            "breadth_1": (C / C.shift(1) - 1.0).gt(0).mean(axis=1),
            "breadth_5": (C / C.shift(5) - 1.0).gt(0).mean(axis=1),
            "mkt_vol_20": mkt.rolling(20).std(),
            "mkt_dist_ma20": C.mean(axis=1) / C.mean(axis=1).rolling(20).mean() - 1.0,
        }
        line = f"h={h:<3}"
        for sname, s in sigs.items():
            both = pd.concat([s, mfwd], axis=1).dropna()
            if len(both) < 20:
                line += f"| {sname}: n/a "
                continue
            r = spearman(both.iloc[:, 0], both.iloc[:, 1])
            line += f"| {sname}={r:+.3f} "
        print(line)

    # ---------- Table D: re-test the shipped signal with good prices ----------
    print("\n=== D. 现有 LLM 方向信号（用完整行情重算，已去重）===")
    con = sqlite3.connect(DB)
    sig = pd.read_sql_query(
        "select symbol, signal_trade_date, direction_bucket from report_t1_outcomes "
        "where status='evaluated' and direction_bucket in ('bullish','bearish')",
        con,
    )
    con.close()
    sig = sig.drop_duplicates(["symbol", "signal_trade_date"])
    sig["date"] = pd.to_datetime(sig["signal_trade_date"])
    sig = sig[sig["symbol"].isin(C.columns)]
    sig = sig[sig["date"].isin(C.index)]
    print(f"去重后可用信号: {len(sig)}  "
          f"(bullish {int((sig.direction_bucket=='bullish').sum())} / "
          f"bearish {int((sig.direction_bucket=='bearish').sum())})")

    cmap = {c: i for i, c in enumerate(C.columns)}
    dmap = {d: i for i, d in enumerate(C.index)}
    pairs = list(
        zip(
            sig["symbol"].tolist(),
            sig["date"].tolist(),
            sig["direction_bucket"].tolist(),
        )
    )
    for h in HORIZONS:
        ex = excess[h].to_numpy()
        vals, dirs = [], []
        for sym, d, bucket in pairs:
            i, j = dmap.get(d), cmap.get(sym)
            if i is None or j is None:
                continue
            v = ex[i, j]
            if np.isnan(v):
                continue
            vals.append(v)
            dirs.append(1.0 if bucket == "bullish" else -1.0)
        if len(vals) < 20:
            continue
        vals = np.array(vals)
        dirs = np.array(dirs)
        signed = vals * dirs                      # >0 means direction was right
        acc = float((signed > 0).mean())
        m, t, n = tstat(pd.Series(signed))
        ic = spearman(pd.Series(vals), pd.Series(dirs))
        print(f"  h={h:<3} n={n:4d} 方向正确率={100*acc:5.2f}%  "
              f"平均超额={100*m:+.3f}% (t={t:+.2f})  IC={ic:+.4f}  "
              f"理论对应={100*implied_accuracy(ic):.2f}%")

    # ---------- Table E: the accuracy ceiling ----------
    print("\n=== E. IC 与方向正确率的数学关系（P=0.5+asin(rho)/pi）===")
    for rho in (0.02, 0.05, 0.10, 0.15, 0.20, 0.30):
        print(f"  IC={rho:.2f} -> 方向正确率上限 {100*implied_accuracy(rho):.2f}%")

    # ---------- Table F: cost break-even vs measured IC ----------
    # For a rank-weighted long-short book, expected period return ~= IC * sigma_excess.
    # So the IC needed to just cover a round-trip cost c is  c / sigma_excess.
    COST = 0.0025  # 0.25% round trip: commission x2 + 0.05% stamp on sell + slippage
    print(f"\n=== F. 成本盈亏平衡所需的 IC（往返成本 {100*COST:.2f}%）===")
    print(f"{'h':<4}{'sigma_excess':>14}{'需要的IC':>12}{'该周期实测最强|IC|':>22}{'净超额可达?':>14}")
    strongest = {}
    for h in HORIZONS:
        sig = float(excess[h].std(axis=1).mean())
        need = COST / sig if sig else np.nan
        best = 0.0
        best_name = ""
        for name, fac in factors.items():
            ic = rank_ic(fac, excess[h])
            m = abs(float(ic.mean())) if len(ic) else 0.0
            if m > best:
                best, best_name = m, name
        strongest[h] = (best, best_name, need)
        verdict = "是" if best > need else "否"
        print(f"{h:<4}{100*sig:>13.2f}%{need:>12.3f}{best:>16.3f} ({best_name:<10}){verdict:>14}")
    print("  (净超额可达 = 实测|IC| 是否超过盈亏平衡所需 IC)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
