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
from tradingagents.methodology import get_sector_macro_ashare_methodology_block


def create_macro_analyst(llm, data_collector=None):
    async def _safe(tool, payload):
        try:
            return await asyncio.to_thread(tool.invoke, payload)
        except Exception as exc:
            return f"调用失败：{exc}"

    async def macro_analyst_node(state):
        current_date = state["trade_date"]
        ticker = state["company_of_interest"]
        print(f"[Macro Analyst] START {ticker} {current_date}")
        horizon = "medium"  # 宏观面固定中长期视角
        user_intent = state.get("user_intent") or {}
        focus_areas = user_intent.get("focus_areas", [])
        specific_questions = user_intent.get("specific_questions", [])

        config = get_config()
        system_message = get_prompt("macro_system_message", config=config) or ""
        _sym = str(ticker or "").strip().upper().replace(".SS", ".SH")
        if is_cn_symbol(_sym):
            _addon = get_sector_macro_ashare_methodology_block(config)
            if _addon:
                system_message = (
                    system_message
                    + "\n\n## A 股行业与宏观专项方法论（须对照执行）\n\n"
                    + _addon
                )
        horizon_ctx = build_horizon_context(horizon, focus_areas, specific_questions, agent_type="macro")

        pool = data_collector.get(ticker, current_date) if data_collector else None

        macro_brief = ""
        if pool is not None:
            macro_brief = pool.get("macro_ashare_brief") or ""

        if pool is not None:
            board_flow = pool.get("fund_flow_board", "无数据")
            recent_news = pool.get("news", "无数据")
        else:
            from datetime import datetime, timedelta
            from tradingagents.agents.utils.agent_utils import get_board_fund_flow, get_news
            days = 7
            end_dt = datetime.strptime(current_date, "%Y-%m-%d")
            start_dt = end_dt - timedelta(days=days)
            
            # Parallelize fallback fetches
            results = await asyncio.gather(
                _safe(get_board_fund_flow, {}),
                _safe(get_news, {
                    "ticker": ticker, "start_date": start_dt.strftime("%Y-%m-%d"), "end_date": current_date,
                })
            )
            board_flow, recent_news = results
            try:
                from tradingagents.methodology.macro_fetch import fetch_ashare_macro_pool_brief

                macro_brief = fetch_ashare_macro_pool_brief(current_date, config) or ""
            except Exception:
                macro_brief = ""

        messages = [
            SystemMessage(content=(
                system_message
                + "\n\n请严格基于提供的数据输出报告，全程使用中文。"
            )),
            HumanMessage(content=(
                horizon_ctx + "\n"
                f"请分析 {ticker} 在 {current_date} 的宏观与板块环境。\n\n"
                f"【国内宏观数据摘要（自动抓取）】\n{macro_brief or '（无或未启用）'}\n\n"
                f"【今日行业板块资金流向】\n{board_flow}\n\n"
                f"【近期相关新闻】\n{recent_news}"
                + get_analyst_json_instruction(agent_role="macro", config=config)
            )),
        ]

        # ── 实现 Token 级流式输出 ──────────────────
        tracker = current_tracker_var.get()
        full_content = ""
        async for chunk in llm.astream(messages):
            content = chunk.content if hasattr(chunk, "content") else str(chunk)
            full_content += content
            if tracker:
                tracker._emit_token("Macro Analyst", "macro_report", content)

        print(f"[Macro Analyst] DONE {ticker}, report length={len(full_content)}")
        verdict, confidence, verdict_parsed = extract_verdict_with_flag(full_content)
        trace = {
            "agent": "macro_analyst",
            "horizon": horizon,
            "data_window": "板块数据",
            "key_finding": f"宏观板块分析结论：{verdict}",
            "verdict": verdict,
            "confidence": confidence,
            "verdict_parsed": verdict_parsed,
        }
        parsed = extract_analyst_structured_json(full_content)
        if parsed:
            trace["structured"] = parsed
        return {
            "macro_report": full_content,
            "analyst_traces": [trace],
        }

    return macro_analyst_node
