"""Database configuration and session management."""

import os
from datetime import datetime, timezone
from typing import Generator

from sqlalchemy import create_engine, Column, String, DateTime, Text, Integer, Float, JSON
from sqlalchemy.orm import declarative_base, sessionmaker, Session

# Database URL - default to SQLite for simplicity
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./tradingagents.db")

# Create engine
if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False},
        echo=False,
    )
else:
    engine = create_engine(DATABASE_URL, echo=False)

# Session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Base class for models
Base = declarative_base()


def get_db() -> Generator[Session, None, None]:
    """Get database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Initialize database tables."""
    Base.metadata.create_all(bind=engine)


# Report Model
class ReportDB(Base):
    """Report database model."""
    
    __tablename__ = "reports"
    
    id = Column(String(36), primary_key=True, index=True)
    user_id = Column(String(64), index=True, nullable=True)  # For future multi-user support
    symbol = Column(String(20), index=True, nullable=False)
    trade_date = Column(String(10), nullable=False)
    
    # Decision info
    decision = Column(String(50), nullable=True)  # BUY, SELL, HOLD, etc.
    confidence = Column(Integer, nullable=True)  # 0-100
    target_price = Column(Float, nullable=True)
    stop_loss_price = Column(Float, nullable=True)
    
    # Full analysis results stored as JSON
    result_data = Column(JSON, nullable=True)
    
    # Individual reports (for quick access)
    market_report = Column(Text, nullable=True)
    sentiment_report = Column(Text, nullable=True)
    news_report = Column(Text, nullable=True)
    fundamentals_report = Column(Text, nullable=True)
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
            "confidence": self.confidence,
            "target_price": self.target_price,
            "stop_loss_price": self.stop_loss_price,
            "result_data": self.result_data,
            "market_report": self.market_report,
            "sentiment_report": self.sentiment_report,
            "news_report": self.news_report,
            "fundamentals_report": self.fundamentals_report,
            "investment_plan": self.investment_plan,
            "trader_investment_plan": self.trader_investment_plan,
            "final_trade_decision": self.final_trade_decision,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
