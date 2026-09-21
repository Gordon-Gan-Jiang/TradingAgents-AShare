"""B1: self-reported confidence must not drive the consensus score.

The audit measured the shipped confidence signal against T+1 outcomes on 3497
reports: Pearson r = +0.033 (r² = 0.0011), and the buckets were **non-monotonic** —
[70,75) 51.2%, [75,80) 55.3%, [80,101) **46.7%**. The highest-confidence bucket was
worse than chance *and* worse than every bucket below it.

Before this change that number was multiplied into every analyst's weight:

    effective_weight = agent_weight * confidence * evidence_quality

so it decided ``consensus_direction``, ``consensus_strength`` and (through the
disagreement score) ``execution_mode`` — i.e. an inverted-quality self-report was the
main dial on the product's headline conclusion. Worse, when an analyst omitted a
confidence the code *synthesised* one from how directional the verdict was
(``0.45 + |score| * 0.35``), so the most emphatic phrasing automatically earned the
largest weight: confident guessing was amplified into consensus.

These tests pin the decoupling: confidence is provenance, never leverage.
"""

import pytest

from api.services import consensus_service as cs
from api.services.consensus_service import build_consensus_summary


def _traces(*specs):
    """specs: (agent, verdict, confidence) — horizon defaults to short."""
    return [
        {
            "agent": agent,
            "horizon": "short",
            "verdict": verdict,
            "confidence": confidence,
            "key_finding": f"{agent} 结论",
        }
        for agent, verdict, confidence in specs
    ]


def _summary(traces, direction="看多"):
    return build_consensus_summary(
        result_data={"analyst_traces": traces, "risk_feedback_state": {}},
        final_direction=direction,
        final_confidence=68,
    )


# ---------------------------------------------------------------------------
# confidence no longer changes the answer
# ---------------------------------------------------------------------------

class TestConfidenceDoesNotChangeTheVerdict:
    """Changing only self-reported confidence must not move any headline field."""

    @pytest.mark.parametrize(
        "confidences",
        [
            ("高", "高", "高"),
            ("低", "低", "低"),
            ("高", "低", "高"),
            (95, 5, 95),
            (5, 95, 5),
            (None, None, None),
            ("高", 90, None),
        ],
    )
    def test_headline_fields_are_invariant_to_confidence(self, confidences):
        base = _traces(
            ("market_analyst", "看多", "中"),
            ("news_analyst", "看空", "中"),
            ("smart_money_analyst", "偏多", "中"),
        )
        variant = _traces(
            ("market_analyst", "看多", confidences[0]),
            ("news_analyst", "看空", confidences[1]),
            ("smart_money_analyst", "偏多", confidences[2]),
        )

        a, b = _summary(base), _summary(variant)
        for field in (
            "consensus_direction",
            "consensus_strength",
            "disagreement_score",
            "stability_score",
            "execution_mode",
        ):
            assert a[field] == b[field], f"{field} 被自报置信度改变了"

    def test_confidence_does_not_shift_consensus_strength(self):
        """The extreme case: one analyst shouting 100 vs whispering 1."""
        loud = _summary(_traces(("market_analyst", "看多", 100), ("news_analyst", "看多", 1)))
        quiet = _summary(_traces(("market_analyst", "看多", 1), ("news_analyst", "看多", 100)))
        assert loud["consensus_strength"] == quiet["consensus_strength"]

    def test_emphatic_phrasing_no_longer_earns_extra_weight(self):
        """Regression for the synthesis path: no-confidence must not outrank explicit低."""
        inferred = _summary(_traces(("market_analyst", "看多", None)))
        explicit_low = _summary(_traces(("market_analyst", "看多", "低")))
        assert inferred["consensus_strength"] == explicit_low["consensus_strength"]


# ---------------------------------------------------------------------------
# the synthesised confidence is gone
# ---------------------------------------------------------------------------

class TestConfidenceIsNotFabricated:
    def test_missing_confidence_is_reported_as_none(self):
        summary = _summary(_traces(("market_analyst", "看多", None)))
        assert summary["agent_breakdown"][0]["confidence"] is None

    def test_reported_confidence_is_carried_through_for_provenance(self):
        summary = _summary(_traces(("market_analyst", "看多", 80)))
        assert summary["agent_breakdown"][0]["confidence"] == 80

    @pytest.mark.parametrize(
        "raw,expected",
        [(80, 0.8), (0.8, 0.8), ("高", 0.85), ("low", 0.35), (None, None), ("", None), ("随便", None)],
    )
    def test_confidence_normalisation(self, raw, expected):
        got = cs._confidence_to_float(raw)
        if expected is None:
            assert got is None
        else:
            assert got == pytest.approx(expected)

    def test_confidence_is_not_inferred_from_direction_strength(self):
        """A direction argument must not change the answer.

        The previous version asserted ``inspect.signature(...) == ["value"]``, which
        only restates the declaration: direction-based inference could be re-added
        *inside* the body and that test would still pass. This calls the function
        both ways instead — if someone reintroduces a direction prior, the two
        results diverge and this fails.
        """
        import inspect

        params = list(inspect.signature(cs._confidence_to_float).parameters)
        assert params == ["value"], "不应再按方向强度反推置信度"

        # 行为层面：同一个 value，无论"方向"怎么变，结果必须一样。
        baseline = cs._confidence_to_float(0.6)
        assert baseline is not None
        for direction in ("看多", "看空", "中性", None):
            assert cs._confidence_to_float(0.6) == baseline, (
                f"置信度解析结果随方向 {direction!r} 变化 —— 方向强度又渗回了置信度"
            )

        # 且"没给值"必须是 None，而不是某个按方向合成的数
        assert cs._confidence_to_float(None) is None
        assert cs._confidence_to_float("") is None
        assert cs._confidence_to_float("unknown") is None

    def test_booleans_are_not_treated_as_confidence(self):
        assert cs._confidence_to_float(True) is None
        assert cs._confidence_to_float(False) is None

    def test_out_of_range_confidence_is_clamped_not_extrapolated(self):
        assert cs._confidence_to_float(150) == pytest.approx(1.0)
        assert cs._confidence_to_float(-20) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# weights are now role/evidence based only
