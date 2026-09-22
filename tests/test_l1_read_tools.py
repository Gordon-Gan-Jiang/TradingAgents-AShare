"""L1 决策工具（第三批）：平台已有结论的只读接入。

覆盖五个工具，全部遵守同一套纪律：

1. **结论来自平台、不可改写**：交易计划锚点、T+1 命中率与其统计效力、模拟盘
   权益、台账盈亏归因、报告内的多视角共识——都是平台算好并落库的东西。
   让模型"记一个止损价""估一下命中率"就是在制造第二份互相矛盾的结论。
2. **用户维度不经过模型**：端口在闭包里绑定 user_id，schema 里没有 user_id。
3. **三种"空"分开**：解析不到（`degraded`）／确实没有（`unavailable`）／
   确实为空的可确证事实（如模拟盘空仓）——三者混起来就会把"读不到"说成"没有"。
4. **效力和覆盖率必须跟着结论**：命中率要带置信区间与所需样本量；归因要带
   覆盖率；共识要带解析失败的分析师数。少一个，结论就成了无法复核的数字。
5. **取数失败降级不抛错**，且**不外传上游原文**；clamp 不写 `x or default`。
"""

from __future__ import annotations

import pytest

# ══ 1. trade_plan_read：纪律锚点 ═══════════════════════════════════════════


class _FakePlanPort:
    def __init__(self, rows=None, *, error: Exception | None = None):
        self.rows = rows if rows is not None else []
        self.error = error
        self.calls: list[dict] = []

    def active_plans(self, *, limit, symbol=None):
        self.calls.append({"limit": limit, "symbol": symbol})
        if self.error is not None:
            raise self.error
        return self.rows


PLAN_ROW = {
    "id": "p1", "symbol": "600519.SH", "name": "贵州茅台", "direction": "BUY",
    "horizon": "short", "horizon_days": 5, "entry_low": 1600.0, "entry_high": 1650.0,
    "position_cap_pct": 20.0, "first_tranche_pct": 10.0, "hard_stop_price": 1550.0,
    "take_profit_ladder": [[1800.0, 30.0], [1900.0, 30.0]], "target_price": 1900.0,
    "trailing_stop_pct": 8.0, "time_stop_days": 5,
    "invalidation_conditions": ["跌破 1550 收盘"], "de_risk_triggers": ["单日放量下跌 5%"],
    "risk_gate": "pass", "confidence": 72, "status": "active", "source": "report",
    "signal_trade_date": "2026-09-18",
}


def _plan_tool(port):
    from tradingagents.tools.plans import make_plan_tools

    return {spec.name: spec for spec in make_plan_tools(port)}["trade_plan_read"]


def test_trade_plan_anchors_are_citable():
    spec = _plan_tool(_FakePlanPort([PLAN_ROW]))
    result = spec.handler(limit=5, symbol=None, as_of="2026-09-21")

    value = str(result.evidence[0].value)
    assert "600519.SH" in value, "标的必须可引用"
    assert "硬止损 1550.00" in value, "止损位必须可引用（这是纪律锚点本体）"
    assert "1800.00(-30%)" in value, "止盈分档必须可引用"
    assert "2026-09-18" in value, "计划信号日必须可引用"


def test_trade_plan_no_plans_is_declared():
    spec = _plan_tool(_FakePlanPort([]))
    result = spec.handler(limit=5, symbol=None, as_of="2026-09-21")

    assert result.evidence == ()
    assert "trade_plan" in result.coverage.available
    assert result.coverage.unavailable, "没有有效计划必须声明"


def test_trade_plan_unparsable_rows_degrade_not_empty():
    spec = _plan_tool(_FakePlanPort(["nope"]))
    result = spec.handler(limit=5, symbol=None, as_of="2026-09-21")

    assert result.degraded is True
    assert "解析" in str(result.degraded_reason)


def test_trade_plan_read_failure_degrades_without_raising():
    spec = _plan_tool(_FakePlanPort(error=RuntimeError("plan table locked")))
    result = spec.handler(limit=5, symbol=None, as_of="2026-09-21")

    assert result.degraded is True
    assert result.coverage.unavailable == ("trade_plan",)
    assert "plan table locked" not in str(result.data)
    assert "plan table locked" not in str(result.degraded_reason)


