from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy.orm import Session

from api.database import ModelProfileDB
from api.services.auth_service import decrypt_secret, encrypt_secret
from tradingagents.llm_clients.validators import validate_llm_provider


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _to_dict(row: ModelProfileDB) -> dict[str, Any]:
    return {
        "id": row.id,
        "user_id": row.user_id,
        "name": row.name,
        "description": row.description,
        "llm_provider": row.llm_provider,
        "backend_url": row.backend_url,
        "quick_think_llm": row.quick_think_llm,
        "deep_think_llm": row.deep_think_llm,
        "is_default": bool(row.is_default),
        "is_active": bool(row.is_active),
        "tags": list(row.tags_json or []),
        "has_api_key": bool(row.api_key_encrypted),
        "last_probe_status": row.last_probe_status,
        "last_probe_error": row.last_probe_error,
        "last_probe_at": row.last_probe_at.isoformat() if row.last_probe_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _normalize_name(name: str) -> str:
    text = (name or "").strip()
    if not text:
        raise ValueError("模型配置名称不能为空")
    if len(text) > 80:
        raise ValueError("模型配置名称长度不能超过 80")
    return text


def _normalize_provider(provider: str) -> str:
    return validate_llm_provider(provider)


def _normalize_model(model: Optional[str], field_name: str) -> Optional[str]:
    if model is None:
        return None
    text = str(model).strip()
    if text == "":
        return None
    if len(text) > 255:
        raise ValueError(f"{field_name} 长度不能超过 255")
    return text


def _normalize_tags(tags: Optional[list[str]]) -> list[str]:
    if not tags:
        return []
    cleaned: list[str] = []
    for item in tags:
        text = str(item or "").strip()
        if text:
            cleaned.append(text[:40])
    return cleaned[:20]


def list_model_profiles(db: Session, user_id: str, *, include_inactive: bool = False) -> list[dict[str, Any]]:
    query = db.query(ModelProfileDB).filter(ModelProfileDB.user_id == user_id)
    if not include_inactive:
        query = query.filter(ModelProfileDB.is_active.is_(True))
    rows = query.order_by(ModelProfileDB.is_default.desc(), ModelProfileDB.created_at.asc()).all()
    return [_to_dict(row) for row in rows]


def get_model_profile(db: Session, user_id: str, profile_id: str) -> Optional[ModelProfileDB]:
    return (
        db.query(ModelProfileDB)
        .filter(ModelProfileDB.user_id == user_id, ModelProfileDB.id == profile_id)
        .first()
    )


def _clear_default_if_needed(db: Session, user_id: str, except_profile_id: str | None = None) -> None:
    query = db.query(ModelProfileDB).filter(
        ModelProfileDB.user_id == user_id,
        ModelProfileDB.is_default.is_(True),
    )
    if except_profile_id:
        query = query.filter(ModelProfileDB.id != except_profile_id)
    for row in query.all():
        row.is_default = False
        row.updated_at = _utcnow()


def create_model_profile(
    db: Session,
    *,
    user_id: str,
    name: str,
    llm_provider: str,
    backend_url: Optional[str] = None,
    quick_think_llm: Optional[str] = None,
    deep_think_llm: Optional[str] = None,
    api_key: Optional[str] = None,
    description: Optional[str] = None,
    tags: Optional[list[str]] = None,
    is_default: bool = False,
    is_active: bool = True,
) -> dict[str, Any]:
    now = _utcnow()
    row = ModelProfileDB(
        id=uuid4().hex,
        user_id=user_id,
        name=_normalize_name(name),
        description=(description or "").strip() or None,
        llm_provider=_normalize_provider(llm_provider),
        backend_url=(backend_url or "").strip() or None,
        quick_think_llm=_normalize_model(quick_think_llm, "quick_think_llm"),
        deep_think_llm=_normalize_model(deep_think_llm, "deep_think_llm"),
        api_key_encrypted=encrypt_secret(api_key) if api_key else None,
        is_default=bool(is_default),
        is_active=bool(is_active),
        tags_json=_normalize_tags(tags),
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.flush()
    if row.is_default:
        _clear_default_if_needed(db, user_id, except_profile_id=row.id)
    db.commit()
    db.refresh(row)
    return _to_dict(row)


def update_model_profile(
    db: Session,
    *,
    user_id: str,
    profile_id: str,
    name: Optional[str] = None,
    llm_provider: Optional[str] = None,
    backend_url: Optional[str] = None,
    quick_think_llm: Optional[str] = None,
    deep_think_llm: Optional[str] = None,
    api_key: Optional[str] = None,
    clear_api_key: bool = False,
    description: Optional[str] = None,
    tags: Optional[list[str]] = None,
    is_default: Optional[bool] = None,
    is_active: Optional[bool] = None,
) -> dict[str, Any]:
    row = get_model_profile(db, user_id, profile_id)
    if row is None:
        raise LookupError("模型配置不存在")

    if name is not None:
        row.name = _normalize_name(name)
    if llm_provider is not None:
        row.llm_provider = _normalize_provider(llm_provider)
    if backend_url is not None:
        row.backend_url = (backend_url or "").strip() or None
    if quick_think_llm is not None:
        row.quick_think_llm = _normalize_model(quick_think_llm, "quick_think_llm")
    if deep_think_llm is not None:
        row.deep_think_llm = _normalize_model(deep_think_llm, "deep_think_llm")
    if description is not None:
        row.description = (description or "").strip() or None
    if tags is not None:
        row.tags_json = _normalize_tags(tags)
    if is_active is not None:
        row.is_active = bool(is_active)
    if clear_api_key:
        row.api_key_encrypted = None
    elif api_key:
        row.api_key_encrypted = encrypt_secret(api_key)
    if is_default is not None:
        row.is_default = bool(is_default)
    row.updated_at = _utcnow()

    db.flush()
    if row.is_default:
        _clear_default_if_needed(db, user_id, except_profile_id=row.id)
    db.commit()
    db.refresh(row)
    return _to_dict(row)


def delete_model_profile(db: Session, *, user_id: str, profile_id: str) -> bool:
    row = get_model_profile(db, user_id, profile_id)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


def resolve_runtime_overrides(
    db: Session,
    *,
    user_id: str,
    profile_id: str,
    include_inactive: bool = False,
) -> dict[str, Any]:
    row = get_model_profile(db, user_id, profile_id)
    if row is None:
        raise LookupError("模型配置不存在")
    if not include_inactive and not row.is_active:
        raise ValueError("模型配置已停用")

    overrides: dict[str, Any] = {
        "llm_provider": row.llm_provider,
    }
    if row.backend_url:
        overrides["backend_url"] = row.backend_url
    if row.quick_think_llm:
        overrides["quick_think_llm"] = row.quick_think_llm
    if row.deep_think_llm:
        overrides["deep_think_llm"] = row.deep_think_llm
    api_key = decrypt_secret(row.api_key_encrypted)
    if api_key:
        overrides["api_key"] = api_key
    return overrides


def update_probe_status(
    db: Session,
    *,
    user_id: str,
    profile_id: str,
    status: str,
    error: Optional[str] = None,
) -> dict[str, Any]:
    row = get_model_profile(db, user_id, profile_id)
    if row is None:
        raise LookupError("模型配置不存在")
    row.last_probe_status = (status or "").strip()[:20] or None
    row.last_probe_error = (error or "").strip()[:500] or None
    row.last_probe_at = _utcnow()
    row.updated_at = _utcnow()
    db.commit()
    db.refresh(row)
    return _to_dict(row)
