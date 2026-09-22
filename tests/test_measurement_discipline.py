"""M5：单次采样不可信——把它变成**代码约束**，而不是靠纪律。

## 为什么

本会话实测：**同一个问题、同一套配置，连跑三次 = 8.4s / 13.2s / 19.8s（2.4 倍离散）**。
如果只采一次，任何结论都能"被证明"：

* 取 8.4s 当基线、19.8s 当新值 → "变慢了 136%"
* 反过来 → "变快了 58%"

同样的陷阱在别处也踩过：两个 `max_tokens` 假设、一个 `reasoning_effort` 假设，
单次采样全都"成立"，重复采样后全部被推翻。

规划器打分同理：精确率两次跑出 88.1↔92.9，**单次分数不足以判定回归**。

## 因此

把判据做成纯函数：**样本数不够就拒绝给方向性结论**。
这样"用单次采样下结论"在 API 层面就做不出来，不用指望人记得。
"""
from __future__ import annotations

import pytest

from tradingagents.measurement import (
    MIN_SAMPLES_FOR_VERDICT,
    compare_samples,
    summarize_samples,
)


# --------------------------------------------------------------- 单组统计

def test_summary_reports_dispersion_not_just_mean():
    """必须报**离散度**。均值正是把双峰分布藏起来的那种统计量。

    实测教训：工具耗时占比"均值 4.5%"掩盖了一个中位数 1.7%、
    P90 29.6%、max 86.4% 的双峰分布。
    """
    s = summarize_samples([8.4, 13.2, 19.8])
    assert s["n"] == 3
    assert s["median"] == 13.2
    assert s["min"] == 8.4
    assert s["max"] == 19.8
    # 离散度：max/min
    assert s["spread_ratio"] == pytest.approx(19.8 / 8.4, rel=1e-6)
    assert "mean" not in s or s["mean"] is not None  # 均值可以给，但不能只有它


def test_summary_of_single_sample_is_flagged_unstable():
    """只有一次采样时，必须显式标成"无法判断稳定性"。"""
    s = summarize_samples([12.0])
    assert s["n"] == 1
    assert s["stable"] is False
    assert s["note"], "单次采样没有给出提示"


def test_summary_flags_high_dispersion():
    """离散度大的组必须被标成不稳定（8.4↔19.8 这种）。"""
    assert summarize_samples([8.4, 13.2, 19.8])["stable"] is False
    assert summarize_samples([10.1, 10.2, 10.0])["stable"] is True


def test_summary_handles_empty():
    """空输入不能崩，也不能伪装成"0 秒"。"""
    s = summarize_samples([])
    assert s["n"] == 0
    assert s["median"] is None
    assert s["stable"] is False


# --------------------------------------------------------- 两组比较（核心）

def test_single_sample_cannot_establish_direction():
    """**核心回归**：每组只有一次采样时，必须拒绝给方向性结论。

    这正是本会话踩过的坑：取 8.4s 与 19.8s 各一次，"证明"了变慢 136%。
    """
    out = compare_samples([19.8], [8.4])
    assert out["verdict"] == "insufficient_samples"
    assert "faster" != out["verdict"] and "slower" != out["verdict"]
    assert out["note"], "拒绝给结论时必须说明原因"


def test_two_samples_still_insufficient():
    """两次也不够（阈值是 3）。"""
    out = compare_samples([19.8, 18.0], [8.4, 9.0])
    assert out["verdict"] == "insufficient_samples"


def test_overlapping_ranges_are_indistinguishable():
    """**核心回归**：区间重叠 → 判"无法区分"，不能报变快/变慢。

    8.4/13.2/19.8 与 9.0/13.0/20.0 这种几乎一样的两组，
    中位数可能有差，但**区间重叠**说明差异被噪声吃掉了。
    """
    out = compare_samples([8.4, 13.2, 19.8], [9.0, 13.0, 20.0])
    assert out["verdict"] == "indistinguishable", out


def test_clearly_separated_medians_yield_a_direction():
    """**真正分开**的两组才给方向：且要说明依据。"""
    out = compare_samples([10.0, 10.2, 10.1], [20.0, 20.3, 20.1])
    assert out["verdict"] == "slower"
    assert out["delta_pct"] > 0
    assert out["before"]["median"] and out["after"]["median"]

    rev = compare_samples([20.0, 20.3, 20.1], [10.0, 10.2, 10.1])
    assert rev["verdict"] == "faster"


def test_same_config_triple_run_is_the_reference_case():
    """复现本会话那个真实场景：8.4/13.2/19.8 vs 一份"新"采样。

    无论新的那次落在哪里，只要它落在旧区间内，就不该给出方向性结论。
    """
    before = [8.4, 13.2, 19.8]
    for single in (8.4, 13.2, 19.8):
        out = compare_samples(before, [single])
        assert out["verdict"] == "insufficient_samples", (
            f"用单次采样 {single}s 与已有区间比较就得出了方向性结论"
        )


