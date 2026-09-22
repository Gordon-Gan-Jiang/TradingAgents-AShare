"""LLM 回答模型适配器：把模型输出转成结构化回答。

## 模型层刻意不做校验

如果这里顺手把「没有引用的 claim」丢掉、或替模型补一个引用，机械校验器就
**永远看不到真实的失败率**——护栏会变成装饰。因此本模块只负责：

* 把证据编号、判定约束、未覆盖维度写进提示
* 解析模型返回的 JSON（容忍代码围栏与前后散文）
* 解析失败时如实降级为文本模式并**计数**

判断「有没有依据」是 `grounding/verifier.py` 的职责，两者不重叠。

## 为什么 completer 是普通可调用对象

不依赖 langchain / openai 的任何类型，只要求 `(prompt) -> str`。这使模型层
可以在没有网络、没有 SDK 的条件下被完整测试；应用层传入
`lambda p: llm.invoke(p).content` 即可。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
import re
from typing import Any, Callable, Mapping

from tradingagents.grounding.types import (
    CheckResult,
    EvidenceSet,
    StructuredAnswer,
    TurnVerdict,
)
from tradingagents.grounding.types import Claim

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Completion:
    """一次补全的文本与用量。

    `reported=False` 表示取数器未提供 usage —— 此时 `total_tokens` 为 0。
    **不得估算**：记账的价值在于可归因，估算出来的数字会把成本分析带偏。
    """

    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    reported: bool = False

    @property
    def total_tokens(self) -> int:
        return int(self.input_tokens) + int(self.output_tokens)

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)

# 静态系统规则。注意 JSON 模板里 `verdict_ref` 是 **null**，不是某个示例 id。
#
# 它原本写成 `"verdict_ref": "V1"`——一个**看起来像真实取值**的占位符，同时又有一条
# 动态指令说"必须填 <真实 id>"。于是同一条提示词里出现两个互相冲突的值，而模板还被
# "输出必须是严格的 JSON，不要输出其它内容"这句推着照抄。校验器只接受本轮的真实判定
# id（`grounding/verifier.py` 的 `verdict_ref not in verdict_ids`），照抄模板就是
# `verdict_ref_not_from_turn`——这正是修复后残余降级里反复出现的那一项。
#
# 为什么选 null 而不是"请填真实 id"之类的说明文字：null 在**两种情况下都安全**
# （有判定时由下方动态行补上真实 id；无判定时校验器直接跳过这条检查），
# 而任何"像取值的占位符"在两种情况下都可能被照抄。占位符的默认值应当是
# **安全的那一侧**，不是看起来最像样例的那一侧。
_SYSTEM_RULES = """你是一名 A 股投研助手。你必须遵守以下规则，违反会导致回答被系统拒绝：

1. 每条结论都必须携带证据编号引用（形如 [E1]），不得引用不存在的编号。
   **例外**：若本轮没有给出任何 E 编号（无可用证据），则不得编造引用——
   此时 `claims` 必须为空数组 `[]`，把**定性**结论写进 `summary`，
   且 `summary` 里不得出现任何具体数值或日期（写数字会被判无出处）。
   这是唯一允许的无引用形态，它只适用于"没有结构化证据、但有工具正文"的回合。
2. 不得编造任何数值或日期：只能用「可用证据」里出现过的数值。
3. 已由探针给出的判定（状态与置信度）**不得改写**，你只能解释它。
4. 数据未覆盖的维度必须列入 unavailable，不得描述为「无影响」。
5. 不得给出祈使式买卖指令（如「立即清仓」），只能给条件化观察与失效条件。

