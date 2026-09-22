"""指令式买卖措辞的**唯一**清单（合规要求）。

## 为什么单独成一个模块

研究结论允许是「条件化观察」，不允许是「立即清仓」这类祈使句。

这条策略此前**写了两遍**，而且两份清单**已经漂移**：

* `grounding/fallback.py::_DIRECTIVE_PHRASES` —— 弱校验路径用它**拒绝**整条回答；
* `agent/middleware.py::DirectiveLanguageGuard._DIRECTIVES` —— 用来**替换**措辞。

后者的清单少了 `建议买入` / `建议卖出`。而结构化回答走的是**强**校验
（`grounding/verifier.py` 根本不检查指令措辞），指令护栏只剩中间件这一道——
于是这两个词能从主路径**原样出门**。实测（`run_turn`，证据已注明来源、数值可溯源）：

    状态 completed
    回答：分析\\n\\n- 建议买入，rebound_volume_recovery 为 1.2280701754385965[E1] …

即：8 个已知措辞里有 2 个能漏出去。**同一策略写两遍，就一定会漂移**——这正是
本项目在别处已经吃过的亏（见 `grounding/numbers.py` 与 `verifier.py` 里关于
「候选1」的注释：同一个假阳性在数值分支修了、日期分支漏了）。

所以清单只留一份，校验器与中间件都从这里取；并由
`tests/test_directive_policy_single_source.py` 断言两者消费的是**同一个对象**，
漂移会直接测失败，而不是等一句合规措辞漏到用户面前。
"""

from __future__ import annotations

#: 指令式措辞全表。**增删只改这里。**
#: 前 6 条原属中间件清单，后 2 条原只属校验器清单——取并集，不丢任何既有覆盖。
DIRECTIVE_PHRASES: tuple[str, ...] = (
    "立即清仓",
    "马上买入",
    "立刻卖出",
    "全仓买入",
    "无条件卖出",
    "抓紧买入",
    "建议买入",
    "建议卖出",
)

#: 命中后替换成的文本。保留位置信息（用户看得出这里原本有内容），
#: 但不复述原措辞——复述等于把被过滤的祈使句又印了一遍。
DIRECTIVE_PLACEHOLDER = "（该措辞已被合规过滤）"


def find_directives(text: str) -> tuple[str, ...]:
    """返回文本中命中的指令式措辞（按全表顺序，便于测试稳定）。"""
    return tuple(phrase for phrase in DIRECTIVE_PHRASES if phrase in text)


def scrub_directives(text: str) -> tuple[str, tuple[str, ...]]:
    """替换掉全部命中措辞，返回（新文本, 命中的措辞）。

    调用方需要知道「是否过滤过」以便留痕（`Notice`），所以命中项一并返回，
    而不是让调用方自己再扫一遍——再扫一遍就是第三次实现同一策略。
    """
    hits = find_directives(text)
    for phrase in hits:
        text = text.replace(phrase, DIRECTIVE_PLACEHOLDER)
    return text, hits
