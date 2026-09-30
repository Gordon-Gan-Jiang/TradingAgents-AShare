"""Database configuration and session management."""

import logging
import os
from datetime import datetime, timezone
from typing import Generator

from sqlalchemy import Boolean, create_engine, Column, String, DateTime, Text, Integer, Float, JSON, UniqueConstraint, event, text
from sqlalchemy.orm import declarative_base, sessionmaker, Session

# Database URL - default to SQLite for simplicity
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./tradingagents.db")

# Create engine
if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False},
        echo=False,
        pool_size=10,
        max_overflow=20,
        pool_timeout=60,
        pool_recycle=3600,
    )

    def _can_use_wal() -> bool:
        """Check if WAL mode is safe: db's parent dir must be writable for -shm/-wal files."""
        import pathlib
        db_path = DATABASE_URL.replace("sqlite:///", "").replace("sqlite://", "")
        parent = pathlib.Path(db_path).resolve().parent
        return os.access(parent, os.W_OK)

    _use_wal = _can_use_wal()

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        # Wait up to 30 s for the write lock before raising OperationalError.
        cursor.execute("PRAGMA busy_timeout=30000")
        if _use_wal:
            cursor.execute("PRAGMA journal_mode=WAL")
            # Limit WAL file size; auto-checkpoint keeps readers fast.
            cursor.execute("PRAGMA wal_autocheckpoint=100")
        cursor.close()
else:
    # For PostgreSQL/MySQL, use a larger pool to handle concurrency
    engine = create_engine(
        DATABASE_URL,
        echo=False,
        pool_size=20,
        max_overflow=10,
        pool_timeout=30,
        pool_recycle=3600,
    )

# Session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Base class for models
Base = declarative_base()
logger = logging.getLogger(__name__)


