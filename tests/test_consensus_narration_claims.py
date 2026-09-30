"""共识叙述句必须与它引用的口径一致（见 docs/report-judgment-accuracy-audit.md §4.2、§4.3）。

## 两条都是"说得比算得狠"

### §4.2 「X 视角权重更强」——说的不是权重

`dominant_horizon` 取的是各周期 `|sum/weight|`（**方向信号强度**）的 argmax，
叙述却写成「权重更强」。实测探针：short 实际权重 **4.862**、medium **1.990**
（short 是 2.44 倍），叙述照样写「medium 视角权重更强」——因为 medium 的
方向信号更强。用户读到"权重"二字会以为资金/证据在 medium 那边更多。

判据（本文件钉住的不变量）：
**只要叙述里出现「权重更…」，被点名的周期就必须是实际权重的 argmax。**

### §4.3 「支持/反对」一个词两套参照系

* 比例（`support_ratio` 等）按 `verdict_score` 的**正负**累计 —— 参照系是 **0**；
* 名单（`supporting_agents` 等）按 `final_direction`（或共识均值）分类 ——
  参照系是**最终结论**。

两者在同一个工具输出里并列（`tools/consensus.py` 的 E1 与「分析师立场」）。
于是出现"支持 0.38，支持者：基本面"，而基本面投的其实是**看空**。

更严重的是**看空报告**：共识方向为看空时，多数投看空的分析师被算进
`oppose_ratio`，卡片显示「共识方向 看空｜支持 20%／反对 65%」——
字面读起来是"多数人反对这个看空结论"，事实正相反。**每一份看空报告都会被误读。**

判据：比例与名单必须同参照系（都由同一份 stance 分类导出），
且参照系本身要显式可见（`stance_reference`），不能靠读者猜。
"""

from __future__ import annotations

from api.services.consensus_service import build_consensus_summary


def _trace(agent, horizon, verdict, confidence="高", key_finding="依据"):
    return {
        "agent": agent,
        "horizon": horizon,
        "verdict": verdict,
        "confidence": confidence,
        "key_finding": key_finding,
    }


def _risk(**kw):
    base = {"latest_risk_verdict": "pass", "execution_preconditions": [], "de_risk_triggers": []}
    base.update(kw)
    return base


def _horizon_weights(summary):
    """从输出里恢复每个周期的**实际权重**（effective_weight 之和）。"""
    totals: dict[str, float] = {}
    for item in summary["agent_breakdown"]:
        h = str(item.get("horizon") or "")
        totals[h] = totals.get(h, 0.0) + float(item.get("effective_weight") or 0.0)
    return totals


# ══════════════════════════════════════════════════════════════════════════
# §4.2 权重口径
# ══════════════════════════════════════════════════════════════════════════


def _two_horizon_case():
    """构造「权重最大的周期」与「方向信号最强的周期」**不是同一个**的局面。

    short：权重更重（3 位分析师，含加权系数更高的资金面/量价面），但三人全投中性
           → 该周期方向均值 0，信号强度 0。
    medium：权重更轻（2 位），但两人一致看多 → 方向信号强度 1.0 拉满。

    于是 `dominant_horizon`（按 |均值|）= medium，而真实权重 argmax = short。
    """
    return {
        "analyst_traces": [
            _trace("market_analyst", "short", "中性"),
            _trace("smart_money_analyst", "short", "中性"),
            _trace("volume_price_analyst", "short", "中性"),
            _trace("fundamentals_analyst", "medium", "看多"),
            _trace("macro_analyst", "medium", "看多"),
        ],
        "risk_feedback_state": _risk(),
    }


def test_weight_claim_names_the_real_weight_argmax():
    """**核心不变量**：说「权重更强」就必须是权重最大的那个周期。"""
    summary = build_consensus_summary(
        result_data=_two_horizon_case(), final_direction="看多", final_confidence=60
    )
    assert summary is not None

    weights = _horizon_weights(summary)
    weight_argmax = max(weights, key=lambda h: weights[h])
    # 夹具前提自检：确实是"权重 argmax ≠ 方向 argmax"的局面，否则本用例没测到东西
    assert weight_argmax == "short", f"夹具没构造出预期局面：{weights}"
    assert summary["dominant_horizon"] == "medium", "夹具前提：方向信号更强的是 medium"

    text = summary["summary"]
    if "权重更" in text:
        assert weight_argmax in text, (
            f"叙述声称「权重更强」的周期必须是实际权重 argmax={weight_argmax}，"
            f"实际叙述：{text!r}（各周期权重 {weights}）"
        )
        assert "medium 视角权重更强" not in text, (
            f"medium 的权重只有 {weights.get('medium')}，小于 short 的 "
            f"{weights.get('short')}，不能说它权重更强：{text!r}"
        )


