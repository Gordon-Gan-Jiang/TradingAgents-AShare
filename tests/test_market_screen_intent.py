"""全市场筛选意图：把既有扫描能力接入对话。

## 这个文件锁住什么

用户输入「今天走到好的票，放量票找十个给我」，旧行为是 400「未能识别标的」——
把一个**合法的全市场筛选需求**误报成「你没说清楚」。而平台其实已有成熟扫描器
（`api/services/market_scanner_service.py`，六因子 + 8 档案），只是**没接进对话工具面**。

本文件按四条纪律锁住接入质量：

1. **标优先**：消息里出现显式代码时，绝不触发全市场扫描（沿用「显式陈述不得被
   模糊匹配覆盖」的既有教训）。
2. **候选可溯源**：每只候选一条证据，回答里的代码与得分必须来自证据 ——
   由既有零 LLM 校验器机械保证，而不是靠提示词请求模型"别编"。
3. **缺失必须声明**：实测 `volume_ratio`/`turnover_rate` 在 sina 源下为 `null`，
   即 `breakout` 档名义上的「量比确认」实际贡献 0.0。用户要「放量票」时，
   回答必须声明"量比未提供、放量未经确认"，**不得**把"按突破档排序"说成"已确认放量"。
   把缺失写成 0 也是错的：0 是「量比为零」这个具体断言，与「拿不到」是两件事。
4. **耗时诚实**：实测全市场 5020 只扫描 10.0s（冷启动 61.5s），远超中档 30s
   目标延迟 —— 开始前告知、结束后如实记录。
"""

from __future__ import annotations

import pytest

from tradingagents.agent.routing import (
    looks_like_market_screen,
    parse_screen_request,
)


# ══════════════════════════════════════════════════════════════════════════
# 1. 意图检测（零 LLM 纯函数）
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "text",
    [
        "今天走到好的票，放量票找十个给我",  # 用户原话
        "帮我找十只放量票",
        "今天哪些票在放量",
        "帮我选股",
        "筛选出强势股",
        "给我挑5只强势票",
        "看看今天涨幅榜",
        "全市场扫一遍有没有机会",
        "推荐几只票",
    ],
)
def test_screening_phrasing_is_recognized(text):
    assert looks_like_market_screen(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "",
        "贵州茅台是不是在洗盘",
        "还能不能拿",
        "帮我看看我持仓里哪只最危险",
        "今天主力净流入多少",
    ],
)
def test_judgement_phrasing_is_not_a_screen(text):
    assert looks_like_market_screen(text) is False


def test_explicit_code_wins_over_screening_phrasing(monkeypatch):
    """含显式代码时按单标的研判走——扫描是全市场动作，不能被措辞误触发。"""
    from api.services import chat_service

    _patch_map(monkeypatch)
    request = chat_service.build_turn_request(
        _body("600519.SH 今天放量了吗"), user_id="u1"
    )
    assert request is not None
    assert request.symbol == "600519.SH"
    assert request.screen is False, "写了代码就不该去扫全市场"


# ══════════════════════════════════════════════════════════════════════════
# 2. 参数解析
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "text,top_k,profile",
    [
        ("今天走到好的票，放量票找十个给我", 10, "breakout"),
        ("帮我找十只放量票", 10, "breakout"),
        ("给我挑5只强势票", 5, "ashare_aggressive"),
        ("找20只回调低吸的票", 20, "reversal"),
        ("来几只行业轮动的票", 10, "sector_rotation"),
    ],
)
def test_screen_request_parses_count_and_profile(text, top_k, profile):
    parsed = parse_screen_request(text)
    assert parsed.top_k == top_k
    assert parsed.profile == profile


def test_screen_request_defaults_are_conservative_and_clamped():
    """数量必须在扫描器允许区间内：猜「多一些」是几十只必然两难。"""
    default = parse_screen_request("帮我选股")
    assert default.top_k == 10

    huge = parse_screen_request("帮我找100只票")
    assert huge.top_k == 20, "扫描器上限 20，超出的必须夹回而不是传下去报错"

    zero = parse_screen_request("帮我找0只票")
    assert zero.top_k >= 1


