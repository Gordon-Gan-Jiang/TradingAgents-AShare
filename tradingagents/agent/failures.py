"""回合失败的**对外措辞**：把异常翻译成用户能据以行动的一句话。

## 为什么放在 agent 层

这段分类逻辑被两处需要：Agent 循环自己的兜底分支（`loop.py`）和 API 路由的
装配兜底（`api/routers/chat.py`）。分层方向是 `api → agent`，所以实现放这里，
两边共用——否则同一个 401 会在两条路径上给出两种说法，其中一条还会把上游原文
（含 request id、内网 URL、疑似密钥）直接发到浏览器。

## 铁律

**绝不回传异常原文**。原文可能带：上游返回体、内网地址、疑似密钥片段、
上游 request id。完整 traceback 只写服务端日志（调用方 `logger.exception`），
客户端只需要知道"是什么问题、我该做什么"。

**措辞必须指向真实存在的入口**。此前写的是「设置 → 模型配置」——导航里
并没有这一项（真实位置是「系统 → 模型管理」`/model-profiles`）。指错地方的
提示和没有提示差不多：用户会去找一个不存在的地方。
"""

from __future__ import annotations

# 每组是 (任意命中即归入该类别的子串, 对外措辞)；顺序即优先级。
# 顺序有意义：`invalid_api_key` 同时含 `api_key`，必须先判鉴权。
_FAILURE_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("authenticationerror", "invalid_api_key", "incorrect api key", "unauthorized", "401"),
        "模型鉴权失败：API Key 无效或已过期，请在「系统 → 模型管理」中更新后重试。",
    ),
    (
        ("api_key", "api key", "apikey"),
        "尚未配置可用的模型：请在「系统 → 模型管理」中填写 API Key 并设为默认，"
        "或在研判对话页选择一个已配置好的模型后重试。",
    ),
    (
        ("rate limit", "ratelimiterror", "429", "quota"),
        "模型服务限流或额度不足：请稍后重试，或在「系统 → 模型管理」中更换模型。",
    ),
    (
        ("timeout", "timed out"),
        "模型服务超时：本轮未取得回答，请重试。",
    ),
)


def describe_turn_failure(exc: BaseException) -> str:
    """异常 → 对外措辞。

    未识别时只回**异常类名**（便于用户报障），不回消息正文。
    """
    probe = f"{type(exc).__name__} {exc}".lower()
    for hints, message in _FAILURE_HINTS:
        if any(hint in probe for hint in hints):
            return message
    return (
        f"本轮回答生成失败（{type(exc).__name__}）：请重试；"
        "若持续失败，请把服务端日志中的 traceback 提供给维护者。"
    )


def failure_reason(exc: BaseException) -> str:
    """`EndEvent.reason` 用的短码：只给类名，不夹带消息正文。"""
    return type(exc).__name__