def test_trade_plan_limit_is_clamped_including_zero():
    port = _FakePlanPort([PLAN_ROW])
    spec = _plan_tool(port)
    spec.handler(limit=999, symbol=None)
    assert port.calls[0]["limit"] == 20

    spec.handler(limit=0, symbol=None)
    assert port.calls[1]["limit"] == 1, "limit=0 不得被 `or` 换成默认值"


def test_trade_plan_symbol_filter_and_schema():
    port = _FakePlanPort([PLAN_ROW])
    spec = _plan_tool(port)
    spec.handler(limit=2, symbol="600519.SH")
    assert port.calls[0]["symbol"] == "600519.SH"
    assert "user_id" not in (spec.input_schema.get("properties") or {})


def test_trade_plan_is_l1_and_only_registered_with_a_port():
    from tradingagents.tools import build_registry
    from tradingagents.tools.spec import PLAN_LAYER_DECISION

    spec = _plan_tool(_FakePlanPort([PLAN_ROW]))
    assert spec.plan_layer == PLAN_LAYER_DECISION
    assert 5000 <= spec.latency_budget_ms <= 15000
    assert spec.freshness_sources == ("trade_plan",)

    without = build_registry(ports_factory=lambda *a: object(), news_fetcher=lambda *a: [])
    assert "trade_plan_read" not in without.names()

    with_port = build_registry(
        ports_factory=lambda *a: object(), news_fetcher=lambda *a: [],
        plan_port=_FakePlanPort([PLAN_ROW]),
    )
    assert "trade_plan_read" in with_port.names_by_layer(PLAN_LAYER_DECISION)


# ══ 2. t1_quality_read：命中率 + 统计效力 ══════════════════════════════════


class _FakeQualityPort:
    def __init__(self, payload=None, *, error: Exception | None = None):
        self.payload = payload
        self.error = error
        self.calls: list[dict] = []

    def t1_quality(self, *, days, scope):
        self.calls.append({"days": days, "scope": scope})
        if self.error is not None:
            raise self.error
        return self.payload


QUALITY = {
    "scope": "all", "window_start": "2026-06-23", "window_end": "2026-09-21",
    "effective_n": 120, "hits": 64, "accuracy_pct": 53.33, "ci_low_pct": 44.2,
    "ci_high_pct": 62.3, "n_days": 60, "required_n": 1234, "underpowered": True,
    "verdict": "not_significant", "clustered": False, "abstain_count": 8,
    "coverage_pct": 93.75, "unique_symbols": 21, "top_symbol_share": 0.18,
    "conflict_n": 3, "excess": {"accuracy_pct": 51.1, "effective_n": 110},
    "excess_spread": 1.2,
}


def _quality_tool(port):
    from tradingagents.tools.quality import make_quality_tools

    return {spec.name: spec for spec in make_quality_tools(port)}["t1_quality_read"]


def test_t1_quality_reports_effect_size_with_the_rate():
    spec = _quality_tool(_FakeQualityPort(QUALITY))
    result = spec.handler(days=90, scope="all", as_of="2026-09-21")

    value = str(result.evidence[0].value)
    assert "53.33%" in value, "命中率必须可引用"
    assert "有效样本 120" in value, "样本量必须可引用"
    assert "44.20%–62.30%" in value, "置信区间必须可引用"
    assert "1234" in value, "必要样本量必须可引用（否则 53% 会被当成结论）"
    assert "无法区分于掷硬币" in value, "效力不足必须说清楚"
    assert "underpowered" in value


def test_t1_quality_no_samples_is_not_a_zero_accuracy():
    spec = _quality_tool(
        _FakeQualityPort({"scope": "all", "effective_n": 0, "accuracy_pct": None,
                          "note": "无已评估样本"})
    )
    result = spec.handler(days=90, scope="all", as_of="2026-09-21")

    assert result.evidence == ()
    assert result.coverage.unavailable, "没有已评估样本必须声明"
    assert "0%" not in " ".join(result.coverage.unavailable)


def test_t1_quality_beats_coin_flip_verdict_is_stated():
    spec = _quality_tool(_FakeQualityPort(dict(QUALITY, verdict="beats_coin_flip")))
    result = spec.handler(days=90)
    assert "有统计证据优于掷硬币" in str(result.evidence[0].value)


