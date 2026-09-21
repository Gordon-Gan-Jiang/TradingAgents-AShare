"""标的解析：**不带 symbol 的真实前端路径**（线上 500 的回归测试）。

## 这个文件为什么存在

线上 `/judgement` 页报 `Failed to fetch`，根因是 `chat_service.resolve_symbol`
里写了 `from api.main import _dual_stock_map`——**`api/main.py` 里没有这个名字**，
运行时 ImportError → 未处理异常 → 500。

而 500 响应**不带 CORS 头**（FastAPI 的 `ServerErrorMiddleware` 在 CORS 中间件
之外），浏览器因此拿不到响应、只报 `TypeError: Failed to fetch`。

## 真正的教训：测试盲区

`resolve_symbol` 只在**未显式传 symbol** 时被调用，而此前我写的**每一个** `/v1/chat`
测试都显式传了 `symbol`——于是这条真实主路径从未被执行过，一个必然失败的
ImportError 躲过了 1900+ 个用例。

因此本文件的第一条纪律是：**必须走不带 symbol 的路径**，且必须有一条 HTTP 级
用例（单元级照样会漏掉"名字写错"这类问题——它只在 import 时才暴露）。
"""

from __future__ import annotations

import pytest


_FAKE_MAP = {
    "贵州茅台": "600519.SH",
    "宁德时代": "300750.SZ",
    "平安银行": "000001.SZ",
}


def _patch_map(monkeypatch, mapping=None):
    """把名称映射替换成确定的小表，使解析路径可离线测。

    返回**同一个** dict 对象：真实实现返回的是共享缓存对象（见
    `api/main.py::_get_stock_name_map_cached_only`）。若桩每次造新对象，
    别名索引的按身份缓存就永远命中不了，测不出真实行为。
    """
    import api.main as main

    stable = dict(mapping or _FAKE_MAP)
    monkeypatch.setattr(main, "_get_stock_name_map_cached_only", lambda: stable)


# ══════════════════════════════════════════════════════════════════════════
# 名称解析（此前从未被覆盖的路径）
# ══════════════════════════════════════════════════════════════════════════


def test_resolves_a_chinese_name_without_an_explicit_symbol(monkeypatch):
    """**线上 500 的直接回归**：不带 symbol 时必须能解析中文名。

    此前这里 import 了一个不存在的名字，必然抛 ImportError。
    """
    from api.services.chat_service import resolve_symbol

    _patch_map(monkeypatch)
    symbol, candidates = resolve_symbol("贵州茅台是不是在洗盘", None)

    assert symbol == "600519.SH"
    assert candidates == ()


def test_resolves_plain_code_without_explicit_symbol(monkeypatch):
    from api.services.chat_service import resolve_symbol

    _patch_map(monkeypatch)
    symbol, _ = resolve_symbol("600519 是不是在洗盘", None)
    assert symbol == "600519.SH"


def test_unresolvable_text_returns_no_symbol_rather_than_guessing(monkeypatch):
    """解析不出时必须返回 None（路由转 400 追问），**不得猜一个标的**。"""
    from api.services.chat_service import resolve_symbol

    _patch_map(monkeypatch)
    symbol, candidates = resolve_symbol("帮我看看还能不能拿", None)
    assert symbol is None
    assert candidates == ()


def test_multiple_candidates_are_returned_for_selection(monkeypatch):
    from api.services.chat_service import resolve_symbol

    _patch_map(monkeypatch)
    symbol, candidates = resolve_symbol("茅台和宁德时代哪个好", None)
    assert symbol is None
    assert set(candidates) == {"600519.SH", "300750.SZ"}


def test_one_character_names_do_not_match_everything(monkeypatch):
    """单字名不得命中——否则「美」会匹配到任何含该字的问句。"""
    from api.services.chat_service import resolve_symbol

    _patch_map(monkeypatch, {"美": "000333.SZ", "美的集团": "000333.SZ"})
    symbol, _ = resolve_symbol("今天美不美", None)
    assert symbol is None


def test_explicit_symbol_short_circuits_the_map(monkeypatch):
    """显式传 symbol 时不得再去查映射（避免无谓的冷启动警告与背景刷新）。"""
    import api.main as main
    from api.services.chat_service import resolve_symbol

    called: list[int] = []

    def _boom():
        called.append(1)
        return {}

    monkeypatch.setattr(main, "_get_stock_name_map_cached_only", _boom)
    symbol, _ = resolve_symbol("随便", "600519.SH")
    assert symbol == "600519.SH"
    assert called == []


