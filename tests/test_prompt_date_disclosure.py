"""提示词必须披露证据的 `as_of`——否则日期断言只能靠猜。

## 这条不变量是怎么发现的

生产库 `chat_messages` 里 54 条降级回答的拒收原因分布：

| 底层原因 | 轻档（无修复预算） | 中档（修复后仍失败） |
|---|---|---|
| `unsourced_date` | **23** | **0** |
| `unsourced_number` | 13 | 6 |
| `flow_direction_conflict` | 3 | 3 |

`unsourced_date` 占轻档降级的 57.5%，却在**修复后一次都不出现**——说明它不是
"模型做不到"，而是"没告诉它"：`_build_prompt` 只给证据编号与字段名，从未给出
任何 `as_of`，而校验器（`date_assertion_ok`）要求日期断言落在**所引证据**的
`as_of` 集合内。模型看不见可引用的日期，只能凭空写。

## 为什么给出 `as_of` 不会引入数值风险

`extract_numbers` 的 docstring 写明"已排除日期、代码与窗口参数"，日期只走
`extract_dates`。因此披露日期是元数据，不会变成 `unsourced_number`。
`test_disclosed_as_of_does_not_become_an_unsourced_number` 把这一点也钉住。
"""

from __future__ import annotations

from tradingagents.agent.loop import TurnRequest, _build_prompt
from tradingagents.agent.middleware import TurnContext
from tradingagents.grounding.types import Claim, EvidenceItem, EvidenceSet, StructuredAnswer
from tradingagents.grounding.verifier import REASON_UNSOURCED_DATE, verify


def _evidence() -> EvidenceSet:
    """两条 as_of **不同**的证据——这正是模型最容易写错日期的场景。"""
    return EvidenceSet(
        items=(
            EvidenceItem(
                id="E1", field="close", value=1500.0, unit="CNY",
                source_key="daily_bars", as_of="2026-09-22",
            ),
            EvidenceItem(
                id="E2", field="chip_concentration", value=0.12, unit="ratio",
                source_key="chip_distribution", as_of="2026-09-19",
            ),
        )
    )


def _prompt(evidence: EvidenceSet) -> str:
    return _build_prompt(
        TurnRequest(text="600519 是不是在洗盘", user_id="u1", symbol="600519.SH"),
        evidence,
        TurnContext(user_id="u1", tier="medium", text="600519 是不是在洗盘"),
    )


# ══════════════════════════════════════════════════════════════════════════
# 提示词披露
# ══════════════════════════════════════════════════════════════════════════


def test_prompt_discloses_every_evidence_as_of():
    """每条证据的 as_of 都必须出现在提示词里。"""
    evidence = _evidence()
    prompt = _prompt(evidence)

    for item in evidence.items:
        assert item.as_of in prompt, f"证据 {item.id} 的 as_of 未披露，模型只能猜日期"
        assert item.id in prompt, f"证据 {item.id} 的编号未披露"


def test_prompt_states_the_date_rule():
    """必须明说日期只能取所引证据的 as_of——软约束对机械校验器无效。"""
    prompt = _prompt(_evidence())

    assert "as_of" in prompt
    assert "日期断言" in prompt, "提示词必须显式给出日期规则"
    assert "所引证据" in prompt or "引用" in prompt


# ══════════════════════════════════════════════════════════════════════════
# 规则是承重的：披露的日期通过，未披露的日期被拒
# ══════════════════════════════════════════════════════════════════════════


def test_claim_using_the_disclosed_as_of_passes_verification():
    """模型按提示词披露的 as_of 写日期 → 校验通过。"""
    evidence = _evidence()
    answer = StructuredAnswer(
        summary="判定为洗盘。",
        claims=(Claim(text="截至 2026-09-22 收盘价为 1500.0 元。", refs=("E1",)),),
    )

    result = verify(answer, evidence, allowed_symbols=("600519.SH",))

    assert result.ok, f"披露过的日期不该校验失败：{result.reason} / {result.detail}"


def test_claim_using_an_undisclosed_date_is_rejected():
    """对照：写一个**不是**所引证据 as_of 的日期 → 必须被拒。

    这条证明日期规则真实生效——否则上面那条通过只是校验器没在工作。
    """
    evidence = _evidence()
    answer = StructuredAnswer(
        summary="判定为洗盘。",
        claims=(Claim(text="截至 2026-09-21 收盘价为 1500.0 元。", refs=("E1",)),),
    )

    result = verify(answer, evidence, allowed_symbols=("600519.SH",))

    assert not result.ok
    assert result.reason == REASON_UNSOURCED_DATE, result.reason


def test_disclosed_as_of_does_not_become_an_unsourced_number():
    """披露 as_of 不得把日期变成"凭空数值"——否则这个修复会制造新错误。"""
    evidence = _evidence()
    answer = StructuredAnswer(
        summary="判定为洗盘。",
        claims=(Claim(text="截至 2026-09-22 收盘价为 1500.0 元。", refs=("E1",)),),
    )

    result = verify(answer, evidence, allowed_symbols=("600519.SH",))

    assert result.reason != "unsourced_number", "日期被当成数值断言抽取了"
