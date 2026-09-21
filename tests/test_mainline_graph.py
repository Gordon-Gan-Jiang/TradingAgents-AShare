"""M3 测试：主线结构化输出、候选池过滤、Analyst/Selector 节点、端到端编排。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from tradingagents.agents.utils.mainline_structured import (
    extract_mainline_json,
    extract_selector_json,
    get_mainline_json_instruction,
    get_selector_json_instruction,
    normalize_candidates,
    normalize_mainlines,
)
from tradingagents.graph.mainline_graph import (
    _filter_candidate_stocks,
    build_candidate_pool,
    format_market_pool_for_llm,
    mainline_analyst,
    mainline_stock_selector,
    run_mainline_analysis,
)


class FakeLLM:
    """Fake async LLM: astream yields the fixed output as one chunk."""

    def __init__(self, output: str):
        self.output = output

    async def astream(self, messages):
        for chunk in [SimpleNamespace(content=self.output)]:
            yield chunk


def _fake_pool() -> dict:
    return {
        "trade_date": "2026-08-31",
        "perspective": "short",
        "sources": {"industry_spot": "ths", "concept_spot": None},
        "warnings": ["概念板块涨幅榜不可用"],
        "emotion": {
            "temperature": 60, "regime": "中性", "zt_count": 88,
            "max_lianban": 6, "gate": "normal", "gate_reason": "",
        },
        "breadth": {"up": 3181, "down": 2217, "flat": 152, "total": 5550},
        "benchmark": {"close": 4625.0, "chg_1d": 0.003, "chg_5d": 0.0135},
        "mainline_candidates": [
            {
                "sector_type": "industry", "name": "影视院线", "chg_1d": 6.5,
                "excess_ret_5d": -0.0436, "heat_score": 92.5, "strength_score": 72.5,
                "composite_score": 84.5, "net_inflow_1d": 21.9, "net_inflow_5d": 30.0,
                "inflow_persistent": True, "leader": "华策影视", "hist_available": True,
            }
        ],
        "zt_heat": [{"industry": "通用设备", "zt_count": 7, "max_lianban": 3}],
        "industry_spot": [{"name": "影视院线", "chg_1d": 6.5}],
        "concept_spot": None,
        "macro_brief": "宏观摘要：政策面平稳。",
    }


_ANALYST_OUTPUT = (
    "市场解读：AI 与传媒方向活跃。\n\n"
    '<!-- MAINLINE_JSON: {"market_reading":"AI算力延续强势",'
    '"mainlines":[{"name":"AI算力","type":"concept","phase":"主升","confidence":78,'
    '"status_vs_yesterday":"延续","logic":"海外算力资本开支+国产政策","drivers":["英伟达财报"],'
    '"representative_boards":[{"board":"CPO概念","chg_1d":3.2}],"leading_stocks":["中际旭创"],'
    '"verify_conditions":["明日板块指数不创新高且资金流出则证伪"],"risks":["高位拥挤"],'
    '"evidence":"CPO概念当日涨幅3.2%，净流入居前"}],"watchlist":["低空经济"],"data_gaps":[]} -->'
)

_SELECTOR_OUTPUT = (
    "选股结论：AI 算力聚焦龙头。\n\n"
    '<!-- SELECTOR_JSON: {"candidates":[{"symbol":"300308.SZ","name":"中际旭创",'
    '"mainline":"AI算力","tier":"龙头","score":82,"reasons":["主力净流入居前"],'
    '"entry_hint":"高位不追，等回踩5日线","risk":"估值分位偏高"}]} -->'
)


# ── 结构化提取与规范化 ───────────────────────────────────────────


def test_extract_and_normalize_mainlines():
    data = normalize_mainlines(extract_mainline_json(_ANALYST_OUTPUT))
    assert len(data["mainlines"]) == 1
    m = data["mainlines"][0]
    assert m["name"] == "AI算力"
    assert m["confidence"] == 78
    assert m["phase"] == "主升"
    assert m["status_vs_yesterday"] == "延续"
    assert m["representative_boards"][0]["board"] == "CPO概念"


def test_normalize_mainlines_coerces_enums_and_confidence():
    data = normalize_mainlines(
        {
            "mainlines": [
                {
                    "name": "X",
                    "phase": "胡说八道",
                    "status_vs_yesterday": "bad",
                    "confidence": 0.85,
                }
            ]
        }
    )
    m = data["mainlines"][0]
    assert m["phase"] == ""          # 非法枚举 → 空
    assert m["status_vs_yesterday"] == ""
    assert m["confidence"] == 85     # 0-1 口径 → 0-100


def test_normalize_mainlines_drops_unnamed_and_caps():
    data = normalize_mainlines(
        {"mainlines": [{"name": ""}, {"name": "A"}, {"name": "B"}, {"name": "C"}, {"name": "D"}]}
    )
    assert len(data["mainlines"]) == 3  # 最多 3 条


def test_extract_and_normalize_candidates():
    data = normalize_candidates(extract_selector_json(_SELECTOR_OUTPUT))
    assert len(data) == 1
    c = data[0]
    assert c["symbol"] == "300308.SZ"
    assert c["tier"] == "龙头"
    assert c["score"] == 82


def test_normalize_candidates_rejects_unknown_tier_and_missing_symbol():
    data = normalize_candidates(
        {"candidates": [{"symbol": "000001.SZ", "name": "平安银行", "tier": "x"}, {"name": "no-symbol"}]}
    )
    assert len(data) == 1
    assert data[0]["tier"] == ""


# ── 市场池格式化 ─────────────────────────────────────────────────


def test_format_market_pool_includes_real_data_and_degradation_note():
    text = format_market_pool_for_llm(_fake_pool())
    assert "影视院线" in text
    assert "沪深300" in text
    assert "概念板块涨幅榜" in text and "数据源不可用" in text
    assert "涨停行业热度" in text
    assert "通用设备" in text


# ── 候选池过滤 ───────────────────────────────────────────────────


def test_filter_candidate_stocks():
    df = pd.DataFrame(
        {
            "code": ["600111", "300308", "830799", "000001"],
            "name": ["北方稀土", "中际旭创", "*ST北讯", "平安银行"],
            "latest": [30.0, 100.0, None, 11.0],
            "turnover": [5.0, 3.0, None, 1.0],
        }
    )
    out = _filter_candidate_stocks(df)
    names = set(out["name"])
    assert "中际旭创" in names and "平安银行" in names
    assert "*ST北讯" not in names      # ST 剔除
    assert "830799" not in set(out["code"])  # 北交所剔除


def test_build_candidate_pool_uses_zt_and_cons():
    zt_raw = pd.DataFrame(
        {
            "代码": ["000011", "000560"],
            "名称": ["深物业A", "我爱我家"],
            "涨跌幅": [10.05, 9.85],
            "连板数": [2, 3],
            "所属行业": ["房地产", "房地产"],
            "换手率": [2.1, 2.7],
            "最新价": [10.0, 2.9],
        }
    )
    cons_raw = pd.DataFrame(
        {
            "代码": ["300308", "600111"],
            "名称": ["中际旭创", "北方稀土"],
            "最新价": [100.0, 30.0],
            "涨跌幅": [5.0, 3.0],
            "换手率": [3.0, 5.0],
        }
    )
    mainlines = [
        {
            "name": "AI算力",
            "type": "concept",
            "representative_boards": [{"board": "CPO概念", "chg_1d": 3.2}],
        }
    ]
    pool = {"trade_date": "2026-08-31"}
    ak = SimpleNamespace(
        stock_zt_pool_em=MagicMock(return_value=zt_raw),
        stock_board_concept_cons_em=MagicMock(return_value=cons_raw),
    )
    result = build_candidate_pool(mainlines, pool, ak_module=ak)
    assert len(result["zt_pool"]) == 2
    assert result["zt_pool"][0]["lianban"] == 3  # 按连板排序
    assert len(result["by_mainline"]["AI算力"]) == 2
    assert result["by_mainline"]["AI算力"][0]["source"] == "cons"


def test_build_candidate_pool_degrades_when_zt_fails():
    ak = SimpleNamespace(
        stock_zt_pool_em=MagicMock(side_effect=ConnectionError("zt down")),
        stock_board_concept_cons_em=MagicMock(side_effect=ConnectionError("cons down")),
    )
    result = build_candidate_pool([], {"trade_date": "2026-08-31"}, ak_module=ak)
    assert result["zt_pool"] == []
    assert len(result["warnings"]) >= 1


# ── Agent 节点 ───────────────────────────────────────────────────


def test_mainline_analyst_parses_json():
    async def _run():
        return await mainline_analyst(FakeLLM(_ANALYST_OUTPUT), _fake_pool(), config={})

    out = asyncio.run(_run())
    assert out["structured"]["mainlines"][0]["name"] == "AI算力"
    assert "市场解读" in out["report"]


def test_mainline_selector_confidence_gate():
    mainlines = [
        {"name": "强主线", "confidence": 80},
        {"name": "弱主线", "confidence": 40},
    ]
    pool = {"zt_pool": [], "by_mainline": {}, "warnings": []}

    async def _run():
        return await mainline_stock_selector(
            FakeLLM(_SELECTOR_OUTPUT), mainlines, pool, config={}
        )

    out = asyncio.run(_run())
    assert out["gated_out"] == ["弱主线"]
    assert len(out["candidates"]) == 1
    assert out["candidates"][0]["mainline"] == "AI算力"


def test_mainline_selector_all_gated_returns_empty():
    mainlines = [{"name": "弱主线", "confidence": 30}]

    async def _run():
        return await mainline_stock_selector(
            FakeLLM(""), mainlines, {"zt_pool": [], "by_mainline": {}}, config={}
        )

    out = asyncio.run(_run())
    assert out["candidates"] == []
    assert out["gated_out"] == ["弱主线"]


# ── 端到端编排 ───────────────────────────────────────────────────


def test_run_mainline_analysis_end_to_end():
    fake_collector = MagicMock()
    fake_collector.collect.return_value = _fake_pool()

    def fake_build_llm(config, model_key):
        if model_key == "deep_think_llm":
            return FakeLLM(_ANALYST_OUTPUT)
        return FakeLLM(_SELECTOR_OUTPUT)

    def fake_build_pool(mainlines, pool, ak_module):
        return {
            "zt_pool": [{"code": "300308.SZ", "name": "中际旭创", "chg_1d": 5.0, "lianban": 1, "industry": "", "turnover": 3.0}],
            "by_mainline": {"AI算力": []},
            "warnings": [],
        }

    with patch(
        "tradingagents.graph.mainline_graph._build_llm", side_effect=fake_build_llm
    ), patch(
        "tradingagents.graph.mainline_graph.build_candidate_pool", side_effect=fake_build_pool
    ), patch(
        "tradingagents.graph.mainline_graph.validate_llm_config", return_value=None  # mock 场景跳过校验
    ):
        result = asyncio.run(
            run_mainline_analysis(
                "2026-08-31", "short", market_collector=fake_collector, include_breadth=False
            )
        )
    assert result["trade_date"] == "2026-08-31"
    assert len(result["mainlines"]) == 1
    assert result["mainlines"][0]["name"] == "AI算力"
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["symbol"] == "300308.SZ"
    assert result["market"]["emotion"]["temperature"] == 60
    assert isinstance(result["warnings"], list)


# ── 审查修复：市值单位（元→亿）、market_snapshot 规则层特征 ──────


def test_build_candidate_pool_total_mv_converted_to_100m():
    from tradingagents.graph.mainline_graph import build_candidate_pool, _opt_mv_100m

    # 市值 2.5e11 元 → 2500 亿
    assert _opt_mv_100m(2.5e11) == 2500.0
    assert _opt_mv_100m(None) is None

    zt_raw = pd.DataFrame(
        {"代码": ["000011"], "名称": ["深物业A"], "涨跌幅": [10.05], "连板数": [2], "所属行业": ["房地产"], "换手率": [2.1], "最新价": [10.0]}
    )
    cons_raw = pd.DataFrame(
        {"代码": ["300308"], "名称": ["中际旭创"], "最新价": [100.0], "涨跌幅": [5.0], "换手率": [3.0], "总市值": [2.5e11]}
    )
    ak = SimpleNamespace(
        stock_zt_pool_em=MagicMock(return_value=zt_raw),
        stock_board_concept_cons_em=MagicMock(return_value=cons_raw),
    )
    result = build_candidate_pool(
        [{"name": "AI算力", "type": "concept", "representative_boards": [{"board": "CPO概念"}]}],
        {"trade_date": "2026-08-31"},
        ak_module=ak,
    )
    cons_row = result["by_mainline"]["AI算力"][0]
    assert cons_row["total_mv"] == 2500.0  # 亿，而非 250000000000