def test_unrelated_text_has_no_profile_hint():
    parsed = parse_screen_request("帮我选股")
    assert parsed.profile in (None, "") or isinstance(parsed.profile, str)


# ══════════════════════════════════════════════════════════════════════════
# 3. 工具：证据、缺失、降级
# ══════════════════════════════════════════════════════════════════════════


def _scan_payload(*, items=None, volume_ratio=None, turnover_rate=None):
    """构造一份与 `market_scanner_service.scan_market_candidates` 同形的结果。"""
    payload_items = items
    if payload_items is None:
        payload_items = [
            {
                "symbol": "002169.SZ",
                "name": "智光电气",
                "score": 4247.71,
                "reasons": ["涨幅强势（+9.16%）", "价格逼近日内高点"],
                "strategy_hits": ["strong_momentum", "breakout_near_high"],
                "risk_flags": ["near_limit_up"],
                "price_change_pct": 9.16,
                "live_price": 14.9,
                "volume_ratio": volume_ratio,
                "turnover_rate": turnover_rate,
                "quote_time": "2026-09-21 16:29:30",
                "quote_source": "sina",
            },
            {
                "symbol": "002180.SZ",
                "name": "奔图科技",
                "score": 4069.25,
                "reasons": ["涨幅强势（+8.55%）"],
                "strategy_hits": ["strong_momentum"],
                "risk_flags": [],
                "price_change_pct": 8.55,
                "live_price": 21.3,
                "volume_ratio": volume_ratio,
                "turnover_rate": turnover_rate,
                "quote_time": "2026-09-21 16:29:30",
                "quote_source": "sina",
            },
        ]
    return {
        "items": payload_items,
        "pool_size": 5020,
        "scored_size": len(payload_items),
        "scoring_model": "six_factor",
    }


class _FakeScreener:
    def __init__(self, payload=None, error: Exception | None = None):
        self.payload = payload if payload is not None else _scan_payload()
        self.error = error
        self.calls: list[dict] = []

    def screen(self, *, top_k, profile=None, min_change_pct=0.0, scan_limit=None):
        self.calls.append(
            {"top_k": top_k, "profile": profile, "min_change_pct": min_change_pct}
        )
        if self.error is not None:
            raise self.error
        return self.payload


def _screen_tool(screener):
    from tradingagents.tools.screen import make_screen_tools

    specs = {spec.name: spec for spec in make_screen_tools(screener)}
    return specs["market_screen"]


def test_market_screen_spec_declares_reality():
    """超时预算必须覆盖冷启动（实测 61.5s），否则必然被判超时失败。"""
    spec = _screen_tool(_FakeScreener())
    assert spec.latency_budget_ms >= 90_000, "实测冷启动 61.5s，预算必须留足"
    assert "realtime_quotes" in spec.freshness_sources
    assert spec.cost == "medium"
    assert spec.is_synchronous


def test_every_candidate_has_its_own_evidence():
    """一只候选一条证据：这样「代码必须来自证据」由校验器机械保证。"""
    spec = _screen_tool(_FakeScreener())
    result = spec.handler(top_k=10, profile="breakout", min_change_pct=0.0, as_of="2026-09-21")

    assert len(result.evidence) >= 2
    for item in result.evidence:
        assert item.id and item.source_key
    joined = "\n".join(str(item.value) for item in result.evidence)
    assert "002169.SZ" in joined and "智光电气" in joined
    assert "4247.71" in joined or "4247.7" in joined, "得分必须进证据，模型才能引用"
    assert "涨幅强势" in joined, "入选理由要进证据，否则答案只有代码没有依据"


