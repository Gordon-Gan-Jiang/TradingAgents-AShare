"""行动提议：把用户的口头请求转成需要确认的具体动作。

## 为什么「提议 + 确认」而不是直接执行

用户说「帮我盯着，跌破 180 告诉我」时，真正有用的响应是把请求落成平台已有的
预警任务（`tracking_price_alert_service`）。但直接执行有三个问题：

1. **用户可能只是在设想**——「如果跌破 180 我就走」不是「帮我设个预警」
2. **参数可能识别错**——阈值抽错会创建一条用户没要求的预警
3. **外部面无法确认**——MCP 调用方没有交互回合，静默改用户状态不可接受

因此统一为：Agent 产出结构化提议 → 用户确认 → 走既有服务执行。

## 为什么深度分析也走同一套

路由层绝不静默升级到重档（成本约 100×）。但用户明确说「启动深度分析」时
必须能满足——所以它是一个**提议**，与预警走同一个确认机制。这样「不静默升级」
与「用户能要到它」同时成立。

## 为什么不猜阈值

缺阈值时**不提议**。猜一个数字会创建用户没要求的预警，而预警是会被推送的
——错误参数比不设置更糟。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

# 执行面：只有产品内对话可以确认执行动作
_PRODUCT_SURFACES = frozenset({"product_chat"})

CONFIRMATION_WORDS = frozenset({"确认", "好的", "好", "要", "启动", "可以", "是的", "执行", "确定"})
REJECTION_WORDS = frozenset({"不要", "取消", "算了", "不用了", "不用", "否"})

# 「确认」类消息通常很短；长句多半是新问题（「好的，那 600519 呢」）
_MAX_CONFIRMATION_CHARS = 8

_ALERT_MARKERS = ("盯着", "提醒我", "告诉我", "预警", "提醒", "通知我")
_WATCHLIST_MARKERS = ("加自选", "加入自选", "自选股", "关注一下")
_DEEP_ANALYSIS_MARKERS = ("启动深度分析", "深度分析", "全面分析", "完整分析", "出份研报", "生成研报")

_BELOW_MARKERS = ("跌破", "低于", "跌到", "下破")
_ABOVE_MARKERS = ("涨到", "突破", "高于", "站上", "超过")

# 阈值：允许 180 / 180.5 / 1,800 / 1800元
_PRICE_RE = re.compile(r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)")

# 六位 A 股代码。「帮我盯着 600519」里的 600519 是标的，不是价格阈值——
# 若不遮蔽，就会创建一条「跌破 600519」的荒谬预警（而且它会被推送出去）。
_CODE_RE = re.compile(r"(?<![\d.])[0368]\d{5}(?![\d.])")


def _strip_codes(text: str, symbol: str | None) -> str:
    """把股票代码从文本里遮蔽掉，使价格抽取只看真正的价格数字。"""
    masked = _CODE_RE.sub(" " * 6, text)
    if symbol:
        bare = str(symbol).split(".")[0]
        if bare:
            masked = masked.replace(bare, " " * len(bare))
            masked = masked.replace(str(symbol), " " * len(str(symbol)))
    return masked


@dataclass(frozen=True)
class ActionProposal:
    """一个需要用户确认的动作。

    `kind` 与既有平台能力一一对应（预警 / 自选 / 深度分析），执行时走既有
    服务而不是新建并行机制。
    """

    kind: str
    summary: str
    params: Mapping[str, Any] = field(default_factory=dict)
    confirmation_prompt: str = "回复「确认」执行，或忽略此提议。"
    requires_confirmation: bool = True

    def as_event_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "summary": self.summary,
            "params": dict(self.params),
            "confirmation_prompt": self.confirmation_prompt,
            "requires_confirmation": self.requires_confirmation,
        }


def can_execute_actions(surface: str) -> bool:
    """该执行面是否允许执行动作。

    产品内对话可以（有两步确认）；MCP 等外部面 V1 一律只读——它能改用户
    状态的话爆炸半径过大，且缺乏确认回路。
    """
    return surface in _PRODUCT_SURFACES


def _extract_price(text: str, symbol: str | None = None) -> float | None:
    text = _strip_codes(text, symbol)
    for match in _PRICE_RE.finditer(text):
        raw = match.group(1).replace(",", "")
        try:
            value = float(raw)
        except ValueError:  # pragma: no cover — 正则已保证可解析
            continue
        # 排除「5 日线」「3 天」这类结构性数字
        tail = text[match.end():match.end() + 3]
        if tail.startswith(("日", "天", "周", "月", "年", "个")):
            continue
        if value > 0:
            return value
    return None


def detect_action_proposal(text: str, *, symbol: str | None) -> ActionProposal | None:
    """从用户消息里识别一个可执行动作。识别不出就返回 None。"""
    text = (text or "").strip()
    if not text:
        return None

    if any(marker in text for marker in _DEEP_ANALYSIS_MARKERS):
        return ActionProposal(
            kind="deep_analysis",
            summary=(
                f"启动 {symbol or '该标的'} 的多智能体深度分析（约 3-5 分钟）。"
                "这会消耗明显多于日常问答的模型用量。"
            ),
            params={"symbol": symbol},
            confirmation_prompt="回复「启动」执行深度分析，或忽略此提议。",
        )

    if not symbol:
        # 没有标的就无法构造任何有意义的动作——不猜
        return None

    if any(marker in text for marker in _WATCHLIST_MARKERS):
        return ActionProposal(
            kind="watchlist_add",
            summary=f"把 {symbol} 加入自选股。",
            params={"symbol": symbol},
            confirmation_prompt="回复「确认」加入自选，或忽略此提议。",
        )

    if any(marker in text for marker in _ALERT_MARKERS):
        threshold = _extract_price(text, symbol)
        if threshold is None:
            # 缺阈值不提议：猜一个数字会创建用户没要求的预警，而预警会被推送
            return None
        if any(marker in text for marker in _BELOW_MARKERS):
            direction = "below"
        elif any(marker in text for marker in _ABOVE_MARKERS):
            direction = "above"
        else:
            direction = "below"
        comparator = "跌破" if direction == "below" else "涨到"
        return ActionProposal(
            kind="price_alert",
            summary=f"为 {symbol} 创建价格预警：{comparator} {threshold:g} 时通知你。",
            params={"symbol": symbol, "threshold": threshold, "direction": direction},
            confirmation_prompt="回复「确认」创建预警，或忽略此提议。",
        )

    return None


def is_confirmation(text: str) -> bool:
    """判断是否是一次「确认」。

    刻意保守：长句多半是新问题（「好的，那 600519 呢」）。把新问题当确认会
    误执行动作，代价高于让用户再确认一次。
    """
    stripped = (text or "").strip().rstrip("。！!，, ")
    if not stripped or len(stripped) > _MAX_CONFIRMATION_CHARS:
        return False
    if is_rejection(stripped):
        return False
    return stripped in CONFIRMATION_WORDS


def is_rejection(text: str) -> bool:
    stripped = (text or "").strip().rstrip("。！!，, ")
    if len(stripped) > _MAX_CONFIRMATION_CHARS:
        return False
    return stripped in REJECTION_WORDS
