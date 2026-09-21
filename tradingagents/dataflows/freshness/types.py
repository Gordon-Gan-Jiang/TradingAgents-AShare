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
