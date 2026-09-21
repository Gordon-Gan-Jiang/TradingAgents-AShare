"""校准端点暴露维度判别力 + 权重版本化激活（审计 D2 修复）。

## 修复前的实际状态

* **7.7 位置不符**：维度判别力统计实现在离线回放模块，`/v1/signals/calibration`
  **完全不输出**它——而该端点是校准闭环的对外窗口
* **7.10 未实现**：只有 `check_activation_gate`（拒绝逻辑）与 `with_weight`
  （纯函数），**没有任何机制产生并持久化新的 `signal_version`**

于是「权重是被数据校准的」这句话在产品上无法落地：没有地方能落一次权重变更，
也就没有版本可对照。

## 设计边界

权重从数据库加载这件事必须在**应用层**完成——`signals/` 是纯函数层，
不得依赖 DB（由架构边界测试强制）。信号层只接收一个 `WashoutWeights` 实例。
"""

from __future__ import annotations

import pytest


def _verdict(user_id: str, symbol: str, as_of: str, *, contributions, state="washout",
             margin=0.6) -> str:
    from api.database import SignalVerdictDB, get_db_ctx
    from tradingagents.agent.repository import new_id

    vid = new_id()
    with get_db_ctx() as db:
        db.add(
            SignalVerdictDB(
                id=vid, user_id=user_id, symbol=symbol, as_of=as_of, probe="washout",
                state=state, confidence=70, margin=margin, signal_version="washout-v1.0.0",
                contributions={"components": contributions, "scores": {}},
            )
        )
        db.commit()
    return vid


def _outcome(verdict_id: str, *, hit: bool, evaluable: bool = True, horizon: int = 5,
             ret_pct: float | None = 1.0) -> None:
    from api.database import SignalVerdictOutcomeDB, get_db_ctx
    from tradingagents.agent.repository import new_id

    with get_db_ctx() as db:
        db.add(
            SignalVerdictOutcomeDB(
                id=new_id(), verdict_id=verdict_id, horizon_days=horizon,
                ref_date="2026-03-09", threshold_hit=hit, evaluable=evaluable,
                ret_pct=ret_pct, reason=None if evaluable else "insufficient window",
            )
        )
        db.commit()


def _component(key: str, contribution: float):
    return {
        "key": key,
        "core": True,
        "available": True,
        "magnitude": abs(contribution),
        "side": "washout" if contribution > 0 else "distribution",
        "weight": 1.0,
        "raw": {},
        "evidence": [],
        "unavailable_reason": None,
    }


# ══════════════════════════════════════════════════════════════════════════
# D2-a：校准端点输出维度判别力
# ══════════════════════════════════════════════════════════════════════════


def test_calibration_report_includes_dimension_power():
    """校准端点必须输出**按维度**的判别力统计——7.7 的原始诉求。"""
    from api.services.signal_service import calibration_report

    base = _verdict("dim-u", "600519.SH", "2026-01-05",
                    contributions=[_component("volume_contraction", 0.9)])
    for i in range(12):
        vid = _verdict("dim-u", f"6005{i:02d}.SH", f"2026-01-{5 + i:02d}",
                       contributions=[_component("volume_contraction", 0.9 if i % 2 == 0 else -0.2)])
        _outcome(vid, hit=(i % 2 == 0))
    _outcome(base, hit=True)

    report = calibration_report(user_id="dim-u")
    # 判别力属于「由监控校准」类指标，因此位于 monitored 段内
    # （与「由构造保证」的未溯源率分列——混在一起会让人分不清哪个更可信）
    assert "dimension_power" in report["monitored"]
    assert "volume_contraction" in report["monitored"]["dimension_power"]
    stats = report["monitored"]["dimension_power"]["volume_contraction"]
    assert stats["sample"] > 0


def test_dimension_power_excludes_non_evaluable_outcomes():
    """停牌样本不得进入判别力统计——与命中率同一条纪律。"""
    from api.services.signal_service import calibration_report

    for i in range(12):
        vid = _verdict("dim-ne", f"6006{i:02d}.SH", f"2026-02-{1 + i:02d}",
                       contributions=[_component("chip_distribution", 0.7)])
        _outcome(vid, hit=False, evaluable=False)

    stats = calibration_report(user_id="dim-ne")["monitored"]["dimension_power"].get("chip_distribution")
    assert stats is None or stats["sample"] == 0


def test_calibration_reports_that_weight_activation_is_gated():
    from api.services.signal_service import calibration_report

    report = calibration_report(user_id="gate-u")
    gate = report["weight_activation_gate"]
    assert gate["min_forward_verdicts"] == 100
    assert gate["satisfied"] is False


# ══════════════════════════════════════════════════════════════════════════
# D2-b：权重版本化激活
# ══════════════════════════════════════════════════════════════════════════


def test_activation_is_refused_below_the_gate():
    """样本不足时**拒绝**激活，并报告差距。

    本平台有真实的样本饥饿前科——用一个 30 样本的噪声替换可用先验是净损失。
    """
    from api.services.signal_weight_service import propose_activation

    proposal = propose_activation(
        forward_samples=40, per_dimension={"volume_contraction": 20}
    )
    assert proposal.allowed is False
    assert proposal.needed_forward == 100
    assert proposal.have_forward == 40
    assert "volume_contraction" in proposal.blocking_dimensions