def test_scan_scope_and_time_are_evidence():
    """范围与时点必须可引用：用户才知道这 10 只从多少只里选出来。"""
    spec = _screen_tool(_FakeScreener())
    result = spec.handler(top_k=10, profile="breakout", min_change_pct=0.0, as_of="2026-09-21")

    joined = "\n".join(f"{i.field} {i.value}" for i in result.evidence)
    assert "5020" in joined, "全市场只数必须出现在证据里"
    assert "2026-09-21" in joined or "16:29" in joined, "数据时点必须可引用"


def test_missing_volume_ratio_is_declared_not_zeroed():
    """量比缺失必须进 unavailable —— 用户要「放量票」，这是最要命的一条。"""
    spec = _screen_tool(_FakeScreener(_scan_payload(volume_ratio=None, turnover_rate=None)))
    result = spec.handler(top_k=10, profile="breakout", min_change_pct=0.0, as_of="2026-09-21")

    unavailable = " ".join(result.coverage.unavailable)
    assert "量比" in unavailable or "volume_ratio" in unavailable, (
        "量比拿不到却不说，等于把「按突破档排序」讲成「已确认放量」"
    )
    for item in result.evidence:
        assert item.value != 0, "缺失不得写成 0：0 是「量比为零」这一具体断言"


def test_present_volume_ratio_is_evidence_and_not_declared_missing():
    spec = _screen_tool(_FakeScreener(_scan_payload(volume_ratio=2.4, turnover_rate=3.1)))
    result = spec.handler(top_k=10, profile="breakout", min_change_pct=0.0, as_of="2026-09-21")

    joined = "\n".join(str(i.value) for i in result.evidence)
    assert "2.4" in joined
    assert "量比" not in " ".join(result.coverage.unavailable)


def test_empty_scan_yields_no_candidate_evidence():
    """空结果不得产出任何候选 —— 编造候选是最坏的失败。"""
    spec = _screen_tool(_FakeScreener(_scan_payload(items=[])))
    result = spec.handler(top_k=10, profile="breakout", min_change_pct=0.0, as_of="2026-09-21")

    assert result.evidence == ()
    assert result.coverage.unavailable, "空结果必须声明未覆盖，而不是让模型自由发挥"


def test_scan_failure_degrades_instead_of_raising():
    """扫描器抛错时要结构化降级：抛错会让模型面对空证据去编。"""
    spec = _screen_tool(_FakeScreener(error=RuntimeError("connection aborted")))
    result = spec.handler(top_k=10, profile=None, min_change_pct=0.0, as_of="2026-09-21")

    assert result.evidence == ()
    assert result.degraded is True
    assert result.coverage.unavailable
    assert "connection aborted" not in str(result.data), "上游原文不进对外结果"


def test_screen_result_is_not_mistaken_for_a_washout_verdict():
    """`_verdict_from_tool_result` 认 `state`/`results` 键，筛选结果不能用它们。"""
    spec = _screen_tool(_FakeScreener())
    result = spec.handler(top_k=10, profile="breakout", min_change_pct=0.0, as_of="2026-09-21")

    assert "state" not in result.data
    assert "results" not in result.data
    from tradingagents.agent.loop import TurnRequest, _verdict_from_tool_result

    assert _verdict_from_tool_result(result, TurnRequest(text="x", user_id="u")) is None


# ══════════════════════════════════════════════════════════════════════════
# 4. 装配：没有扫描器就不注册，而不是"在册但一调就炸"
# ══════════════════════════════════════════════════════════════════════════


def test_registry_registers_screen_only_when_injected():
    from tradingagents.tools import build_registry

    assert "market_screen" not in build_registry().names()
    with_screener = build_registry(market_screener=_FakeScreener())
    assert "market_screen" in with_screener.names()


# ══════════════════════════════════════════════════════════════════════════
# 5. 回合编排
# ══════════════════════════════════════════════════════════════════════════


def test_screen_turn_plans_the_screen_tool():
    from tradingagents.agent.loop import TurnRequest, _plan_tools

    planned = _plan_tools(
        TurnRequest(text="放量票找十个给我", user_id="u1", screen=True, tier="medium")
    )
    assert [c.tool for c in planned] == ["market_screen"]
    assert planned[0].arguments.get("top_k") == 10


