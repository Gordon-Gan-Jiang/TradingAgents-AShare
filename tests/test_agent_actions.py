"""行动提议与澄清：把「口头答应」变成「可确认的动作」。

## 为什么需要这一层

「帮我盯着，跌破 180 告诉我」的正确响应**不是**一段文字，而是把请求转成
平台已有的预警/自选/定时任务。若 Agent 只回一句「好的，我会关注」，用户
以为已经设置好了——这是一个比不回答更糟的结果。

## 三条约束

1. **行动必须两步确认**：Agent 只提议，用户确认后才执行。外部 Agent
   （MCP）能看到的状态改动的爆炸半径过大，V1 一律不开放。
2. **不能暗示已执行**：无法执行时必须明说，而不是含糊带过。
3. **深度分析属于提议而非静默升级**：路由层绝不静默升级到重档（成本 100×），
   但用户可以明确要求——所以它是「重档提议」，走同一套确认机制。
"""

from __future__ import annotations

import pytest


# ══════════════════════════════════════════════════════════════════════════
# 行动识别
# ══════════════════════════════════════════════════════════════════════════


def test_price_alert_request_becomes_a_proposal():
    from tradingagents.agent.actions import detect_action_proposal

    proposal = detect_action_proposal("帮我盯着 600519，跌破 180 告诉我", symbol="600519.SH")
    assert proposal is not None
    assert proposal.kind == "price_alert"
    assert proposal.params["symbol"] == "600519.SH"
    assert proposal.params["threshold"] == pytest.approx(180.0)
    assert proposal.requires_confirmation is True
    # 必须能说清「确认后会做什么」，否则用户无法判断
    assert "预警" in proposal.summary


def test_price_alert_accepts_chinese_number_units():
    from tradingagents.agent.actions import detect_action_proposal

    proposal = detect_action_proposal("跌破 1800 元提醒我", symbol="600519.SH")
    assert proposal is not None
    assert proposal.params["threshold"] == pytest.approx(1800.0)


def test_stock_code_is_not_mistaken_for_a_price_threshold():
    """「帮我盯着 600519」里的 600519 是标的，不是价格。

    若不遮蔽代码，会创建一条「跌破 600519」的荒谬预警——而预警是会被真正
    推送出去的，错误参数比不设置更糟。
    """
    from tradingagents.agent.actions import detect_action_proposal

    proposal = detect_action_proposal("帮我盯着 600519", symbol="600519.SH")
    assert proposal is None

    with_price = detect_action_proposal("帮我盯着 600519，跌破 180 告诉我", symbol="600519.SH")
    assert with_price is not None
    assert with_price.params["threshold"] == pytest.approx(180.0)


def test_direction_of_alert_is_captured():
    from tradingagents.agent.actions import detect_action_proposal

    down = detect_action_proposal("跌破 180 告诉我", symbol="600519.SH")
    up = detect_action_proposal("涨到 220 告诉我", symbol="600519.SH")
    assert down is not None and down.params["direction"] == "below"
    assert up is not None and up.params["direction"] == "above"


def test_watchlist_request_becomes_a_proposal():
    from tradingagents.agent.actions import detect_action_proposal

    proposal = detect_action_proposal("把 600519 加自选", symbol="600519.SH")
    assert proposal is not None
    assert proposal.kind == "watchlist_add"
    assert proposal.params["symbol"] == "600519.SH"


def test_deep_analysis_request_becomes_a_confirmed_escalation():
    """重档只能由用户明示触发，因此它是「提议」而不是静默升级。"""
    from tradingagents.agent.actions import detect_action_proposal

    proposal = detect_action_proposal("启动深度分析", symbol="600519.SH")
    assert proposal is not None
    assert proposal.kind == "deep_analysis"
    assert "深度分析" in proposal.summary
    assert proposal.requires_confirmation is True


def test_plain_question_produces_no_proposal():
    from tradingagents.agent.actions import detect_action_proposal

    for text in ("600519 是不是在洗盘", "现在多少钱", "还能不能拿"):
        assert detect_action_proposal(text, symbol="600519.SH") is None, text


def test_alert_without_a_price_is_not_proposed():
    """缺阈值时不能猜一个数字——那会创建一条用户没要求的预警。"""
    from tradingagents.agent.actions import detect_action_proposal

    assert detect_action_proposal("帮我盯着 600519", symbol="600519.SH") is None


def test_proposal_without_symbol_is_not_proposed():
    from tradingagents.agent.actions import detect_action_proposal

    assert detect_action_proposal("跌破 180 告诉我", symbol=None) is None


# ══════════════════════════════════════════════════════════════════════════
# 确认识别
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("text", ["确认", "好的", "好", "要", "启动", "可以", "是的", "执行"])
def test_confirmation_words_are_recognised(text):
    from tradingagents.agent.actions import is_confirmation

    assert is_confirmation(text) is True


@pytest.mark.parametrize("text", ["不要", "取消", "算了", "不用了"])
def test_rejection_words_are_recognised(text):
    from tradingagents.agent.actions import is_confirmation, is_rejection

    assert is_confirmation(text) is False
    assert is_rejection(text) is True


def test_a_new_question_is_not_a_confirmation():
    """「好的，那 600519 呢」不是确认——把新问题当确认会误执行动作。"""
    from tradingagents.agent.actions import is_confirmation

    assert is_confirmation("好的，那 600519 现在多少钱") is False
    assert is_confirmation("确认一下 600519 是不是在洗盘") is False


# ══════════════════════════════════════════════════════════════════════════
# 执行面限制
# ══════════════════════════════════════════════════════════════════════════


def test_external_surface_cannot_execute_actions():
    """MCP 等外部面 V1 只读：它能改用户状态的话爆炸半径过大。

    注意判据是「面」而不是「用户意图」——同一个提议在产品内可以确认执行，
    在 MCP 面必须被拒绝。
    """
    from tradingagents.agent.actions import can_execute_actions

    assert can_execute_actions("product_chat") is True
    assert can_execute_actions("mcp") is False
    assert can_execute_actions("unknown") is False


def test_proposal_exposes_an_explicit_confirmation_prompt():
    from tradingagents.agent.actions import detect_action_proposal

    proposal = detect_action_proposal("跌破 180 告诉我", symbol="600519.SH")
    assert proposal is not None
    assert proposal.confirmation_prompt
    assert "确认" in proposal.confirmation_prompt or "回复" in proposal.confirmation_prompt
    assert proposal.as_event_payload()["requires_confirmation"] is True
