"""市场级数据池（M2）：多源稳定采集 + TTL 缓存 + 新鲜度锚点。

与个股 DataCollector 分离：MarketCollector 面向"市场级"数据（板块涨幅榜、资金流、
涨停池、市场宽度、宏观简报、规则层主线候选），供 M3 主线分析师与前端板块页消费。

数据稳定性设计（实测于本机网络）：
- 东财 push2 系接口（spot_em / hist_em / rank）可能被网络重置 → 多源兜底链：
  行业涨幅榜 em → ths → sina；行业历史 em → ths；资金流 em → sina；市场宽度 sina；
  涨停池东财（稳定）。
- 每个数据块带 source 标注与 data_as_of 锚点；失败降级并记入 warnings，不阻塞整体。
- TTL：盘中 5 分钟、收盘后 12 小时、非交易日 24 小时（按 trade_date 分键）。
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from typing import Any, Optional

import pandas as pd

from .trade_calendar import cn_market_phase, cn_today_str, is_cn_trading_day


def _df_to_rows(df: Optional[pd.DataFrame], limit: Optional[int] = None) -> list[dict]:
    """DataFrame → JSON 兼容的 list[dict]（NaN → None，保留中文）。"""
    if df is None or df.empty:
        return []
    d = df.head(limit) if limit is not None else df
    cleaned = d.where(pd.notna(d), None)
    return json.loads(cleaned.to_json(orient="records", force_ascii=False))


class MarketCollector:
    """市场级数据池：按 (trade_date, perspective) 分键，TTL 缓存，可强制刷新。"""

    def __init__(
        self,
        *,
        intraday_ttl: int = 300,      # 盘中缓存秒数（防反爬 + 数据稳定）
        closed_ttl: int = 12 * 3600,  # 收盘后缓存秒数
        non_trading_ttl: int = 24 * 3600,
        fail_cooldown: float = 300.0,  # 数据源失败冷却秒数（避免反复请求被网络重置/限流）
        ak_module=None,
    ):
        self._intraday_ttl = intraday_ttl
        self._closed_ttl = closed_ttl
        self._non_trading_ttl = non_trading_ttl
        self._fail_cooldown = fail_cooldown
        self._ak_module = ak_module
        self._cache: dict[str, tuple[float, dict]] = {}
        self._fail_until: dict[str, float] = {}  # label -> 冷却截止(monotonic)
        self._inflight: dict[str, Future] = {}
        self._lock = threading.Lock()

    # ── 缓存控制 ────────────────────────────────────────────────

    def _ttl_seconds(self, trade_date: str) -> int:
        if not is_cn_trading_day(trade_date):
            return self._non_trading_ttl
        phase = cn_market_phase()
        if phase in ("in_session", "lunch_break"):
            return self._intraday_ttl
        return self._closed_ttl

    @staticmethod
    def _key(trade_date: str, perspective: str, include_breadth: bool) -> str:
        """缓存键必须包含数据形态，避免轻量结果冒充完整市场池。"""
        return f"{trade_date}|{perspective}|breadth={int(include_breadth)}"

    def get(
        self,
        trade_date: str,
        perspective: str = "short",
        *,
        include_breadth: bool = True,
    ) -> Optional[dict]:
        """返回缓存中的市场池（不触发抓取），无缓存返回 None。"""
        with self._lock:
            hit = self._cache.get(
                self._key(trade_date, perspective, include_breadth)
            )
            return hit[1] if hit is not None else None

    def collect(
        self,
        trade_date: str,
        perspective: str = "short",
        *,
        force: bool = False,
        include_breadth: bool = True,
    ) -> dict:
        """取市场级数据池：命中 TTL 缓存直接返回，否则抓取并缓存。

        - force=True 忽略 TTL 强制刷新；
        - include_breadth=False 跳过新浪全市场快照（约 20s，前端板块页可关闭）。
        """
        # 完整主线采集与板块页轻量采集的数据形态不同，不能共享缓存或
        # single-flight。否则轻量请求先发时，主线任务会等待同一个 Future，
        # 120 秒后抛 TimeoutError，且即便及时返回也会拿到缺少 breadth 的数据。
        key = self._key(trade_date, perspective, include_breadth)
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None and not force and (now - hit[0]) < self._ttl_seconds(trade_date):
                return hit[1]
            inflight = None if force else self._inflight.get(key)
            leader = False
            if inflight is None:
                inflight = Future()
                self._inflight[key] = inflight
                leader = True
        if not leader:
            return inflight.result(timeout=120)
        try:
            payload = self._fetch_market(trade_date, perspective, include_breadth=include_breadth)
            with self._lock:
                self._cache[key] = (time.monotonic(), payload)
            inflight.set_result(payload)
            return payload
        except Exception as exc:
            if not inflight.done():
                inflight.set_exception(exc)
            raise
        finally:
            with self._lock:
                if self._inflight.get(key) is inflight:
                    self._inflight.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()

    # ── 抓取 ────────────────────────────────────────────────────

    def _ak(self):
        if self._ak_module is not None:
            return self._ak_module
        import akshare as ak  # type: ignore

        return ak

    def _fetch_market(
        self, trade_date: str, perspective: str, *, include_breadth: bool = True
    ) -> dict:
        from .mainline_scoring import build_mainline_candidates

        ak = self._ak()
        sources: dict[str, Optional[str]] = {}
        warnings: list[str] = []

        def _safe(label: str, fn):
            """带失败冷却的兜底：失败后 cooldown 秒内不再重复请求该数据源。"""
            now = time.monotonic()
            with self._lock:
                if self._fail_until.get(label, 0.0) > now:
                    return None  # 冷却中：跳过请求（不再产生重复告警）
            try:
                result = fn()
                with self._lock:
                    self._fail_until.pop(label, None)
                return result
            except Exception as exc:
                with self._lock:
                    self._fail_until[label] = now + self._fail_cooldown
                sources[label] = None
                warnings.append(
                    f"{label} 不可用：{type(exc).__name__}: {str(exc)[:120]}"
                    f"（已进入失败冷却 {int(self._fail_cooldown)}s，期间不再重复请求）"
                )
                return None

        def _industry_spot():
            from .providers.cn_akshare_provider import fetch_board_spot_df_chain

            df, src = fetch_board_spot_df_chain(ak, "industry")
            sources["industry_spot"] = src
            return _df_to_rows(df, limit=30)

        def _concept_spot():
            from .providers.cn_akshare_provider import fetch_board_spot_df_chain

            df, src = fetch_board_spot_df_chain(ak, "concept")
            sources["concept_spot"] = src
            return _df_to_rows(df, limit=30)

        def _zt_heat():
            from .providers.cn_akshare_provider import fetch_zt_industry_heat_df

            df = fetch_zt_industry_heat_df(ak, trade_date)
            sources["zt_heat"] = "em"
            return _df_to_rows(df, limit=15)

        def _breadth():
            """市场宽度：东财全市场直连(push2delay，精确) → 新浪全市场 → THS 行业聚合（近似）。"""
            from .providers.cn_akshare_provider import (
                fetch_market_breadth_df,
                fetch_ths_industry_summary_df,
            )
            from .providers.cn_eastmoney_direct import fetch_em_market_breadth_direct

            # 1) 东财直连（push2delay，最可靠）
            try:
                b = fetch_em_market_breadth_direct()
                sources["breadth"] = "em"
                return b
            except Exception:
                pass
            # 2) 新浪全市场
            try:
                b = fetch_market_breadth_df(ak)
                sources["breadth"] = "sina"
                return b
            except Exception:
                pass
            # 3) 降级：THS 行业板块家数加总（行业互斥，总量接近全市场；flat 不可得）
            ths = fetch_ths_industry_summary_df(ak)
            up = int(pd.to_numeric(ths.get("up_count"), errors="coerce").sum()) if "up_count" in ths.columns else 0
            down = int(pd.to_numeric(ths.get("down_count"), errors="coerce").sum()) if "down_count" in ths.columns else 0
            sources["breadth"] = "ths_approx"
            return {
                "up": up,
                "down": down,
                "flat": None,
                "total": up + down,
                "as_of": None,
                "approximate": True,  # 前端可标注"近似"
            }

        def _benchmark():
            from .mainline_scoring import _default_fetch_benchmark

            df = _default_fetch_benchmark(ak)()
            sources["benchmark"] = "index"
            closes = df["close"].dropna().astype(float)
            out: dict[str, Optional[float]] = {"close": float(closes.iloc[-1]) if len(closes) else None}
            if len(closes) >= 2:
                out["chg_1d"] = float(closes.iloc[-1] / closes.iloc[-2] - 1)
            if len(closes) >= 6:
                out["chg_5d"] = float(closes.iloc[-1] / closes.iloc[-6] - 1)
            return out

        def _macro():
            from tradingagents.dataflows.config import get_config
            from tradingagents.methodology.macro_fetch import fetch_ashare_macro_pool_brief

            return fetch_ashare_macro_pool_brief(trade_date, get_config()) or ""

        tasks: dict[str, Any] = {
            "industry_spot": _industry_spot,
            "concept_spot": _concept_spot,
            "zt_heat": _zt_heat,
            "benchmark": _benchmark,
            "macro": _macro,
        }
        if include_breadth:
            tasks["breadth"] = _breadth

        results: dict[str, Any] = {}
        # akshare 全局只有 5 个槽；并行过多会把登录/状态查询一起堵死。
        with ThreadPoolExecutor(max_workers=min(2, len(tasks))) as executor:
            future_to_label = {executor.submit(_safe, label, fn): label for label, fn in tasks.items()}
            for future in future_to_label:
                label = future_to_label[future]
                results[label] = future.result()

        # 规则层主线候选（默认多源 fetchers，内部自带降级）
        try:
            candidates = build_mainline_candidates(
                trade_date, perspective=perspective, ak_module=ak
            )
            candidate_warnings = candidates.get("warnings", [])
        except Exception as exc:
            candidates = {}
            candidate_warnings = [f"主线候选计算失败：{type(exc).__name__}: {str(exc)[:120]}"]

        warnings.extend(candidate_warnings)

        return {
            "trade_date": trade_date,
            "perspective": perspective,
            "collected_at": datetime.now().isoformat(timespec="seconds"),
            "data_as_of": trade_date,
            "sources": sources,
            "warnings": warnings,
            "industry_spot": results.get("industry_spot") or [],
            "concept_spot": results.get("concept_spot"),
            "zt_heat": results.get("zt_heat") or [],
            "breadth": results.get("breadth"),
            "benchmark": results.get("benchmark"),
            "macro_brief": results.get("macro") or "",
            "emotion": candidates.get("emotion"),
            "market_median_chg_1d": candidates.get("market_median_chg_1d"),
            "benchmark_chg_5d": candidates.get("benchmark_chg_5d"),
            "mainline_candidates": candidates.get("boards", [])[:20],
            "spot_sources": candidates.get("spot_sources"),
            "candidate_warnings": candidate_warnings,
        }


_SHARED_COLLECTOR: Optional[MarketCollector] = None
_SHARED_COLLECTOR_GUARD = threading.Lock()


def get_shared_market_collector() -> MarketCollector:
    """Process-wide collector so the mainline page and job share TTL / single-flight."""
    global _SHARED_COLLECTOR
    with _SHARED_COLLECTOR_GUARD:
        if _SHARED_COLLECTOR is None:
            _SHARED_COLLECTOR = MarketCollector()
        return _SHARED_COLLECTOR
