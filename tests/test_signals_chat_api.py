"""信号与对话端点的 HTTP 级测试（TestClient，无网络、无模型）。

## 这些用例守的四件事

1. **新端点必须真的可用**，不是「写好了但没人调得通」——本仓库有过
   「实现了却没人用」的先例（`recommendation_eval_items` 0 行、
   `/v1/backtest` 无前端调用者）。
2. **对话端点必须返回回答文本**，而不是既有 `/v1/chat/completions` 那样的
   任务回执。
3. **鉴权与用户隔离**：判定与会话都只属于其所有者。
4. **校准报告必须把「构造保证」与「监控校准」分列**——混在一起会退化成
   「我们加了校验，应该没问题」。

依赖通过替换模块级工厂注入：`api.services.signal_service.build_ports_factory`、
`api.services.chat_service.build_registry` / `build_answer_model`。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from tests._signal_fixtures import make_chips, make_context, make_daily
from tradingagents.grounding.types import Claim, EvidenceSet, StructuredAnswer
from tradingagents.signals.ports import PeerSnapshot, SignalPorts


def _client():
    from api.main import app

    return TestClient(app, raise_server_exceptions=False)


def _auth(client: TestClient, email: str = "signals@test.com") -> str:
    r = client.post("/v1/auth/request-code", json={"email": email})
    code = r.json()["dev_code"]
    r2 = client.post("/v1/auth/verify-code", json={"email": email, "code": code})
    return r2.json()["access_token"]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _fake_ports_factory():
    """给出足以产出 washout 判定的端口。"""

    def factory(symbol: str, as_of: str) -> SignalPorts:
        daily = make_daily([10.0] * 5 + [9.8, 9.6, 9.4, 9.3, 9.6],
                           [1000, 1000, 1000, 1000, 1000, 700, 600, 500, 480, 700])

        class _P:
            def bars(self, s, end, lookback):
                return daily

        class _Peer:
            def snapshot(self, s, day):
                return PeerSnapshot(board_name="白酒", board_return_pct=-3.0,
                                    market_return_pct=-1.5, stock_turnover=2.2,
                                    board_median_turnover=1.8)

        class _Chips:
            def chips(self, s, day):
                return make_chips(profit_ratio=0.3, avg_cost=10.4)

        return SignalPorts(daily=_P(), peer=_Peer(), chips=_Chips())

    return factory


@pytest.fixture()
def wired(monkeypatch):
    """把探针与对话链路的依赖替换为内存实现。"""
    from api.services import chat_service, signal_service
    from tradingagents.tools import build_registry

    factory = _fake_ports_factory()
    monkeypatch.setattr(signal_service, "build_ports_factory", lambda user_id=None: factory)
    monkeypatch.setattr(chat_service, "build_ports_factory", lambda user_id=None: factory)
    monkeypatch.setattr(
        chat_service, "build_registry", lambda user_id=None: build_registry(ports_factory=factory)
    )

    def _model(*, user_id=None, model_profile_id=None, completer=None):
        from tradingagents.agent.model import LlmAnswerModel

        def fake_complete(prompt: str) -> str:
            # 从提示里取第一个证据编号，构造一条有引用的回答
            marker = prompt.find("[E1]")
            ref = "E1" if marker >= 0 else "E1"
            return json.dumps(
                {
                    "summary": "缩量下杀后出现承接，判定为洗盘（震仓）。",
                    "claims": [{"text": "反抽量能恢复至 1.228", "refs": [ref]}],
                    "verdict_ref": "V1",
                    "unavailable": [
                        "capital_composition",
                        "chip_distribution",
                        "intraday_absorption",
                        "ma_structure",
                    ],
                },
                ensure_ascii=False,
            )

        return LlmAnswerModel(fake_complete)

    monkeypatch.setattr(chat_service, "build_answer_model", _model)
    return factory


# ══════════════════════════════════════════════════════════════════════════
# /v1/signals/washout —— 直取判定，无模型调用
# ══════════════════════════════════════════════════════════════════════════


def test_washout_endpoint_requires_auth():
    client = _client()
    r = client.get("/v1/signals/washout", params={"symbol": "600519.SH", "as_of": "2026-01-16"})
    assert r.status_code == 401


def test_washout_endpoint_returns_structured_verdict(wired):
    client = _client()
    token = _auth(client)
    r = client.get(
        "/v1/signals/washout",
        params={"symbol": "600519.SH", "as_of": "2026-01-16"},
        headers=_bearer(token),
    )

    assert r.status_code == 200
    body = r.json()
    assert body["state"] in {"washout", "distribution", "trend_break", "insufficient_evidence"}
    assert isinstance(body["confidence"], int)
    assert body["state_label"]
    assert body["scores"]["washout"] >= 0
    assert "disclaimer" in body
    # 逐维度观测必须返回：回答层要引用、校准层要按维度统计
    assert body["components"]
    assert any(c["key"] == "volume_contraction" for c in body["components"])
    assert body["modulator"]["key"] == "position"


def test_washout_endpoint_returns_falsifiers(wired):
    client = _client()
    token = _auth(client)
    body = client.get(
        "/v1/signals/washout",
        params={"symbol": "600519.SH", "as_of": "2026-01-16"},
        headers=_bearer(token),
    ).json()

    assert body["state"] != "insufficient_evidence"
    assert len(body["falsifiers"]) >= 2
    for f in body["falsifiers"]:
        assert f["observable"] and f["threshold"] and f["window_days"] >= 1
    # 每日线数据时必须自我标注，且不得声称高置信
    assert body["daily_only"] is True
    assert body["high_confidence"] is False


def test_washout_endpoint_maps_evaluation_failure_to_422(monkeypatch):
    from api.services import signal_service

    def _boom(symbol, as_of):
        raise RuntimeError("provider unreachable")

    monkeypatch.setattr(signal_service, "build_ports_factory", lambda user_id=None: _boom)

    client = _client()
    token = _auth(client)
    r = client.get(
        "/v1/signals/washout",
        params={"symbol": "600519.SH", "as_of": "2026-01-16"},
        headers=_bearer(token),
    )
    assert r.status_code == 422
    assert "600519" in r.json()["detail"]


def test_washout_endpoint_persists_and_lists_verdict(wired):
    client = _client()
    token = _auth(client, "persist@test.com")
    r = client.get(
        "/v1/signals/washout",
        params={"symbol": "600519.SH", "as_of": "2026-01-16", "persist": "true"},
        headers=_bearer(token),
    )
    assert r.status_code == 200
    assert r.json()["verdict_id"]

    listed = client.get("/v1/signals/verdicts", headers=_bearer(token)).json()
    assert listed["count"] == 1
    assert listed["items"][0]["symbol"] == "600519.SH"


def test_verdicts_are_isolated_per_user(wired):
    client = _client()
    token_a = _auth(client, "verdict-a@test.com")
    token_b = _auth(client, "verdict-b@test.com")

    client.get(
        "/v1/signals/washout",
        params={"symbol": "600519.SH", "as_of": "2026-01-16", "persist": "true"},
        headers=_bearer(token_a),
    )
    assert client.get("/v1/signals/verdicts", headers=_bearer(token_b)).json()["count"] == 0


def test_calibration_separates_construction_guarantees_from_monitoring(wired):
    client = _client()
    token = _auth(client, "calib@test.com")
    body = client.get("/v1/signals/calibration", headers=_bearer(token)).json()

    assert "guaranteed_by_construction" in body
    assert body["guaranteed_by_construction"]["unsourced_numeric_assertion_rate"] == 0.0
    assert "monitored" in body
    assert body["monitored"]["abstention_rate_healthy_range"] == [0.30, 0.50]
    gate = body["weight_activation_gate"]
    assert gate["min_forward_verdicts"] == 100
    assert gate["min_per_dimension_samples"] == 30
    assert gate["satisfied"] is False  # 样本不足时必须如实说不足


# ══════════════════════════════════════════════════════════════════════════
# /v1/chat —— 流式回答
# ══════════════════════════════════════════════════════════════════════════


def _stream_events(response) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    current = None
    for line in response.text.splitlines():
        if line.startswith("event: "):
            current = line[7:]
        elif line.startswith("data: ") and current:
            events.append((current, json.loads(line[6:])))
    return events


def test_chat_requires_auth():
    client = _client()
    r = client.post("/v1/chat", json={"messages": [{"role": "user", "content": "600519 是不是在洗盘"}]})
    assert r.status_code == 401


def test_chat_streams_an_answer_not_a_job_acknowledgement(wired):
    """**核心回归**：既有 `/v1/chat/completions` 只回「已启动分析任务」。

    新端点必须给出真正的回答，并流出工具轨迹与结构化判定。
    """
    client = _client()
    token = _auth(client, "chat@test.com")
    r = client.post(
        "/v1/chat",
        json={
            "messages": [{"role": "user", "content": "600519 是不是在洗盘"}],
            "symbol": "600519.SH",
            "as_of": "2026-01-16",
        },
        headers=_bearer(token),
    )

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    events = _stream_events(r)
    kinds = [k for k, _ in events]

    assert "AnswerDelta" in kinds
    assert "VerdictEvent" in kinds
    assert "ToolStart" in kinds and "ToolDone" in kinds
    assert kinds[-1] == "EndEvent"

    answer = "".join(d["text"] for k, d in events if k == "AnswerDelta")
    assert answer.strip()
    assert "已启动分析任务" not in answer

    verdict = next(d["verdict"] for k, d in events if k == "VerdictEvent")
    assert verdict["state"] in {"washout", "distribution", "trend_break", "insufficient_evidence"}


def test_chat_persists_conversation_and_returns_it(wired):
    client = _client()
    token = _auth(client, "conv@test.com")
    client.post(
        "/v1/chat",
        json={
            "messages": [{"role": "user", "content": "600519 是不是在洗盘"}],
            "symbol": "600519.SH",
            "as_of": "2026-01-16",
        },
        headers=_bearer(token),
    )

    conversations = client.get("/v1/conversations", headers=_bearer(token)).json()
    assert conversations["count"] == 1
    cid = conversations["items"][0]["conversation_id"]

    detail = client.get(f"/v1/conversations/{cid}", headers=_bearer(token)).json()
    roles = [m["role"] for m in detail["messages"]]
    assert roles == ["user", "assistant"]
    assert detail["messages"][1]["content"].strip()


def test_conversation_is_not_visible_to_other_users(wired):
    client = _client()
    token_a = _auth(client, "conv-a@test.com")
    token_b = _auth(client, "conv-b@test.com")

    client.post(
        "/v1/chat",
        json={
            "messages": [{"role": "user", "content": "600519 是不是在洗盘"}],
            "symbol": "600519.SH",
            "as_of": "2026-01-16",
        },
        headers=_bearer(token_a),
    )
    cid = client.get("/v1/conversations", headers=_bearer(token_a)).json()["items"][0]["conversation_id"]

    # 跨用户访问必须拒绝，且不披露记录是否存在（统一 404）
    r = client.get(f"/v1/conversations/{cid}", headers=_bearer(token_b))
    assert r.status_code == 404
    assert client.get("/v1/conversations", headers=_bearer(token_b)).json()["count"] == 0


def test_chat_light_tier_does_not_start_the_probe(wired):
    """轻档问句不得付探针成本。"""
    client = _client()
    token = _auth(client, "light@test.com")
    r = client.post(
        "/v1/chat",
        json={
            "messages": [{"role": "user", "content": "600519 现在多少钱"}],
            "symbol": "600519.SH",
            "as_of": "2026-01-16",
            "tier": "light",
        },
        headers=_bearer(token),
    )
    events = _stream_events(r)
    assert not [k for k, _ in events if k == "VerdictEvent"]