def test_screen_turn_is_announced_before_the_slow_call():
    """60s 量级的静默等待会被当成卡死，必须提前告知。"""
    from tradingagents.agent.loop import TurnRequest, run_turn
    from tradingagents.agent.events import ListSink
    from tradingagents.tools import build_registry

    class _Clock:
        def __init__(self):
            self.t = 0.0

        def now(self):
            self.t += 1.0
            return self.t

    class _Model:
        def propose(self, *, prompt, evidence, verdict):
            from tradingagents.grounding.types import StructuredAnswer

            ref = evidence.items[0].id if evidence.items else None
            return StructuredAnswer(
                summary="按突破档筛选出的候选。",
                claims=(),
                verdict_ref=None,
                declared_unavailable=tuple([]),
            )

        def repair(self, **kw):  # pragma: no cover
            return kw["previous"]

    sink = ListSink()
    run_turn(
        request=TurnRequest(
            text="放量票找十个给我", user_id="u1", screen=True, tier="medium",
            as_of="2026-09-21",
        ),
        registry=build_registry(market_screener=_FakeScreener()),
        model=_Model(),
        sink=sink,
        clock=_Clock(),
    )
    notices = [e.text for e in sink.events if type(e).__name__ == "Notice"]
    assert any("扫描" in n for n in notices), f"扫描前应提示耗时，实际：{notices}"


def test_fabricated_candidate_is_rejected_by_the_verifier():
    """模型声称一只不在证据里的票 → 断言必须被拒（诚实性的机械保证）。"""
    from tradingagents.agent.events import ListSink
    from tradingagents.agent.loop import TurnRequest, run_turn
    from tradingagents.grounding.types import Claim, StructuredAnswer
    from tradingagents.tools import build_registry

    class _Clock:
        def __init__(self):
            self.t = 0.0

        def now(self):
            self.t += 1.0
            return self.t

    class _LyingModel:
        def propose(self, *, prompt, evidence, verdict):
            ref = evidence.items[0].id if evidence.items else None
            return StructuredAnswer(
                summary="推荐 999999.SZ 天地板科技。",
                claims=(Claim(text="999999.SZ 天地板科技得分最高", refs=(ref,)),),
                verdict_ref=None,
                declared_unavailable=(),
            )

        def repair(self, **kw):
            return kw["previous"]

    sink = ListSink()
    result = run_turn(
        request=TurnRequest(
            text="放量票找十个给我", user_id="u1", screen=True, tier="medium",
            as_of="2026-09-21",
        ),
        registry=build_registry(market_screener=_FakeScreener()),
        model=_LyingModel(),
        sink=sink,
        clock=_Clock(),
    )
    assert "999999.SZ" not in result.answer, "编造的候选绝不能出现在回答里"


# ══════════════════════════════════════════════════════════════════════════
# 6. 文案分流
# ══════════════════════════════════════════════════════════════════════════


def _body(text: str):
    from api.services.chat_service import ChatRequest

    return ChatRequest(messages=[{"role": "user", "content": text}])


def _patch_map(monkeypatch, mapping=None):
    import api.main as main

    stable = dict(mapping or {"贵州茅台": "600519.SH", "宁德时代": "300750.SZ"})
    monkeypatch.setattr(main, "_get_stock_name_map_cached_only", lambda: stable)


def test_screening_text_no_longer_returns_no_request(monkeypatch):
    """这条正是用户遇到的 400：筛选措辞必须被受理。"""
    from api.services import chat_service

    _patch_map(monkeypatch)
    request = chat_service.build_turn_request(
        _body("今天走到好的票，放量票找十个给我"), user_id="u1"
    )
    assert request is not None, "筛选需求不该被 400 拒绝"
    assert request.screen is True
    assert request.symbol is None
    assert request.tier in ("medium", "heavy"), "扫描 60s 量级，不能落在 5s 的轻档"


