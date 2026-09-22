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


# 降级文本里最多列几条证据。列满一屏没有意义，但**截断必须说出来**。
_INSUFFICIENT_EVIDENCE_LIMIT = 12


def render_insufficient(available_evidence: EvidenceSet, reason: str) -> str:
    """无法给出有依据回答时的**诚实降级**文本。

    绝不输出未校验内容，而是说明「缺什么」以及「已拿到什么」——让用户能判断
    是数据问题还是问题本身无法回答。

    ## 为什么列**值**而不是只列字段名（实测）

    轻档问句「600519 今天主力净流入多少」取到了 4 条证据，但首答未过校验、轻档
    只允许 1 次模型调用，于是走到这里。旧文本只给了一串字段名：

        已获取的证据字段：主力净流入、超大单净流入、大单净流入、数据日期与单位

    用户问"净流入多少"，回答里**没有那个数**——而平台明明有它，还带引用编号。
    降级的意义是"不编造"，不是"把取到的东西也藏起来"。

    渲染证据值不违反"未通过校验的文本绝不出门"：这段文本不是模型写的，内容就是
    工具产出的证据本身（早已是引用校验的依据），并带上 `[E1]` 让用户可追溯。
    它也**不做任何加工**：原值、原单位，不四舍五入、不换单位、不加方向性措辞。
    """
    items = list(available_evidence.items)
    if not items:
        return (
            f"{reason}\n\n"
            "已获取的证据字段：（无）\n\n"
            "为避免给出无依据的结论，本次不作方向性判断。"
        )

    shown = items[:_INSUFFICIENT_EVIDENCE_LIMIT]
    lines = []
    for item in shown:
        unit = f" {item.unit}" if item.unit else ""
        lines.append(f"- {item.field}：{item.value}{unit}[{item.id}]")
    body = "\n".join(lines)
    overflow = ""
    if len(items) > len(shown):
        overflow = f"\n（以上为前 {len(shown)} 条，本轮证据共 {len(items)} 条）"
    return (
        f"{reason}\n\n"
        f"已获取的证据（未生成结论，原始值如下）：\n{body}{overflow}\n\n"
        "为避免给出无依据的结论，本次不作方向性判断。"
    )
