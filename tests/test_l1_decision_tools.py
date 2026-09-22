"""L1 决策工具（第二批）：把平台**已有结论**接进工具面。

覆盖五个工具，全部遵守与 `verdict_history_read` / `market_mainline_view` 相同的纪律：

1. **结论来自平台、不可改写**：判定兑现、历史报告结论、平台推荐、跟踪看板、
   模型评测榜——这五样都是平台**算好并落库**的东西。模型的职责是读取与解释。
   让模型"临场生成一份推荐"或"回忆某个模型的准确率"，就是把平台结论与即兴
   编造混为一谈，这也是本平台最不能接受的失败形态。
2. **用户维度不经过模型**：端口在闭包里绑定 user_id，工具参数里**没有** user_id。
   给模型一个 user_id 参数就等于给它越权的可能。
3. **弃权要显式**：没有数据时声明 `unavailable`，而不是返回空列表当成功。
   并且「解析不到结构」与「确实是空」必须走**不同**的路径（前者降级并说明，
   后者只声明该维度没有内容）——把两者混起来会让"读不到"变成"没有风险"。
4. **取数失败降级不抛错**：抛错会让循环记一条工具失败并继续，模型面对空证据
   自由发挥。降级时**不外传上游原文**。
5. **clamp 不用 `x or default`**：`limit=0` 会被 falsy 换成默认值，本仓库已多次
   踩过这类坑，因此逐条断言 `limit=0 → 收窄到 1`。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

# ══ 1. verdict_outcome_lookup：判定后 T+3/T+5 的兑现 ═══════════════════════


class _FakeOutcomePort:
    def __init__(self, rows=None, *, error: Exception | None = None):
        self.rows = rows if rows is not None else []
        self.error = error
        self.calls: list[dict] = []

    def recent_outcomes(self, *, limit, symbol=None):
        self.calls.append({"limit": limit, "symbol": symbol})
        if self.error is not None:
            raise self.error
        return self.rows


OUTCOME_ROWS = [
    {
        "symbol": "600519.SH", "as_of": "2026-09-15", "state": "distribution",
        "state_label": "派发", "horizon_days": 3, "ref_date": "2026-09-20",
        "ret_pct": -3.2, "threshold_hit": True, "evaluable": True, "reason": "hit",
    },
    {
        "symbol": "600519.SH", "as_of": "2026-09-15", "state": "distribution",
        "state_label": "派发", "horizon_days": 5, "ref_date": "2026-09-24",
        "ret_pct": -5.1, "threshold_hit": True, "evaluable": True, "reason": "hit",
    },
    {
        # 停牌/退市这类不可评估必须与"没命中"区分开：把它算进命中率会污染统计
        "symbol": "300750.SZ", "as_of": "2026-09-10", "state": "washout",
        "state_label": "洗盘", "horizon_days": 3, "ref_date": "2026-09-16",
        "ret_pct": None, "threshold_hit": False, "evaluable": False,
        "reason": "suspended",
    },
]


def _outcome_tool(port):
    from tradingagents.tools.outcomes import make_outcome_tools

    return {spec.name: spec for spec in make_outcome_tools(port)}["verdict_outcome_lookup"]


def test_outcome_becomes_citable_evidence_with_horizon_and_date():
    spec = _outcome_tool(_FakeOutcomePort(OUTCOME_ROWS))
    result = spec.handler(limit=10, symbol=None, as_of="2026-09-21")

    assert len(result.evidence) == 3
    first = str(result.evidence[0].value)
    assert "600519.SH" in first, "标的必须可引用"
    assert "T+3" in first, "持有期必须可引用，否则「兑现了吗」没有时间维度"
    assert "-3.2" in first or "-3.20" in first, "兑现收益必须进证据"
    assert "2026-09-15" in first, "判定日必须可引用"


def test_outcome_unevaluable_is_not_reported_as_miss():
    """不可评估 ≠ 未命中。停牌导致的 evaluable=False 不得被读成「判定错了」。"""
    spec = _outcome_tool(_FakeOutcomePort(OUTCOME_ROWS))
    result = spec.handler(limit=10, symbol=None, as_of="2026-09-21")

    third = str(result.evidence[2].value)
    assert "不可评估" in third, "不可评估必须显式说出来"
    assert "300750.SZ" in third


def test_outcome_no_records_is_declared_not_silently_empty():
    spec = _outcome_tool(_FakeOutcomePort([]))
    result = spec.handler(limit=10, symbol=None, as_of="2026-09-21")

    assert result.evidence == ()
    assert "verdict_outcome" in result.coverage.available
    assert result.coverage.unavailable, "没有兑现记录必须声明，不能当成「没问题」"


def test_outcome_read_failure_degrades_without_raising():
    spec = _outcome_tool(_FakeOutcomePort(error=RuntimeError("db locked")))
    result = spec.handler(limit=5, symbol=None, as_of="2026-09-21")

    assert result.degraded is True
    assert result.coverage.unavailable == ("verdict_outcome",)
    assert "db locked" not in str(result.data), "上游原文不进对外结果"
    assert "db locked" not in str(result.degraded_reason)


def test_outcome_limit_is_clamped_including_zero():
    port = _FakeOutcomePort(OUTCOME_ROWS)
    spec = _outcome_tool(port)
    spec.handler(limit=999, symbol=None)
    assert port.calls[0]["limit"] == 30

    spec.handler(limit=0, symbol=None)
    assert port.calls[1]["limit"] == 1, "limit=0 不得被 `or` 换成默认值"


def test_outcome_symbol_filter_is_passed_through():
    port = _FakeOutcomePort(OUTCOME_ROWS)
    _outcome_tool(port).handler(limit=3, symbol="600519.SH")
    assert port.calls[0]["symbol"] == "600519.SH"


def test_outcome_schema_has_no_user_id():
    spec = _outcome_tool(_FakeOutcomePort(OUTCOME_ROWS))
    assert "user_id" not in (spec.input_schema.get("properties") or {})


def test_outcome_is_l1_and_only_registered_with_a_port():
    from tradingagents.tools import build_registry
    from tradingagents.tools.spec import PLAN_LAYER_DECISION

    spec = _outcome_tool(_FakeOutcomePort(OUTCOME_ROWS))
    assert spec.plan_layer == PLAN_LAYER_DECISION
    assert 5000 <= spec.latency_budget_ms <= 15000
    assert spec.freshness_sources == ("verdict_outcome",)

    without = build_registry(ports_factory=lambda *a: object(), news_fetcher=lambda *a: [])
    assert "verdict_outcome_lookup" not in without.names(), "没有端口不该注册"

    with_port = build_registry(
        ports_factory=lambda *a: object(), news_fetcher=lambda *a: [],
        outcome_port=_FakeOutcomePort(OUTCOME_ROWS),
    )
    assert "verdict_outcome_lookup" in with_port.names()
    assert "verdict_outcome_lookup" in with_port.names_by_layer(PLAN_LAYER_DECISION)


def _seed_verdict(user_id, symbol, *, hours_ago, state="distribution"):
    """往测试库塞一条判定（测试库由 conftest 指向一次性临时目录）。"""
    from api.database import SignalVerdictDB, get_db_ctx
    from tradingagents.agent.repository import new_id

    vid = new_id()
    with get_db_ctx() as db:
        db.add(
            SignalVerdictDB(
                id=vid, user_id=user_id, symbol=symbol, as_of="2026-09-15", probe="washout",
                state=state, confidence=66, margin=0.8, signal_version="washout-v1.0.0",
                created_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
            )
        )
        db.commit()
    return vid


def _seed_outcome(verdict_id, horizon_days, *, ret_pct, evaluable=True, hit=True):
    from api.database import SignalVerdictOutcomeDB, get_db_ctx
    from tradingagents.agent.repository import new_id

    with get_db_ctx() as db:
        db.add(
            SignalVerdictOutcomeDB(
                id=new_id(), verdict_id=verdict_id, horizon_days=horizon_days,
                ref_date="2026-09-20", ret_pct=ret_pct, threshold_hit=hit,
                evaluable=evaluable, reason=None,
            )
        )
        db.commit()


def test_outcome_lookup_not_shadowed_by_immature_recent_verdicts():
    """**回归**：更早的判定有兑现记录时，不得因"最近的判定还没成熟"而报无记录。

    旧实现先取最近 N 条判定、再从中筛兑现行——最近的判定都还没成熟时会得到空结果，
    于是工具把"有记录"说成"没有兑现记录"（实测 limit=10 返回空、limit=30 找到 2 条）。
    这与 `verdict_history_read` 的 `verdicts`/`items` 键名事故同类：
    **读不到被说成没有**，而且完全静默。现在分页主体是兑现行，未成熟的判定不占配额。
    """
    from api.services import chat_service

    uid = "outcome-shadow-user"
    for index in range(12):
        verdict_id = _seed_verdict(uid, f"60000{index}.SH", hours_ago=index + 1)
        if index >= 10:  # 最新的 10 条没有兑现行，最早的 2 条有
            _seed_outcome(verdict_id, 5, ret_pct=-5.1)
            _seed_outcome(verdict_id, 3, ret_pct=-3.2)

    registry = chat_service.build_registry(user_id=uid)
    result = registry.invoke("verdict_outcome_lookup", limit=10)

    assert result.data["count"] == 4, "更早的兑现记录必须被发现（2 条判定 × T+3/T+5）"
    assert result.coverage.unavailable == (), "有兑现记录时不得声明「尚无已回填的兑现记录」"
    horizons = [row["horizon_days"] for row in result.data["outcomes"][:2]]
    assert horizons == [3, 5], "同一判定内 T+3 在 T+5 之前（证据顺序即阅读顺序）"
    assert "T+3" in str(result.evidence[0].value)


def test_outcome_lookup_stays_user_scoped():
    """兑现行同样按用户隔离：别人的兑现不得出现在我的证据里。"""
    from api.services import chat_service

    mine = _seed_verdict("outcome-owner", "600519.SH", hours_ago=1)
    _seed_outcome(mine, 3, ret_pct=-3.2)
    theirs = _seed_verdict("outcome-other", "300750.SZ", hours_ago=1)
    _seed_outcome(theirs, 3, ret_pct=9.9)

    registry = chat_service.build_registry(user_id="outcome-owner")
    result = registry.invoke("verdict_outcome_lookup", limit=10)

    symbols = {row["symbol"] for row in result.data["outcomes"]}
    assert symbols == {"600519.SH"}, "user_id 在闭包里绑定，跨用户兑现行必须被过滤掉"


# ══ 2. report_conclusion_lookup：历史报告的结论 ═══════════════════════════

class _FakeReportPort:
    def __init__(self, rows=None, *, error: Exception | None = None):
        self.rows = rows if rows is not None else []
        self.error = error
        self.calls: list[dict] = []

    def recent_reports(self, *, limit, symbol=None):
        self.calls.append({"limit": limit, "symbol": symbol})
        if self.error is not None:
            raise self.error
        return self.rows


REPORT_ROWS = [
    {
        "report_id": "r-1", "symbol": "600519.SH", "trade_date": "2026-09-18",
        "status": "completed", "decision": "BUY", "direction": "看多", "confidence": 78,
        "target_price": 1800.0, "stop_loss_price": 1550.0, "horizon": "short",
        "freshness_status": "ok", "created_at": "2026-09-18T09:30:00",
    },
    {
        "report_id": "r-2", "symbol": "300750.SZ", "trade_date": "2026-09-17",
        "status": "running", "decision": None, "direction": None, "confidence": None,
        "target_price": None, "stop_loss_price": None, "horizon": None,
        "freshness_status": None, "created_at": "2026-09-17T10:00:00",
    },
]


def _report_tool(port):
    from tradingagents.tools.reports import make_report_tools

    return {spec.name: spec for spec in make_report_tools(port)}["report_conclusion_lookup"]


def test_report_conclusion_becomes_citable_evidence():
    spec = _report_tool(_FakeReportPort(REPORT_ROWS))
    result = spec.handler(limit=5, symbol=None, as_of="2026-09-21")

    first = str(result.evidence[0].value)
    assert "600519.SH" in first and "2026-09-18" in first, "日期与标的必须可引用"
    assert "看多" in first, "结论方向必须可引用（这是「结论是什么」的答案本体）"
    assert "78" in first, "置信度必须可引用"
    assert "1800" in first, "目标价必须可引用"


def test_report_still_running_is_not_read_as_a_conclusion():
    """报告还在跑 ≠ 有一份结论。解析不到结论必须说出来，不能被当成交集为空。"""
    spec = _report_tool(_FakeReportPort(REPORT_ROWS))
    result = spec.handler(limit=5, symbol=None, as_of="2026-09-21")

    second = str(result.evidence[1].value)
    assert "尚未生成" in second, "未完成的报告不得被读成一个结论"
    assert "300750.SZ" in second


def test_report_decision_only_is_still_a_conclusion():
    """只有 decision（BUY/SELL/HOLD）、direction 为空时，那也是**结论**。

    实测库里大量历史报告就是这种形状（direction 是后来才补的列）。把它渲染成
    「结论未提供」会让一个真实的卖出结论在对话里消失。
    """
    rows = [dict(REPORT_ROWS[0], direction=None, decision="SELL", target_price=None,
                 stop_loss_price=None, confidence=None)]
    spec = _report_tool(_FakeReportPort(rows))
    result = spec.handler(limit=1, symbol=None, as_of="2026-09-21")

    value = str(result.evidence[0].value)
    assert "SELL" in value, "decision 本身就是结论"
    assert "结论未提供" not in value


def test_report_no_records_is_declared_not_silently_empty():
    spec = _report_tool(_FakeReportPort([]))
    result = spec.handler(limit=5, symbol=None, as_of="2026-09-21")

    assert result.evidence == ()
    assert "report_conclusion" in result.coverage.available
    assert result.coverage.unavailable, "没有历史报告必须声明"


def test_report_read_failure_degrades_without_raising():
    spec = _report_tool(_FakeReportPort(error=RuntimeError("db down")))
    result = spec.handler(limit=5, symbol=None, as_of="2026-09-21")

    assert result.degraded is True
    assert result.coverage.unavailable == ("report_conclusion",)
    assert "db down" not in str(result.data)
    assert "db down" not in str(result.degraded_reason)


def test_report_limit_is_clamped_including_zero():
    port = _FakeReportPort(REPORT_ROWS)
    spec = _report_tool(port)
    spec.handler(limit=999, symbol=None)
    assert port.calls[0]["limit"] == 20

    spec.handler(limit=0, symbol=None)
    assert port.calls[1]["limit"] == 1, "limit=0 不得被 `or` 换成默认值"


def test_report_schema_has_no_user_id():
    spec = _report_tool(_FakeReportPort(REPORT_ROWS))
    assert "user_id" not in (spec.input_schema.get("properties") or {})


def test_report_is_l1_and_only_registered_with_a_port():
    from tradingagents.tools import build_registry
    from tradingagents.tools.spec import PLAN_LAYER_DECISION

    spec = _report_tool(_FakeReportPort(REPORT_ROWS))
    assert spec.plan_layer == PLAN_LAYER_DECISION
    assert 5000 <= spec.latency_budget_ms <= 15000
    assert spec.freshness_sources == ("report_conclusion",)

    without = build_registry(ports_factory=lambda *a: object(), news_fetcher=lambda *a: [])
    assert "report_conclusion_lookup" not in without.names()

    with_port = build_registry(
        ports_factory=lambda *a: object(), news_fetcher=lambda *a: [],
        report_port=_FakeReportPort(REPORT_ROWS),
    )
    assert "report_conclusion_lookup" in with_port.names_by_layer(PLAN_LAYER_DECISION)


# ══ 3. recommendation_read：平台**已生成**的推荐结果 ═══════════════════════


class _FakeRecommendationPort:
    def __init__(self, payload=None, *, error: Exception | None = None):
        self.payload = payload
        self.error = error
        self.calls: list[int] = []

    def list_recommendations(self, *, limit: int):
        self.calls.append(limit)
        if self.error is not None:
            raise self.error
        return self.payload


RECOMMENDATION_PAYLOAD = {
    "source": "persisted",
    "score_profile": "ashare_robust",
    "generated_at": "2026-09-21T09:35:00",
    "items": [
        {
            "symbol": "600519.SH", "name": "贵州茅台", "rank": 1, "score": 82.5,
            "reasons": ["当日涨幅较强（+2.30%）", "成交额活跃（>=10亿）"],
            "strategy_hits": ["strong_momentum"], "risk_flags": [],
            "quote": {"price": 1700.0, "change_pct": 2.3},
            "feedback_status": "evaluated", "feedback_horizon_days": 5,
            "realized_return_pct": 3.2, "entry_price": 1700.0,
            "source_mode": "market_scan",
        },
        {
            "symbol": "000001.SZ", "name": "平安银行", "rank": 2, "score": 71.0,
            "reasons": ["成交额活跃"], "strategy_hits": [], "risk_flags": ["高换手"],
            "quote": {"price": 11.2, "change_pct": -0.4},
            "feedback_status": "pending", "feedback_horizon_days": 5,
            "realized_return_pct": None, "entry_price": 11.2,
            "source_mode": "market_scan",
        },
    ],
}


def _recommendation_tool(port):
    from tradingagents.tools.recommendations import make_recommendation_tools

    return {
        spec.name: spec
        for spec in make_recommendation_tools(port)
    }["recommendation_read"]


def test_recommendation_becomes_citable_evidence():
    spec = _recommendation_tool(_FakeRecommendationPort(RECOMMENDATION_PAYLOAD))
    result = spec.handler(limit=5, as_of="2026-09-21")

    # 两条推荐各一条证据，外加一条批次口径（不说生成时间的推荐无法判断新鲜度）
    assert len(result.evidence) == 3
    first = str(result.evidence[0].value)
    assert "600519.SH" in first, "标的必须可引用"
    assert "82.5" in first, "平台给出的得分必须可引用"
    assert "当日涨幅较强" in first, "入选理由必须可引用（否则推荐无法被解释）"
    assert "2026-09-21T09:35:00" in str(result.evidence[2].value), "批次生成时间必须可引用"


def test_recommendation_evaluated_and_pending_are_distinguished():
    """已兑现与待观察必须分开：把 pending 读成"已验证有效"是编造。"""
    spec = _recommendation_tool(_FakeRecommendationPort(RECOMMENDATION_PAYLOAD))
    result = spec.handler(limit=5, as_of="2026-09-21")

    first = str(result.evidence[0].value)
    second = str(result.evidence[1].value)
    assert "3.2" in first, "已评估的兑现收益必须可引用"
    assert "待评估" in second or "待观察" in second, "未评估必须显式说明"


def test_recommendation_no_results_is_declared_not_silently_empty():
    spec = _recommendation_tool(
        _FakeRecommendationPort({"source": "persisted", "items": []})
    )
    result = spec.handler(limit=5, as_of="2026-09-21")

    assert result.evidence == ()
    assert "recommendation" in result.coverage.available
    assert result.coverage.unavailable, "平台没有推荐结果必须声明"


def test_recommendation_unparsable_payload_degrades_not_empty():
    """「解析不到结构」与「确实是空」必须分开：前者降级并说明。"""
    spec = _recommendation_tool(_FakeRecommendationPort("not-a-mapping"))
    result = spec.handler(limit=5, as_of="2026-09-21")

    assert result.degraded is True
    assert result.coverage.unavailable == ("recommendation",)
    assert "not-a-mapping" not in str(result.data), "上游原文不进对外结果"


def test_recommendation_read_failure_degrades_without_raising():
    spec = _recommendation_tool(_FakeRecommendationPort(error=RuntimeError("scan down")))
    result = spec.handler(limit=5, as_of="2026-09-21")

    assert result.degraded is True
    assert result.coverage.unavailable == ("recommendation",)
    assert "scan down" not in str(result.data)
    assert "scan down" not in str(result.degraded_reason)


def test_recommendation_limit_is_clamped_including_zero():
    port = _FakeRecommendationPort(RECOMMENDATION_PAYLOAD)
    spec = _recommendation_tool(port)
    spec.handler(limit=999)
    assert port.calls[0] == 20

    spec.handler(limit=0)
    assert port.calls[1] == 1, "limit=0 不得被 `or` 换成默认值"


def test_recommendation_schema_has_no_user_id():
    spec = _recommendation_tool(_FakeRecommendationPort(RECOMMENDATION_PAYLOAD))
    assert "user_id" not in (spec.input_schema.get("properties") or {})


def test_recommendation_is_l1_and_only_registered_with_a_port():
    from tradingagents.tools import build_registry
    from tradingagents.tools.spec import PLAN_LAYER_DECISION

    spec = _recommendation_tool(_FakeRecommendationPort(RECOMMENDATION_PAYLOAD))
    assert spec.plan_layer == PLAN_LAYER_DECISION
    assert 5000 <= spec.latency_budget_ms <= 15000
    assert spec.freshness_sources == ("recommendation",)

    without = build_registry(ports_factory=lambda *a: object(), news_fetcher=lambda *a: [])
    assert "recommendation_read" not in without.names()

    with_port = build_registry(
        ports_factory=lambda *a: object(), news_fetcher=lambda *a: [],
        recommendation_port=_FakeRecommendationPort(RECOMMENDATION_PAYLOAD),
    )
    assert "recommendation_read" in with_port.names_by_layer(PLAN_LAYER_DECISION)


# ══ 4. tracking_board_read：跟踪看板 ══════════════════════════════════════


class _FakeTrackingPort:
    def __init__(self, payload=None, *, error: Exception | None = None):
        self.payload = payload
        self.error = error
        self.calls: int = 0

    def tracking_board(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.payload


TRACKING_PAYLOAD = {
    "previous_trade_date": "2026-09-18",
    "refresh_interval_seconds": 20,
    "items": [
        {
            "symbol": "600519.SH", "name": "贵州茅台", "current_position": 100.0,
            "available_position": 100.0, "average_cost": 1650.0, "market_value": 170000.0,
            "current_position_pct": 12.3, "live_price": 1700.0, "floating_pnl": 5000.0,
            "floating_pnl_pct": 3.03, "price_change_pct": 1.2,
            "analysis": {"report_id": "r-1", "trade_date": "2026-09-18",
                         "decision": "BUY", "direction": "看多",
                         "trader_advice_summary": "回踩不破可加仓"},
        },
        {
            "symbol": "000001.SZ", "name": "平安银行", "current_position": 1000.0,
            "available_position": 0.0, "average_cost": 12.0, "market_value": 11200.0,
            "current_position_pct": 0.8, "live_price": 11.2, "floating_pnl": -800.0,
            "floating_pnl_pct": -6.67, "price_change_pct": -0.4,
            "analysis": None,
        },
    ],
}


def _tracking_tool(port):
    from tradingagents.tools.tracking import make_tracking_tools

    return {spec.name: spec for spec in make_tracking_tools(port)}["tracking_board_read"]


def test_tracking_board_becomes_citable_evidence():
    spec = _tracking_tool(_FakeTrackingPort(TRACKING_PAYLOAD))
    result = spec.handler(limit=20, as_of="2026-09-21")

    assert len(result.evidence) == 2
    first = str(result.evidence[0].value)
    assert "600519.SH" in first, "标的必须可引用"
    assert "1700" in first, "现价必须可引用"
    assert "5000" in first or "3.03" in first, "浮动盈亏必须可引用"
    assert "看多" in first, "跟踪池上的最新分析结论必须可引用"


def test_tracking_board_missing_analysis_is_declared_per_item():
    """没有分析结论 ≠ 看多/看空。逐条声明，不能留空让模型自己补。"""
    spec = _tracking_tool(_FakeTrackingPort(TRACKING_PAYLOAD))
    result = spec.handler(limit=20, as_of="2026-09-21")

    second = str(result.evidence[1].value)
    assert "000001.SZ" in second
    assert "无分析结论" in second or "尚未" in second


def test_tracking_board_empty_pool_is_declared_not_silently_empty():
    spec = _tracking_tool(_FakeTrackingPort({"items": [], "previous_trade_date": "2026-09-18"}))
    result = spec.handler(limit=20, as_of="2026-09-21")

    assert result.evidence == ()
    assert "tracking_board" in result.coverage.available
    assert result.coverage.unavailable, "跟踪池为空必须声明（空仓与没导入是两件事）"


def test_tracking_board_unparsable_payload_degrades_not_empty():
    spec = _tracking_tool(_FakeTrackingPort(["not", "a", "mapping"]))
    result = spec.handler(limit=20, as_of="2026-09-21")

    assert result.degraded is True
    assert result.coverage.unavailable == ("tracking_board",)


def test_tracking_board_read_failure_degrades_without_raising():
    spec = _tracking_tool(_FakeTrackingPort(error=RuntimeError("quote timeout")))
    result = spec.handler(limit=20, as_of="2026-09-21")

    assert result.degraded is True
    assert result.coverage.unavailable == ("tracking_board",)
    assert "quote timeout" not in str(result.data)
    assert "quote timeout" not in str(result.degraded_reason)


def test_tracking_board_limit_is_clamped_including_zero():
    from tradingagents.tools.tracking import MAX_EVIDENCE as TRACKING_MAX

    many = {
        "previous_trade_date": "2026-09-18",
        "items": [
            {"symbol": f"60000{i}.SH", "name": f"标的{i}", "current_position": 100.0,
             "available_position": 100.0, "average_cost": 10.0, "market_value": 1000.0,
             "current_position_pct": 1.0, "live_price": 10.0, "floating_pnl": 0.0,
             "floating_pnl_pct": 0.0, "price_change_pct": 0.0, "analysis": None}
            for i in range(35)
        ],
    }
    spec = _tracking_tool(_FakeTrackingPort(many))

    big = spec.handler(limit=999, as_of="2026-09-21")
    assert len(big.evidence) == TRACKING_MAX == 30, "上限必须被收窄到 30"

    # limit=0 必须收窄到 1，而不是回退到默认 20（`x or 20` 正是这里的坑）
    zero = spec.handler(limit=0, as_of="2026-09-21")
    assert len(zero.evidence) == 1, "limit=0 不得被 `or` 换成默认值"


def test_tracking_board_schema_has_no_user_id():
    spec = _tracking_tool(_FakeTrackingPort(TRACKING_PAYLOAD))
    assert "user_id" not in (spec.input_schema.get("properties") or {})


def test_tracking_board_is_l1_and_only_registered_with_a_port():
    from tradingagents.tools import build_registry
    from tradingagents.tools.spec import PLAN_LAYER_DECISION

    spec = _tracking_tool(_FakeTrackingPort(TRACKING_PAYLOAD))
    assert spec.plan_layer == PLAN_LAYER_DECISION
    assert 5000 <= spec.latency_budget_ms <= 15000
    assert spec.freshness_sources == ("tracking_board",)

    without = build_registry(ports_factory=lambda *a: object(), news_fetcher=lambda *a: [])
    assert "tracking_board_read" not in without.names()

    with_port = build_registry(
        ports_factory=lambda *a: object(), news_fetcher=lambda *a: [],
        tracking_port=_FakeTrackingPort(TRACKING_PAYLOAD),
    )
    assert "tracking_board_read" in with_port.names_by_layer(PLAN_LAYER_DECISION)


# ══ 5. model_arena_leaderboard：模型对比榜 ════════════════════════════════


class _FakeArenaPort:
    def __init__(self, payload=None, *, error: Exception | None = None):
        self.payload = payload
        self.error = error
        self.calls: list[dict] = []

    def leaderboard(self, *, limit: int, days: int, scope: str):
        self.calls.append({"limit": limit, "days": days, "scope": scope})
        if self.error is not None:
            raise self.error
        return self.payload


ARENA_PAYLOAD = {
    "window_start": "2026-06-23",
    "window_end": "2026-09-21",
    "scope": "portfolio",
    "total_samples": 42,
    "generated_at": "2026-09-21",
    "leaderboard": [
        {
            "rank": 1, "model_key": "model:deepseek-v4", "model_display_name": "deepseek-v4",
            "llm_provider": "deepseek", "sample_count": 24, "accuracy_pct": 61.9,
            "win_rate_pct": 55.0, "avg_return_t1_pct": 0.85, "return_volatility": 1.2,
            "composite_score": 45.6,
        },
        {
            "rank": 2, "model_key": "model:gpt-5", "model_display_name": "gpt-5",
            "llm_provider": "openai", "sample_count": 18, "accuracy_pct": 55.6,
            "win_rate_pct": 50.0, "avg_return_t1_pct": 0.2, "return_volatility": 1.5,
            "composite_score": 38.1,
        },
    ],
}


def _arena_tool(port):
    from tradingagents.tools.arena import make_arena_tools

    return {spec.name: spec for spec in make_arena_tools(port)}["model_arena_leaderboard"]


def test_arena_leaderboard_becomes_citable_evidence():
    spec = _arena_tool(_FakeArenaPort(ARENA_PAYLOAD))
    result = spec.handler(limit=5, days=90, as_of="2026-09-21")

    first = str(result.evidence[0].value)
    assert "deepseek-v4" in first, "模型名必须可引用（否则「哪个模型更准」无从回答）"
    assert "61.9" in first, "准确率必须可引用"
    assert "24" in first, "样本量必须可引用（样本量不说，准确率不可信）"


def test_arena_leaderboard_states_the_evaluation_window():
    """准确率必须带窗口口径：不说窗口的准确率是不可复核的。"""
    spec = _arena_tool(_FakeArenaPort(ARENA_PAYLOAD))
    result = spec.handler(limit=5, days=90, as_of="2026-09-21")

    joined = " ".join(str(item.value) for item in result.evidence)
    assert "2026-06-23" in joined and "2026-09-21" in joined, "评测窗口必须可引用"


def test_arena_leaderboard_empty_window_is_declared_not_silently_empty():
    spec = _arena_tool(
        _FakeArenaPort({"leaderboard": [], "total_samples": 0, "scope": "portfolio"})
    )
    result = spec.handler(limit=5, days=90, as_of="2026-09-21")

    assert result.evidence == ()
    assert "model_arena" in result.coverage.available
    assert result.coverage.unavailable, "窗口内没有样本必须声明，不能编一个排名"


def test_arena_leaderboard_read_failure_degrades_without_raising():
    spec = _arena_tool(_FakeArenaPort(error=RuntimeError("db exploded")))
    result = spec.handler(limit=5, days=90, as_of="2026-09-21")

    assert result.degraded is True
    assert result.coverage.unavailable == ("model_arena",)
    assert "db exploded" not in str(result.data)
    assert "db exploded" not in str(result.degraded_reason)


def test_arena_clamps_limit_and_days_including_zero():
    port = _FakeArenaPort(ARENA_PAYLOAD)
    spec = _arena_tool(port)

    spec.handler(limit=999, days=99999)
    assert port.calls[0] == {"limit": 20, "days": 400, "scope": "all"}

    spec.handler(limit=0, days=0)
    assert port.calls[1] == {"limit": 1, "days": 7, "scope": "all"}, (
        "limit=0 / days=0 不得被 `or` 换成默认值"
    )


def test_arena_scope_is_restricted_to_platform_values():
    """口径只接受平台支持的取值：非法值收敛到默认，而不是原样透传给服务。"""
    port = _FakeArenaPort(ARENA_PAYLOAD)
    spec = _arena_tool(port)

    spec.handler(limit=3, days=90, scope="portfolio")
    assert port.calls[0]["scope"] == "portfolio"

    spec.handler(limit=3, days=90, scope="DROP TABLE reports")
    assert port.calls[1]["scope"] == "all", "非法口径不得透传"

    spec.handler(limit=3, days=90, scope="")
    assert port.calls[2]["scope"] == "all"


def test_arena_schema_has_no_user_id():
    spec = _arena_tool(_FakeArenaPort(ARENA_PAYLOAD))
    assert "user_id" not in (spec.input_schema.get("properties") or {})


def test_arena_is_l1_and_only_registered_with_a_port():
    from tradingagents.tools import build_registry
    from tradingagents.tools.spec import PLAN_LAYER_DECISION

    spec = _arena_tool(_FakeArenaPort(ARENA_PAYLOAD))
    assert spec.plan_layer == PLAN_LAYER_DECISION
    assert 5000 <= spec.latency_budget_ms <= 15000
    assert spec.freshness_sources == ("model_arena",)

    without = build_registry(ports_factory=lambda *a: object(), news_fetcher=lambda *a: [])
    assert "model_arena_leaderboard" not in without.names()

    with_port = build_registry(
        ports_factory=lambda *a: object(), news_fetcher=lambda *a: [],
        arena_port=_FakeArenaPort(ARENA_PAYLOAD),
    )
    assert "model_arena_leaderboard" in with_port.names_by_layer(PLAN_LAYER_DECISION)


# ══ 全批量守卫 ═════════════════════════════════════════════════════════════


def test_all_five_tools_share_the_same_l1_discipline():
    """五个工具的结构性共同点必须在同一个地方被断言，避免逐个漂移。"""
    from tradingagents.tools import build_registry
    from tradingagents.tools.spec import PLAN_LAYER_DECISION

    registry = build_registry(
        ports_factory=lambda *a: object(),
        news_fetcher=lambda *a: [],
        outcome_port=_FakeOutcomePort(OUTCOME_ROWS),
        report_port=_FakeReportPort(REPORT_ROWS),
        recommendation_port=_FakeRecommendationPort(RECOMMENDATION_PAYLOAD),
        tracking_port=_FakeTrackingPort(TRACKING_PAYLOAD),
        arena_port=_FakeArenaPort(ARENA_PAYLOAD),
    )
    names = (
        "verdict_outcome_lookup",
        "report_conclusion_lookup",
        "recommendation_read",
        "tracking_board_read",
        "model_arena_leaderboard",
    )
    layer = registry.names_by_layer(PLAN_LAYER_DECISION)
    for name in names:
        assert name in layer, f"{name} 必须落在 L1"
        spec = registry.require(name)
        assert "user_id" not in (spec.input_schema.get("properties") or {}), name
        assert spec.cost == "cheap", f"{name} 是只读，不该标成重成本"
        assert 5000 <= spec.latency_budget_ms <= 15000, name
        assert len(spec.freshness_sources) == 1, name
        assert spec.asynchronous is False, name


def test_outcome_lookup_with_no_records_says_not_yet_backfilled():
    """**「尚无回填」与「读取失败」必须能分辨**（与 trade_ledger 同一条纪律）。

    空表是当前的真实状态（`signal_verdict_outcomes` 0 行），所以这不是假想情形：
    用户问"上次那个判定兑现了吗"，正确答案是"尚无已回填的兑现记录"，
    而不是"取不到"，更不是"没有兑现"——"尚未成熟"与"已经失效"是两件事。
    """
    from api.services import chat_service

    uid = "outcome-empty-owner"
    _seed_verdict(uid, "600519.SH", hours_ago=1)  # 有判定，但没有兑现行

    registry = chat_service.build_registry(user_id=uid)
    result = registry.invoke("verdict_outcome_lookup", limit=10)

    assert result.data["count"] == 0
    joined = " ".join(result.coverage.unavailable)
    assert "尚无已回填" in joined, "必须说清是「还没回填」，不能只说取不到"
    assert result.coverage.available, "没有兑现行不等于数据源不可用"