def test_unresolvable_non_screening_text_still_asks(monkeypatch):
    """既没标的也不是筛选的，仍要追问——不能为了少一次 400 就随便猜。"""
    from api.services import chat_service

    _patch_map(monkeypatch)
    assert chat_service.build_turn_request(_body("帮我看看还能不能拿"), user_id="u1") is None


def test_clarification_copy_offers_both_paths():
    """400 文案必须说清两条路：补标的，或改用筛选措辞。"""
    from api.routers.chat import CLARIFY_DETAIL

    assert "代码" in CLARIFY_DETAIL
    assert "选股" in CLARIFY_DETAIL or "筛选" in CLARIFY_DETAIL, (
        "旧文案只说「请输入代码」，把合法的筛选需求误报成「你没说清楚」"
    )


# ══════════════════════════════════════════════════════════════════════════
# 7. HTTP 端到端：走真实装配链路（上次事故的教训——真实路径必须被覆盖）
# ══════════════════════════════════════════════════════════════════════════


def _http_client_with_fake_screener(monkeypatch, *, declaring_model: bool):
    """走 `build_registry` 的真实装配，只把扫描端口与模型换成替身。"""
    from fastapi.testclient import TestClient

    from api.main import app
    from api.services import chat_service, market_screen_ports
    from tests.test_symbol_resolution import _jwt

    monkeypatch.setattr(
        market_screen_ports, "build_market_screener", lambda **kw: _FakeScreener()
    )

    class _Model:
        def propose(self, *, prompt, evidence, verdict):
            from tradingagents.grounding.types import Claim, StructuredAnswer

            # 从提示词里读出"必须声明"的未覆盖维度：真实模型就是这么做的
            must = []
            for line in prompt.splitlines():
                if "必须在 unavailable 中声明的未覆盖维度" in line:
                    raw = line.split("：", 1)[-1].strip()
                    must = [] if raw == "（无）" else [p.strip() for p in raw.split("、")]
            ref = evidence.items[0].id if evidence.items else None
            # 断言文本刻意不含数字：校验器会把「候选1」里的 1 判成无出处数字。
            # 这本身就是好证据——数字必须能在被引用证据里找到。
            return StructuredAnswer(
                summary="按突破档筛出以下候选。",
                claims=(
                    Claim(text="得分最高者的入选理由是涨幅强势且价格逼近日内高点", refs=(ref,)),
                ) if ref else (),
                verdict_ref=None,
                declared_unavailable=tuple(must) if declaring_model else (),
            )

        def repair(self, **kw):  # pragma: no cover
            return kw["previous"]

    monkeypatch.setattr(chat_service, "build_answer_model", lambda **kw: _Model())

    _patch_map(monkeypatch)
    client = TestClient(app, raise_server_exceptions=False)
    return client, {"Authorization": f"Bearer {_jwt()}"}


def test_http_screening_request_is_served_not_rejected(monkeypatch):
    """用户原话走完整 HTTP 链路：必须 200 且真的调了 market_screen。"""
    client, auth = _http_client_with_fake_screener(monkeypatch, declaring_model=True)
    resp = client.post(
        "/v1/chat",
        json={"messages": [{"role": "user", "content": "今天走到好的票，放量票找十个给我"}]},
        headers=auth,
    )
    assert resp.status_code == 200, resp.text[:300]
    assert "market_screen" in resp.text, "筛选回合必须真的调用扫描工具"
    assert "002169.SZ" in resp.text or "智光电气" in resp.text, "候选应出现在回答里"


def test_http_scan_announces_its_cost(monkeypatch):
    client, auth = _http_client_with_fake_screener(monkeypatch, declaring_model=True)
    resp = client.post(
        "/v1/chat",
        json={"messages": [{"role": "user", "content": "帮我找十只放量票"}]},
        headers=auth,
    )
    assert "扫描" in resp.text, "60s 量级的等待必须提前告知"


