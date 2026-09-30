"""交易员系统提示词里的机读块必须是**单**花括号。

背景（见 docs/report-judgment-accuracy-audit.md §2.5）
----------------------------------------------------
`trader_system_prompt` 里写的是一行示例::

    ...<!-- VERDICT: {{"direction": "看多", "reason": "...", "confidence": 72}} -->...

在 Python 提示词表里，双花括号是用来「在 ``str.format()`` 之后留下单花括号」的转义。
但 `trader_system_prompt` **从不经过 ``.format()``**
（`tradingagents/agents/trader/trader.py:31` 取回后直接作为 system message 使用；
唯一被 ``.format()`` 的是另一个 key ``trader_user_prompt``，见 `trader.py:42`）。
于是模型看到的是**字面的双花括号**，照抄出来的块自然无法解析：

- 生产库中 **189 份报告**的正文里出现字面的 ``{{``；
- 交易员原本就是最常丢失方向的那一环。

全语料核查（zh/en 各一遍）：中英文各有且**仅有** `trader_system_prompt` 这一个
「未格式化却带 ``{{``」的 key，且恰好 1 处 —— 就是这里的 VERDICT 模板。

本测试同时给出**防漂移护栏**：凡带机读块又出现 ``{{`` 的 key，必须出现在
`FORMATTED_PROMPT_KEYS`（即真的会被 ``.format()`` 的 key）里；否则断言失败，
逼迫后来者显式说明，而不是悄悄留下字面双花括号。
"""

from __future__ import annotations

import json
import re

import pytest

from tradingagents.agents.utils.direction import extract_direction_result
from tradingagents.prompts.en import PROMPTS as EN_PROMPTS
from tradingagents.prompts.zh import PROMPTS as ZH_PROMPTS

#: 源码里真的对取回值调用 ``.format(`` 的 prompt key。
#: 来源：`grep -rn 'get_prompt(.*)\.format(' tradingagents/ api/`
FORMATTED_PROMPT_KEYS = frozenset(
    {
        "bull_prompt",
        "bear_prompt",
        "conservative_prompt",
        "aggressive_prompt",
        "neutral_prompt",
        "trader_user_prompt",
        "decision_critic_prompt",
        "risk_manager_prompt",
        "research_manager_prompt",
    }
)

PACKS = {"zh": ZH_PROMPTS, "en": EN_PROMPTS}


def _trader_system(lang: str) -> str:
    return PACKS[lang]["trader_system_prompt"]


# ---------------------------------------------------------------------------
# 1. 未格式化的提示词里不得出现字面双花括号
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lang", ["zh", "en"])
def test_trader_system_prompt_has_no_doubled_braces(lang):
    text = _trader_system(lang)
    assert "{{" not in text, (
        f"{lang}/trader_system_prompt 不经过 .format()，双花括号会原样送给模型；"
        "机读块必须写成单花括号"
    )
    assert "}}" not in text


# ---------------------------------------------------------------------------
# 2. 提示词里给出的示例块必须能被流水线自己的解析器读懂
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lang,expected", [("zh", "看多"), ("en", "看多")])
def test_verdict_example_in_prompt_is_parseable(lang, expected):
    """两种语言示范的方向都必须被解析器读懂，并归一为规范值。

    英文块里写的是 ``BULLISH``，解析器按仓库规范将其归一为 ``看多``
    （canonical direction），因此这里断言规范值而不是原文。
    """
    text = _trader_system(lang)
    result = extract_direction_result(text, allow_labelled_lines=False)
    assert result.direction is not None, (
        f"{lang}/trader_system_prompt 里示范的 VERDICT 块解析不出方向 —— "
        "模型照抄这个格式就会产出无法解析的结论"
    )
    assert result.direction == expected


def test_en_prompt_declared_vocabulary_is_fully_recognised():
    """英文提示词声明可填的 5 个方向词，解析器必须全部认。

    否则英文模式下模型照提示词填词，却会被判成「无方向」——那正是交易员环节
    丢方向的成因之一。
    """
    from tradingagents.agents.utils.direction import classify_direction_phrase

    for word in ("BULLISH", "LEAN_BULLISH", "NEUTRAL", "LEAN_BEARISH", "BEARISH"):
        assert word in _trader_system("en"), f"英文提示词应声明方向词 {word}"
        resolved = classify_direction_phrase(word)
        assert resolved is not None, f"提示词声明了 {word}，但解析器不认它"


@pytest.mark.parametrize("lang", ["zh", "en"])
def test_verdict_example_is_valid_json(lang):
    text = _trader_system(lang)
    match = re.search(r"<!--\s*VERDICT:\s*(\{[\s\S]*?\})\s*-->", text)
    assert match, f"{lang}/trader_system_prompt 必须包含一个 VERDICT 示例块"
    payload = json.loads(match.group(1))
    assert payload.get("direction")
    assert "confidence" in payload
    assert "reason" in payload


# ---------------------------------------------------------------------------
# 3. 提示词必须继续要求输出机读块（不得为了修括号而把要求删掉）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lang", ["zh", "en"])
def test_prompt_still_demands_machine_readable_blocks(lang):
    text = _trader_system(lang)
    assert "VERDICT" in text
    assert "TRADE_PLAN" in text
    # 交易计划块的键名一个都不能少（解析器与提示词共同约定）
    for key in (
        "direction",
        "horizon_days",
        "entry_low",
        "entry_high",
        "position_cap_pct",
        "first_tranche_pct",
        "hard_stop_price",
        "take_profit_ladder",
        "trailing_stop_pct",
        "time_stop_days",
        "invalidation_conditions",
    ):
        assert key in text, f"{lang}/trader_system_prompt 缺少交易计划键 {key}"


# ---------------------------------------------------------------------------
# 4. 防漂移护栏：带机读块的 prompt，其花括号写法必须与是否格式化一致
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lang", ["zh", "en"])
def test_doubled_braces_only_in_prompts_that_are_formatted(lang):
    offenders = []
    for key, text in PACKS[lang].items():
        if not isinstance(text, str) or "<!--" not in text:
            continue
        if "{{" not in text:
            continue
        if key not in FORMATTED_PROMPT_KEYS:
            offenders.append(key)
    assert not offenders, (
        f"{lang} 里这些 prompt 不经过 .format()，却在机读块里用了双花括号：{offenders}；"
        "请在提示词里改成单花括号，或（若确实会格式化）把它加入 FORMATTED_PROMPT_KEYS"
    )
