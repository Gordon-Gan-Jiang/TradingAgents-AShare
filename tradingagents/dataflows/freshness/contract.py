from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_PKG_DIR = Path(__file__).resolve().parent

_BASE_COLLECTOR_KEY_TO_SOURCE: dict[str, str] = {
    "fund_flow_individual": "individual_fund_flow",
    "fund_flow_board": "board_fund_flow",
    "lhb": "lhb_detail",
    "hot_stocks": "hot_stocks_xq",
}


@lru_cache(maxsize=1)
def load_contract() -> dict[str, dict[str, Any]]:
    path = _PKG_DIR / "contract.yaml"
    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    entries: dict[str, dict[str, Any]] = {}
    for source_key, cfg in raw.items():
        if not isinstance(cfg, dict):
            continue
        entry = dict(cfg)
        entry["source_key"] = source_key
        entries[source_key] = entry
    return entries


@lru_cache(maxsize=1)
def load_collector_key_map() -> dict[str, str]:
    mapping = dict(_BASE_COLLECTOR_KEY_TO_SOURCE)
    for source_key, entry in load_contract().items():
        for ck in entry.get("collector_keys") or []:
            mapping[str(ck)] = source_key
    return mapping


@lru_cache(maxsize=1)
def load_section_map() -> dict[str, dict[str, Any]]:
    path = _PKG_DIR / "section_map.yaml"
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def get_contract_entry(source_key: str) -> dict[str, Any] | None:
    return load_contract().get(source_key)


def resolve_source_key(collector_key: str) -> str:
    return load_collector_key_map().get(collector_key, collector_key)


def all_contract_source_keys() -> list[str]:
    return list(load_contract().keys())
