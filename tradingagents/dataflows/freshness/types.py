from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

FreshnessStatus = Literal[
    "fresh", "warning", "stale", "error", "empty_ok", "not_applicable", "unknown"
]
OverallStatus = Literal["fresh", "warning", "stale", "error"]
IssueType = Literal["lag", "fetch_error"]
Category = Literal[
    "event_driven", "calendar_anchored", "disclosure_period", "conditional_daily"
]
Criticality = Literal["critical", "important", "informational"]

OVERALL_LABELS = {
    "fresh": "数据最新",
    "warning": "部分滞后",
    "stale": "数据过时",
    "error": "数据异常",
}

CONFIDENCE_CAPS = {
    "fresh": 100,
    "warning": 70,
    # `unknown` = **没有验证过**（时间戳解析不出、category 不认识、状态缺失）。
    #
    # 这个键曾经不存在，于是所有 `CONFIDENCE_CAPS.get(overall, 100)` 的默认值
    # 把"不知道"放行成"完全可信"：未验证的新鲜度拿到上限 100（审计 §4.16）。
    # 60 的取值理由：它必须**低于** `warning`(70)——warning 至少是"知道有问题、
    # 且问题有限"，而 unknown 是"连有没有问题都不知道"，证据更弱；
    # 又必须**高于** `error`(40)——error 是已确证的取数失败，比无知更严重。
    "unknown": 60,
    "stale": 50,
    "error": 40,
}


@dataclass
class FreshnessMeta:
    source_key: str
    category: Category | str
    criticality: Criticality | str
    status: FreshnessStatus
    anchor_expected: str | None = None
    anchor_actual: str | None = None
    evaluated_at: str | None = None
    issue_type: IssueType | None = None
    error_code: str | None = None
    error_message: str | None = None
    lag_note: str | None = None
    lag_trading_days: int | None = None
    empty_ok: bool = False
    # 通用说明：给「为什么是这个 status」一个落点。`unknown` 尤其需要它——
    # 否则下游只看到「未知」，无从判断是没验证、还是契约里没这个源。
    # 默认 None，`to_dict()` 会丢弃 None，因此对既有 freshness 路径零影响。
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in asdict(self).items():
            if v is None:
                continue
            if k == "empty_ok" and v is False:
                continue
            out[k] = v
        return out


@dataclass
class EvalContext:
    trade_date: str
    analysis_mode: str
    expected_anchor: str
    now_iso: str
    in_grace: bool = False
