import re
import time
from datetime import datetime, timedelta

import pandas as pd
from stockstats import wrap

from .base import BaseMarketDataProvider


class CnAkshareProvider(BaseMarketDataProvider):
    """A-share provider backed by AkShare."""

    INDICATOR_DESCRIPTIONS = {
        "close_50_sma": (
            "50 SMA: A medium-term trend indicator. "
            "Usage: Identify trend direction and serve as dynamic support/resistance."
        ),
        "close_200_sma": (
            "200 SMA: A long-term trend benchmark. "
            "Usage: Confirm overall market trend and identify golden/death cross setups."
        ),
        "close_10_ema": (
            "10 EMA: A responsive short-term average. "
            "Usage: Capture quick shifts in momentum and potential entry points."
        ),
        "macd": "MACD momentum indicator.",
        "macds": "MACD signal line.",
        "macdh": "MACD histogram.",
        "rsi": "RSI momentum indicator.",
        "boll": "Bollinger middle band.",
        "boll_ub": "Bollinger upper band.",
        "boll_lb": "Bollinger lower band.",
        "atr": "Average True Range (ATR).",
        "vwma": "Volume weighted moving average (VWMA).",
        "mfi": "Money Flow Index (MFI).",
    }

    @property
    def name(self) -> str:
        return "cn_akshare"

    def _ak(self):
        try:
            import akshare as ak  # type: ignore
        except ImportError as exc:
            raise NotImplementedError(
                "cn_akshare requires 'akshare'. Install it with: pip install akshare"
            ) from exc
        return ak

    def _normalize_symbol(self, symbol: str) -> str:
        s = symbol.strip().lower()
        m = re.search(r"(\d{6})", s)
        if not m:
            raise NotImplementedError(
                f"cn_akshare only supports A-share 6-digit symbols, got: {symbol}"
            )
        return m.group(1)

    def _sina_symbol(self, symbol: str) -> str:
        code = self._normalize_symbol(symbol)
        if code.startswith(("6", "9")):
            return f"sh{code}"
        return f"sz{code}"

    def _format_ak_hist(self, df: pd.DataFrame, symbol: str, start: str, end: str) -> str:
        if df is None or df.empty:
            return f"No data found for symbol '{symbol}' between {start} and {end}"

        renamed = df.rename(
            columns={
                "日期": "Date",
                "开盘": "Open",
                "最高": "High",
                "最低": "Low",
                "收盘": "Close",
                "成交量": "Volume",
            }
        )
        out = renamed[["Date", "Open", "High", "Low", "Close", "Volume"]].copy()
        out["Dividends"] = 0.0
        out["Stock Splits"] = 0.0
        out["Date"] = pd.to_datetime(out["Date"]).dt.strftime("%Y-%m-%d")

        header = f"# Stock data for {symbol} from {start} to {end}\n"
        header += f"# Total records: {len(out)}\n"
        header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        return header + out.to_csv(index=False)

    def _fetch_hist_df(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        ak = self._ak()
        code = self._normalize_symbol(symbol)
        last_exc = None
        for i in range(3):
            try:
                return ak.stock_zh_a_hist(
                    symbol=code,
                    period="daily",
                    start_date=start_date.replace("-", ""),
                    end_date=end_date.replace("-", ""),
                    adjust="qfq",
                )
            except Exception as exc:
                last_exc = exc
                if i < 2:
                    time.sleep(0.6 * (i + 1))
        raise NotImplementedError(
            f"cn_akshare is temporarily unavailable for price history: {last_exc}"
        ) from last_exc

    def get_stock_data(self, symbol: str, start_date: str, end_date: str) -> str:
        df = self._fetch_hist_df(symbol, start_date, end_date)
        return self._format_ak_hist(df, symbol, start_date, end_date)

    def get_indicators(
        self, symbol: str, indicator: str, curr_date: str, look_back_days: int
    ) -> str:
        if indicator not in self.INDICATOR_DESCRIPTIONS:
            raise ValueError(
                f"Indicator {indicator} is not supported. "
                f"Please choose from: {list(self.INDICATOR_DESCRIPTIONS.keys())}"
            )

        curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
        start_dt = curr_dt - timedelta(days=max(look_back_days, 260))
        df = self._fetch_hist_df(symbol, start_dt.strftime("%Y-%m-%d"), curr_date)
        if df is None or df.empty:
            return f"No data found for {symbol} for indicator {indicator}"

        ind_df = df.rename(
            columns={
                "日期": "date",
                "开盘": "open",
                "最高": "high",
                "最低": "low",
                "收盘": "close",
                "成交量": "volume",
            }
        )[["date", "open", "high", "low", "close", "volume"]].copy()
        ind_df["date"] = pd.to_datetime(ind_df["date"])
        ind_df = ind_df.sort_values("date")

        ss = wrap(ind_df)
        _ = ss[indicator]

        values_by_date = {}
        for _, row in ss.iterrows():
            date_str = pd.to_datetime(row["date"]).strftime("%Y-%m-%d")
            val = row[indicator]
            values_by_date[date_str] = "N/A" if pd.isna(val) else str(val)

        begin = curr_dt - timedelta(days=look_back_days)
        lines = []
        d = curr_dt
        while d >= begin:
            key = d.strftime("%Y-%m-%d")
            lines.append(f"{key}: {values_by_date.get(key, 'N/A: Not a trading day (weekend or holiday)')}")
            d -= timedelta(days=1)

        result = (
            f"## {indicator} values from {begin.strftime('%Y-%m-%d')} to {curr_date}:\n\n"
            + "\n".join(lines)
            + "\n\n"
            + self.INDICATOR_DESCRIPTIONS[indicator]
        )
        return result

    def get_fundamentals(self, ticker: str, curr_date: str = None) -> str:
        ak = self._ak()
        code = self._normalize_symbol(ticker)
        try:
            df = ak.stock_individual_info_em(symbol=code)
            if df is None or df.empty:
                return f"No fundamentals data found for symbol '{ticker}'"
            return f"## Fundamentals for {ticker}\n\n{df.to_markdown(index=False)}"
        except Exception as exc:
            raise NotImplementedError(
                f"cn_akshare is temporarily unavailable for fundamentals: {exc}"
            ) from exc

    def _financial_report_sina(self, ticker: str, report_name: str) -> str:
        ak = self._ak()
        symbol = self._sina_symbol(ticker)
        try:
            df = ak.stock_financial_report_sina(stock=symbol, symbol=report_name)
            if df is None or df.empty:
                return f"No {report_name} data found for symbol '{ticker}'"
            return df.head(12).to_markdown(index=False)
        except Exception as exc:
            raise NotImplementedError(
                f"cn_akshare is temporarily unavailable for {report_name}: {exc}"
            ) from exc

    def get_balance_sheet(
        self, ticker: str, freq: str = "quarterly", curr_date: str = None
    ) -> str:
        table = self._financial_report_sina(ticker, "资产负债表")
        return f"## Balance Sheet ({ticker})\n\n{table}"

    def get_cashflow(
        self, ticker: str, freq: str = "quarterly", curr_date: str = None
    ) -> str:
        table = self._financial_report_sina(ticker, "现金流量表")
        return f"## Cashflow ({ticker})\n\n{table}"

    def get_income_statement(
        self, ticker: str, freq: str = "quarterly", curr_date: str = None
    ) -> str:
        table = self._financial_report_sina(ticker, "利润表")
        return f"## Income Statement ({ticker})\n\n{table}"

    def get_news(self, ticker: str, start_date: str, end_date: str) -> str:
        ak = self._ak()
        code = self._normalize_symbol(ticker)
        try:
            df = ak.stock_news_em(symbol=code)
            if df is None or df.empty:
                return f"No news found for {ticker}"

            date_col = "发布时间" if "发布时间" in df.columns else None
            if date_col is not None:
                df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
                start_dt = datetime.strptime(start_date, "%Y-%m-%d")
                end_dt = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
                df = df[(df[date_col] >= start_dt) & (df[date_col] < end_dt)]

            if df.empty:
                return f"No news found for {ticker} between {start_date} and {end_date}"

            rows = []
            for _, row in df.head(20).iterrows():
                title = str(row.get("新闻标题", row.get("标题", "No title")))
                src = str(row.get("文章来源", row.get("来源", "Unknown")))
                summary = str(row.get("新闻内容", row.get("内容", "")))
                link = str(row.get("新闻链接", row.get("链接", "")))
                rows.append(f"### {title} (source: {src})")
                if summary and summary != "nan":
                    rows.append(summary[:400])
                if link and link != "nan":
                    rows.append(f"Link: {link}")
                rows.append("")

            return f"## {ticker} News, from {start_date} to {end_date}:\n\n" + "\n".join(rows)
        except Exception as exc:
            raise NotImplementedError(
                f"cn_akshare is temporarily unavailable for news: {exc}"
            ) from exc

    def get_global_news(
        self, curr_date: str, look_back_days: int = 7, limit: int = 50
    ) -> str:
        ak = self._ak()
        try:
            if hasattr(ak, "news_cctv"):
                df = ak.news_cctv(date=curr_date.replace("-", ""))
                if df is None or df.empty:
                    return f"No global news found for {curr_date}"
                rows = []
                for _, row in df.head(limit).iterrows():
                    title = str(row.get("title", row.get("标题", "No title")))
                    content = str(row.get("content", row.get("内容", "")))
                    rows.append(f"### {title}")
                    if content and content != "nan":
                        rows.append(content[:300])
                    rows.append("")
                start = (
                    datetime.strptime(curr_date, "%Y-%m-%d") - timedelta(days=look_back_days)
                ).strftime("%Y-%m-%d")
                return f"## Global Market News, from {start} to {curr_date}:\n\n" + "\n".join(rows)
            return "Global news is not available in current cn_akshare implementation."
        except Exception as exc:
            raise NotImplementedError(
                f"cn_akshare is temporarily unavailable for global news: {exc}"
            ) from exc

    def get_insider_transactions(self, symbol: str) -> str:
        ak = self._ak()
        code = self._normalize_symbol(symbol)
        try:
            if hasattr(ak, "stock_ggcg_em"):
                df = ak.stock_ggcg_em(symbol=code)
                if df is None or df.empty:
                    return f"No insider transactions found for {symbol}"
                return f"## Insider Transactions for {symbol}\n\n{df.head(20).to_markdown(index=False)}"
            return "Insider transactions endpoint is not available in current AkShare version."
        except Exception as exc:
            raise NotImplementedError(
                f"cn_akshare is temporarily unavailable for insider transactions: {exc}"
            ) from exc
