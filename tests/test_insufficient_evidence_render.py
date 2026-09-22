"""诚实降级的文本必须**让人读到事实**，而不是只读到字段名。

## 实测（真实对话）

轻档问句「600519 今天主力净流入多少」实际取到了 4 条证据，但首个回答没通过
校验、且轻档只允许 1 次模型调用，于是走到降级路径。用户看到的是：

    模型调用预算已用尽（1/1），未能修复未通过校验的回答（flow_direction_conflict）。
    已获取的证据字段：主力净流入、超大单净流入、大单净流入、数据日期与单位
    为避免给出无依据的结论，本次不作方向性判断。

**字段名不是事实**：用户问"净流入多少"，得到的回答里没有那个数——而平台明明
有它，还带引用编号。降级的意义是"不编造"，不是"把取到的东西也藏起来"。

## 为什么这比放宽预算更该做

放宽模型调用预算会改变"轻档 = 1 次归纳"这条已声明的成本约束（有测试锁着）。
而把证据值渲染出来是**纯代码**行为：零额外成本、零模型参与、不引入任何未经校验
的断言——文本内容就是工具产出的证据本身，早已是引用校验的依据。
"""

from __future__ import annotations


def _evidence():
    from tradingagents.grounding.types import EvidenceItem, EvidenceSet

    return EvidenceSet(
        items=(
            EvidenceItem(id="E1", field="主力净流入", value=-32275440.0, unit="元",
                         source_key="individual_fund_flow", as_of="2026-09-22"),
            EvidenceItem(id="E2", field="数据日期", value="2026-09-22", unit="",
                         source_key="individual_fund_flow", as_of="2026-09-22"),
        )
    )


def test_degraded_text_shows_evidence_values_with_citations():
    from tradingagents.grounding.renderer import render_insufficient

    text = render_insufficient(_evidence(), "未能生成可溯源的回答（flow_direction_conflict）。")

    assert "-32275440" in text, "证据的**值**必须出现——用户问的就是这个数"
    assert "主力净流入" in text, "字段名也要在，否则数字没有语境"
    assert "[E1]" in text, "必须带引用编号，用户才能追溯"
    assert "未能生成可溯源的回答" in text, "降级原因不能丢"
    assert "不作方向性判断" in text, "免责声明不能丢"


def test_degraded_text_without_evidence_stays_honest():
    from tradingagents.grounding.renderer import render_insufficient
    from tradingagents.grounding.types import EvidenceSet

    text = render_insufficient(EvidenceSet(items=()), "本轮无可用证据。")
    assert "（无）" in text, "没有证据时要说「没有」，不能留空"


def test_long_evidence_lists_are_capped_but_say_so():
    """证据很多时要截断，但**必须说出截断了**——静默截断会让人以为这就是全部。"""
    from tradingagents.grounding.renderer import render_insufficient
    from tradingagents.grounding.types import EvidenceItem, EvidenceSet

    items = tuple(
        EvidenceItem(id=f"E{i}", field=f"字段{i}", value=i, unit="", source_key="s", as_of="")
        for i in range(1, 21)
    )
    text = render_insufficient(EvidenceSet(items=items), "原因。")

    assert "[E12]" in text, "前 12 条应在"
    assert "[E13]" not in text, "超出上限的不列"
    assert "共 20 条" in text, "必须说明实际条数，否则像是只有 12 条"