def test_verdict_always_carries_evidence():
    """任何结论都要带上支撑数据，便于复核——不能只给一个词。"""
    out = compare_samples([10.0, 10.2, 10.1], [20.0, 20.3, 20.1])
    for key in ("before", "after", "verdict", "min_samples"):
        assert key in out, f"结论缺少 {key}"
    assert out["min_samples"] == MIN_SAMPLES_FOR_VERDICT


# ------------------------------------------------- 接进真实归因端点（不是死代码）

def test_attribution_summary_carries_structural_sufficiency():
    """归因汇总必须带**结构化**的样本充分性判定，而不只是一句文案。

    文案（`sample.warning`）人可能不读；结构化字段可以被程序消费，
    于是"拿单次采样下结论"在调用方那里就做不出来。
    """
    from api.services.trace_service import _summarize_rows

    class _Row:
        """`_summarize_rows` 走属性访问（真实传入的是 ORM 行）。"""

        def __init__(self, i: int):
            self.created_at = f"2026-09-22T10:{i:02d}:00"
            self.status = "ok"
            self.tier = "light"
            self.latency_ms = 1000 + i * 10
            self.tool_calls = "[]"
            self.tokens = 100
            self.model_calls = 1
            self.steps = "[]"
            self.user_id = "u1"

    rows = [_Row(i) for i in range(5)]
    out = _summarize_rows(rows)
    assert "latency_sufficiency" in out["sample"], "归因汇总没有带样本充分性判定"
    suff = out["sample"]["latency_sufficiency"]
    assert suff["n"] == 5
    assert "stable" in suff and "median" in suff and "spread_ratio" in suff


# --------------------------------------------- 0 不是"很快"，是"没有测量值"

def test_zero_latency_rows_are_not_treated_as_instant():
    """**核心回归**：耗时里的 0 必须能被排除，否则同时污染均值与离散度。

    实测：trace 里有 `latency_ms=0, tokens=0, model_calls=0` 的占位行
    （500 行里有 5 行）。把它们当测量值会：
      * 拉低均值（0 被读成"瞬间完成"）
      * 让 `min=0`，于是 max/min 无定义 → 离散度直接算不出来
    """
    vals = [14552.0, 21840.0, 0.0, 4763.0, 0.0]
    naive = summarize_samples(vals)
    assert naive["spread_ratio"] is None, "本用例的前提是 0 会让离散度无定义"

    fixed = summarize_samples(vals, drop_non_positive=True)
    assert fixed["n"] == 3, "占位行没有被排除"
    assert fixed["dropped"] == 2, "没有报出排除了几条"
    assert fixed["min"] == 4763.0
    assert fixed["median"] == 14552.0
    assert fixed["spread_ratio"] == pytest.approx(21840.0 / 4763.0, rel=1e-6)


def test_zero_is_kept_by_default_because_it_can_be_meaningful():
    """反向护栏：默认**不**丢 0。

    对"失败次数"这类量，`0` 是真实取值。隐式丢 0 会让"零失败"消失，
    所以是否丢弃必须由调用方按语义显式声明。
    """
    s = summarize_samples([0.0, 0.0, 3.0])
    assert s["n"] == 3
    assert s["dropped"] == 0


def test_all_dropped_is_reported_not_hidden():
    """全被排除时要说清楚是"没有有效采样"，不能伪装成正常结果。"""
    s = summarize_samples([0.0, 0.0], drop_non_positive=True)
    assert s["n"] == 0
    assert s["median"] is None
    assert s["stable"] is False
    assert "2" in s["note"], f"没说清排除了几条：{s['note']}"


def test_attribution_excludes_zero_latency_placeholders():
    """归因端点必须走排除逻辑——否则离散度永远是 None，"稳不稳"永远答不出。"""
    from api.services.trace_service import _summarize_rows

    class _Row:
        def __init__(self, latency, status="completed"):
            self.created_at = "2026-09-22T10:00:00"
            self.status = status
            self.tier = "light"
            self.latency_ms = latency
            self.tool_calls = "[]"
            self.tokens = 100
            self.model_calls = 1
            self.steps = "[]"
            self.user_id = "u1"

    rows = [_Row(x) for x in (14552, 21840, 0, 4763, 0)]
    suff = _summarize_rows(rows)["sample"]["latency_sufficiency"]
    assert suff["n"] == 3, "归因端点没有排除占位行"
    assert suff["spread_ratio"] is not None, "离散度仍然算不出来"
