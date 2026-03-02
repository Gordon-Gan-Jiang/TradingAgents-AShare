from __future__ import annotations

import json
import os
import queue
import re
import traceback
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime
from threading import Lock
from typing import Any, Dict, List, Literal, Optional
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.dataflows.trade_calendar import cn_today_str

load_dotenv()

app = FastAPI(title="TradingAgents-AShare API", version="0.1.0")

_executor = ThreadPoolExecutor(max_workers=2)
_jobs_lock = Lock()
_jobs: Dict[str, Dict[str, Any]] = {}
_job_events: Dict[str, "queue.Queue[Dict[str, Any]]"] = {}


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
    m = re.search(r"\b(\d{6})(?:\.(SH|SZ|SS))?\b", s)
    if m:
        code = m.group(1)
        suffix = m.group(2)
        if suffix:
            if suffix == "SS":
                return f"{code}.SH"
            return f"{code}.{suffix}"
        market = "SH" if code.startswith(("5", "6", "9")) else "SZ"
        return f"{code}.{market}"
    m2 = re.search(r"\b[A-Z]{1,6}(?:\.[A-Z]{1,3})?\b", s)
    if m2:
        return m2.group(0)
    return s


def _extract_chat_text(messages: List[ChatMessage]) -> str:
    if not messages:
        return ""
    last = messages[-1]
    return _extract_message_text(last.content)


def _extract_symbol_and_date(text: str) -> tuple[Optional[str], Optional[str]]:
    date_match = re.search(r"\b\d{4}-\d{2}-\d{2}\b", text)
    date = date_match.group(0) if date_match else None

    sym_match = re.search(r"\b\d{6}(?:\.(?:SH|SZ|SS))?\b", text, re.IGNORECASE)
    if sym_match:
        return _normalize_symbol(sym_match.group(0)), date

    us_match = re.search(r"\b[A-Z]{1,6}(?:\.[A-Z]{1,3})?\b", text.upper())
    if us_match:
        return us_match.group(0), date

    return None, date


def _sse_pack(event: str, data: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


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


@app.post("/v1/chat/completions")
def chat_completions(request: ChatCompletionRequest):
    text = _extract_chat_text(request.messages)
    symbol, trade_date = _extract_symbol_and_date(text)
    if not symbol:
        raise HTTPException(status_code=400, detail="无法从消息中识别标的代码")

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
