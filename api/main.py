from __future__ import annotations

import json
import os
import queue
import re
import traceback
from io import StringIO
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta
from threading import Lock
from typing import Any, Dict, List, Literal, Optional
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
import pandas as pd

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.dataflows.trade_calendar import cn_today_str
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.interface import route_to_vendor

load_dotenv()

app = FastAPI(title="TradingAgents-AShare API", version="0.1.0")
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
        for team_agents in FIXED_TEAMS.values():
            for agent in team_agents:
                self.status[agent] = "pending"

        # 未选中的分析师标记为 skipped（仍展示，便于固定 12-agent 看板）
        for key in ANALYST_ORDER:
            agent = ANALYST_AGENT_NAMES[key]
            if key not in self.selected_analysts:
                self.status[agent] = "skipped"

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

    def apply_chunk(self, chunk: Dict[str, Any]) -> None:
        # CLI: 分析师阶段状态推进
        found_active = False
        for analyst_key in ANALYST_ORDER:
            if analyst_key not in self.selected_analysts:
                continue

            agent_name = ANALYST_AGENT_NAMES[analyst_key]
            report_key = ANALYST_REPORT_MAP[analyst_key]
            has_report = bool(chunk.get(report_key))

            if has_report:
                self._set_status(agent_name, "completed")
                self.report_sections[report_key] = chunk.get(report_key)
            elif not found_active:
                self._set_status(agent_name, "in_progress")
                found_active = True
            else:
                self._set_status(agent_name, "pending")

        if not found_active and self.selected_analysts:
            if self.status.get("Bull Researcher") == "pending":
                self._set_status("Bull Researcher", "in_progress")

        # CLI: 研究团队
        debate_state = chunk.get("investment_debate_state") or {}
        bull_hist = str(debate_state.get("bull_history", "")).strip()
        bear_hist = str(debate_state.get("bear_history", "")).strip()
        judge = str(debate_state.get("judge_decision", "")).strip()
        if bull_hist or bear_hist:
            self._update_research_team_status("in_progress")
        if judge:
            self._update_research_team_status("completed")
            self._set_status("Trader", "in_progress")

        # CLI: 交易团队
        if chunk.get("trader_investment_plan"):
            if self.status.get("Trader") != "completed":
                self._set_status("Trader", "completed")
                self._set_status("Aggressive Analyst", "in_progress")

        # CLI: 风控与组合团队
        risk_state = chunk.get("risk_debate_state") or {}
        agg_hist = str(risk_state.get("aggressive_history", "")).strip()
        con_hist = str(risk_state.get("conservative_history", "")).strip()
        neu_hist = str(risk_state.get("neutral_history", "")).strip()
        risk_judge = str(risk_state.get("judge_decision", "")).strip()

        if agg_hist and self.status.get("Aggressive Analyst") != "completed":
            self._set_status("Aggressive Analyst", "in_progress")
        if con_hist and self.status.get("Conservative Analyst") != "completed":
            self._set_status("Conservative Analyst", "in_progress")
        if neu_hist and self.status.get("Neutral Analyst") != "completed":
            self._set_status("Neutral Analyst", "in_progress")
        if risk_judge:
            if self.status.get("Portfolio Manager") != "completed":
                self._set_status("Portfolio Manager", "in_progress")
                self._set_status("Aggressive Analyst", "completed")
                self._set_status("Conservative Analyst", "completed")
                self._set_status("Neutral Analyst", "completed")
                self._set_status("Portfolio Manager", "completed")


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


def _run_job(job_id: str, request: AnalyzeRequest, stream_events: bool = False) -> None:
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
                    if content:
                        _emit_job_event(
                            job_id,
                            "agent.message",
                            {
                                "agent": getattr(msg, "name", None),
                                "message_type": getattr(msg, "type", None),
                                "content": content,
                            },
                        )

                    for tool_call in getattr(msg, "tool_calls", []) or []:
                        _emit_job_event(
                            job_id,
                            "agent.tool_call",
                            {"agent": getattr(msg, "name", None), "tool_call": tool_call},
                        )

                for key in report_keys:
                    value = chunk.get(key)
                    if value and value != last_report.get(key):
                        last_report[key] = value
                        _emit_job_event(
                            job_id,
                            "agent.report",
                            {"section": key, "content": str(value)},
                        )
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

    # Fallback to regex if AI fails
    return _extract_symbol_and_date(text)

@app.post("/v1/chat/completions")
def chat_completions(request: ChatCompletionRequest):
    text = _extract_chat_text(request.messages)
    config = _build_runtime_config(request.config_overrides)

    # NEW: Use AI to extract instead of just regex
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


def run() -> None:
    import uvicorn

    uvicorn.run("api.main:app", host="0.0.0.0", port=8000, reload=False)
