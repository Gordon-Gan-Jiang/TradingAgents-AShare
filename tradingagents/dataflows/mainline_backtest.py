"""规则层主线回测（M6）：用真实历史板块数据回放"主线识别"，统计前瞻超额收益。

设计（见 docs/mainline-market-design.md 6.1-11）：
- 判断主线识别是否有价值的唯一硬指标：识别出的板块，其后 5/10 日相对基准（沪深300）的超额收益。
- 回测只使用规则层（确定性、可回测），不调用 LLM——规则层是管道中"算分/过滤"的确定部分，
  LLM 层负责归并/叙事（M3），其质量由结构约束兜底。
- 数据：同花顺行业板块指数日K（全行业面板，覆盖 2020-01 ~ 2024-01，一次性抓取并缓存到 data_cache），基准 sh000300。
  注意：同花顺板块指数历史停更于 2024-01，回测窗口应设在数据覆盖范围内（--end 2024-01-08 以内，
  程序会自动对齐面板最大日期）；近期板块历史（东财）在本网络不稳定，暂不用于回测。
- 方法：对每个交易日 D，用 D 当日及之前的收盘数据计算 heat（当日涨幅分位）+ strength（5日动量分位）
  合成得分，取 top N 板块，统计其 D 后 horizon 日的平均收益 − 基准同期收益 = 超额收益；
  同时取 bottom N 作为 sanity 对照（若规则无效，top≈bottom）。

CLI：python -m tradingagents.dataflows.mainline_backtest --end 2023-12-29 --days 120 --top-n 5 --horizon 5
"""
from __future__ import annotations

import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

_CACHE_DIR = Path(__file__).resolve().parent / "data_cache"
_CACHE_DIR.mkdir(exist_ok=True)

# 缓存新鲜度（P1）。默认 3 天：覆盖一个周末 + 一天缓冲。
#
# ## 为什么必须有 TTL
#
# 此前 `cache=True` 的语义是"文件存在就无条件返回"。2026-08-31 写下的缓存
# 因此被**永久**复用，导致 `evaluate_t1_for_report` 里
# `check_date <= trade_date` 恒真 → 静默 `return []` → `mainline_t1_outcomes` 0 行
# → 「昨日主线今日兑现了吗」永远答不出来，且全程没有任何提示。
#
# 判据用**数据末日**而不是文件 mtime：mtime 只说"什么时候写的"，
# 写的时候数据本身可能已经旧了。
_BENCHMARK_CACHE_MAX_AGE_ENV = "TA_BENCHMARK_CACHE_MAX_AGE_DAYS"
_PANEL_CACHE_MAX_AGE_ENV = "TA_PANEL_CACHE_MAX_AGE_DAYS"
_CACHE_MAX_AGE_DAYS_DEFAULT = 3


def _default_ak():
    import akshare as ak  # type: ignore

    return ak


def _cache_max_age_days(env_name: str) -> int:
    """TTL 天数，可用 env 覆盖。非法值回落到默认（不因配置写错而崩）。"""
    raw = (os.getenv(env_name) or "").strip()
    if not raw:
        return _CACHE_MAX_AGE_DAYS_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        return _CACHE_MAX_AGE_DAYS_DEFAULT
    return value if value >= 0 else _CACHE_MAX_AGE_DAYS_DEFAULT