def get_db() -> Generator[Session, None, None]:
    """Get database session (for FastAPI Depends)."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class get_db_ctx:
    """Context manager for manual DB session usage.

    Usage:
        with get_db_ctx() as db:
            db.query(...)
    """

    def __init__(self) -> None:
        self.db: Session | None = None

    def __enter__(self) -> Session:
        self.db = SessionLocal()
        return self.db

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self.db is not None:
            if exc_type is not None:
                self.db.rollback()
            self.db.close()


def init_db() -> None:
    """Initialize database tables."""
    Base.metadata.create_all(bind=engine)
    _ensure_report_schema()
    _ensure_report_t1_outcome_schema()
    _ensure_user_schema()
    _ensure_market_scan_t1_schema()
    _ensure_scheduled_prompt_schema()
    _ensure_data_source_probe_schema()
    _ensure_trade_plan_schema()
    _ensure_t1_daily_stat_schema()


def _ensure_weight_version_schema() -> None:
    """为既有部署补齐权重版本表（create_all 不改造已存在的表）。"""
    with engine.begin() as conn:
        try:
            existing = {row[1] for row in conn.execute(text("PRAGMA table_info(signal_weight_versions)"))}
        except Exception:
            return
        for column, ddl in (("is_active", "BOOLEAN DEFAULT 0"), ("per_dimension", "JSON")):
            if column not in existing:
                conn.execute(text(f"ALTER TABLE signal_weight_versions ADD COLUMN {column} {ddl}"))


def _ensure_report_schema() -> None:
    """Add lightweight columns for existing SQLite deployments without migrations."""
    try:
        with engine.begin() as conn:
            columns = {row[1] for row in conn.execute(text("PRAGMA table_info(reports)"))}
            if "direction" not in columns:
                conn.execute(text("ALTER TABLE reports ADD COLUMN direction VARCHAR(50)"))
            if "status" not in columns:
                conn.execute(text("ALTER TABLE reports ADD COLUMN status VARCHAR(20) DEFAULT 'completed'"))
            if "error" not in columns:
                conn.execute(text("ALTER TABLE reports ADD COLUMN error TEXT"))
            if "analyst_traces" not in columns:
                conn.execute(text("ALTER TABLE reports ADD COLUMN analyst_traces JSON"))
            if "macro_report" not in columns:
                conn.execute(text("ALTER TABLE reports ADD COLUMN macro_report TEXT"))
            if "smart_money_report" not in columns:
                conn.execute(text("ALTER TABLE reports ADD COLUMN smart_money_report TEXT"))
            if "game_theory_report" not in columns:
                conn.execute(text("ALTER TABLE reports ADD COLUMN game_theory_report TEXT"))
            if "volume_price_report" not in columns:
                conn.execute(text("ALTER TABLE reports ADD COLUMN volume_price_report TEXT"))
            if "freshness_status" not in columns:
                conn.execute(text("ALTER TABLE reports ADD COLUMN freshness_status VARCHAR(16)"))
            if "horizon" not in columns:
                conn.execute(text("ALTER TABLE reports ADD COLUMN horizon VARCHAR(16)"))
    except Exception as e:
        logger.error("Failed to ensure report schema: %s", e)


def _ensure_report_t1_outcome_schema() -> None:
    """Add the A5/F1 horizon columns to ``report_t1_outcomes``.

    Historical rows keep ``horizon IS NULL``: they were written before the column
    existed, so their period is genuinely unknown and must not be backfilled with a
    guess. Readers treat NULL as "pre-A5 row", the same convention already used for
    ``t1_daily_stats.effective_n``.
    """
    try:
        with engine.begin() as conn:
            columns = {row[1] for row in conn.execute(text("PRAGMA table_info(report_t1_outcomes)"))}
            if "horizon" not in columns:
                conn.execute(text("ALTER TABLE report_t1_outcomes ADD COLUMN horizon VARCHAR(16)"))
            if "plan_horizon_days" not in columns:
                conn.execute(text("ALTER TABLE report_t1_outcomes ADD COLUMN plan_horizon_days INTEGER"))
    except Exception as e:
        logger.error("Failed to ensure report_t1_outcomes schema: %s", e)


def _ensure_trade_plan_schema() -> None:
    """Add lightweight columns to ``trade_plans`` for existing SQLite deployments.

    ``target_price`` 是报告结构化抽取出的**单一**目标价（``build_from_extracted``
    的回退来源）。历史实现把它包成 ``[[target, 100.0]]`` 写进
    ``take_profit_ladder_json``，让一个抽取数字在库里长得像模型提出的分批止盈。
    拆出独立列后，阶梯与单一目标价不再混为一谈；退出引擎通过
    ``trade_plan_service.target_levels`` 统一读取两者。

    不做数据回填：无法区分历史行里的单档阶梯是"模型真给了 100% 单档止盈"还是
    "抽取字段被包装"，猜测回填正是要被消除的那类问题。历史行继续按阶梯读取。
    """
    try:
        with engine.begin() as conn:
            columns = {row[1] for row in conn.execute(text("PRAGMA table_info(trade_plans)"))}
            if "target_price" not in columns:
                conn.execute(text("ALTER TABLE trade_plans ADD COLUMN target_price FLOAT"))
    except Exception as e:
        logger.error("Failed to ensure trade_plan schema: %s", e)


def _ensure_t1_daily_stat_schema() -> None:
    """Add the honest-measurement columns to ``t1_daily_stats`` (D1/D2/D3).

    Historical rows keep ``NULL`` for the new columns — no backfill. The old rows
    only stored row-count-based aggregates, and their deduped equivalents cannot be
    recovered from the aggregate alone (the underlying per-window directions are
    gone). A reader that sees ``effective_n IS NULL`` should treat the row as
    "pre-honest-metrics" rather than assume the sample was fully independent;
    re-running ``refresh_t1_daily_stats`` recomputes the affected days properly.
    """
    try:
        with engine.begin() as conn:
            columns = {row[1] for row in conn.execute(text("PRAGMA table_info(t1_daily_stats)"))}
            for name, ddl_type in (
                ("effective_n", "INTEGER"),
                ("unique_symbols", "INTEGER"),
                ("top_symbol_share", "FLOAT"),
                ("ci_low", "FLOAT"),
                ("ci_high", "FLOAT"),
                ("abstain_count", "INTEGER"),
                ("conflict_count", "INTEGER"),
            ):
                if name not in columns:
                    conn.execute(text(f"ALTER TABLE t1_daily_stats ADD COLUMN {name} {ddl_type}"))
    except Exception as e:
        logger.error("Failed to ensure t1_daily_stats schema: %s", e)


def _ensure_user_schema() -> None:
    """Add columns to users table for existing SQLite deployments without migrations."""
    try:
        with engine.begin() as conn:
            columns = {row[1] for row in conn.execute(text("PRAGMA table_info(users)"))}
            if "last_login_ip" not in columns:
                conn.execute(text("ALTER TABLE users ADD COLUMN last_login_ip VARCHAR(45)"))
            if "email_report_enabled" not in columns:
                conn.execute(text("ALTER TABLE users ADD COLUMN email_report_enabled BOOLEAN NOT NULL DEFAULT 1"))
            if "wecom_report_enabled" not in columns:
                conn.execute(text("ALTER TABLE users ADD COLUMN wecom_report_enabled BOOLEAN NOT NULL DEFAULT 1"))
            if "wps_report_enabled" not in columns:
                conn.execute(text("ALTER TABLE users ADD COLUMN wps_report_enabled BOOLEAN NOT NULL DEFAULT 1"))
            llm_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(user_llm_configs)"))}
            if "wecom_webhook_encrypted" not in llm_columns:
                conn.execute(text("ALTER TABLE user_llm_configs ADD COLUMN wecom_webhook_encrypted TEXT"))
            if "wps_webhook_encrypted" not in llm_columns:
                conn.execute(text("ALTER TABLE user_llm_configs ADD COLUMN wps_webhook_encrypted TEXT"))
            if "decision_critic_enabled" not in llm_columns:
                conn.execute(text("ALTER TABLE user_llm_configs ADD COLUMN decision_critic_enabled BOOLEAN"))
            if "decision_critic_revision_threshold" not in llm_columns:
                conn.execute(
                    text("ALTER TABLE user_llm_configs ADD COLUMN decision_critic_revision_threshold FLOAT")
                )
    except Exception as e:
        logger.error("Failed to ensure user schema: %s", e)

    _migrate_tokens_to_hashed()


def _ensure_market_scan_t1_schema() -> None:
    """Add T+1 evaluation columns for market_scan_results (SQLite)."""
    try:
        with engine.begin() as conn:
            columns = {row[1] for row in conn.execute(text("PRAGMA table_info(market_scan_results)"))}
            if not columns:
                return
            if "t1_signal_date" not in columns:
                conn.execute(text("ALTER TABLE market_scan_results ADD COLUMN t1_signal_date VARCHAR(10)"))
            if "t1_trade_date" not in columns:
                conn.execute(text("ALTER TABLE market_scan_results ADD COLUMN t1_trade_date VARCHAR(10)"))
            if "t1_return_pct" not in columns:
                conn.execute(text("ALTER TABLE market_scan_results ADD COLUMN t1_return_pct FLOAT"))
            if "t1_status" not in columns:
                conn.execute(text("ALTER TABLE market_scan_results ADD COLUMN t1_status VARCHAR(20)"))
    except Exception as e:
        logger.error("Failed to ensure market scan T+1 schema: %s", e)
    _migrate_api_keys_reencrypt()


def _ensure_data_source_probe_schema() -> None:
    """Add timeout/row_unit columns for the data source probe tables (SQLite).

    `create_all` 只建新表、**不给已存在的表加列**。这两张表在本机已经存在并有
    历史数据，所以新增列必须走 ALTER——否则升级后查询会直接报
    "no such column: timeout_count"。
    """
    try:
        with engine.begin() as conn:
            run_columns = {
                row[1] for row in conn.execute(text("PRAGMA table_info(data_source_probe_runs)"))
            }
            if run_columns and "timeout_count" not in run_columns:
                conn.execute(
                    text("ALTER TABLE data_source_probe_runs ADD COLUMN timeout_count INTEGER DEFAULT 0")
                )

            result_columns = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(data_source_probe_results)"))
            }
            if result_columns and "row_unit" not in result_columns:
                conn.execute(
                    text("ALTER TABLE data_source_probe_results ADD COLUMN row_unit VARCHAR(8)")
                )
    except Exception as e:
        logger.error("Failed to ensure data source probe schema: %s", e)


def _ensure_scheduled_prompt_schema() -> None:
    """Add prompt template fields for scheduled_analyses (SQLite)."""
    try:
        with engine.begin() as conn:
            columns = {row[1] for row in conn.execute(text("PRAGMA table_info(scheduled_analyses)"))}
            if not columns:
                return
            if "prompt_template_id" not in columns:
                conn.execute(text("ALTER TABLE scheduled_analyses ADD COLUMN prompt_template_id VARCHAR(64)"))
            if "prompt_vars_json" not in columns:
                conn.execute(text("ALTER TABLE scheduled_analyses ADD COLUMN prompt_vars_json JSON"))
    except Exception as e:
        logger.error("Failed to ensure scheduled prompt schema: %s", e)


def _migrate_tokens_to_hashed() -> None:
    """Migrate plaintext API tokens to HMAC-SHA256 hashed storage."""
    import hashlib, hmac
    try:
        with engine.begin() as conn:
            # Add token_hint column if missing
            token_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(user_tokens)"))}
            if "token_hint" not in token_cols:
                conn.execute(text("ALTER TABLE user_tokens ADD COLUMN token_hint VARCHAR(8)"))

            # Detect un-migrated rows: plaintext tokens start with "ta-sk-"
            rows = conn.execute(text("SELECT id, token FROM user_tokens WHERE token LIKE 'ta-sk-%'")).fetchall()
            if not rows:
                return
            from api.services.auth_service import _secret_key
            key = _secret_key().encode("utf-8")
            for row_id, plaintext in rows:
                token_hash = hmac.new(key, plaintext.encode("utf-8"), hashlib.sha256).hexdigest()
                hint = plaintext[-4:]
                conn.execute(
                    text("UPDATE user_tokens SET token = :hash, token_hint = :hint WHERE id = :id"),
                    {"hash": token_hash, "hint": hint, "id": row_id},
                )
            logger.info("[security] Migrated %s API tokens from plaintext to hashed storage.", len(rows))
    except Exception as e:
        logger.error("Token hash migration failed: %s", e)


def _migrate_api_keys_reencrypt() -> None:
    """Re-encrypt user secrets when TA_APP_SECRET_KEY changes.

    On startup, if a custom secret is configured, tries to decrypt each secret
    with the current secret. If that fails, tries the default secret (old data).
    If the default key works, re-encrypts with the current key and writes back.
    """
    from api.services.auth_service import (
        is_custom_secret_configured, decrypt_secret,
        decrypt_secret_with_fallback, encrypt_secret,
    )
    if not is_custom_secret_configured():
        return
    try:
        with engine.begin() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT user_id, api_key_encrypted, wecom_webhook_encrypted, wps_webhook_encrypted
                    FROM user_llm_configs
                    WHERE api_key_encrypted IS NOT NULL OR wecom_webhook_encrypted IS NOT NULL
                       OR wps_webhook_encrypted IS NOT NULL
                    """
                )
            ).fetchall()
            if not rows:
                return
            # Quick check: if the first row decrypts fine, likely all are OK already.
            _, first_api_key, first_wecom_webhook, first_wps_webhook = rows[0]
            first_secret = first_api_key or first_wecom_webhook or first_wps_webhook
            if first_secret and decrypt_secret(first_secret) is not None and len(rows) < 50:
                # Small dataset, still verify all — but for large sets, skip if first is OK
                pass
            migrated = 0
            for user_id, encrypted_api_key, encrypted_wecom_webhook, encrypted_wps_webhook in rows:
                for column_name, encrypted_value in (
                    ("api_key_encrypted", encrypted_api_key),
                    ("wecom_webhook_encrypted", encrypted_wecom_webhook),
                    ("wps_webhook_encrypted", encrypted_wps_webhook),
                ):
                    if not encrypted_value:
                        continue
                    if decrypt_secret(encrypted_value) is not None:
                        continue
                    plaintext = decrypt_secret_with_fallback(encrypted_value)
                    if plaintext is None:
                        logger.warning(
                            "[security] Cannot decrypt %s for user %s with any known key. Skipping.",
                            column_name,
                            user_id,
                        )
                        continue
                    new_encrypted = encrypt_secret(plaintext)
                    if column_name == "api_key_encrypted":
                        conn.execute(
                            text("UPDATE user_llm_configs SET api_key_encrypted = :enc WHERE user_id = :uid"),
                            {"enc": new_encrypted, "uid": user_id},
                        )
                    elif column_name == "wecom_webhook_encrypted":
                        conn.execute(
                            text("UPDATE user_llm_configs SET wecom_webhook_encrypted = :enc WHERE user_id = :uid"),
                            {"enc": new_encrypted, "uid": user_id},
                        )
                    elif column_name == "wps_webhook_encrypted":
                        conn.execute(
                            text("UPDATE user_llm_configs SET wps_webhook_encrypted = :enc WHERE user_id = :uid"),
                            {"enc": new_encrypted, "uid": user_id},
                        )
                    migrated += 1
            if migrated:
                logger.info("[security] Re-encrypted %s user secret(s) with new TA_APP_SECRET_KEY.", migrated)
    except Exception as e:
        logger.error("User secret re-encryption migration failed: %s", e)


