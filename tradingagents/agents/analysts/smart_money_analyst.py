import asyncio

from langchain_core.messages import HumanMessage, SystemMessage
from tradingagents.dataflows.config import get_config
from tradingagents.methodology import get_stock_team_analysis_framework_block
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


def create_smart_money_analyst(llm, data_collector=None):
    async def _safe(tool, payload):
        try:
            return await asyncio.to_thread(tool.invoke, payload)
        except Exception as exc:
            return f"调用失败：{exc}"

    async def smart_money_analyst_node(state):
        current_date = state["trade_date"]
        ticker = state["company_of_interest"]
        print(f"[Smart Money Analyst] START {ticker} {current_date}")
        horizon = "short"  # 资金面固定短期视角
        user_intent = state.get("user_intent") or {}
        focus_areas = user_intent.get("focus_areas", [])
        specific_questions = user_intent.get("specific_questions", [])

        config = get_config()
        system_message = get_prompt("smart_money_system_message", config=config) or ""
        stock_team_framework = get_stock_team_analysis_framework_block(config)
        if stock_team_framework:
            system_message = system_message + "\n\n" + stock_team_framework
        horizon_ctx = build_horizon_context(horizon, focus_areas, specific_questions, agent_type="smart_money")

        pool = data_collector.get(ticker, current_date) if data_collector else None
        freshness_ctx = ""
        fallback_results = None

        if pool is not None:
            fund_flow = pool.get("fund_flow_individual", "无数据")
            lhb = pool.get("lhb", "无数据")
            volume = pool.get("indicators", {}).get("vwma", "无数据")
            fallback_results = pool
        else:
            from tradingagents.agents.utils.agent_utils import (
                get_individual_fund_flow, get_lhb_detail, get_indicators,
            )

            results = await asyncio.gather(
                _safe(get_individual_fund_flow, {"symbol": ticker}),
                _safe(get_lhb_detail, {"symbol": ticker, "date": current_date}),
                _safe(get_indicators, {
                    "symbol": ticker, "indicator": "volume",
                    "curr_date": current_date, "look_back_days": 20,
                })
            )
            fund_flow, lhb, volume = results
            fallback_results = {
                "fund_flow_individual": fund_flow,
                "lhb": lhb,
            }

        from tradingagents.dataflows.freshness import resolve_freshness_pool
        from tradingagents.dataflows.freshness.prompt import format_freshness_context_for_sources

        freshness_pool = resolve_freshness_pool(
            state, data_collector, ticker, current_date, fallback_results=fallback_results
        )
        freshness_ctx = format_freshness_context_for_sources(
            freshness_pool,
            ["individual_fund_flow", "lhb_detail"],
        )

        messages = [
            SystemMessage(content=(
                system_message
                + "\n\n请严格基于提供的量化数据输出分析，全程使用中文。"
            )),
            HumanMessage(content=(
                horizon_ctx + "\n"
                + (freshness_ctx + "\n" if freshness_ctx else "")
                + f"请分析 {ticker} 在 {current_date} 的主力资金行为。\n\n"
                f"【近5日主力资金净流向】\n{fund_flow}\n\n"
                f"【龙虎榜数据】\n{lhb}\n\n"
                f"【成交量指标(vwma)】\n{volume}"
                + get_analyst_json_instruction(agent_role="smart_money", config=config)
            )),
        ]

        tracker = current_tracker_var.get()
        full_content = ""
        async for chunk in llm.astream(messages):
            content = chunk.content if hasattr(chunk, "content") else str(chunk)
            full_content += content
            if tracker:
                tracker._emit_token("Smart Money Analyst", "smart_money_report", content)

        print(f"[Smart Money Analyst] DONE {ticker}, report length={len(full_content)}")
        verdict, confidence, verdict_parsed = extract_verdict_with_flag(full_content)
        trace = {
            "agent": "smart_money_analyst",
            "horizon": horizon,
            "data_window": "近期可用",
            "key_finding": f"主力资金分析结论：{verdict}",
            "verdict": verdict,
            "confidence": confidence,
            "verdict_parsed": verdict_parsed,
        }
        parsed = extract_analyst_structured_json(full_content)
        if parsed:
            trace["structured"] = parsed
        return {
            "smart_money_report": full_content,
            "analyst_traces": [trace],
        }

    return smart_money_analyst_node