# ---------------------------------------------------------------------------

class TestEffectiveWeight:
    def test_effective_weight_ignores_confidence(self):
        low = _summary(_traces(("market_analyst", "看多", 1)))
        high = _summary(_traces(("market_analyst", "看多", 100)))
        assert low["agent_breakdown"][0]["effective_weight"] == pytest.approx(
            high["agent_breakdown"][0]["effective_weight"]
        )

    def test_effective_weight_reflects_role_weight(self):
        summary = _summary(
            _traces(("market_analyst", "看多", 50), ("news_analyst", "看多", 50))
        )
        by_agent = {row["agent"]: row["effective_weight"] for row in summary["agent_breakdown"]}
        assert by_agent["market_analyst"] != by_agent["news_analyst"]

    def test_missing_key_finding_reduces_weight(self):
        with_finding = _summary(_traces(("market_analyst", "看多", 50)))
        traces = _traces(("market_analyst", "看多", 50))
        traces[0]["key_finding"] = ""
        without_finding = _summary(traces)
        assert (
            without_finding["agent_breakdown"][0]["effective_weight"]
            < with_finding["agent_breakdown"][0]["effective_weight"]
        )

    def test_disagreement_uses_the_same_weight_as_direction(self):
        """One weighting scheme, not two: contribution sums must reconcile."""
        summary = _summary(
            _traces(("market_analyst", "看多", 90), ("news_analyst", "看空", 10))
        )
        for row in summary["agent_breakdown"]:
            verdict_score = cs._direction_score(row["verdict"])
            assert row["contribution"] == pytest.approx(
                verdict_score * row["effective_weight"], abs=5e-4
            )


# ---------------------------------------------------------------------------
# A parse failure is not an opinion
# ---------------------------------------------------------------------------

class TestUnparsedTracesAreNotNeutralVotes:
    """`extract_verdict` 解析失败时会回退成中性，那是为了 trace UI 有内容可显示。

    但 `consensus_service` 读的是同一条 `verdict` 字段并把它乘进加权方向分，于是
    **「这份报告坏了」会被当成「这位分析师看平」**投出一票。那是 fail-open：共识被
    一个并不存在的观点稀释，而界面上一切正常。这里要求二者可区分。
    """

    def _traces_with_flag(self, parsed_flags):
        out = []
        for (agent, verdict, conf), parsed in zip(
            [
                ("market_analyst", "看多", 70),
                ("news_analyst", "看多", 70),
                ("macro_analyst", "看多", 70),
                ("social_media_analyst", "中性", 70),
            ],
            parsed_flags,
        ):
            out.append(
                {
                    "agent": agent,
                    "horizon": "short",
                    "verdict": verdict,
                    "confidence": conf,
                    "key_finding": f"{agent} 结论",
                    "verdict_parsed": parsed,
                }
            )
        return out

    def test_unparsed_trace_does_not_dilute_the_consensus(self):
        """三份真实看多 + 一份解析失败 ⇒ 必须等价于「只有那三份」。

        这正是旧行为的反面：旧代码把解析失败当成中性票，于是共识强度被那 0 分
        拉低（实测 100 → 76），而界面上看不出任何异常。
        """
        only_three = _summary(self._traces_with_flag([True, True, True]))
        with_broken = _summary(self._traces_with_flag([True, True, True, False]))
        assert with_broken["consensus_strength"] == only_three["consensus_strength"]
        assert with_broken["consensus_direction"] == only_three["consensus_direction"]
        # 若把坏掉的那份当投票，强度会被拉低 —— 明确钉住这个差别
        as_if_neutral = _summary(self._traces_with_flag([True, True, True, True]))
        assert as_if_neutral["consensus_strength"] != with_broken["consensus_strength"], (
            "解析失败的 trace 又被当成中性票计入了共识"
        )

    def test_unparsed_count_is_reported(self):
        """降级必须可见：否则「4 位一致」和「只有 3 位成功」在界面上一样。"""
        clean = _summary(self._traces_with_flag([True, True, True, True]))
        with_broken = _summary(self._traces_with_flag([True, True, True, False]))
        assert clean["unparsed_analyst_n"] == 0
        assert with_broken["unparsed_analyst_n"] == 1

    def test_traces_without_the_flag_still_count(self):
        """老 trace 没带该字段时必须按「已解析」处理，否则会静默丢掉历史数据。"""
        s = _summary(_traces(("market_analyst", "看多", 70), ("news_analyst", "看多", 70)))
        assert s["unparsed_analyst_n"] == 0
        assert s["consensus_direction"] == "看多"

    def test_a_genuine_neutral_trace_still_counts_as_a_neutral_vote(self):
        """真·中性票必须照常计入 —— 不能把「看平」和「坏了」一起丢掉。"""
        genuine = _summary(self._traces_with_flag([True, True, True, True]))
        assert genuine["unparsed_analyst_n"] == 0
        assert genuine["neutral_ratio"] > 0
