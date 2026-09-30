"""Participation floor: agreement among a handful of voters must not read as a panel consensus.

Background (see docs/report-judgment-accuracy-audit.md §2.2)
-----------------------------------------------------------
``consensus_strength`` is computed as ``|weighted mean| * 100`` over the analysts
that actually voted. Abstentions are excluded from the denominator (doctrine
D2: 弃权不计入分母但要可见), so one analyst out of a seven-analyst panel voting
``看多`` produced ``共识强度 100/100`` — **byte-identical** to all seven agreeing.
``execution_mode`` then read that 100 and returned ``direct`` ("直接执行"), i.e.
the system told the user to just execute on the strength of a single voice.

The audit found 6 stored reports with strength >= 85 and <= 3 participants, and
one report (``f7e60b27``) at 1/7 -> 100.

Design note — why ``consensus_strength`` itself is not redefined
----------------------------------------------------------------
``consensus_strength`` is a precise quantity: *among the analysts who voted, how
aligned were they*. Multiplying it by a coverage factor would fold two different
things (agreement, coverage) into one opaque number and break its meaning for
every statistic that reads it. The misleading part was never the arithmetic; it
was (a) that nothing said how many analysts were behind the number, and (b) that
``execution_mode`` ignored coverage entirely. So this fix adds participation as a
first-class, visible field, gates ``direct`` on a participation floor, and makes
the narration state the count — which is what makes 1/7 and 7/7 distinguishable.
"""

from __future__ import annotations

from api.services import consensus_service

# 面板里的 7 个分析师角色（权重表 key，见 consensus_service._BASE_WEIGHTS）
PANEL = [
    "market_analyst",
    "news_analyst",
    "social_media_analyst",
    "fundamentals_analyst",
    "macro_analyst",
    "smart_money_analyst",
    "volume_price_analyst",
]


def _voter(agent: str, verdict: str = "看多") -> dict:
    return {
        "agent": agent,
        "verdict": verdict,
        "key_finding": f"{agent} 给出证据",
        "horizon": "short",
    }


def _abstainer(agent: str) -> dict:
    """报告了但没给出方向：两个机读块都没有 direction。"""
    return {
        "agent": agent,
        "verdict": "中性",
        "verdict_parsed": False,
        "horizon": "short",
    }


FULL_PANEL = [_voter(a) for a in PANEL]
ONE_VOTER = [_voter(PANEL[0])] + [_abstainer(a) for a in PANEL[1:]]


def _summary(traces):
    summary = consensus_service.build_consensus_summary(result_data={"analyst_traces": traces})
    assert summary is not None, "测试前置条件失效：这组轨迹本应形成共识"
    return summary


# ---------------------------------------------------------------------------
# 0. 前置：确认本用例确实复现了审计里的「1/7 → 100」
# ---------------------------------------------------------------------------


def test_setup_reproduces_the_audit_finding():
    full = _summary(FULL_PANEL)
    one = _summary(ONE_VOTER)
    # 两种情形的**达成度**本来就相同（投票者内部一致），这正是问题的来源
    assert full["consensus_strength"] == 100
    assert one["consensus_strength"] == 100
    assert full["consensus_direction"] == one["consensus_direction"] == "看多"


# ---------------------------------------------------------------------------
# 1. 参与度必须是一等公民且数值正确
# ---------------------------------------------------------------------------


def test_participation_fields_are_exposed():
    summary = _summary(FULL_PANEL)
    assert summary["participant_n"] == 7
    assert summary["expected_n"] == 7
    assert summary["participation_ratio"] == 1.0
    assert summary["low_participation"] is False


def test_single_voter_is_low_participation():
    summary = _summary(ONE_VOTER)
    assert summary["participant_n"] == 1
    assert summary["expected_n"] == 7
    # 与 support_ratio / oppose_ratio 等兄弟字段一致，落盘保留 3 位小数
    assert abs(summary["participation_ratio"] - 1 / 7) < 1e-3
    assert summary["low_participation"] is True


def test_abstaining_traces_reduce_participation():
    """弃权者不计入分母（约定 D2），但必须让参与度可见。"""
    summary = _summary(ONE_VOTER)
    assert summary["unparsed_analyst_n"] == 6
    assert summary["participant_n"] + summary["unparsed_analyst_n"] == summary["expected_n"]


# ---------------------------------------------------------------------------
# 2. 低参与度不得给出「直接执行」
# ---------------------------------------------------------------------------


def test_low_participation_is_never_direct():
    summary = _summary(ONE_VOTER)
    assert summary["execution_mode"] != "direct", (
        "1/7 参与且强度 100 时不得给出「直接执行」——那是让一条声音决定下单"
    )


def test_full_panel_can_still_be_direct():
    """正常路径不得因参与度下限而被误伤。"""
    summary = _summary(FULL_PANEL)
    assert summary["execution_mode"] == "direct"


def test_low_participation_is_distinguishable_from_full():
    """1/7 与 7/7 不得给出完全相同的结论画像。"""
    full = _summary(FULL_PANEL)
    one = _summary(ONE_VOTER)
    assert one["low_participation"] is True
    assert full["low_participation"] is False
    assert one["execution_mode"] != full["execution_mode"]
    # 叙述也必须不同，否则界面上仍看不出差别
    assert one["summary"] != full["summary"]


# ---------------------------------------------------------------------------
# 3. 叙述必须含参与度
# ---------------------------------------------------------------------------


def test_narration_states_participation():
    summary = _summary(FULL_PANEL)
    assert "7" in summary["summary"]
    assert "参与" in summary["summary"]


def test_narration_flags_low_participation():
    summary = _summary(ONE_VOTER)
    text = summary["summary"]
    assert "参与" in text
    assert "1" in text and "7" in text
    # 低参与度必须被明确点出，而不是让读者自己数
    assert ("仅" in text) or ("参与度低" in text) or ("不足" in text)


# ---------------------------------------------------------------------------
# 4. 稳定性同样不得对低参与度无感
# ---------------------------------------------------------------------------


def test_low_participation_lowers_stability():
    full = _summary(FULL_PANEL)
    one = _summary(ONE_VOTER)
    assert one["stability_score"] < full["stability_score"], (
        "1/7 参与的稳定性不得与 7/7 一致相同"
    )


def test_stability_stays_in_range():
    for traces in (FULL_PANEL, ONE_VOTER):
        value = _summary(traces)["stability_score"]
        assert 0 <= value <= 100
