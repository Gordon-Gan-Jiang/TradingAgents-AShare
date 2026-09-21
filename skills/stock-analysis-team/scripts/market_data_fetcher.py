#!/usr/bin/env python3
"""
股票市场数据获取脚本

功能：
- 获取行情与技术指标（MA、MACD、RSI 等）
- A 股默认走 AkShare（与主项目数据源一致），可通过 TA_STOCK_SKILL_CN_SOURCE=yfinance 切回 Yahoo
- 美股仍使用 yfinance

依赖：
- akshare（A 股）
- yfinance（美股）
- ta、pandas
"""

import argparse
import json
import math
import os
import random
import sys
import time
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
import yfinance as yf
import ta

# 与 api/main.py CN_INDEX_SYMBOL_MAP 一致，供 AkShare 指数日线
_CN_INDEX_EM_SYMBOL = {
    "000001.SH": "sh000001",
    "000001.SS": "sh000001",
    "399001.SZ": "sz399001",
    "399006.SZ": "sz399006",
    "000300.SH": "sh000300",
    "000688.SH": "sh000688",
    "000905.SH": "sh000905",
    "000852.SH": "sh000852",
    "899050.BJ": "bj899050",
}


def _cn_data_source() -> str:
    """akshare（默认）或 yfinance。"""
    return os.getenv("TA_STOCK_SKILL_CN_SOURCE", "akshare").strip().lower()


def _cn_sina_symbol(code: str) -> str:
    """与 cn_akshare_provider._sina_symbol 一致，供新浪/腾讯日线接口。"""
    if code.startswith(("5", "6", "9")):
        return f"sh{code}"
    return f"sz{code}"


def _ak_hist_max_retries() -> int:
    return max(1, int(os.getenv("TA_STOCK_SKILL_AK_RETRIES", "5")))


def _ak_hist_retry_base_sec() -> float:
    return float(os.getenv("TA_STOCK_SKILL_AK_RETRY_BASE_SEC", "0.8"))


def _fetch_cn_a_hist_raw(ak, code: str, ys: str, ye: str) -> tuple[pd.DataFrame | None, str | None]:
    """
    A 股日线：东财 stock_zh_a_hist（重试）→ 新浪 stock_zh_a_daily → 腾讯 stock_zh_a_hist_tx。
    东财常出现 RemoteDisconnected，多源与退避与主项目 cn_akshare 一致。
    """
    last_err: str | None = None
    n = _ak_hist_max_retries()
    base = _ak_hist_retry_base_sec()
    for attempt in range(n):
        try:
            raw = ak.stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=ys,
                end_date=ye,
                adjust="qfq",
            )
            if raw is not None and not raw.empty:
                return raw, None
            last_err = "stock_zh_a_hist returned empty"
        except Exception as e:
            last_err = str(e)
        if attempt < n - 1:
            time.sleep(base * (2**attempt) + random.uniform(0, 0.5))

    sina_sym = _cn_sina_symbol(code)
    try:
        raw = ak.stock_zh_a_daily(
            symbol=sina_sym,
            start_date=ys,
            end_date=ye,
            adjust="qfq",
        )
        if raw is not None and not raw.empty:
            return raw, None
    except Exception as e:
        last_err = str(e)

    try:
        raw = ak.stock_zh_a_hist_tx(
            symbol=sina_sym,
            start_date=ys,
            end_date=ye,
            adjust="qfq",
        )
        if raw is not None and not raw.empty:
            return raw, None
    except Exception as e:
        last_err = str(e)

    return None, last_err


