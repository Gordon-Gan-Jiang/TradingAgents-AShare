"""回答证据锚定层：让「不能乱说」成为构造保证而不是提示词愿望。

本包的定位与 `tradingagents/signals/` 平行——同样是纯函数、零 LLM、
零网络、零环境时钟（由 `tests/test_architecture_boundaries.py` 强制）。

工作方式：
    模型输出结构化 claims[] + refs[]  →  渲染器拼正文
                                      →  机械校验器查引用与数值
                                      →  未通过则重写一次，再失败则诚实降级

它不依赖 LLM 做任何判断：所有校验都是可单测的确定性代码。
"""

from __future__ import annotations

from .numbers import (
    DateAssertion,
    NumberToken,
    extract_dates,
    extract_numbers,
    matches_any,
    scalar_values,
)
from .renderer import claim_line, render, render_insufficient
from .types import (
    CheckResult,
    Claim,
    Coverage,
    EvidenceItem,
    EvidenceSet,
    StructuredAnswer,
    TurnVerdict,
)
from .verifier import coverage_unavailable_of, verify

__all__ = [
    # types
    "Coverage",
    "EvidenceItem",
    "EvidenceSet",
    "Claim",
    "StructuredAnswer",
    "TurnVerdict",
    "CheckResult",
    # numbers
    "NumberToken",
    "DateAssertion",
    "extract_numbers",
    "extract_dates",
    "matches_any",
    "scalar_values",
    # verifier / renderer
    "verify",
    "coverage_unavailable_of",
    "render",
    "render_insufficient",
    "claim_line",
]