def test_t1_quality_clamps_days_and_scope_including_zero():
    port = _FakeQualityPort(QUALITY)
    spec = _quality_tool(port)

    spec.handler(days=99999, scope="portfolio")
    assert port.calls[0] == {"days": 400, "scope": "portfolio"}

    spec.handler(days=0, scope="drop table")
    assert port.calls[1] == {"days": 7, "scope": "all"}, "非法值不得透传，0 不得被 `or` 换掉"


def test_t1_quality_read_failure_degrades_without_raising():
    spec = _quality_tool(_FakeQualityPort(error=RuntimeError("stats exploded")))
    result = spec.handler(days=90)

    assert result.degraded is True
    assert result.coverage.unavailable == ("t1_quality",)
    assert "stats exploded" not in str(result.data)


def test_t1_quality_is_l1_and_only_registered_with_a_port():
    from tradingagents.tools import build_registry
    from tradingagents.tools.spec import PLAN_LAYER_DECISION

    spec = _quality_tool(_FakeQualityPort(QUALITY))
    assert spec.plan_layer == PLAN_LAYER_DECISION
    assert "user_id" not in (spec.input_schema.get("properties") or {})

    without = build_registry(ports_factory=lambda *a: object(), news_fetcher=lambda *a: [])
    assert "t1_quality_read" not in without.names()

    with_port = build_registry(
        ports_factory=lambda *a: object(), news_fetcher=lambda *a: [],
        quality_port=_FakeQualityPort(QUALITY),
    )
    assert "t1_quality_read" in with_port.names_by_layer(PLAN_LAYER_DECISION)


# ══ 3. paper_portfolio_read：模拟盘 ═══════════════════════════════════════


