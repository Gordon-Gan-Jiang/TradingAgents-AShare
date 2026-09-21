from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from ._http_utils import HttpRetry, http_get_json
from .cn_market_only_base import CnMarketOnlyProvider
from ..trade_calendar import cn_market_phase, cn_today_str, is_cn_trading_day, previous_cn_trading_day


@dataclass(frozen=True)
class _Retry:
    attempts: int = 6
    base_sleep_s: float = 0.6
    timeout_s: float = 10.0


class CnEastmoneyHttpProvider(CnMarketOnlyProvider):
    """CN market data provider via Eastmoney public HTTP endpoints (free).

    Focus: fund flow and (optional) billboards, intended as a robust fallback when AkShare
    wrapper endpoints are blocked/changed.
    """

    RETRY = HttpRetry(attempts=6, base_sleep_s=0.6, timeout_s=10.0)
    HOSTS = ("https://push2.eastmoney.com", "https://push2his.eastmoney.com")
    PATH = "/api/qt/stock/fflow/kline/get"

    @property
    def name(self) -> str:
        return "cn_eastmoney_http"

    # ── Helpers ──────────────────────────────────────────────────────────────
    @staticmethod
    def _normalize_6digit(symbol: str) -> str:
        import re

        m = re.search(r"(\d{6})", (symbol or "").strip())
        if not m:
            raise NotImplementedError(f"cn_eastmoney_http only supports 6-digit A-share codes, got: {symbol}")
        return m.group(1)

    @staticmethod
    def _secid(symbol: str) -> str:
        code = CnEastmoneyHttpProvider._normalize_6digit(symbol)
        market = "1" if code.startswith(("5", "6", "9")) else "0"
        return f"{market}.{code}"

    @staticmethod
    def _safe_float(v: Any) -> float:
        try:
            return float(v)
        except Exception:
            return 0.0

    def _fetch_klines(self, symbol: str) -> list[str]:
        secid = self._secid(symbol)
        params = {
            "secid": secid,
            "klt": 101,  # daily
            "lmt": 5,
            "fields1": "f1,f2,f3,f7",
            "fields2": "f51,f52,f53,f54,f55,f56",
            "ut": "fa5fd1943c7b386f172d6893dbfba10b",
            "_": int(time.time() * 1000),
        }
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://quote.eastmoney.com/",
            "Accept": "application/json,text/plain,*/*",
            "Connection": "close",
        }

        last_exc: Exception | None = None
        for host in self.HOSTS:
            try:
                obj = http_get_json(host + self.PATH, params=params, headers=headers, retry=self.RETRY)
                data = obj.get("data") if isinstance(obj, dict) else None
                klines = (data or {}).get("klines") if isinstance(data, dict) else None
                if not isinstance(klines, list) or not klines:
                    raise NotImplementedError(f"eastmoney fund flow empty for {symbol} ({secid})")
                return [str(x) for x in klines]
            except Exception as exc:
                last_exc = exc
                continue
        raise RuntimeError(f"eastmoney fund flow failed on all hosts: {last_exc}")

    # ── CN market tools ──────────────────────────────────────────────────────
    def get_individual_fund_flow(self, symbol: str) -> str:
        """Fetch last 5 days main fund flow (Eastmoney push2).

        Endpoint returns `klines` rows:
        YYYY-MM-DD,主力净流入,小单净流入,中单净流入,大单净流入,超大单净流入
        Values are in Yuan.
        """
        klines = self._fetch_klines(symbol)

        rows: list[dict[str, Any]] = []
        for line in klines:
            parts = line.split(",")
            if len(parts) < 6:
                continue
            d, main_in, small_in, mid_in, large_in, xl_in = parts[:6]
            rows.append(
                {
                    "日期": d,
                    "主力净流入": self._safe_float(main_in),
                    "小单净流入": self._safe_float(small_in),
                    "中单净流入": self._safe_float(mid_in),
                    "大单净流入": self._safe_float(large_in),
                    "超大单净流入": self._safe_float(xl_in),
                }
            )

        if not rows:
            raise NotImplementedError(f"eastmoney fund flow parse failed for {symbol}")
        # Sort by date ascending
        rows = sorted(rows, key=lambda r: r["日期"])
        cutoff = rows[-1]["日期"]

        to_yi = 1e8
        header = "日期 主力净流向(亿元) 超大单(亿元) 大单(亿元) 中单(亿元) 小单(亿元)"
        lines = [header]
        for r in rows:
            d = str(r["日期"])
            lines.append(
                f"{d[5:]} "
                f"{r['主力净流入']/to_yi:>12.2f} "
                f"{r['超大单净流入']/to_yi:>10.2f} "
                f"{r['大单净流入']/to_yi:>9.2f} "
                f"{r['中单净流入']/to_yi:>9.2f} "
                f"{r['小单净流入']/to_yi:>9.2f}"
            )
        today = cn_today_str()
        expected = today
        if not is_cn_trading_day(today):
            expected = previous_cn_trading_day(today)
        else:
            phase = cn_market_phase()
            if phase != "post_close":
                expected = previous_cn_trading_day(today)

        lag_note = ""
        if cutoff < expected:
            lag_note = f"（提示：数据源尚未更新至 {expected}）"

        return (
            f"{symbol} 近5日主力资金净流向（数据截止 {cutoff}）{lag_note}：\n"
            + "\n".join(lines)
        )

