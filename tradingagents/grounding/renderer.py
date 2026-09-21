"""渲染器：由结构化 claims 生成正文。

## 为什么正文不交给模型自由书写

自由文本的校验只能靠正则从散文里抽数字猜语境，脆弱且易漏。改为「模型输出
结构化 claims → 渲染器拼正文」之后，校验发生在结构化数据上（100% 可靠），
正文只是一次确定的格式转换。

## 两条渲染纪律

1. **不改数字**：渲染器对 claim 文本做原样输出，只追加引用标记。任何「美化」
   （四舍五入、换单位）都会让正文与证据不再一致，使前一步的校验白做。
2. **保留引用标记**：`[E1]` 之类的标记让「这句话的依据」在界面上可追溯，
   也是回答层与判定卡片联动的锚点。
"""

from __future__ import annotations

from .types import EvidenceSet, StructuredAnswer

UNAVAILABLE_HEADING = "数据未覆盖"
CITATION_OPEN = "["
CITATION_CLOSE = "]"


def claim_line(text: str, refs: tuple[str, ...]) -> str:
    """单条断言的渲染：原样文本 + 引用标记。"""
    if not refs:
        return text
    marks = "".join(f"{CITATION_OPEN}{ref}{CITATION_CLOSE}" for ref in refs)
    return f"{text}{marks}"


def render(answer: StructuredAnswer, evidence: EvidenceSet | None = None) -> str:
    """把结构化回答渲染成 markdown 正文。"""
    parts: list[str] = []

    if answer.summary:
        parts.append(answer.summary.strip())

    if answer.claims:
        lines = [f"- {claim_line(c.text, c.refs)}" for c in answer.claims]
        parts.append("\n".join(lines))

    if answer.declared_unavailable:
        items = "、".join(answer.declared_unavailable)
        parts.append(f"**{UNAVAILABLE_HEADING}**：{items}（相关判断已相应降低置信度）")

    if answer.falsifier_observed:
        parts.append(f"**已触发的失效条件**：{answer.falsifier_observed}")

    if answer.rendered_note:
        parts.append(answer.rendered_note)

    return "\n\n".join(part for part in parts if part)


def render_insufficient(available_evidence: EvidenceSet, reason: str) -> str:
    """无法给出有依据回答时的**诚实降级**文本。

    绝不输出未校验内容，而是说明「缺什么」以及「已拿到什么」——让用户能判断
    是数据问题还是问题本身无法回答。
    """
    fields = "、".join(item.field for item in available_evidence.items[:12]) or "（无）"
    return (
        f"{reason}\n\n"
        f"已获取的证据字段：{fields}\n\n"
        "为避免给出无依据的结论，本次不作方向性判断。"
    )
