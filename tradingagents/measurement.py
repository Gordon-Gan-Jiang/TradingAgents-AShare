"""度量纪律：把"单次采样不可信"做成**代码约束**，而不是靠人记得。

## 问题

本会话实测：**同一个问题、同一套配置，连跑三次 = 8.4s / 13.2s / 19.8s**
（2.4 倍离散）。只采一次的话，任何结论都能"被证明"：

* 取 8.4s 当基线、19.8s 当新值 → "变慢了 136%"
* 反过来取 → "变快了 58%"

同一个陷阱在本会话踩过三次：两个 `max_tokens` 假设、一个 `reasoning_effort`
假设，**单次采样全都"成立"，重复采样后全部被推翻**。规划器打分同理——
精确率两次跑出 88.1↔92.9，单次分数不足以判定回归。

## 做法

把判据做成纯函数（无 IO、可单测）：**样本数不够就拒绝给方向性结论**。
"用单次采样下结论"于是变成 API 层面做不出来的事，不用指望纪律。

## 判据为什么用"区间重叠"而不是"中位数差"

中位数差在小样本下方差极大。两组各 3 个样本，中位数差 20% 完全可能只是噪声。
区间重叠是一条**保守**判据：宁可说"无法区分"，也不要报一个假的方向。
这与本项目"判定权在确定性层"的一贯取向一致——证据不足时不下判断。
"""
from __future__ import annotations

from statistics import median
from typing import Any, Sequence

# 少于这么多次采样，**不给方向性结论**。
#
# 为什么是 3：本会话实测离散度达 2.4 倍，两次采样仍可能同时落在分布的
# 同一侧（例如都偏快），于是给出错误方向。3 次是能看出"有没有抖"的最小值，
# 也是成本上可接受的（相对单次 3 倍）。
MIN_SAMPLES_FOR_VERDICT = 3

# 一组采样内 max/min 超过这个倍数就标成不稳定。
# 实测的 19.8/8.4 = 2.36，取 1.5 能抓住它，同时放过正常的抖动（1.1~1.2）。
_STABLE_SPREAD_RATIO = 1.5


def summarize_samples(
    values: Sequence[float],
    *,
    drop_non_positive: bool = False,
) -> dict[str, Any]:
    """汇总一组采样：**必须报离散度**，不能只给均值。

    均值正是会把双峰分布藏起来的那种统计量。实测教训：工具耗时占比
    "均值 4.5%" 掩盖了一个中位数 1.7% / P90 29.6% / max 86.4% 的双峰分布。

    `drop_non_positive=True` 用于**耗时**这类量：`0` 通常意味着"没有测量值"
    （实测 trace 里有 `latency_ms=0, tokens=0, model_calls=0` 的占位行），
    不是"瞬间完成"。把它算进去会同时污染均值（被拉低）与离散度
    （`min=0` 让 max/min 无定义）。

    默认**不**丢弃：对"失败次数"这类量，`0` 是有意义的真实取值。
    所以这里不做隐式判断，由调用方按语义显式声明。
    """
    vals = [float(v) for v in values if v is not None]
    dropped = 0
    if drop_non_positive:
        kept = [v for v in vals if v > 0]
        dropped = len(vals) - len(kept)
        vals = kept

    if not vals:
        return {
            "n": 0,
            "median": None,
            "min": None,
            "max": None,
            "mean": None,
            "spread_ratio": None,
            "stable": False,
            "dropped": dropped,
            "note": "没有采样，无法判断"
            + (f"（已排除 {dropped} 条非正值记录）" if dropped else ""),
        }

    lo, hi = min(vals), max(vals)
    # 全为 0 时比值无定义；此时若全部相等就是稳定的
    if lo == 0:
        spread: float | None = 1.0 if hi == 0 else None
    else:
        spread = hi / lo

    if len(vals) < MIN_SAMPLES_FOR_VERDICT:
        stable = False
        note = f"只有 {len(vals)} 次采样（少于 {MIN_SAMPLES_FOR_VERDICT}），无法判断稳定性"
    elif spread is None:
        stable = False
        note = "离散度无法计算（含 0 与非 0 混合），按不稳定处理"
    elif spread > _STABLE_SPREAD_RATIO:
        stable = False
        note = f"离散度 {spread:.2f}× 超过 {_STABLE_SPREAD_RATIO}×，采样不稳定"
    else:
        stable = True
        note = ""

    return {
        "n": len(vals),
        "median": median(vals),
        "min": lo,
        "max": hi,
        "mean": sum(vals) / len(vals),
        "spread_ratio": spread,
        "stable": stable,
        "dropped": dropped,
        "note": note,
    }


def _overlap(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """两个区间是否重叠。任一组不完整时按"重叠"处理（保守）。"""
    if None in (a.get("min"), a.get("max"), b.get("min"), b.get("max")):
        return True
    return not (a["max"] < b["min"] or b["max"] < a["min"])


def compare_samples(
    before: Sequence[float],
    after: Sequence[float],
    *,
    min_samples: int = MIN_SAMPLES_FOR_VERDICT,
) -> dict[str, Any]:
    """比较两组采样，给出**保守**结论。

    verdict 取值：

    * `insufficient_samples` —— 任一组样本数不足。**拒绝给方向**
    * `indistinguishable` —— 样本够但区间重叠，差异被噪声吃掉
    * `faster` / `slower` —— 区间分离，且中位数差异方向明确

    任何情况下都返回 `before` / `after` 的完整汇总与 `min_samples`，
    便于复核——结论不能只是一个词。
    """
    b = summarize_samples(before)
    a = summarize_samples(after)
    base = {
        "before": b,
        "after": a,
        "min_samples": min_samples,
    }

    if b["n"] < min_samples or a["n"] < min_samples:
        base.update(
            verdict="insufficient_samples",
            delta_pct=None,
            note=(
                f"样本不足（before={b['n']} / after={a['n']}，需要各 ≥ {min_samples}）："
                "单次或两次采样无法区分真实变化与抖动——"
                "实测同一配置连跑三次可差 2.4 倍，任何方向性结论都能被单向采样"
                "'证明'出来，所以这里不下判断。"
            ),
        )
        return base

    if _overlap(b, a):
        base.update(
            verdict="indistinguishable",
            delta_pct=None,
            note=(
                f"两组区间重叠（before {b['min']:.3g}~{b['max']:.3g}，"
                f"after {a['min']:.3g}~{a['max']:.3g}）：中位数差异落在噪声范围内，"
                "不足以判方向。"
            ),
        )
        return base

    delta_pct = (a["median"] - b["median"]) / b["median"] * 100.0 if b["median"] else None
    base.update(
        verdict="slower" if a["median"] > b["median"] else "faster",
        delta_pct=delta_pct,
        note=(
            f"区间分离（before {b['median']:.3g}，after {a['median']:.3g}），"
            f"中位数变化 {delta_pct:+.1f}%"
        ),
    )
    return base
