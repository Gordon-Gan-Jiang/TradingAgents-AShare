"""判定信号接入退出引擎（tasks 13.3）。

## 设计意图（D17）

`washout` 判定**不新建探针**，而是作为 `exit_engine_service` 的**新触发信号**接入。
这样复用既有 102 个用例保护的退出逻辑，而不是复制一份。

## 接入的边界——研究结论不得变成交易指令

平台的定位是研究工具，判定属于对他人意图的概率化推断。因此本接入**只降级、
不升级**：

* `distribution` / `trend_break`：把「无事可做」的默认（持有）降级为「观察」，
  并附上判定依据与失效条件
* `washout`：只作为支撑性说明，**不改变动作**（洗盘意味着可能继续持有，
  但那是用户的决定）
* `insufficient_evidence`：显式声明该维度不可用，**不得静默忽略**

绝不因为一个判定就给出「清仓」——那越过了研究结论的边界，也与既有
`EXIT_DISCLAIMER` 的定位冲突。

## 向后兼容

`market_state` 是可选参数（默认 None）。不传时行为与之前逐字一致——
既有 102 个退出引擎用例不受影响。
"""

from __future__ import annotations

import pytest


def _holding(**over):
    from api.services.exit_engine_service import evaluate_holding

    base = dict(
        symbol="600519.SH",
        name="贵州茅台",
        position=300,
        available_position=300,
        average_cost=1850.0,
        price=1860.0,
        previous_close=1855.0,
        plan=None,
        today="2026-03-04",
    )
    base.update(over)
    return evaluate_holding(**base)


def _state(state: str, **over):
    payload = {
        "state": state,
        "confidence": 61,
        "symbol": "600519.SH",
        "as_of": "2026-03-04",
        "signal_version": "washout-v1.0.0",
        "falsifiers": [{"description": "次日放量下跌 → 假设失效", "observable": "next_day_volume_ratio"}],
        "daily_only": False,
    }
    payload.update(over)
    return payload


# ══════════════════════════════════════════════════════════════════════════
# 向后兼容
# ══════════════════════════════════════════════════════════════════════════


def test_without_market_state_behaviour_is_unchanged():
    """不传判定信号时，输出必须与之前完全一致（含 triggers 与 reasons）。"""
    a = _holding()
    b = _holding(market_state=None)
    assert a.action == b.action
    assert a.triggers == b.triggers
    assert a.reasons == b.reasons


# ══════════════════════════════════════════════════════════════════════════
# 派发 / 趋势破坏：降级到观察
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("state", ["distribution", "trend_break"])
def test_adverse_market_state_downgrades_a_clean_hold_to_watch(state):
    """判定为派发/结构破坏时，「无事可做」的默认必须降级为「观察」。

    否则用户看到的是一次「无需操作」的评估，而市场侧证据已经指向风险。
    """
    decision = _holding(market_state=_state(state))
    assert decision.action == "观察"
    assert any(state in t for t in decision.triggers), decision.triggers
    assert decision.reasons


def test_adverse_state_does_not_override_a_stop_loss_exit():
    """止损优先：判定信号不得把明确的清仓降级为观察。

    价格纪律是**客观事实**（已跌破计划止损位），市场判定是**概率化推断**
    （对他人意图的推测）。让推断推翻事实会把纪律掏空。

    夹具说明：止损需要计划提供锚点（`hard_stop_price`）——没有计划时引擎
    刻意不给清仓（见既有用例「没有计划就没有止损锚点」），因此这里必须给计划。
    """
    plan = {"hard_stop_price": 1800.0, "source": "trade_plan_block"}
    decision = _holding(price=1700.0, average_cost=2000.0, plan=plan, market_state=_state("distribution"))
    assert decision.action == "清仓"


def test_adverse_state_appends_reason_even_when_action_is_unchanged():
    """动作不变时也必须附上理由——否则市场侧证据对用户不可见。"""
    plan = {"hard_stop_price": 1800.0, "source": "trade_plan_block"}
    decision = _holding(price=1700.0, average_cost=2000.0, plan=plan, market_state=_state("trend_break"))
    assert any("trend_break" in t for t in decision.triggers)


# ══════════════════════════════════════════════════════════════════════════
# 洗盘：只说明，不改变动作
# ══════════════════════════════════════════════════════════════════════════


