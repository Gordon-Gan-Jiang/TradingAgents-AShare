"""API smoke tests using FastAPI TestClient (no external server needed).

Covers:
1. AnalyzeRequest schema — query field exists, symbol optional
2. /v1/analyze dry_run — legacy single-horizon path works
3. /v1/analyze with query field — schema accepts it, dry_run still short-circuits
4. /v1/chat/completions — unrecognizable stock returns 400
5. /v1/chat/completions — valid stock dry_run completes job
6. /v1/jobs/{id}/result — completed job returns result
"""
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from api.database import ImportedPortfolioPositionDB, get_db_ctx


# ---------------------------------------------------------------------------
# Schema-only test (no server needed)
# ---------------------------------------------------------------------------

class TestAnalyzeRequestSchema:
    def test_query_field_exists_and_optional(self):
        from api.main import AnalyzeRequest
        # query defaults to None
        req = AnalyzeRequest(symbol="600519.SH")
        assert req.query is None

    def test_query_field_accepts_string(self):
        from api.main import AnalyzeRequest
        req = AnalyzeRequest(symbol="600519.SH", query="分析贵州茅台短线机会")
        assert req.query == "分析贵州茅台短线机会"

    def test_symbol_is_optional(self):
        from api.main import AnalyzeRequest
        # should not raise
        req = AnalyzeRequest()
        assert req.symbol == ""

    def test_dry_run_defaults_false(self):
        from api.main import AnalyzeRequest
        req = AnalyzeRequest(symbol="600519.SH")
        assert req.dry_run is False

    def test_model_profile_id_optional(self):
        from api.main import AnalyzeRequest
        req = AnalyzeRequest(symbol="600519.SH")
        assert req.model_profile_id is None