def _period_to_dates(period: str) -> tuple[str, str]:
    """返回 (start_date, end_date) YYYY-MM-DD。"""
    end = datetime.now().date()
    if period == "1mo":
        delta_days = 31
    elif period == "3mo":
        delta_days = 93
    elif period == "6mo":
        delta_days = 186
    else:
        delta_days = 372
    start = end - timedelta(days=delta_days)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def _normalize_ak_ohlcv_df(raw: pd.DataFrame | None) -> pd.DataFrame:
    """统一为 Date + Open/High/Low/Close/Volume。"""
    if raw is None or raw.empty:
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
    df = raw.rename(columns=col_map).copy()
    need = ["Date", "Open", "High", "Low", "Close"]
    if any(c not in df.columns for c in need):
        return pd.DataFrame()
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df.dropna(subset=["Date"]).sort_values("Date")
    for c in ["Open", "High", "Low", "Close", "Volume"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    return df.reset_index(drop=True)


def _ohlcv_df_to_price_index(df: pd.DataFrame) -> pd.DataFrame:
    out = df.set_index("Date").sort_index()
    cols = ["Open", "High", "Low", "Close"]
    if "Volume" not in out.columns:
        out["Volume"] = 0.0
    return out[cols + ["Volume"]]


def _build_result_from_ohlcv(
    symbol: str,
    market: str,
    hist: pd.DataFrame,
    info: dict,
    data_source: str,
) -> dict:
    """hist: DatetimeIndex, columns Open High Low Close Volume"""
    if hist.empty:
        return {"error": f"无法获取股票 {symbol} 的数据，请检查股票代码是否正确"}

    df = hist.copy()

    df["MA5"] = df["Close"].rolling(window=5).mean()
    df["MA10"] = df["Close"].rolling(window=10).mean()
    df["MA20"] = df["Close"].rolling(window=20).mean()
    df["MA60"] = df["Close"].rolling(window=60).mean()

    df["MACD"] = ta.trend.MACD(df["Close"]).macd()
    df["MACD_Signal"] = ta.trend.MACD(df["Close"]).macd_signal()
    df["MACD_Diff"] = df["MACD"] - df["MACD_Signal"]

    df["RSI"] = ta.momentum.RSIIndicator(df["Close"]).rsi()

    bb = ta.volatility.BollingerBands(df["Close"])
    df["BB_Upper"] = bb.bollinger_hband()
    df["BB_Middle"] = bb.bollinger_mavg()
    df["BB_Lower"] = bb.bollinger_lband()

    df["Volume_MA5"] = df["Volume"].rolling(window=5).mean()
    df["Volume_MA10"] = df["Volume"].rolling(window=10).mean()

    latest = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else latest

    vol = latest["Volume"]
    vol_i = int(vol) if pd.notna(vol) else 0

    historical_data = []
    for idx, row in df.tail(30).iterrows():
        v = row["Volume"]
        historical_data.append(
            {
                "date": idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10],
                "open": round(float(row["Open"]), 2),
                "high": round(float(row["High"]), 2),
                "low": round(float(row["Low"]), 2),
                "close": round(float(row["Close"]), 2),
                "volume": int(v) if pd.notna(v) else 0,
            }
        )

    return {
        "symbol": symbol,
        "market": market,
        "data_source": data_source,
        "company_name": info.get("longName", "N/A"),
        "current_price": round(float(latest["Close"]), 2),
        "price_change": round(float(latest["Close"] - prev["Close"]), 2),
        "price_change_pct": round(
            float((latest["Close"] - prev["Close"]) / prev["Close"] * 100) if prev["Close"] else 0.0,
            2,
        ),
        "volume": vol_i,
        "high_52w": info.get("fiftyTwoWeekHigh", 0),
        "low_52w": info.get("fiftyTwoWeekLow", 0),
        "market_cap": info.get("marketCap", 0),
        "pe_ratio": info.get("trailingPE", 0),
        "pb_ratio": info.get("priceToBook", 0),
        "technical_indicators": {
            "MA5": round(latest["MA5"], 2) if pd.notna(latest["MA5"]) else None,
            "MA10": round(latest["MA10"], 2) if pd.notna(latest["MA10"]) else None,
            "MA20": round(latest["MA20"], 2) if pd.notna(latest["MA20"]) else None,
            "MA60": round(latest["MA60"], 2) if pd.notna(latest["MA60"]) else None,
            "MACD": round(latest["MACD"], 4) if pd.notna(latest["MACD"]) else None,
            "MACD_Signal": round(latest["MACD_Signal"], 4) if pd.notna(latest["MACD_Signal"]) else None,
            "MACD_Diff": round(latest["MACD_Diff"], 4) if pd.notna(latest["MACD_Diff"]) else None,
            "RSI": round(latest["RSI"], 2) if pd.notna(latest["RSI"]) else None,
            "BB_Upper": round(latest["BB_Upper"], 2) if pd.notna(latest["BB_Upper"]) else None,
            "BB_Middle": round(latest["BB_Middle"], 2) if pd.notna(latest["BB_Middle"]) else None,
            "BB_Lower": round(latest["BB_Lower"], 2) if pd.notna(latest["BB_Lower"]) else None,
        },
        "trend_analysis": {
            "multi_bullish": check_bullish_alignment(df),
            "ma_signal": get_ma_signal(latest),
            "macd_signal": "金叉"
            if latest["MACD_Diff"] > 0
            else "死叉"
            if pd.notna(latest["MACD_Diff"])
            else "信号不明",
            "rsi_signal": get_rsi_signal(latest["RSI"]) if pd.notna(latest["RSI"]) else "信号不明",
        },
        "historical_data": historical_data,
        "data_timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _ak_stock_meta(code: str) -> dict:
    """东财个股信息 → yfinance info 形状子集。"""
    try:
        import akshare as ak  # type: ignore

        df = ak.stock_individual_info_em(symbol=code)
        if df is None or df.empty or df.shape[1] < 2:
            return {}
        kv = dict(zip(df.iloc[:, 0].astype(str), df.iloc[:, 1]))
        name = kv.get("股票简称") or kv.get("证券简称") or kv.get("股票代码")
        out: dict = {"longName": name or "N/A"}
        for label, key in [
            ("总市值", "marketCap"),
            ("市盈率(动)", "trailingPE"),
            ("市净率", "priceToBook"),
            ("52周最高", "fiftyTwoWeekHigh"),
            ("52周最低", "fiftyTwoWeekLow"),
        ]:
            raw = kv.get(label)
            if raw is None or str(raw).strip() in ("", "-", "nan"):
                continue
            try:
                s = str(raw).replace(",", "").replace("亿", "e8").replace("万", "e4")
                if "e8" in s:
                    out[key] = float(s.replace("e8", "")) * 1e8
                elif "e4" in s:
                    out[key] = float(s.replace("e4", "")) * 1e4
                else:
                    out[key] = float(s)
            except (TypeError, ValueError):
                continue
        return out
    except Exception:
        return {}


def _get_stock_data_cn_akshare(symbol: str, period: str, interval: str) -> dict:
    if interval != "1d":
        return {"error": "A 股使用 AkShare 时暂仅支持日线 interval=1d"}
    try:
        import akshare as ak  # type: ignore
    except ImportError:
        return {"error": "未安装 akshare，请在项目根执行 uv sync"}

    start_s, end_s = _period_to_dates(period)
    ys, ye = start_s.replace("-", ""), end_s.replace("-", "")
    sym_key = symbol.upper().replace(".SS", ".SH")

    vendor = _CN_INDEX_EM_SYMBOL.get(sym_key)
    if vendor:
        raw = None
        try:
            raw = ak.stock_zh_index_daily_em(symbol=vendor, start_date=ys, end_date=ye)
        except Exception:
            try:
                raw = ak.stock_zh_index_daily(symbol=vendor)
            except Exception:
                raw = None
        norm = _normalize_ak_ohlcv_df(raw)
        if norm.empty:
            return {"error": f"无法获取指数 {symbol} 的 AkShare 数据"}
        d0, d1 = pd.to_datetime(start_s), pd.to_datetime(end_s)
        norm = norm[(norm["Date"] >= d0) & (norm["Date"] <= d1)]
        if norm.empty:
            return {"error": f"指数 {symbol} 在选定周期内无数据"}
        hist = _ohlcv_df_to_price_index(norm)
        idx_name = "上证指数" if "000001" in sym_key else sym_key
        info = {"longName": idx_name}
        hi = float(hist["High"].max()) if not hist.empty else 0
        lo = float(hist["Low"].min()) if not hist.empty else 0
        info["fiftyTwoWeekHigh"] = hi
        info["fiftyTwoWeekLow"] = lo
        return _build_result_from_ohlcv(symbol, "cn", hist, info, "akshare")

    code = sym_key.replace(".SH", "").replace(".SZ", "").replace(".BJ", "")
    if not (code.isdigit() and len(code) == 6):
        return {"error": f"A 股代码格式应为 600519.SH / 000001.SZ，收到: {symbol}"}

    raw, fetch_err = _fetch_cn_a_hist_raw(ak, code, ys, ye)
    if raw is None:
        hint = (
            " 可稍后重试，或设置 TA_STOCK_SKILL_CN_SOURCE=yfinance 走 Yahoo；"
            "亦可调大 TA_STOCK_SKILL_AK_RETRIES / TA_STOCK_SKILL_AK_RETRY_BASE_SEC。"
        )
        return {"error": f"获取数据失败（东财/新浪/腾讯）: {fetch_err}{hint}"}

    norm = _normalize_ak_ohlcv_df(raw)
    if norm.empty:
        return {"error": f"无法获取股票 {symbol} 的数据，请检查股票代码是否正确"}

    hist = _ohlcv_df_to_price_index(norm)
    meta = _ak_stock_meta(code)
    if not meta.get("longName") or meta.get("longName") == "N/A":
        meta["longName"] = symbol
    if "fiftyTwoWeekHigh" not in meta and not hist.empty:
        meta["fiftyTwoWeekHigh"] = float(hist["High"].tail(252).max())
    if "fiftyTwoWeekLow" not in meta and not hist.empty:
        meta["fiftyTwoWeekLow"] = float(hist["Low"].tail(252).min())
    return _build_result_from_ohlcv(symbol, "cn", hist, meta, "akshare")


def _is_rate_limit_error(msg: str) -> bool:
    s = str(msg).lower()
    return "too many requests" in s or "rate limit" in s or "429" in s


def _yf_max_retries() -> int:
    return max(1, int(os.getenv("YFINANCE_MAX_RETRIES", "5")))


def _yf_retry_base_sec() -> float:
    return float(os.getenv("YFINANCE_RETRY_BASE_SEC", "2.0"))


def _yf_after_history_sec() -> float:
    """history 与 info 各算一次请求，中间稍停可降低连击限流。"""
    return float(os.getenv("YFINANCE_AFTER_HISTORY_SEC", "1.2"))


def _yf_inter_market_sec() -> float:
    """大盘 both 模式下，沪/美两次拉取之间的间隔。"""
    return float(os.getenv("YFINANCE_INTER_MARKET_SEC", "3.0"))


def _download_yahoo_series(ticker: yf.Ticker, period: str, interval: str) -> pd.DataFrame:
    last_err: Exception | None = None
    n = _yf_max_retries()
    base = _yf_retry_base_sec()
    for attempt in range(n):
        try:
            hist = ticker.history(period=period, interval=interval)
            return hist
        except Exception as e:
            last_err = e
            if _is_rate_limit_error(str(e)) and attempt < n - 1:
                delay = base * (2**attempt) + random.uniform(0, 1.0)
                time.sleep(delay)
                continue
            raise
    raise last_err  # type: ignore[misc]


def _download_yahoo_info(ticker: yf.Ticker) -> dict:
    last_err: Exception | None = None
    n = _yf_max_retries()
    base = _yf_retry_base_sec()
    for attempt in range(n):
        try:
            return ticker.info or {}
        except Exception as e:
            last_err = e
            if _is_rate_limit_error(str(e)) and attempt < n - 1:
                delay = base * (2**attempt) + random.uniform(0, 1.0)
                time.sleep(delay)
                continue
            raise
    raise last_err  # type: ignore[misc]


def _get_stock_data_yfinance(symbol, market, period="1y", interval="1d"):
    """美股或显式指定 Yahoo 的 A 股。"""
    try:
        yf_symbol = symbol
        if market == "cn" and ".SH" in symbol:
            yf_symbol = symbol.replace(".SH", ".SS")
        elif market == "cn" and ".SZ" in symbol:
            yf_symbol = symbol.replace(".SZ", ".SZ")

        ticker = yf.Ticker(yf_symbol)
        hist = _download_yahoo_series(ticker, period, interval)

        if hist.empty:
            return {"error": f"无法获取股票 {symbol} 的数据，请检查股票代码是否正确"}

        time.sleep(_yf_after_history_sec())

        try:
            info = _download_yahoo_info(ticker)
        except Exception as e:
            if _is_rate_limit_error(str(e)):
                info = {}
            else:
                raise

        df = hist.copy()
        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index)
        for col in ("Open", "High", "Low", "Close", "Volume"):
            if col not in df.columns:
                return {"error": f"无法获取股票 {symbol} 的数据（缺少列 {col}）"}
        df = df[["Open", "High", "Low", "Close", "Volume"]]
        return _build_result_from_ohlcv(symbol, market, df, info, "yfinance")
    except Exception as e:
        return {"error": f"获取数据失败: {str(e)}"}