def _last_data_date(frame) -> Optional[str]:
    """数据（Series 或 DataFrame）的最后一个索引日期。解析不出则 None。"""
    if frame is None or len(frame) == 0:
        return None
    raw = str(frame.index[-1]).strip()
    parsed = pd.to_datetime(raw, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.strftime("%Y-%m-%d")


def cache_is_stale(frame, *, today: str, max_age_days: int) -> bool:
    """缓存是否陈旧——判据是**数据末日**，不是文件时间。

    陈旧 = 最后一个数据日 < (today - max_age_days)。

    这样周末不会误判：周一(9-23)看周五(9-20)的数据，TTL=3 时
    cutoff=9-20，9-20 不早于 cutoff → 仍算新鲜，不会天天重抓 90 个板块。

    空数据、索引解析不出日期，一律算**陈旧**——宁可重抓，也不要拿
    "看起来有文件"当依据。
    """
    last = _last_data_date(frame)
    if last is None:
        return True
    today_dt = pd.to_datetime(today, errors="coerce")
    if pd.isna(today_dt):
        # 基准日不可解析时无法判断新鲜度 → 保守算陈旧
        return True
    cutoff = (today_dt - pd.Timedelta(days=max(0, int(max_age_days)))).strftime("%Y-%m-%d")
    return last < cutoff


def _read_cache_csv(path: Path) -> Optional[pd.DataFrame]:
    """读缓存 CSV；读不出（损坏/空）返回 None，由调用方走实时抓取。"""
    try:
        df = pd.read_csv(path, index_col=0)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    df.index = df.index.astype(str)
    return df


def fetch_benchmark_close(
    ak_module=None,
    *,
    cache: bool = True,
    today: Optional[str] = None,
    max_age_days: Optional[int] = None,
) -> pd.Series:
    """沪深300 日收盘（新浪源，稳定）。返回 index=date(str), values=close。

    `cache=True` 的语义是"**新鲜的**缓存才直接返回"，不是"文件存在就返回"——
    后者会让一份陈旧缓存永久生效（见模块顶部 `_CACHE_MAX_AGE_DAYS_DEFAULT` 的说明）。
    陈旧时尝试实时刷新；刷新失败**仍返回陈旧缓存**（可用性优先），
    由上层（`evaluate_t1_for_report`）负责把"拿到的是旧数据"声明出来。
    """
    from .providers.cn_akshare_provider import _call_akshare_retry

    ak = ak_module or _default_ak()
    cache_path = _CACHE_DIR / "benchmark_sh000300.csv"
    staleness_today = today or datetime.now().strftime("%Y-%m-%d")
    ttl = _cache_max_age_days(_BENCHMARK_CACHE_MAX_AGE_ENV) if max_age_days is None else max_age_days

    cached: Optional[pd.Series] = None
    if cache and cache_path.exists():
        df = _read_cache_csv(cache_path)
        if df is not None:
            cached = df.iloc[:, 0]
            if not cache_is_stale(cached, today=staleness_today, max_age_days=ttl):
                return cached

    last_exc: Exception | None = None
    for api_name in ("stock_zh_index_daily_em", "stock_zh_index_daily"):
        func = getattr(ak, api_name, None)
        if func is None:
            continue
        try:
            raw = _call_akshare_retry(lambda f=func: f(symbol="sh000300"), api_name=api_name)
            if raw is None or raw.empty:
                continue
            date_col = next((c for c in ("date", "日期") if c in raw.columns), None)
            close_col = next((c for c in ("close", "收盘") if c in raw.columns), None)
            if date_col is None or close_col is None:
                continue
            s = pd.to_numeric(raw[close_col], errors="coerce")
            s.index = raw[date_col].astype(str)
            s = s[~s.index.duplicated(keep="last")].sort_index()
            if cache:
                s.to_csv(cache_path)
            return s
        except Exception as exc:
            last_exc = exc
            continue

    # 刷新失败：有旧数据就用旧数据（可用性优先），没有才抛。
    if cached is not None and len(cached) > 0:
        return cached
    raise last_exc or RuntimeError("no benchmark source available")


def fetch_industry_panel(
    ak_module=None,
    *,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    max_workers: int = 5,
    cache: bool = True,
    today: Optional[str] = None,
    max_age_days: Optional[int] = None,
) -> pd.DataFrame:
    """抓取同花顺全部行业板块指数日K，构建 面板（index=日期, columns=板块名, values=收盘）。

    一次性抓取约 90 个板块（每个 1~2s，并发 5），结果缓存为 CSV，后续秒回。

    `cache=True` 只在缓存**新鲜**时秒回；陈旧时重抓（判据同 `fetch_benchmark_close`）。
    """
    from .providers.cn_akshare_provider import fetch_ths_board_index_df

    ak = ak_module or _default_ak()
    cache_path = _CACHE_DIR / "ths_industry_panel.csv"
    staleness_today = today or datetime.now().strftime("%Y-%m-%d")
    ttl = _cache_max_age_days(_PANEL_CACHE_MAX_AGE_ENV) if max_age_days is None else max_age_days

    cached: Optional[pd.DataFrame] = None
    if cache and cache_path.exists():
        df = _read_cache_csv(cache_path)
        if df is not None:
            if not cache_is_stale(df, today=staleness_today, max_age_days=ttl):
                return df
            cached = df

    try:
        df = _fetch_industry_panel_live(
            ak,
            fetch_ths_board_index_df=fetch_ths_board_index_df,
            max_workers=max_workers,
            start_date=start_date,
            end_date=end_date,
        )
    except Exception:
        # 重抓失败：有旧面板就用旧的（可用性优先），没有才把异常抛出去。
        if cached is not None and len(cached) > 0:
            return cached
        raise
    if cache:
        df.to_csv(cache_path)
    return df


def _fetch_industry_panel_live(
    ak,
    *,
    fetch_ths_board_index_df,
    max_workers: int,
    start_date: Optional[str],
    end_date: Optional[str],
) -> pd.DataFrame:
    """实时抓取板块面板（原 `fetch_industry_panel` 的主体，抽出以便缓存分支保持扁平）。"""
    summary = __import__(
        "tradingagents.dataflows.providers.cn_akshare_provider", fromlist=["fetch_ths_industry_summary_df"]
    ).fetch_ths_industry_summary_df(ak)
    board_names = sorted(set(summary["name"].astype(str).tolist()))
    if not board_names:
        raise RuntimeError("THS industry board list is empty")

    panel: dict[str, pd.Series] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(fetch_ths_board_index_df, ak, name, "industry"): name
            for name in board_names
        }
        for future in futures:
            name = futures[future]
            try:
                h = future.result(timeout=60)
            except Exception:
                continue
            if h is None or h.empty or "date" not in h.columns or "close" not in h.columns:
                continue
            s = pd.to_numeric(h["close"], errors="coerce")
            s.index = h["date"].astype(str)
            s = s[~s.index.duplicated(keep="last")].sort_index()
            panel[name] = s

    if not panel:
        raise RuntimeError("no board history fetched")
    df = pd.DataFrame(panel).sort_index()
    df = df.ffill()
    if start_date:
        df = df[df.index >= start_date]
    if end_date:
        df = df[df.index <= end_date]
    return df