# ══════════════════════════════════════════════════════════════════════════
# HTTP 级：真实前端路径必须返回 2xx / 4xx，绝不 500
# ══════════════════════════════════════════════════════════════════════════


def _jwt() -> str:
    """直接签发 JWT，**不走验证码邮件链路**。

    走 `/v1/auth/request-code` 会依赖 `_light_executor` 线程池，而套件里
    有测试会启停真实 uvicorn 并关掉模块级线程池——于是本用例单独跑通过、
    整轮跑却拿到非 JSON 响应（500）。本用例要验的是标的解析，不是发信，
    因此把认证这一步收敛成一次纯函数调用。
    """
    from datetime import datetime, timezone
    from uuid import uuid4

    from api.database import UserDB, get_db_ctx, init_db
    from api.services import auth_service

    init_db()
    email = auth_service.normalize_email(f"nosym-{uuid4().hex[:8]}@test.com")
    now = datetime.now(timezone.utc)
    with get_db_ctx() as db:
        user = auth_service.get_user_by_email(db, email)
        if not user:
            user = UserDB(
                id=str(uuid4()), email=email, is_active=True,
                created_at=now, updated_at=now, last_login_at=now,
            )
            db.add(user)
            db.commit()
            db.refresh(user)
    return auth_service.create_access_token(user)


def test_http_chat_without_symbol_never_returns_500(monkeypatch):
    """不带 symbol 的 `/v1/chat` 必须可解析或明确追问，**不得 500**。

    500 会让浏览器只看到 `Failed to fetch`（错误响应缺 CORS 头），
    真实原因被完全掩盖——这是本次事故的传播路径。
    """
    from fastapi.testclient import TestClient

    from api.services import chat_service, signal_service
    from tests.test_acceptance_conversational_signal_agent import _ports_factory
    from tradingagents.tools import build_registry

    factory = _ports_factory()
    monkeypatch.setattr(signal_service, "build_ports_factory", lambda user_id=None: factory)
    monkeypatch.setattr(chat_service, "build_ports_factory", lambda user_id=None: factory)
    monkeypatch.setattr(
        chat_service, "build_registry",
        lambda user_id=None: build_registry(ports_factory=factory, news_fetcher=lambda *a: []),
    )
    from tradingagents.grounding.types import Claim, StructuredAnswer

    class _Model:
        def propose(self, *, prompt, evidence, verdict):
            ref = evidence.items[0].id if evidence.items else None
            unavailable = list(verdict.unavailable) if verdict else []
            if "position" in prompt and "position" not in unavailable:
                unavailable.append("position")
            return StructuredAnswer(
                summary="缩量下杀后出现承接。",
                claims=(Claim(text="量能出现萎缩", refs=(ref,)),) if ref else (),
                verdict_ref=verdict.id if verdict else None,
                declared_unavailable=tuple(unavailable),
            )

        def repair(self, **kw):  # pragma: no cover
            return kw["previous"]

    monkeypatch.setattr(chat_service, "build_answer_model", lambda **kw: _Model())
    _patch_map(monkeypatch)

    from api.main import app

    client = TestClient(app, raise_server_exceptions=False)
    auth = {"Authorization": f"Bearer {_jwt()}"}

    # ① 可解析的中文名 → 正常回答
    ok = client.post(
        "/v1/chat",
        json={"messages": [{"role": "user", "content": "贵州茅台是不是在洗盘"}]},
        headers=auth,
    )
    assert ok.status_code != 500, ok.text[:300]
    assert ok.status_code == 200
    assert "SessionEvent" in ok.text

    # ② 无法解析 → 明确追问（400），不是 500
    clarify = client.post(
        "/v1/chat",
        json={"messages": [{"role": "user", "content": "帮我看看还能不能拿"}]},
        headers=auth,
    )
    assert clarify.status_code == 400, clarify.text[:300]
    assert "标的" in clarify.json()["detail"]


def test_ambiguous_alias_does_not_resolve(monkeypatch):
    """通用后缀（多家共有）不得被猜成某一只股票。"""
    from api.services.chat_service import resolve_symbol

    _patch_map(
        monkeypatch,
        {"招商银行": "600036.SH", "兴业银行": "601166.SH", "贵州茅台": "600519.SH"},
    )
    symbol, candidates = resolve_symbol("银行是不是在洗盘", None)
    assert symbol is None, "「银行」对应两个代码，必须问清楚而不是猜"
    assert candidates == ()