def get_stock_data(symbol, market, period="1y", interval="1d"):
    """
    获取股票数据。A 股默认 AkShare；环境变量 TA_STOCK_SKILL_CN_SOURCE=yfinance 可切回 Yahoo。
    """
    if market == "cn" and _cn_data_source() == "akshare":
        try:
            return _get_stock_data_cn_akshare(symbol, period, interval)
        except Exception as e:
            return {"error": f"获取数据失败: {str(e)}"}
    return _get_stock_data_yfinance(symbol, market, period, interval)


def check_bullish_alignment(df):
    """
    检查是否形成多头排列
    
    多头排列定义: MA5 > MA10 > MA20 > MA60
    """
    latest = df.iloc[-1]
    try:
        if (pd.notna(latest['MA5']) and pd.notna(latest['MA10']) and
            pd.notna(latest['MA20']) and pd.notna(latest['MA60'])):
            # pandas 链式比较得到 numpy.bool_，json.dumps 无法序列化
            return bool(
                latest["MA5"] > latest["MA10"] > latest["MA20"] > latest["MA60"]
            )
    except:
        return False
    return False


def get_ma_signal(latest):
    """
    获取均线信号
    """
    current = latest['Close']
    if pd.notna(latest['MA5']):
        if current > latest['MA5'] > latest['MA10']:
            return "强势"
        elif current < latest['MA5'] < latest['MA10']:
            return "弱势"
        elif current > latest['MA5']:
            return "反弹"
        else:
            return "调整"
    return "信号不明"