输出必须是严格的 JSON，不要输出其它内容：
{
  "summary": "一句话结论",
  "claims": [{"text": "带数值的断言", "refs": ["E1"]}],
  "verdict_ref": null,
  "unavailable": ["维度名"],
  "falsifier_observed": null
}"""


def _extract_json(text: str) -> Mapping[str, Any] | None:
    """从模型输出里抽出 JSON 对象，容忍代码围栏与前后散文。

    这种格式噪声是无害的；若因此判为解析失败，`fallback_rate` 会被推高到
    失去意义——指标一旦被噪声主导，就没人会再看它。
    """
    if not text or not text.strip():
        return None
    candidates: list[str] = []
    candidates.extend(match.group(1).strip() for match in _FENCE_RE.finditer(text))
    match = _JSON_OBJECT_RE.search(text)
    if match:
        candidates.append(match.group(0))

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            try:  # 常见：尾随逗号、单引号
                import json_repair

                parsed = json_repair.loads(candidate)
            except Exception:  # noqa: BLE001
                continue
        if isinstance(parsed, Mapping):
            return parsed
    return None


def _coerce_claims(raw: Any) -> tuple[Claim, ...]:
    """解析 claims。**不修补**缺失引用——那是校验器要看到的事实。"""
    if not isinstance(raw, list):
        return ()
    claims: list[Claim] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        refs_raw = item.get("refs") or item.get("evidence") or []
        if isinstance(refs_raw, str):
            refs_raw = [refs_raw]
        refs = tuple(str(r).strip() for r in refs_raw if str(r).strip())
        claims.append(Claim(text=text, refs=refs))
    return tuple(claims)


class LlmAnswerModel:
    """基于文本补全的回答模型。"""

    def __init__(self, completer: Callable[[str], Any], *, max_retries: int = 0) -> None:
        # 兼容两种取数器：返回 `str`（旧）或 `Completion`（带 usage）。
        # 前者 usage 记为 0 并标 reported=False，不估算。
        self._complete = completer
        self._max_retries = max_retries
        self.last_usage: Completion | None = None
        self.total_tokens = 0
        # 结构化解析失败次数：对外暴露，用于 fallback_rate 指标。
        # 它是「需要调整 schema 或更换归纳模型」的信号，不是可接受的稳态。
        self.fallback_used = 0
        self.calls = 0

    # ── 提示构造 ──────────────────────────────────────────────────────────

    def _build_prompt(
        self,
        prompt: str,
        evidence: EvidenceSet,
        verdict: TurnVerdict | None,
        *,
        has_tool_text: bool = False,
    ) -> str:
        lines = [
            _SYSTEM_RULES,
            "",
            "## 本轮任务",
            prompt,
            "",
            "## 可用证据（只能引用这里的编号与数值）",
        ]
        if evidence.items:
            for item in evidence.items:
                lines.append(f"[{item.id}] {item.field} = {item.value} ({item.unit}, 截至 {item.as_of})")
        elif has_tool_text:
            # Phase 6.1：有工具正文可用时**不能**再命令弃权。
            #
            # 这里原本只有 `else: 你必须说明无法给出有依据的判断`，而那会把工具正文
            # 通道整个抵消掉——实测就是如此：正文明明进了提示词，模型仍然回答
            # 「本轮无可用证据」，因为它在这条**硬性指令**下照办。
            #
            # 无结构化证据 ≠ 无信息，但**形态受限**：系统规则 1 要求每条结论带引用，
            # 而 `_check_claim` 会拒掉无引用的断言（`REASON_NO_CITATION`），
            # 所以定性内容只能放 `summary`、`claims` 留空（空 claims + 非空 summary
            # 走 `_is_text_mode` 的弱校验路径）。
            # 弱校验照旧核对数值与日期，而本轮证据为空——所以**一个数字都不能写**。
            # 安全边界不变，只是把"能说的话"从"什么都不能说"放宽到"可以定性描述"。
            lines.append(
                "（本轮没有可引用的**结构化**证据，但「工具正文」段里有同源的原始文本。"
                "此时 `claims` 必须为空数组，把**定性**结论写进 `summary`："
                "讲了什么事、涉及哪些主题，都可以说；"
                "但 `summary` 里**不得出现任何数值或日期**（本轮证据为空，写了就是无出处）。"
                "若正文与问题无关，就按无证据处理并在 unavailable 里声明未覆盖。）"
            )
        else:
            lines.append("（本轮无可用证据——你必须说明无法给出有依据的判断，不得编造）")

        if verdict is not None:
            lines += [
                "",
                "## 探针判定（不得改写）",
                f"- 标的：{verdict.symbol}    基准日：{verdict.as_of}",
                f"- 状态：{verdict.state}    置信度：{verdict.confidence}",
                f"- 失效条件：{'；'.join(verdict.falsifiers) or '（无）'}",
                f"- 数据未覆盖：{'、'.join(verdict.unavailable) or '（无）'}",
                f"你必须在 verdict_ref 中填 \"{verdict.id}\"。",
            ]
        else:
            # 没判定时必须**明说**该字段留空。上面那段静态模板里
            # 只有 `"verdict_ref": null`，而模型很容易顺手填上一个不存在的引用。
            # 校验器只在 `verdict_ref is not None` 时才核对（见 grounding/verifier.py），
            # 所以"留空"永远安全，"编一个"永远失败——这层不对称要直接告诉模型。
            lines += [
                "",
                "## 本轮无探针判定",
                "verdict_ref 必须为 null，不得编造判定引用。",
            ]
        return "\n".join(lines)

    # ── 解析 ──────────────────────────────────────────────────────────────

    def _parse(self, text: str) -> StructuredAnswer:
        parsed = _extract_json(text)
        if parsed is None:
            self.fallback_used += 1
            logger.info("answer model: structured parse failed, falling back to text mode")
            return StructuredAnswer(
                summary=(text or "").strip(),
                claims=(),
                rendered_note="（本轮未能生成结构化回答，已降级为文本模式，仅做数值存在性校验）",
            )

        unavailable_raw = parsed.get("unavailable") or []
        if isinstance(unavailable_raw, str):
            unavailable_raw = [unavailable_raw]
        verdict_ref = parsed.get("verdict_ref")
        return StructuredAnswer(
            summary=str(parsed.get("summary") or "").strip(),
            claims=_coerce_claims(parsed.get("claims")),
            verdict_ref=str(verdict_ref) if verdict_ref else None,
            declared_unavailable=tuple(str(u) for u in unavailable_raw),
            falsifier_observed=(
                str(parsed["falsifier_observed"]) if parsed.get("falsifier_observed") else None
            ),
        )

    # ── AnswerModel 协议 ─────────────────────────────────────────────────

    def _run(self, prompt: str) -> str:
        """执行一次补全并记录用量。"""
        result = self._complete(prompt)
        if isinstance(result, Completion):
            usage = result
            raw = result.text
        else:
            usage = Completion(text=str(result), reported=False)
            raw = str(result)
        self.last_usage = usage
        self.total_tokens += usage.total_tokens
        return raw

    def propose(
        self,
        *,
        prompt: str,
        evidence: EvidenceSet,
        verdict: TurnVerdict | None,
        has_tool_text: bool = False,
    ) -> StructuredAnswer:
        self.calls += 1
        raw = self._run(
            self._build_prompt(prompt, evidence, verdict, has_tool_text=has_tool_text)
        )
        return self._parse(raw)

    def repair(
        self,
        *,
        previous: StructuredAnswer,
        failure: CheckResult,
        evidence: EvidenceSet,
        verdict: TurnVerdict | None,
    ) -> StructuredAnswer:
        self.calls += 1
        repair_prompt = "\n".join(
            [
                self._build_prompt("", evidence, verdict),
                "",
                "## 你上一次的回答未通过校验，必须修正",
                f"- 失败原因：{failure.reason}",
                f"- 问题断言：{failure.failing_claim}",
                f"- 细节：{failure.detail}",
                "",
                "请只使用上面列出的证据编号与数值重新作答。",
            ]
        )
        raw = self._run(repair_prompt)
        return self._parse(raw)
