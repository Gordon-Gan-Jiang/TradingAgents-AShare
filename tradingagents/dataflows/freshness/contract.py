from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

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


@lru_cache(maxsize=1)
def report_pipeline_sources() -> frozenset[str]:
    """报告管线（DataCollector）**自己采集**的源（声明在 `contract.yaml` 顶部）。

    这张表只回答一件事：某源**没出现在本轮采集结果里**时，是"该采没采到"
    （error）还是"不归这条管线管"（not_applicable）。详见 `contract.yaml` 顶部
    注释里记录的那条真实事故。

    **声明缺失/为空时回退到「全部契约源」并告警**，这是刻意的取舍：一句
    "拿不到就当没事"会让报告在采集器整体返回空时**照样显示 healthy**——伪造
    fresh 是本平台最不容忍的失败形态。宁可退化成"多报一个数据异常"（响亮但
    可查），也不要静默变成"数据最新"。声明内容本身由
    `tests/test_freshness_contract.py` 的用例看住，删掉它会让测试失败。
    """
    path = _PKG_DIR / "contract.yaml"
    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    listed = raw.get("report_pipeline_sources") or []
    declared = frozenset(str(item) for item in listed if isinstance(item, str))

    known = set(load_contract().keys())
    unknown = sorted(declared - known)
    if unknown:
        # 拼错的源名会把一个**本该在范围内**的源变成 not_applicable，也就是
        # 把"采集失败"静默降级成"不适用"——必须喊出来。
        logger.warning(
            "[freshness] report_pipeline_sources 含未知源名（拼写错误？）：%s", unknown
        )
    if not declared:
        logger.warning(
            "[freshness] contract.yaml 缺少 report_pipeline_sources，"
            "回退为「全部契约源」（保守：宁可多报数据异常，也不伪造 fresh）"
        )
        return frozenset(known)
    return declared