def run_backtest_variant(
    panel: pd.DataFrame,
    bench: pd.Series,
    *,
    variant: str = "v1",
    top_n: int = 5,
    horizon: int = 5,
    warmup: int = 60,
) -> dict:
    """在历史面板上回放指定变体的主线识别，统计 top N 板块的前瞻超额收益。

    variant:
      v1 = 0.6·heat(1日涨幅分位) + 0.4·mom5(5日动量分位)，无硬门槛（v1 原始规则）；
      v2 = 中期动量结构（金融理论版）：RS20(20日超额) + mom20(20日动量)，
           硬门槛 = RS20>0 且 MA20>MA60（多头排列），top 只在过门槛板块中选取。
    超额 = 板块平均 horizon 收益 − 基准 horizon 收益。
    返回逐日记录 + 汇总统计。
    """
    dates = list(panel.index)
    if len(dates) < warmup + horizon + 2:
        raise ValueError("panel too short for backtest")

    # 预计算（用 D 当日及之前数据，避免未来函数）
    chg = panel.pct_change(fill_method=None)
    mom5 = panel / panel.shift(5) - 1
    mom20 = panel / panel.shift(20) - 1
    ma20 = panel.rolling(20).mean()
    ma60 = panel.rolling(60).mean()
    ma_bullish = (ma20 > ma60).fillna(False)

    # 基准对齐到面板日期
    bench_aligned = bench.reindex(panel.index).ffill()
    bench_mom20 = bench_aligned / bench_aligned.shift(20) - 1
    rs20 = mom20.sub(bench_mom20, axis=0)  # 板块20日涨幅 − 基准20日涨幅

    bench_fwd: dict[str, float] = {}
    for i, d in enumerate(dates):
        if i + horizon < len(dates):
            b0, b1 = bench_aligned.iloc[i], bench_aligned.iloc[i + horizon]
            if pd.notna(b0) and pd.notna(b1) and b0 != 0:
                bench_fwd[d] = float(b1 / b0 - 1)

    daily: list[dict] = []
    for i, d in enumerate(dates):
        if i < warmup or i + horizon >= len(dates):
            continue
        c = chg.loc[d]
        m5 = mom5.loc[d]
        r20 = rs20.loc[d]
        m20 = mom20.loc[d]
        valid = c.dropna()
        if len(valid) < 10:
            continue
        if variant == "v2":
            gate = (r20 > 0) & ma_bullish.loc[d]
            if int(gate.sum()) < 1:
                continue  # 当日无过门槛板块，无主线
            composite = 0.6 * r20.rank(pct=True).fillna(0.5) + 0.4 * m20.rank(pct=True).fillna(0.5)
            composite = composite.where(gate, -1.0)  # 未过门槛者排最后（不入选）
        else:
            composite = 0.6 * c.rank(pct=True).fillna(0.5) + 0.4 * m5.rank(pct=True).fillna(0.5)
        ordered = composite.sort_values(ascending=False)
        top = ordered.head(top_n).index.tolist()
        bottom = ordered.tail(top_n).index.tolist()
        fwd = panel.iloc[i + horizon] / panel.iloc[i] - 1
        top_ret = float(fwd[top].mean()) if len(top) else np.nan
        bot_ret = float(fwd[bottom].mean()) if len(bottom) else np.nan
        bf = bench_fwd.get(d)
        daily.append(
            {
                "date": d,
                "top_boards": top,
                "bottom_boards": bottom,
                "top_ret": top_ret,
                "bottom_ret": bot_ret,
                "bench_ret": bf,
                "excess_top": (top_ret - bf) if bf is not None else np.nan,
                "excess_bottom": (bot_ret - bf) if bf is not None else np.nan,
            }
        )

    df = pd.DataFrame(daily).dropna(subset=["excess_top", "excess_bottom"])
    if df.empty:
        return {"daily": daily, "summary": {"n_days": 0, "note": "no valid days", "variant": variant}}

    def _stat(col: str) -> dict:
        s = df[col]
        return {
            "mean": float(s.mean()),
            "median": float(s.median()),
            "hit_rate": float((s > 0).mean()),
            "std": float(s.std()),
            "min": float(s.min()),
            "max": float(s.max()),
        }

    summary = {
        "n_days": int(len(df)),
        "horizon_days": horizon,
        "top_n": top_n,
        "variant": variant,
        "top": _stat("excess_top"),
        "bottom": _stat("excess_bottom"),
        "edge_top_minus_bottom": float(df["excess_top"].mean() - df["excess_bottom"].mean()),
        "start_date": str(df["date"].iloc[0]),
        "end_date": str(df["date"].iloc[-1]),
    }
    return {"daily": daily, "summary": summary}