# Report Model
class ReportDB(Base):
    """Report database model."""
    
    __tablename__ = "reports"
    
    id = Column(String(36), primary_key=True, index=True)
    user_id = Column(String(64), index=True, nullable=True)  # For future multi-user support
    symbol = Column(String(20), index=True, nullable=False)
    trade_date = Column(String(10), nullable=False)
    
    # Task lifecycle info
    status = Column(String(20), default="completed", index=True)  # pending, running, completed, failed
    error = Column(Text, nullable=True)
    
    # Decision info
    decision = Column(String(50), nullable=True)  # BUY, SELL, HOLD, etc.
    direction = Column(String(50), nullable=True)  # 看多、偏多、中性、偏空、看空
    # A5：这条结论属于哪个周期。取值与 `trade_plans.horizon` 一致（short|medium|dual），
    # 旧行为下结论的周期无法被记录，于是 T+1 打分无法区分自己评的是哪个周期的观点。
    horizon = Column(String(16), nullable=True, index=True)
    confidence = Column(Integer, nullable=True)  # 0-100
    target_price = Column(Float, nullable=True)
    stop_loss_price = Column(Float, nullable=True)
    
    # Full analysis results stored as JSON
    result_data = Column(JSON, nullable=True)
    freshness_status = Column(String(16), nullable=True, index=True)

    # LLM-extracted structured data
    risk_items = Column(JSON, nullable=True)   # [{"name": "...", "level": "high|medium|low", "description": "..."}]
    key_metrics = Column(JSON, nullable=True)  # [{"name": "...", "value": "...", "status": "good|neutral|bad"}]
    # [{"agent": "...", "verdict": "...", "key_finding": "...",
    #   "verdict_parsed": bool,   # 仅表示 VERDICT 块是否解析成功
    #   "verdict_source": "verdict|analyst_json|none",  # verdict 的值从哪个机读块来
    #   "structured": {...}}]     # ANALYST_JSON 原文
    # 方向一律用 analyst_structured.resolve_trace_direction 判定，不要直接读 verdict。
    analyst_traces = Column(JSON, nullable=True)

    # Individual reports (for quick access)
    market_report = Column(Text, nullable=True)
    sentiment_report = Column(Text, nullable=True)
    news_report = Column(Text, nullable=True)
    fundamentals_report = Column(Text, nullable=True)
    macro_report = Column(Text, nullable=True)
    smart_money_report = Column(Text, nullable=True)
    volume_price_report = Column(Text, nullable=True)
    game_theory_report = Column(Text, nullable=True)
    investment_plan = Column(Text, nullable=True)
    trader_investment_plan = Column(Text, nullable=True)
    final_trade_decision = Column(Text, nullable=True)
    
    # Metadata
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    
    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "user_id": self.user_id,
            "symbol": self.symbol,
            "trade_date": self.trade_date,
            "decision": self.decision,
            "direction": self.direction,
            "confidence": self.confidence,
            "target_price": self.target_price,
            "stop_loss_price": self.stop_loss_price,
            "result_data": self.result_data,
            "freshness_status": self.freshness_status,
            "risk_items": self.risk_items,
            "key_metrics": self.key_metrics,
            "analyst_traces": self.analyst_traces,
            "market_report": self.market_report,
            "sentiment_report": self.sentiment_report,
            "news_report": self.news_report,
            "fundamentals_report": self.fundamentals_report,
            "macro_report": self.macro_report,
            "smart_money_report": self.smart_money_report,
            "volume_price_report": self.volume_price_report,
            "game_theory_report": self.game_theory_report,
            "investment_plan": self.investment_plan,
            "trader_investment_plan": self.trader_investment_plan,
            "final_trade_decision": self.final_trade_decision,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class UserDB(Base):
    __tablename__ = "users"

    id = Column(String(36), primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    last_login_at = Column(DateTime, nullable=True)
    last_login_ip = Column(String(45), nullable=True)
    email_report_enabled = Column(Boolean, default=True, nullable=False, server_default="1")
    wecom_report_enabled = Column(Boolean, default=True, nullable=False, server_default="1")
    wps_report_enabled = Column(Boolean, default=True, nullable=False, server_default="1")


class EmailVerificationCodeDB(Base):
    __tablename__ = "email_verification_codes"

    id = Column(String(36), primary_key=True, index=True)
    email = Column(String(255), index=True, nullable=False)
    code_hash = Column(String(255), nullable=False)
    purpose = Column(String(50), default="login", nullable=False)
    expires_at = Column(DateTime, nullable=False)
    consumed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class UserLLMConfigDB(Base):
    __tablename__ = "user_llm_configs"

    user_id = Column(String(36), primary_key=True, index=True)
    llm_provider = Column(String(50), nullable=True)
    backend_url = Column(String(500), nullable=True)
    quick_think_llm = Column(String(255), nullable=True)
    deep_think_llm = Column(String(255), nullable=True)
    max_debate_rounds = Column(Integer, nullable=True)
    max_risk_discuss_rounds = Column(Integer, nullable=True)
    decision_critic_enabled = Column(Boolean, nullable=True)
    decision_critic_revision_threshold = Column(Float, nullable=True)
    api_key_encrypted = Column(Text, nullable=True)
    wecom_webhook_encrypted = Column(Text, nullable=True)
    wps_webhook_encrypted = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class ModelProfileDB(Base):
    """User-managed model profiles for stock analysis and A/B comparisons."""

    __tablename__ = "model_profiles"

    id = Column(String(36), primary_key=True, index=True)
    user_id = Column(String(64), index=True, nullable=False)
    name = Column(String(80), nullable=False)
    description = Column(Text, nullable=True)
    llm_provider = Column(String(50), nullable=False)
    backend_url = Column(String(500), nullable=True)
    quick_think_llm = Column(String(255), nullable=True)
    deep_think_llm = Column(String(255), nullable=True)
    api_key_encrypted = Column(Text, nullable=True)
    is_default = Column(Boolean, default=False, nullable=False, server_default="0", index=True)
    is_active = Column(Boolean, default=True, nullable=False, server_default="1", index=True)
    tags_json = Column(JSON, nullable=True)
    last_probe_status = Column(String(20), nullable=True)
    last_probe_error = Column(Text, nullable=True)
    last_probe_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint('user_id', 'name', name='uq_model_profile_user_name'),
    )


