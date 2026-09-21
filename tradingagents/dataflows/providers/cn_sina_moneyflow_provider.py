from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from ._http_utils import HttpRetry, http_get_json
from .cn_market_only_base import CnMarketOnlyProvider

_SINA_BOARD_FLOW_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "MoneyFlow.ssl_bkzj_bk"
)
_SINA_BOARD_FLOW_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "http://vip.stock.finance.sina.com.cn/moneyflow/",
    "Accept": "application/json,text/plain,*/*",
    "Connection": "close",
}


def fetch_sina_industry_board_fund_flow_df() -> pd.DataFrame:
    """Fetch Sina industry board fund-flow rankings (fenlei=0).

    Stable fallback when Eastmoney push2 endpoints are blocked or reset connections.
    """
    params = {
        "page": 1,
        "num": 100,
        "sort": "netamount",
        "asc": 0,
        "fenlei": 0,
    }
    obj = http_get_json(
        _SINA_BOARD_FLOW_URL,
        params=params,
        headers=_SINA_BOARD_FLOW_HEADERS,
        retry=HttpRetry(attempts=4, base_sleep_s=0.5, timeout_s=10.0),
    )
    if not isinstance(obj, list) or not obj:
        raise NotImplementedError("sina board fund flow empty")

    rows: list[dict[str, Any]] = []
    for item in obj:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        rows.append(
            {
                "名称": name,
                "今日涨跌幅": float(item.get("avg_changeratio") or 0),
                "今日主力净流入-净额": float(item.get("netamount") or 0),
                "今日主力净流入-净占比": float(item.get("ratioamount") or 0),
            }
        )
    if not rows:
        raise NotImplementedError("sina board fund flow parse failed")
    return pd.DataFrame(rows)


@dataclass(frozen=True)
class _MoneyflowRow:
    date: str  # YYYY-MM-DD
    main_net_yuan: float
    total_net_yuan: float


class CnSinaMoneyflowProvider(CnMarketOnlyProvider):
    """CN fund flow provider via Sina Finance public endpoint (free).

    Uses:
    - MoneyFlow.ssl_qsfx_zjlrqs: historical main net inflow series (r0_net) and total net (netamount).

    Notes:
    - Sina's response is valid JSON array (as of now), so parsing is stable.
    - This provider focuses on "主力净流向" trend (近5日) for Smart Money analyst.
    """

    RETRY = HttpRetry(attempts=4, base_sleep_s=0.5, timeout_s=10.0)

    @property
    def name(self) -> str:
        return "cn_sina_moneyflow"

    # ── Helpers ──
    @staticmethod
    def _normalize(symbol: str) -> str:
        import re

        m = re.search(r"(\d{6})", (symbol or "").strip())
        if not m:
            raise NotImplementedError(f"cn_sina_moneyflow only supports 6-digit symbols, got: {symbol}")
        code = m.group(1)
        prefix = "sh" if code.startswith(("5", "6", "9")) else "sz"
        return f"{prefix}{code}"

    @staticmethod
    def _safe_float(v: Any) -> float:
        try:
            return float(v)
        except Exception:
            return 0.0

    def _fetch_rows(self, symbol: str) -> list[_MoneyflowRow]:
        code = self._normalize(symbol)
        url = (
            "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            "MoneyFlow.ssl_qsfx_zjlrqs"
        )
        params = {"page": 1, "num": 5, "sort": "opendate", "asc": 0, "daima": code}
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://vip.stock.finance.sina.com.cn/",
            "Accept": "application/json,text/plain,*/*",
            "Connection": "close",
        }
        obj = http_get_json(url, params=params, headers=headers, retry=self.RETRY)
        if not isinstance(obj, list) or not obj:
            raise NotImplementedError(f"sina moneyflow empty for {symbol}")

        rows: list[_MoneyflowRow] = []
        for it in obj:
            if not isinstance(it, dict):
                continue
            d = str(it.get("opendate") or "").strip()
            if not d:
                continue
            rows.append(
                _MoneyflowRow(
                    date=d,
                    main_net_yuan=self._safe_float(it.get("r0_net")),
                    total_net_yuan=self._safe_float(it.get("netamount")),
                )
            )
        if not rows:
            raise NotImplementedError(f"sina moneyflow parse failed for {symbol}")
        return rows

    # ── CN market tool ──
    def get_individual_fund_flow(self, symbol: str) -> str:
        """Return last 5 trading days main net inflow series.

        Sina fields:
        - opendate: YYYY-MM-DD
        - r0_net: 主力净流入（元）
        - netamount: 全部净流入（元）
        """
        rows = self._fetch_rows(symbol)
        # API is typically desc by date; we show asc for readability.
        rows_sorted = sorted(rows, key=lambda r: r.date)
        cutoff = rows_sorted[-1].date

        header = "日期 主力净流向(亿元) 全部净流向(亿元)"
        lines = [header]
        for r in rows_sorted:
            lines.append(f"{r.date[5:]} {r.main_net_yuan/1e8:>10.2f} {r.total_net_yuan/1e8:>12.2f}")
        return f"{symbol} 近5日主力资金净流向（数据截止 {cutoff}）：\n" + "\n".join(lines)

    def get_board_fund_flow(self) -> str:
        """Return today's industry board fund-flow ranking via Sina."""
        from tradingagents.dataflows.providers.cn_akshare_provider import format_board_fund_flow_ranking

        df = fetch_sina_industry_board_fund_flow_df()
        return format_board_fund_flow_ranking(df)