def test_ambiguous_alias_does_not_block_full_name_match(monkeypatch):
    """别名歧义只影响该别名，全名命中照常返回。"""
    from api.services.chat_service import resolve_symbol

    _patch_map(
        monkeypatch,
        {"招商银行": "600036.SH", "兴业银行": "601166.SH", "贵州茅台": "600519.SH"},
    )
    symbol, candidates = resolve_symbol("贵州茅台是不是在洗盘", None)
    assert symbol == "600519.SH"
    assert candidates == ()


def test_alias_and_full_name_are_merged_into_candidates(monkeypatch):
    """全名 + 简称同时出现时两个都要进候选，不能只认全名那个。"""
    from api.services.chat_service import resolve_symbol

    _patch_map(monkeypatch)
    symbol, candidates = resolve_symbol("茅台和宁德时代哪个好", None)
    assert symbol is None
    assert set(candidates) == {"600519.SH", "300750.SZ"}


def test_alias_index_is_cached_per_mapping(monkeypatch):
    """3 万条名称的表不能每个请求重建索引。"""
    import api.main as main
    from api.services import chat_service

    _patch_map(monkeypatch)
    chat_service.resolve_symbol("茅台怎么样", None)
    before = len(chat_service._ALIAS_INDEX_CACHE)
    first = chat_service._alias_index(main._get_stock_name_map_cached_only())
    second = chat_service._alias_index(main._get_stock_name_map_cached_only())
    assert first is second
    assert len(chat_service._ALIAS_INDEX_CACHE) == before


def test_alias_index_scales_to_full_market():
    """全市场规模（3 万只）建索引要够快，且通用词必须被判为歧义。"""
    import time

    from api.services.chat_service import _build_alias_index

    provinces = ["贵州", "江苏", "浙江", "广东", "山东", "北京", "上海", "四川"]
    mapping = {}
    for i in range(30000):
        name = f"{provinces[i % len(provinces)]}某某{i:05d}"
        mapping[name] = f"{600000 + i:06d}.SH"
    mapping["招商银行"] = "600036.SH"
    mapping["兴业银行"] = "601166.SH"
    mapping["贵州茅台"] = "600519.SH"

    start = time.perf_counter()
    index = _build_alias_index(mapping)
    elapsed = time.perf_counter() - start
    assert elapsed < 5.0, f"建索引耗时 {elapsed:.2f}s，超出可接受范围"

    assert index.get("茅台") == "600519.SH", "唯一简称必须可解析"
    assert "银行" not in index, "「银行」有两个持有者，不得进唯一别名表"
    assert "某某" not in index, "30k 名称共有的后缀必须被判为歧义"


def test_digit_aliases_never_enter_the_index():
    """纯数字/含字母的后缀不得成为别名。

    真实事故：基金「诺德研发创新100」的后缀产生别名 `"00"`，而它在全表唯一，
    于是用户输入的 **"600519"** 里恰好含 `"00"` → 解析成 `007737.SZ`
    （那只基金）。用户问 A 股，系统答基金——静默分析错标的是最坏的失败。
    """
    from api.services.chat_service import _build_alias_index

    index = _build_alias_index({"诺德研发创新100": "007737.SZ", "贵州茅台": "600519.SH"})
    for alias in index:
        assert alias.isalpha() or all("\u4e00" <= ch <= "\u9fff" for ch in alias), alias
    assert "00" not in index
    assert "100" not in index
    assert index.get("茅台") == "600519.SH"


def test_explicit_code_in_text_beats_alias_match(monkeypatch):
    """问句里写了代码，就按代码走——代码是用户的显式陈述，不能被模糊别名顶掉。"""
    from api.services.chat_service import resolve_symbol

    _patch_map(monkeypatch, {"诺德研发创新100": "007737.SZ", "贵州茅台": "600519.SH"})
    symbol, candidates = resolve_symbol("600519.SH 是不是在洗盘", None)
    assert symbol == "600519.SH", f"代码已被显式写出，不该解析成 {symbol!r}"
    assert candidates == ()


def test_alias_must_not_match_inside_a_code(monkeypatch):
    """数字串里不得抠出别名：裸代码解析成它自己，而不是那只基金。"""
    from api.services.chat_service import resolve_symbol

    _patch_map(monkeypatch, {"诺德研发创新100": "007737.SZ"})
    symbol, candidates = resolve_symbol("帮我看看 300750 这只票", None)
    assert symbol == "300750.SZ", f"裸代码应解析成它自己，实际 {symbol!r}"
    assert candidates == ()