class UserTokenDB(Base):
    __tablename__ = "user_tokens"

    id = Column(String(36), primary_key=True, index=True)
    user_id = Column(String(36), index=True, nullable=False)
    name = Column(String(50), nullable=False)
    token = Column(String(128), unique=True, index=True, nullable=False)
    token_hint = Column(String(8), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    last_used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class VersionStatsDB(Base):
    __tablename__ = "version_stats"

    id = Column(Integer, primary_key=True, autoincrement=True)
    version = Column(String(50), nullable=True)
    nonce = Column(String(64), nullable=True)
    remote_ip = Column(String(45), nullable=True, index=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class WatchlistItemDB(Base):
    """User watchlist items."""
    __tablename__ = "watchlist_items"

    id = Column(String(36), primary_key=True)
    user_id = Column(String(64), index=True, nullable=False)
    symbol = Column(String(20), nullable=False)
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (UniqueConstraint('user_id', 'symbol', name='uq_watchlist_user_symbol'),)


class ScheduledAnalysisDB(Base):
    """Scheduled daily analysis tasks."""
    __tablename__ = "scheduled_analyses"

    id = Column(String(36), primary_key=True)
    user_id = Column(String(64), index=True, nullable=False)
    symbol = Column(String(20), nullable=False)
    horizon = Column(String(10), default="short")
    trigger_time = Column(String(5), default="20:00")
    prompt_template_id = Column(String(64), nullable=True)
    prompt_vars_json = Column(JSON, nullable=True)
    is_active = Column(Boolean, default=True)
    last_run_date = Column(String(10), nullable=True)
    last_run_status = Column(String(10), nullable=True)
    last_report_id = Column(String(36), nullable=True)
    consecutive_failures = Column(Integer, default=0)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (UniqueConstraint('user_id', 'symbol', name='uq_scheduled_user_symbol'),)


class FeedbackDB(Base):
    """User feedback / message board."""
    __tablename__ = "feedbacks"

    id = Column(String(36), primary_key=True, index=True)
    user_id = Column(String(64), index=True, nullable=False)
    user_email = Column(String(255), nullable=False)
    subject = Column(String(200), nullable=False)
    content = Column(Text, nullable=False)
    admin_reply = Column(Text, nullable=True)
    replied_at = Column(DateTime, nullable=True)
    is_read = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class AnalysisPromptTemplateDB(Base):
    """User/system analysis prompt templates."""

    __tablename__ = "analysis_prompt_templates"

    id = Column(String(64), primary_key=True, index=True)
    user_id = Column(String(64), index=True, nullable=True)
    scope = Column(String(32), nullable=False, default="deep_analysis", index=True)
    name = Column(String(120), nullable=False)
    description = Column(Text, nullable=True)
    template_text = Column(Text, nullable=False)
    intent_json = Column(JSON, nullable=True)
    is_builtin = Column(Boolean, default=False, nullable=False, index=True)
    is_active = Column(Boolean, default=True, nullable=False, index=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class ImportedPortfolioPositionDB(Base):
    """Imported current holdings snapshot plus recent trade points for a symbol."""

    __tablename__ = "imported_portfolio_positions"

    id = Column(String(36), primary_key=True)
    user_id = Column(String(64), index=True, nullable=False)
    source = Column(String(32), default="manual", nullable=False)
    symbol = Column(String(20), nullable=False)
    security_name = Column(String(80), nullable=True)
    current_position = Column(Float, nullable=True)
    available_position = Column(Float, nullable=True)
    average_cost = Column(Float, nullable=True)
    market_value = Column(Float, nullable=True)
    current_position_pct = Column(Float, nullable=True)
    trade_points_json = Column(JSON, nullable=True)
    trade_points_count = Column(Integer, default=0, nullable=False)
    latest_trade_at = Column(String(32), nullable=True)
    latest_trade_action = Column(String(16), nullable=True)
    last_imported_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint('user_id', 'source', 'symbol', name='uq_imported_portfolio_user_source_symbol'),
    )


class DailyProductRunDB(Base):
    __tablename__ = "daily_product_runs"

    id = Column(String(36), primary_key=True, index=True)
    user_id = Column(String(64), index=True, nullable=False)
    mode = Column(String(32), nullable=False)
    status = Column(String(20), nullable=False, index=True)
    summary_json = Column(JSON, nullable=True)
    recommendation_json = Column(JSON, nullable=True)
    jobs_json = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    finished_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class MarketScanResultDB(Base):
    __tablename__ = "market_scan_results"

    id = Column(String(36), primary_key=True, index=True)
    run_id = Column(String(36), index=True, nullable=True)
    user_id = Column(String(64), index=True, nullable=False)
    source_mode = Column(String(32), nullable=False, default="market_scan")
    market = Column(String(8), nullable=False, default="cn")
    score_profile = Column(String(64), nullable=True)
    symbol = Column(String(20), index=True, nullable=False)
    name = Column(String(80), nullable=True)
    rank = Column(Integer, nullable=True)
    score = Column(Float, nullable=True)
    reasons_json = Column(JSON, nullable=True)
    strategy_hits_json = Column(JSON, nullable=True)
    risk_flags_json = Column(JSON, nullable=True)
    score_breakdown_json = Column(JSON, nullable=True)
    quote_json = Column(JSON, nullable=True)
    selected_for_analysis = Column(Boolean, default=False, nullable=False)
    entry_price = Column(Float, nullable=True)
    feedback_horizon_days = Column(Integer, default=5, nullable=False)
    feedback_status = Column(String(20), default="pending", index=True)
    exit_price = Column(Float, nullable=True)
    realized_return_pct = Column(Float, nullable=True)
    feedback_evaluated_at = Column(DateTime, nullable=True)
    # T+1 close-to-close vs next trading day (independent from multi-day feedback_*)
    t1_signal_date = Column(String(10), nullable=True, index=True)
    t1_trade_date = Column(String(10), nullable=True)
    t1_return_pct = Column(Float, nullable=True)
    t1_status = Column(String(20), nullable=True, index=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class MarketDailyPriceDB(Base):
    """Persisted market daily OHLC cache (symbol + trade date)."""

    __tablename__ = "market_daily_prices"

    id = Column(String(36), primary_key=True, index=True)
    market = Column(String(8), nullable=False, default="cn", index=True)
    symbol = Column(String(20), nullable=False, index=True)
    trade_date = Column(String(10), nullable=False, index=True)
    open_price = Column(Float, nullable=True)
    close_price = Column(Float, nullable=True)
    source = Column(String(32), nullable=False, default="vendor")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("market", "symbol", "trade_date", name="uq_market_daily_price_symbol_date"),
    )


class ReportT1OutcomeDB(Base):
    """T+1 outcome label for depth-analysis reports (portfolio symbols)."""

    __tablename__ = "report_t1_outcomes"

    id = Column(String(36), primary_key=True, index=True)
    user_id = Column(String(64), index=True, nullable=False)
    report_id = Column(String(36), unique=True, index=True, nullable=False)
    symbol = Column(String(20), index=True, nullable=False)
    signal_trade_date = Column(String(10), index=True, nullable=False)
    t1_trade_date = Column(String(10), nullable=True)
    p0 = Column(Float, nullable=True)
    p1 = Column(Float, nullable=True)
    return_t1_pct = Column(Float, nullable=True)
    direction_bucket = Column(String(16), nullable=True)
    label_correct = Column(Boolean, nullable=True)
    # A5/D5：本条打分**衡量的是哪个周期**。价格窗口固定为一个交易日（收盘→收盘），
    # 所以新行恒为 "t1"。显式存储而不靠推断，是为了让 D5 的"按周期统计"有列可依，
    # 也为了让 F1 的期限错配可被度量而不是被默认忽略。
    horizon = Column(String(16), nullable=True, index=True)
    # F1：被评报告所附交易计划自己的持有期（自然日）。目标价/止损/时间止损是约 20 天期的
    # 论点，却由一日收益打分。把计划的周期一并记下来，才能统计"有多少个 T+1 窗口
    # 其实承载的是中期方案"，而不是把两种期限悄悄混在一个命中率里。
    plan_horizon_days = Column(Integer, nullable=True)
    status = Column(String(20), nullable=False, default="pending", index=True)
    reason = Column(String(200), nullable=True)
    evaluated_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class T1DailyStatDB(Base):
    """Daily aggregated T+1 metrics for fast trend queries."""

    __tablename__ = "t1_daily_stats"

    id = Column(String(36), primary_key=True, index=True)
    user_id = Column(String(64), index=True, nullable=False)
    metric = Column(String(32), index=True, nullable=False)  # recommendation_t1 | report_accuracy_t1
    scope = Column(String(16), index=True, nullable=False, default="all")  # all | portfolio
    signal_trade_date = Column(String(10), index=True, nullable=False)
    t1_trade_date = Column(String(10), nullable=True)
    sample_count = Column(Integer, nullable=False, default=0)
    avg_return_pct = Column(Float, nullable=True)
    win_rate_pct = Column(Float, nullable=True)
    accuracy_pct = Column(Float, nullable=True)
    avg_calendar_gap_days = Column(Float, nullable=True)
    poor_tradability_rate_pct = Column(Float, nullable=True)
    high_impact_risk_rate_pct = Column(Float, nullable=True)
    # D1/D2/D3 诚实度量字段。
    #
    # `sample_count` 是**行数**，不是独立观测数：同一 (标的, 信号日) 上的多份研报
    # 共享同一次前瞻收益。实测 4601 行去重后仅 1583 个唯一价格窗口，单键最多 100 行，
    # 两只标的占 31.5%。所以按行数算置信区间会把样本量虚增约 3 倍。
    #
    # `effective_n` = 去重后的唯一价格窗口数，是命中率真正该用的分母；
    # `unique_symbols` / `top_symbol_share` 暴露集中度；
    # `ci_low`/`ci_high` 是 Wilson 区间（小样本下唯一可用的比例区间）；
    # `abstain_count`/`conflict_count` 让"弃权"与"方向矛盾"各自可见——
    # 否则一提高弃权率，命中率会自动上升而没人发现覆盖率掉了。
    effective_n = Column(Integer, nullable=True)
    unique_symbols = Column(Integer, nullable=True)
    top_symbol_share = Column(Float, nullable=True)
    ci_low = Column(Float, nullable=True)
    ci_high = Column(Float, nullable=True)
    abstain_count = Column(Integer, nullable=True)
    conflict_count = Column(Integer, nullable=True)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("user_id", "metric", "scope", "signal_trade_date", name="uq_t1_daily_stat_user_metric_scope_day"),
    )


class AnalysisLessonDB(Base):
    """【已废弃，仅保留历史数据】T+1 错例蒸馏出的复盘要点。

    该功能已整体下线：经验蒸馏并注入提示词的做法会把历史个例当作市场事实，
    反而污染分析。模型与表保留只为不破坏既有数据（不做破坏性迁移），
    代码中已无任何读写方；不要重新接入提示词。
    """

    __tablename__ = "analysis_lessons"

    id = Column(String(36), primary_key=True, index=True)
    user_id = Column(String(64), index=True, nullable=True)
    symbol = Column(String(20), index=True, nullable=True)
    lesson_text = Column(Text, nullable=False)
    source = Column(String(32), nullable=False, default="t1_incorrect")
    source_report_id = Column(String(36), nullable=True, index=True)
    direction_bucket = Column(String(16), nullable=True)
    signal_trade_date = Column(String(10), nullable=True)
    return_t1_pct = Column(Float, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)


class BacktestJobDB(Base):
    __tablename__ = "backtest_jobs"

    job_id = Column(String(36), primary_key=True, index=True)
    user_id = Column(String(64), index=True, nullable=True)
    symbol = Column(String(20), index=True, nullable=False)
    start_date = Column(String(10), nullable=False)
    end_date = Column(String(10), nullable=False)
    selected_analysts_json = Column(JSON, nullable=True)
    hold_days = Column(Integer, nullable=False, default=5)
    sample_interval = Column(Integer, nullable=False, default=7)
    status = Column(String(20), nullable=False, index=True)
    total_dates = Column(Integer, nullable=False, default=0)
    completed_dates = Column(Integer, nullable=False, default=0)
    records_json = Column(JSON, nullable=True)
    stats_json = Column(JSON, nullable=True)
    config_snapshot_json = Column(JSON, nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class StrategyFeedbackStatDB(Base):
    __tablename__ = "strategy_feedback_stats"

    id = Column(String(36), primary_key=True, index=True)
    user_id = Column(String(64), index=True, nullable=False)
    strategy_key = Column(String(64), index=True, nullable=False)
    factor_bucket = Column(String(32), nullable=False)
    sample_count = Column(Integer, nullable=False, default=0)
    win_rate = Column(Float, nullable=True)
    avg_return_pct = Column(Float, nullable=True)
    avg_score = Column(Float, nullable=True)
    weight_delta = Column(Float, nullable=True)
    last_entry_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint('user_id', 'strategy_key', name='uq_strategy_feedback_user_strategy'),
    )


class RecommendationEvalRunDB(Base):
    __tablename__ = "recommendation_eval_runs"

    id = Column(String(36), primary_key=True, index=True)
    user_id = Column(String(64), index=True, nullable=False)
    status = Column(String(20), nullable=False, default="completed", index=True)
    market = Column(String(8), nullable=False, default="cn")
    source_mode = Column(String(32), nullable=False, default="market_scan")
    baseline_profile = Column(String(64), nullable=False, default="ashare_balanced")
    variant_profile = Column(String(64), nullable=False, default="ashare_aggressive")
    lookback_days = Column(Integer, nullable=False, default=60)
    top_k = Column(Integer, nullable=False, default=5)
    benchmark_symbol = Column(String(20), nullable=False, default="000300.SH")
    summary_json = Column(JSON, nullable=True)
    gate_json = Column(JSON, nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    evaluated_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class RecommendationEvalItemDB(Base):
    __tablename__ = "recommendation_eval_items"

    id = Column(String(36), primary_key=True, index=True)
    run_id = Column(String(36), index=True, nullable=False)
    user_id = Column(String(64), index=True, nullable=False)
    group_tag = Column(String(16), nullable=False, index=True)  # baseline | variant
    bucket_key = Column(String(64), nullable=False, index=True)  # run_id/day
    symbol = Column(String(20), nullable=False, index=True)
    rank = Column(Integer, nullable=True)
    score = Column(Float, nullable=True)
    signal_date = Column(String(10), nullable=True, index=True)
    hold_days = Column(Integer, nullable=True)
    return_pct = Column(Float, nullable=True)
    benchmark_return_pct = Column(Float, nullable=True)
    excess_return_pct = Column(Float, nullable=True)
    max_drawdown_pct = Column(Float, nullable=True)
    hit = Column(Boolean, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)

    __table_args__ = (
        UniqueConstraint('run_id', 'group_tag', 'bucket_key', 'symbol', name='uq_rec_eval_item_dedup'),
    )


class PaperPortfolioDB(Base):
    __tablename__ = "paper_portfolios"

    id = Column(String(36), primary_key=True, index=True)
    user_id = Column(String(64), unique=True, index=True, nullable=False)
    initial_cash = Column(Float, nullable=False, default=1000000.0)
    cash_balance = Column(Float, nullable=False, default=1000000.0)
    total_realized_pnl = Column(Float, nullable=False, default=0.0)
    total_fees = Column(Float, nullable=False, default=0.0)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class PaperPositionDB(Base):
    __tablename__ = "paper_positions"

    id = Column(String(36), primary_key=True, index=True)
    portfolio_id = Column(String(36), index=True, nullable=False)
    symbol = Column(String(20), nullable=False)
    security_name = Column(String(80), nullable=True)
    quantity = Column(Float, nullable=False, default=0.0)
    avg_cost = Column(Float, nullable=False, default=0.0)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint('portfolio_id', 'symbol', name='uq_paper_positions_portfolio_symbol'),
    )


class PaperTradeDB(Base):
    __tablename__ = "paper_trades"

    id = Column(String(36), primary_key=True, index=True)
    portfolio_id = Column(String(36), index=True, nullable=False)
    user_id = Column(String(64), index=True, nullable=False)
    trade_date = Column(String(10), index=True, nullable=False)
    symbol = Column(String(20), index=True, nullable=False)
    side = Column(String(8), nullable=False)
    quantity = Column(Float, nullable=False)
    price = Column(Float, nullable=False)
    fee = Column(Float, nullable=False, default=0.0)
    gross_amount = Column(Float, nullable=False)
    realized_pnl = Column(Float, nullable=True)
    reason = Column(String(200), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)


class DailyReviewDB(Base):
    __tablename__ = "daily_reviews"

    id = Column(String(36), primary_key=True, index=True)
    user_id = Column(String(64), index=True, nullable=False)
    portfolio_id = Column(String(36), index=True, nullable=True)
    trade_date = Column(String(10), index=True, nullable=False)
    summary_json = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint('user_id', 'trade_date', name='uq_daily_reviews_user_trade_date'),
    )


