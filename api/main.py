from __future__ import annotations

import os
import traceback
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime
from threading import Lock
from typing import Any, Dict, List, Literal, Optional
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.dataflows.trade_calendar import cn_today_str

load_dotenv()

app = FastAPI(title="TradingAgents-AShare API", version="0.1.0")

_executor = ThreadPoolExecutor(max_workers=2)
_jobs_lock = Lock()
_jobs: Dict[str, Dict[str, Any]] = {}


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


def _run_job(job_id: str, request: AnalyzeRequest) -> None:
    _set_job(job_id, status="running", started_at=datetime.now().isoformat())
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
            return

        graph = TradingAgentsGraph(
            selected_analysts=request.selected_analysts,
            debug=False,
            config=config,
        )
        final_state, decision = graph.propagate(request.symbol, request.trade_date)

        result = {
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

        _set_job(
            job_id,
            status="completed",
            result=result,
            decision=decision,
            finished_at=datetime.now().isoformat(),
        )
    except Exception as exc:
        _set_job(
            job_id,
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
            traceback=traceback.format_exc(),
            finished_at=datetime.now().isoformat(),
        )


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
    _executor.submit(_run_job, job_id, request)
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


def run() -> None:
    import uvicorn

    uvicorn.run("api.main:app", host="0.0.0.0", port=8000, reload=False)
