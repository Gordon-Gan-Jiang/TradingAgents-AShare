"""F3: the analytical prompts must permit abstention.

The deterministic audit found that identical inputs produced opposite directions in
48.6% of cases (334/687 groups), 20.5% emitted both directions, all at
``llm_temperature=0.0``. A large part of that is manufactured by the prompts: the
Chinese trader prompt used to declare HOLD "not a default option" and order the model
to "choose between BUY and SELL, no escaping to HOLD", and both prompts restricted
NEUTRAL/中性 to a last resort. A forced directional call on evidence that does not
support one is a coin flip with a rationale attached.

These tests pin the *absence* of the coercion and the *presence* of an explicit,
non-penalised abstention path. They are deliberately about prompt semantics, not
wording, so the assertions look for the coercive constructs and for the honest
alternative.
"""

import pytest

from tradingagents.prompts.en import PROMPTS as EN_PROMPTS
from tradingagents.prompts.zh import PROMPTS as ZH_PROMPTS

COERCIVE_PHRASES = [
    "不允许逃避到 HOLD",
    "必须在 BUY 和 SELL 之间选择",
    "HOLD 不是默认选项",
    "use NEUTRAL only when data is genuinely insufficient",
    # 本条曾长期幸存：它不在 trader_system_prompt 里，而在 **12 个分析师的 direction
    # 字段说明**里（zh 9 处 / en 5 处）。原先的测试只检查 trader_system_prompt，
    # 于是强制语在源头继续生效而测试全绿——这正是「测试只覆盖被清理的那一个键」
    # 造成的假通过。
    "仅数据确实不足时可选中性",
    "不要回避",
    "机械默认 Hold",
]


def _text(key, lang="zh"):
    """Read a prompt directly, bypassing language resolution from config."""
    table = ZH_PROMPTS if lang == "zh" else EN_PROMPTS
    return table.get(key) or ""


def _all_text(lang):
    """Every string prompt value, concatenated.

    The coercion was spread across many keys, so a per-key assertion is not a guard:
    it passes as long as *that one key* is clean. Scanning the whole table is what
    actually pins the property.
    """
    table = ZH_PROMPTS if lang == "zh" else EN_PROMPTS
    return "\n".join(v for v in table.values() if isinstance(v, str))


# ---------------------------------------------------------------------------
# The coercion is gone
# ---------------------------------------------------------------------------

class TestCoercionRemoved:
    @pytest.mark.parametrize("lang", ["zh", "en"])
    @pytest.mark.parametrize("phrase", COERCIVE_PHRASES)
    def test_no_coercive_phrase_survives_anywhere(self, lang, phrase):
        """Must scan the WHOLE prompt table, not one key.

        Restricting this to `trader_system_prompt` is what let the clause survive in
        the analyst contracts while the suite stayed green.
        """
        blob = _all_text(lang)
        assert phrase not in blob, (
            f"{lang} 提示词中仍存在强制语 {phrase!r} —— 弃权（F3）在源头被禁止，"
            "而模型只能在证据不足时被迫表态"
        )

    @pytest.mark.parametrize("lang", ["zh", "en"])
    def test_trader_prompt_does_not_forbid_abstention(self, lang):
        text = _text("trader_system_prompt", lang)
        assert "逃避" not in text or "不是逃避" in text
        assert "evasion" not in text or "not an evasion" in text

    @pytest.mark.parametrize("lang", ["zh", "en"])
    def test_analyst_direction_contract_permits_neutral(self, lang):
        """The分析师 direction 字段说明 must not gate 中性 behind 'insufficient data'.

        This is the authoritative defect site: every analyst emits a `direction` using
        this vocabulary, so coercion here propagates to all 7 analysts.
        """
        blob = _all_text(lang)
        if lang == "zh":
            assert "无明确指向时请选「中性」" in blob
            assert "不算回避" in blob
        else:
            assert "choose NEUTRAL whenever this dimension does not point clearly" in blob
            assert "NEUTRAL is not a cop-out" in blob

    @pytest.mark.parametrize("lang", ["zh", "en"])
    def test_trader_prompt_does_not_forbid_abstention(self, lang):
        text = _text("trader_system_prompt", lang)
        assert "逃避" not in text or "不是逃避" in text
        assert "evasion" not in text or "not an evasion" in text


# ---------------------------------------------------------------------------
# Abstention is explicitly legitimate
# ---------------------------------------------------------------------------

class TestAbstentionIsLegitimate:
    def test_zh_prompt_states_hold_is_legitimate(self):
        text = _text("trader_system_prompt", "zh")
        assert "合法结论" in text
        assert "不要把它当默认" in text or "也不要把 HOLD 当默认答案" in text

    def test_zh_prompt_forbids_forcing_a_direction(self):
        text = _text("trader_system_prompt", "zh")
        assert "严禁" in text and "硬选" in text

    def test_en_prompt_states_hold_is_legitimate(self):
        text = _text("trader_system_prompt", "en")
        assert "legitimate conclusion" in text
        assert "not an evasion" in text

    def test_en_prompt_forbids_forcing_a_direction(self):
        text = _text("trader_system_prompt", "en")
        assert "never force a direction" in text

    def test_neutral_is_no_longer_restricted_to_last_resort(self):
        """中性/NEUTRAL must not be gated behind "only when data is insufficient"."""
        zh = _text("trader_system_prompt", "zh")
        en = _text("trader_system_prompt", "en")
        assert "中性是合法答案" in zh
        assert "NEUTRAL is a legitimate honest answer" in en

    def test_abstention_is_recorded_and_not_penalised(self):
        """The model must know abstaining is counted honestly, not as a failure."""
        zh = _text("trader_system_prompt", "zh")
        assert "单独统计" in zh
        assert "不计入方向正确率的分母" in zh


# ---------------------------------------------------------------------------
# The machine-readable contract still holds
# ---------------------------------------------------------------------------

class TestContractsIntact:
    def test_zh_verdict_block_contract_preserved(self):
        text = _text("trader_system_prompt", "zh")
        assert "<!-- VERDICT:" in text
        assert "看多" in text and "看空" in text and "中性" in text

    def test_zh_trade_plan_block_contract_preserved(self):
        text = _text("trader_system_prompt", "zh")
        assert "<!-- TRADE_PLAN" in text
        for key in (
            "direction:", "entry_low:", "entry_high:", "hard_stop_price:",
            "take_profit_ladder:", "time_stop_days:", "invalidation_conditions:",
        ):
            assert key in text

    def test_en_final_proposal_line_preserved(self):
        assert "FINAL TRANSACTION PROPOSAL" in _text("trader_system_prompt", "en")

    def test_no_dangling_memory_subsystem_reference(self):
        """The reflection/memory subsystem was deleted; prompts must not cite it."""
        for lang in ("zh", "en"):
            text = _text("trader_system_prompt", lang)
            assert "lessons learned" not in text
            assert "{past_memory_str}" not in text