class MainlineReportDB(Base):
    """市场主线报告（M4）：一次主线识别任务的完整结果。"""

    __tablename__ = "mainline_reports"

    id = Column(String(36), primary_key=True, index=True)   # run_id == job_id
    user_id = Column(String(64), index=True, nullable=True)
    trade_date = Column(String(10), index=True, nullable=False)
    perspective = Column(String(20), default="short", index=True)  # short | medium
    status = Column(String(20), default="pending", index=True)     # pending/running/completed/failed
    error = Column(Text, nullable=True)
    summary = Column(Text, nullable=True)                 # market_reading 一句话结论
    mainlines = Column(JSON, nullable=True)               # 主线 JSON 列表
    candidates = Column(JSON, nullable=True)              # 候选股 JSON 列表（冗余存储，便于列表页）
    analyst_report = Column(Text, nullable=True)          # 主线分析师全文
    selector_report = Column(Text, nullable=True)         # 选股师全文
    gated_out = Column(JSON, nullable=True)               # 置信度不足未选股的主线
    market_snapshot = Column(JSON, nullable=True)         # 市场池摘要（情绪/宽度/板块榜 top）
    warnings = Column(JSON, nullable=True)
    job_id = Column(String(36), index=True, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class MainlineCandidateDB(Base):
    """市场主线候选股（M4）：每条候选股一行，支持一键加入自选/发起分析。"""

    __tablename__ = "mainline_candidates"

    id = Column(String(36), primary_key=True, index=True)
    report_id = Column(String(36), index=True, nullable=False)
    user_id = Column(String(64), index=True, nullable=True)
    mainline = Column(String(100), nullable=True, index=True)
    symbol = Column(String(20), index=True, nullable=False)
    name = Column(String(50), nullable=True)
    tier = Column(String(20), nullable=True)              # 龙头 | 中军 | 补涨
    score = Column(Integer, nullable=True)                # 0-100
    reasons = Column(JSON, nullable=True)
    entry_hint = Column(Text, nullable=True)
    risk = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)


