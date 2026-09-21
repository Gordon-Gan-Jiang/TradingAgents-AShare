import asyncio

from langchain_core.messages import HumanMessage, SystemMessage
from tradingagents.dataflows.config import get_config
from tradingagents.prompts import get_prompt
from tradingagents.graph.intent_parser import build_horizon_context
from tradingagents.agents.utils.agent_states import (
    current_tracker_var,
    extract_verdict_with_flag,
)
from tradingagents.agents.utils.analyst_structured import (
    extract_analyst_structured_json,
    get_analyst_json_instruction,
)
from tradingagents.dataflows.trade_calendar import is_cn_symbol
from tradingagents.methodology import get_fundamentals_ashare_methodology_block


def create_fundamentals_analyst(llm, data_collector=None):
    async def _safe(tool, payload):
        try:
            return await asyncio.to_thread(tool.invoke, payload)
        except Exception as exc:
            return f"调用失败：{exc}"

    async def fundamentals_analyst_node(state):
        current_date = state["trade_date"]
        ticker = state["company_of_interest"]
        horizon = "medium"  # 基本面固定中长期视角
        user_intent = state.get("user_intent") or {}
        focus_areas = user_intent.get("focus_areas", [])
        specific_questions = user_intent.get("specific_questions", [])

        config = get_config()
        system_message = get_prompt("fundamentals_system_message", config=config)
        _sym = str(ticker or "").strip().upper().replace(".SS", ".SH")
        if is_cn_symbol(_sym):
            _addon = get_fundamentals_ashare_methodology_block(config)
            if _addon:
                system_message = (
                    system_message
                    + "\n\n## A 股专项方法论（须对照执行，缺失数据须显式声明）\n\n"
                    + _addon
                )
        horizon_ctx = build_horizon_context(horizon, focus_areas, specific_questions, agent_type="fundamentals")

        pool = data_collector.get(ticker, current_date) if data_collector else None
        fallback_results = None

        if pool is not None:
            outputs = {k: pool.get(k, "无数据") for k in
                       ["fundamentals", "balance_sheet", "cashflow", "income_statement"]}
            fallback_results = pool
        else:
            from tradingagents.agents.utils.agent_utils import (
                get_fundamentals, get_balance_sheet, get_cashflow, get_income_statement,
            )
            tasks = {
                "fundamentals": _safe(get_fundamentals, {"ticker": ticker, "curr_date": current_date}),
                "balance_sheet": _safe(get_balance_sheet, {"ticker": ticker, "freq": "quarterly", "curr_date": current_date}),
                "cashflow": _safe(get_cashflow, {"ticker": ticker, "freq": "quarterly", "curr_date": current_date}),
                "income_statement": _safe(get_income_statement, {"ticker": ticker, "freq": "quarterly", "curr_date": current_date}),
            }
            keys = list(tasks.keys())
            results = await asyncio.gather(*[tasks[k] for k in keys])
            outputs = dict(zip(keys, results))
            fallback_results = outputs

        from tradingagents.dataflows.freshness import resolve_freshness_pool
        from tradingagents.dataflows.freshness.prompt import format_freshness_context_for_sources

        freshness_pool = resolve_freshness_pool(
            state, data_collector, ticker, current_date, fallback_results=fallback_results
        )
        freshness_ctx = format_freshness_context_for_sources(
            freshness_pool,
            ["fundamentals", "balance_sheet", "cashflow", "income_statement"],
        )

        messages = [
            SystemMessage(content=system_message + "\n\n请全程使用中文。"),
            HumanMessage(content=(
                horizon_ctx + "\n"
                + (freshness_ctx + "\n" if freshness_ctx else "")
                + f"以下是 {ticker} 在 {current_date} 的基本面资料。\n\n"
                f"【get_fundamentals】\n{outputs['fundamentals']}\n\n"
                f"【get_balance_sheet】\n{outputs['balance_sheet']}\n\n"
                f"【get_cashflow】\n{outputs['cashflow']}\n\n"
                f"【get_income_statement】\n{outputs['income_statement']}\n"
                + get_analyst_json_instruction(agent_role="fundamentals", config=config)
            )),
        ]

        # ── 实现 Token 级流式输出 ──────────────────
        tracker = current_tracker_var.get()
        full_content = ""
        async for chunk in llm.astream(messages):
            content = chunk.content if hasattr(chunk, "content") else str(chunk)
            full_content += content
            if tracker:
                tracker._emit_token("Fundamentals Analyst", "fundamentals_report", content)

        verdict, confidence, verdict_parsed = extract_verdict_with_flag(full_content)
        trace = {
            "agent": "fundamentals_analyst",
            "horizon": horizon,
            "data_window": "财报周期",
            "key_finding": f"基本面分析结论：{verdict}",
            "verdict": verdict,
            "confidence": confidence,
            "verdict_parsed": verdict_parsed,
        }
        parsed = extract_analyst_structured_json(full_content)
        if parsed:
            trace["structured"] = parsed
        return {
            "fundamentals_report": full_content,
            "analyst_traces": [trace],
        }

    return fundamentals_analyst_node
