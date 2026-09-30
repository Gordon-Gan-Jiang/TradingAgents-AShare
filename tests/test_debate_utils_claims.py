"""辩论 claim 解析的容错契约。

## 为什么有这些用例

2026-09-28 的 600699.SH 研判在**风险辩论阶段**整条失败，报错只有一行：

    RuntimeError: Horizon analysis failed: short: 'str' object has no attribute 'get'

根因是 `debate_utils.update_debate_state_with_payload` 假定模型给出的
`new_claims` 一定是"对象数组"。提示词确实是这么写的，但轻量模型会写成
`["风险一", "风险二"]`、单个对象，甚至一个裸字符串 —— 这三种形态都会在
`claim_payload.get("claim")` 上抛 AttributeError，于是**已经跑完的**分析师、
研究团队、交易员全部作废，用户等了十分钟只拿到一行报错。

这些用例把"非规范但可无损折算"的输出钉死在容错路径上，同时保证规范输出
的行为一字不变。
"""
import pytest

from tradingagents.agents.utils.debate_utils import (
    format_claims_for_prompt,
    format_claim_subset_for_prompt,
    update_debate_state_with_payload,
)


def _empty_risk_state(**overrides):
    state = {
        "history": "",
        "aggressive_history": "",
        "conservative_history": "",
        "neutral_history": "",
        "latest_speaker": "",
        "current_aggressive_response": "",
        "current_conservative_response": "",
        "current_neutral_response": "",
        "judge_decision": "",
        "count": 0,
        "claims": [],
        "focus_claim_ids": [],
        "open_claim_ids": [],
        "resolved_claim_ids": [],
        "unresolved_claim_ids": [],
        "round_summary": "",
        "round_goal": "",
        "claim_counter": 0,
    }
    state.update(overrides)
    return state


def _apply(raw_response, state=None):
    return update_debate_state_with_payload(
        state=state if state is not None else _empty_risk_state(),
        raw_response=raw_response,
        speaker_label="Aggressive Analyst",
        speaker_key="Aggressive",
        stance="aggressive",
        history_key="aggressive_history",
        marker="RISK_STATE",
        claim_prefix="RISK",
        domain="risk",
        speaker_field="latest_speaker",
        store_current_response=False,
    )


def _tagged(payload_json: str) -> str:
    return f"本轮分析正文。\n<!-- RISK_STATE: {payload_json} -->"


class TestMalformedNewClaims:
    """模型把 new_claims 写成非规范形态时，必须继续跑，而不是炸掉整条 horizon。"""

    def test_list_of_strings_registers_each_string_as_one_claim(self):
        # 事故现场形态：字符串数组
        new_state = _apply(
            _tagged('{"new_claims": ["跌停流动性断层", "估值泡沫"], "round_summary": "两条风险"}')
        )

        claims = new_state["claims"]
        assert [c["claim"] for c in claims] == ["跌停流动性断层", "估值泡沫"]
        assert [c["claim_id"] for c in claims] == ["RISK-1", "RISK-2"]
        assert all(c["status"] == "open" for c in claims)
        assert new_state["claim_counter"] == 2

    def test_single_object_instead_of_array(self):
        new_state = _apply(_tagged('{"new_claims": {"claim": "单一风险", "evidence": ["公告"]}}'))

        assert [c["claim"] for c in new_state["claims"]] == ["单一风险"]
        assert new_state["claims"][0]["evidence"] == ["公告"]

    def test_bare_string(self):
        new_state = _apply(_tagged('{"new_claims": "裸字符串风险"}'))

        assert [c["claim"] for c in new_state["claims"]] == ["裸字符串风险"]

    def test_unusable_entries_are_dropped_not_fatal(self):
        new_state = _apply(_tagged('{"new_claims": [123, null, {"claim": "有效风险"}]}'))

        assert [c["claim"] for c in new_state["claims"]] == ["有效风险"]

    def test_blank_string_entries_are_ignored(self):
        new_state = _apply(_tagged('{"new_claims": ["   ", ""]}'))

        assert new_state["claims"] == []
        assert new_state["claim_counter"] == 0

    def test_evidence_as_bare_string_is_not_split_into_characters(self):
        new_state = _apply(
            _tagged('{"new_claims": [{"claim": "风险", "evidence": "半年报应收账款"}]}')
        )

        assert new_state["claims"][0]["evidence"] == ["半年报应收账款"]

    def test_responded_claim_ids_as_bare_string(self):
        state = _empty_risk_state(
            claims=[{"claim_id": "RISK-1", "claim": "旧风险", "status": "open"}],
            claim_counter=1,
        )
        new_state = _apply(
            _tagged('{"responded_claim_ids": "RISK-1", "new_claims": []}'), state=state
        )

        assert new_state["claims"][0]["status"] == "addressed"


