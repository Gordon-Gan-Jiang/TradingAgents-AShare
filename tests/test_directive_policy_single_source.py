"""指令式措辞策略**只有一份**，且两个消费者都用它。

## 为什么需要这个测试（真实漏洞，已复现）

这条合规策略此前写了两遍，并且已经漂移：

* `grounding/fallback.py` 的清单有 8 条；
* `agent/middleware.py` 的清单只有 6 条，少了 `建议买入` / `建议卖出`。

结构化回答走**强**校验（`grounding/verifier.py` 根本不检查指令措辞），
所以指令护栏只剩中间件这一道——那两个词能从主路径原样出门。实测
（`run_turn`，数值可溯源、`verdict_ref` 合法）：

    状态 completed
    回答：分析\\n\\n- 建议买入，rebound_volume_recovery 为 1.2280701754385965[E1] …

**同一策略写两遍就一定会漂移**。所以这里断言两个消费者拿的是同一个对象，
而不是"两份内容恰好一样"——后者迟早again 分叉。
"""

from __future__ import annotations

from tradingagents.agent import middleware as mw
from tradingagents.grounding import fallback
from tradingagents.grounding.directives import (
    DIRECTIVE_PHRASES,
    find_directives,
    scrub_directives,
)


def test_both_consumers_share_one_object():
    """两个消费者必须引用**同一份**清单（同一对象，不是同内容）。"""
    assert fallback._DIRECTIVE_PHRASES is DIRECTIVE_PHRASES
    # 中间件不再自带清单：若有人重新加回 `_DIRECTIVES`，说明策略又被复制了一份
    assert not hasattr(mw.DirectiveLanguageGuard, "_DIRECTIVES"), (
        "中间件不得再自带措辞清单——必须用 grounding/directives.py 那一份"
    )


def test_known_phrases_are_not_lost():
    """并集不丢：原先两份清单里的措辞都要在。"""
    for phrase in (
        "立即清仓", "马上买入", "立刻卖出", "全仓买入", "无条件卖出", "抓紧买入",
        "建议买入", "建议卖出",
    ):
        assert phrase in DIRECTIVE_PHRASES


def test_the_two_phrases_that_used_to_leak_are_now_scrubbed():
    """`建议买入` / `建议卖出`：曾能出门，现在必须被替换。"""
    for phrase in ("建议买入", "建议卖出"):
        scrubbed, hits = scrub_directives(f"综合看，{phrase}，注意仓位。")
        assert phrase not in scrubbed
        assert phrase in hits
        assert find_directives(f"综合看，{phrase}。") == (phrase,)


def test_scrub_reports_every_hit_for_the_notice():
    text, hits = scrub_directives("建议买入并全仓买入")
    assert hits == ("全仓买入", "建议买入")
    assert "建议买入" not in text and "全仓买入" not in text


def test_middleware_scrubs_and_leaves_a_notice():
    """中间件是强校验路径上唯一的指令护栏，必须真的替换并留痕。"""
    from tradingagents.agent.events import Notice

    class _Ctx:
        def __init__(self):
            self.notices = []

    ctx = _Ctx()
    out = mw.DirectiveLanguageGuard().before_answer(ctx, "建议买入 600519")
    assert "建议买入" not in out
    assert ctx.notices and "建议买入" in ctx.notices[0]
    assert "600519" in out, "只替换措辞，不得吞掉其余内容"


def test_non_directive_text_is_untouched():
    text = "量能萎缩，属于条件化观察，不构成投资建议。"
    assert scrub_directives(text) == (text, ())
