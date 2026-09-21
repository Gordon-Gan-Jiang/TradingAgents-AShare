"""LLM 配置链路测试：模型管理页（model_profiles）key 合并 + 缺 key 的可读指引。"""

from __future__ import annotations

import asyncio
import os
import tempfile
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("TA_APP_SECRET_KEY", "mainline-test-secret-key-123456")

from api.database import Base, ModelProfileDB  # noqa: E402
from api.main import _build_mainline_runtime_config  # noqa: E402
from api.services import auth_service, mainline_service  # noqa: E402


def _mk_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def test_mainline_runtime_config_merges_model_profile_key():
    db = _mk_db()
    # 用户只有一个模型管理配置（含真实 key），设置页无 key
    db.add(
        ModelProfileDB(
            id="prof-1",
            user_id="u1",
            name="deepseek-chat",
            llm_provider="openai",
            backend_url="https://api.deepseek.com/v1",
            deep_think_llm="deepseek-chat",
            quick_think_llm="deepseek-v4-flash",
            api_key_encrypted=auth_service.encrypt_secret("sk-test-abcdef"),
            is_active=True,
            is_default=True,
        )
    )
    db.commit()

    fake_base = {
        "llm_provider": "openai",
        "backend_url": "https://api.deepseek.com/v1",
        "deep_think_llm": "",
        "quick_think_llm": "",
        "api_key": None,  # 设置页无 key
    }
    with patch("api.main._build_runtime_config", return_value=dict(fake_base)):
        cfg = _build_mainline_runtime_config(db, "u1")
    assert cfg is not None
    assert cfg["api_key"] == "sk-test-abcdef"   # 模型管理页 key 被合并
    assert cfg["deep_think_llm"] == "deepseek-chat"
    assert cfg["quick_think_llm"] == "deepseek-v4-flash"


def test_mainline_runtime_config_without_any_key_returns_no_key():
    db = _mk_db()  # 无 model_profile
    fake_base = {"llm_provider": "openai", "api_key": None, "deep_think_llm": "x", "quick_think_llm": "y"}
    with patch("api.main._build_runtime_config", return_value=dict(fake_base)):
        cfg = _build_mainline_runtime_config(db, "u1")
    assert cfg is not None
    assert not cfg.get("api_key")


def test_run_mainline_job_missing_key_raises_readable_error():
    db = _mk_db()
    mainline_service.create_run(
        db, user_id="u1", run_id="j1", trade_date="2026-08-31", perspective="short", job_id="j1"
    )
    events: list[tuple[str, str, dict]] = []
    job_updates: dict[str, dict] = {}

    def _set_job(job_id, **kw):
        job_updates.setdefault(job_id, {}).update(kw)

    def _emit(job_id, event, data):
        events.append((job_id, event, data))

    class _FakeCtx:
        def __init__(self, session):
            self._s = session

        def __enter__(self):
            return self._s

        def __exit__(self, *a):
            return False

    cfg = {"llm_provider": "openai", "api_key": "", "deep_think_llm": "x", "quick_think_llm": "y"}
    with patch("api.services.mainline_service.get_db_ctx", side_effect=lambda: _FakeCtx(db)):
        with pytest.raises(ValueError) as exc_info:
            asyncio.run(
                mainline_service.run_mainline_job(
                    "j1", "u1", "2026-08-31", "short",
                    set_job=_set_job, emit_event=_emit, config=cfg,
                )
            )
    assert "未配置 LLM API Key" in str(exc_info.value)
    assert "设置" in str(exc_info.value)
    # job 被正确标记 failed 且发出失败事件
    assert job_updates["j1"]["status"] == "failed"
    assert any("job.failed" == e for _, e, _ in events)


def test_default_config_empty_env_falls_back(monkeypatch):
    """空字符串环境变量应回退默认值（修复系统默认模型报错的核心）。"""
    from tradingagents import default_config as dc

    monkeypatch.setenv("TA_LLM_DEEP", "")
    monkeypatch.setenv("TA_LLM_QUICK", "")
    monkeypatch.setenv("TA_LLM_PROVIDER", "")
    monkeypatch.setenv("TA_BASE_URL", "")
    # 重新导入会缓存模块？default_config 是模块级常量——直接测 _env helper
    assert dc._env("TA_LLM_DEEP", "gpt-4o") == "gpt-4o"
    assert dc._env("TA_LLM_QUICK", "gpt-4o-mini") == "gpt-4o-mini"
    assert dc._env("TA_LLM_PROVIDER", "openai") == "openai"
    assert dc._env("TA_BASE_URL", "https://api.openai.com/v1") == "https://api.openai.com/v1"
    assert dc._env("TA_API_KEY", "") == ""
    # 有值时保留
    monkeypatch.setenv("TA_LLM_DEEP", "deepseek-chat")
    assert dc._env("TA_LLM_DEEP", "gpt-4o") == "deepseek-chat"


def test_validate_llm_config_readable_error():
    from tradingagents.graph.mainline_graph import validate_llm_config

    # 缺 key + 缺模型 → 可读错误（含配置指引）
    with pytest.raises(ValueError) as exc:
        validate_llm_config({"llm_provider": "openai", "api_key": "", "deep_think_llm": "", "quick_think_llm": ""})
    msg = str(exc.value)
    assert "API Key" in msg and "深度模型" in msg
    assert ".env" in msg and "设置" in msg

    # 有 key 有模型 → 不抛
    validate_llm_config({"api_key": "sk-x", "deep_think_llm": "m", "quick_think_llm": "m"})

    # 只缺模型
    with pytest.raises(ValueError) as exc2:
        validate_llm_config({"api_key": "sk-x", "deep_think_llm": "m", "quick_think_llm": ""})
    assert "快速模型" in str(exc2.value)
