"""对话回答必须用**用户在设置里选的那套模型**（模型档案），否则等于没配。

## 为什么需要这个文件

`chat_service.build_llm_completer` 里算出了 `profile_id`，却把
`_build_runtime_config({}, user_id=user_id, db=None)` 的空 overrides 传了下去——
**档案解析结果被丢掉**。于是：

* 用户新建并设为默认的模型档案（`model_profiles`）对对话**完全无效**；
* 缺 Key 时还会提示"尚未配置可用的模型"，而用户明明配了——**错误提示指错方向**；
* 报告分析路径（`api/main.py` 的 `_merge_model_profile_overrides` + `strategy="profile_selected"`）
  与对话路径**装配口径分叉**，同一个账号两条路径用不同模型。

本文件锁住"对话与报告用同一套装配"这一条：断言档案里的 provider/model/base_url/api_key
真的流到了 LLM 客户端工厂。
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4


def _user() -> str:
    from api.database import UserDB, get_db_ctx, init_db
    from api.services import auth_service

    init_db()
    email = auth_service.normalize_email(f"profile-wire-{uuid4().hex[:8]}@test.com")
    now = datetime.now(timezone.utc)
    with get_db_ctx() as db:
        user = UserDB(
            id=str(uuid4()), email=email, is_active=True,
            created_at=now, updated_at=now, last_login_at=now,
        )
        db.add(user)
        db.commit()
        return user.id


def _capture_client(monkeypatch) -> list[dict]:
    """拦住 LLM 客户端工厂，捕获它实际收到的装配参数。"""
    calls: list[dict] = []

    class _FakeLlm:
        def invoke(self, prompt):  # pragma: no cover - 本用例不真的调用模型
            raise AssertionError("不该真的调用模型")

    class _FakeClient:
        def get_llm(self):
            return _FakeLlm()

    def _factory(provider, model, **kwargs):
        calls.append({"provider": provider, "model": model, **kwargs})
        return _FakeClient()

    import tradingagents.llm_clients.factory as factory

    monkeypatch.setattr(factory, "create_llm_client", _factory)
    return calls


def test_chat_uses_the_default_model_profile(monkeypatch):
    """设为默认的模型档案必须作用于对话路径。"""
    from api.database import get_db_ctx
    from api.services import chat_service, model_profile_service

    user_id = _user()
    with get_db_ctx() as db:
        model_profile_service.create_model_profile(
            db,
            user_id=user_id,
            name="对话专用档",
            llm_provider="openai",
            backend_url="https://profile.example/v1",
            quick_think_llm="profile-model-quick",
            deep_think_llm="profile-model-deep",
            api_key="sk-profile-secret",
            is_default=True,
        )

    calls = _capture_client(monkeypatch)
    chat_service.build_llm_completer(user_id=user_id)

    assert calls, "应当构造了一次 LLM 客户端"
    call = calls[0]
    assert call["provider"] == "openai"
    assert call["model"] == "profile-model-deep", (
        f"对话应使用档案里的模型，实际：{call['model']!r}——"
        "说明档案解析结果没传进运行时配置"
    )
    assert call.get("base_url") == "https://profile.example/v1"
    # Key 必须显式传到工厂：`OpenAIClient` 只从 kwargs 透传 api_key，
    # 否则会退到 OPENAI_API_KEY 环境变量，档案里的 Key 形同不存在。
    assert call.get("api_key") == "sk-profile-secret", (
        f"档案里的 API Key 没传到客户端工厂，实际：{call.get('api_key')!r}"
    )


def test_chat_uses_an_explicitly_selected_profile(monkeypatch):
    """显式指定的 profile（前端选择）优先于默认档。"""
    from api.database import get_db_ctx
    from api.services import chat_service, model_profile_service

    user_id = _user()
    with get_db_ctx() as db:
        model_profile_service.create_model_profile(
            db, user_id=user_id, name="默认档", llm_provider="openai",
            deep_think_llm="model-default", api_key="sk-default", is_default=True,
        )
        picked = model_profile_service.create_model_profile(
            db, user_id=user_id, name="被选中的档", llm_provider="openai",
            deep_think_llm="model-picked", api_key="sk-picked",
        )

    calls = _capture_client(monkeypatch)
    chat_service.build_llm_completer(user_id=user_id, model_profile_id=str(picked["id"]))

    assert calls[0]["model"] == "model-picked"


def test_profile_selection_uses_profile_strategy(monkeypatch):
    """选中档案时必须用 `profile_selected` 策略。

    该策略会剔除全局模型覆盖键——否则「设置页改的默认模型」会盖掉用户
    在当前档案里的选择，表现为"选了却不生效"。
    """
    from api.database import get_db_ctx
    from api.services import chat_service, model_profile_service

    user_id = _user()
    with get_db_ctx() as db:
        model_profile_service.create_model_profile(
            db, user_id=user_id, name="策略档", llm_provider="openai",
            deep_think_llm="model-strategy", api_key="sk-strategy", is_default=True,
        )

    seen: dict = {}
    import api.main as main

    original = main._build_runtime_config

    def _spy(overrides, **kwargs):
        seen["overrides"] = dict(overrides or {})
        seen["strategy"] = kwargs.get("strategy")
        return original(overrides, **kwargs)

    monkeypatch.setattr(main, "_build_runtime_config", _spy)
    _capture_client(monkeypatch)
    chat_service.build_llm_completer(user_id=user_id)

    assert seen.get("strategy") == "profile_selected", (
        f"策略应为 profile_selected，实际：{seen.get('strategy')!r}"
    )
    assert seen.get("overrides", {}).get("deep_think_llm") == "model-strategy", (
        "档案 overrides 必须真的传进运行时配置"
    )


def test_missing_profile_still_falls_back_to_legacy_config(monkeypatch):
    """没有档案时不得抛错——要退回既有 env/用户设置兜底路径。"""
    from api.services import chat_service

    user_id = _user()
    calls = _capture_client(monkeypatch)
    chat_service.build_llm_completer(user_id=user_id)
    assert len(calls) == 1, "无档案时必须仍能构造客户端（走兜底配置）"


def _user_and_jwt() -> tuple[str, str]:
    """建一个用户并签发 JWT（HTTP 级用例需要 user_id 才能挂档案）。"""
    from datetime import datetime, timezone
    from uuid import uuid4

    from api.database import UserDB, get_db_ctx, init_db
    from api.services import auth_service

    init_db()
    email = auth_service.normalize_email(f"http-profile-{uuid4().hex[:8]}@test.com")
    now = datetime.now(timezone.utc)
    with get_db_ctx() as db:
        user = UserDB(
            id=str(uuid4()), email=email, is_active=True,
            created_at=now, updated_at=now, last_login_at=now,
        )
        db.add(user)
        db.commit()
        return user.id, auth_service.create_access_token(user)


def test_http_chat_honours_the_selected_profile(monkeypatch):
    """面板发出的 `model_profile_id` 必须真的决定用哪个模型（端到端）。

    这一条是「研判对话页的模型选择」这个功能的地基：前端加了选择器、后端若不认，
    用户会以为自己换了模型而其实没换——比没有选择器更糟。
    """
    from fastapi.testclient import TestClient

    from api.database import get_db_ctx
    from api.main import app
    from api.services import model_profile_service

    user_id, jwt = _user_and_jwt()
    with get_db_ctx() as db:
        model_profile_service.create_model_profile(
            db, user_id=user_id, name="HTTP 选中的档", llm_provider="openai",
            backend_url="https://http-selected.example/v1",
            deep_think_llm="http-selected-model", api_key="sk-http-selected",
            is_default=True,
        )
        picked = model_profile_service.create_model_profile(
            db, user_id=user_id, name="第二个档", llm_provider="openai",
            deep_think_llm="second-model", api_key="sk-second",
        )

    calls = _capture_client(monkeypatch)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/v1/chat",
        json={
            "messages": [{"role": "user", "content": "600519.SH 是不是在洗盘"}],
            "symbol": "600519.SH",
            "model_profile_id": str(picked["id"]),
        },
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert resp.status_code == 200, resp.text[:300]
    assert calls, "应当构造 LLM 客户端"
    assert calls[0]["model"] == "second-model", (
        f"请求里指定的档案没生效，实际用了 {calls[0]['model']!r}——"
        "那样用户会以为换了模型而其实没换"
    )
    assert calls[0].get("api_key") == "sk-second"
