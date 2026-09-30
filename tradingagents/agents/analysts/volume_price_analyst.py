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
    resolve_analyst_direction,
)


def create_volume_price_analyst(llm, data_collector=None):
    async def volume_price_analyst_node(state):
        current_date = state["trade_date"]
        ticker = state["company_of_interest"]
        horizon = "short"
        user_intent = state.get("user_intent") or {}
        focus_areas = user_intent.get("focus_areas", [])
        specific_questions = user_intent.get("specific_questions", [])

        config = get_config()
        horizon_ctx = build_horizon_context(horizon, focus_areas, specific_questions, agent_type="volume_price")
        system_message = get_prompt("volume_price_system_message", config=config)
        stock_team_framework = get_stock_team_analysis_framework_block(config)
        if stock_team_framework:
            system_message = system_message + "\n\n" + stock_team_framework

        if data_collector is not None:
            pool = data_collector.get(ticker, current_date)
            if pool is not None:
                windowed = data_collector.get_window(pool, horizon, current_date)
                vpa_data = windowed.get("vpa_indicators", "无数据")
                stock_data = windowed.get("stock_data", "无数据")
                data_window = windowed.get("_data_window", "14天")
            else:
                vpa_data, stock_data, data_window = "无数据", "无数据", "14天"
        else:
            vpa_data, stock_data, data_window = "无数据", "无数据", "14天"

        messages = [
            SystemMessage(content=horizon_ctx + system_message + "\n\n请全程使用中文。"),
            HumanMessage(content=(
                f"以下是 {ticker} 在 {current_date} 的量价分析预计算数据（数据窗口：{data_window}）。\n\n"
                f"{vpa_data}\n\n"
                f"【原始 K 线数据参考】\n{stock_data}"
                + get_analyst_json_instruction(agent_role="volume_price", config=config)
            )),
        ]

        tracker = current_tracker_var.get()
        full_content = ""
        async for chunk in llm.astream(messages):
            content = chunk.content if hasattr(chunk, "content") else str(chunk)
            full_content += content
            if tracker:
                tracker._emit_token("Volume Price Analyst", "volume_price_report", content)

        parsed = extract_analyst_structured_json(full_content)
        verdict, confidence, verdict_parsed = extract_verdict_with_flag(full_content)
        # 方向来自该分析师真正写下的机读块（VERDICT → ANALYST_JSON），判定规则统一在
        # resolve_analyst_direction，与共识层、研究经理共用，避免三处各判一套。
        direction = resolve_analyst_direction(
            verdict=verdict,
            confidence=confidence,
            verdict_parsed=verdict_parsed,
            structured=parsed,
        )

        trace = {
            "agent": "volume_price_analyst",
            "horizon": horizon,
            "data_window": data_window,
            "key_finding": f"量价分析结论：{direction.direction or verdict}",
            "verdict": direction.direction or verdict,
            "confidence": (
                direction.confidence
                if direction.source == "analyst_json"
                else confidence
            ),
            # VERDICT 块本身是否解析成功；方向可能来自 ANALYST_JSON，故 False ≠ 未计入。
            "verdict_parsed": verdict_parsed,
            "verdict_source": direction.source,
        }
        if parsed:
            trace["structured"] = parsed
        return {
            "volume_price_report": full_content,
            "analyst_traces": [trace],
        }

    return volume_price_analyst_node