def test_washout_state_never_changes_the_action():
    """洗盘是「可能继续持有」，不是「应该持有」——动作留给用户决定。"""
    base = _holding()
    with_state = _holding(market_state=_state("washout"))
    assert with_state.action == base.action
    assert any("washout" in t for t in with_state.triggers)


# ══════════════════════════════════════════════════════════════════════════
# 弃权：显式声明，不得静默忽略
# ══════════════════════════════════════════════════════════════════════════


def test_abstention_is_disclosed_not_silently_ignored():
    """证据不足时必须显式说明该维度不可用。

    静默忽略会让用户以为「市场侧没问题」，而实际上是没有结论。
    """
    decision = _holding(market_state=_state("insufficient_evidence", confidence=0))
    assert any("证据不足" in t or "insufficient_evidence" in t for t in decision.triggers)
    assert decision.action == _holding().action  # 弃权不改变动作


def test_daily_only_limitation_is_disclosed():
    """仅日线数据时，退出建议要说明该判定的证据限制。"""
    decision = _holding(market_state=_state("washout", daily_only=True))
    joined = " ".join(decision.triggers + decision.reasons + decision.constraints)
    assert "日线" in joined


def test_falsifiers_are_surfaced_to_the_user():
    """失效条件必须传给用户——那是「什么情况下这个判断就不成立了」。"""
    decision = _holding(market_state=_state("distribution"))
    joined = " ".join(decision.reasons + decision.triggers + decision.constraints)
    assert "失效" in joined or "放量下跌" in joined


def test_market_state_without_confidence_is_tolerated():
    """载荷缺字段时不得抛错——判定来自外部，格式容错是必须的。"""
    decision = _holding(market_state={"state": "distribution"})
    assert decision.action in ("观察", "持有", "减仓", "清仓")


# ══════════════════════════════════════════════════════════════════════════
# 13.4 端到端：判定 → 退出决策
# ══════════════════════════════════════════════════════════════════════════


def test_end_to_end_verdict_flows_into_exit_advice_endpoint():
    """存储的判定必须真的影响 `/v1/exit-advice` 的输出。

    这是「接线了」与「端到端可用」的区别：只测服务层函数无法发现
    「端点算了 market_states 却没传下去」这类断链（本轮就出现过一次）。
    """
    from fastapi.testclient import TestClient

    from api.database import ImportedPortfolioPositionDB, SignalVerdictDB, get_db_ctx
    from api.main import app
    from tradingagents.agent.repository import new_id

    client = TestClient(app, raise_server_exceptions=False)
    code = client.post("/v1/auth/request-code", json={"email": "exit-e2e@test.com"}).json()["dev_code"]
    jwt = client.post(
        "/v1/auth/verify-code", json={"email": "exit-e2e@test.com", "code": code}
    ).json()["access_token"]

    from api.services import auth_service

    with get_db_ctx() as db:
        user = auth_service.get_user_by_email(db, "exit-e2e@test.com") if hasattr(
            auth_service, "get_user_by_email"
        ) else None
        if user is None:
            pytest.skip("无法解析测试用户（鉴权服务契约变化）")
        uid = user.id
        db.add(
            ImportedPortfolioPositionDB(
                id=new_id(), user_id=uid, symbol="600519.SH", source="manual",
                current_position=300, available_position=300, average_cost=1850.0,
            )
        )
        db.add(
            SignalVerdictDB(
                id=new_id(), user_id=uid, symbol="600519.SH", as_of="2026-03-04",
                probe="washout", state="distribution", confidence=61, margin=0.8,
                signal_version="washout-v1.0.0",
                falsifiers=[{"description": "次日放量下跌 → 假设失效"}],
            )
        )
        db.commit()

    response = client.get("/v1/exit-advice", headers={"Authorization": f"Bearer {jwt}"})
    assert response.status_code == 200
    body = response.json()
    assert body["decisions"], "应至少返回一条持仓决策"

    decision = body["decisions"][0]
    joined = " ".join(
        list(decision.get("triggers") or [])
        + list(decision.get("reasons") or [])
        + list(decision.get("constraints") or [])
    )
    assert "distribution" in joined or "派发" in joined, joined
    # 失效条件必须传到用户面前（否则「什么情况下这个判断不成立」丢失）
    assert "失效" in joined or "放量下跌" in joined
