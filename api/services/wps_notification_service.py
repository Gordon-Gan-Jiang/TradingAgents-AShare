"""WPS 协作（金山协作）群机器人 Webhook：Markdown 消息。"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlencode, urlparse

import requests

if TYPE_CHECKING:
    from api.database import ReportDB

logger = logging.getLogger(__name__)

# 官方 Webhook 入口（金山协作 / 金山文档等可能使用不同域名与 path）
_WPS_WEBHOOK_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("xz.wps.cn", "/api/v1/webhook/send"),
    ("365.kdocs.cn", "/woa/api/v1/webhook/send"),
)
_DEFAULT_WPS_HOST, _DEFAULT_WPS_PATH = _WPS_WEBHOOK_ENDPOINTS[0]
_MAX_MARKDOWN_LEN = 5000


def _strip_trailing_slash(path: str) -> str:
    p = path or "/"
    return p[:-1] if len(p) > 1 and p.endswith("/") else p


def _match_wps_webhook_endpoint(host: str, path: str) -> tuple[str, str] | None:
    h = (host or "").split(":")[0].lower()
    p = _strip_trailing_slash(path)
    for allowed_host, allowed_path in _WPS_WEBHOOK_ENDPOINTS:
        if h == allowed_host and p == allowed_path:
            return allowed_host, allowed_path
    return None


def _clip_text(text: str | None, limit: int = 720) -> str:
    if not text:
        return ""
    compact = " ".join(str(text).split()).strip()
    return compact[:limit]


def _lookup_cn_stock_display_name(symbol: str) -> str:
    s = (symbol or "").strip()
    if not s:
        return ""
    try:
        from api.main import _get_reverse_stock_map

        return _get_reverse_stock_map().get(s, s)
    except Exception:
        return s


def _format_target_line(symbol: str, stock_name: str | None = None) -> str:
    sym = (symbol or "").strip()
    if not sym:
        return "**标的**：-"
    explicit = (stock_name or "").strip()
    name = explicit if explicit else _lookup_cn_stock_display_name(sym)
    if name and name != sym:
        return f"**标的**：{name}（`{sym}`）"
    return f"**标的**：`{sym}`"


def _wps_escape_line(text: str) -> str:
    """弱化摘要里破坏 Markdown 结构的字符（WPS 仅支持 md 子集）。"""
    if not text:
        return ""
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"^#+\s*", "＃ ", t, flags=re.MULTILINE)
    return t


def build_report_markdown(report: "ReportDB", stock_name: str | None = None) -> str:
    lines = [
        "## AlphaPilot A-Share 定时分析完成",
        "",
        _format_target_line(getattr(report, "symbol", "") or "", stock_name),
        f"**交易日**：{getattr(report, 'trade_date', '') or '-'}",
    ]
    if getattr(report, "decision", None):
        lines.append(f"**决策**：{report.decision}")
    if getattr(report, "direction", None):
        lines.append(f"**方向**：{report.direction}")
    if getattr(report, "confidence", None) is not None:
        lines.append(f"**置信度**：{report.confidence}%")

    summary_raw = (
        _clip_text(getattr(report, "final_trade_decision", None), 2400)
        or _clip_text(getattr(report, "trader_investment_plan", None), 2400)
        or _clip_text(getattr(report, "investment_plan", None), 2400)
    )
    if summary_raw:
        safe = _wps_escape_line(summary_raw)
        lines.extend(["", "**摘要**", "", "> " + safe.replace("\n", "\n> ")])

    body = "\n".join(lines)
    return body[:_MAX_MARKDOWN_LEN]


def build_test_markdown(content: str | None = None) -> str:
    custom = " ".join(str(content or "").split()).strip()
    base = custom or (
        "**AlphaPilot A-Share** Webhook 测试\n\n"
        "若你在 WPS 协作群内看到本条 **Markdown** 消息，说明机器人配置正确。"
    )
    return base[:_MAX_MARKDOWN_LEN]


def normalize_wps_webhook_url(webhook_url: str) -> str:
    normalized = str(webhook_url or "").strip()
    if not normalized:
        raise ValueError("WPS 协作 Webhook 不能为空")

    if not normalized.startswith("http"):
        if not all(c.isalnum() or c in "-_" for c in normalized):
            raise ValueError("WPS Webhook key 格式不正确")
        return f"https://{_DEFAULT_WPS_HOST}{_DEFAULT_WPS_PATH}?{urlencode({'key': normalized})}"

    parsed = urlparse(normalized)
    if parsed.scheme != "https":
        raise ValueError("WPS 协作 Webhook 必须使用 HTTPS")
    matched = _match_wps_webhook_endpoint(parsed.netloc or "", parsed.path or "")
    if not matched:
        raise ValueError(
            "仅支持金山官方 Webhook 地址，例如："
            "https://xz.wps.cn/api/v1/webhook/send?key=… 或 "
            "https://365.kdocs.cn/woa/api/v1/webhook/send?key=…"
        )
    host_ok, path_ok = matched
    if parsed.params or parsed.fragment:
        raise ValueError("WPS Webhook 地址格式不正确")

    query = parse_qs(parsed.query, keep_blank_values=False)
    if set(query.keys()) != {"key"}:
        raise ValueError("WPS Webhook 地址必须仅包含 key 查询参数")
    keys = query.get("key") or []
    if len(keys) != 1:
        raise ValueError("WPS Webhook 地址格式不正确")
    key = keys[0].strip()
    if not key:
        raise ValueError("WPS Webhook key 不能为空")

    return f"https://{host_ok}{path_ok}?{urlencode({'key': key})}"


def _wps_response_ok(body: dict) -> bool:
    if not isinstance(body, dict):
        return False
    if "result" in body:
        return str(body.get("result", "")).lower() == "ok"
    if "code" in body:
        try:
            return int(body["code"]) == 0
        except (TypeError, ValueError):
            return False
    if "errcode" in body:
        try:
            return int(body["errcode"]) == 0
        except (TypeError, ValueError):
            return False
    if "success" in body:
        return bool(body["success"])
    return True


def send_markdown_message(text: str, webhook_url: str) -> bool:
    if not webhook_url:
        return False
    payload = {
        "msgtype": "markdown",
        "markdown": {"text": (text or "")[:_MAX_MARKDOWN_LEN]},
    }
    url = normalize_wps_webhook_url(webhook_url)
    response = requests.post(
        url,
        data=json.dumps(payload, ensure_ascii=False),
        headers={"Content-Type": "application/json;charset=utf-8"},
        timeout=15,
    )
    response.raise_for_status()
    try:
        body = response.json()
    except Exception:
        logger.warning(
            "[wps] non-JSON response body=%s",
            _clip_text(getattr(response, "text", None), 240),
        )
        return response.ok
    if not _wps_response_ok(body):
        logger.warning("[wps] API error body=%s", _clip_text(str(body), 400))
        return False
    return True


async def send_report_markdown_with_retry(
    report: "ReportDB", webhook_url: str, stock_name: str | None = None
) -> bool:
    content = build_report_markdown(report, stock_name=stock_name)
    try:
        ok = await asyncio.to_thread(send_markdown_message, content, webhook_url)
        if ok:
            logger.info("[wps] sent OK for %s", report.symbol)
            return True
    except Exception as exc:
        logger.warning("[wps] first send failed for %s: %s", report.symbol, exc)

    await asyncio.sleep(15)
    try:
        ok = await asyncio.to_thread(send_markdown_message, content, webhook_url)
        if ok:
            logger.info("[wps] retry sent OK for %s", report.symbol)
            return True
    except Exception as exc:
        logger.error("[wps] retry failed for %s: %s", report.symbol, exc)
    return False
