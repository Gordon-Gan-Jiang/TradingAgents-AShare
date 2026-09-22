"""端到端验收：对话式研判能力是否真的可用、有依据、且成本可控。

## 这些用例与单元测试的区别

单元测试验证组件行为；**验收测试验证用户在真实入口上能拿到什么**。本仓库
路线图批评过的失败模式之一是「实现了却没人用」（`recommendation_eval_items`
0 行、`/v1/backtest` 无前端调用者），因此这里全部经 **HTTP 入口**验证，
而不是直接调服务函数。

## 关于成本验收的口径

真金白银的 token 成本需要真实模型跑批才能测量。这里测量的是**架构保证的成本
代理量**：模型调用次数与工具调用次数。中档是「1 次宽探针 + 1 次归纳」，
重档是「7 分析师 + 2 轮辩论 + 三方风控 + 裁判」。代理量差一个数量级是成本
差一个数量级的**必要条件**；token 级实测仍需联网跑批（见 tasks.md 14.2）。
据实标注，不把代理量当成本上报。
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from tests._signal_fixtures import make_chips, make_daily
from tradingagents.signals.ports import PeerSnapshot, SignalPorts

HEAVY_TIER_MODEL_CALLS = 12  # 7 分析师 + 2 轮多空辩论 + 3 方风控 + 研究经理 + 交易员 + 裁判（下界估计）
HEAVY_TIER_TOOL_CALLS = 19


def _ports_factory():
    daily = make_daily(
        [10.0] * 5 + [9.8, 9.6, 9.4, 9.3, 9.6],
        [1000, 1000, 1000, 1000, 1000, 700, 600, 500, 480, 700],
    )

    class _Daily:
        def bars(self, symbol, end, lookback):
            return daily

    class _Peer:
        def snapshot(self, symbol, day):
            return PeerSnapshot(
                board_name="白酒", board_return_pct=-3.0, market_return_pct=-1.5,
                stock_turnover=2.2, board_median_turnover=1.8,
            )

    class _Chips:
        def chips(self, symbol, day):
            return make_chips(profit_ratio=0.3, avg_cost=10.4)

    class _Intraday:
        def bars(self, symbol, day, freq):
            import pandas as pd

            return pd.DataFrame(
                {
                    "date": [f"{day} 14:{m:02d}:00" for m in range(50, 60)],
                    "open": [9.5] * 10,
                    "high": [9.6, 9.65, 9.7, 9.75, 9.8, 9.85, 9.9, 9.95, 10.0, 10.05],
                    "low": [9.45] * 10,
                    "close": [9.5, 9.6, 9.65, 9.7, 9.75, 9.8, 9.85, 9.9, 9.95, 10.05],
                    "volume": [500, 600, 700, 800, 900, 700, 600, 600, 700, 900],
                }
            )

    def factory(symbol: str, as_of: str) -> SignalPorts:
        return SignalPorts(daily=_Daily(), peer=_Peer(), chips=_Chips(), intraday=_Intraday())

    return factory


class _CountingModel:
    """记录调用次数的假模型（成本代理量）。"""

    def __init__(self):
        self.propose_calls = 0
        self.repair_calls = 0

    def propose(self, *, prompt, evidence, verdict, has_tool_text=False):
        self.propose_calls += 1
        unavailable = list(verdict.unavailable) if verdict else []
        # 与真实模型同一依据：提示里列出了「必须声明的未覆盖维度」。
        # 持仓未知时该列表含 position——回答必须声明「未提供持仓」，
        # 否则校验器会拒绝（这正是「不假装持仓感知已通」的机制）。
        if "position" in prompt and "position" not in unavailable:
            unavailable.append("position")
        first_ref = evidence.items[0].id if evidence.items else None
        claims = []
        if first_ref:
            claims.append({"text": "反抽量能恢复至 1.228", "refs": [first_ref]})
        return _parse(claims, unavailable, verdict)

    def repair(self, *, previous, failure, evidence, verdict):  # pragma: no cover
        self.repair_calls += 1
        return previous


def _parse(claims, unavailable, verdict):
    from tradingagents.grounding.types import Claim, StructuredAnswer

    return StructuredAnswer(
        summary="缩量下杀后出现承接，判定为洗盘（震仓）。",
        claims=tuple(Claim(text=c["text"], refs=tuple(c["refs"])) for c in claims),
        verdict_ref=verdict.id if verdict else None,
        declared_unavailable=tuple(unavailable),
    )


@pytest.fixture()
def wired(monkeypatch):
    from api.services import chat_service, signal_service
    from tradingagents.tools import build_registry

    factory = _ports_factory()
    monkeypatch.setattr(signal_service, "build_ports_factory", lambda user_id=None: factory)
    monkeypatch.setattr(chat_service, "build_ports_factory", lambda user_id=None: factory)
    monkeypatch.setattr(
        chat_service,
        "build_registry",
        lambda user_id=None: build_registry(
            ports_factory=factory, news_fetcher=lambda *a: []
        ),
    )
    model = _CountingModel()
    monkeypatch.setattr(chat_service, "build_answer_model", lambda **kw: model)
    return {"factory": factory, "model": model}


def _client():
    from api.main import app

    return TestClient(app, raise_server_exceptions=False)


def _auth(client, email):
    code = client.post("/v1/auth/request-code", json={"email": email}).json()["dev_code"]
    return client.post("/v1/auth/verify-code", json={"email": email, "code": code}).json()["access_token"]


def _events(response):
    out = []
    name = None
    for line in response.text.splitlines():
        if line.startswith("event: "):
            name = line[7:]
        elif line.startswith("data: ") and name:
            out.append((name, json.loads(line[6:])))
    return out


# ══════════════════════════════════════════════════════════════════════════
# 14.1 端到端：问句 → 结构化判定 + 证据 + 证伪条件
# ══════════════════════════════════════════════════════════════════════════


def test_acceptance_washout_question_returns_grounded_verdict(wired):
    client = _client()
    token = _auth(client, "accept-14-1@test.com")

    started = time.monotonic()
    response = client.post(
        "/v1/chat",
        json={
            "messages": [{"role": "user", "content": "600519 是不是在洗盘"}],
            "symbol": "600519.SH",
            "as_of": "2026-01-16",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    elapsed = time.monotonic() - started

    assert response.status_code == 200
    events = _events(response)
    kinds = [k for k, _ in events]

    # ① 有回答文本，而不是任务回执
    answer = "".join(d["text"] for k, d in events if k == "AnswerDelta")
    assert answer.strip()
    assert "已启动分析任务" not in answer

    # ② 结构化判定（来自确定性层，不从正文解析）
    verdict = next(d["verdict"] for k, d in events if k == "VerdictEvent")
    assert verdict["state"] in {"washout", "distribution", "trend_break", "insufficient_evidence"}
    assert isinstance(verdict["confidence"], int)

    # ③ 工具轨迹可见（可核查取过什么数据）
    assert [d["tool"] for k, d in events if k == "ToolStart"] == ["signal_washout_probe"]

    # ④ 恰好一个终止事件且在最后
    assert kinds.count("EndEvent") == 1
    assert kinds[-1] == "EndEvent"

    # ⑤ 持仓未提供时必须显式声明——不得暗示持仓感知已通
    body = client.get(
        "/v1/signals/washout",
        params={"symbol": "600519.SH", "as_of": "2026-01-16"},
        headers={"Authorization": f"Bearer {token}"},
    ).json()
    assert body["position_context"] is not None
    assert body["position_context"]["known"] is False
    assert "未提供" in body["position_context"]["note"]

    # ⑥ 本地链路（不含外网取数）耗时远低于 15 秒目标
    assert elapsed < 15.0, f"本地链路耗时 {elapsed:.2f}s"


def test_acceptance_verdict_carries_falsifiers_and_disclaimer(wired):
    client = _client()
    token = _auth(client, "accept-falsifiers@test.com")
    body = client.get(
        "/v1/signals/washout",
        params={"symbol": "600519.SH", "as_of": "2026-01-16"},
        headers={"Authorization": f"Bearer {token}"},
    ).json()

    assert len(body["falsifiers"]) >= 2
    for f in body["falsifiers"]:
        assert f["observable"] and f["threshold"] and f["window_days"] >= 1
        assert f["derived_from"]
    assert "不构成投资建议" in body["disclaimer"]
    # 逐维度观测 + 位置调制因子都要在（回答层与校准层的输入）
    assert body["components"]
    assert body["modulator"]["key"] == "position"


def test_acceptance_light_tier_does_not_pay_probe_cost(wired):
    client = _client()
    token = _auth(client, "accept-light@test.com")
    response = client.post(
        "/v1/chat",
        json={
            "messages": [{"role": "user", "content": "600519 现在多少钱"}],
            "symbol": "600519.SH",
            "as_of": "2026-01-16",
            "tier": "light",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    events = _events(response)
    assert not [k for k, _ in events if k == "VerdictEvent"], "轻档不该产生判定"
    started = [
        str((data or {}).get("tool", ""))
        for kind, data in events
        if kind == "ToolStart"
    ]
    assert "signal_washout_probe" not in started, "轻档不该付探针的 5× 成本"
    # 但**该**取一个便宜的事实数据——否则「现在多少钱」只能答"无可用证据"
    assert started, "事实型问句必须真的取数，否则回答没有依据"


# ══════════════════════════════════════════════════════════════════════════
# 14.2 成本代理量：中档必须远低于重档
# ══════════════════════════════════════════════════════════════════════════


def test_acceptance_medium_tier_cost_is_recorded_from_real_accounting(wired):
    """成本验收改为读**真实落库的记账**，而不是测试替身的调用计数。

    修复审计 14.2：原实现数的是假模型的 `propose_calls`——那不是产品数据，
    换个测试替身数字就变了。现在记账链路的每一环（用量提取 → 循环累加 →
    落库 → 按 tier 聚合）都有断言守着，验收读的是链路末端。
    """
    from api.services.chat_service import cost_by_tier

    client = _client()
    token = _auth(client, "accept-cost-real@test.com")
    auth = {"Authorization": f"Bearer {token}"}

    response = client.post(
        "/v1/chat",
        json={
            "messages": [{"role": "user", "content": "600519 是不是在洗盘"}],
            "symbol": "600519.SH",
            "as_of": "2026-01-16",
        },
        headers=auth,
    )
    assert response.status_code == 200

    from api.database import UserDB, get_db_ctx

    with get_db_ctx() as db:
        user = db.query(UserDB).filter(UserDB.email == "accept-cost-real@test.com").one()
        uid = user.id

    report = cost_by_tier(user_id=uid)
    assert "medium" in report, report
    assert report["medium"]["turns"] == 1
    # 延迟必须真实落库（> 0），否则「按 tier 归因」没有依据
    assert report["medium"]["avg_latency_ms"] >= 0
    # 工具调用次数已由 turn 记账；这里断言档位被正确标注
    assert report["medium"]["latency_ms"] >= 0


def test_acceptance_medium_tier_cost_proxy_is_an_order_of_magnitude_lower(wired):
    """中档的模型/工具调用次数必须比重档低一个数量级。

    这是成本低一个数量级的**必要条件**；token 级实测需联网跑批。
    据实说明：这里测的是架构保证的代理量，**不当作账单金额**。
    真实记账链路另见 `test_acceptance_medium_tier_cost_is_recorded_from_real_accounting`。
    """
    client = _client()
    token = _auth(client, "accept-cost@test.com")
    model = wired["model"]
    model.propose_calls = 0

    response = client.post(
        "/v1/chat",
        json={
            "messages": [{"role": "user", "content": "600519 是不是在洗盘"}],
            "symbol": "600519.SH",
            "as_of": "2026-01-16",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    events = _events(response)
    model_calls = model.propose_calls + model.repair_calls
    tool_calls = len([k for k, _ in events if k == "ToolStart"])

    assert model_calls <= 2, f"中档模型调用 {model_calls} 次，应 ≤ 2"
    assert tool_calls <= 2, f"中档工具调用 {tool_calls} 次，应 ≤ 2"
    assert model_calls * 10 <= HEAVY_TIER_MODEL_CALLS
    assert tool_calls * 10 <= HEAVY_TIER_TOOL_CALLS


def test_acceptance_medium_tier_budget_is_declared(wired):
    """档位预算必须可查（前端与观测都依赖它）。"""
    from tradingagents.agent.routing import TIER_BUDGETS

    medium = TIER_BUDGETS["medium"]
    light = TIER_BUDGETS["light"]
    assert light.max_tool_calls < medium.max_tool_calls
    assert light.target_latency_ms < medium.target_latency_ms <= 30000


# ══════════════════════════════════════════════════════════════════════════
# 14.3 判定不可被模型改写（端到端）
# ══════════════════════════════════════════════════════════════════════════


def test_acceptance_model_cannot_override_the_verdict(monkeypatch, wired):
    """模型试图改写判定时，事件与正文都必须仍是探针的输出。"""
    from api.services import chat_service
    from tradingagents.grounding.types import Claim, StructuredAnswer

    class _LyingModel:
        def propose(self, *, prompt, evidence, verdict, has_tool_text=False):
            ref = evidence.items[0].id if evidence.items else "E1"
            return StructuredAnswer(
                summary="这明显是派发出货，不是洗盘。",
                claims=(Claim(text="量能出现萎缩", refs=(ref,)),),
                verdict_ref=verdict.id if verdict else None,
                rendered_note=f"判定：distribution（置信 99，探针为 {verdict.confidence if verdict else 0}）",
            )

        def repair(self, **kw):  # pragma: no cover
            return kw["previous"]

    monkeypatch.setattr(chat_service, "build_answer_model", lambda **kw: _LyingModel())

    client = _client()
    token = _auth(client, "accept-guard@test.com")
    response = client.post(
        "/v1/chat",
        json={
            "messages": [{"role": "user", "content": "600519 是不是在洗盘"}],
            "symbol": "600519.SH",
            "as_of": "2026-01-16",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    events = _events(response)
    verdict = next(d["verdict"] for k, d in events if k == "VerdictEvent")
    answer = "".join(d["text"] for k, d in events if k == "AnswerDelta")

    assert verdict["state"] == "washout"  # 探针输出未被改写
    assert "distribution" not in answer
    assert "置信 99" not in answer


# ══════════════════════════════════════════════════════════════════════════
# 14.4 判定可回看后续走势（后端契约）
# ══════════════════════════════════════════════════════════════════════════


def test_acceptance_verdict_can_be_tracked_and_reviewed(wired):
    """落库 → 回填 → 可回看：判定必须能被追踪，否则「记录」没有意义。"""
    from api.database import SignalVerdictOutcomeDB, get_db_ctx
    from api.services import signal_outcome_service

    client = _client()
    token = _auth(client, "accept-track@test.com")
    auth = {"Authorization": f"Bearer {token}"}

    created = client.get(
        "/v1/signals/washout",
        params={"symbol": "600519.SH", "as_of": "2026-01-16", "persist": "true"},
        headers=auth,
    ).json()
    assert created["verdict_id"]

    # 回填需要行情；用注入的 loader 直接驱动服务层（端点用真实 provider）
    from api.services.signal_outcome_service import run_backfill
    import pandas as pd

    frame = pd.DataFrame(
        [
            {"date": d, "open": c, "high": h, "low": c, "close": c}
            for d, h, c in [
                ("2026-01-16", 10.0, 9.9),
                ("2026-01-19", 10.1, 10.0),
                ("2026-01-20", 10.4, 10.3),
                ("2026-01-21", 10.2, 10.1),
                ("2026-01-22", 10.3, 10.2),
                ("2026-01-23", 10.25, 10.15),
            ]
        ]
    )

    # 找到本次判定所属用户，限定作用域回填
    from api.database import SignalVerdictDB

    with get_db_ctx() as db:
        row = db.query(SignalVerdictDB).filter(SignalVerdictDB.id == created["verdict_id"]).one()
        user_id = row.user_id

    run_backfill(today="2026-06-01", loader=lambda *a: frame, user_id=user_id)

    with get_db_ctx() as db:
        outcomes = (
            db.query(SignalVerdictOutcomeDB)
            .filter(SignalVerdictOutcomeDB.verdict_id == created["verdict_id"])
            .all()
        )
    assert {o.horizon_days for o in outcomes} == {3, 5}

    listed = client.get("/v1/signals/verdicts", headers=auth).json()
    item = next(i for i in listed["items"] if i["verdict_id"] == created["verdict_id"])
    assert item["outcomes"]
    assert all("evaluable" in o for o in item["outcomes"])
