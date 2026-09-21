"""方案A：默认模型自动落到数据库中的「系统默认模型」（model_profiles 中 is_default=True）。

覆盖：
1. _resolve_effective_model_profile_id 的解析规则（显式 > 默认配置 > None）
2. 未显式选择模型配置时，dry-run 分析会自动使用默认 profile 的模型配置
3. 无默认 profile 时保持原有兜底（env/用户设置页）
"""
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from api.database import get_db_ctx

# ---------------------------------------------------------------------------
# Helpers (mirror test_api_smoke)
# ---------------------------------------------------------------------------

def _get_client() -> TestClient:
    from api.main import app
    return TestClient(app, raise_server_exceptions=False)


def _auth_unique(client: TestClient) -> str:
    from api.database import UserDB, init_db
    from api.services import auth_service

    init_db()
    email = auth_service.normalize_email(f"default-model-{uuid4().hex[:8]}@test.com")
    now = datetime.now(timezone.utc)
    with get_db_ctx() as db:
        user = auth_service.get_user_by_email(db, email)
        if not user:
            user = UserDB(
                id=str(uuid4()),
                email=email,
                is_active=True,
                created_at=now,
                updated_at=now,
                last_login_at=now,
            )
            db.add(user)
            db.commit()
            db.refresh(user)
    return auth_service.create_access_token(user)


# ---------------------------------------------------------------------------
# Unit tests: _resolve_effective_model_profile_id
# ---------------------------------------------------------------------------

class TestResolveEffectiveModelProfileId:
    def test_explicit_profile_id_wins(self):
        from api.main import _resolve_effective_model_profile_id
        from api.services import model_profile_service

        user_id = str(uuid4())
        with get_db_ctx() as db:
            default = model_profile_service.create_model_profile(
                db, user_id=user_id, name="Default", llm_provider="openai", is_default=True,
            )
            explicit = model_profile_service.create_model_profile(
                db, user_id=user_id, name="Explicit", llm_provider="openai", is_default=False,
            )
            resolved = _resolve_effective_model_profile_id(
                db, user_id=user_id, model_profile_id=explicit["id"],
            )
            assert resolved == explicit["id"]
            assert resolved != default["id"]

    def test_no_user_returns_none(self):
        from api.main import _resolve_effective_model_profile_id
        with get_db_ctx() as db:
            assert _resolve_effective_model_profile_id(db, user_id=None, model_profile_id=None) is None

    def test_no_profiles_returns_none(self):
        from api.main import _resolve_effective_model_profile_id
        user_id = str(uuid4())
        with get_db_ctx() as db:
            assert _resolve_effective_model_profile_id(db, user_id=user_id, model_profile_id=None) is None

    def test_profiles_without_default_returns_none(self):
        """方案A 语义：只有被标记为默认的配置才算「系统默认模型」，避免误用未设默认的配置。"""
        from api.main import _resolve_effective_model_profile_id
        from api.services import model_profile_service

        user_id = str(uuid4())
        with get_db_ctx() as db:
            model_profile_service.create_model_profile(
                db, user_id=user_id, name="No-Default", llm_provider="openai", is_default=False,
            )
            assert _resolve_effective_model_profile_id(db, user_id=user_id, model_profile_id=None) is None

    def test_default_profile_returns_its_id(self):
        from api.main import _resolve_effective_model_profile_id
        from api.services import model_profile_service

        user_id = str(uuid4())
        with get_db_ctx() as db:
            default = model_profile_service.create_model_profile(
                db, user_id=user_id, name="Default", llm_provider="openai", is_default=True,
            )
            model_profile_service.create_model_profile(
                db, user_id=user_id, name="Other", llm_provider="openai", is_default=False,
            )
            resolved = _resolve_effective_model_profile_id(db, user_id=user_id, model_profile_id=None)
            assert resolved == default["id"]


# ---------------------------------------------------------------------------
# API-level tests: /v1/analyze dry_run with no model_profile_id
# ---------------------------------------------------------------------------

