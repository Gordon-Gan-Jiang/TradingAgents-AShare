"""Tests for api.services.wps_notification_service."""
from unittest.mock import MagicMock, patch

import pytest

from api.database import ReportDB


def _make_report():
    r = MagicMock(spec=ReportDB)
    r.symbol = "600519.SH"
    r.trade_date = "2026-04-09"
    r.decision = "HOLD"
    r.direction = "持有"
    r.confidence = 72
    r.final_trade_decision = "观望为主。"
    r.trader_investment_plan = None
    r.investment_plan = None
    return r


def test_normalize_wps_webhook_url_full_and_key_only():
    from api.services.wps_notification_service import normalize_wps_webhook_url

    u = normalize_wps_webhook_url("https://xz.wps.cn/api/v1/webhook/send?key=abc-def_123")
    assert u == "https://xz.wps.cn/api/v1/webhook/send?key=abc-def_123"

    u2 = normalize_wps_webhook_url("mykey123")
    assert u2 == "https://xz.wps.cn/api/v1/webhook/send?key=mykey123"


def test_normalize_kdocs_woa_webhook_url():
    from api.services.wps_notification_service import normalize_wps_webhook_url

    u = normalize_wps_webhook_url(
        "https://365.kdocs.cn/woa/api/v1/webhook/send?key=39cb03c56279ecfdc8b638a7270153d3"
    )
    assert u == "https://365.kdocs.cn/woa/api/v1/webhook/send?key=39cb03c56279ecfdc8b638a7270153d3"


def test_normalize_wps_rejects_wrong_host():
    from api.services.wps_notification_service import normalize_wps_webhook_url

    with pytest.raises(ValueError, match="金山|Webhook"):
        normalize_wps_webhook_url("https://evil.com/api/v1/webhook/send?key=x")


def test_build_report_markdown_contains_headers():
    from api.services.wps_notification_service import build_report_markdown

    md = build_report_markdown(_make_report(), stock_name="贵州茅台")
    assert "## AlphaPilot A-Share" in md
    assert "600519.SH" in md
    assert "贵州茅台" in md
    assert "观望为主" in md


@patch("api.services.wps_notification_service.requests.post")
def test_send_markdown_message_success_code_zero(mock_post):
    from api.services.wps_notification_service import send_markdown_message

    mock_post.return_value = MagicMock()
    mock_post.return_value.raise_for_status = MagicMock()
    mock_post.return_value.json.return_value = {"code": 0, "msg": ""}

    ok = send_markdown_message("**hi**", "https://xz.wps.cn/api/v1/webhook/send?key=k")
    assert ok is True
    args, kwargs = mock_post.call_args
    import json

    body = json.loads(kwargs["data"])
    assert body["msgtype"] == "markdown"
    assert body["markdown"]["text"] == "**hi**"


@patch("api.services.wps_notification_service.requests.post")
def test_send_markdown_message_success_result_ok(mock_post):
    from api.services.wps_notification_service import send_markdown_message

    mock_post.return_value = MagicMock()
    mock_post.return_value.raise_for_status = MagicMock()
    mock_post.return_value.json.return_value = {"result": "ok"}

    ok = send_markdown_message("x", "https://365.kdocs.cn/woa/api/v1/webhook/send?key=k")
    assert ok is True


@patch("api.services.wps_notification_service.requests.post")
def test_send_markdown_message_failure_result_not_ok(mock_post):
    from api.services.wps_notification_service import send_markdown_message

    mock_post.return_value = MagicMock()
    mock_post.return_value.raise_for_status = MagicMock()
    mock_post.return_value.json.return_value = {"result": "error", "msg": "rate limit"}

    ok = send_markdown_message("x", "https://365.kdocs.cn/woa/api/v1/webhook/send?key=k")
    assert ok is False


@patch("api.services.wps_notification_service.requests.post")
def test_send_markdown_message_failure_code(mock_post):
    from api.services.wps_notification_service import send_markdown_message

    mock_post.return_value = MagicMock()
    mock_post.return_value.raise_for_status = MagicMock()
    mock_post.return_value.json.return_value = {"code": 400, "msg": "bad"}

    ok = send_markdown_message("x", "https://xz.wps.cn/api/v1/webhook/send?key=k")
    assert ok is False
