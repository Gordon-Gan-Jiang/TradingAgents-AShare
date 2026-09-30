"""TRADE_PLAN 块必须从**真正携带它的那份文档**解析。

背景（见 docs/report-judgment-accuracy-audit.md §2.4）
---------------------------------------------------
`compose_trade_plan` 写的是::

    parse_trade_plan_block(decision_text or trader_plan_text)

`or` 只在**字符串为空**时才回退，而决定性的条件是「这份文档里到底有没有块」。
生产库里实测：57 个 TRADE_PLAN 块**全部**写在 `trader_investment_plan` 里，
**0 个**在 `final_trade_decision` 里；而 `final_trade_decision` 总是非空
（它有最终裁决正文）。于是块永远读不到 ——
`trade_plans` 表里 `source='trade_plan_block'` 的行数为 **0**，
56 行的 `direction` 全部为 NULL（全部由更低优先级的「逻辑推导」兜出来）。

本测试用**逐字取自生产库**的两个真实块作为素材。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.database import Base
from api.services import trade_plan_service as tps

# --- 逐字取自生产库的真实块（均位于 trader_investment_plan，final_trade_decision 非空）---

REAL_BLOCK_A = """<!-- TRADE_PLAN
direction: BUY
horizon_days: 10
entry_low: 21.40
entry_high: 23.00
position_cap_pct: 40
first_tranche_pct: 20
hard_stop_price: 21.60
take_profit_ladder: 24.50:50, 25.10:50
trailing_stop_pct: 5
time_stop_days: 10
invalidation_conditions: 主力2日累计净流出超5亿 | 跌破21.6元支撑 | 汽车板块大幅走弱
"""

REAL_BLOCK_B = """<!-- TRADE_PLAN
direction: BUY
horizon_days: 14
entry_low: 21.50
entry_high: 22.50
position_cap_pct: 40
first_tranche_pct: 20
hard_stop_price: 21.46
take_profit_ladder: 24.50:50, 26.00:50
trailing_stop_pct: 0
time_stop_days: 10
invalidation_conditions: 连续2日主力净流出超5亿 | 汽车板块单日流出超10亿 | 放量长上影滞涨超2根
"""

# 真实的最终裁决正文：非空，但不含 TRADE_PLAN 块
DECISION_TEXT_NO_BLOCK = (
    "风控委员会最终裁决：\n"
    "最终交易建议：买入\n"
    "本标的短线量价配合良好，建议轻仓参与，严格止损。\n"
    "<!-- VERDICT: {\"direction\": \"看多\", \"reason\": \"量价配合\", \"confidence\": 72} -->"
)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


# ---------------------------------------------------------------------------
# 1. 块只在 trader_plan_text 里 —— 必须解析出来
# ---------------------------------------------------------------------------


def test_block_in_trader_plan_is_parsed_when_decision_text_is_non_empty():
    plan, source, _warnings = tps.compose_trade_plan(
        decision_text=DECISION_TEXT_NO_BLOCK,
        trader_plan_text=REAL_BLOCK_A,
    )
    assert source == tps.SOURCE_BLOCK, (
        "块写在 trader_investment_plan 里时必须按机读块解析；"
        "此前 final_trade_decision 非空就把块彻底遮蔽了"
    )
    assert plan.get("direction") == "BUY"
    assert plan.get("horizon_days") == 10
    assert plan.get("entry_low") == 21.40
    assert plan.get("entry_high") == 23.00
    assert plan.get("hard_stop_price") == 21.60


@pytest.mark.parametrize("block", [REAL_BLOCK_A, REAL_BLOCK_B])
def test_real_corpus_blocks_are_parsed(block):
    """两个真实块都要能解析出方向与止盈阶梯。"""
    plan, source, _warnings = tps.compose_trade_plan(
        decision_text=DECISION_TEXT_NO_BLOCK,
        trader_plan_text=block,
    )
    assert source == tps.SOURCE_BLOCK
    assert plan.get("direction") == "BUY"
    ladder = plan.get("take_profit_ladder")
    assert ladder, "真实块带 take_profit_ladder，必须解析出来"


def test_ladder_entries_survive():
    plan, _source, _warnings = tps.compose_trade_plan(
        decision_text=DECISION_TEXT_NO_BLOCK,
        trader_plan_text=REAL_BLOCK_A,
    )
    ladder = plan.get("take_profit_ladder")
    # 阶梯是「价格:比例」形式，两个档位都必须保留
    assert len(ladder) == 2
    assert 24.50 in [float(item[0]) for item in ladder]


# ---------------------------------------------------------------------------
# 2. 优先级：两份文档都有块时，decision_text 优先
# ---------------------------------------------------------------------------


def test_decision_text_wins_when_both_carry_a_block():
    plan, source, _warnings = tps.compose_trade_plan(
        decision_text=REAL_BLOCK_B,
        trader_plan_text=REAL_BLOCK_A,
    )
    assert source == tps.SOURCE_BLOCK
    assert plan.get("horizon_days") == 14, "两份都有块时以 final_trade_decision 为准"


# ---------------------------------------------------------------------------
# 3. 既有行为不得回退
# ---------------------------------------------------------------------------


def test_block_in_decision_text_still_works():
    plan, source, _warnings = tps.compose_trade_plan(
        decision_text=REAL_BLOCK_A,
        trader_plan_text="",
    )
    assert source == tps.SOURCE_BLOCK
    assert plan.get("direction") == "BUY"


def test_no_block_anywhere_falls_back_without_error():
    plan, source, _warnings = tps.compose_trade_plan(
        decision_text=DECISION_TEXT_NO_BLOCK,
        trader_plan_text="交易员计划正文，没有机读块。",
        target_price=25.0,
        stop_loss_price=20.0,
    )
    assert source != tps.SOURCE_BLOCK
    assert plan.get("target_price") == 25.0


def test_empty_both_documents_is_safe():
    plan, source, warnings = tps.compose_trade_plan(decision_text="", trader_plan_text="")
    assert isinstance(plan, dict)
    assert isinstance(warnings, list)


# ---------------------------------------------------------------------------
# 4. 落库时 raw_text 也必须能取到块
# ---------------------------------------------------------------------------


def test_persist_captures_raw_block_from_trader_plan(db):
    row, plan, source, _warnings = tps.persist_plan_from_analysis(
        db,
        user_id=1,
        report_id=1,
        symbol="600519.SH",
        name="贵州茅台",
        signal_trade_date="2026-09-29",
        decision_text=DECISION_TEXT_NO_BLOCK,
        trader_plan_text=REAL_BLOCK_A,
    )
    assert row is not None
    assert source == tps.SOURCE_BLOCK
    # raw_text 是「块原文」的留存，用于人工复核；此前因同一 `or` 短路而恒为 None
    assert row.raw_text, "必须留存 TRADE_PLAN 块原文"
    assert "TRADE_PLAN" in row.raw_text
    assert "hard_stop_price" in row.raw_text