def get_rsi_signal(rsi):
    """
    获取RSI信号
    """
    if rsi > 70:
        return "超买"
    elif rsi < 30:
        return "超卖"
    elif rsi > 50:
        return "强势"
    else:
        return "弱势"


def get_market_index(market):
    """
    获取市场指数数据
    """
    try:
        sh_index = None
        sp500 = None
        if market == "cn" or market == "both":
            # 上证指数
            sh_index = get_stock_data("000001.SS", "cn", period="1mo")
        
        if market == "us" or market == "both":
            if market == "both":
                time.sleep(_yf_inter_market_sec())
            # 标普500
            sp500 = get_stock_data("^GSPC", "us", period="1mo")
        
        return {
            "cn_market": sh_index if market in ["cn", "both"] else None,
            "us_market": sp500 if market in ["us", "both"] else None
        }
    except Exception as e:
        return {"error": f"获取市场指数失败: {str(e)}"}


def _json_sanitize(o):
    """numpy 标量 / nan / inf 转为 JSON 可编码类型。"""
    if isinstance(o, dict):
        return {k: _json_sanitize(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_json_sanitize(v) for v in o]
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating, float)):
        v = float(o)
        return None if not math.isfinite(v) else v
    if isinstance(o, str) or o is None:
        return o
    if isinstance(o, (bool, int)):
        return o
    return o