def test_activation_creates_a_new_versioned_record():
    from api.services.signal_weight_service import activate, list_versions

    activated = activate(
        weights={"weight_volume_contraction": 1.35},
        rationale="回放显示缩量维度判别力提升，前瞻样本已达门槛",
        forward_samples=150,
        per_dimension={"volume_contraction": 60},
        activated_by="tester",
    )
    assert activated.version
    assert activated.version != "washout-v1.0.0"
    assert activated.sample_size == 150
    assert activated.rationale

    versions = list_versions()
    assert any(v.version == activated.version for v in versions)


def test_activation_records_the_sample_size_and_rationale():
    from api.services.signal_weight_service import activate, get_version

    activated = activate(
        weights={"weight_volume_contraction": 1.35},
        rationale="理由必须落库，否则事后无法判断这次变更是凭什么做的",
        forward_samples=200,
        per_dimension={"volume_contraction": 80},
        activated_by="tester",
    )
    row = get_version(activated.version)
    assert row is not None
    assert row.sample_size == 200
    assert "理由" in row.rationale
    assert row.activated_by == "tester"


def test_previous_version_is_retained_for_comparison():
    """旧版本必须保留可对照——否则无法回答「新权重到底有没有更好」。"""
    from api.services.signal_weight_service import activate, list_versions

    first = activate(
        weights={"weight_volume_contraction": 1.1}, rationale="第一次",
        forward_samples=120, per_dimension={"volume_contraction": 50}, activated_by="t",
    )
    second = activate(
        weights={"weight_volume_contraction": 1.4}, rationale="第二次",
        forward_samples=200, per_dimension={"volume_contraction": 90}, activated_by="t",
    )
    versions = {v.version for v in list_versions()}
    assert {first.version, second.version} <= versions


def test_only_one_version_is_active():
    from api.services.signal_weight_service import activate, active_version

    activate(weights={"weight_volume_contraction": 1.2}, rationale="a",
             forward_samples=110, per_dimension={"volume_contraction": 40}, activated_by="t")
    v2 = activate(weights={"weight_volume_contraction": 1.5}, rationale="b",
                  forward_samples=130, per_dimension={"volume_contraction": 45}, activated_by="t")
    assert active_version() == v2.version


def test_active_weights_fall_back_to_defaults_when_nothing_activated():
    """没有任何激活记录时必须回落到内置先验——不得返回空权重。"""
    from api.database import SignalWeightVersionDB, get_db_ctx
    from api.services.signal_weight_service import active_weights
    from tradingagents.signals.weights import DEFAULT_WEIGHTS

    # 会话级共享 DB：先清空激活状态，否则前一个用例留下的激活会让本用例失真
    with get_db_ctx() as db:
        db.query(SignalWeightVersionDB).delete()
        db.commit()

    weights = active_weights(user_id=None)
    assert weights.version == DEFAULT_WEIGHTS.version
    assert weights.weight_for("volume_contraction") == DEFAULT_WEIGHTS.weight_for("volume_contraction")


def test_active_weights_reflect_the_activated_version():
    from api.services.signal_weight_service import activate, active_weights

    activate(weights={"weight_volume_contraction": 1.77}, rationale="c",
             forward_samples=120, per_dimension={"volume_contraction": 40}, activated_by="t")
    weights = active_weights()
    assert weights.weight_for("volume_contraction") == pytest.approx(1.77)
    assert weights.version != "washout-v1.0.0"


def test_activation_requires_a_rationale():
    """没有理由的权重变更等于把「拍脑袋」制度化。"""
    from api.services.signal_weight_service import activate

    with pytest.raises(ValueError, match="rationale"):
        activate(weights={"weight_volume_contraction": 1.2}, rationale="",
                 forward_samples=150, per_dimension={"volume_contraction": 60}, activated_by="t")


def test_activation_requires_the_per_dimension_floor():
    from api.services.signal_weight_service import activate

    with pytest.raises(ValueError, match="per-dimension|维度"):
        activate(weights={"weight_volume_contraction": 1.2}, rationale="ok",
                 forward_samples=150, per_dimension={"volume_contraction": 5}, activated_by="t")


# ══════════════════════════════════════════════════════════════════════════
# 激活必须真的影响评估（否则激活记录只是一张没人读的表）
# ══════════════════════════════════════════════════════════════════════════


def test_evaluation_uses_the_activated_weights():
    from api.services.signal_service import evaluate_symbol
    from api.services.signal_weight_service import activate
    from tests.test_acceptance_conversational_signal_agent import _ports_factory

    factory = _ports_factory()

    baseline = evaluate_symbol(
        symbol="600519.SH", as_of="2026-01-16", window_days=20,
        user_id="w-u", ports_factory=factory,
    )

    activate(weights={"weight_volume_contraction": 4.0}, rationale="把量能权重显著调高",
             forward_samples=120, per_dimension={"volume_contraction": 40}, activated_by="t")

    updated = evaluate_symbol(
        symbol="600519.SH", as_of="2026-01-16", window_days=20,
        user_id="w-u", ports_factory=factory,
    )

    assert updated["signal_version"] != baseline["signal_version"]
    # 权重变了，得分必须随之变化——否则激活是装饰
    assert updated["scores"]["washout"] != baseline["scores"]["washout"]


def test_explicit_weights_override_the_activated_version():
    """显式传入权重必须优先——回放校准需要用它跑指定版本。"""
    from api.services.signal_service import evaluate_symbol
    from tests.test_acceptance_conversational_signal_agent import _ports_factory
    from tradingagents.signals.weights import DEFAULT_WEIGHTS

    payload = evaluate_symbol(
        symbol="600519.SH", as_of="2026-01-16", window_days=20,
        user_id="w-u2", ports_factory=_ports_factory(), weights=DEFAULT_WEIGHTS,
    )
    assert payload["signal_version"] == DEFAULT_WEIGHTS.version
