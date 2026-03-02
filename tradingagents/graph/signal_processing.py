# TradingAgents/graph/signal_processing.py

from langchain_openai import ChatOpenAI
from tradingagents.dataflows.config import get_config
from tradingagents.prompts import get_prompt


class SignalProcessor:
    """Processes trading signals to extract actionable decisions."""

    def __init__(self, quick_thinking_llm: ChatOpenAI):
        """Initialize with an LLM for processing."""
        self.quick_thinking_llm = quick_thinking_llm

    def process_signal(self, full_signal: str) -> str:
        """
        Process a full trading signal to extract the core decision.

        Args:
            full_signal: Complete trading signal text

        Returns:
            Extracted decision (BUY, SELL, or HOLD)
        """
        messages = [
            (
                "system",
                get_prompt("signal_extractor_system", config=get_config()),
            ),
            ("human", full_signal),
        ]

        return self.quick_thinking_llm.invoke(messages).content