def test_weight_claim_reports_the_measured_ratio_not_a_vague_word():
    """万一保留「权重」措辞，就必须同时说出实测倍数，让读者能自己核对。"""
    summary = build_consensus_summary(
        result_data=_two_horizon_case(), final_direction="看多", final_confidence=60
    )
    text = summary["summary"]
    if "权重更" in text:
        weights = _horizon_weights(summary)
        heavier = max(weights, key=lambda h: weights[h])
        lighter = min(weights, key=lambda h: weights[h])
        assert f"{heavier}" in text
        # 必须能看出量级差（否则"更重"是个不可核对的说法）
        assert ("倍" in text) or (str(round(weights[heavier], 2)) in text), (
            f"「更重」必须可核对：{text!r}（{heavier}={weights[heavier]}, {lighter}={weights[lighter]}）"
        )


def test_single_horizon_does_not_claim_a_comparison():
    """只有一个周期时，**没有可比对象**，不得说"某个视角更强"。"""
    result = {
        "analyst_traces": [
            _trace("market_analyst", "short", "看多"),
            _trace("news_analyst", "short", "偏多"),
            _trace("fundamentals_analyst", "short", "看多"),
        ],
        "risk_feedback_state": _risk(),
    }
    summary = build_consensus_summary(result_data=result, final_direction="看多", final_confidence=70)
    assert summary is not None
    text = summary["summary"]
    assert "权重更" not in text and "更强" not in text, (
        f"单周期没有对比对象，不能说谁更强：{text!r}"
    )


# ══════════════════════════════════════════════════════════════════════════
# §4.3 支持/反对必须同一个参照系
# ══════════════════════════════════════════════════════════════════════════


def test_bearish_panel_is_not_reported_as_opposing_itself():
    """**每一份看空报告都会被误读**：全票看空时，支持率必须是 1.0。

    修复前：比例按正负号累计 → 看空票全部计入 `opposing_weight`
    → 「共识方向 看空｜支持 0%／反对 100%」，字面意思是"所有人反对这个结论"。
    """
    result = {
        "analyst_traces": [
            _trace("market_analyst", "short", "看空"),
            _trace("news_analyst", "short", "看空"),
            _trace("fundamentals_analyst", "medium", "看空"),
        ],
        "risk_feedback_state": _risk(),
    }
    summary = build_consensus_summary(result_data=result, final_direction="看空", final_confidence=70)
    assert summary is not None
    assert summary["consensus_direction"] in {"看空", "偏空"}
    assert summary["support_ratio"] == 1.0, (
        f"全票看空时支持率应为 1.0，实际 {summary['support_ratio']}"
        "（说明比例仍以 0 为参照系，把看空票算成了反对）"
    )
    assert summary["oppose_ratio"] == 0.0
    assert summary["neutral_ratio"] == 0.0


def test_support_ratio_and_supporting_agents_share_one_frame():
    """名单与比例必须同参照系：被列为"支持"的人，其权重必须计入 support_ratio。

    构造"少数看空 + 多数看多，而最终结论是看空"的局面：
    支持最终结论的是那**少数**看空者，比例必须跟着一起小。
    """
    result = {
        "analyst_traces": [
            _trace("market_analyst", "short", "看多"),
            _trace("news_analyst", "short", "看多"),
            _trace("smart_money_analyst", "short", "偏多"),
            _trace("fundamentals_analyst", "medium", "看空"),
        ],
        "risk_feedback_state": _risk(),
    }
    summary = build_consensus_summary(result_data=result, final_direction="看空", final_confidence=55)
    assert summary is not None

    labels_by_stance = {
        item["label"]: item["stance"] for item in summary["agent_breakdown"]
    }
    supporting = summary["supporting_agents"]
    assert supporting, "最终结论是看空，投看空的基本面分析师应在支持名单里"
    for label in supporting:
        assert labels_by_stance.get(label) == "supporting", (
            f"{label} 出现在支持名单里，但其 stance 是 {labels_by_stance.get(label)}"
        )
    # 比例与名单同源：支持率的分子必须正是 stance=supporting 的权重
    weights = {
        item["label"]: float(item.get("effective_weight") or 0.0)
        for item in summary["agent_breakdown"]
    }
    total = sum(weights.values())
    expected = sum(weights[l] for l in supporting) / total
    assert abs(summary["support_ratio"] - round(expected, 3)) < 1e-3, (
        f"支持名单权重占比 {expected:.3f} 与 support_ratio={summary['support_ratio']} 不一致"
    )


def test_stance_reference_is_explicit():
    """参照系必须显式可见——否则读者无法判断"支持"是在支持什么。"""
    result = {
        "analyst_traces": [
            _trace("market_analyst", "short", "看多"),
            _trace("fundamentals_analyst", "medium", "看空"),
        ],
        "risk_feedback_state": _risk(),
    }
    summary = build_consensus_summary(result_data=result, final_direction="看空", final_confidence=55)
    assert summary is not None
    ref = summary.get("stance_reference")
    assert ref in {"final_direction", "panel_mean"}, f"参照系必须显式：{ref!r}"

    no_final = build_consensus_summary(result_data=result, final_direction=None)
    assert no_final is not None
    assert no_final["stance_reference"] == "panel_mean", (
        "没有最终结论时，参照系只能退回面板均值，且必须说明"
    )