def run_backtest(
    panel: pd.DataFrame,
    bench: pd.Series,
    *,
    top_n: int = 5,
    horizon: int = 5,
    warmup: int = 10,
) -> dict:
    """兼容旧接口：v1 规则（warmup 保持默认 10）。"""
    return run_backtest_variant(panel, bench, variant="v1", top_n=top_n, horizon=horizon, warmup=warmup)


def run_backtest_comparison(
    panel: pd.DataFrame,
    bench: pd.Series,
    *,
    top_n: int = 5,
    horizons: tuple[int, ...] = (5, 10, 20),
) -> dict:
    """v1 vs v2 对比：各 horizon 下 Top N 板块前瞻超额收益与边际。"""
    out: dict[str, dict] = {}
    for h in horizons:
        v1 = run_backtest_variant(panel, bench, variant="v1", top_n=top_n, horizon=h, warmup=60)
        v2 = run_backtest_variant(panel, bench, variant="v2", top_n=top_n, horizon=h, warmup=60)
        out[str(h)] = {"v1": v1["summary"], "v2": v2["summary"]}
    return {"comparison": out, "top_n": top_n}


def run_mainline_backtest(
    *,
    end_date: str,
    days: int = 60,
    top_n: int = 5,
    horizon: int = 5,
    variant: str = "v1",
    compare: bool = False,
    ak_module=None,
    cache: bool = True,
) -> dict:
    """完整回测入口：抓面板 + 基准 → 回放（v1/v2 或对比）→ 汇总；结果写 results/。

    面板数据源为同花顺行业板块指数历史（覆盖 2020-01 ~ 2024-01），
    end_date 会被自动对齐到面板实际覆盖范围（如请求 2026 会回退到面板最大日期）。
    """
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    start_date = (end_dt - timedelta(days=int(days * 1.9) + 90)).strftime("%Y-%m-%d")
    panel_full = fetch_industry_panel(ak_module, cache=cache)
    if panel_full is None or panel_full.empty:
        raise RuntimeError("industry panel is empty")
    # 对齐 end：取 min(请求 end, 面板最大日期)
    panel_max = str(panel_full.index.max())
    eff_end = end_date if end_date <= panel_max else panel_max
    panel = panel_full[panel_full.index <= eff_end]
    if start_date > eff_end:
        start_date = panel.index[0] if len(panel.index) else eff_end
    panel = panel[panel.index >= start_date]
    bench = fetch_benchmark_close(ak_module, cache=cache)

    if compare:
        result = run_backtest_comparison(panel, bench, top_n=top_n)
    else:
        result = run_backtest_variant(
            panel, bench, variant=variant, top_n=top_n, horizon=horizon, warmup=60
        )
    result["config"] = {
        "end_date": end_date,
        "effective_end": eff_end,
        "days": days,
        "top_n": top_n,
        "horizon": horizon,
        "variant": variant if not compare else "comparison",
    }

    results_dir = Path(os.getenv("TA_RESULTS_DIR", "results"))
    results_dir.mkdir(exist_ok=True)
    tag = "compare" if compare else f"{variant}"
    out_path = results_dir / f"mainline_backtest_{eff_end.replace('-', '')}_{tag}.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    result["output_path"] = str(out_path)
    return result