class MainlineT1OutcomeDB(Base):
    """市场主线 T+1 兑现跟踪（M6）：主线代表板块在报告日之后的表现评估。"""

    __tablename__ = "mainline_t1_outcomes"

    id = Column(String(36), primary_key=True, index=True)
    report_id = Column(String(36), index=True, nullable=False)
    user_id = Column(String(64), index=True, nullable=True)
    mainline = Column(String(100), nullable=True)
    trade_date = Column(String(10), index=True, nullable=False)
    check_date = Column(String(10), nullable=True)        # 实际评估所用最新交易日
    board = Column(String(100), nullable=True)
    board_fwd_ret = Column(Float, nullable=True)          # 板块前瞻收益
    benchmark_fwd_ret = Column(Float, nullable=True)      # 基准同期收益
    excess_ret = Column(Float, nullable=True)             # 超额收益
    outcome = Column(String(20), nullable=True, index=True)  # 兑现/部分兑现/走平/证伪/数据不足
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)

    __table_args__ = (
        UniqueConstraint('report_id', 'mainline', name='uq_mainline_t1_report_mainline'),
    )




class MainlineHistoryDB(Base):
    """主线周期档案（C2）：跨天识别同一主线，累积每日快照与周期判定。"""

    __tablename__ = "mainline_history"

    id = Column(String(36), primary_key=True, index=True)
    user_id = Column(String(64), index=True, nullable=True)
    mainline_key = Column(String(100), index=True, nullable=False)   # 归一化主线名
    representative_boards = Column(JSON, nullable=True)              # 代表板块（身份匹配用）
    first_date = Column(String(10), index=True, nullable=False)
    last_date = Column(String(10), index=True, nullable=True)
    status = Column(String(20), default="active", index=True)        # active | ended
    cycle_position = Column(String(20), nullable=True)               # 发酵|主升|高位分歧|退潮|已终结|数据不足
    cycle_reason = Column(Text, nullable=True)
    progress = Column(Float, nullable=True)                          # 生命周期进度 0-100
    peak_strength = Column(Float, nullable=True)
    peak_gap = Column(Float, nullable=True)
    alerts = Column(JSON, nullable=True)                             # 退潮预警信号
    action = Column(String(20), nullable=True)                       # 布局|持有|减仓|规避|观察
    position_pct = Column(Float, nullable=True)                      # 建议仓位 %
    daily_track = Column(JSON, nullable=True)                        # [{date, strength, heat, ...}]
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint('user_id', 'mainline_key', name='uq_mainline_history_user_key'),
    )


class MainlineDecisionDB(Base):
    """主线决策卡（C2）：每次周期判定的动作/仓位/验证条件与事后结果（能力曲线数据源）。"""

    __tablename__ = "mainline_decisions"

    id = Column(String(36), primary_key=True, index=True)
    user_id = Column(String(64), index=True, nullable=True)
    report_id = Column(String(36), index=True, nullable=True)
    mainline_key = Column(String(100), index=True, nullable=False)
    trade_date = Column(String(10), index=True, nullable=False)
    stage = Column(String(20), nullable=True)
    action = Column(String(20), nullable=True)
    position_pct = Column(Float, nullable=True)
    reason = Column(Text, nullable=True)
    verify_conditions = Column(JSON, nullable=True)
    emotion_temperature = Column(Integer, nullable=True)
    # 事后验证（能力曲线）
    outcome = Column(String(20), nullable=True)   # verified | falsified | pending
    outcome_note = Column(Text, nullable=True)
    forward_excess_ret = Column(Float, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class MainlineTradeCandidateDB(Base):
    """主线可买入标的 + 自动深挖状态（C3）。"""

    __tablename__ = "mainline_trade_candidates"

    id = Column(String(36), primary_key=True, index=True)
    user_id = Column(String(64), index=True, nullable=True)
    report_id = Column(String(36), index=True, nullable=True)
    mainline_key = Column(String(100), index=True, nullable=False)
    symbol = Column(String(20), index=True, nullable=False)
    name = Column(String(50), nullable=True)
    tier = Column(String(20), nullable=True)
    score = Column(Integer, nullable=True)
    buyable = Column(Boolean, default=False, nullable=False)
    timing = Column(Text, nullable=True)                       # 介入时机
    position_tier = Column(String(20), nullable=True)          # 重仓|标准|轻仓|观察
    deep_dive_job_id = Column(String(36), index=True, nullable=True)
    deep_dive_status = Column(String(20), default="none")      # none|queued|running|completed|failed
    deep_dive_report_id = Column(String(36), nullable=True)
    deep_dive_error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint('report_id', 'symbol', name='uq_mainline_trade_report_symbol'),
    )


class TradePlanDB(Base):
    """把研报里"已经算出来但只存成自由文本"的交易计划变成一等对象。

    背景：模型每天其实已经产出总仓上限 / 首仓 / 硬止损 / 分批止盈 / 时间止损 /
    逻辑失效条件，但过去只以散文形式落在 `trader_investment_plan` 里，
    止盈锚点在结构化抽取时丢失约 86%，既无法监控、也无法预警、更无法复盘。

    本表是"计划"的唯一事实来源：监控、预警、卖出建议、归因全部读它。
    一个研报对应一条计划（report_id 唯一）。
    """

    __tablename__ = "trade_plans"

    id = Column(String(36), primary_key=True, index=True)
    user_id = Column(String(64), index=True, nullable=True)
    report_id = Column(String(36), index=True, nullable=False)
    symbol = Column(String(20), index=True, nullable=False)
    name = Column(String(80), nullable=True)
    signal_trade_date = Column(String(10), nullable=True)
    horizon = Column(String(16), nullable=True)                 # short|medium|dual
    horizon_days = Column(Integer, nullable=True)               # 计划持有期（自然日）

    direction = Column(String(16), nullable=True)               # BUY / SELL / HOLD

    entry_low = Column(Float, nullable=True)                    # 入场区间下沿
    entry_high = Column(Float, nullable=True)                   # 入场区间上沿
    position_cap_pct = Column(Float, nullable=True)             # 总仓上限 %
    first_tranche_pct = Column(Float, nullable=True)            # 首仓 %

    hard_stop_price = Column(Float, nullable=True)              # 硬止损价
    take_profit_ladder_json = Column(JSON, nullable=True)       # [[price, reduce_pct], ...]
    target_price = Column(Float, nullable=True)                 # 单一目标价（结构化抽取回退，无分档）
    trailing_stop_pct = Column(Float, nullable=True)            # 移动止盈回撤触发 %
    time_stop_days = Column(Integer, nullable=True)             # 时间止损（交易日）

    invalidation_conditions_json = Column(JSON, nullable=True)  # 逻辑失效条件（自由文本列表）
    de_risk_triggers_json = Column(JSON, nullable=True)         # 来自 risk_feedback_state
    execution_preconditions_json = Column(JSON, nullable=True)  # 来自 risk_feedback_state
    hard_constraints_json = Column(JSON, nullable=True)         # 来自 risk_feedback_state

    risk_gate = Column(String(16), nullable=True)               # pass / revise / reject / empty
    confidence = Column(Float, nullable=True)

    status = Column(String(20), default="active", nullable=False, index=True)
    # active | expired | filled | invalidated | superseded
    status_reason = Column(Text, nullable=True)

    source = Column(String(32), default="trade_plan_block", nullable=False)
    # trade_plan_block = 模型机读块解析；extracted = LLM 抽取兜底；derived = 由已有字段推导
    parse_warnings_json = Column(JSON, nullable=True)
    raw_text = Column(Text, nullable=True)                      # 原始机读块，便于审计与回溯解析

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint('report_id', name='uq_trade_plans_report_id'),
    )


