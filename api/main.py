from __future__ import annotations

import json
import os
import queue
import re
import traceback
from contextlib import asynccontextmanager
from io import StringIO
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta
from threading import Lock
from typing import Any, Dict, List, Literal, Optional
from uuid import uuid4

from pydantic import field_serializer

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
import pandas as pd

from api.database import init_db, get_db, SessionLocal
from api.services import report_service

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.dataflows.trade_calendar import cn_today_str
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.interface import route_to_vendor

load_dotenv()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize resources on startup."""
    init_db()
    print("Database initialized.")
    yield


app = FastAPI(title="TradingAgents-AShare API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5174",
        "http://localhost:5174",
        "http://127.0.0.1:5175",
        "http://localhost:5175",
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_executor = ThreadPoolExecutor(max_workers=2)
_jobs_lock = Lock()
_jobs: Dict[str, Dict[str, Any]] = {}
_job_events: Dict[str, "queue.Queue[Dict[str, Any]]"] = {}

FIXED_TEAMS = {
    "Analyst Team": [
        "Market Analyst",
        "Social Analyst",
        "News Analyst",
        "Fundamentals Analyst",
    ],
    "Research Team": ["Bull Researcher", "Bear Researcher", "Research Manager"],
    "Trading Team": ["Trader"],
    "Risk Management": ["Aggressive Analyst", "Neutral Analyst", "Conservative Analyst"],
    "Portfolio Management": ["Portfolio Manager"],
}
ANALYST_ORDER = ["market", "social", "news", "fundamentals"]
ANALYST_AGENT_NAMES = {
    "market": "Market Analyst",
    "social": "Social Analyst",
    "news": "News Analyst",
    "fundamentals": "Fundamentals Analyst",
}
ANALYST_REPORT_MAP = {
    "market": "market_report",
    "social": "sentiment_report",
    "news": "news_report",
    "fundamentals": "fundamentals_report",
}


class AnalyzeRequest(BaseModel):
    symbol: str = Field(..., description="股票代码，如 600519.SH")
    trade_date: str = Field(default_factory=cn_today_str, description="交易日期 YYYY-MM-DD")
    selected_analysts: List[str] = Field(
        default_factory=lambda: ["market", "social", "news", "fundamentals"]
    )
    config_overrides: Dict[str, Any] = Field(default_factory=dict)
    dry_run: bool = False


class AnalyzeResponse(BaseModel):
    job_id: str
    status: Literal["pending", "running", "completed", "failed"]
    created_at: str


class JobStatusResponse(BaseModel):
    job_id: str
    status: Literal["pending", "running", "completed", "failed"]
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    symbol: str
    trade_date: str
    error: Optional[str] = None


class ChatMessage(BaseModel):
    role: str
    content: Any


class ChatCompletionRequest(BaseModel):
    model: Optional[str] = "tradingagents-ashare"
    messages: List[ChatMessage]
    stream: bool = True
    selected_analysts: List[str] = Field(
        default_factory=lambda: ["market", "social", "news", "fundamentals"]
    )
    config_overrides: Dict[str, Any] = Field(default_factory=dict)
    dry_run: bool = False


class KlineResponse(BaseModel):
    symbol: str
    start_date: str
    end_date: str
    candles: List[Dict[str, Any]]


# Report API Models
class ReportCreateRequest(BaseModel):
    symbol: str = Field(..., description="股票代码")
    trade_date: str = Field(..., description="交易日期 YYYY-MM-DD")
    decision: Optional[str] = Field(None, description="交易决策")
    result_data: Optional[Dict[str, Any]] = Field(None, description="完整分析结果")


class ReportResponse(BaseModel):
    id: str
    user_id: Optional[str]
    symbol: str
    trade_date: str
    decision: Optional[str]
    confidence: Optional[int]
    target_price: Optional[float]
    stop_loss_price: Optional[float]
    created_at: Optional[str]
    updated_at: Optional[str]
    
    @field_serializer('created_at', 'updated_at')
    def serialize_datetime(self, value):
        if isinstance(value, datetime):
            return value.isoformat()
        return value
    
    class Config:
        from_attributes = True


class ReportDetailResponse(ReportResponse):
    market_report: Optional[str]
    sentiment_report: Optional[str]
    news_report: Optional[str]
    fundamentals_report: Optional[str]
    investment_plan: Optional[str]
    trader_investment_plan: Optional[str]
    final_trade_decision: Optional[str]
    result_data: Optional[Dict[str, Any]]


class ReportListResponse(BaseModel):
    total: int
    reports: List[ReportResponse]


def _deep_merge(base: Dict[str, Any], overrides: Dict[str, Any]) -> Dict[str, Any]:
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def _build_runtime_config(overrides: Dict[str, Any]) -> Dict[str, Any]:
    config = deepcopy(DEFAULT_CONFIG)

    # Env defaults (align with main.py behavior)
    config["llm_provider"] = os.getenv("LLM_PROVIDER", config["llm_provider"])
    config["backend_url"] = os.getenv("OPENAI_BASE_URL", config["backend_url"])
    config["quick_think_llm"] = os.getenv("QUICK_THINK_LLM", config["quick_think_llm"])
    config["deep_think_llm"] = os.getenv("DEEP_THINK_LLM", config["deep_think_llm"])
    config["max_debate_rounds"] = int(os.getenv("MAX_DEBATE_ROUNDS", "1"))
    config["max_risk_discuss_rounds"] = int(os.getenv("MAX_RISK_DISCUSS_ROUNDS", "1"))

    # Default CN-first provider chain
    config["data_vendors"] = {
        "core_stock_apis": "cn_akshare,cn_baostock,yfinance",
        "technical_indicators": "cn_akshare,cn_baostock,yfinance",
        "fundamental_data": "cn_akshare,cn_baostock,yfinance",
        "news_data": "cn_akshare,cn_baostock,yfinance",
    }

    if overrides:
        config = _deep_merge(config, overrides)
    return config


def _set_job(job_key: str, **kwargs) -> None:
    with _jobs_lock:
        if job_key not in _jobs:
            _jobs[job_key] = {}
        _jobs[job_key].update(kwargs)


def _ensure_job_event_queue(job_id: str) -> "queue.Queue[Dict[str, Any]]":
    with _jobs_lock:
        q = _job_events.get(job_id)
        if q is None:
            q = queue.Queue()
            _job_events[job_id] = q
        return q


def _emit_job_event(job_id: str, event: str, data: Dict[str, Any]) -> None:
    payload = {
        "event": event,
        "data": data,
        "timestamp": datetime.now().isoformat(),
    }
    _ensure_job_event_queue(job_id).put(payload)


def _build_result_payload(final_state: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "symbol": final_state.get("company_of_interest"),
        "trade_date": final_state.get("trade_date"),
        "market_report": final_state.get("market_report"),
        "sentiment_report": final_state.get("sentiment_report"),
        "news_report": final_state.get("news_report"),
        "fundamentals_report": final_state.get("fundamentals_report"),
        "investment_plan": final_state.get("investment_plan"),
        "trader_investment_plan": final_state.get("trader_investment_plan"),
        "final_trade_decision": final_state.get("final_trade_decision"),
    }


class AgentProgressTracker:
    # 阶段标题映射
    STAGE_TITLES = {
        "market_analysis": "市场分析完成",
        "sentiment_analysis": "舆情分析完成",
        "news_analysis": "新闻分析完成",
        "fundamentals_analysis": "基本面分析完成",
        "research_decision": "研究团队决策",
        "trader_plan": "交易计划制定",
        "risk_assessment": "风险评估完成",
        "final_decision": "最终决策",
    }
    
    def __init__(self, selected_analysts: List[str], job_id: str):
        self.job_id = job_id
        self.selected_analysts = [a.lower() for a in selected_analysts]
        self.status: Dict[str, str] = {}
        self.report_sections: Dict[str, Optional[str]] = {
            "market_report": None,
            "sentiment_report": None,
            "news_report": None,
            "fundamentals_report": None,
            "investment_plan": None,
            "trader_investment_plan": None,
            "final_trade_decision": None,
        }
        # 跟踪已完成的阶段，避免重复发送里程碑
        self._completed_stages: set = set()
        # 跟踪已发送的 writing 状态，避免重复发送
        self._writing_status_sent: set = set()
        
        for team_agents in FIXED_TEAMS.values():
            for agent in team_agents:
                self.status[agent] = "pending"

        # 未选中的分析师标记为 skipped（仍展示，便于固定 12-agent 看板）
        for key in ANALYST_ORDER:
            agent = ANALYST_AGENT_NAMES[key]
            if key not in self.selected_analysts:
                self.status[agent] = "skipped"

    def _emit_milestone(self, stage: str, summary: str = "") -> None:
        """发送用户可见的里程碑事件"""
        if stage in self._completed_stages:
            return
        self._completed_stages.add(stage)
        
        title = self.STAGE_TITLES.get(stage, stage)
        _emit_job_event(
            self.job_id,
            "agent.milestone",
            {
                "stage": stage,
                "title": title,
                "summary": summary,
                "timestamp": datetime.now().isoformat(),
            },
        )
        print(f"[Milestone] {title}: {summary[:100]}...")

    def _emit_report_chunked(self, job_id: str, section: str, content: str) -> None:
        """将报告内容分片发送，直接透传不做人工延迟
        
        按较大块分片（如按段落），让前端自然渲染
        """
        # 按段落分割，保持Markdown结构
        paragraphs = content.split('\n\n')
        
        for i, para in enumerate(paragraphs):
            if not para.strip():
                continue
                
            _emit_job_event(
                job_id,
                "agent.report.chunk",
                {
                    "section": section,
                    "chunk": para + '\n\n',
                    "index": i,
                    "is_complete": False,
                },
            )
        
        # 发送完成标记
        _emit_job_event(
            job_id,
            "agent.report.chunk",
            {
                "section": section,
                "chunk": "",
                "index": -1,
                "is_complete": True,
            },
        )

    def snapshot(self) -> Dict[str, Any]:
        agents = []
        for team, members in FIXED_TEAMS.items():
            for m in members:
                agents.append({"team": team, "agent": m, "status": self.status.get(m, "pending")})
        return {"agents": agents}

    def _set_status(self, agent: str, status: str) -> None:
        prev = self.status.get(agent)
        if prev == status:
            return
        self.status[agent] = status
        _emit_job_event(
            self.job_id,
            "agent.status",
            {"agent": agent, "status": status, "previous_status": prev},
        )

    def _update_research_team_status(self, status: str) -> None:
        for agent in ["Bull Researcher", "Bear Researcher", "Research Manager"]:
            self._set_status(agent, status)

    def _generate_stage_summary(self, stage: str, chunk: Dict[str, Any]) -> str:
        """根据阶段生成简要总结"""
        if stage == "market_analysis":
            report = chunk.get("market_report", "")
            # 提取关键信息
            if "支撑" in report or "压力" in report:
                return "技术面关键位已识别"
            return "技术面分析完成"
        elif stage == "sentiment_analysis":
            return "舆情数据已收集"
        elif stage == "news_analysis":
            return "新闻影响已评估"
        elif stage == "fundamentals_analysis":
            return "基本面指标已计算"
        elif stage == "research_decision":
            return "多空观点已形成"
        elif stage == "trader_plan":
            return "交易策略已制定"
        elif stage == "risk_assessment":
            return "风险水平已评估"
        elif stage == "final_decision":
            decision = chunk.get("final_trade_decision", "")
            return f"最终建议: {decision[:50]}..." if len(decision) > 50 else f"最终建议: {decision}"
        return ""

    def _emit_writing_status(self, agent_name: str, report_type: str) -> None:
        """发送正在编写报告的状态（每个agent只发送一次）"""
        # 检查是否已经发送过
        status_key = f"{agent_name}:{report_type}"
        if status_key in self._writing_status_sent:
            return
        self._writing_status_sent.add(status_key)
        
        report_names = {
            "market_report": "市场分析",
            "sentiment_report": "舆情分析",
            "news_report": "新闻分析",
            "fundamentals_report": "基本面分析",
            "investment_plan": "投资计划",
            "trader_investment_plan": "交易计划",
            "final_trade_decision": "最终交易决策",
        }
        _emit_job_event(
            self.job_id,
            "agent.writing",
            {
                "agent": agent_name,
                "report": report_type,
                "report_name": report_names.get(report_type, report_type),
                "status": "writing",
            },
        )

    def apply_chunk(self, chunk: Dict[str, Any]) -> None:
        # 分析师阶段状态推进
        found_active = False
        for analyst_key in ANALYST_ORDER:
            if analyst_key not in self.selected_analysts:
                continue

            agent_name = ANALYST_AGENT_NAMES[analyst_key]
            report_key = ANALYST_REPORT_MAP[analyst_key]
            has_report = bool(chunk.get(report_key))

            if has_report:
                if self.status.get(agent_name) != "completed":
                    self._set_status(agent_name, "completed")
                    self.report_sections[report_key] = chunk.get(report_key)
            elif not found_active:
                # 只在状态从 pending 变为 in_progress 时发送 writing 状态
                prev_status = self.status.get(agent_name)
                if prev_status != "in_progress":
                    self._set_status(agent_name, "in_progress")
                    # 发送正在分析的状态（只发送一次）
                    self._emit_writing_status(agent_name, report_key)
                found_active = True
            else:
                self._set_status(agent_name, "pending")

        if not found_active and self.selected_analysts:
            if self.status.get("Bull Researcher") == "pending":
                self._set_status("Bull Researcher", "in_progress")

        # 研究团队状态更新
        debate_state = chunk.get("investment_debate_state") or {}
        bull_hist = str(debate_state.get("bull_history", "")).strip()
        bear_hist = str(debate_state.get("bear_history", "")).strip()
        judge = str(debate_state.get("judge_decision", "")).strip()
        if bull_hist or bear_hist:
            self._update_research_team_status("in_progress")
        if judge:
            self._update_research_team_status("completed")
            if self.status.get("Trader") != "in_progress":
                self._set_status("Trader", "in_progress")
                self._emit_writing_status("Trader", "trader_investment_plan")

        # 交易团队
        if chunk.get("trader_investment_plan"):
            if self.status.get("Trader") != "completed":
                self._set_status("Trader", "completed")
                self._set_status("Aggressive Analyst", "in_progress")

        # 风控与组合团队（发送最终决策）
        risk_state = chunk.get("risk_debate_state") or {}
        risk_judge = str(risk_state.get("judge_decision", "")).strip()

        if risk_judge:
            if self.status.get("Portfolio Manager") != "completed":
                self._set_status("Portfolio Manager", "in_progress")
                self._set_status("Aggressive Analyst", "completed")
                self._set_status("Conservative Analyst", "completed")
                self._set_status("Neutral Analyst", "completed")
                self._set_status("Portfolio Manager", "completed")
                final_summary = self._generate_stage_summary("final_decision", chunk)
                self._emit_milestone("final_decision", final_summary)


def _extract_message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts).strip()
    return str(content)


def _generate_tool_description(tool_name: str, tool_args: Dict[str, Any]) -> str:
    """生成工具调用的可读描述"""
    if tool_name == "get_indicators":
        indicators = tool_args.get("indicators", [])
        if indicators:
            return f"计算 {', '.join(indicators[:3])}{' 等' if len(indicators) > 3 else ''} 指标"
        return "获取技术指标"
    elif tool_name == "get_stock_data":
        return "获取股票历史数据"
    elif tool_name == "get_fundamentals":
        metrics = tool_args.get("metrics", [])
        if metrics:
            return f"获取 {', '.join(metrics[:2])}{' 等' if len(metrics) > 2 else ''} 基本面数据"
        return "获取基本面数据"
    elif tool_name == "get_income_statement":
        return "获取利润表"
    elif tool_name == "get_balance_sheet":
        return "获取资产负债表"
    elif tool_name == "get_cash_flow":
        return "获取现金流量表"
    elif tool_name == "get_news":
        return "获取相关新闻"
    elif tool_name == "get_social_sentiment":
        return "获取舆情数据"
    return f"调用 {tool_name}"


def _run_job(job_id: str, request: AnalyzeRequest, stream_events: bool = False, save_report: bool = True) -> None:
    _set_job(job_id, status="running", started_at=datetime.now().isoformat())
    _emit_job_event(
        job_id,
        "job.running",
        {"job_id": job_id, "symbol": request.symbol, "trade_date": request.trade_date},
    )
    tracker = AgentProgressTracker(request.selected_analysts, job_id)
    _emit_job_event(job_id, "agent.snapshot", tracker.snapshot())
    try:
        config = _build_runtime_config(request.config_overrides)
        if request.dry_run:
            result = {
                "mode": "dry_run",
                "symbol": request.symbol,
                "trade_date": request.trade_date,
                "selected_analysts": request.selected_analysts,
                "llm_provider": config.get("llm_provider"),
                "data_vendors": config.get("data_vendors"),
            }
            _set_job(
                job_id,
                status="completed",
                result=result,
                decision="DRY_RUN",
                finished_at=datetime.now().isoformat(),
            )
            _emit_job_event(
                job_id,
                "job.completed",
                {"job_id": job_id, "decision": "DRY_RUN", "result": result},
            )
            return

        graph = TradingAgentsGraph(
            selected_analysts=request.selected_analysts,
            debug=False,
            config=config,
        )
        final_state: Optional[Dict[str, Any]] = None

        if stream_events:
            init_state = graph.propagator.create_initial_state(
                request.symbol, request.trade_date
            )
            args = graph.propagator.get_graph_args()
            report_keys = (
                "market_report",
                "sentiment_report",
                "news_report",
                "fundamentals_report",
                "investment_plan",
                "trader_investment_plan",
                "final_trade_decision",
            )
            last_report: Dict[str, str] = {}

            for chunk in graph.graph.stream(init_state, **args):
                final_state = chunk
                tracker.apply_chunk(chunk)
                messages = chunk.get("messages", [])
                if messages:
                    msg = messages[-1]
                    content = _extract_message_text(getattr(msg, "content", ""))
                    agent_name = getattr(msg, "name", None)
                    
                    # 服务器日志
                    if content:
                        print(f"[Agent Message] {agent_name}: {content[:200]}...")

                    # 发送工具调用到前端，让用户看到系统正在做什么
                    for tool_call in getattr(msg, "tool_calls", []) or []:
                        tool_name = tool_call.get("name", "unknown") if isinstance(tool_call, dict) else getattr(tool_call, "name", "unknown")
                        tool_args = tool_call.get("args", {}) if isinstance(tool_call, dict) else getattr(tool_call, "args", {})
                        print(f"[Tool Call] {agent_name}: {tool_name}")
                        
                        # 根据工具名推断是哪个Agent在调用
                        agent_display = agent_name
                        if not agent_display:
                            # 根据工具名推断Agent
                            tool_to_agent = {
                                "get_stock_data": "数据获取",
                                "get_indicators": "技术分析师",
                                "get_fundamentals": "基本面分析师",
                                "get_income_statement": "基本面分析师",
                                "get_balance_sheet": "基本面分析师",
                                "get_cash_flow": "基本面分析师",
                                "get_news": "新闻分析师",
                                "get_social_sentiment": "舆情分析师",
                            }
                            agent_display = tool_to_agent.get(tool_name, "系统")
                        
                        # 生成工具调用的描述（包含具体指标）
                        tool_description = _generate_tool_description(tool_name, tool_args)
                        
                        # 发送给用户可见的工具调用事件
                        _emit_job_event(
                            job_id,
                            "agent.tool_call",
                            {
                                "agent": agent_display,
                                "tool": tool_name,
                                "description": tool_description,
                            },
                        )

                for key in report_keys:
                    value = chunk.get(key)
                    if value and value != last_report.get(key):
                        last_report[key] = value
                        # 使用分片推送，支持打字机效果
                        tracker._emit_report_chunked(job_id, key, str(value))
        else:
            final_state, _ = graph.propagate(request.symbol, request.trade_date)

        if not final_state:
            raise RuntimeError("graph returned empty final state")

        decision = graph.process_signal(final_state["final_trade_decision"])
        result = _build_result_payload(final_state)

        _set_job(
            job_id,
            status="completed",
            result=result,
            decision=decision,
            finished_at=datetime.now().isoformat(),
        )
        # 全量收口为 completed/skipped
        for agent, status in tracker.status.items():
            if status not in ("completed", "skipped"):
                tracker._set_status(agent, "completed")
        
        # 自动保存报告到数据库
        if save_report:
            db = SessionLocal()
            try:
                report_service.create_report(
                    db=db,
                    symbol=request.symbol,
                    trade_date=request.trade_date,
                    decision=decision,
                    result_data=result,
                    user_id=None,  # TODO: 后续添加用户认证后传入 user_id
                )
            except Exception as e:
                print(f"Failed to save report: {e}")
            finally:
                db.close()
        
        _emit_job_event(
            job_id,
            "job.completed",
            {"job_id": job_id, "decision": decision, "result": result},
        )
    except Exception as exc:
        _set_job(
            job_id,
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
            traceback=traceback.format_exc(),
            finished_at=datetime.now().isoformat(),
        )
        _emit_job_event(
            job_id,
            "job.failed",
            {"job_id": job_id, "error": f"{type(exc).__name__}: {exc}"},
        )


def _normalize_symbol(raw: str) -> str:
    s = raw.strip().upper()
    # Priority: 6-digit CN stock code
    m = re.search(r"(\d{6})(?:\.(SH|SZ|SS))?", s)
    if m:
        code = m.group(1)
        suffix = m.group(2)
        if suffix:
            if suffix == "SS":
                return f"{code}.SH"
            return f"{code}.{suffix}"
        market = "SH" if code.startswith(("5", "6", "9")) else "SZ"
        return f"{code}.{market}"
    # Fallback: 1-6 letter ticker
    m2 = re.search(r"([A-Z]{1,6}(?:\.[A-Z]{1,3})?)", s)
    if m2:
        return m2.group(1)
    return s


def _extract_chat_text(messages: List[ChatMessage]) -> str:
    if not messages:
        return ""
    last = messages[-1]
    return _extract_message_text(last.content)


def _extract_symbol_and_date(text: str) -> tuple[Optional[str], Optional[str]]:
    # Date extraction (flexible boundaries)
    date_match = re.search(r"\d{4}-\d{2}-\d{2}", text)
    date = date_match.group(0) if date_match else None

    # Priority 1: A-Share 6-digit code (even if stuck to Chinese characters)
    sym_match = re.search(r"(\d{6}(?:\.(?:SH|SZ|SS))?)", text, re.IGNORECASE)
    if sym_match:
        return _normalize_symbol(sym_match.group(1)), date

    # Priority 2: US Stocks or other Tickers (use boundaries for letters to avoid partial words)
    us_match = re.search(r"\b([A-Z]{1,6}(?:\.[A-Z]{1,3})?)\b", text.upper())
    if us_match:
        return us_match.group(1), date

    return None, date


def _sse_pack(event: str, data: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _parse_stock_csv(raw: str) -> List[Dict[str, Any]]:
    if not raw:
        return []
    lines = [ln for ln in raw.splitlines() if ln.strip() and not ln.startswith("#")]
    if not lines:
        return []

    try:
        df = pd.read_csv(StringIO("\n".join(lines)))
    except Exception:
        return []

    if "Date" not in df.columns:
        return []

    rename_map = {k: k.strip() for k in df.columns}
    df = df.rename(columns=rename_map)
    required = ["Date", "Open", "High", "Low", "Close"]
    for col in required:
        if col not in df.columns:
            return []

    for col in ["Open", "High", "Low", "Close", "Volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df.dropna(subset=["Date", "Open", "High", "Low", "Close"]).sort_values("Date")
    if df.empty:
        return []

    candles: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        candles.append(
            {
                "date": row["Date"].strftime("%Y-%m-%d"),
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
                "volume": float(row["Volume"]) if "Volume" in df.columns and pd.notna(row.get("Volume")) else None,
            }
        )
    return candles


def _stream_job_events(job_id: str):
    q = _ensure_job_event_queue(job_id)
    yield _sse_pack("job.ready", {"job_id": job_id})
    while True:
        try:
            event = q.get(timeout=30)
            yield _sse_pack(event["event"], event["data"])
            if event["event"] in ("job.completed", "job.failed"):
                yield "event: done\ndata: [DONE]\n\n"
                break
        except queue.Empty:
            with _jobs_lock:
                status = _jobs.get(job_id, {}).get("status")
            if status in ("completed", "failed"):
                yield "event: done\ndata: [DONE]\n\n"
                break
            yield ": keep-alive\n\n"


@app.get("/healthz")
def healthz() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/market/kline", response_model=KlineResponse)
def get_kline(
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> KlineResponse:
    end = end_date or cn_today_str()
    if start_date:
        start = start_date
    else:
        start = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=120)).strftime("%Y-%m-%d")

    config = _build_runtime_config({})
    set_config(config)
    raw = route_to_vendor("get_stock_data", symbol, start, end)
    candles = _parse_stock_csv(raw)
    if not candles:
        raise HTTPException(status_code=404, detail="no kline data")
    return KlineResponse(
        symbol=symbol,
        start_date=start,
        end_date=end,
        candles=candles,
    )


@app.post("/v1/analyze", response_model=AnalyzeResponse)
def analyze(request: AnalyzeRequest) -> AnalyzeResponse:
    job_id = uuid4().hex
    now = datetime.now().isoformat()
    _set_job(
        job_id,
        job_id=job_id,
        status="pending",
        created_at=now,
        started_at=None,
        finished_at=None,
        symbol=request.symbol,
        trade_date=request.trade_date,
        error=None,
        result=None,
        decision=None,
    )
    _ensure_job_event_queue(job_id)
    _emit_job_event(
        job_id,
        "job.created",
        {"job_id": job_id, "symbol": request.symbol, "trade_date": request.trade_date},
    )
    _executor.submit(_run_job, job_id, request, True)
    return AnalyzeResponse(job_id=job_id, status="pending", created_at=now)


@app.get("/v1/jobs/{job_id}", response_model=JobStatusResponse)
def get_job_status(job_id: str) -> JobStatusResponse:
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return JobStatusResponse(
        job_id=job["job_id"],
        status=job["status"],
        created_at=job["created_at"],
        started_at=job.get("started_at"),
        finished_at=job.get("finished_at"),
        symbol=job["symbol"],
        trade_date=job["trade_date"],
        error=job.get("error"),
    )


@app.get("/v1/jobs/{job_id}/result")
def get_job_result(job_id: str) -> Dict[str, Any]:
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if job["status"] != "completed":
        raise HTTPException(status_code=409, detail=f"job status is {job['status']}")
    return {
        "job_id": job_id,
        "status": job["status"],
        "decision": job.get("decision"),
        "result": job.get("result"),
        "finished_at": job.get("finished_at"),
    }


@app.get("/v1/jobs/{job_id}/events")
def stream_job_events(job_id: str):
    with _jobs_lock:
        if job_id not in _jobs:
            raise HTTPException(status_code=404, detail="job not found")
    return StreamingResponse(
        _stream_job_events(job_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


def _ai_extract_symbol_and_date(text: str, config: Dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """Use LLM to resolve symbol and date from natural language."""
    from tradingagents.llm_clients.factory import create_llm_client
    
    try:
        # 1. Initialize a lightweight client
        client = create_llm_client(
            provider=config.get("llm_provider", "openai"),
            model=config.get("quick_think_llm", "gpt-4o-mini"),
            base_url=config.get("backend_url"),
        )

        # 2. Craft a strict extraction prompt
        today = datetime.now().strftime("%Y-%m-%d")
        prompt = f"""
        You are a financial data assistant. Extract the STOCK SYMBOL and TRADE DATE from the user's message.

        Rules:
        - If it's a Chinese company name, convert to A-share code (e.g., '贵州茅台' -> '600519.SH').
        - If it's a US company, use ticker (e.g., '苹果' -> 'AAPL').
        - Use YYYY-MM-DD for date. Today is {today}.
        - If no date mentioned, use {today}.
        - Output ONLY a JSON: {{"symbol": "...", "date": "..."}}. 
        - If no stock found, return {{"symbol": null, "date": null}}.

        User message: "{text}"
        """

        # 3. Call LLM
        llm = client.get_llm()
        response = llm.invoke(prompt)
        raw_text = response if isinstance(response, str) else getattr(response, "content", str(response))

        # 4. Parse JSON from response
        import json
        match = re.search(r"\{.*\}", raw_text, re.DOTALL)
        if match:
            data = json.loads(match.group(0))
            return _normalize_symbol(data.get("symbol") or ""), data.get("date")
    except Exception as e:
        print(f"AI Extraction failed: {e}")

    return None, None

@app.post("/v1/chat/completions")
def chat_completions(request: ChatCompletionRequest):
    text = _extract_chat_text(request.messages)
    config = _build_runtime_config(request.config_overrides)

    # 仅使用 LLM 解析，避免本地正则误判后缀
    symbol, trade_date = _ai_extract_symbol_and_date(text, config)

    if not symbol:
        message = "抱歉，我没能从您的消息中识别出股票标的。请输入代码（如 600519.SH）或可识别的公司名称。"
        if request.stream:
            def _error_stream():
                yield _sse_pack("job.failed", {"error": message})
                yield "event: done\ndata: [DONE]\n\n"
            return StreamingResponse(
                _error_stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
            )
        raise HTTPException(status_code=400, detail=message)


    analyze_req = AnalyzeRequest(
        symbol=symbol,
        trade_date=trade_date or cn_today_str(),
        selected_analysts=request.selected_analysts,
        config_overrides=request.config_overrides,
        dry_run=request.dry_run,
    )
    job_id = uuid4().hex
    now = datetime.now().isoformat()
    _set_job(
        job_id,
        job_id=job_id,
        status="pending",
        created_at=now,
        started_at=None,
        finished_at=None,
        symbol=analyze_req.symbol,
        trade_date=analyze_req.trade_date,
        error=None,
        result=None,
        decision=None,
    )
    _ensure_job_event_queue(job_id)
    _emit_job_event(
        job_id,
        "job.created",
        {"job_id": job_id, "symbol": analyze_req.symbol, "trade_date": analyze_req.trade_date},
    )
    _executor.submit(_run_job, job_id, analyze_req, True)

    if request.stream:
        return StreamingResponse(
            _stream_job_events(job_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    return {
        "id": f"chatcmpl-{job_id}",
        "object": "chat.completion",
        "created": int(datetime.now().timestamp()),
        "model": request.model,
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": (
                        f"已启动分析任务：{job_id}\n"
                        f"symbol={analyze_req.symbol}, trade_date={analyze_req.trade_date}\n"
                        f"可通过 /v1/jobs/{job_id} 与 /v1/jobs/{job_id}/result 查询结果。"
                    ),
                },
            }
        ],
    }


# Report API Endpoints
@app.post("/v1/reports", response_model=ReportResponse)
def create_report_endpoint(
    request: ReportCreateRequest,
    db: Session = Depends(get_db),
):
    """手动创建报告（通常由系统自动调用）."""
    report = report_service.create_report(
        db=db,
        symbol=request.symbol,
        trade_date=request.trade_date,
        decision=request.decision,
        result_data=request.result_data,
        user_id=None,  # TODO: 后续添加用户认证后从 token 中提取
    )
    return report


@app.get("/v1/reports", response_model=ReportListResponse)
def list_reports(
    symbol: Optional[str] = Query(None, description="按股票代码筛选"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    """获取报告列表."""
    total = report_service.count_reports(db=db, symbol=symbol)
    reports = report_service.get_reports_by_user(
        db=db,
        user_id=None,  # TODO: 后续添加用户认证后从 token 中提取
        symbol=symbol,
        skip=skip,
        limit=limit,
    )
    return {"total": total, "reports": reports}


@app.get("/v1/reports/{report_id}", response_model=ReportDetailResponse)
def get_report_endpoint(
    report_id: str,
    db: Session = Depends(get_db),
):
    """获取报告详情."""
    report = report_service.get_report(db, report_id)
    if not report:
        raise HTTPException(status_code=404, detail="报告不存在")
    return report


@app.delete("/v1/reports/{report_id}")
def delete_report_endpoint(
    report_id: str,
    db: Session = Depends(get_db),
):
    """删除报告."""
    success = report_service.delete_report(db, report_id)
    if not success:
        raise HTTPException(status_code=404, detail="报告不存在")
    return {"message": "报告已删除"}


def run() -> None:
    import uvicorn

    uvicorn.run("api.main:app", host="0.0.0.0", port=8000, reload=False)
