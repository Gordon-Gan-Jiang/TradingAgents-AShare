from __future__ import annotations

import base64
import hashlib
import os
import secrets
import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Optional
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from api.database import EmailVerificationCodeDB, UserDB, UserLLMConfigDB


ALGORITHM = "HS256"


def _utcnow() -> datetime:
    return datetime.utcnow()


def _secret_key() -> str:
    return os.getenv("APP_SECRET_KEY") or os.getenv("SECRET_KEY") or "tradingagents-ashare-dev-secret"


def _fernet() -> Fernet:
    digest = hashlib.sha256(_secret_key().encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_secret(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        return _fernet().decrypt(value.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        return None


def normalize_email(email: str) -> str:
    return email.strip().lower()


def generate_login_code() -> str:
    return f"{secrets.randbelow(1000000):06d}"


def hash_code(email: str, code: str) -> str:
    return hashlib.sha256(f"{normalize_email(email)}:{code}:{_secret_key()}".encode("utf-8")).hexdigest()


def create_access_token(user: UserDB, expires_days: int = 30) -> str:
    now = _utcnow()
    payload = {
        "sub": user.id,
        "email": user.email,
        "exp": now + timedelta(days=expires_days),
        "iat": now,
    }
    return jwt.encode(payload, _secret_key(), algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict:
    return jwt.decode(token, _secret_key(), algorithms=[ALGORITHM])


def get_user_by_email(db: Session, email: str) -> Optional[UserDB]:
    return db.query(UserDB).filter(UserDB.email == normalize_email(email)).first()


def get_user_by_id(db: Session, user_id: str) -> Optional[UserDB]:
    return db.query(UserDB).filter(UserDB.id == user_id).first()


def upsert_login_code(db: Session, email: str, purpose: str = "login") -> str:
    email = normalize_email(email)
    code = generate_login_code()
    now = _utcnow()

    db.query(EmailVerificationCodeDB).filter(
        EmailVerificationCodeDB.email == email,
        EmailVerificationCodeDB.purpose == purpose,
        EmailVerificationCodeDB.consumed_at.is_(None),
    ).update({"consumed_at": now})

    row = EmailVerificationCodeDB(
        id=str(uuid4()),
        email=email,
        code_hash=hash_code(email, code),
        purpose=purpose,
        expires_at=now + timedelta(minutes=10),
        created_at=now,
    )
    db.add(row)
    db.commit()
    return code


def verify_login_code(db: Session, email: str, code: str, purpose: str = "login") -> Optional[UserDB]:
    email = normalize_email(email)
    now = _utcnow()
    code_row = (
        db.query(EmailVerificationCodeDB)
        .filter(
            EmailVerificationCodeDB.email == email,
            EmailVerificationCodeDB.purpose == purpose,
            EmailVerificationCodeDB.consumed_at.is_(None),
        )
        .order_by(EmailVerificationCodeDB.created_at.desc())
        .first()
    )
    if not code_row or code_row.expires_at < now:
        return None
    if code_row.code_hash != hash_code(email, code):
        return None

    code_row.consumed_at = now
    user = get_user_by_email(db, email)
    if not user:
        user = UserDB(
            id=str(uuid4()),
            email=email,
            is_active=True,
            created_at=now,
            updated_at=now,
            last_login_at=now,
        )
        db.add(user)
    else:
        user.last_login_at = now
        user.updated_at = now
    db.commit()
    db.refresh(user)
    return user


def send_login_code(email: str, code: str) -> Optional[str]:
    smtp_host = (
        os.getenv("MAIL_SERVER", "").strip()
        or os.getenv("SMTP_HOST", "").strip()
    )
    if not smtp_host:
        print(f"[auth] login code for {email}: {code}")
        if os.getenv("APP_ENV", "development") != "production":
            return code
        return None

    smtp_port = int(os.getenv("MAIL_PORT") or os.getenv("SMTP_PORT") or "587")
    smtp_user = (os.getenv("MAIL_USERNAME") or os.getenv("SMTP_USER") or "").strip()
    smtp_password = (os.getenv("MAIL_PASSWORD") or os.getenv("SMTP_PASSWORD") or "").strip()
    smtp_from = (os.getenv("MAIL_FROM") or os.getenv("SMTP_FROM") or smtp_user or "noreply@example.com").strip()
    smtp_starttls = (os.getenv("MAIL_STARTTLS") or os.getenv("SMTP_TLS") or "1").strip().lower() not in ("0", "false", "off", "no")
    smtp_ssl_tls = (os.getenv("MAIL_SSL_TLS") or "0").strip().lower() in ("1", "true", "on", "yes")

    msg = EmailMessage()
    msg["Subject"] = "TradingAgents 登录验证码"
    msg["From"] = smtp_from
    msg["To"] = email
    msg.set_content(f"你的 TradingAgents 登录验证码是：{code}\n\n10 分钟内有效。")

    smtp_cls = smtplib.SMTP_SSL if smtp_ssl_tls else smtplib.SMTP
    with smtp_cls(smtp_host, smtp_port, timeout=20) as server:
        if smtp_starttls and not smtp_ssl_tls:
            server.starttls()
        if smtp_user:
            server.login(smtp_user, smtp_password)
        server.send_message(msg)
    return None


def get_user_llm_config(db: Session, user_id: str) -> Optional[UserLLMConfigDB]:
    return db.query(UserLLMConfigDB).filter(UserLLMConfigDB.user_id == user_id).first()


def upsert_user_llm_config(
    db: Session,
    user_id: str,
    *,
    llm_provider: Optional[str] = None,
    backend_url: Optional[str] = None,
    quick_think_llm: Optional[str] = None,
    deep_think_llm: Optional[str] = None,
    max_debate_rounds: Optional[int] = None,
    max_risk_discuss_rounds: Optional[int] = None,
    api_key: Optional[str] = None,
    clear_api_key: bool = False,
) -> UserLLMConfigDB:
    row = get_user_llm_config(db, user_id)
    now = _utcnow()
    if not row:
        row = UserLLMConfigDB(user_id=user_id, created_at=now, updated_at=now)
        db.add(row)

    if llm_provider is not None:
        row.llm_provider = llm_provider
    if backend_url is not None:
        row.backend_url = backend_url
    if quick_think_llm is not None:
        row.quick_think_llm = quick_think_llm
    if deep_think_llm is not None:
        row.deep_think_llm = deep_think_llm
    if max_debate_rounds is not None:
        row.max_debate_rounds = max_debate_rounds
    if max_risk_discuss_rounds is not None:
        row.max_risk_discuss_rounds = max_risk_discuss_rounds

    if clear_api_key:
        row.api_key_encrypted = None
    elif api_key:
        row.api_key_encrypted = encrypt_secret(api_key)

    row.updated_at = now
    db.commit()
    db.refresh(row)
    return row
