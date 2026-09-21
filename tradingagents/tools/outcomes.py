"""L1 决策工具：判定**兑现**（T+3 / T+5）。

## 与 `verdict_history_read` 的分工（为什么不是同一个工具）

`verdict_history_read` 回答「我最近的判定是什么」——主体是**判定**，兑现只是
附带的字段；本工具回答「上次那个判定兑现了吗」——主体是**兑现**，一
(判定, 持有期) 一条记录。两者的证据形状不同：前者一条证据 = 一个判定，
后者一条证据 = 一个持有期上的结果。合并成一个工具会让「T+3 命中、T+5 未命中」
这类结论在证据里表达不出来。

## 不可评估 ≠ 未命中（本工具最重要的约束）

停牌、退市、窗口不完整时 `evaluable=False`。把它读成「判定错了」会污染命中率，
进而污染权重校准——`signal_outcome_service` 的注释把这一点写得很清楚：
「取数失败计入命中率会让统计被基础设施问题污染」。因此证据里**必须**把
「不可评估」与其原因说出来，而不是只给一个 `threshold_hit=False`。

## 数据源的如实说明（避免后来者误解）

`api/services/signal_outcome_service.py` 提供的是**回填写入**入口
（`select_matured` / `evaluate_and_record` / `backfill` / `run_backfill`），
它没有「读出已落库兑现」的公开函数。兑现行的读取入口是
`signal_service.list_verdicts`（该函数 join 了 `signal_verdict_outcomes`）。
所以：端口在应用层由**读取入口**实现，而 T+3/T+5 的持有期口径取自
`signal_outcome_service.DEFAULT_HORIZONS`——两边共用一份口径，不另立第二份。
本模块只声明端口，不 import `api/`（AST 边界测试强制）。

## 弃权与降级

* 没有兑现记录 → `unavailable`，而不是空列表当成功；
* 「判定已落库但窗口未走完」与「一条判定都没有」在 `unavailable` 里用**不同
  描述**区分；
* 取数失败 → `degraded=True` + 一句可读原因，**不外传上游原文**（上游的
  `reason` 列可能带异常原文，本工具只输出归因后的中文标签）。
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence

from tradingagents.grounding.types import Coverage, EvidenceItem

from .spec import PLAN_LAYER_DECISION, RESEARCH_DISCLAIMER, ToolResult, ToolSpec

VERDICT_OUTCOME_CONTRACT = "VERDICT_OUTCOME"
_SOURCE_KEY = "verdict_outcome"
MAX_EVIDENCE = 30

# 上游 reason → 中文归因。**刻意不原样透传**：`signal_outcome_service` 在取数
# 失败时会把异常类名与消息写进 reason（`loader_failed: ...`），那是基础设施细节，
# 不属于对外结论。
_REASON_LABELS: dict[str, str] = {
    "no_new_high": "未创出新高",
    "made_new_high": "创出新高（未回落）",
    "decline_below_threshold": "回落未达阈值",
    "price_reclaimed": "收复判定日收盘",
}


class VerdictOutcomePort(Protocol):
    """读取当前用户已落库的判定兑现记录（实现由应用层提供，闭包内绑定 user_id）。

    返回行（一条 = 一个持有期上的结果）：
    `symbol / as_of / state / state_label / horizon_days / ref_date /
      ret_pct / threshold_hit / evaluable / reason`
    """

    def recent_outcomes(
        self, *, limit: int, symbol: str | None = None
    ) -> Sequence[Mapping[str, Any]]: ...


def _fmt(value: Any, *, digits: int = 2, suffix: str = "") -> str:
    if value in (None, ""):
        return "未提供"
    try:
        return f"{float(value):.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return str(value)


def _reason_label(reason: Any, *, evaluable: bool) -> str:
    """把上游 reason 归因成一句中文；未知/取数失败一律脱敏。"""
    text = str(reason or "").strip()
    if not text:
        return "不可评估" if not evaluable else ""
    if text in _REASON_LABELS:
        return _REASON_LABELS[text]
    if text.startswith("loader_failed"):
        # 上游原文里含异常消息，绝不外传
        return "取数失败（不计入命中率）"
    if text.startswith("insufficient window"):
        return "窗口不完整（停牌/退市，不计入命中率）"
    if text.startswith("malformed window"):
        return "行情结构异常（不计入命中率）"
    if text.startswith("abstention"):
        return "该判定为弃权，没有可检验的预测"
    if text.startswith("unknown state"):
        return "判定状态未归类，无法评估"
    return "不可评估"


def make_outcome_tools(outcomes: VerdictOutcomePort) -> tuple[ToolSpec, ...]:
    def verdict_outcome_lookup(
        *, limit: int = 10, symbol: str | None = None, as_of: str = ""
    ) -> ToolResult:
        # 不能写 `limit or 10`：limit=0 会被 falsy 换成 10（本仓库已多次踩坑）
        requested = 10 if limit is None else int(limit)
        capped = max(1, min(requested, MAX_EVIDENCE))
        try:
            rows = list(outcomes.recent_outcomes(limit=capped, symbol=symbol) or [])
        except Exception as exc:  # noqa: BLE001 — 取不到就如实声明，不抛给模型
            return ToolResult(
                data={"outcomes": [], "error": type(exc).__name__},
                evidence=(),
                coverage=Coverage(available=(), unavailable=(_SOURCE_KEY,)),
                degraded=True,
                degraded_reason=f"判定兑现读取失败（{type(exc).__name__}）",
                as_of=as_of,
                disclaimer=RESEARCH_DISCLAIMER,
            )

        if not rows:
            # 「一条兑现记录都没有」是事实，但它是**未覆盖**：判定可能还没有成熟
            # 到 T+3/T+5，也可能回填还没跑。不能当成"兑现没问题"。
            return ToolResult(
                data={"outcomes": [], "count": 0},
                evidence=(),
                coverage=Coverage(
                    available=(_SOURCE_KEY,),
                    unavailable=(
                        "verdict_outcome（尚无已回填的兑现记录：判定可能尚未成熟或回填未执行）",
                    ),
                ),
                as_of=as_of,
                disclaimer=RESEARCH_DISCLAIMER,
            )

        evidence: list[EvidenceItem] = []
        for index, row in enumerate(rows[:capped], start=1):
            state = str(row.get("state") or "").strip()
            state_label = str(row.get("state_label") or "").strip()
            state_text = f"{state_label}（{state}）" if state_label and state else (state_label or state or "未提供")
            horizon = row.get("horizon_days")
            parts = [
                f"{row.get('symbol') or '未提供'}（{row.get('as_of') or '未提供'}）",
                f"判定 {state_text}",
                f"T+{horizon}" if horizon is not None else "持有期 未提供",
                f"参考日 {row.get('ref_date') or '未提供'}",
            ]
            evaluable = bool(row.get("evaluable"))
            if evaluable:
                parts.append(f"收益 {_fmt(row.get('ret_pct'), suffix='%')}")
                parts.append(f"命中 {'是' if row.get('threshold_hit') else '否'}")
                label = _reason_label(row.get("reason"), evaluable=True)
                if label:
                    parts.append(f"说明 {label}")
            else:
                # 不可评估必须显式，且不得与"未命中"混为一谈
                parts.append("评估 不可评估")
                parts.append(_reason_label(row.get("reason"), evaluable=False))
            evidence.append(
                EvidenceItem(
                    id=f"E{index}",
                    field=f"判定兑现{index}",
                    value="｜".join(parts),
                    unit="",
                    source_key=_SOURCE_KEY,
                    as_of=str(row.get("as_of") or as_of),
                )
            )

        return ToolResult(
            data={"outcomes": [dict(row) for row in rows[:capped]], "count": len(rows[:capped])},
            evidence=tuple(evidence),
            coverage=Coverage(available=(_SOURCE_KEY,), unavailable=()),
            as_of=as_of,
            disclaimer=RESEARCH_DISCLAIMER,
        )

    spec = ToolSpec(
        name="verdict_outcome_lookup",
        title="判定兑现（T+3/T+5）",
        description=(
            "读取当前用户**已落库的判定兑现结果**：每条判定在其后 T+3/T+5 个交易日"
            "上的收益、是否命中（洗盘看是否创新高、派发看是否未创新高且回落过阈值）、"
            "以及是否可评估（停牌/退市/窗口不完整时为不可评估，**不计入命中率**）。"
            "用于回答「上次那个判定兑现了吗」「我的判定准不准」。"
            "兑现是已落库事实，不接受模型改写；不可评估必须说成不可评估，不得读成未命中。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": f"返回条数，1-{MAX_EVIDENCE}，默认 10"},
                "symbol": {"type": "string", "description": "只看某只标的（可选）"},
                "as_of": {"type": "string", "description": "基准日 YYYY-MM-DD"},
            },
            "required": [],
        },
        output_contract=VERDICT_OUTCOME_CONTRACT,
        handler=verdict_outcome_lookup,
        cost="cheap",
        plan_layer=PLAN_LAYER_DECISION,
        latency_budget_ms=8000,
        freshness_sources=(_SOURCE_KEY,),
    )
    return (spec,)
