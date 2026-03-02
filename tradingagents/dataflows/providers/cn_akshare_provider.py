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
            "50 日均线（SMA）：中期趋势指标。"
            "用途：识别趋势方向，并作为动态支撑/阻力参考。"
        ),
        "close_200_sma": (
            "200 日均线（SMA）：长期趋势基准。"
            "用途：确认大级别趋势，并辅助识别金叉/死叉结构。"
        ),
        "close_10_ema": (
            "10 日指数均线（EMA）：短期响应更快。"
            "用途：捕捉短线动量变化与潜在入场时机。"
        ),
        "macd": "MACD：趋势与动量综合指标。",
        "macds": "MACD 信号线（Signal）。",
        "macdh": "MACD 柱状图（Histogram）。",
        "rsi": "RSI：衡量超买/超卖的动量指标。",
        "boll": "布林中轨（20 日均线）。",
        "boll_ub": "布林上轨。",
        "boll_lb": "布林下轨。",
        "atr": "ATR：真实波动幅度均值，用于波动与风控。",
        "vwma": "VWMA：成交量加权均线。",
        "mfi": "MFI：资金流量指标。",
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

    def _normalize_hist_df(self, raw_df: pd.DataFrame) -> pd.DataFrame:
        if raw_df is None or raw_df.empty:
            return pd.DataFrame()

        col_map = {
            "日期": "Date",
            "date": "Date",
            "Date": "Date",
            "开盘": "Open",
            "open": "Open",
            "Open": "Open",
            "最高": "High",
            "high": "High",
            "High": "High",
            "最低": "Low",
            "low": "Low",
            "Low": "Low",
            "收盘": "Close",
            "close": "Close",
            "Close": "Close",
            "成交量": "Volume",
            "volume": "Volume",
            "Volume": "Volume",
        }
        df = raw_df.rename(columns=col_map).copy()
        required = ["Date", "Open", "High", "Low", "Close", "Volume"]
        missing = [c for c in required if c not in df.columns]
        if missing:
            raise ValueError(f"hist dataframe missing columns: {missing}")

        out = df[required].copy()
        out["Date"] = pd.to_datetime(out["Date"], errors="coerce")
        out = out.dropna(subset=["Date"]).sort_values("Date")

        for c in ["Open", "High", "Low", "Close", "Volume"]:
            out[c] = pd.to_numeric(out[c], errors="coerce")
        out = out.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
        out["Volume"] = out["Volume"].astype(float)

        return out

    def _format_ak_hist(self, df: pd.DataFrame, symbol: str, start: str, end: str) -> str:
        if df is None or df.empty:
            return f"No data found for symbol '{symbol}' between {start} and {end}"
        out = self._normalize_hist_df(df)
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
        symbol_with_market = self._sina_symbol(symbol)
        start_yyyymmdd = start_date.replace("-", "")
        end_yyyymmdd = end_date.replace("-", "")

        # Source 1: Eastmoney (default)
        em_last_exc = None
        for i in range(2):
            try:
                df = ak.stock_zh_a_hist(
                    symbol=code,
                    period="daily",
                    start_date=start_yyyymmdd,
                    end_date=end_yyyymmdd,
                    adjust="qfq",
                )
                return self._normalize_hist_df(df)
            except Exception as exc:
                em_last_exc = exc
                if i < 1:
                    time.sleep(0.6 * (i + 1))

        # Source 2: Sina
        try:
            df = ak.stock_zh_a_daily(
                symbol=symbol_with_market,
                start_date=start_yyyymmdd,
                end_date=end_yyyymmdd,
                adjust="qfq",
            )
            return self._normalize_hist_df(df)
        except Exception:
            pass

        # Source 3: Tencent
        try:
            df = ak.stock_zh_a_hist_tx(
                symbol=symbol_with_market,
                start_date=start_yyyymmdd,
                end_date=end_yyyymmdd,
                adjust="qfq",
            )
            return self._normalize_hist_df(df)
        except Exception:
            pass

        raise NotImplementedError(
            f"cn_akshare is temporarily unavailable for price history (eastmoney/sina/tencent all failed): {em_last_exc}"
        ) from em_last_exc

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
                "Date": "date",
                "Open": "open",
                "High": "high",
                "Low": "low",
                "Close": "close",
                "Volume": "volume",
            }
        )[["date", "open", "high", "low", "close", "volume"]].copy()
        ind_df["date"] = pd.to_datetime(ind_df["date"], errors="coerce")
        ind_df = ind_df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)

        ss = wrap(ind_df)
        indicator_series = ss[indicator]

        values_by_date = {}
        for idx, dt_val in enumerate(ind_df["date"]):
            date_str = pd.to_datetime(dt_val).strftime("%Y-%m-%d")
            val = indicator_series.iloc[idx]
            values_by_date[date_str] = "N/A" if pd.isna(val) else str(val)

        begin = curr_dt - timedelta(days=look_back_days)
        lines = []
        d = curr_dt
        while d >= begin:
            key = d.strftime("%Y-%m-%d")
            lines.append(f"{key}: {values_by_date.get(key, 'N/A：该日期暂无数据（可能未收盘、数据延迟或非交易日）')}")
            d -= timedelta(days=1)

        result = (
            f"## {indicator} 指标值（{begin.strftime('%Y-%m-%d')} 至 {curr_date}）：\n\n"
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

            return f"## {ticker} 新闻（{start_date} 至 {end_date}）：\n\n" + "\n".join(rows)
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
                    return f"{curr_date} 未获取到全球市场新闻"
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
                return f"## 全球市场新闻（{start} 至 {curr_date}）：\n\n" + "\n".join(rows)
            return "当前 cn_akshare 实现暂不支持全球新闻接口。"
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
