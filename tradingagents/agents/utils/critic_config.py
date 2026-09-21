"""Runtime helpers for Decision Critic (enable flag + revision severity threshold)."""

from __future__ import annotations

import os
from typing import Any, Optional

from tradingagents.dataflows.config import get_config


def _env_disables_critic() -> bool:
    return os.getenv("TA_DECISION_CRITIC", "1").strip().lower() in ("0", "false", "no", "off")


def is_decision_critic_enabled(config: Optional[dict[str, Any]] = None) -> bool:
    """Return whether the Decision Critic node should run.

    TA_DECISION_CRITIC=0 is a global kill switch (applies even if user DB says on).
    """
    if _env_disables_critic():
        return False
    cfg = get_config() if config is None else config
    return bool(cfg.get("decision_critic_enabled", True))


def decision_critic_revision_threshold(config: Optional[dict[str, Any]] = None) -> float:
    """Minimum critic severity (0–100) required to trigger a revision pass."""
    cfg = get_config() if config is None else config
    try:
        return float(cfg.get("decision_critic_revision_threshold", 40.0))
    except (TypeError, ValueError):
        return 40.0
