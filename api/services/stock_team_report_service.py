from __future__ import annotations

from html import escape
from pathlib import Path
from typing import Any

from api.database import ReportDB

_ROOT = Path(__file__).resolve().parents[2]
_STOCK_TEAM_TEMPLATE = _ROOT / "skills" / "stock-analysis-team" / "references" / "html-template.html"


def render_html_report(report: ReportDB, *, stock_name: str | None = None) -> str:
    payload = _build_payload(report, stock_name=stock_name)
    template = _load_template()
    if template:
        return _render_template(template, payload)
    return _render_fallback(payload)


def _build_payload(report: ReportDB, *, stock_name: str | None = None) -> dict[str, Any]:
    symbol = str(getattr(report, "symbol", "") or "")
    return {
        "title": f"{stock_name or symbol}（{symbol}）分析报告",
        "symbol": symbol,
        "name": stock_name or symbol,
        "trade_date": str(getattr(report, "trade_date", "") or ""),
        "decision": str(getattr(report, "decision", "") or "-"),
        "direction": str(getattr(report, "direction", "") or "-"),
        "confidence": getattr(report, "confidence", None),
        "target_price": getattr(report, "target_price", None),
        "stop_loss_price": getattr(report, "stop_loss_price", None),
        "fundamentals_report": getattr(report, "fundamentals_report", "") or "",
        "news_report": getattr(report, "news_report", "") or "",
        "market_report": getattr(report, "market_report", "") or "",
        "sentiment_report": getattr(report, "sentiment_report", "") or "",
        "trader_plan": getattr(report, "trader_investment_plan", "") or "",
        "final_decision": getattr(report, "final_trade_decision", "") or "",
        "risk_items": getattr(report, "risk_items", []) or [],
        "key_metrics": getattr(report, "key_metrics", []) or [],
        "analyst_traces": getattr(report, "analyst_traces", []) or [],
    }


def _load_template() -> str:
    try:
        if _STOCK_TEAM_TEMPLATE.is_file():
            return _STOCK_TEAM_TEMPLATE.read_text(encoding="utf-8")
    except OSError:
        return ""
    return ""


def _render_template(template: str, payload: dict[str, Any]) -> str:
    html = template
    html = html.replace("{{title}}", escape(payload["title"]))
    html = html.replace("{{stock_symbol}}", escape(payload["symbol"]))
    html = html.replace("{{stock_name}}", escape(payload["name"]))
    html = html.replace("{{trade_date}}", escape(payload["trade_date"]))
    html = html.replace("{{decision}}", escape(payload["decision"]))
    html = html.replace("{{direction}}", escape(payload["direction"]))
    html = html.replace("{{confidence}}", _safe_text(payload["confidence"]))
    html = html.replace("{{target_price}}", _safe_text(payload["target_price"]))
    html = html.replace("{{stop_loss_price}}", _safe_text(payload["stop_loss_price"]))
    html = html.replace("{{fundamentals_report}}", _as_pre(payload["fundamentals_report"]))
    html = html.replace("{{news_report}}", _as_pre(payload["news_report"]))
    html = html.replace("{{market_report}}", _as_pre(payload["market_report"]))
    html = html.replace("{{sentiment_report}}", _as_pre(payload["sentiment_report"]))
    html = html.replace("{{trader_plan}}", _as_pre(payload["trader_plan"]))
    html = html.replace("{{final_decision}}", _as_pre(payload["final_decision"]))
    html = html.replace("{{risk_items}}", _as_ul(payload["risk_items"]))
    html = html.replace("{{key_metrics}}", _as_ul(payload["key_metrics"]))
    html = html.replace("{{analyst_traces}}", _as_ul(payload["analyst_traces"]))
    return html


def _render_fallback(payload: dict[str, Any]) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{escape(payload["title"])}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif; max-width: 980px; margin: 24px auto; padding: 0 16px; color: #222; }}
    h1, h2 {{ margin: 8px 0; }}
    .meta {{ display: grid; grid-template-columns: repeat(3, minmax(120px, 1fr)); gap: 12px; margin: 12px 0 20px; }}
    .card {{ border: 1px solid #e5e7eb; border-radius: 10px; padding: 12px; background: #fff; }}
    pre {{ white-space: pre-wrap; word-wrap: break-word; margin: 0; }}
    ul {{ margin-top: 4px; }}
  </style>
</head>
<body>
  <h1>{escape(payload["title"])}</h1>
  <div class="meta">
    <div class="card"><strong>日期</strong><div>{escape(payload["trade_date"])}</div></div>
    <div class="card"><strong>决策</strong><div>{escape(payload["decision"])}</div></div>
    <div class="card"><strong>方向</strong><div>{escape(payload["direction"])}</div></div>
    <div class="card"><strong>置信度</strong><div>{_safe_text(payload["confidence"])}</div></div>
    <div class="card"><strong>目标价</strong><div>{_safe_text(payload["target_price"])}</div></div>
    <div class="card"><strong>止损价</strong><div>{_safe_text(payload["stop_loss_price"])}</div></div>
  </div>
  <h2>关键指标</h2>{_as_ul(payload["key_metrics"])}
  <h2>主要风险</h2>{_as_ul(payload["risk_items"])}
  <h2>基本面</h2>{_as_pre(payload["fundamentals_report"])}
  <h2>新闻事件</h2>{_as_pre(payload["news_report"])}
  <h2>市场技术面</h2>{_as_pre(payload["market_report"])}
  <h2>情绪面</h2>{_as_pre(payload["sentiment_report"])}
  <h2>交易计划</h2>{_as_pre(payload["trader_plan"])}
  <h2>最终结论</h2>{_as_pre(payload["final_decision"])}
</body>
</html>"""


def _safe_text(value: Any) -> str:
    if value is None:
        return "-"
    return escape(str(value))


def _as_pre(text: str) -> str:
    if not text:
        return "<pre>-</pre>"
    return f"<pre>{escape(text)}</pre>"


def _as_ul(items: list[Any]) -> str:
    if not items:
        return "<ul><li>-</li></ul>"
    rows = []
    for item in items[:12]:
        if isinstance(item, dict):
            content = " | ".join(f"{k}:{v}" for k, v in item.items() if v not in (None, "", []))
        else:
            content = str(item)
        rows.append(f"<li>{escape(content)}</li>")
    return "<ul>" + "".join(rows) + "</ul>"