class _FakePaperPort:
    def __init__(self, payload=None, *, error: Exception | None = None):
        self.payload = payload
        self.error = error
        self.calls: int = 0

    def portfolio_snapshot(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.payload


SNAPSHOT = {
    "portfolio_id": "pp1", "cash_balance": 300000.0, "initial_cash": 1000000.0,
    "market_value": 700000.0, "equity": 1000000.0, "total_return_pct": 0.0,
    "positions": [
        {"symbol": "600519.SH", "name": "贵州茅台", "quantity": 100.0,
         "avg_cost": 1650.0, "last_price": 1700.0},
    ],
}


def _paper_tool(port):
    from tradingagents.tools.paper import make_paper_tools

    return {spec.name: spec for spec in make_paper_tools(port)}["paper_portfolio_read"]


def test_paper_portfolio_overview_and_positions_are_citable():
    spec = _paper_tool(_FakePaperPort(SNAPSHOT))
    result = spec.handler(limit=20, as_of="2026-09-21")

    overview = str(result.evidence[0].value)
    assert "初始资金 1000000.00" in overview and "总权益 1000000.00" in overview
    holding = str(result.evidence[1].value)
    assert "600519.SH" in holding and "1700.00" in holding, "逐只持仓必须可引用"


def test_paper_portfolio_flat_is_a_fact_not_an_uncovered_dimension():
    """模拟盘空仓是可确证的事实（服务明确返回空列表），不是"未覆盖"。

    把它声明成未覆盖会在回答里凭空多出一条噪音，而用户需要知道的是"现在是空仓"。
    """
    spec = _paper_tool(_FakePaperPort(dict(SNAPSHOT, positions=[])))
    result = spec.handler(limit=20, as_of="2026-09-21")

    assert result.evidence, "空仓也要有可引用的事实"
    assert "空仓" in " ".join(str(item.value) for item in result.evidence)
    assert result.coverage.unavailable == ()


def test_paper_portfolio_unparsable_and_failure_paths():
    unparsable = _paper_tool(_FakePaperPort(["not", "a", "mapping"]))
    result = unparsable.handler(limit=20)
    assert result.degraded is True and result.coverage.unavailable == ("paper_portfolio",)

    failing = _paper_tool(_FakePaperPort(error=RuntimeError("quote down")))
    result = failing.handler(limit=20)
    assert result.degraded is True
    assert "quote down" not in str(result.data)
    assert "quote down" not in str(result.degraded_reason)


def test_paper_portfolio_limit_is_clamped_including_zero():
    spec = _paper_tool(_FakePaperPort(SNAPSHOT))
    # limit=0 → 收窄到 1：只剩总览 + 第一条持仓
    zero = spec.handler(limit=0, as_of="2026-09-21")
    assert len(zero.evidence) == 2
    assert "600519.SH" in str(zero.evidence[1].value)
    assert "user_id" not in (spec.input_schema.get("properties") or {})


def test_paper_portfolio_is_l1_and_only_registered_with_a_port():
    from tradingagents.tools import build_registry
    from tradingagents.tools.spec import PLAN_LAYER_DECISION

    spec = _paper_tool(_FakePaperPort(SNAPSHOT))
    assert spec.plan_layer == PLAN_LAYER_DECISION

    without = build_registry(ports_factory=lambda *a: object(), news_fetcher=lambda *a: [])
    assert "paper_portfolio_read" not in without.names()

    with_port = build_registry(
        ports_factory=lambda *a: object(), news_fetcher=lambda *a: [],
        paper_port=_FakePaperPort(SNAPSHOT),
    )
    assert "paper_portfolio_read" in with_port.names_by_layer(PLAN_LAYER_DECISION)


# ══ 4. trade_ledger_read：盈亏归因 ════════════════════════════════════════


class _FakeLedgerPort:
    def __init__(self, payload=None, *, error: Exception | None = None):
        self.payload = payload
        self.error = error
        self.calls: list[dict] = []

    def ledger(self, *, limit, symbol=None):
        self.calls.append({"limit": limit, "symbol": symbol})
        if self.error is not None:
            raise self.error
        return self.payload


LEDGER = {
    "summary": {
        "total_realized_pnl": 12500.0, "trade_count": 42, "buy_count": 25,
        "sell_count": 17, "win_count": 11, "loss_count": 6, "win_rate": 64.71,
        "average_win": 1200.0, "average_loss": -800.0, "attribution_coverage": 0.8333,
        "missing_sell_reason_count": 3, "symbol_count": 2,
        "by_symbol": [
            {"symbol": "600519.SH", "name": "贵州茅台", "realized_pnl": 8000.0,
             "trade_count": 12, "buy_count": 8, "sell_count": 4,
             "bought_amount": 200000.0, "sold_amount": 180000.0},
        ],
        "by_sell_reason": [{"sell_reason": "止盈", "realized_pnl": 9000.0, "trade_count": 9}],
    },
    "trades": [
        {"symbol": "600519.SH", "action": "SELL", "trade_date": "2026-09-18",
         "price": 1700.0, "shares": 100.0, "amount": 170000.0,
         "realized_pnl": -1500.0, "sell_reason": "止盈"},
    ],
}


def _ledger_tool(port):
    from tradingagents.tools.ledger import make_ledger_tools

    return {spec.name: spec for spec in make_ledger_tools(port)}["trade_ledger_read"]


def test_trade_ledger_summary_attribution_and_trades_are_citable():
    spec = _ledger_tool(_FakeLedgerPort(LEDGER))
    result = spec.handler(limit=10, symbol=None, as_of="2026-09-21")

    summary = str(result.evidence[0].value)
    assert "+12500.00" in summary and "64.71%" in summary, "盈亏与胜率必须可引用"
    assert "83.33%" in summary, "归因覆盖率必须可引用"
    joined = " ".join(str(item.value) for item in result.evidence)
    assert "止盈" in joined, "按卖出原因的归因必须可引用"
    assert "2026-09-18" in joined, "流水日期必须可引用"


def test_trade_ledger_declares_incomplete_attribution_coverage():
    """覆盖率不足时必须声明未覆盖：不完整的归因表不能被当成全部。"""
    spec = _ledger_tool(_FakeLedgerPort(LEDGER))
    result = spec.handler(limit=10, symbol=None, as_of="2026-09-21")

    assert result.coverage.unavailable, "有未归因流水就必须声明"
    assert "sell_reason" in " ".join(result.coverage.unavailable)
    assert "3 笔" in " ".join(result.coverage.unavailable)


def test_trade_ledger_full_coverage_declares_nothing_uncovered():
    full = dict(LEDGER["summary"], attribution_coverage=1.0, missing_sell_reason_count=0)
    spec = _ledger_tool(_FakeLedgerPort({"summary": full, "trades": LEDGER["trades"]}))
    result = spec.handler(limit=10)
    assert result.coverage.unavailable == ()


def test_trade_ledger_no_trades_is_declared_not_zero_pnl():
    spec = _ledger_tool(
        _FakeLedgerPort({"summary": dict(LEDGER["summary"], trade_count=0), "trades": []})
    )
    result = spec.handler(limit=10, as_of="2026-09-21")

    assert result.evidence == ()
    assert result.coverage.unavailable, "没有流水必须声明，不能报成「盈亏 0」"


def test_trade_ledger_no_trades_keeps_the_source_available():
    """**「你没录入」与「数据源坏了」必须能分辨**。

    两个分支的 `unavailable` 都非空，所以只看"有没有声明"是分不出来的：
    * 没有流水（本用例）：`available` 含源键、**不 degraded** —— 工具是好的，
      只是用户还没有流水；模型该说"尚未录入"，不该说"取不到"。
    * 读取失败（见下一条用例）：`available` 为空、`degraded=True` —— 真取不到。

    这层区分是产品语义：把"没有记录"讲成"数据源故障"会让用户以为平台坏了，
    反过来把"故障"讲成"没有记录"会让用户以为自己没录。空表是当前的真实状态
    （`trade_ledger` 0 行），所以这条不是假想。
    """
    spec = _ledger_tool(
        _FakeLedgerPort({"summary": dict(LEDGER["summary"], trade_count=0), "trades": []})
    )
    result = spec.handler(limit=10, as_of="2026-09-21")

    assert result.coverage.available, "没有任何流水不等于数据源不可用"
    assert result.degraded is False, "空表不是故障，不得标降级"
    assert "没有已录入" in " ".join(result.coverage.unavailable), "必须说清是「尚未录入」"


def test_trade_ledger_failure_and_unparsable_paths():
    failing = _ledger_tool(_FakeLedgerPort(error=RuntimeError("ledger locked")))
    result = failing.handler(limit=10)
    assert result.degraded is True
    assert "ledger locked" not in str(result.data)

    missing_summary = _ledger_tool(_FakeLedgerPort({"trades": []}))
    result = missing_summary.handler(limit=10)
    assert result.degraded is True
    assert result.coverage.unavailable == ("trade_ledger",)


def test_trade_ledger_limit_is_clamped_including_zero():
    port = _FakeLedgerPort(LEDGER)
    spec = _ledger_tool(port)
    spec.handler(limit=999)
    assert port.calls[0]["limit"] == 20

    spec.handler(limit=0)
    assert port.calls[1]["limit"] == 1, "limit=0 不得被 `or` 换成默认值"
    assert "user_id" not in (spec.input_schema.get("properties") or {})


def test_trade_ledger_is_l1_and_only_registered_with_a_port():
    from tradingagents.tools import build_registry
    from tradingagents.tools.spec import PLAN_LAYER_DECISION

    spec = _ledger_tool(_FakeLedgerPort(LEDGER))
    assert spec.plan_layer == PLAN_LAYER_DECISION

    without = build_registry(ports_factory=lambda *a: object(), news_fetcher=lambda *a: [])
    assert "trade_ledger_read" not in without.names()

    with_port = build_registry(
        ports_factory=lambda *a: object(), news_fetcher=lambda *a: [],
        ledger_port=_FakeLedgerPort(LEDGER),
    )
    assert "trade_ledger_read" in with_port.names_by_layer(PLAN_LAYER_DECISION)


# ══ 5. report_consensus_read：多视角共识 ══════════════════════════════════


class _FakeConsensusPort:
    def __init__(self, payload=None, *, error: Exception | None = None):
        self.payload = payload
        self.error = error
        self.calls: list[str] = []

    def latest_consensus(self, *, symbol):
        self.calls.append(symbol)
        if self.error is not None:
            raise self.error
        return self.payload


CONSENSUS_PAYLOAD = {
    "report_id": "r1", "symbol": "600519.SH", "trade_date": "2026-09-18",
    "direction": "看多", "confidence": 78,
    "consensus": {
        "consensus_direction": "看多", "consensus_score": 0.42, "consensus_strength": "medium",
        "disagreement_score": 0.31, "stability_score": 0.62, "execution_mode": "normal",
        "execution_mode_label": "常规执行", "dominant_horizon": "short",
        "horizon_conflict": False, "support_ratio": 0.5, "oppose_ratio": 0.17,
        "neutral_ratio": 0.33, "risk_gate": "pass",
        "execution_preconditions": ["放量突破 1650"], "de_risk_triggers": ["跌破 1550"],
        "final_direction": "看多", "final_confidence": 78,
        "supporting_agents": ["market", "fundamentals"], "opposing_agents": ["news"],
        "unparsed_analyst_n": 2,
    },
}


def _consensus_tool(port):
    from tradingagents.tools.consensus import make_consensus_tools

    return {spec.name: spec for spec in make_consensus_tools(port)}["report_consensus_read"]


def test_report_consensus_is_citable_with_ratios_and_agents():
    spec = _consensus_tool(_FakeConsensusPort(CONSENSUS_PAYLOAD))
    result = spec.handler(symbol="600519.SH", as_of="2026-09-21")

    head = str(result.evidence[0].value)
    assert "看多" in head and "0.420" in head, "共识方向与得分必须可引用"
    assert "支持 50%" in head and "反对 17%" in head, "票型比例必须可引用"
    joined = " ".join(str(item.value) for item in result.evidence)
    assert "market" in joined and "news" in joined, "支持/反对名单必须可引用"
    assert "风险闸 pass" in joined


def test_report_consensus_declares_unparsed_analysts():
    """解析失败的分析师必须单独声明：否则「一致看多」与「只有两份成功」长得一样。"""
    spec = _consensus_tool(_FakeConsensusPort(CONSENSUS_PAYLOAD))
    result = spec.handler(symbol="600519.SH", as_of="2026-09-21")

    fields = [item.field for item in result.evidence]
    assert "未计入的分析师" in fields
    text = " ".join(str(item.value) for item in result.evidence if item.field == "未计入的分析师")
    assert "2 位" in text and "未计入" in text


def test_report_consensus_no_report_and_no_consensus_are_different():
    no_report = _consensus_tool(_FakeConsensusPort(None)).handler(symbol="600519.SH")
    assert no_report.evidence == ()
    assert "没有历史报告" in " ".join(no_report.coverage.unavailable)

    no_summary = _consensus_tool(
        _FakeConsensusPort({"report_id": "r1", "symbol": "600519.SH",
                            "trade_date": "2026-01-01", "consensus": None})
    ).handler(symbol="600519.SH")
    assert no_summary.evidence == ()
    assert "没有多视角共识" in " ".join(no_summary.coverage.unavailable), (
        "「有报告但没共识」与「没有报告」必须给不同的声明"
    )


def test_report_consensus_requires_symbol_and_degrades_on_failure():
    missing = _consensus_tool(_FakeConsensusPort(CONSENSUS_PAYLOAD)).handler(symbol="")
    assert missing.degraded is True and "symbol" in str(missing.degraded_reason)

    failing = _consensus_tool(_FakeConsensusPort(error=RuntimeError("traces broken")))
    result = failing.handler(symbol="600519.SH")
    assert result.degraded is True
    assert "traces broken" not in str(result.data)
    assert "traces broken" not in str(result.degraded_reason)


def test_report_consensus_schema_and_registration():
    from tradingagents.tools import build_registry
    from tradingagents.tools.spec import PLAN_LAYER_DECISION

    spec = _consensus_tool(_FakeConsensusPort(CONSENSUS_PAYLOAD))
    assert spec.plan_layer == PLAN_LAYER_DECISION
    assert "user_id" not in (spec.input_schema.get("properties") or {})
    assert spec.input_schema.get("required") == ["symbol"]

    without = build_registry(ports_factory=lambda *a: object(), news_fetcher=lambda *a: [])
    assert "report_consensus_read" not in without.names()

    with_port = build_registry(
        ports_factory=lambda *a: object(), news_fetcher=lambda *a: [],
        consensus_port=_FakeConsensusPort(CONSENSUS_PAYLOAD),
    )
    assert "report_consensus_read" in with_port.names_by_layer(PLAN_LAYER_DECISION)