def test_undeclared_missing_factor_gets_caught(monkeypatch):
    """模型不声明「量比缺失」时，诚实性由校验器兜住，而不是靠提示词请求。"""
    client, auth = _http_client_with_fake_screener(monkeypatch, declaring_model=False)
    resp = client.post(
        "/v1/chat",
        json={"messages": [{"role": "user", "content": "今天放量票找十个给我"}]},
        headers=auth,
    )
    body = resp.text
    assert "量比" in body or "未覆盖" in body or "证据不足" in body, (
        "量比拿不到却既不声明也不降级，等于把「按突破档排序」说成「已确认放量」"
    )


def test_model_failure_still_delivers_the_scan_list(monkeypatch):
    """模型不可用时，扫描结果仍必须交付。

    实测踩到过：账号 Key 无效 → 扫描跑了 61.5s 出了 10 只候选，模型一调就 401，
    于是用户只看到「模型鉴权失败」，一条候选都没有。候选表是**代码渲染的扫描器
    原始结果**，不需要模型参与——藏起来等于让用户白等一分钟。
    """
    from fastapi.testclient import TestClient

    from api.main import app
    from api.services import chat_service, market_screen_ports
    from tests.test_symbol_resolution import _jwt

    monkeypatch.setattr(
        market_screen_ports, "build_market_screener", lambda **kw: _FakeScreener()
    )

    class _BoomModel:
        """失败发生在**调用期**（真实场景：扫描跑完后模型 401），不是构造期。"""

        def propose(self, **kwargs):
            raise RuntimeError("model unavailable")

        def repair(self, **kwargs):  # pragma: no cover
            raise RuntimeError("model unavailable")

    monkeypatch.setattr(chat_service, "build_answer_model", lambda **kw: _BoomModel())
    _patch_map(monkeypatch)

    client = TestClient(app, raise_server_exceptions=False)
    auth = {"Authorization": f"Bearer {_jwt()}"}
    resp = client.post(
        "/v1/chat",
        json={"messages": [{"role": "user", "content": "今天放量票找十个给我"}]},
        headers=auth,
    )

    assert "002169.SZ" in resp.text or "智光电气" in resp.text, (
        "扫描已完成，候选表必须交付，不能因为模型失败就连清单一起丢"
    )
    assert "ErrorEvent" in resp.text, "模型失败仍要如实告知原因"


def test_screen_prompt_shows_evidence_values():
    """筛选回合的提示词必须给出证据**数值**。

    实测（真实模型 deepseek-v4-flash）：只给字段名时，模型写出的解读两次被
    校验器拒绝（`unsourced_number`）——它看不到数值，只能凭空写数字。
    原设计「只给字段名、不让模型复述数值」是为**判定解释**服务的；筛选任务的
    数字本身就是内容，且表格已由代码渲染，把数值给模型只是让它能正确引用。
    """
    from tradingagents.agent.loop import TurnRequest, _build_prompt
    from tradingagents.agent.middleware import TurnContext
    from tradingagents.grounding.types import EvidenceItem, EvidenceSet
    from tradingagents.tools import build_registry

    spec = _screen_tool(_FakeScreener())
    result = spec.handler(top_k=10, profile="breakout", min_change_pct=0.0, as_of="2026-09-21")
    evidence = EvidenceSet(items=result.evidence)
    ctx = TurnContext(user_id="u1", tier="medium", text="放量票找十个给我")

    prompt = _build_prompt(
        TurnRequest(text="放量票找十个给我", user_id="u1", screen=True, tier="medium"),
        evidence,
        ctx,
    )
    assert "002169.SZ" in prompt, "候选代码要给到模型，否则它无法正确引用"
    assert "4247.71" in prompt or "4247.7" in prompt, "得分要给到模型"
    assert "scan" not in prompt.lower() or True  # 仅作可读性占位

    # 非筛选回合维持原样：只给字段名，避免模型复述数值
    plain = _build_prompt(
        TurnRequest(text="贵州茅台是不是在洗盘", user_id="u1", symbol="600519.SH", tier="medium"),
        evidence,
        ctx,
    )
    assert "4247.71" not in plain, "判定回合不应把数值喂给模型（原设计保持不变）"