class TradeLedgerDB(Base):
    """真实成交流水（交易台账），替代"静态持仓快照"作为盈亏归因的事实来源。

    导入持仓表 `imported_portfolio_positions` 只是一张会被整体覆盖的快照：
    它无法还原"哪一笔买入赚了钱、哪一笔止损了"，也无法计算 T+1 可卖数量。
    台账按笔记录买卖，才支撑得起：摊薄成本、已实现盈亏、卖出原因归因。
    """

    __tablename__ = "trade_ledger"

    id = Column(String(36), primary_key=True, index=True)
    user_id = Column(String(64), index=True, nullable=False)
    symbol = Column(String(20), index=True, nullable=False)
    name = Column(String(80), nullable=True)
    trade_date = Column(String(10), nullable=False, index=True)
    action = Column(String(8), nullable=False)                  # BUY / SELL

    price = Column(Float, nullable=False)
    shares = Column(Float, nullable=False)
    amount = Column(Float, nullable=True)                       # price * shares（成交额）
    fee = Column(Float, nullable=True)                          # 佣金等
    stamp_tax = Column(Float, nullable=True)                    # 印花税（仅卖出）
    net_amount = Column(Float, nullable=True)                   # 实际发生额（含费用）
    slippage_pct = Column(Float, nullable=True)

    position_after = Column(Float, nullable=True)               # 该笔之后总持仓
    available_after = Column(Float, nullable=True)              # 该笔之后 T+1 可卖数量
    average_cost_after = Column(Float, nullable=True)           # 摊薄后成本
    realized_pnl = Column(Float, nullable=True)                 # 该笔已实现盈亏（卖出时）

    # 卖出原因分类学：stop_loss | take_profit | invalidation | time_stop |
    # trailing_stop | rebalance | manual | unknown
    sell_reason = Column(String(32), nullable=True, index=True)
    sell_reason_note = Column(Text, nullable=True)

    report_id = Column(String(36), nullable=True, index=True)   # 依据的研报
    plan_id = Column(String(36), nullable=True, index=True)     # 依据的交易计划
    source = Column(String(24), default="manual", nullable=False)  # manual|import|paper|suggested
    note = Column(Text, nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)

    __table_args__ = (
        UniqueConstraint('user_id', 'symbol', 'trade_date', 'action', 'price', 'shares',
                         name='uq_trade_ledger_dedup'),
    )


# ---------------------------------------------------------------------------
# 对话式研判：会话、消息、判定、判定结果
#
# 判定（signal_verdicts）刻意是**一等实体**而不是聊天消息里的一个 JSON 字段：
# T+3/T+5 校准闭环要按 (symbol, as_of, signal_version) 检索并回填结果，埋在
# 消息里就无法索引、无法去重、无法按维度统计判别力。
# ---------------------------------------------------------------------------


class ChatConversationDB(Base):
    """一次对话会话。"""

    __tablename__ = "chat_conversations"

    id = Column(String(36), primary_key=True, index=True)
    user_id = Column(String(64), index=True, nullable=False)
    title = Column(String(200), default="", nullable=False)
    primary_symbol = Column(String(32), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class ChatTurnTraceDB(Base):
    """一轮对话的**完整路径**：计划、每步执行、校验结论、终态。

    为什么单独一张表而不是塞进 `chat_messages`：消息表是给用户看的内容，
    trace 是给**排查与评测**看的证据。混在一起会让消息表长成几十列宽表，
    且用户删会话时不该顺手毁掉度量数据。

    为什么存 plan 与 steps 而不是只存 tool_calls：`tool_calls` 只有名字，
    无法回答"这一步为什么慢""哪个参数导致空结果""模型看到几张证据"。
    而 Phase 0 的全部意义就是让这些问题可被回答。
    """

    __tablename__ = "chat_turn_traces"

    id = Column(String(36), primary_key=True, index=True)
    conversation_id = Column(String(36), index=True, nullable=False)
    user_id = Column(String(64), index=True, nullable=False)
    # 本轮的用户问句：评测要按问句比对期望（黄金集跑分依赖它）
    text = Column(Text, default="", nullable=False)
    tier = Column(String(16), default="", nullable=False)
    status = Column(String(16), default="", nullable=False)
    # 计划：{tier, symbol, symbols, screen, planned:[{tool,args,reason}]}
    plan = Column(JSON, nullable=True)
    # 每步：{tool, arguments, ok, elapsed_ms, evidence_count, detail}
    steps = Column(JSON, nullable=True)
    tool_calls = Column(JSON, nullable=True)
    tokens = Column(Integer, default=0, nullable=False)
    model_calls = Column(Integer, default=0, nullable=False)
    model = Column(String(120), default="", nullable=False)
    latency_ms = Column(Integer, default=0, nullable=False)
    rejected_claims = Column(Integer, default=0, nullable=False)
    weak_verification = Column(Boolean, default=False, nullable=False)
    verdict_id = Column(String(36), nullable=True)
    unavailable = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class ChatConversationStateDB(Base):
    """一个会话的**跨轮状态**：槽位、证据账本、未覆盖项、待确认提议、任务号。

    为什么必须落库而不是只放内存：多轮的全部意义是"下一轮记得上一轮"。进程重启
    或多副本部署后内存态就没了，而用户看到的是"它"突然不知道该指谁、上一轮声明
    的未覆盖项凭空消失。

    为什么整块塞 JSON 而不是拆成列：状态是**引擎层的值对象**
    （`tradingagents/agent/state.py`），带版本号（`payload["version"]`），演进时
    只加字段不改表。拆成列会让每次加一个槽位都要迁移，而这里没有任何按字段查询
    的需求（只按 conversation_id + user_id 取整条）。

    `user_id` 与 `conversation_id` 是**双列**：查询必须两者都匹配，否则一个会话 id
    猜错就能读到别人的槽位与证据——与消息表同一条防串号纪律。
    """

    __tablename__ = "chat_conversation_states"

    conversation_id = Column(String(36), primary_key=True, index=True)
    user_id = Column(String(64), index=True, nullable=False)
    payload = Column(JSON, nullable=True)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class ChatMessageDB(Base):
    """会话内的一条消息。

    `tool_calls` 只存摘要（工具名），不存完整工具返回：单轮完整返回可达
    10-50KB，按 1000 轮/天估算会把年增量从 ~1.5GB 推到 ~18GB。结构化判定
    已在 signal_verdicts 里，原始数据按需重算比存旧快照更正确（可取到修正后数据）。
    """

    __tablename__ = "chat_messages"

    id = Column(String(36), primary_key=True, index=True)
    conversation_id = Column(String(36), index=True, nullable=False)
    user_id = Column(String(64), index=True, nullable=False)
    role = Column(String(16), nullable=False)
    content = Column(Text, default="", nullable=False)
    tier = Column(String(16), default="", nullable=False)
    model = Column(String(120), default="", nullable=False)
    tokens = Column(Integer, default=0, nullable=False)
    latency_ms = Column(Integer, default=0, nullable=False)
    status = Column(String(16), default="completed", nullable=False)
    verdict_id = Column(String(36), nullable=True)
    tool_calls = Column(JSON, nullable=True)
    weak_verification = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class SignalVerdictDB(Base):
    """一次确定性探针判定的完整快照（含权重与观测，供校准重放）。"""

    __tablename__ = "signal_verdicts"

    id = Column(String(36), primary_key=True, index=True)
    user_id = Column(String(64), index=True, nullable=False)
    symbol = Column(String(32), index=True, nullable=False)
    as_of = Column(String(10), index=True, nullable=False)
    probe = Column(String(32), index=True, nullable=False)
    state = Column(String(32), index=True, nullable=False)
    confidence = Column(Integer, default=0, nullable=False)
    margin = Column(Float, default=0.0, nullable=False)
    contributions = Column(JSON, nullable=True)
    modulator = Column(JSON, nullable=True)
    falsifiers = Column(JSON, nullable=True)
    unavailable = Column(JSON, nullable=True)
    daily_only = Column(Boolean, default=False, nullable=False)
    high_confidence = Column(Boolean, default=False, nullable=False)
    signal_version = Column(String(64), index=True, default="", nullable=False)
    conversation_id = Column(String(36), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        # 唯一约束必须包含 user_id：判定是 (symbol, as_of, version) 的确定性函数，
        # 但**归属**属于发起它的用户。若跨用户共享一行，第二个用户的
        # list_for_user 就查不到自己的判定——用户隔离语义在数据层就破裂了。
        # 统计口径上的重复由 calibration_report 按 (symbol, as_of, probe, version)
        # 去重解决，而不是靠约束。
        UniqueConstraint(
            "user_id", "symbol", "as_of", "probe", "signal_version", name="uq_signal_verdict"
        ),
    )


class SignalVerdictOutcomeDB(Base):
    """判定在某个持有期上的实际结果。停牌/退市记 evaluable=False，不计命中。"""

    __tablename__ = "signal_verdict_outcomes"

    id = Column(String(36), primary_key=True, index=True)
    verdict_id = Column(String(36), index=True, nullable=False)
    horizon_days = Column(Integer, nullable=False)
    ref_date = Column(String(10), nullable=False)
    max_high = Column(Float, nullable=True)
    close = Column(Float, nullable=True)
    ret_pct = Column(Float, nullable=True)
    threshold_hit = Column(Boolean, default=False, nullable=False)
    evaluable = Column(Boolean, default=True, nullable=False)
    reason = Column(String(200), nullable=True)
    evaluated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("verdict_id", "horizon_days", name="uq_signal_verdict_outcome"),
    )


