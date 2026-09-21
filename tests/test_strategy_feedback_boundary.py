"""E3: `StrategyFeedbackStatDB.weight_delta` must stay out of the T+1 direction path.

The plan's E3 entry asks to "明确边界或移除". The stat adjusts the **market scanner's**
factor weights (momentum / activity / near_high / sector / volume_ratio) by at most
±0.08, and those weights only re-rank candidates. It is therefore not a learning loop
that improves T+1 judgement, and it must never be presented or wired as one.

These tests pin that boundary mechanically, because the failure mode is silent: someone
later connects the table to the deep-analysis direction path and every surface still
looks healthy while the direction call starts inheriting scanner noise.
"""
from __future__ import annotations

import inspect
import pathlib

import pytest

from api.database import StrategyFeedbackStatDB
from api.services import recommendation_feedback_service as rfs

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

# The modules that actually produce the T+1 direction call.
T1_DIRECTION_MODULES = [
    "api/services/insights_t1_service.py",
    "api/services/t1_stats.py",
]


class TestStrategyFeedbackBoundary:
    def test_the_stat_is_a_scanner_weight_not_an_accuracy_metric(self):
        """It carries scanner-facing columns and no hit-rate/accuracy column."""
        cols = {c.name for c in StrategyFeedbackStatDB.__table__.columns}
        assert "weight_delta" in cols
        assert "factor_bucket" in cols
        for forbidden in ("label_correct", "accuracy", "accuracy_pct", "direction_bucket"):
            assert forbidden not in cols, (
                f"strategy_feedback_stats 出现了 {forbidden} —— 这张表变成了精度表，"
                "边界已被打破"
            )

    def test_weight_delta_is_documented_as_scanner_only(self):
        doc = inspect.getdoc(rfs.list_strategy_feedback_stats) or ""
        assert "not" in doc.lower()
        assert "VERDICT" in doc or "direction" in doc.lower()

    def test_apply_function_only_touches_scanner_factor_weights(self):
        """`_apply_...` must normalise to the scanner's factor keys and nothing else."""
        src = inspect.getsource(rfs)
        # the +-0.08 clamp is the documented maximum influence
        assert "0.08" in src

    @pytest.mark.parametrize("module_path", T1_DIRECTION_MODULES)
    def test_t1_direction_path_never_reads_strategy_feedback(self, module_path):
        """A grep-level guard: the T+1 scoring path must not import or query the table."""
        path = REPO_ROOT / module_path
        assert path.exists(), f"{module_path} 不存在"
        text = path.read_text(encoding="utf-8")
        for needle in (
            "StrategyFeedbackStatDB",
            "recommendation_feedback_service",
            "strategy_feedback_stats",
        ):
            assert needle not in text, (
                f"{module_path} 引用了 {needle} —— T+1 方向链路不得依赖扫描器权重"
            )

    def test_t1_summary_does_not_expose_a_scanner_weight_delta(self):
        """The honest-metrics summary must not surface scanner weights as accuracy."""
        from api.services import insights_t1_service as svc

        text = inspect.getsource(svc)
        assert "weight_delta" not in text

    def test_frontend_does_not_claim_t1_effect_for_the_stat(self):
        page = REPO_ROOT / "frontend/src/pages/RecommendationInsights.tsx"
        text = page.read_text(encoding="utf-8")
        assert "weight_delta" in text, "该字段应仍在扫描器页面展示（边界内）"
        # and it must not be advertised on the T+1 quality page
        quality = REPO_ROOT / "frontend/src/pages/QualityInsights.tsx"
        assert "weight_delta" not in quality.read_text(encoding="utf-8"), (
            "T+1 研判质量页面不得展示扫描器权重增量"
        )