class TestPollutedStateClaims:
    """state 里的 claims 被污染成字符串时，跳过脏条目而不是整轮失败。"""

    def test_string_entries_in_state_are_dropped(self):
        state = _empty_risk_state(claims=["RISK-1", {"claim_id": "RISK-2", "claim": "干净风险"}])

        new_state = _apply(_tagged('{"new_claims": []}'), state=state)

        assert [c.get("claim_id") for c in new_state["claims"]] == ["RISK-2"]

    def test_claims_state_as_plain_string_does_not_raise(self):
        # dict("...") 会抛 ValueError，必须走 _mapping_items 过滤
        state = _empty_risk_state(claims="RISK-1")

        new_state = _apply(_tagged('{"new_claims": []}'), state=state)

        assert new_state["claims"] == []

    def test_prompt_rendering_skips_non_mapping_entries(self):
        rendered = format_claims_for_prompt(
            ["脏字符串", {"claim_id": "RISK-2", "claim": "干净风险", "speaker": "Aggressive"}]
        )

        assert "干净风险" in rendered
        assert "脏字符串" not in rendered

    def test_claim_subset_skips_non_mapping_entries(self):
        rendered = format_claim_subset_for_prompt(
            ["脏字符串", {"claim_id": "RISK-2", "claim": "干净风险"}], ["RISK-2"]
        )

        assert "干净风险" in rendered


class TestCanonicalPayloadUnchanged:
    """规范输出（提示词里写的那种）行为必须与改动前完全一致。"""

    def test_canonical_object_array_keeps_ids_evidence_and_focus(self):
        new_state = _apply(
            _tagged(
                '{"new_claims": ['
                '{"claim": "流动性风险", "evidence": ["成交额", "跌停家数"], "confidence": 0.8},'
                '{"claim": "估值风险", "evidence": ["PE分位"], "confidence": 0.6}'
                '], "next_focus_claim_ids": ["RISK-1"], "round_summary": "两条风险", "round_goal": "盯流动性"}'
            )
        )

        claims = new_state["claims"]
        assert [c["claim_id"] for c in claims] == ["RISK-1", "RISK-2"]
        assert claims[0]["evidence"] == ["成交额", "跌停家数"]
        assert claims[0]["confidence"] == 0.8
        assert claims[0]["speaker"] == "Aggressive Analyst"
        assert new_state["focus_claim_ids"] == ["RISK-1"]
        assert new_state["round_summary"] == "两条风险"
        assert new_state["round_goal"] == "盯流动性"
        assert new_state["open_claim_ids"] == ["RISK-1", "RISK-2"]
        assert new_state["history"].startswith("Aggressive Analyst: 本轮分析正文。")

    def test_evidence_is_capped_at_three_items(self):
        new_state = _apply(
            _tagged('{"new_claims": [{"claim": "风险", "evidence": ["a", "b", "c", "d"]}]}')
        )

        assert new_state["claims"][0]["evidence"] == ["a", "b", "c"]

    def test_resolved_and_unresolved_flow(self):
        state = _empty_risk_state(
            claims=[
                {"claim_id": "RISK-1", "claim": "风险一", "status": "open"},
                {"claim_id": "RISK-2", "claim": "风险二", "status": "open"},
            ],
            claim_counter=2,
        )

        new_state = _apply(
            _tagged('{"resolved_claim_ids": ["RISK-1"], "unresolved_claim_ids": ["RISK-2"]}'),
            state=state,
        )

        by_id = {c["claim_id"]: c["status"] for c in new_state["claims"]}
        assert by_id == {"RISK-1": "resolved", "RISK-2": "unresolved"}
        assert new_state["resolved_claim_ids"] == ["RISK-1"]
        assert new_state["unresolved_claim_ids"] == ["RISK-2"]

    def test_missing_machine_block_still_advances_round(self):
        new_state = _apply("没有机读块，只有正文。")

        assert new_state["count"] == 1
        assert new_state["claims"] == []
        assert new_state["round_summary"]