class SignalWeightVersionDB(Base):
    """一次权重版本激活记录（审计 D2 修复）。

    修复前只有 `check_activation_gate`（拒绝逻辑）与 `with_weight`（纯函数），
    **没有任何机制产生并持久化新版本**——于是「权重被数据校准」无处落地，
    也无法做版本对照。

    刻意把 `sample_size` 与 `rationale` 设为必填：一次没有样本量、没有理由的
    权重变更，等于把「拍脑袋」制度化。旧版本永久保留，用于同标的同日期的对照。
    """

    __tablename__ = "signal_weight_versions"

    version = Column(String(64), primary_key=True, index=True)
    weights = Column(JSON, nullable=False)
    rationale = Column(Text, nullable=False)
    sample_size = Column(Integer, nullable=False)
    per_dimension = Column(JSON, nullable=True)
    activated_by = Column(String(64), default="", nullable=False)
    is_active = Column(Boolean, default=False, nullable=False, index=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class DailyBarDB(Base):
    """本地日线库：判定不再逐次打外网，改为"查库、缺了再补网"。

    为什么需要这张表：日线取数走的是 provider 降级链（东财 → 新浪 → 腾讯），
    而东财系接口存在 **IP 级封禁**（实测 `RemoteDisconnected`）。每一次判定都
    实时打一次外网，等于持续把自己推向封禁——一旦三家全挂，判定就没有日线可用。
    把已取到的日线留在本地，补网频率就从"每次请求"降到"每只每天一次"。

    为什么 `(symbol, date)` 必须唯一：盘后定时采集是**每天重跑**的，而且经常
    回补同一段历史。没有唯一约束时，"采两次"= "库里两份日线"，而信号层是按
    时间取尾部窗口的（`_normalize_daily` 的 `tail(window_days)`），重复行不会
    报错，只会让窗口悄悄少覆盖一半时间——数值上完全看不出来。
    """

    __tablename__ = "daily_bars"

    id = Column(String(36), primary_key=True, index=True)
    # 规范形式是带市场后缀的 `600519.SH`（见 daily_bar_store.canonical_symbol）：
    # 读取侧与采集侧若各用一种写法，本地库会永远查不中且不报错。
    symbol = Column(String(16), index=True, nullable=False)
    date = Column(String(10), index=True, nullable=False)  # YYYY-MM-DD
    open = Column(Float, nullable=True)
    high = Column(Float, nullable=True)
    low = Column(Float, nullable=True)
    close = Column(Float, nullable=True)
    volume = Column(Float, nullable=True)
    amount = Column(Float, nullable=True)  # 成交额
    turnover = Column(Float, nullable=True)  # 换手率（百分数）
    # 数据来自哪个源（provider/sina/tencent/local）。留这一列是为了让"某个源在
    # 退化"可被事后追溯，而不是只能看日志。
    source = Column(String(32), nullable=False)
    ingested_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("symbol", "date", name="uq_daily_bar_symbol_date"),
    )


# --------------------------------------------------------------------------
# 数据源探测（data-source probe）
#
# 为什么落库而不是只打日志：本次事故里"Baostock 从某天起不可用"这件事**没有任何
# 地方能看出来**——链上的静默降级把故障藏了几个月，直到人工实测才发现上游主机名
# 早就迁移了。有了这两张表，页面才能回答"这个源是什么时候坏的、坏了几天、期间
# 用的什么版本"，而不是只能 grep 日志。
#
# 两张表分工：`runs` 是一次探测的汇总（画趋势），`results` 是每条 源×维度 的明细
# （看现状）。分开是因为趋势只关心计数，明细要保留错误类型/版本指纹。
# --------------------------------------------------------------------------


class DataSourceProbeRunDB(Base):
    """一次数据源探测的汇总。"""

    __tablename__ = "data_source_probe_runs"

    id = Column(String(36), primary_key=True, index=True)
    started_at = Column(DateTime, index=True)
    finished_at = Column(DateTime, nullable=True)
    #: ok / degraded / failed —— 整轮的健康度（有任一 failed 即 failed）
    status = Column(String(16), index=True)
    #: scheduled（交易日定时）/ manual（页面上点按钮）
    trigger = Column(String(16), default="scheduled")
    #: 探测时刻是否为 A 股交易日。非交易日的"空数据"是正常的，页面要区分显示。
    trading_day = Column(Boolean, default=True)
    total_count = Column(Integer, default=0)
    ok_count = Column(Integer, default=0)
    degraded_count = Column(Integer, default=0)
    #: 硬截止超时的条数。**与 failed 分开**：超时可能只是"源很慢"，
    #: 处置方式（调窗口/加缓存）与"源坏了"（去修）完全不同。
    timeout_count = Column(Integer, default=0)
    failed_count = Column(Integer, default=0)
    unsupported_count = Column(Integer, default=0)
    duration_ms = Column(Integer, default=0)
    #: 本轮被跳过的原因（例如非交易日跳过、全部源被禁用）
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class DataSourceProbeResultDB(Base):
    """一条「数据源 × 能力维度」的探测明细。"""

    __tablename__ = "data_source_probe_results"

    id = Column(String(36), primary_key=True, index=True)
    run_id = Column(String(36), index=True, nullable=False)
    source = Column(String(64), index=True, nullable=False)
    #: provider 方法名（与能力矩阵的 tool 对齐）
    tool = Column(String(64), index=True, nullable=False)
    label = Column(String(128), nullable=True)
    category = Column(String(64), nullable=True)
    #: ok / degraded / timeout / failed / unsupported
    status = Column(String(16), index=True, nullable=False)
    latency_ms = Column(Integer, nullable=True)
    #: 返回规模。含义由 `row_unit` 决定（rows=真实记录数；lines=文本非空行数）
    row_count = Column(Integer, nullable=True)
    #: `rows` / `lines`——不区分会让"N 行"被误读成"N 条记录"：
    #: 预格式化文本产物（板块历史、个股行情摘要）的行数并不是记录数。
    row_unit = Column(String(8), nullable=True)
    #: **库版本指纹**——本次事故的核心变量就是版本漂移，不记录就永远查不出来
    version = Column(String(32), nullable=True)
    #: 错误语义（unavailable / empty / timeout / error），与 error_type 分开：
    #: 前者是"该做什么"，后者是"哪个异常"
    error_kind = Column(String(32), nullable=True)
    error_type = Column(String(64), nullable=True)
    error_message = Column(Text, nullable=True)
    checked_at = Column(DateTime, index=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
