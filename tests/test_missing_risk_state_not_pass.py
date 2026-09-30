"""没有风控状态时，不得报「风险闸通过」。

背景（见 docs/report-judgment-accuracy-audit.md §4.1）
---------------------------------------------------
`build_consensus_summary` 写的是::

    "risk_gate": latest_risk_verdict or "pass"

`latest_risk_verdict` 来自 `result_data["risk_feedback_state"]`。当风控环节**根本没
运行**（该字段缺失或为空字典）时，它取空字符串，于是 `or` 把它变成 **"pass"** ——
界面与下游工具于是显示「风险闸 通过」。

**「风控没有反对」和「风控没有运行」是完全不同的两件事**：前者是经审查后的放行，
后者是未经审查。把后者写成前者，等于让用户以为这笔建议已经过了风控。

实测：2026-09 的 80 份报告中 24 份如此；926 份显示「直接执行」的报告**全部**没有
风控状态 —— 即「未经风控 + 直接执行」被呈现为「已通过风控 + 直接执行」。

连带后果：`tradingagents/tools/consensus.py:198` 专门写了
``f"风险闸 {summary.get('risk_gate') or '未提供'}"``，但因为上面永远写入非空的
"pass"，那个「未提供」分支**永远不可达**。

顺带修掉 §4.4：`tools/consensus.py` 用 ``str(summary.get("consensus_strength") or "")``
读取强度，于是**真实的 0 强度**（多空完全对冲，最需要谨慎的情形）被静默省略。
"""

from __future__ import annotations

import pytest

from api.services import consensus_service
from tradingagents.tools.consensus import make_consensus_tools


def _traces():
    return [
        {
            "agent": "market_analyst",
            "verdict": "看多",
            "key_finding": "量价配合",
            "horizon": "short",
        }
    ]


def _summary(**extra):
    result_data = {"analyst_traces": _traces()}
    result_data.update(extra)
    summary = consensus_service.build_consensus_summary(result_data=result_data)
    assert summary is not None
    return summary


# ---------------------------------------------------------------------------
# 1. 缺失/空的风控状态不得变成 "pass"
# ---------------------------------------------------------------------------


def test_missing_risk_state_is_not_pass():
    summary = _summary()
    assert summary["risk_gate"] != "pass", (
        "风控环节没有运行时不得报「通过」——那是把「未审查」说成「已放行」"
    )
    assert summary["risk_gate"] is None


def test_empty_risk_state_is_not_pass():
    summary = _summary(risk_feedback_state={})
    assert summary["risk_gate"] is None


def test_risk_state_without_verdict_is_not_pass():
    """有风控反馈但没给 verdict（例如只有执行前提）同样不得报 pass。"""
    summary = _summary(risk_feedback_state={"execution_preconditions": ["放量突破 1650"]})
    assert summary["risk_gate"] is None


# ---------------------------------------------------------------------------
# 2. 真实的风控结论必须原样保留
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verdict", ["pass", "revise", "reject"])
def test_real_risk_verdict_is_preserved(verdict):
    summary = _summary(risk_feedback_state={"latest_risk_verdict": verdict})
    assert summary["risk_gate"] == verdict


def test_real_pass_is_case_insensitive_normalised():
    summary = _summary(risk_feedback_state={"latest_risk_verdict": "  PASS "})
    assert summary["risk_gate"] == "pass"


# ---------------------------------------------------------------------------
# 3. 工具层：「未提供」分支必须可达
# ---------------------------------------------------------------------------


class _Port:
    def __init__(self, summary):
        self.payload = {
            "report_id": "r1",
            "symbol": "600519.SH",
            "trade_date": "2026-09-18",
            "consensus": summary,
        }

    def latest_consensus(self, *, symbol):
        return self.payload


def _handler(summary):
    specs = {spec.name: spec for spec in make_consensus_tools(_Port(summary))}
    return specs["report_consensus_read"].handler


def _evidence_text(result) -> str:
    return "\n".join(
        f"{item.field}｜{item.value}" for item in (result.evidence or ())
    )


def test_tool_reports_risk_gate_as_unavailable_when_absent():
    summary = _summary()
    result = _handler(summary)(symbol="600519.SH", as_of="2026-09-21")
    text = _evidence_text(result)
    assert "风险闸 未提供" in text, (
        "没有风控状态时，工具必须如实说「未提供」；该分支此前因上游永远写入 "
        "'pass' 而不可达"
    )
    assert "风险闸 pass" not in text


def test_tool_still_reports_a_real_pass():
    summary = _summary(risk_feedback_state={"latest_risk_verdict": "pass"})
    result = _handler(summary)(symbol="600519.SH", as_of="2026-09-21")
    assert "风险闸 pass" in _evidence_text(result)


# ---------------------------------------------------------------------------
# 4. §4.4：真实的 0 强度不得被静默省略
# ---------------------------------------------------------------------------


def _balanced_summary() -> dict:
    """多空完全对冲 → 共识得分 0 → 强度 0（最需要谨慎的情形）。"""
    return {
        "consensus_direction": "中性",
        "consensus_score": 0.0,
        "consensus_strength": 0,
        "disagreement_score": 50,
        "stability_score": 50,
        "execution_mode": "observe",
        "execution_mode_label": "观望",
        "dominant_horizon": "short",
        "horizon_conflict": False,
        "support_ratio": 0.5,
        "oppose_ratio": 0.5,
        "neutral_ratio": 0.0,
        "risk_gate": None,
        "supporting_agents": ["market"],
        "opposing_agents": ["news"],
    }


def test_zero_strength_is_not_hidden():
    result = _handler(_balanced_summary())(symbol="600519.SH", as_of="2026-09-21")
    text = _evidence_text(result)
    assert "强度 0" in text, (
        "强度 0 是「多空完全对冲」这一最谨慎的情形，不得因 `0 or ''` 而被省略"
    )


def test_zero_strength_from_real_summary_is_visible():
    """用真实计算出来的 0 强度再验一次，避免只测手写字典。"""
    traces = [
        {"agent": "market_analyst", "verdict": "看多", "key_finding": "x", "horizon": "short"},
        {"agent": "news_analyst", "verdict": "看空", "key_finding": "y", "horizon": "short"},
    ]
    summary = consensus_service.build_consensus_summary(result_data={"analyst_traces": traces})
    assert summary is not None
    if summary["consensus_strength"] == 0:
        result = _handler(summary)(symbol="600519.SH", as_of="2026-09-21")
        assert "强度 0" in _evidence_text(result)
