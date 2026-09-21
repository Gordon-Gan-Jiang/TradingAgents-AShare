"""Model name validators for each provider.

Only validates model names - does NOT enforce limits.
Let LLM providers use their own defaults for unspecified params.
"""

from __future__ import annotations

import re

SUPPORTED_LLM_PROVIDERS = frozenset({
    "openai",
    "anthropic",
    "google",
    "xai",
    "ollama",
    "openrouter",
})

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

VALID_MODELS = {
    "openai": [
        # GPT-5 series (2025)
        "gpt-5.2",
        "gpt-5.1",
        "gpt-5",
        "gpt-5-mini",
        "gpt-5-nano",
        # GPT-4.1 series (2025)
        "gpt-4.1",
        "gpt-4.1-mini",
        "gpt-4.1-nano",
        # o-series reasoning models
        "o4-mini",
        "o3",
        "o3-mini",
        "o1",
        "o1-preview",
        # GPT-4o series (legacy but still supported)
        "gpt-4o",
        "gpt-4o-mini",
    ],
    "anthropic": [
        # Claude 4.5 series (2025)
        "claude-opus-4-5",
        "claude-sonnet-4-5",
        "claude-haiku-4-5",
        # Claude 4.x series
        "claude-opus-4-1-20250805",
        "claude-sonnet-4-20250514",
        # Claude 3.7 series
        "claude-3-7-sonnet-20250219",
        # Claude 3.5 series (legacy)
        "claude-3-5-haiku-20241022",
        "claude-3-5-sonnet-20241022",
    ],
    "google": [
        # Gemini 3 series (preview)
        "gemini-3-pro-preview",
        "gemini-3-flash-preview",
        # Gemini 2.5 series
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        # Gemini 2.0 series
        "gemini-2.0-flash",
        "gemini-2.0-flash-lite",
    ],
    "xai": [
        # Grok 4.1 series
        "grok-4-1-fast",
        "grok-4-1-fast-reasoning",
        "grok-4-1-fast-non-reasoning",
        # Grok 4 series
        "grok-4",
        "grok-4-0709",
        "grok-4-fast-reasoning",
        "grok-4-fast-non-reasoning",
    ],
}


def looks_like_uuid(value: str) -> bool:
    """Return True when value matches a UUID (e.g. Volcengine Ark endpoint ID)."""
    return bool(_UUID_RE.match(str(value or "").strip()))


def validate_llm_provider(provider: str) -> str:
    """Normalize and validate llm_provider; reject misplaced endpoint IDs."""
    text = str(provider or "").strip().lower()
    if not text:
        raise ValueError("llm_provider 不能为空")
    if text in SUPPORTED_LLM_PROVIDERS:
        return text
    raw = str(provider or "").strip()
    if looks_like_uuid(raw):
        raise ValueError(
            f"llm_provider 配置错误：'{raw}' 看起来是火山方舟接入点 ID，不是 provider。"
            "请将接入点 ID 填在模型名（quick_think_llm / deep_think_llm 或 TA_LLM_QUICK / TA_LLM_DEEP），"
            "llm_provider 请设为 openai。"
        )
    raise ValueError(
        f"不支持的 llm_provider: {provider}。"
        f"支持的值: {', '.join(sorted(SUPPORTED_LLM_PROVIDERS))}"
    )


def validate_model(provider: str, model: str) -> bool:
    """Check if model name is valid for the given provider.

    For ollama, openrouter - any model is accepted.
    """
    provider_lower = provider.lower()

    if provider_lower in ("ollama", "openrouter"):
        return True

    if provider_lower not in VALID_MODELS:
        return True

    return model in VALID_MODELS[provider_lower]
