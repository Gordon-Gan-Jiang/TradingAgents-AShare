"""回答模型的生成预算（纯函数，便于单测）。

## 结论先说：`max_tokens` **不是**这条路径的延迟杠杆，默认关闭

本模块存在的原因是查一个**真 bug**：`OpenAIClient.get_llm()` 的透传白名单原本是
`("api_key", "callbacks", "reasoning_effort")`——**没有 `max_tokens`**。也就是说
调用方就算传了也会被**静默丢弃**，"配了却不生效"是最难查的一类问题。白名单已补上。

至于"用 `max_tokens` 压延迟"这个想法：**实测否掉了**，故默认不设（`None`），
行为与改动前完全一致。理由如下。

## 为什么压不动延迟

原假设是"延迟 ≈ 输出 token × 5 ms，所以封顶就能快"。这个假设**只对了一半**。
实测（`docs/agent-status.md` §4）：

| 事实 | 值 |
|---|---|
| 模型调用占回合延迟 | **93–98%** |
| 可见正文长度 | 一次复杂问题仅 **745 字符** |
| 该次**计费**输出 token | **2340** |

差额是**服务端推理 token**。而实测把 `max_tokens` 从"不设"一路压到 1024，
延迟与输出 token **都没有变化**（不设：2746 token / 11.4 s；1024：2620 token / 12.0 s）：
该 provider 的 `max_tokens` **只约束可见输出，不约束推理 token**。

所以封顶不触及瓶颈——瓶颈是推理，而推理不受这个参数管。

## 为什么默认关闭：截断的代价大于收益

`max_tokens` 若真的生效，截断的是**同一段 JSON**。JSON 不完整 → 解析失败 →
退化成纯文本模式 → 丢掉全部结构化引用（弱校验）。而库里实测：

* 单次调用 >4000 token 且**成功**的回合有 **199 个**（>6000 的 54 个）。

也就是说封顶会截掉相当一批**本来正常**的回答。既然收益证不出来、风险是实的，
**默认不设**是对的。需要刻意给尾部封顶时用 `TA_ANSWER_MAX_TOKENS` 显式打开。

## 这一轮真正的教训

收益不可测的优化不该进主路径：它同时改变了质量与延迟两个变量，事后无法归因。
同一轮的"简洁性提示词"实验就是这么翻车的——它让输出**更多**、延迟**更差 21%**
（8278 ms vs 6842 ms），已回退，经过记在 `loop.py` 的 `_BREVITY_RULE` 位置。
"""

from __future__ import annotations

import os

# 回答模型的输出上限。**留空/未设 = 默认不封顶**（= 改动前的行为）。
ANSWER_MAX_TOKENS_ENV = "TA_ANSWER_MAX_TOKENS"

# 默认不设。理由见模块文档"为什么默认关闭"。
DEFAULT_ANSWER_MAX_TOKENS: int | None = None


def answer_max_tokens() -> int | None:
    """回答路径的 `max_tokens`。

    **默认返回 `None`（不设）**——因为实测它压不动延迟，而截断会毁掉
    正常回答的结构化引用。详见模块文档。

    显式设置 `TA_ANSWER_MAX_TOKENS=<正整数>` 才会封顶，供"宁可截断也要给尾部
    设上界"的场景使用。`0`/`off`/`none`/`unlimited` 同样表示不封顶。

    非法值（非数字、负数）按**未设置**处理并忽略，不抛异常：一个配错的环境变量
    不应该让整个对话路径起不来。
    """
    raw = (os.getenv(ANSWER_MAX_TOKENS_ENV) or "").strip()
    if not raw:
        return DEFAULT_ANSWER_MAX_TOKENS
    if raw.lower() in ("0", "off", "none", "unlimited"):
        return None
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_ANSWER_MAX_TOKENS
    return value if value > 0 else None


def describe_answer_budget() -> dict[str, object]:
    """配置摘要，供日志/诊断——"这轮为什么没封顶"要能一句话答出来。"""
    return {
        "max_tokens": answer_max_tokens(),
        "env": ANSWER_MAX_TOKENS_ENV,
        "default": DEFAULT_ANSWER_MAX_TOKENS,
        "note": "实测 max_tokens 不约束推理 token，压不动延迟，故默认不设",
    }
