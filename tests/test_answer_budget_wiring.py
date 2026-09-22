"""生成预算的接线契约：**传下去的必须真的到达模型实例**。

本模块锁的是一个"配了却不生效"的缺陷。

`OpenAIClient.get_llm()` 用**白名单**透传额外参数，原本是
`("api_key", "callbacks", "reasoning_effort")`——**没有 `max_tokens`**。
调用方传了 `max_tokens` 也不会报错，只是被静默丢掉，模型的输出长度照旧。
这类缺陷不写测试就只能靠"读到那段代码"才发现，而它偏偏是最容易看漏的
一类（参数名对、调用对、只有白名单少一项）。

## 为什么默认是"不设"

`max_tokens` 曾被寄望用来压延迟（延迟 ≈ 输出 token × 5 ms）。实测**否掉了**：
把上限从"不设"压到 1024，延迟与输出 token 都没变——该 provider 的
`max_tokens` **只约束可见输出，不约束推理 token**，而瓶颈正是推理。
同时库里实测"单次调用 >4000 token 且成功"的回合有 199 个，封顶会截断
本来正常的回答。所以默认不设，行为与改动前一致。
详见 `docs/agent-status.md` §4.4。
"""

from __future__ import annotations

import pytest

from tradingagents.agent.answer_config import (
    ANSWER_MAX_TOKENS_ENV,
    answer_max_tokens,
    describe_answer_budget,
)


# ── 配置语义 ──────────────────────────────────────────────────────────


def test_default_is_no_cap_so_behavior_is_unchanged(monkeypatch):
    """默认不封顶——这是"改动前后行为一致"的保证。

    实测封顶压不动延迟（推理 token 不受它管），却会截断正常回答的 JSON，
    所以默认必须是"不设"。
    """
    monkeypatch.delenv(ANSWER_MAX_TOKENS_ENV, raising=False)
    assert answer_max_tokens() is None


def test_explicit_value_is_honored(monkeypatch):
    """显式设置时才封顶——给"宁可截断也要给尾部设上界"的场景留出开关。"""
    monkeypatch.setenv(ANSWER_MAX_TOKENS_ENV, "4096")
    assert answer_max_tokens() == 4096


@pytest.mark.parametrize("raw", ["0", "off", "none", "unlimited", "OFF", "None"])
def test_off_switches_are_equivalent_to_unset(monkeypatch, raw):
    """多种写法都能关掉——"回退"必须比"打开"更容易做到。"""
    monkeypatch.setenv(ANSWER_MAX_TOKENS_ENV, raw)
    assert answer_max_tokens() is None


@pytest.mark.parametrize("raw", ["abc", "-5", "3.5", " "])
def test_a_bad_value_does_not_take_down_the_chat_path(monkeypatch, raw):
    """配错的环境变量**不能**让整个对话路径起不来。

    这类值的正确处置是"当作没设"，而不是抛异常——抛异常的位置在请求路径上，
    代价是用户看到 500。
    """
    monkeypatch.setenv(ANSWER_MAX_TOKENS_ENV, raw)
    assert answer_max_tokens() is None


def test_describe_reports_the_env_name_so_it_can_be_answered(monkeypatch):
    """"这轮为什么没封顶"要能一句话答出来——所以摘要里要有 env 名与实际值。"""
    monkeypatch.delenv(ANSWER_MAX_TOKENS_ENV, raising=False)
    info = describe_answer_budget()
    assert info["env"] == ANSWER_MAX_TOKENS_ENV
    assert info["max_tokens"] is None


# ── 接线：真的到达模型实例 ────────────────────────────────────────────


def test_max_tokens_reaches_the_llm_instance_when_passed():
    """核心契约：**传下去的必须真的到达**。

    这条测试若失败，说明白名单又漏了这一项——那正是本次修掉的缺陷。
    """
    from tradingagents.llm_clients.factory import create_llm_client

    client = create_llm_client(
        "openai", "deepseek-v4-flash", base_url="http://x/v1", api_key="k", max_tokens=4096
    )
    assert getattr(client.get_llm(), "max_tokens", None) == 4096


def test_max_tokens_stays_unset_when_not_passed():
    """不传时不许凭空给个默认值——那会改变所有既有路径（含报告生成）的行为。"""
    from tradingagents.llm_clients.factory import create_llm_client

    client = create_llm_client(
        "openai", "deepseek-v4-flash", base_url="http://x/v1", api_key="k"
    )
    assert getattr(client.get_llm(), "max_tokens", None) is None
