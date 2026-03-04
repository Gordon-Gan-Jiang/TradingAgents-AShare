"""Report service for database operations."""

import json
import re
from datetime import datetime
from typing import List, Optional, Dict, Any
from uuid import uuid4

from sqlalchemy.orm import Session
from api.database import ReportDB


def extract_confidence_from_decision(decision_text: Optional[str]) -> Optional[int]:
    """Extract confidence percentage from decision text."""
    if not decision_text:
        return None
    # Look for patterns like "置信度: 78%" or "confidence: 78%"
    match = re.search(r'置信度[:：]\s*(\d+)%', decision_text)
    if match:
        return int(match.group(1))
    match = re.search(r'confidence[:：]\s*(\d+)%', decision_text, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


def extract_price_from_text(text: Optional[str], price_type: str = "target") -> Optional[float]:
    """Extract target or stop-loss price from text."""
    if not text:
        return None
    
    if price_type == "target":
        # Look for 目标价, target price
        patterns = [
            r'目标价[:：]\s*[¥$]?\s*(\d+\.?\d*)',
            r'target[:：]\s*[¥$]?\s*(\d+\.?\d*)',
            r'目标价格[:：]\s*[¥$]?\s*(\d+\.?\d*)',
        ]
    else:
        # Look for 止损价, stop loss
        patterns = [
            r'止损价[:：]\s*[¥$]?\s*(\d+\.?\d*)',
            r'stop[-\s]?loss[:：]\s*[¥$]?\s*(\d+\.?\d*)',
            r'止损价格[:：]\s*[¥$]?\s*(\d+\.?\d*)',
        ]
    
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return float(match.group(1))
    return None


def create_report(
    db: Session,
    symbol: str,
    trade_date: str,
    decision: Optional[str] = None,
    result_data: Optional[Dict[str, Any]] = None,
    user_id: Optional[str] = None,
) -> ReportDB:
    """Create a new report."""
    
    report_id = str(uuid4())
    
    # Extract individual reports from result_data
    market_report = None
    sentiment_report = None
    news_report = None
    fundamentals_report = None
    investment_plan = None
    trader_investment_plan = None
    final_trade_decision = None
    
    if result_data:
        market_report = result_data.get("market_report")
        sentiment_report = result_data.get("sentiment_report")
        news_report = result_data.get("news_report")
        fundamentals_report = result_data.get("fundamentals_report")
        investment_plan = result_data.get("investment_plan")
        trader_investment_plan = result_data.get("trader_investment_plan")
        final_trade_decision = result_data.get("final_trade_decision")
    
    # Extract confidence and prices
    confidence = None
    target_price = None
    stop_loss_price = None
    
    if final_trade_decision:
        confidence = extract_confidence_from_decision(final_trade_decision)
        target_price = extract_price_from_text(final_trade_decision, "target")
        stop_loss_price = extract_price_from_text(final_trade_decision, "stop_loss")
    
    db_report = ReportDB(
        id=report_id,
        user_id=user_id,
        symbol=symbol,
        trade_date=trade_date,
        decision=decision,
        confidence=confidence,
        target_price=target_price,
        stop_loss_price=stop_loss_price,
        result_data=result_data,
        market_report=market_report,
        sentiment_report=sentiment_report,
        news_report=news_report,
        fundamentals_report=fundamentals_report,
        investment_plan=investment_plan,
        trader_investment_plan=trader_investment_plan,
        final_trade_decision=final_trade_decision,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    
    db.add(db_report)
    db.commit()
    db.refresh(db_report)
    return db_report


def get_report(db: Session, report_id: str) -> Optional[ReportDB]:
    """Get a report by ID."""
    return db.query(ReportDB).filter(ReportDB.id == report_id).first()


def get_reports_by_user(
    db: Session,
    user_id: Optional[str] = None,
    symbol: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
) -> List[ReportDB]:
    """Get reports for a user with optional filtering."""
    query = db.query(ReportDB)
    
    if user_id:
        query = query.filter(ReportDB.user_id == user_id)
    
    if symbol:
        query = query.filter(ReportDB.symbol == symbol)
    
    return query.order_by(ReportDB.created_at.desc()).offset(skip).limit(limit).all()


def delete_report(db: Session, report_id: str) -> bool:
    """Delete a report."""
    report = db.query(ReportDB).filter(ReportDB.id == report_id).first()
    if report:
        db.delete(report)
        db.commit()
        return True
    return False


def update_report(
    db: Session,
    report_id: str,
    **kwargs
) -> Optional[ReportDB]:
    """Update a report."""
    report = db.query(ReportDB).filter(ReportDB.id == report_id).first()
    if not report:
        return None
    
    for key, value in kwargs.items():
        if hasattr(report, key):
            setattr(report, key, value)
    
    report.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(report)
    return report
