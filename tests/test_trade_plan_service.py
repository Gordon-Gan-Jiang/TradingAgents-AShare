"""交易计划结构化（P0-2）单元测试。

覆盖：机读块解析、三来源合并优先级、计划自洽性校验、落库与顶替、监控计划选取，
以及"提示词与解析器不漂移"的护栏。
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.database import Base
from api.services import trade_plan_service as tps
from tradingagents.prompts.zh import PROMPTS


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


# 提示词与解析器共同约定的规范键名（防漂移护栏使用）
CANONICAL_PLAN_KEYS = [
    "direction", "horizon_days", "entry_low", "entry_high", "position_cap_pct",
    "first_tranche_pct", "hard_stop_price", "take_profit_ladder",
    "trailing_stop_pct", "time_stop_days", "invalidation_conditions",
]

WELL_FORMED_BLOCK = """最终交易建议：买入

<!-- TRADE_PLAN
direction: BUY
horizon_days: 20
entry_low: 12.30
entry_high: 12.90
position_cap_pct: 9
first_tranche_pct: 2
hard_stop_price: 11.50
take_profit_ladder: 13.50:30, 14.80:40, 16.00:30
trailing_stop_pct: 8
time_stop_days: 10
invalidation_conditions: 跌破20日线 | 商誉减值超5亿 | 毛利率低于15%
-->
"""


class TestBlockParsing:
    def test_parses_all_fields(self):
        plan, warnings = tps.parse_trade_plan_block(WELL_FORMED_BLOCK)
        assert plan["direction"] == "BUY"
        assert plan["horizon_days"] == 20
        assert plan["entry_low"] == pytest.approx(12.30)
        assert plan["entry_high"] == pytest.approx(12.90)
        assert plan["position_cap_pct"] == pytest.approx(9.0)
        assert plan["first_tranche_pct"] == pytest.approx(2.0)
        assert plan["hard_stop_price"] == pytest.approx(11.50)
        assert plan["take_profit_ladder"] == [[13.5, 30.0], [14.8, 40.0], [16.0, 30.0]]
        assert plan["trailing_stop_pct"] == pytest.approx(8.0)
        assert plan["time_stop_days"] == 10
        assert plan["invalidation_conditions"] == ["跌破20日线", "商誉减值超5亿", "毛利率低于15%"]
        assert warnings == []

    @pytest.mark.parametrize(
        "direction,expected",
        [
            ("BUY", "BUY"), ("buy", "BUY"), ("买入", "BUY"), ("看多", "BUY"), ("加仓", "BUY"),
            ("SELL", "SELL"), ("卖出", "SELL"), ("减仓", "SELL"), ("看空", "SELL"),
            ("HOLD", "HOLD"), ("观望", "HOLD"), ("持有", "HOLD"), ("中性", "HOLD"),
        ],
    )
    def test_direction_normalization(self, direction, expected):
        plan, _ = tps.parse_trade_plan_block(f"<!-- TRADE_PLAN\ndirection: {direction}\n-->")
        assert plan["direction"] == expected

    def test_fullwidth_colon_and_currency_and_comma(self):
        plan, _ = tps.parse_trade_plan_block(
            "<!-- TRADE_PLAN\n"
            "direction：买入\n"
            "hard_stop_price: ￥11.5元\n"
            "entry_range: 1,234.5-1,290.0\n"
            "-->"
        )
        assert plan["direction"] == "BUY"
        assert plan["hard_stop_price"] == pytest.approx(11.5)
        assert plan["entry_low"] == pytest.approx(1234.5)
        assert plan["entry_high"] == pytest.approx(1290.0)

    def test_decimal_ratio_percent_is_scaled(self):
        plan, _ = tps.parse_trade_plan_block(
            "<!-- TRADE_PLAN\nhard_stop_price: 11.5\nposition_cap_pct: 0.09\n-->"
        )
        assert plan["position_cap_pct"] == pytest.approx(9.0)

    def test_ladder_variants(self):
        for raw, expected in [
            ("13.5:30, 14.8:40", [[13.5, 30.0], [14.8, 40.0]]),
            ("13.5(30%), 14.8 40%", [[13.5, 30.0], [14.8, 40.0]]),
            ("13.5；14.8", [[13.5, 50.0], [14.8, 50.0]]),
        ]:
            plan, _ = tps.parse_trade_plan_block(
                f"<!-- TRADE_PLAN\nhard_stop_price: 11.5\ntake_profit_ladder: {raw}\n-->"
            )
            assert plan["take_profit_ladder"] == expected, raw

    def test_ladder_sorted_and_scaled_when_over_100(self):
        plan, _ = tps.parse_trade_plan_block(
            "<!-- TRADE_PLAN\nhard_stop_price: 11.5\ntake_profit_ladder: 16.0:90, 13.5:90\n-->"
        )
        ladder = plan["take_profit_ladder"]
        assert [p for p, _ in ladder] == [13.5, 16.0]
        assert sum(pct for _, pct in ladder) == pytest.approx(100.0)

    def test_zero_means_not_applicable(self):
        plan, _ = tps.parse_trade_plan_block(
            "<!-- TRADE_PLAN\nhard_stop_price: 0\nentry_low: 0\nentry_high: 0\n-->"
        )
        assert "hard_stop_price" not in plan
        assert "entry_low" not in plan

    def test_null_tokens_ignored(self):
        plan, _ = tps.parse_trade_plan_block(
            "<!-- TRADE_PLAN\nhard_stop_price: 无\ntrailing_stop_pct: N/A\n-->"
        )
        assert plan == {}

    def test_missing_block_returns_empty_without_raising(self):
        plan, warnings = tps.parse_trade_plan_block("只有一段普通的中文决策文本，没有机读块。")
        assert plan == {}
        assert warnings == []

    def test_unknown_keys_ignored(self):
        plan, _ = tps.parse_trade_plan_block(
            "<!-- TRADE_PLAN\nhard_stop_price: 11.5\nsome_random_key: 3\n-->"
        )
        assert plan["hard_stop_price"] == pytest.approx(11.5)
        assert "some_random_key" not in plan


class TestComposition:
    def test_block_wins_over_extracted(self):
        plan, source, _ = tps.compose_trade_plan(
            decision_text=WELL_FORMED_BLOCK,
            target_price=14.0,
            stop_loss_price=11.0,
        )
        assert source == tps.SOURCE_BLOCK
        assert plan["hard_stop_price"] == pytest.approx(11.50)
        assert plan["take_profit_ladder"] == [[13.5, 30.0], [14.8, 40.0], [16.0, 30.0]]

    def test_extracted_fallback(self):
        """抽取回退只写报告真给出的字段：单一目标价存 target_price，不包成阶梯。

        历史实现存 ``[[14.0, 100.0]]``，让一个抽取数字在库里长得像模型提出的
        分批止盈方案——下游据此展示"止盈阶梯"，但根本没有阶梯。
        """
        plan, source, _ = tps.compose_trade_plan(target_price=14.0, stop_loss_price=11.0)
        assert source == tps.SOURCE_EXTRACTED
        assert plan["hard_stop_price"] == pytest.approx(11.0)
        assert plan["target_price"] == pytest.approx(14.0)
        assert "take_profit_ladder" not in plan
        assert tps.is_monitorable(plan)
        # 单一目标价仍然是可监控的止盈位（唯一一档、一次性 100%）
        assert tps.target_levels(plan) == [(14.0, 100.0)]

    def test_risk_feedback_is_merged_not_discarded(self):
        plan, _, _ = tps.compose_trade_plan(
            decision_text=WELL_FORMED_BLOCK,
            risk_feedback_state={
                "de_risk_triggers": ["单日跌幅超5%"],
                "execution_preconditions": ["站稳20日线"],
                "hard_constraints": ["总仓不超10%"],
            },
        )
        assert plan["de_risk_triggers"] == ["单日跌幅超5%"]
        assert plan["execution_preconditions"] == ["站稳20日线"]
        assert plan["hard_constraints"] == ["总仓不超10%"]

    def test_derived_only_is_not_monitorable(self):
        plan, source, _ = tps.compose_trade_plan(
            risk_feedback_state={"hard_constraints": ["总仓不超10%"]}
        )
        assert source == tps.SOURCE_DERIVED
        assert not tps.is_monitorable(plan)

    def test_empty_sources_reported(self):
        plan, _, warnings = tps.compose_trade_plan()
        assert plan == {}
        assert any("均未产出" in w for w in warnings)


class TestValidation:
    def test_stop_above_entry_is_flagged(self):
        warnings = tps.validate_plan(
            {"direction": "BUY", "entry_low": 12.0, "hard_stop_price": 12.5}
        )
        assert any("止损无效" in w for w in warnings)

    def test_missing_stop_for_buy_is_flagged(self):
        warnings = tps.validate_plan({"direction": "BUY", "entry_low": 12.0})
        assert any("缺少硬止损" in w for w in warnings)

    def test_first_tranche_exceeding_cap_is_flagged(self):
        warnings = tps.validate_plan(
            {"direction": "BUY", "position_cap_pct": 5.0, "first_tranche_pct": 8.0}
        )
        assert any("超过总仓上限" in w for w in warnings)

    def test_healthy_plan_has_no_warnings(self):
        plan, warnings = tps.parse_trade_plan_block(WELL_FORMED_BLOCK)
        assert tps.validate_plan(plan) == []
        assert warnings == []


class TestPersistence:
    def test_persist_and_read_back(self, db):
        row, plan, source, _ = tps.persist_plan_from_analysis(
            db,
            user_id="u1",
            report_id="r1",
            symbol="002384.SZ",
            name="东山精密",
            signal_trade_date="2026-04-10",
            decision_text=WELL_FORMED_BLOCK,
            risk_feedback_state={"de_risk_triggers": ["商誉减值"]},
            target_price=14.0,
            stop_loss_price=11.0,
            risk_gate="pass",
            confidence=72.0,
            horizon="dual",
        )
        db.commit()
        assert row is not None
        assert source == tps.SOURCE_BLOCK
        assert row.hard_stop_price == pytest.approx(11.50)
        assert row.status == tps.STATUS_ACTIVE

        monitor = tps.get_monitor_plan(db, user_id="u1", symbol="002384.SZ")
        assert monitor is not None
        restored = tps.plan_dict_from_row(monitor)
        assert restored["take_profit_ladder"] == [[13.5, 30.0], [14.8, 40.0], [16.0, 30.0]]
        assert restored["de_risk_triggers"] == ["商誉减值"]
        assert restored["invalidation_conditions"][0] == "跌破20日线"

    def test_upsert_is_idempotent_for_same_report(self, db):
        for _ in range(2):
            tps.persist_plan_from_analysis(
                db, user_id="u1", report_id="r1", symbol="002384.SZ",
                decision_text=WELL_FORMED_BLOCK,
            )
            db.commit()
        rows = db.query(tps.TradePlanDB).filter(tps.TradePlanDB.report_id == "r1").all()
        assert len(rows) == 1

    def test_new_monitorable_plan_supersedes_previous(self, db):
        tps.persist_plan_from_analysis(
            db, user_id="u1", report_id="r1", symbol="002384.SZ",
            decision_text=WELL_FORMED_BLOCK,
        )
        db.commit()
        second_mark = WELL_FORMED_BLOCK.replace("11.50", "10.80")
        tps.persist_plan_from_analysis(
            db, user_id="u1", report_id="r2", symbol="002384.SZ",
            decision_text=second_mark,
        )
        db.commit()

        active = tps.list_active_plans(db, user_id="u1")
        assert len(active) == 1
        assert active[0].report_id == "r2"
        monitor = tps.get_monitor_plan(db, user_id="u1", symbol="002384.SZ")
        assert monitor.report_id == "r2"
        assert monitor.hard_stop_price == pytest.approx(10.80)

    def test_unanchored_new_plan_does_not_displace_anchored_one(self, db):
        """新报告没给止损时，不能把原来可用的止损计划顶掉。"""
        tps.persist_plan_from_analysis(
            db, user_id="u1", report_id="r1", symbol="002384.SZ",
            decision_text=WELL_FORMED_BLOCK,
        )
        db.commit()
        tps.persist_plan_from_analysis(
            db, user_id="u1", report_id="r2", symbol="002384.SZ",
            risk_feedback_state={"hard_constraints": ["总仓不超10%"]},
        )
        db.commit()

        monitor = tps.get_monitor_plan(db, user_id="u1", symbol="002384.SZ")
        assert monitor is not None
        assert monitor.report_id == "r1", "无锚点的新计划不应顶替可监控的旧计划"
        assert monitor.hard_stop_price == pytest.approx(11.50)

    def test_expired_plan_not_returned_as_monitor(self, db):
        row, _, _, _ = tps.persist_plan_from_analysis(
            db, user_id="u1", report_id="r1", symbol="002384.SZ",
            decision_text=WELL_FORMED_BLOCK,
        )
        db.commit()
        tps.set_plan_status(db, plan_id=row.id, status=tps.STATUS_EXPIRED)
        db.commit()
        assert tps.get_monitor_plan(db, user_id="u1", symbol="002384.SZ") is None

    def test_persist_with_no_plan_returns_none(self, db):
        row, plan, _, _ = tps.persist_plan_from_analysis(
            db, user_id="u1", report_id="r9", symbol="000001.SZ"
        )
        assert row is None and plan == {}

    def test_persist_never_raises_on_bad_input(self, db):
        row, _, _, warnings = tps.persist_plan_from_analysis(
            db, user_id=None, report_id="r1", symbol="", decision_text=WELL_FORMED_BLOCK
        )
        assert row is None

    def test_days_elapsed(self):
        plan = {"signal_trade_date": "2026-04-01"}
        assert tps.plan_days_elapsed(plan, "2026-04-11") == 10
        assert tps.plan_days_elapsed(plan, "2026-03-01") == 0
        assert tps.plan_days_elapsed({}, "2026-04-11") is None


class TestPromptContract:
    """护栏：提示词必须仍然要求模型输出 TRADE_PLAN 块与全部规范键名。"""

    def test_trader_prompt_requests_trade_plan_block(self):
        prompt = PROMPTS["trader_system_prompt"]
        assert "TRADE_PLAN" in prompt

    @pytest.mark.parametrize("key", CANONICAL_PLAN_KEYS)
    def test_prompt_declares_every_canonical_key(self, key):
        assert key in PROMPTS["trader_system_prompt"]

    @pytest.mark.parametrize("key", CANONICAL_PLAN_KEYS)
    def test_parser_accepts_every_canonical_key(self, key):
        """模型原样输出规范键名时必须能被解析，否则计划被静默丢弃。"""
        assert tps._KEY_ALIASES.get(key) == key

    @pytest.mark.parametrize("key", CANONICAL_PLAN_KEYS)
    def test_spec_constant_stays_in_sync_with_prompt(self, key):
        """TRADE_PLAN_BLOCK_SPEC 与提示词防漂移。"""
        assert key in tps.TRADE_PLAN_BLOCK_SPEC
        assert key in PROMPTS["trader_system_prompt"]


class TestAnchorBackfill:
    """研报字段回填：把计划块里的锚点写回 target_price / stop_loss_price。

    这是"86% 止盈锚点被丢弃"这个核心缺陷的修复点——结构化提取漏掉的锚点，
    只要模型在计划块里写了数字，就不再丢失。
    """

    def test_backfills_stop_and_target_from_block(self):
        from api.main import _backfill_plan_anchors

        result = {
            "final_trade_decision": WELL_FORMED_BLOCK,
            "target_price": None,
            "stop_loss_price": None,
        }
        _backfill_plan_anchors(result)
        assert result["stop_loss_price"] == pytest.approx(11.50)
        # 目标价取**最高档** 16.0，不是最低档 13.5。阶梯 [[13.5,30],[14.8,40],[16.0,30]]
        # 里的 13.5 是第一档减仓位，把它当目标价会产出"看多 + 目标价低于现价"的记录。
        assert result["target_price"] == pytest.approx(16.0)
        assert result["stop_loss_source"] == "trade_plan"
        assert result["trade_plan_source"] == tps.SOURCE_BLOCK

    def test_falls_back_to_extracted_when_block_absent(self):
        from api.main import _backfill_plan_anchors

        result = {
            "final_trade_decision": "最终交易建议：买入。没有机读块。",
            "target_price": None,
            "stop_loss_price": None,
        }
        _backfill_plan_anchors(result)
        assert result["target_price"] is None

    def test_does_not_overwrite_existing_anchors(self):
        from api.main import _backfill_plan_anchors

        result = {
            "final_trade_decision": WELL_FORMED_BLOCK,
            "target_price": 20.0,
            "stop_loss_price": 9.0,
        }
        _backfill_plan_anchors(result)
        assert result["target_price"] == 20.0
        assert result["stop_loss_price"] == 9.0
        assert "stop_loss_source" not in result

    def test_never_raises_on_empty_result(self):
        from api.main import _backfill_plan_anchors

        _backfill_plan_anchors({})  # 不得抛异常