def main():
    parser = argparse.ArgumentParser(description='获取股票市场数据')
    parser.add_argument('--symbol', type=str, help='股票代码（如 600519.SH 或 AAPL）')
    parser.add_argument('--market', type=str, choices=['cn', 'us', 'both'], help='市场类型（cn=A股, us=美股, both=双市）')
    parser.add_argument('--mode', type=str, choices=['stock', 'market', 'both'], default='stock', 
                       help='获取模式（stock=个股, market=大盘, both=两者）')
    parser.add_argument('--period', type=str, default='1y', help='数据周期（1y, 6mo, 3mo）')
    parser.add_argument('--interval', type=str, default='1d', help='数据间隔（1d=日线, 1h=小时线）')
    
    args = parser.parse_args()
    
    # 如果获取大盘数据
    if args.mode in ['market', 'both']:
        market_data = get_market_index(args.market)
        print(json.dumps(_json_sanitize(market_data), indent=2, ensure_ascii=False))

    # 如果获取个股数据
    if args.mode in ['stock', 'both']:
        if not args.symbol:
            print(json.dumps({"error": "个股模式需要提供 --symbol 参数"}, indent=2, ensure_ascii=False))
            sys.exit(1)

        stock_data = get_stock_data(args.symbol, args.market, args.period, args.interval)
        print(json.dumps(_json_sanitize(stock_data), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