def _print_summary(result: dict) -> None:
    if "comparison" in result:
        print(f"回测对比（v1 vs v2，top_n={result.get('top_n')}）:")
        for h, row in result["comparison"].items():
            v1, v2 = row["v1"], row["v2"]
            print(
                f"  horizon={h:>2}d | v1: top={v1['top']['mean']*100:+.2f}% hit={v1['top']['hit_rate']*100:.0f}% "
                f"edge={v1['edge_top_minus_bottom']*100:+.2f}% | "
                f"v2: top={v2['top']['mean']*100:+.2f}% hit={v2['top']['hit_rate']*100:.0f}% "
                f"edge={v2['edge_top_minus_bottom']*100:+.2f}% | n={v1.get('n_days')}"
            )
        if result.get("output_path"):
            print(f"结果已保存: {result['output_path']}")
        return
    s = result.get("summary", {})
    print(f"回测区间: {s.get('start_date')} ~ {s.get('end_date')}  ({s.get('n_days')} 个交易日)")
    print(f"variant={s.get('variant')}, horizon={s.get('horizon_days')}d, top_n={s.get('top_n')}")
    t, b = s.get("top", {}), s.get("bottom", {})
    print(f"\nTop {s.get('top_n')} 板块前瞻超额收益: 均值 {t.get('mean')*100:.2f}%  中位 {t.get('median')*100:.2f}%  胜率 {t.get('hit_rate')*100:.1f}%")
    print(f"Bottom {s.get('top_n')} 板块前瞻超额收益: 均值 {b.get('mean')*100:.2f}%  中位 {b.get('median')*100:.2f}%  胜率 {b.get('hit_rate')*100:.1f}%")
    print(f"Top−Bottom 边际: {s.get('edge_top_minus_bottom')*100:.2f}%")
    if result.get("output_path"):
        print(f"结果已保存: {result['output_path']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="规则层主线回测（真实历史数据，v1/v2 对比）")
    parser.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"), help="回测结束日 YYYY-MM-DD（自动对齐面板范围）")
    parser.add_argument("--days", type=int, default=250, help="回测交易日跨度")
    parser.add_argument("--top-n", type=int, default=5, help="每日识别的板块数")
    parser.add_argument("--horizon", type=int, default=5, help="前瞻天数（5 或 10；仅单变体模式）")
    parser.add_argument("--variant", choices=["v1", "v2"], default="v1", help="规则变体")
    parser.add_argument("--compare", action="store_true", help="v1 vs v2 对比（horizons 5/10/20）")
    parser.add_argument("--no-cache", action="store_true", help="忽略本地缓存重新抓取")
    args = parser.parse_args()
    result = run_mainline_backtest(
        end_date=args.end,
        days=args.days,
        top_n=args.top_n,
        horizon=args.horizon,
        variant=args.variant,
        compare=args.compare,
        cache=not args.no_cache,
    )
    _print_summary(result)


if __name__ == "__main__":
    main()