class TestStreamChunkNormalization:
    def test_coerces_string_debate_state_to_empty_dict(self):
        from api.main import _normalize_stream_chunk

        chunk = {
            "investment_debate_state": "unexpected-string",
            "risk_debate_state": {"judge_decision": "ok"},
            "market_report": "done",
        }
        normalized = _normalize_stream_chunk(chunk, job_id="job-x", horizon="short")

        assert normalized["investment_debate_state"] == {}
        assert normalized["risk_debate_state"] == {"judge_decision": "ok"}
        assert normalized["market_report"] == "done"

    def test_handles_non_dict_chunk(self):
        from api.main import _normalize_stream_chunk

        normalized = _normalize_stream_chunk("bad-chunk", job_id="job-y", horizon="short")
        assert normalized == {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_client():
    """Create a TestClient for the FastAPI app."""
    from api.main import app
    return TestClient(app, raise_server_exceptions=False)


def _auth(client: TestClient) -> str:
    """Register a test user and return a valid JWT token."""
    r = client.post("/v1/auth/request-code", json={"email": "apitest@test.com"})
    code = r.json()["dev_code"]
    r2 = client.post("/v1/auth/verify-code", json={"email": "apitest@test.com", "code": code})
    return r2.json()["access_token"]


def _auth_unique(client: TestClient) -> str:
    from api.database import UserDB, get_db_ctx, init_db
    from api.services import auth_service

    init_db()
    email = auth_service.normalize_email(f"apitest-{uuid4().hex[:8]}@test.com")
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


def _wait_job(client: TestClient, token: str, job_id: str, timeout: float = 5.0) -> dict:
    """Poll until job is no longer running, return result dict."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/v1/jobs/{job_id}", headers={"Authorization": f"Bearer {token}"})
        status = r.json().get("status")
        if status in ("completed", "failed"):
            break
        time.sleep(0.2)
    r2 = client.get(f"/v1/jobs/{job_id}/result", headers={"Authorization": f"Bearer {token}"})
    return r2.json()


# ---------------------------------------------------------------------------
# API integration tests
# ---------------------------------------------------------------------------

class TestAnalyzeEndpoint:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.client = _get_client()
        self.token = _auth(self.client)
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def test_dry_run_completes(self):
        """Legacy path: symbol + dry_run → completed immediately."""
        r = self.client.post("/v1/analyze", headers=self.headers, json={
            "symbol": "600519.SH",
            "trade_date": "2024-01-15",
            "dry_run": True,
        })
        assert r.status_code == 200
        job_id = r.json()["job_id"]
        result = _wait_job(self.client, self.token, job_id)
        assert result["status"] == "completed"
        assert result["decision"] == "DRY_RUN"
        assert result["result"]["symbol"] == "600519.SH"

    def test_query_field_accepted_with_dry_run(self):
        """query field is accepted by schema; dry_run still short-circuits before LLM."""
        r = self.client.post("/v1/analyze", headers=self.headers, json={
            "symbol": "600519.SH",
            "trade_date": "2024-01-15",
            "query": "分析贵州茅台短线机会，关注量价关系",
            "dry_run": True,
        })
        assert r.status_code == 200
        job_id = r.json()["job_id"]
        result = _wait_job(self.client, self.token, job_id)
        assert result["status"] == "completed"
        assert result["decision"] == "DRY_RUN"

    def test_missing_symbol_accepted_by_schema(self):
        """symbol is optional in schema; job is created (may fail later without LLM, but 200 on submit)."""
        r = self.client.post("/v1/analyze", headers=self.headers, json={
            "trade_date": "2024-01-15",
            "dry_run": True,
        })
        assert r.status_code == 200
        assert "job_id" in r.json()

    def test_requires_auth(self):
        """Unauthenticated request returns 401/403."""
        r = self.client.post("/v1/analyze", json={
            "symbol": "600519.SH", "dry_run": True,
        })
        assert r.status_code in (401, 403)

    def test_selected_analysts_field(self):
        """selected_analysts are echoed back in dry_run result."""
        r = self.client.post("/v1/analyze", headers=self.headers, json={
            "symbol": "600519.SH",
            "selected_analysts": ["market", "news"],
            "dry_run": True,
        })
        job_id = r.json()["job_id"]
        result = _wait_job(self.client, self.token, job_id)
        assert result["result"]["selected_analysts"] == ["market", "news"]

    def test_dry_run_ignores_imported_position_context_for_manual_analysis(self):
        """深度分析保持客观：/v1/analyze 不再自动合并导入持仓。"""
        current_user = self.client.get("/v1/auth/me", headers=self.headers).json()
        now = datetime.now(timezone.utc)

        with get_db_ctx() as db:
            # (user_id, source, symbol) 上有唯一约束，先清理以保证测试可重复运行。
            db.query(ImportedPortfolioPositionDB).filter(
                ImportedPortfolioPositionDB.user_id == current_user["id"],
                ImportedPortfolioPositionDB.source == "manual",
                ImportedPortfolioPositionDB.symbol == "600519.SH",
            ).delete(synchronize_session=False)
            db.add(
                ImportedPortfolioPositionDB(
                    id=uuid4().hex,
                    user_id=current_user["id"],
                    source="manual",
                    symbol="600519.SH",
                    security_name="贵州茅台",
                    current_position=300.0,
                    average_cost=1680.5,
                    market_value=504150.0,
                    current_position_pct=42.5,
                    trade_points_json=[],
                    trade_points_count=0,
                    last_imported_at=now,
                )
            )
            db.commit()

        r = self.client.post("/v1/analyze", headers=self.headers, json={
            "symbol": "600519.SH",
            "trade_date": "2024-01-15",
            "dry_run": True,
        })
        assert r.status_code == 200
        job_id = r.json()["job_id"]
        result = _wait_job(self.client, self.token, job_id)

        user_context = result["result"]["user_context"]
        # 即使库里存在导入持仓，也不得自动带入：让分析保持客观。
        assert user_context.get("current_position") is None
        assert user_context.get("average_cost") is None
        assert user_context.get("current_position_pct") is None
        assert "持仓导入" not in (user_context.get("user_notes") or "")


class TestChatCompletionsEndpoint:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.client = _get_client()
        self.token = _auth(self.client)
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def test_unrecognizable_stock_returns_error(self):
        """Non-stock text returns 400 with Chinese error message."""
        # Mock the LLM used for stock extraction to return no stock
        with patch("api.main._ai_extract_symbol_and_date", return_value=(None, None, ["short"], [], [], {})):
            r = self.client.post("/v1/chat/completions", headers=self.headers, json={
                "messages": [{"role": "user", "content": "今天天气真好"}],
                "stream": False,
                "dry_run": True,
            })
        assert r.status_code == 400

    def test_valid_stock_dry_run_creates_job(self):
        """Valid stock message with dry_run creates and completes a job."""
        with patch("api.main._ai_extract_symbol_and_date", return_value=("600519.SH", "2024-01-15", ["short"], [], [], {})):
            r = self.client.post("/v1/chat/completions", headers=self.headers, json={
                "messages": [{"role": "user", "content": "分析600519短线机会"}],
                "stream": False,
                "dry_run": True,
            })
        assert r.status_code == 200
        body = r.json()
        # Non-stream returns OpenAI-compatible format with job_id embedded in content
        assert "choices" in body
        content = body["choices"][0]["message"]["content"]
        # Extract job_id from content (format: "已启动分析任务：<job_id>")
        job_id = body["id"].replace("chatcmpl-", "")
        result = _wait_job(self.client, self.token, job_id)
        assert result["status"] == "completed"
        assert result["decision"] == "DRY_RUN"

    def test_fallback_symbol_parse_when_llm_extract_fails(self):
        """If LLM extraction fails, code in raw text should still be parsed."""

        class _BrokenLLM:
            def invoke(self, _prompt):
                raise RuntimeError("upstream llm failed")

        class _BrokenClient:
            def get_llm(self):
                return _BrokenLLM()

        with patch("tradingagents.llm_clients.factory.create_llm_client", return_value=_BrokenClient()):
            r = self.client.post("/v1/chat/completions", headers=self.headers, json={
                "messages": [{"role": "user", "content": "请分析 600519 的短线机会"}],
                "stream": False,
                "dry_run": True,
            })
        assert r.status_code == 200
        body = r.json()
        job_id = body["id"].replace("chatcmpl-", "")
        result = _wait_job(self.client, self.token, job_id)
        assert result["status"] == "completed"
        assert result["result"]["symbol"] == "600519.SH"

    def test_requires_auth(self):
        r = self.client.post("/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "分析600519"}],
            "stream": False,
        })
        assert r.status_code in (401, 403)


class TestOpenAPISchema:
    def test_analyze_request_has_query_field(self):
        client = _get_client()
        r = client.get("/openapi.json")
        assert r.status_code == 200
        schema = r.json()["components"]["schemas"]["AnalyzeRequest"]
        assert "query" in schema["properties"]

    def test_analyze_request_symbol_not_required(self):
        client = _get_client()
        r = client.get("/openapi.json")
        schema = r.json()["components"]["schemas"]["AnalyzeRequest"]
        assert "symbol" not in schema.get("required", [])

    def test_healthz(self):
        client = _get_client()
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


class TestRuntimeConfigWarmup:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.client = _get_client()
        self.token = _auth(self.client)
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def test_model_change_schedules_warmup(self):
        model_name = f"gpt-test-quick-{uuid4().hex[:8]}"
        with patch("api.main._probe_runtime_config", return_value={"status": "ok", "model": model_name}) as probe, \
             patch("api.main._run_config_warmup") as warmup:
            r = self.client.patch("/v1/config", headers=self.headers, json={
                "quick_think_llm": model_name,
            })
        assert r.status_code == 200
        body = r.json()
        assert body["warmup"]["status"] == "scheduled"
        assert body["warmup"]["triggered"] is True
        assert model_name in body["warmup"]["models"]
        warmup.assert_called_once()

    def test_non_model_change_skips_warmup(self):
        with patch("api.main._run_config_warmup") as warmup:
            r = self.client.patch("/v1/config", headers=self.headers, json={
                "max_debate_rounds": 3,
            })
        assert r.status_code == 200
        body = r.json()
        assert body["warmup"]["status"] == "skipped"
        assert body["warmup"]["triggered"] is False
        warmup.assert_not_called()

    def test_api_key_is_probed_before_save(self):
        with patch("api.main._probe_runtime_config", return_value={"status": "ok", "model": "moonshot-v1-8k"}) as probe, \
             patch("api.main._run_config_warmup") as warmup:
            r = self.client.patch("/v1/config", headers=self.headers, json={
                "llm_provider": "openai",
                "backend_url": "https://api.moonshot.cn/v1",
                "quick_think_llm": "moonshot-v1-8k",
                "api_key": "sk-test-valid",
            })
        assert r.status_code == 200
        probe.assert_called_once()
        warmup.assert_called_once()

    def test_invalid_api_key_is_rejected_before_save(self):
        with patch("api.main._probe_runtime_config", side_effect=HTTPException(status_code=400, detail="模型 Key 验证失败")) as probe, \
             patch("api.main._run_config_warmup") as warmup:
            r = self.client.patch("/v1/config", headers=self.headers, json={
                "llm_provider": "openai",
                "backend_url": "https://api.moonshot.cn/v1",
                "quick_think_llm": "moonshot-v1-8k",
                "api_key": "sk-test-invalid",
            })
        assert r.status_code == 400
        assert "模型 Key 验证失败" in r.json()["detail"]
        probe.assert_called_once()
        warmup.assert_not_called()

    def test_force_warmup_schedules_even_without_model_change(self):
        with patch("api.main._run_config_warmup") as warmup:
            r = self.client.patch("/v1/config", headers=self.headers, json={
                "max_debate_rounds": 3,
                "force_warmup": True,
            })
        assert r.status_code == 200
        body = r.json()
        assert body["warmup"]["status"] == "scheduled"
        assert body["warmup"]["triggered"] is True
        warmup.assert_called_once()

    def test_manual_warmup_returns_model_reply(self):
        with patch("api.main._invoke_runtime_warmup", return_value=[{
            "model": "gpt-test-quick",
            "targets": ["常规模型"],
            "content": "你好，我已准备就绪。",
            "error": None,
        }]) as invoke:
            r = self.client.post("/v1/config/warmup", headers=self.headers, json={
                "quick_think_llm": "gpt-test-quick",
                "prompt": "你好",
            })

        assert r.status_code == 200
        body = r.json()
        assert body["prompt"] == "你好"
        assert body["results"][0]["content"] == "你好，我已准备就绪。"
        invoke.assert_called_once()
        assert invoke.call_args.args[0]["quick_think_llm"] == "gpt-test-quick"
        assert invoke.call_args.args[1] == "你好"

    def test_manual_warmup_surfaces_upstream_error(self):
        with patch(
            "api.main._invoke_runtime_warmup",
            side_effect=HTTPException(status_code=400, detail="模型 warmup 失败：upstream timeout"),
        ):
            r = self.client.post("/v1/config/warmup", headers=self.headers, json={
                "quick_think_llm": "gpt-test-quick",
                "prompt": "你好",
            })

        assert r.status_code == 400
        assert "模型 warmup 失败" in r.json()["detail"]

    def test_runtime_config_sanitizes_deepseek_incompatible_model(self):
        save_resp = self.client.patch("/v1/config", headers=self.headers, json={
            "llm_provider": "openai",
            "backend_url": "https://api.deepseek.com/v1",
            "quick_think_llm": "deepseek-v4-flash",
            "deep_think_llm": "deepseek-v4-pro",
            "warmup": False,
        })
        assert save_resp.status_code == 200

        with patch("api.main._invoke_runtime_warmup", return_value=[{
            "model": "deepseek-v4-flash",
            "targets": ["常规模型"],
            "content": "ok",
            "error": None,
        }]) as invoke:
            r = self.client.post("/v1/config/warmup", headers=self.headers, json={
                "quick_think_llm": "doubao-1-5-lite-32k-250115",
                "deep_think_llm": "deepseek-v4-pro",
                "prompt": "你好",
            })
        assert r.status_code == 200
        runtime_cfg = invoke.call_args.args[0]
        assert runtime_cfg["backend_url"] == "https://api.deepseek.com/v1"
        assert runtime_cfg["quick_think_llm"] == "deepseek-v4-flash"
        assert runtime_cfg["deep_think_llm"] == "deepseek-v4-pro"

    def test_model_profile_strategy_does_not_use_system_model_runtime_keys(self):
        from api.main import _build_runtime_config, _merge_model_profile_overrides
        from api.database import UserDB
        from api.services import auth_service, model_profile_service

        user_id = str(uuid4())
        now = datetime.now(timezone.utc)
        with get_db_ctx() as db:
            user = UserDB(
                id=user_id,
                email=auth_service.normalize_email(f"model-infer-{uuid4().hex[:8]}@test.com"),
                is_active=True,
                created_at=now,
                updated_at=now,
                last_login_at=now,
            )
            db.add(user)
            db.commit()

            # Simulate system runtime config using Ark endpoint.
            auth_service.upsert_user_llm_config(
                db,
                user_id,
                llm_provider="openai",
                backend_url="https://ark.cn-beijing.volces.com/api/v3",
                quick_think_llm="doubao-1-5-lite-32k-250115",
                deep_think_llm="doubao-1-5-lite-32k-250115",
            )

            # Selected profile provides explicit model runtime keys.
            profile = model_profile_service.create_model_profile(
                db,
                user_id=user_id,
                name="Profile-Selected-Strategy",
                llm_provider="openai",
                backend_url="https://api.deepseek.com/v1",
                quick_think_llm="deepseek-v4-flash",
                deep_think_llm="deepseek-v4-pro",
                is_active=True,
            )

            merged = _merge_model_profile_overrides(
                db,
                user_id=user_id,
                model_profile_id=profile["id"],
                request_overrides={},
            )
            runtime_cfg = _build_runtime_config(
                merged,
                user_id=user_id,
                db=db,
                trusted_overrides=True,
                strategy="profile_selected",
            )

        # System runtime model settings must not override selected profile model runtime keys.
        assert runtime_cfg["backend_url"] == "https://api.deepseek.com/v1"
        assert runtime_cfg["quick_think_llm"] == "deepseek-v4-flash"
        assert runtime_cfg["deep_think_llm"] == "deepseek-v4-pro"


class TestModelProfileApi:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.client = _get_client()
        self.token = _auth_unique(self.client)
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def test_model_profile_crud_and_analyze_dry_run(self):
        create_resp = self.client.post(
            "/v1/model-profiles",
            headers=self.headers,
            json={
                "name": "OpenAI-Test",
                "llm_provider": "openai",
                "quick_think_llm": "gpt-test-quick",
                "deep_think_llm": "gpt-test-deep",
                "is_default": True,
                "tags": ["ab", "stock"],
            },
        )
        assert create_resp.status_code == 200
        created = create_resp.json()
        assert created["name"] == "OpenAI-Test"
        assert created["is_default"] is True
        profile_id = created["id"]

        list_resp = self.client.get("/v1/model-profiles", headers=self.headers)
        assert list_resp.status_code == 200
        profiles = list_resp.json()["profiles"]
        assert any(item["id"] == profile_id for item in profiles)

        patch_resp = self.client.patch(
            f"/v1/model-profiles/{profile_id}",
            headers=self.headers,
            json={"name": "OpenAI-Test-V2", "is_active": False},
        )
        assert patch_resp.status_code == 200
        assert patch_resp.json()["name"] == "OpenAI-Test-V2"
        assert patch_resp.json()["is_active"] is False

        activate_resp = self.client.patch(
            f"/v1/model-profiles/{profile_id}",
            headers=self.headers,
            json={"is_active": True},
        )
        assert activate_resp.status_code == 200
        assert activate_resp.json()["is_active"] is True

        analyze_resp = self.client.post(
            "/v1/analyze",
            headers=self.headers,
            json={
                "symbol": "600519.SH",
                "selected_analysts": [],
                "dry_run": True,
                "model_profile_id": profile_id,
            },
        )
        assert analyze_resp.status_code == 200
        job_id = analyze_resp.json()["job_id"]
        result_resp = self.client.get(f"/v1/jobs/{job_id}/result", headers=self.headers)
        assert result_resp.status_code == 200
        payload = result_resp.json().get("result") or {}
        assert payload.get("llm_provider") == "openai"
        assert payload.get("model_profile_id") == profile_id
        assert payload.get("quick_think_llm") == "gpt-test-quick"
        assert payload.get("deep_think_llm") == "gpt-test-deep"

        reports_resp = self.client.get("/v1/reports?skip=0&limit=50", headers=self.headers)
        assert reports_resp.status_code == 200
        reports = reports_resp.json().get("reports") or []
        row = next((item for item in reports if item.get("id") == job_id), None)
        assert row is not None
        assert row.get("model_profile_id") == profile_id
        assert row.get("quick_think_llm") == "gpt-test-quick"
        assert row.get("deep_think_llm") == "gpt-test-deep"

        detail_resp = self.client.get(f"/v1/reports/{job_id}", headers=self.headers)
        assert detail_resp.status_code == 200
        detail = detail_resp.json()
        assert detail.get("model_profile_id") == profile_id
        assert detail.get("quick_think_llm") == "gpt-test-quick"
        assert detail.get("deep_think_llm") == "gpt-test-deep"

        delete_resp = self.client.delete(f"/v1/model-profiles/{profile_id}", headers=self.headers)
        assert delete_resp.status_code == 200
        assert delete_resp.json()["id"] == profile_id

    def test_model_profile_warmup_updates_probe_status(self):
        create_resp = self.client.post(
            "/v1/model-profiles",
            headers=self.headers,
            json={
                "name": "Warmup-Profile",
                "llm_provider": "openai",
                "quick_think_llm": "gpt-test-quick",
                "deep_think_llm": "gpt-test-deep",
            },
        )
        assert create_resp.status_code == 200
        profile_id = create_resp.json()["id"]

        with patch("api.main._invoke_runtime_warmup", return_value=[{
            "model": "gpt-test-quick",
            "targets": ["常规模型"],
            "content": "你好，我已准备就绪。",
            "error": None,
        }]):
            warmup_resp = self.client.post(
                f"/v1/model-profiles/{profile_id}/warmup",
                headers=self.headers,
                json={"prompt": "你好"},
            )

        assert warmup_resp.status_code == 200
        body = warmup_resp.json()
        assert body["profile_id"] == profile_id
        assert body["results"][0]["content"] == "你好，我已准备就绪。"
        assert body["profile"]["last_probe_status"] == "ok"
        assert body["profile"]["last_probe_at"] is not None


class TestModelArenaApi:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.client = _get_client()
        self.token = _auth_unique(self.client)
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def test_model_arena_leaderboard_compare_promote_and_rollback(self):
        from api.database import ReportDB, ReportT1OutcomeDB
        from api.services import model_profile_service

        me = self.client.get("/v1/auth/me", headers=self.headers)
        assert me.status_code == 200
        user_id = me.json()["id"]
        now = datetime.now(timezone.utc)
        signal_day = "2026-05-20"
        t1_day = "2026-05-21"

        with get_db_ctx() as db:
            base = model_profile_service.create_model_profile(
                db,
                user_id=user_id,
                name=f"Arena-Base-{uuid4().hex[:6]}",
                llm_provider="openai",
                quick_think_llm="doubao-1-5-lite-32k-250115",
                deep_think_llm="doubao-1-5-lite-32k-250115",
                backend_url="https://ark.cn-beijing.volces.com/api/v3",
                is_default=True,
                is_active=True,
            )
            target = model_profile_service.create_model_profile(
                db,
                user_id=user_id,
                name=f"Arena-Target-{uuid4().hex[:6]}",
                llm_provider="openai",
                quick_think_llm="deepseek-v4-flash",
                deep_think_llm="deepseek-v4-pro",
                backend_url="https://api.deepseek.com/v1",
                is_default=False,
                is_active=True,
            )

            r1_id = uuid4().hex
            r2_id = uuid4().hex
            db.add(
                ReportDB(
                    id=r1_id,
                    user_id=user_id,
                    symbol="600519.SH",
                    trade_date=signal_day,
                    status="completed",
                    decision="BUY",
                    direction="bullish",
                    result_data={
                        "model_info": {
                            "model_profile_id": base["id"],
                            "model_profile_name": base["name"],
                            "llm_provider": "openai",
                            "quick_think_llm": base["quick_think_llm"],
                            "deep_think_llm": base["deep_think_llm"],
                            "backend_url": base["backend_url"],
                        }
                    },
                    created_at=now,
                    updated_at=now,
                )
            )
            db.add(
                ReportDB(
                    id=r2_id,
                    user_id=user_id,
                    symbol="600519.SH",
                    trade_date=signal_day,
                    status="completed",
                    decision="BUY",
                    direction="bullish",
                    result_data={
                        "model_info": {
                            "model_profile_id": target["id"],
                            "model_profile_name": target["name"],
                            "llm_provider": "openai",
                            "quick_think_llm": target["quick_think_llm"],
                            "deep_think_llm": target["deep_think_llm"],
                            "backend_url": target["backend_url"],
                        }
                    },
                    created_at=now,
                    updated_at=now,
                )
            )
            db.add(
                ReportT1OutcomeDB(
                    id=uuid4().hex,
                    user_id=user_id,
                    report_id=r1_id,
                    symbol="600519.SH",
                    signal_trade_date=signal_day,
                    t1_trade_date=t1_day,
                    p0=1700.0,
                    p1=1690.0,
                    return_t1_pct=-0.5882,
                    direction_bucket="bullish",
                    label_correct=False,
                    status="evaluated",
                    evaluated_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
            db.add(
                ReportT1OutcomeDB(
                    id=uuid4().hex,
                    user_id=user_id,
                    report_id=r2_id,
                    symbol="600519.SH",
                    signal_trade_date=signal_day,
                    t1_trade_date=t1_day,
                    p0=1700.0,
                    p1=1725.0,
                    return_t1_pct=1.4706,
                    direction_bucket="bullish",
                    label_correct=True,
                    status="evaluated",
                    evaluated_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
            db.commit()

        board_resp = self.client.get(
            "/v1/model-arena/leaderboard?start_date=2026-05-01&end_date=2026-05-31&scope=all",
            headers=self.headers,
        )
        assert board_resp.status_code == 200
        board = board_resp.json()
        rows = board.get("leaderboard") or []
        assert len(rows) >= 2
        assert any(item.get("model_profile_id") == target["id"] for item in rows)
        assert any(
            (item.get("deep_think_llm") in ("deepseek-v4-pro", "deepseek-v4-flash"))
            or (item.get("model_profile_name") == target["name"])
            for item in rows
        )

        compare_resp = self.client.get(
            "/v1/model-arena/symbol-compare?symbol=600519.SH&start_date=2026-05-01&end_date=2026-05-31&scope=all",
            headers=self.headers,
        )
        assert compare_resp.status_code == 200
        compare = compare_resp.json()
        assert compare["symbol"] == "600519.SH"
        assert len(compare.get("rows") or []) >= 2
        assert any((item.get("model_profile_id") == target["id"]) for item in compare.get("rows") or [])

        detail_resp = self.client.get(
            f"/v1/model-arena/model-detail?model_profile_id={target['id']}&start_date=2026-05-01&end_date=2026-05-31&scope=all&limit=50",
            headers=self.headers,
        )
        assert detail_resp.status_code == 200
        detail = detail_resp.json()
        assert detail["model_profile_id"] == target["id"]
        assert detail["total_samples"] >= 1
        assert any(item.get("symbol") == "600519.SH" for item in detail.get("rows") or [])

        trend_resp = self.client.get(
            "/v1/model-arena/model-trend?start_date=2026-05-01&end_date=2026-05-31&scope=all&top_n=5&min_samples=1",
            headers=self.headers,
        )
        assert trend_resp.status_code == 200
        trend = trend_resp.json()
        assert "series" in trend
        assert any(item.get("model_profile_id") == target["id"] for item in trend.get("series") or [])

        promote_resp = self.client.post(
            "/v1/model-arena/promote-default",
            headers=self.headers,
            json={
                "profile_id": target["id"],
                "lookback_days": 90,
                "scope": "all",
                "min_samples": 1,
                "min_accuracy_improvement_pct": 0.0,
                "max_return_drop_pct": 5.0,
            },
        )
        assert promote_resp.status_code == 200
        assert promote_resp.json()["promoted"] is True
        assert promote_resp.json()["profile"]["id"] == target["id"]

        list_after_promote = self.client.get("/v1/model-profiles?include_inactive=true", headers=self.headers)
        assert list_after_promote.status_code == 200
        profiles = list_after_promote.json()["profiles"]
        promoted_default = next((p for p in profiles if p.get("is_default")), None)
        assert promoted_default is not None
        assert promoted_default["id"] == target["id"]

        rollback_resp = self.client.post(
            "/v1/model-arena/rollback-default",
            headers=self.headers,
            json={"profile_id": base["id"], "reason": "smoke_test_rollback"},
        )
        assert rollback_resp.status_code == 200
        assert rollback_resp.json()["rolled_back"] is True
        assert rollback_resp.json()["profile"]["id"] == base["id"]

        list_after_rollback = self.client.get("/v1/model-profiles?include_inactive=true", headers=self.headers)
        assert list_after_rollback.status_code == 200
        profiles2 = list_after_rollback.json()["profiles"]
        rollback_default = next((p for p in profiles2 if p.get("is_default")), None)
        assert rollback_default is not None
        assert rollback_default["id"] == base["id"]

    def test_model_arena_run_and_drift_alerts(self):
        from api.database import ReportDB, ReportT1OutcomeDB
        from api.services import model_profile_service

        me = self.client.get("/v1/auth/me", headers=self.headers)
        assert me.status_code == 200
        user_id = me.json()["id"]
        now = datetime.now(timezone.utc)

        with get_db_ctx() as db:
            p1 = model_profile_service.create_model_profile(
                db,
                user_id=user_id,
                name=f"Run-P1-{uuid4().hex[:6]}",
                llm_provider="openai",
                quick_think_llm="deepseek-v4-flash",
                deep_think_llm="deepseek-v4-pro",
                backend_url="https://api.deepseek.com/v1",
                is_default=True,
                is_active=True,
            )
            p2 = model_profile_service.create_model_profile(
                db,
                user_id=user_id,
                name=f"Run-P2-{uuid4().hex[:6]}",
                llm_provider="openai",
                quick_think_llm="doubao-1-5-lite-32k-250115",
                deep_think_llm="doubao-1-5-lite-32k-250115",
                backend_url="https://ark.cn-beijing.volces.com/api/v3",
                is_default=False,
                is_active=True,
            )

            # Baseline strong accuracy + recent degradation to trigger drift alert.
            for idx in range(12):
                signal_day = f"2026-04-{(idx % 20) + 1:02d}"
                rid = uuid4().hex
                db.add(
                    ReportDB(
                        id=rid,
                        user_id=user_id,
                        symbol="300750.SZ",
                        trade_date=signal_day,
                        status="completed",
                        decision="BUY",
                        direction="bullish",
                        result_data={
                            "model_info": {
                                "model_profile_id": p1["id"],
                                "model_profile_name": p1["name"],
                                "llm_provider": "openai",
                                "quick_think_llm": p1["quick_think_llm"],
                                "deep_think_llm": p1["deep_think_llm"],
                            }
                        },
                        created_at=now,
                        updated_at=now,
                    )
                )
                db.add(
                    ReportT1OutcomeDB(
                        id=uuid4().hex,
                        user_id=user_id,
                        report_id=rid,
                        symbol="300750.SZ",
                        signal_trade_date=signal_day,
                        t1_trade_date="2026-04-30",
                        p0=200.0,
                        p1=205.0,
                        return_t1_pct=2.5,
                        direction_bucket="bullish",
                        label_correct=True,
                        status="evaluated",
                        evaluated_at=now,
                        created_at=now,
                        updated_at=now,
                    )
                )
            for idx in range(8):
                signal_day = f"2026-05-{(idx % 20) + 1:02d}"
                rid = uuid4().hex
                db.add(
                    ReportDB(
                        id=rid,
                        user_id=user_id,
                        symbol="300750.SZ",
                        trade_date=signal_day,
                        status="completed",
                        decision="BUY",
                        direction="bullish",
                        result_data={
                            "model_info": {
                                "model_profile_id": p1["id"],
                                "model_profile_name": p1["name"],
                                "llm_provider": "openai",
                                "quick_think_llm": p1["quick_think_llm"],
                                "deep_think_llm": p1["deep_think_llm"],
                            }
                        },
                        created_at=now,
                        updated_at=now,
                    )
                )
                db.add(
                    ReportT1OutcomeDB(
                        id=uuid4().hex,
                        user_id=user_id,
                        report_id=rid,
                        symbol="300750.SZ",
                        signal_trade_date=signal_day,
                        t1_trade_date="2026-05-31",
                        p0=200.0,
                        p1=195.0,
                        return_t1_pct=-2.5,
                        direction_bucket="bullish",
                        label_correct=False,
                        status="evaluated",
                        evaluated_at=now,
                        created_at=now,
                        updated_at=now,
                    )
                )
            db.commit()

        run_resp = self.client.post(
            "/v1/model-arena/runs",
            headers=self.headers,
            json={
                "symbol": "600519.SH",
                "trade_date": "2026-05-22",
                "model_profile_ids": [p1["id"], p2["id"]],
                "dry_run": True,
                "query": "分析贵州茅台短线机会",
            },
        )
        assert run_resp.status_code == 200
        run_body = run_resp.json()
        assert run_body["experiment_id"]
        assert run_body["input_snapshot_hash"]
        assert len(run_body["jobs"]) == 2

        for job in run_body["jobs"]:
            r = self.client.get(f"/v1/jobs/{job['job_id']}/result", headers=self.headers)
            assert r.status_code == 200
            payload = r.json().get("result") or {}
            assert payload.get("experiment_id") == run_body["experiment_id"]
            assert payload.get("input_snapshot_hash") == run_body["input_snapshot_hash"]

        drift_resp = self.client.get(
            "/v1/model-arena/drift-alerts?scope=all&lookback_days=180&recent_days=7&baseline_days=30&min_recent_samples=5&alert_drop_pct=5",
            headers=self.headers,
        )
        assert drift_resp.status_code == 200
        drift = drift_resp.json()
        assert "alerts" in drift
        assert any(item.get("model_profile_id") == p1["id"] for item in drift.get("alerts") or [])


class TestWecomRuntimeConfig:
    WEBHOOK_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=e1d21302-1925-4247-ad5a-6bc023c7fd2a"

    @pytest.fixture(autouse=True)
    def setup(self):
        self.client = _get_client()
        self.token = _auth_unique(self.client)
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def test_config_returns_masked_webhook_and_toggle_state(self):
        r = self.client.patch("/v1/config", headers=self.headers, json={
            "wecom_webhook_url": self.WEBHOOK_URL,
            "wecom_report_enabled": False,
            "warmup": False,
        })

        assert r.status_code == 200
        body = r.json()
        current = body["current"]
        assert current["has_wecom_webhook"] is True
        assert current["wecom_report_enabled"] is False
        assert current["wecom_webhook_display"].startswith("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=")
        assert current["wecom_webhook_display"] != self.WEBHOOK_URL
        assert "fd2a" in current["wecom_webhook_display"]
        assert body["applied"]["wecom_report_enabled"] is False

        config_resp = self.client.get("/v1/config", headers=self.headers)
        assert config_resp.status_code == 200
        assert config_resp.json()["wecom_report_enabled"] is False

    def test_wecom_warmup_uses_stored_webhook_when_input_missing(self):
        save_resp = self.client.patch("/v1/config", headers=self.headers, json={
            "wecom_webhook_url": self.WEBHOOK_URL,
            "warmup": False,
        })
        assert save_resp.status_code == 200

        with patch("api.services.wecom_notification_service.send_message", return_value=True) as mock_send:
            r = self.client.post("/v1/config/wecom/warmup", headers=self.headers, json={})

        assert r.status_code == 200
        body = r.json()
        assert body["sent"] is True
        assert "成功" in body["message"]
        assert mock_send.call_count == 1
        assert "AlphaPilot A-Share Webhook Warmup" in mock_send.call_args.args[0]
        assert mock_send.call_args.args[1] == self.WEBHOOK_URL

    def test_inline_wecom_warmup_does_not_persist_unsaved_webhook(self):
        with patch("api.services.wecom_notification_service.send_message", return_value=True) as mock_send:
            r = self.client.post("/v1/config/wecom/warmup", headers=self.headers, json={
                "wecom_webhook_url": "inline-key-1234",
            })

        assert r.status_code == 200
        assert mock_send.call_count == 1
        assert mock_send.call_args.args[1] == (
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=inline-key-1234"
        )

        config_resp = self.client.get("/v1/config", headers=self.headers)
        assert config_resp.status_code == 200
        assert config_resp.json()["has_wecom_webhook"] is False

    def test_invalid_wecom_url_is_rejected(self):
        r = self.client.patch("/v1/config", headers=self.headers, json={
            "wecom_webhook_url": "http://169.254.169.254/latest/meta-data/",
            "warmup": False,
        })

        assert r.status_code == 400
        assert "企业微信 Webhook" in r.json()["detail"]


class TestWpsRuntimeConfig:
    WPS_HOOK = "https://xz.wps.cn/api/v1/webhook/send?key=wps-test-key-abcdef"

    @pytest.fixture(autouse=True)
    def setup(self):
        self.client = _get_client()
        self.token = _auth_unique(self.client)
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def test_config_returns_masked_wps_webhook(self):
        r = self.client.patch("/v1/config", headers=self.headers, json={
            "wps_webhook_url": self.WPS_HOOK,
            "warmup": False,
        })
        assert r.status_code == 200
        current = r.json()["current"]
        assert current["has_wps_webhook"] is True
        assert current["wps_webhook_display"].startswith("https://xz.wps.cn/api/v1/webhook/send?key=")
        assert current["wps_webhook_display"] != self.WPS_HOOK

    def test_wps_warmup_uses_stored_webhook(self):
        save_resp = self.client.patch("/v1/config", headers=self.headers, json={
            "wps_webhook_url": self.WPS_HOOK,
            "warmup": False,
        })
        assert save_resp.status_code == 200

        with patch(
            "api.services.wps_notification_service.send_markdown_message", return_value=True
        ) as mock_send:
            r = self.client.post("/v1/config/wps/warmup", headers=self.headers, json={})

        assert r.status_code == 200
        assert r.json()["sent"] is True
        assert mock_send.call_count == 1
        assert "AlphaPilot A-Share" in mock_send.call_args.args[0]
        assert mock_send.call_args.args[1] == self.WPS_HOOK

    def test_invalid_wps_url_is_rejected(self):
        r = self.client.patch("/v1/config", headers=self.headers, json={
            "wps_webhook_url": "https://evil.com/api/v1/webhook/send?key=x",
            "warmup": False,
        })
        assert r.status_code == 400
        assert "WPS" in r.json()["detail"] or "金山" in r.json()["detail"]

    def test_config_accepts_kdocs_woa_webhook(self):
        kdocs = "https://365.kdocs.cn/woa/api/v1/webhook/send?key=kdocs-smoke-test-key"
        r = self.client.patch("/v1/config", headers=self.headers, json={
            "wps_webhook_url": kdocs,
            "warmup": False,
        })
        assert r.status_code == 200
        cur = r.json()["current"]
        assert cur["has_wps_webhook"] is True
        assert cur["wps_webhook_display"].startswith("https://365.kdocs.cn/woa/api/v1/webhook/send?key=")


class TestWatchlistAddEndpoint:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.client = _get_client()
        self.token = _auth_unique(self.client)
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def test_batch_add_supports_codes_and_full_names(self):
        name_to_code = {
            "贵州茅台": "600519.SH",
            "宁德时代": "300750.SZ",
        }
        code_to_name = {value: key for key, value in name_to_code.items()}
        with patch("api.main._load_cn_stock_map", return_value=name_to_code), \
             patch("api.main._get_reverse_stock_map", return_value=code_to_name):
            r = self.client.post("/v1/watchlist", headers=self.headers, json={
                "text": "600519 宁德时代, 未知标的",
            })
        assert r.status_code == 200
        body = r.json()
        assert body["summary"] == {"total": 3, "added": 2, "duplicate": 0, "failed": 1}
        assert [item["status"] for item in body["results"]] == ["added", "added", "invalid"]
        assert body["results"][0]["symbol"] == "600519.SH"
        assert body["results"][1]["symbol"] == "300750.SZ"

    def test_batch_add_marks_duplicates(self):
        name_to_code = {
            "贵州茅台": "600519.SH",
        }
        code_to_name = {value: key for key, value in name_to_code.items()}
        with patch("api.main._load_cn_stock_map", return_value=name_to_code), \
             patch("api.main._get_reverse_stock_map", return_value=code_to_name):
            r = self.client.post("/v1/watchlist", headers=self.headers, json={
                "text": "600519.SH 贵州茅台",
            })
        assert r.status_code == 200
        body = r.json()
        assert body["summary"] == {"total": 2, "added": 1, "duplicate": 1, "failed": 0}
        assert [item["status"] for item in body["results"]] == ["added", "duplicate"]


class TestRecommendationEndpoint:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.client = _get_client()
        self.token = _auth_unique(self.client)
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def test_recommendations_endpoint_returns_ranked_items(self):
        # 先放一个跟踪持仓，确保候选池非空
        sync = self.client.post(
            "/v1/portfolio/imports",
            headers=self.headers,
            json={
                "positions": [{"symbol": "600519.SH", "name": "贵州茅台", "current_position": 100}],
                "auto_apply_scheduled": False,
            },
        )
        assert sync.status_code == 200

        fake_quotes = {
            "600519.SH": {
                "price": 1720,
                "change_pct": 2.9,
                "high": 1725,
                "amount": 1500000000,
                "volume": 2400000,
                "quote_time": "2026-04-10 14:30:00",
                "source": "mock",
            }
        }
        with patch(
            "api.services.daily_stock_analysis_service.route_to_vendor",
            return_value=str(fake_quotes).replace("'", '"'),
        ):
            r = self.client.post(
                "/v1/recommendations",
                headers=self.headers,
                json={"top_k": 3, "include_tracking": True, "include_watchlist": False},
            )

        assert r.status_code == 200
        body = r.json()
        assert body["pool_size"] >= 1
        assert len(body["items"]) >= 1
        assert body["items"][0]["symbol"] == "600519.SH"
        assert body["scoring_model"]["market"] == "cn"

    def test_recommendations_endpoint_supports_market_scan_source(self):
        universe = {
            "000001.SZ": "平安银行",
            "600519.SH": "贵州茅台",
        }
        fake_quotes = {
            "000001.SZ": {
                "price": 12.3,
                "change_pct": 2.1,
                "high": 12.35,
                "amount": 300000000,
                "volume": 1000000,
            },
            "600519.SH": {
                "price": 1720,
                "change_pct": 6.0,
                "high": 1724,
                "amount": 1800000000,
                "volume": 2200000,
            },
        }
        with patch("api.services.market_scanner_service._load_cn_universe", return_value=universe), \
             patch("api.services.market_scanner_service.route_to_vendor", return_value=str(fake_quotes).replace("'", '"')):
            r = self.client.post(
                "/v1/recommendations",
                headers=self.headers,
                json={"top_k": 2, "source_mode": "market_scan", "scan_limit": 500},
            )
        assert r.status_code == 200
        body = r.json()
        assert body["items"][0]["symbol"] == "600519.SH"
        assert body["items"][0]["strategy_hits"]
        assert body["scoring_model"]["engine"] == "market_scan_v3"


class TestReportsEndpoint:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.client = _get_client()
        self.token = _auth_unique(self.client)
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def _create_report(self, symbol: str, trade_date: str, decision: str):
        response = self.client.post("/v1/reports", headers=self.headers, json={
            "symbol": symbol,
            "trade_date": trade_date,
            "decision": decision,
        })
        assert response.status_code == 200
        return response.json()

    def test_latest_by_symbols_returns_only_each_symbol_latest_report(self):
        self._create_report("600519.SH", "2026-03-28", "HOLD")
        self._create_report("600519.SH", "2026-03-30", "BUY")
        self._create_report("300750.SZ", "2026-03-29", "SELL")

        response = self.client.post(
            "/v1/reports/latest-by-symbols",
            headers=self.headers,
            json={"symbols": ["300750.SZ", "600519.SH", "000001.SZ"]},
        )

        assert response.status_code == 200
        body = response.json()
        assert [item["symbol"] for item in body["reports"]] == ["300750.SZ", "600519.SH"]
        assert body["reports"][0]["decision"] == "SELL"
        assert body["reports"][1]["decision"] == "BUY"

    def test_list_reports_filters_by_trade_date_range(self):
        self._create_report("600519.SH", "2026-03-28", "HOLD")
        self._create_report("600519.SH", "2026-03-30", "BUY")
        self._create_report("300750.SZ", "2026-04-02", "SELL")

        response = self.client.get(
            "/v1/reports?start_date=2026-03-29&end_date=2026-04-01",
            headers=self.headers,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 1
        assert len(body["reports"]) == 1
        assert body["reports"][0]["symbol"] == "600519.SH"
        assert body["reports"][0]["trade_date"] == "2026-03-30"

    def test_list_reports_filters_by_created_at_minute_range(self):
        from datetime import datetime, timezone

        from api.database import ReportDB, get_db_ctx

        early = self._create_report("600519.SH", "2026-04-10", "HOLD")
        late = self._create_report("600519.SH", "2026-04-10", "BUY")
        outside = self._create_report("300750.SZ", "2026-04-10", "SELL")
        with get_db_ctx() as db:
            db.query(ReportDB).filter(ReportDB.id == early["id"]).update(
                {ReportDB.created_at: datetime(2026, 4, 10, 10, 0, 0, tzinfo=timezone.utc)}
            )
            db.query(ReportDB).filter(ReportDB.id == late["id"]).update(
                {ReportDB.created_at: datetime(2026, 4, 10, 10, 45, 0, tzinfo=timezone.utc)}
            )
            db.query(ReportDB).filter(ReportDB.id == outside["id"]).update(
                {ReportDB.created_at: datetime(2026, 4, 10, 11, 30, 0, tzinfo=timezone.utc)}
            )
            db.commit()

        response = self.client.get(
            "/v1/reports?start_date=2026-04-10T10:00:00%2B00:00&end_date=2026-04-10T10:30:00%2B00:00",
            headers=self.headers,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 1
        assert len(body["reports"]) == 1
        assert body["reports"][0]["id"] == early["id"]

        response = self.client.get(
            "/v1/reports?start_date=2026-04-10T10:00:00%2B00:00&end_date=2026-04-10T11:00:00%2B00:00",
            headers=self.headers,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 2
        ids = {item["id"] for item in body["reports"]}
        assert ids == {early["id"], late["id"]}

    def test_list_reports_filters_by_model_profile(self):
        from api.database import ReportDB, get_db_ctx
        from api.services import model_profile_service

        me = self.client.get("/v1/auth/me", headers=self.headers)
        user_id = me.json()["id"]
        with get_db_ctx() as db:
            profile = model_profile_service.create_model_profile(
                db,
                user_id=user_id,
                name=f"Filter-Model-{uuid4().hex[:6]}",
                llm_provider="openai",
                quick_think_llm="filter-model-quick",
                deep_think_llm="filter-model-deep",
                backend_url="https://example.com/v1",
                is_default=False,
                is_active=True,
            )
            matched = self._create_report("600519.SH", "2026-04-10", "BUY")
            other = self._create_report("300750.SZ", "2026-04-11", "SELL")
            db.query(ReportDB).filter(ReportDB.id == matched["id"]).update(
                {
                    ReportDB.result_data: {
                        "model_info": {
                            "model_profile_id": profile["id"],
                            "model_profile_name": profile["name"],
                            "deep_think_llm": profile["deep_think_llm"],
                            "quick_think_llm": profile["quick_think_llm"],
                        }
                    }
                }
            )
            db.query(ReportDB).filter(ReportDB.id == other["id"]).update(
                {
                    ReportDB.result_data: {
                        "model_info": {
                            "deep_think_llm": "other-model-deep",
                            "quick_think_llm": "other-model-quick",
                        }
                    }
                }
            )
            db.commit()

        response = self.client.get(
            f"/v1/reports?model_profile_id={profile['id']}",
            headers=self.headers,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 1
        assert body["reports"][0]["id"] == matched["id"]

    def test_batch_delete_endpoint_removes_multiple_reports(self):
        first = self._create_report("600519.SH", "2026-03-28", "HOLD")
        second = self._create_report("300750.SZ", "2026-03-29", "SELL")
        third = self._create_report("000001.SZ", "2026-03-30", "BUY")

        response = self.client.post(
            "/v1/reports/batch/delete",
            headers=self.headers,
            json={"report_ids": [first["id"], second["id"], "missing-report-id"]},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["deleted_ids"] == [first["id"], second["id"]]
        assert body["missing_ids"] == ["missing-report-id"]

        remaining = self.client.get("/v1/reports", headers=self.headers)
        assert remaining.status_code == 200
        remaining_ids = [item["id"] for item in remaining.json()["reports"]]
        assert remaining_ids == [third["id"]]

    def test_export_report_html_endpoint_returns_html_content(self):
        created = self._create_report("600519.SH", "2026-03-30", "BUY")
        report_id = created["id"]
        response = self.client.get(f"/v1/reports/{report_id}/export/html", headers=self.headers)
        assert response.status_code == 200
        assert response.headers.get("content-type", "").startswith("text/html")
        assert "600519.SH" in response.text

    def test_export_stock_team_enhanced_html_endpoint_returns_html_content(self):
        created = self._create_report("600519.SH", "2026-03-30", "BUY")
        report_id = created["id"]
        with patch(
            "api.services.stock_analysis_skill_service.generate_enhanced_html_from_report",
            return_value={"success": True, "html": "<html><body>enhanced</body></html>"},
        ):
            response = self.client.get(
                f"/v1/reports/{report_id}/export/stock-team-enhanced-html?include_charts=false",
                headers=self.headers,
            )
        assert response.status_code == 200
        assert response.headers.get("content-type", "").startswith("text/html")
        assert "enhanced" in response.text

    def test_report_detail_contains_model_info(self):
        create_resp = self.client.post("/v1/reports", headers=self.headers, json={
            "symbol": "600519.SH",
            "trade_date": "2026-03-30",
            "decision": "BUY",
            "result_data": {
                "model_profile_id": "profile-demo",
                "model_profile_name": "DeepSeek 主配置",
                "llm_provider": "openai",
                "quick_think_llm": "deepseek-v4-flash",
                "deep_think_llm": "deepseek-v4-pro",
                "backend_url": "https://api.deepseek.com/v1",
            },
        })
        assert create_resp.status_code == 200
        report_id = create_resp.json()["id"]

        detail_resp = self.client.get(f"/v1/reports/{report_id}", headers=self.headers)
        assert detail_resp.status_code == 200
        detail = detail_resp.json()
        assert detail["model_profile_id"] == "profile-demo"
        assert detail["model_profile_name"] == "DeepSeek 主配置"
        assert detail["llm_provider"] == "openai"
        assert detail["quick_think_llm"] == "deepseek-v4-flash"
        assert detail["deep_think_llm"] == "deepseek-v4-pro"
        assert detail["backend_url"] == "https://api.deepseek.com/v1"

    def test_report_list_contains_model_info(self):
        create_resp = self.client.post("/v1/reports", headers=self.headers, json={
            "symbol": "300750.SZ",
            "trade_date": "2026-03-31",
            "decision": "HOLD",
            "result_data": {
                "model_profile_name": "DeepSeek 主配置",
                "llm_provider": "openai",
                "quick_think_llm": "deepseek-v4-flash",
                "deep_think_llm": "deepseek-v4-pro",
            },
        })
        assert create_resp.status_code == 200

        list_resp = self.client.get("/v1/reports?symbol=300750.SZ&skip=0&limit=5", headers=self.headers)
        assert list_resp.status_code == 200
        reports = list_resp.json()["reports"]
        assert reports
        assert reports[0]["model_profile_name"] == "DeepSeek 主配置"
        assert reports[0]["llm_provider"] == "openai"
        assert reports[0]["quick_think_llm"] == "deepseek-v4-flash"
        assert reports[0]["deep_think_llm"] == "deepseek-v4-pro"


class TestDailyProductBatchEndpoint:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.client = _get_client()
        self.token = _auth_unique(self.client)
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def test_daily_product_run_recommended_creates_jobs(self):
        sync = self.client.post(
            "/v1/portfolio/imports",
            headers=self.headers,
            json={
                "positions": [{"symbol": "600519.SH", "name": "贵州茅台", "current_position": 100}],
                "auto_apply_scheduled": False,
            },
        )
        assert sync.status_code == 200

        fake_quotes = {
            "600519.SH": {
                "price": 1720,
                "change_pct": 2.9,
                "high": 1725,
                "amount": 1500000000,
                "volume": 2400000,
                "quote_time": "2026-04-10 14:30:00",
                "source": "mock",
            }
        }
        with patch(
            "api.services.daily_stock_analysis_service.route_to_vendor",
            return_value=str(fake_quotes).replace("'", '"'),
        ):
            r = self.client.post(
                "/v1/daily-product/run",
                headers=self.headers,
                json={
                    "mode": "recommended",
                    "top_k": 1,
                    "include_tracking": True,
                    "include_watchlist": False,
                    "ensure_scheduled": True,
                },
            )
        assert r.status_code == 200
        body = r.json()
        assert body["mode"] == "recommended"
        assert body["run_id"]
        assert body["status"] in ("pending", "running", "completed")
        assert body["summary"]["queued_jobs"] == 1
        assert len(body["jobs"]) == 1
        assert body["jobs"][0]["symbol"] == "600519.SH"
        assert body["recommendation"]["items"][0]["symbol"] == "600519.SH"

    def test_daily_product_run_list_and_detail(self):
        fake_quotes = {
            "600519.SH": {"price": 1720, "change_pct": 2.9, "high": 1725, "amount": 1500000000, "volume": 2400000}
        }
        with patch(
            "api.services.daily_stock_analysis_service.route_to_vendor",
            return_value=str(fake_quotes).replace("'", '"'),
        ):
            created = self.client.post(
                "/v1/daily-product/run",
                headers=self.headers,
                json={"mode": "recommended", "top_k": 1, "seed_symbols": ["600519.SH"]},
            )
        assert created.status_code == 200
        run_id = created.json()["run_id"]

        lst = self.client.get("/v1/daily-product/runs?limit=5", headers=self.headers)
        assert lst.status_code == 200
        runs = lst.json()["runs"]
        assert any(r["run_id"] == run_id for r in runs)

        detail = self.client.get(f"/v1/daily-product/runs/{run_id}", headers=self.headers)
        assert detail.status_code == 200
        assert detail.json()["run_id"] == run_id

    def test_daily_product_run_auto_strategy_skills(self):
        fake_quotes = {
            "600519.SH": {"price": 1720, "change_pct": 2.9, "high": 1725, "amount": 1500000000, "volume": 2400000}
        }
        with patch(
            "api.services.daily_stock_analysis_service.route_to_vendor",
            return_value=str(fake_quotes).replace("'", '"'),
        ):
            created = self.client.post(
                "/v1/daily-product/run",
                headers=self.headers,
                json={
                    "mode": "recommended",
                    "top_k": 1,
                    "seed_symbols": ["600519.SH"],
                    "strategy_mode": "auto",
                    "strategy_skills": ["backtesting_guidelines"],
                },
            )
        assert created.status_code == 200
        summary = created.json().get("summary") or {}
        assert summary.get("strategy_mode") == "auto"
        skills = summary.get("strategy_skills") or []
        assert "analysis_framework" in skills
        assert "market_strategy" in skills
        assert "risk_scoring" in skills
        assert "backtesting_guidelines" in skills

    def test_daily_product_run_market_scan_creates_jobs(self):
        universe = {
            "000001.SZ": "平安银行",
            "600519.SH": "贵州茅台",
        }
        fake_quotes = {
            "000001.SZ": {"price": 12.3, "change_pct": 2.1, "high": 12.35, "amount": 300000000, "volume": 1000000},
            "600519.SH": {"price": 1720, "change_pct": 6.0, "high": 1724, "amount": 1800000000, "volume": 2200000},
        }
        with patch("api.services.market_scanner_service._load_cn_universe", return_value=universe), \
             patch("api.services.market_scanner_service.route_to_vendor", return_value=str(fake_quotes).replace("'", '"')):
            created = self.client.post(
                "/v1/daily-product/run",
                headers=self.headers,
                json={
                    "mode": "recommended",
                    "top_k": 1,
                    "recommendation_source": "market_scan",
                    "scan_limit": 500,
                },
            )
        assert created.status_code == 200
        body = created.json()
        assert body["recommendation"]["items"][0]["symbol"] == "600519.SH"
        assert body["summary"]["recommendation_source"] == "market_scan"
        assert len(body["jobs"]) == 1

    def test_daily_product_strategy_skills_endpoint(self):
        resp = self.client.get("/v1/daily-product/strategy-skills", headers=self.headers)
        assert resp.status_code == 200
        body = resp.json()
        assert any(item.get("id") == "auto" for item in body.get("routing_modes") or [])
        assert any(item.get("id") == "market_strategy" for item in body.get("skills") or [])

    def test_recommendation_history_and_strategy_stats_endpoints(self):
        universe = {
            "600519.SH": "贵州茅台",
        }
        fake_quotes = {
            "600519.SH": {"price": 1720, "change_pct": 6.0, "high": 1724, "amount": 1800000000, "volume": 2200000},
        }
        with patch("api.services.market_scanner_service._load_cn_universe", return_value=universe), \
             patch("api.services.market_scanner_service.route_to_vendor", return_value=str(fake_quotes).replace("'", '"')):
            created = self.client.post(
                "/v1/daily-product/run",
                headers=self.headers,
                json={"mode": "recommended", "top_k": 1, "recommendation_source": "market_scan", "scan_limit": 500},
            )
        assert created.status_code == 200

        history = self.client.get("/v1/recommendations/history?limit=10", headers=self.headers)
        assert history.status_code == 200
        assert history.json()["total"] >= 1

        stats = self.client.get("/v1/recommendations/strategy-stats?limit=10", headers=self.headers)
        assert stats.status_code == 200
        assert "learned_weights" in stats.json()

    def test_insights_t1_endpoints(self):
        r1 = self.client.get("/v1/insights/t1/recommendations-trend?days=30", headers=self.headers)
        assert r1.status_code == 200
        assert "series" in r1.json()
        r2 = self.client.get("/v1/insights/t1/reports-accuracy-trend?days=30", headers=self.headers)
        assert r2.status_code == 200
        j2 = r2.json()
        assert "series" in j2 and "series_all" in j2
        r3 = self.client.post("/v1/insights/t1/refresh", headers=self.headers)
        assert r3.status_code == 200
        body = r3.json()
        assert "market_scan" in body and "reports" in body


class TestPortfolioOverviewEndpoint:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.client = _get_client()
        self.token = _auth_unique(self.client)
        self.headers = {"Authorization": f"Bearer {self.token}"}
        self.name_to_code = {
            "贵州茅台": "600519.SH",
            "宁德时代": "300750.SZ",
        }
        self.code_to_name = {value: key for key, value in self.name_to_code.items()}

    def _add_watchlist(self, text: str):
        with patch("api.main._load_cn_stock_map", return_value=self.name_to_code), \
             patch("api.main._get_reverse_stock_map", return_value=self.code_to_name):
            response = self.client.post("/v1/watchlist", headers=self.headers, json={"text": text})
        assert response.status_code == 200

    def _create_scheduled(self, symbol: str):
        with patch("api.main._get_reverse_stock_map", return_value=self.code_to_name):
            response = self.client.post(
                "/v1/scheduled",
                headers=self.headers,
                json={"symbol": symbol, "horizon": "short", "trigger_time": "20:00"},
            )
        assert response.status_code == 201

    def test_overview_returns_watchlist_scheduled_portfolio_and_latest_reports(self):
        from api.database import ImportedPortfolioPositionDB, get_db_ctx

        self._add_watchlist("600519.SH 300750.SZ")
        self._create_scheduled("600519.SH")

        self.client.post("/v1/reports", headers=self.headers, json={
            "symbol": "600519.SH",
            "trade_date": "2026-03-30",
            "decision": "BUY",
        })
        self.client.post("/v1/reports", headers=self.headers, json={
            "symbol": "300750.SZ",
            "trade_date": "2026-03-29",
            "decision": "SELL",
        })

        current_user = self.client.get("/v1/auth/me", headers=self.headers).json()
        with get_db_ctx() as db:
            db.add(
                ImportedPortfolioPositionDB(
                    id=uuid4().hex,
                    user_id=current_user["id"],
                    source="manual",
                    symbol="600519.SH",
                    security_name="贵州茅台",
                    current_position=300.0,
                    average_cost=1680.5,
                    market_value=504150.0,
                )
            )
            db.commit()

        with patch("api.main._get_reverse_stock_map", return_value=self.code_to_name):
            response = self.client.get("/v1/portfolio/overview", headers=self.headers)

        assert response.status_code == 200
        body = response.json()
        assert [item["symbol"] for item in body["watchlist"]] == ["600519.SH", "300750.SZ"]
        assert body["watchlist"][0]["name"] == "贵州茅台"
        assert len(body["scheduled"]) == 1
        assert body["scheduled"][0]["symbol"] == "600519.SH"
        assert body["scheduled"][0]["has_imported_context"] is True
        assert [item["symbol"] for item in body["latest_reports"]] == ["600519.SH", "300750.SZ"]
        assert body["portfolio_import"]["summary"]["positions"] == 1


class TestScheduledBatchEndpoints:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.client = _get_client()
        self.token = _auth_unique(self.client)
        self.headers = {"Authorization": f"Bearer {self.token}"}
        self.code_to_name = {
            "300750.SZ": "宁德时代",
            "600519.SH": "贵州茅台",
        }

    def _create_scheduled(self, symbol: str):
        with patch("api.main._get_reverse_stock_map", return_value=self.code_to_name):
            response = self.client.post(
                "/v1/scheduled",
                headers=self.headers,
                json={"symbol": symbol, "horizon": "short", "trigger_time": "20:00"},
            )
        assert response.status_code == 201
        return response.json()

    def test_batch_update_endpoint_updates_multiple_items(self):
        first = self._create_scheduled("300750.SZ")
        second = self._create_scheduled("600519.SH")

        with patch("api.main._get_reverse_stock_map", return_value=self.code_to_name):
            response = self.client.patch(
                "/v1/scheduled/batch",
                headers=self.headers,
                json={
                    "item_ids": [first["id"], second["id"]],
                    "horizon": "medium",
                    "trigger_time": "21:30",
                    "is_active": True,
                },
            )

        assert response.status_code == 200
        body = response.json()
        assert [item["horizon"] for item in body["items"]] == ["medium", "medium"]
        assert [item["trigger_time"] for item in body["items"]] == ["21:30", "21:30"]
        assert [item["name"] for item in body["items"]] == ["宁德时代", "贵州茅台"]

    def test_batch_delete_endpoint_removes_multiple_items(self):
        first = self._create_scheduled("300750.SZ")
        second = self._create_scheduled("600519.SH")

        response = self.client.post(
            "/v1/scheduled/batch/delete",
            headers=self.headers,
            json={"item_ids": [first["id"], second["id"]]},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["deleted_ids"] == [first["id"], second["id"]]
        assert body["missing_ids"] == []

        remaining = self.client.get("/v1/scheduled", headers=self.headers)
        assert remaining.status_code == 200
        assert remaining.json()["items"] == []

    def test_manual_trigger_endpoint_queues_single_scheduled_task(self):
        item = self._create_scheduled("300750.SZ")
        run_once = AsyncMock()

        def _close_coro(coro):
            coro.close()
            return MagicMock()

        with patch("api.main._run_scheduled_analysis_once", run_once), \
             patch("api.main._create_tracked_task", side_effect=_close_coro), \
             patch("api.main.cn_today_str", return_value="2026-03-31"), \
             patch("api.main._resolve_scheduled_trade_date", return_value="2026-03-31"):
            response = self.client.post(
                f"/v1/scheduled/{item['id']}/trigger",
                headers=self.headers,
            )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "pending"
        assert run_once.call_count == 1

        args, kwargs = run_once.call_args
        assert args[0]["id"] == item["id"]
        assert args[0]["symbol"] == "300750.SZ"
        assert args[0]["user_id"]
        assert args[1] == "2026-03-31"
        assert args[2] == body["job_id"]
        assert kwargs == {"mark_schedule_run": False}

    def test_batch_trigger_endpoint_ignores_imported_position_context(self):
        """深度分析保持客观：即使存在导入持仓，也不注入持仓上下文。"""
        from api.database import ImportedPortfolioPositionDB, get_db_ctx

        first = self._create_scheduled("300750.SZ")
        second = self._create_scheduled("600519.SH")
        current_user = self.client.get("/v1/auth/me", headers=self.headers).json()

        with get_db_ctx() as db:
            # (user_id, source, symbol) 上有唯一约束，先清理以保证测试可重复运行。
            db.query(ImportedPortfolioPositionDB).filter(
                ImportedPortfolioPositionDB.user_id == current_user["id"],
                ImportedPortfolioPositionDB.source == "manual",
                ImportedPortfolioPositionDB.symbol == "600519.SH",
            ).delete(synchronize_session=False)
            db.add(
                ImportedPortfolioPositionDB(
                    id=uuid4().hex,
                    user_id=current_user["id"],
                    source="manual",
                    symbol="600519.SH",
                    security_name="贵州茅台",
                    current_position=300.0,
                    average_cost=1680.5,
                    market_value=504150.0,
                )
            )
            db.commit()

        run_once = AsyncMock()

        def _close_coro(coro):
            coro.close()
            return MagicMock()

        with patch("api.main._run_scheduled_analysis_once", run_once), \
             patch("api.main._create_tracked_task", side_effect=_close_coro), \
             patch("api.main.cn_today_str", return_value="2026-03-31"), \
             patch("api.main._resolve_scheduled_trade_date", return_value="2026-03-31"), \
             patch("api.main._get_reverse_stock_map", return_value=self.code_to_name):
            response = self.client.post(
                "/v1/scheduled/batch/trigger",
                headers=self.headers,
                json={"item_ids": [first["id"], second["id"]]},
            )

        assert response.status_code == 200
        body = response.json()
        assert body["summary"] == {"total": 2}
        assert [job["symbol"] for job in body["jobs"]] == ["300750.SZ", "600519.SH"]
        # 请求回显里的持仓字段必须为空，说明持仓没有被注入
        assert body["jobs"][0]["current_position"] is None
        assert body["jobs"][0]["average_cost"] is None
        assert body["jobs"][1]["current_position"] is None
        assert body["jobs"][1]["average_cost"] is None
        assert run_once.call_count == 2

        first_args, first_kwargs = run_once.call_args_list[0]
        second_args, second_kwargs = run_once.call_args_list[1]
        assert first_args[0]["id"] == first["id"]
        assert first_args[0]["symbol"] == "300750.SZ"
        assert second_args[0]["id"] == second["id"]
        assert second_args[0]["symbol"] == "600519.SH"
        assert first_args[1] == "2026-03-31"
        assert second_args[1] == "2026-03-31"
        assert first_kwargs == {"mark_schedule_run": False}
        assert second_kwargs == {"mark_schedule_run": False}
        # 任务快照不得携带持仓上下文（否则会进入提示词）
        assert first_args[0]["manual_user_context"] == {}
        assert second_args[0]["manual_user_context"] == {}


class TestPaperTradingFlow:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.client = _get_client()
        self.token = _auth_unique(self.client)
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def test_bootstrap_daily_ops_and_review(self):
        sync = self.client.post(
            "/v1/portfolio/imports",
            headers=self.headers,
            json={
                "positions": [{"symbol": "600519.SH", "name": "贵州茅台", "current_position": 100, "average_cost": 100.0}],
                "auto_apply_scheduled": False,
            },
        )
        assert sync.status_code == 200

        bootstrap = self.client.post(
            "/v1/paper-portfolio/bootstrap",
            headers=self.headers,
            json={"initial_cash": 100000, "reset_existing": True},
        )
        assert bootstrap.status_code == 200
        assert bootstrap.json()["positions"][0]["symbol"] == "600519.SH"

        fake_quotes = {
            "600519.SH": {"price": 115.0},
            "000001.SZ": {"price": 12.0},
        }
        fake_rec = {
            "items": [{"symbol": "000001.SZ", "name": "平安银行", "live_price": 12.0, "score": 88.0}],
        }
        with patch("api.services.paper_trading_service._fetch_quotes", return_value=fake_quotes), \
             patch("api.services.recommendation_service.recommend_for_user", return_value=fake_rec):
            run = self.client.post(
                "/v1/paper-portfolio/daily-ops",
                headers=self.headers,
                json={
                    "trade_date": "2026-04-12",
                    "include_recommendations": True,
                    "recommendation_top_k": 3,
                    "auto_execute": True,
                },
            )
        assert run.status_code == 200
        body = run.json()
        assert "plan" in body
        assert "review" in body
        assert body["review"]["trade_date"] == "2026-04-12"
        names = {item["symbol"]: item.get("name") for item in body["plan"]["actions"]}
        assert names["600519.SH"] == "贵州茅台"
        assert names["000001.SZ"] == "平安银行"

        review = self.client.get("/v1/paper-portfolio/daily-reviews/2026-04-12", headers=self.headers)
        assert review.status_code == 200
        assert review.json()["summary"]["trade_date"] == "2026-04-12"

    def test_manual_buy_trade_persists_security_name(self):
        with patch("api.services.paper_trading_service._fetch_quotes", return_value={"300750.SZ": {"price": 210.0}}):
            trade = self.client.post(
                "/v1/paper-trades",
                headers=self.headers,
                json={
                    "symbol": "300750.SZ",
                    "name": "宁德时代",
                    "side": "BUY",
                    "quantity": 100,
                    "trade_date": "2026-04-12",
                },
            )
        assert trade.status_code == 200
        assert trade.json()["name"] == "宁德时代"

        snapshot = self.client.get("/v1/paper-portfolio", headers=self.headers)
        assert snapshot.status_code == 200
        positions = {item["symbol"]: item for item in snapshot.json()["positions"]}
        assert positions["300750.SZ"]["name"] == "宁德时代"

    def test_sell_more_than_position_is_rejected(self):
        bootstrap = self.client.get("/v1/paper-portfolio", headers=self.headers)
        assert bootstrap.status_code == 200
        trade = self.client.post(
            "/v1/paper-trades",
            headers=self.headers,
            json={
                "symbol": "600519.SH",
                "side": "SELL",
                "quantity": 100,
                "price": 100.0,
                "trade_date": "2026-04-12",
            },
        )
        assert trade.status_code == 400


class TestAutoDailyOpsScheduler:
    def test_auto_daily_ops_tick_deduplicates_same_day(self):
        from api import main as main_api

        class _FakeDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 4, 13, 14, 30, tzinfo=tz)

        main_api._auto_daily_ops_sent.clear()
        with patch("api.main._auto_daily_ops_enabled", return_value=True), \
             patch("api.main._in_daily_ops_window", return_value=True), \
             patch("tradingagents.dataflows.trade_calendar.is_cn_trading_day", return_value=True), \
             patch("api.main._list_auto_daily_ops_user_ids", return_value=["user-a"]), \
             patch("api.main._execute_daily_operation_pipeline", return_value={"plan": {"actions": []}, "executed": []}) as execute_mock, \
             patch("api.main.datetime", _FakeDatetime):
            main_api._auto_daily_ops_tick()
            main_api._auto_daily_ops_tick()

        assert execute_mock.call_count == 1


# ---------------------------------------------------------------------------
# 主线候选股动作：报告必须带 candidate id（前端「加自选 / 深度分析」依赖它）
# ---------------------------------------------------------------------------

def _auth_unique_user(client: TestClient):
    """注册唯一测试用户，返回 (token, user_id)。"""
    from api.database import UserDB, get_db_ctx, init_db
    from api.services import auth_service

    init_db()
    email = auth_service.normalize_email(f"apitest-{uuid4().hex[:8]}@test.com")
    now = datetime.now(timezone.utc)
    with get_db_ctx() as db:
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
    return auth_service.create_access_token(user), user.id


class TestMainlineCandidateActions:
    """回归：报告 JSON 的 candidates 缺 id 时，前端点「加自选 / 深度分析」会静默无响应。"""

    @pytest.fixture(autouse=True)
    def setup(self):
        from api.database import MainlineCandidateDB, MainlineReportDB, get_db_ctx
        from api.services import mainline_service

        self.client = _get_client()
        self.token, self.user_id = _auth_unique_user(self.client)
        self.headers = {"Authorization": f"Bearer {self.token}"}
        self.run_id = f"smoke-{uuid4().hex[:12]}"

        with get_db_ctx() as db:
            mainline_service.create_run(
                db,
                user_id=self.user_id,
                run_id=self.run_id,
                trade_date="2026-08-31",
                perspective="short",
            )
            mainline_service.save_run_result(db, self.run_id, {
                "trade_date": "2026-08-31",
                "perspective": "short",
                "market": {"sources": {}, "emotion": None, "breadth": None, "benchmark": None},
                "analyst_report": "主线分析全文",
                "selector_report": "选股全文",
                "mainlines": [{"name": "AI算力", "type": "concept", "phase": "主升", "confidence": 78}],
                "candidates": [{
                    "symbol": "688120.SH",
                    "name": "华海清科",
                    "mainline": "AI算力",
                    "tier": "中军",
                    "score": 78,
                    "reasons": ["资金聚焦度最高"],
                    "entry_hint": "等回调",
                    "risk": "高位分歧",
                }],
                "gated_out": [],
                "warnings": [],
            })
        yield
        with get_db_ctx() as db:
            db.query(MainlineCandidateDB).filter(MainlineCandidateDB.report_id == self.run_id).delete()
            db.query(MainlineReportDB).filter(MainlineReportDB.id == self.run_id).delete()
            db.commit()

    def test_report_exposes_candidate_id(self):
        """GET 报告必须返回带 id 的候选股，且 id 能被候选股接口命中。"""
        r = self.client.get(f"/v1/mainline/runs/{self.run_id}", headers=self.headers)
        assert r.status_code == 200
        cands = r.json()["candidates"]
        assert len(cands) == 1
        assert cands[0]["id"], "候选股缺少 id：前端按钮会静默无响应"
        assert cands[0]["symbol"] == "688120.SH"

    def test_analyze_candidate_returns_job(self):
        """点「深度分析」必须真的提交任务并返回 job_id（这里打桩后台任务，不跑 LLM）。"""
        r = self.client.get(f"/v1/mainline/runs/{self.run_id}", headers=self.headers)
        candidate_id = r.json()["candidates"][0]["id"]

        def _drop_task(coro, **kwargs):
            coro.close()  # 不执行真实分析，避免写库/调模型
            return MagicMock()

        with patch("api.main._create_tracked_task", side_effect=_drop_task) as create_task:
            resp = self.client.post(
                f"/v1/mainline/candidates/{candidate_id}/analyze",
                headers=self.headers,
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["job_id"]
        # 命中的必须是同一只票
        assert body["candidate"]["symbol"] == "688120.SH"
        assert create_task.call_count == 1

    def test_watchlist_candidate_uses_same_id(self):
        """「加自选」同样按这个 id 定位。"""
        r = self.client.get(f"/v1/mainline/runs/{self.run_id}", headers=self.headers)
        candidate_id = r.json()["candidates"][0]["id"]

        resp = self.client.post(
            f"/v1/mainline/candidates/{candidate_id}/watchlist",
            headers=self.headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["candidate"]["symbol"] == "688120.SH"
