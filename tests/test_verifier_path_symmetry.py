"""强校验（`verifier.verify`）与弱校验（`fallback.verify_text`）的口径一致性。

## 为什么需要这个测试（同类 bug 已经发生三次）

两条路径各自实现了一遍「数值/日期是否在所引证据里」的判断。于是**修一处、漏另一处**
反复发生：

1. 「候选1」事故：模型照抄字段名（含数字）被判无源 —— 强路径修了，弱路径没修
   （它当时只比对数值**标量**，连 `textual_values` 都没有）；
2. 证据字段名里的「当日」被判成日期断言 —— 强路径修了，弱路径又漏了；
3. 引用标记 `[E1]` 里的 `1` 被判成数值断言 —— 两条路径共用 `numbers.py`，一次修好。

结论：这两条路径可以各自实现**流程**（强路径要看 claim/refs/verdict，弱路径只看散文），
但**不能各自实现口径**。本文件把共有口径锁死，并显式记录有意为之的差异，
避免它们再一次悄悄分叉。
"""

from __future__ import annotations

import pytest

from tradingagents.grounding.fallback import verify_text
from tradingagents.grounding.types import Claim, EvidenceItem, EvidenceSet, StructuredAnswer
from tradingagents.grounding.verifier import verify


def _ev(*items: tuple[str, object, str | None]) -> EvidenceSet:
    return EvidenceSet(
        items=tuple(
            EvidenceItem(
                id=f"E{i}",
                field=field,
                value=value,
                unit=unit,
                source_key="stock_data",
                as_of="2026-09-22",
            )
            for i, (field, value, unit) in enumerate(items, start=1)
        )
    )


def _both(text: str, evidence: EvidenceSet, *, reference_date: str | None = None):
    """同一句话分别走强、弱两条路径。

    强路径需要 claim 与 refs；这里把**全部**证据都作为引用，好让两条路径看到的
    证据集合相同（否则比的是"引了哪些"而不是"口径是否一致"）。
    """
    answer = StructuredAnswer(
        summary="", claims=(Claim(text=text, refs=evidence.ids()),)
    )
    strong = verify(answer, evidence, reference_date=reference_date)
    weak = verify_text(text, evidence, reference_date=reference_date)
    return strong, weak


# 共有检查：数值与日期。这些用例两条路径必须给出**同一个结论**。
_AGREE_CASES = (
    # (说明, 文本, 证据)
    ("有源的数值：都通过", "量能萎缩 0.632", _ev(("量能萎缩比", 0.632, "ratio"))),
    ("无源的数值：都拒绝", "量能萎缩 0.999", _ev(("量能萎缩比", 0.632, "ratio"))),
    # 引用标记里的数字不是数据断言 —— 第三条假阳性（`[E1]` → 1）
    ("内联引用标记不算数值断言", "量能萎缩 0.632[E1]", _ev(("量能萎缩比", 0.632, "ratio"))),
    ("裸引用标记不算数值断言", "量能萎缩 0.632 E1", _ev(("量能萎缩比", 0.632, "ratio"))),
    # 编号列表序号不是数据断言 —— 第一条假阳性
    ("编号列表序号不算数值断言", "1. 量能萎缩 0.632", _ev(("量能萎缩比", 0.632, "ratio"))),
    # 字段名里的日期词是**照抄**，不是日期断言 —— 第二条假阳性
    (
        "照抄含日期词的字段名：都通过",
        "相对当日开盘涨跌幅 0.131773",
        _ev(("相对当日开盘涨跌幅", 0.131773, "pct")),
    ),
    # 证据里没有的日期词仍然是编造
    (
        "证据里没有的日期词：都拒绝",
        "截至 2026-08-01 收盘 1253.8 元",
        _ev(("收盘价", 1253.8, "元")),
    ),
)


@pytest.mark.parametrize("label,text,evidence", _AGREE_CASES)
def test_shared_checks_agree_between_paths(label, text, evidence):
    strong, weak = _both(text, evidence, reference_date="2026-09-23")
    assert strong.ok == weak.ok, (
        f"{label}：强路径 ok={strong.ok}({strong.reason}) 与弱路径 "
        f"ok={weak.ok}({weak.reason}) 结论不一致——两条路径口径又分叉了"
    )
    if not strong.ok:
        # 拒绝时要落在**同一类**原因上，否则"都拒绝"会掩盖口径差异
        assert strong.reason == weak.reason, (
            f"{label}：都拒绝了但原因不同（强 {strong.reason} / 弱 {weak.reason}）"
        )


def test_both_paths_record_a_diagnosable_detail():
    """拒绝时必须带可诊断信息——否则排查要重新写插桩脚本（见 loop.py 的同类说明）。"""
    evidence = _ev(("量能萎缩比", 0.632, "ratio"))
    strong, weak = _both("量能萎缩 0.999", evidence)
    for check in (strong, weak):
        assert check.failing_claim
        assert check.detail


# ── 有意为之的差异：显式记录，避免被当成 bug 顺手"修"掉 ──────────────────


def test_strong_path_checks_direction_but_weak_path_does_not():
    """资金流方向冲突：强路径拦，弱路径不拦。

    这是**有意**的：散文路径无法把方向断言绑定到具体引用，硬判会误伤。
    记录在此是为了让差异可见——不是待修的 bug。

    注意判定依据是**证据值的正负**（约定：净流出存为负数），不是字段名里的
    「流出/流入」字样——`flow_direction_conflict(text, value)` 只看符号。
    所以这里必须用 `-32000000.0` 才能构成冲突。
    """
    evidence = _ev(("主力净流出", -32000000.0, "元"))
    text = "主力净流入 3200万"
    strong, weak = _both(text, evidence)
    assert strong.ok is False and strong.reason == "flow_direction_conflict"
    assert weak.ok is True, "弱路径若也开始判方向，请连同本测试一起修改并说明依据"


def test_weak_path_checks_directives_but_via_shared_policy():
    """指令措辞：弱路径拒绝整条回答；强路径不查，由中间件替换。

    清单只有一份（`grounding/directives.py`），见
    `tests/test_directive_policy_single_source.py`。
    """
    evidence = _ev(("量能萎缩比", 0.632, "ratio"))
    strong, weak = _both("建议买入，量能萎缩 0.632", evidence)
    assert weak.ok is False and weak.reason == "directive_language"
    assert strong.ok is True, "强路径不管措辞（由中间件过滤），若改请同时改中间件与文档"
