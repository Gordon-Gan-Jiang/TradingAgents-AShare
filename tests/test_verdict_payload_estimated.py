"""判定载荷必须带出"哪些维度是模型估计"。

## 为什么

实测 600519：加上**自算筹码**（模型估计）后，margin 由 0.400 掉到 0.114，
判定从 `distribution / 置信 80` 变成 `insufficient_evidence / 0`（低于结构弃权阈值）。

也就是说：**一个模型估计有能力和真实数据一样改变判定结论**。那么判定载荷
就必须说明"这个结论受估计影响"，否则用户看到的是一个由估计驱动的结论、
却以为它由数据驱动——这与"把估计说成事实"是同一类失败，只是发生在判定层。

`ProbeResult.estimated_dimensions` 早已存在（agent 路径在用），但
`verdict_payload` 没带出来 → 判定卡片与 `/v1/signals/washout` 的调用方都看不到。
"""

from __future__ import annotations


def _result(*, estimated: tuple[str, ...]):
    from tradingagents.signals.types import ProbeResult

    return ProbeResult(
        probe="signal_washout", symbol="600519.SH", as_of="2026-09-21",
        state="insufficient_evidence", confidence=0, margin=0.1142,
        score_washout=0.32, score_distribution=0.28,
        estimated_dimensions=estimated,
    )


def test_estimated_dimensions_reach_the_payload():
    from api.services.signal_service import verdict_payload

    payload = verdict_payload(_result(estimated=("chip_distribution",)))
    assert payload.get("estimated_dimensions") == ["chip_distribution"], (
        "估计维度必须随判定出去，否则用户会把估计驱动的结论当成数据驱动的"
    )


def test_payload_without_estimates_stays_empty():
    from api.services.signal_service import verdict_payload

    payload = verdict_payload(_result(estimated=()))
    assert payload.get("estimated_dimensions") == [], "没有估计维度时必须是空列表，不能凭空加"
