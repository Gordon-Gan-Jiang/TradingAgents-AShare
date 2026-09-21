"""Prompt template management and rendering for analysis requests."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional
from uuid import uuid4

from sqlalchemy.orm import Session

from api.database import AnalysisPromptTemplateDB

SCOPE_DEEP_ANALYSIS = "deep_analysis"
DEFAULT_MANUAL_TEMPLATE_ID = "builtin-trade-decision"
DEFAULT_SCHEDULED_TEMPLATE_ID = "builtin-scheduled-review"


_BUILTIN_TEMPLATES: tuple[dict[str, Any], ...] = (
    {
        "id": "builtin-trade-decision",
        "scope": SCOPE_DEEP_ANALYSIS,
        "name": "交易决策模板",
        "description": "聚焦交易方向、价位、仓位和风险触发条件。",
        "template_text": (
            "请对{name}（{symbol}）做{horizon_label}深度分析。"
            "输出：交易方向、关键依据、目标价、止损价、仓位建议、触发条件。"
        ),
        "intent_json": {
            "focus_areas": ["方向判断", "风险收益比", "执行条件"],
            "specific_questions": ["今天是否适合执行交易？如果执行，如何分批和止损？"],
        },
    },
    {
        "id": "builtin-market-action",
        "scope": SCOPE_DEEP_ANALYSIS,
        "name": "市场动作模板",
        "description": "强调盘面节奏、催化与短期交易动作。",
        "template_text": (
            "请结合盘面和催化因素，分析{name}（{symbol}）在{trade_date}附近的{horizon_label}机会。"
            "重点给出可执行动作和风险对冲方案。"
        ),
        "intent_json": {
            "focus_areas": ["市场节奏", "催化事件", "交易动作"],
            "specific_questions": ["当前应该买入/持有/减仓吗？触发反转时怎么处理？"],
        },
    },
    {
        "id": "builtin-scheduled-review",
        "scope": SCOPE_DEEP_ANALYSIS,
        "name": "定时复盘模板",
        "description": "用于定时任务，强调复盘与持仓风控。",
        "template_text": (
            "请按定时复盘方式分析{name}（{symbol}），周期：{horizon_label}。"
            "若已有持仓，请优先评估成本位、风险敞口和调仓建议。"
        ),
        "intent_json": {
            "focus_areas": ["持仓复盘", "风控校验", "调仓建议"],
            "specific_questions": ["当前持仓是否需要调整？关键风险位在哪里？"],
        },
    },
)


def _safe_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    return {}


class _TemplateVars(dict):
    def __missing__(self, key: str) -> str:
        return ""


def _horizon_label(horizon: str) -> str:
    if str(horizon).lower() == "medium":
        return "中线"
    return "短线"


def ensure_builtin_templates(db: Session) -> None:
    existing = {
        row.id: row
        for row in db.query(AnalysisPromptTemplateDB)
        .filter(AnalysisPromptTemplateDB.is_builtin.is_(True))
        .all()
    }
    changed = False
    now = datetime.now(timezone.utc)
    for item in _BUILTIN_TEMPLATES:
        row = existing.get(item["id"])
        if row is None:
            db.add(
                AnalysisPromptTemplateDB(
                    id=item["id"],
                    user_id=None,
                    scope=item["scope"],
                    name=item["name"],
                    description=item["description"],
                    template_text=item["template_text"],
                    intent_json=deepcopy(item["intent_json"]),
                    is_builtin=True,
                    is_active=True,
                    created_at=now,
                    updated_at=now,
                )
            )
            changed = True
            continue
        # Keep builtins in sync with current code defaults.
        if (
            row.scope != item["scope"]
            or row.name != item["name"]
            or row.description != item["description"]
            or row.template_text != item["template_text"]
            or _safe_dict(row.intent_json) != _safe_dict(item["intent_json"])
            or not row.is_active
        ):
            row.scope = item["scope"]
            row.name = item["name"]
            row.description = item["description"]
            row.template_text = item["template_text"]
            row.intent_json = deepcopy(item["intent_json"])
            row.is_active = True
            changed = True
    if changed:
        db.commit()


def _to_dict(row: AnalysisPromptTemplateDB) -> Dict[str, Any]:
    return {
        "id": row.id,
        "user_id": row.user_id,
        "scope": row.scope,
        "name": row.name,
        "description": row.description,
        "template_text": row.template_text,
        "intent_json": _safe_dict(row.intent_json),
        "is_builtin": bool(row.is_builtin),
        "is_active": bool(row.is_active),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def list_templates(db: Session, user_id: str, *, scope: Optional[str] = None) -> list[Dict[str, Any]]:
    ensure_builtin_templates(db)
    query = db.query(AnalysisPromptTemplateDB).filter(
        (AnalysisPromptTemplateDB.user_id == user_id) | (AnalysisPromptTemplateDB.is_builtin.is_(True))
    )
    if scope:
        query = query.filter(AnalysisPromptTemplateDB.scope == scope)
    rows = query.order_by(
        AnalysisPromptTemplateDB.is_builtin.desc(),
        AnalysisPromptTemplateDB.created_at.asc(),
    ).all()
    return [_to_dict(row) for row in rows]


def create_custom_template(
    db: Session,
    user_id: str,
    *,
    scope: str,
    name: str,
    description: Optional[str],
    template_text: str,
    intent_json: Optional[Dict[str, Any]] = None,
    is_active: bool = True,
) -> Dict[str, Any]:
    scope_text = (scope or "").strip() or SCOPE_DEEP_ANALYSIS
    name_text = (name or "").strip()
    prompt_text = (template_text or "").strip()
    if not name_text:
        raise ValueError("模板名称不能为空")
    if not prompt_text:
        raise ValueError("模板内容不能为空")
    row = AnalysisPromptTemplateDB(
        id=uuid4().hex,
        user_id=user_id,
        scope=scope_text,
        name=name_text,
        description=(description or "").strip() or None,
        template_text=prompt_text,
        intent_json=_safe_dict(intent_json),
        is_builtin=False,
        is_active=bool(is_active),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _to_dict(row)


def update_custom_template(
    db: Session,
    user_id: str,
    template_id: str,
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    template_text: Optional[str] = None,
    intent_json: Optional[Dict[str, Any]] = None,
    is_active: Optional[bool] = None,
) -> Dict[str, Any]:
    row = db.query(AnalysisPromptTemplateDB).filter(
        AnalysisPromptTemplateDB.id == template_id,
        AnalysisPromptTemplateDB.user_id == user_id,
        AnalysisPromptTemplateDB.is_builtin.is_(False),
    ).first()
    if row is None:
        raise ValueError("模板不存在或不可编辑")
    if name is not None:
        text = (name or "").strip()
        if not text:
            raise ValueError("模板名称不能为空")
        row.name = text
    if description is not None:
        row.description = (description or "").strip() or None
    if template_text is not None:
        text = (template_text or "").strip()
        if not text:
            raise ValueError("模板内容不能为空")
        row.template_text = text
    if intent_json is not None:
        row.intent_json = _safe_dict(intent_json)
    if is_active is not None:
        row.is_active = bool(is_active)
    db.commit()
    db.refresh(row)
    return _to_dict(row)


def resolve_template(
    db: Session,
    user_id: str,
    template_id: Optional[str],
    *,
    fallback_template_id: str = DEFAULT_MANUAL_TEMPLATE_ID,
    scope: str = SCOPE_DEEP_ANALYSIS,
) -> Dict[str, Any]:
    ensure_builtin_templates(db)
    chosen_id = (template_id or "").strip() or fallback_template_id
    row = db.query(AnalysisPromptTemplateDB).filter(
        AnalysisPromptTemplateDB.id == chosen_id,
        AnalysisPromptTemplateDB.scope == scope,
        AnalysisPromptTemplateDB.is_active.is_(True),
        ((AnalysisPromptTemplateDB.user_id == user_id) | (AnalysisPromptTemplateDB.is_builtin.is_(True))),
    ).first()
    if row is None and chosen_id != fallback_template_id:
        row = db.query(AnalysisPromptTemplateDB).filter(
            AnalysisPromptTemplateDB.id == fallback_template_id,
            AnalysisPromptTemplateDB.scope == scope,
            AnalysisPromptTemplateDB.is_active.is_(True),
            ((AnalysisPromptTemplateDB.user_id == user_id) | (AnalysisPromptTemplateDB.is_builtin.is_(True))),
        ).first()
    if row is None:
        raise ValueError("未找到可用的提示词模板")
    return _to_dict(row)


def render_template(
    template: Dict[str, Any],
    *,
    symbol: str,
    name: Optional[str],
    horizon: str = "short",
    trade_date: Optional[str] = None,
    prompt_vars: Optional[Dict[str, Any]] = None,
    user_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    symbol_text = (symbol or "").strip().upper()
    stock_name = (name or "").strip() or symbol_text
    vars_data = _TemplateVars()
    vars_data.update(
        {
            "symbol": symbol_text,
            "name": stock_name,
            "horizon": horizon,
            "horizon_label": _horizon_label(horizon),
            "trade_date": trade_date or "",
        }
    )
    if prompt_vars:
        vars_data.update({str(k): v for k, v in prompt_vars.items()})

    template_text = str(template.get("template_text") or "").strip()
    query = template_text.format_map(vars_data) if template_text else f"分析 {symbol_text}"

    user_intent = deepcopy(_safe_dict(template.get("intent_json")))
    user_intent["ticker"] = symbol_text
    user_intent["horizons"] = [horizon]
    user_intent["prompt_template_id"] = template.get("id")
    if user_context:
        user_intent["user_context"] = deepcopy(user_context)
    else:
        user_intent.setdefault("user_context", {})
    user_intent.setdefault("focus_areas", [])
    user_intent.setdefault("specific_questions", [])

    return {
        "query": query,
        "user_intent": user_intent,
    }


def get_template_defaults() -> Dict[str, str]:
    return {
        "manual_deep_analysis": DEFAULT_MANUAL_TEMPLATE_ID,
        "scheduled_analysis": DEFAULT_SCHEDULED_TEMPLATE_ID,
    }


def list_builtin_template_ids() -> Iterable[str]:
    return (item["id"] for item in _BUILTIN_TEMPLATES)
