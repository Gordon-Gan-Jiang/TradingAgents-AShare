"""Invoke skills/stock-analysis-team scripts in a subprocess (charts + enhanced HTML report)."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SKILL_ROOT = _REPO_ROOT / "skills" / "stock-analysis-team"
_CHART_SCRIPT = _SKILL_ROOT / "scripts" / "chart_generator.py"
_ENHANCED_REPORT_SCRIPT = _SKILL_ROOT / "scripts" / "enhanced_html_report_generator.py"

_PERIOD_ALLOW = frozenset({"1y", "6mo", "3mo", "1mo"})
_CHART_TYPE_ALLOW = frozenset({"price", "candlestick", "macd", "rsi", "all"})


def validate_symbol(symbol: str) -> bool:
    s = symbol.strip()
    if not s or len(s) > 32:
        return False
    if re.match(r"^\d{6}\.(SH|SZ)$", s, re.I):
        return True
    if re.match(r"^[A-Z][A-Z0-9.-]{0,15}$", s, re.I):
        return True
    return False


def run_chart_generator(
    *,
    symbol: str,
    market: Literal["cn", "us"],
    period: str = "6mo",
    chart_type: Literal["price", "candlestick", "macd", "rsi", "all"] = "all",
) -> dict[str, Any]:
    """Run chart_generator.py and return parsed JSON payload."""
    if not _CHART_SCRIPT.is_file():
        return {"error": "skill_not_installed", "detail": str(_CHART_SCRIPT)}
    if not validate_symbol(symbol):
        raise ValueError("invalid_symbol")
    if period not in _PERIOD_ALLOW:
        raise ValueError("invalid_period")
    if chart_type not in _CHART_TYPE_ALLOW:
        raise ValueError("invalid_chart_type")

    output_dir = _make_skill_output_dir("charts")
    cmd = [
        sys.executable,
        str(_CHART_SCRIPT),
        "--symbol",
        symbol,
        "--market",
        market,
        "--period",
        period,
        "--chart-type",
        chart_type,
        "--output-dir",
        str(output_dir),
    ]
    timeout = float(os.getenv("TA_STOCK_SKILL_CHART_TIMEOUT", "200"))
    data = _run_script_json(cmd=cmd, timeout=timeout)
    if isinstance(data, dict):
        data.setdefault("output_dir", str(output_dir))
    return data


def generate_enhanced_html_from_report(
    *,
    report: Any,
    stock_name: str | None = None,
    market: Literal["cn", "us"] = "cn",
    period: str = "6mo",
    include_charts: bool = True,
) -> dict[str, Any]:
    """Generate enhanced stock-analysis-team HTML report from ReportDB-like object."""
    if not _ENHANCED_REPORT_SCRIPT.is_file():
        return {"error": "skill_not_installed", "detail": str(_ENHANCED_REPORT_SCRIPT)}
    symbol = str(getattr(report, "symbol", "") or "").strip().upper()
    if not validate_symbol(symbol):
        raise ValueError("invalid_symbol")
    if period not in _PERIOD_ALLOW:
        raise ValueError("invalid_period")

    work_dir = _make_skill_output_dir("enhanced")
    charts_dir = work_dir / "charts"
    charts_dir.mkdir(parents=True, exist_ok=True)
    chart_result: dict[str, Any] | None = None
    if include_charts:
        chart_result = run_chart_generator(symbol=symbol, market=market, period=period, chart_type="all")
        if isinstance(chart_result, dict) and not chart_result.get("error"):
            # chart script may write into its own output dir; prefer that if provided
            out = chart_result.get("output_dir")
            if out:
                charts_dir = Path(str(out))

    payload = _build_enhanced_report_payload(report=report, stock_name=stock_name)
    payload_file = work_dir / "report_data.json"
    output_file = work_dir / f"{symbol}_{getattr(report, 'trade_date', '')}_enhanced.html"
    payload_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    cmd = [
        sys.executable,
        str(_ENHANCED_REPORT_SCRIPT),
        "--data",
        str(payload_file),
        "--charts-dir",
        str(charts_dir),
        "--output",
        str(output_file),
    ]
    timeout = float(os.getenv("TA_STOCK_SKILL_REPORT_TIMEOUT", "220"))
    result = _run_script_json(cmd=cmd, timeout=timeout)
    if isinstance(result, dict) and result.get("success"):
        try:
            html = output_file.read_text(encoding="utf-8")
        except OSError:
            html = ""
        result["html"] = html
        result["output_file"] = str(output_file)
        if chart_result is not None:
            result["charts"] = chart_result
    return result


def _make_skill_output_dir(kind: str) -> Path:
    root = Path(os.getenv("TA_STOCK_SKILL_OUTPUT_DIR", str(_REPO_ROOT / "results" / "stock-analysis-team")))
    root.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix=f"{kind}_", dir=str(root)))
    return path


def _run_script_json(*, cmd: list[str], timeout: float) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(_SKILL_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"error": "timeout", "detail": f"exceeded {timeout}s"}

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        return {
            "error": "script_failed",
            "returncode": proc.returncode,
            "stderr": err[-2000:] if err else None,
            "stdout": out[-2000:] if out else None,
        }
    try:
        data = json.loads(out) if out else {}
    except json.JSONDecodeError:
        data = {"error": "invalid_json", "stdout": out[:2000] if out else None}
    return data if isinstance(data, dict) else {"data": data}


def _build_enhanced_report_payload(*, report: Any, stock_name: str | None = None) -> dict[str, Any]:
    symbol = str(getattr(report, "symbol", "") or "").strip().upper()
    decision = str(getattr(report, "decision", "") or "持有")
    target_price = getattr(report, "target_price", None)
    stop_loss_price = getattr(report, "stop_loss_price", None)
    confidence = getattr(report, "confidence", None)
    risk_items = getattr(report, "risk_items", None) or []
    key_metrics = getattr(report, "key_metrics", None) or []
    risk_score = _estimate_risk_score(risk_items)

    fundamentals = str(getattr(report, "fundamentals_report", "") or "").strip()
    market_report = str(getattr(report, "market_report", "") or "").strip()
    sentiment_report = str(getattr(report, "sentiment_report", "") or "").strip()
    news_report = str(getattr(report, "news_report", "") or "").strip()
    final_decision = str(getattr(report, "final_trade_decision", "") or "").strip()

    return {
        "company_name": stock_name or symbol,
        "symbol": symbol,
        "analysis_date": str(getattr(report, "trade_date", "") or ""),
        "risk_level": _risk_level_text(risk_score),
        "risk_score": risk_score,
        "one_line_summary": (final_decision[:120] if final_decision else f"综合判断：{decision}"),
        "recommendation": decision,
        "expected_return": _format_expected_return(confidence),
        "max_risk": _format_max_risk(stop_loss_price),
        "industry": "N/A",
        "market_cap": "N/A",
        "float_cap": "N/A",
        "current_price": "N/A",
        "high_52w": "N/A",
        "low_52w": "N/A",
        "fundamental_score": _estimate_fundamental_score(key_metrics),
        "overall_risk_score": risk_score,
        "financial_indicators": _metrics_to_financial_rows(key_metrics),
        "charts": [],
        "fundamental_details": [
            {"dimension": "基本面报告", "score": _estimate_fundamental_score(key_metrics), "comment": fundamentals[:200] or "暂无"}
        ],
        "risk_details": _risk_rows(risk_items),
        "trading_points": [
            {"action": decision or "持有", "price": target_price if target_price is not None else "N/A", "position": "按策略控制仓位"},
            {"action": "止损", "price": stop_loss_price if stop_loss_price is not None else "N/A", "position": "触发条件严格执行"},
        ],
        "sentiment_indicators": [
            {"metric": "情绪评分", "value": "中性", "percentile": "N/A", "signal": "中性"},
            {"metric": "新闻影响", "value": "混合", "percentile": "N/A", "signal": "中性"},
        ],
        "product_images": [],
        "price_dates": [],
        "price_values": [],
        "ma5_values": [],
        "ma20_values": [],
        "macd_values": [],
        "macd_signal_values": [],
        "rsi_values": [],
        "fundamental_dates": [],
        "revenue_values": [],
        "profit_values": [],
        "sentiment_dates": [],
        "sentiment_values": [],
        "analysis_sections": {
            "fundamentals_report": fundamentals,
            "market_report": market_report,
            "sentiment_report": sentiment_report,
            "news_report": news_report,
            "final_trade_decision": final_decision,
        },
    }


def _estimate_risk_score(risk_items: list[Any]) -> int:
    if not risk_items:
        return 5
    level_to_score = {"low": 3, "medium": 6, "high": 8}
    vals: list[int] = []
    for item in risk_items:
        if isinstance(item, dict):
            level = str(item.get("level") or "medium").lower()
            vals.append(level_to_score.get(level, 6))
    if not vals:
        return 5
    return max(1, min(10, int(round(sum(vals) / len(vals)))))


def _risk_level_text(score: int) -> str:
    if score <= 3:
        return "低风险"
    if score <= 6:
        return "中等风险"
    return "高风险"


def _estimate_fundamental_score(key_metrics: list[Any]) -> int:
    if not key_metrics:
        return 5
    score = 5
    for item in key_metrics:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "neutral").lower()
        if status == "good":
            score += 1
        elif status == "bad":
            score -= 1
    return max(1, min(10, score))


def _metrics_to_financial_rows(key_metrics: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in key_metrics[:8]:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "metric": item.get("name") or "指标",
                "value": item.get("value") or "N/A",
                "industry_avg": "N/A",
                "rating": _status_to_rating(str(item.get("status") or "neutral")),
            }
        )
    if not out:
        out.append({"metric": "关键指标", "value": "暂无", "industry_avg": "N/A", "rating": "中性"})
    return out


def _status_to_rating(status: str) -> str:
    s = status.lower()
    if s == "good":
        return "积极"
    if s == "bad":
        return "消极"
    return "中性"


def _risk_rows(risk_items: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in risk_items[:6]:
        if not isinstance(item, dict):
            continue
        level = str(item.get("level") or "medium").lower()
        base = {"low": 3, "medium": 6, "high": 8}.get(level, 6)
        out.append(
            {
                "dimension": str(item.get("name") or "风险项"),
                "score": base,
                "comment": str(item.get("description") or "需持续跟踪"),
            }
        )
    if not out:
        out.append({"dimension": "综合风险", "score": 5, "comment": "暂无结构化风险项，按中性处理"})
    return out


def _format_expected_return(confidence: Any) -> str:
    try:
        c = float(confidence)
    except Exception:
        return "+0%"
    value = max(0.0, min(30.0, c / 5.0))
    return f"+{value:.1f}%"


def _format_max_risk(stop_loss_price: Any) -> str:
    if stop_loss_price is None:
        return "-8.0%"
    try:
        float(stop_loss_price)
        return "以止损价为准"
    except Exception:
        return "-8.0%"