class TestAnalyzeUsesSystemDefaultModel:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.client = _get_client()
        self.token = _auth_unique(self.client)
        self.headers = {"Authorization": f"Bearer {self.token}"}
        self.user_id = self.client.get("/v1/auth/me", headers=self.headers).json()["id"]

    def _analyze_dry_run(self, **extra):
        body = {"symbol": "600519.SH", "selected_analysts": [], "dry_run": True, **extra}
        resp = self.client.post("/v1/analyze", headers=self.headers, json=body)
        assert resp.status_code == 200
        job_id = resp.json()["job_id"]
        result_resp = self.client.get(f"/v1/jobs/{job_id}/result", headers=self.headers)
        assert result_resp.status_code == 200
        return job_id, (result_resp.json().get("result") or {})

    def _report_row(self, job_id: str):
        reports_resp = self.client.get("/v1/reports?skip=0&limit=50", headers=self.headers)
        assert reports_resp.status_code == 200
        reports = reports_resp.json().get("reports") or []
        row = next((item for item in reports if item.get("id") == job_id), None)
        assert row is not None
        return row

    def test_dry_run_without_profile_uses_default_profile(self):
        from api.services import model_profile_service

        # 建两个 profile：一个默认，一个非默认
        with get_db_ctx() as db:
            default_prof = model_profile_service.create_model_profile(
                db,
                user_id=self.user_id,
                name=f"Default-Model-{uuid4().hex[:6]}",
                llm_provider="openai",
                backend_url="https://api.deepseek.com/v1",
                quick_think_llm="deepseek-v4-flash",
                deep_think_llm="deepseek-v4-pro",
                is_default=True,
                is_active=True,
            )
            other_prof = model_profile_service.create_model_profile(
                db,
                user_id=self.user_id,
                name=f"Other-Model-{uuid4().hex[:6]}",
                llm_provider="openai",
                quick_think_llm="gpt-other-quick",
                deep_think_llm="gpt-other-deep",
                is_default=False,
                is_active=True,
            )

        # 不传 model_profile_id → 应自动落到默认 profile
        job_id, payload = self._analyze_dry_run()
        assert payload.get("model_profile_id") == default_prof["id"]
        assert payload.get("model_profile_id") != other_prof["id"]

        # 报告里记录默认 profile 及其模型配置
        row = self._report_row(job_id)
        assert row.get("model_profile_id") == default_prof["id"]
        assert row.get("model_profile_name") == default_prof["name"]
        assert row.get("quick_think_llm") == "deepseek-v4-flash"
        assert row.get("deep_think_llm") == "deepseek-v4-pro"

    def test_dry_run_without_any_default_profile_keeps_fallback(self):
        from api.services import model_profile_service

        # 只有非默认 profile → 方案A 不启用，仍走 env/设置页兜底
        with get_db_ctx() as db:
            model_profile_service.create_model_profile(
                db,
                user_id=self.user_id,
                name=f"Only-Profile-{uuid4().hex[:6]}",
                llm_provider="openai",
                quick_think_llm="gpt-fallback-quick",
                deep_think_llm="gpt-fallback-deep",
                is_default=False,
                is_active=True,
            )

        job_id, payload = self._analyze_dry_run()
        # 未启用默认 profile → 不绑定任何 profile
        assert payload.get("model_profile_id") is None

        # 报告模型名来自 env/设置页兜底（不应是那个非默认 profile 的模型名）
        row = self._report_row(job_id)
        assert row.get("model_profile_id") is None
        assert row.get("quick_think_llm") != "gpt-fallback-quick"
        assert row.get("deep_think_llm") != "gpt-fallback-deep"

    def test_explicit_profile_still_wins_over_default(self):
        from api.services import model_profile_service

        with get_db_ctx() as db:
            default_prof = model_profile_service.create_model_profile(
                db,
                user_id=self.user_id,
                name=f"Default-Model-{uuid4().hex[:6]}",
                llm_provider="openai",
                backend_url="https://api.deepseek.com/v1",
                quick_think_llm="deepseek-v4-flash",
                deep_think_llm="deepseek-v4-pro",
                is_default=True,
                is_active=True,
            )
            explicit_prof = model_profile_service.create_model_profile(
                db,
                user_id=self.user_id,
                name=f"Explicit-Model-{uuid4().hex[:6]}",
                llm_provider="openai",
                quick_think_llm="gpt-explicit-quick",
                deep_think_llm="gpt-explicit-deep",
                is_default=False,
                is_active=True,
            )

        job_id, payload = self._analyze_dry_run(model_profile_id=explicit_prof["id"])
        assert payload.get("model_profile_id") == explicit_prof["id"]
        assert payload.get("model_profile_id") != default_prof["id"]

        row = self._report_row(job_id)
        assert row.get("model_profile_id") == explicit_prof["id"]
        assert row.get("quick_think_llm") == "gpt-explicit-quick"
        assert row.get("deep_think_llm") == "gpt-explicit-deep"
