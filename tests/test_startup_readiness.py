import threading
import time
from unittest.mock import patch

from fastapi.testclient import TestClient


def test_cors_includes_vite_preview_and_dev_ports(monkeypatch):
    monkeypatch.delenv("CORS_ALLOW_ORIGINS", raising=False)
    monkeypatch.delenv("CORS_ALLOW_ORIGIN_REGEX", raising=False)
    monkeypatch.delenv("ENV", raising=False)
    from api.main import _cors_allow_origins, _cors_allow_origin_regex

    origins = _cors_allow_origins()
    for port in (4173, 5173, 8000):
        assert f"http://localhost:{port}" in origins
        assert f"http://127.0.0.1:{port}" in origins
    regex = _cors_allow_origin_regex()
    assert regex is not None
    import re

    compiled = re.compile(regex)
    assert compiled.match("http://localhost:4173")
    assert compiled.match("http://127.0.0.1:5176")


def test_cors_keeps_local_dev_origins_when_env_allowlist_set(monkeypatch):
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://app.example.com")
    monkeypatch.delenv("ENV", raising=False)
    from api import main as main_mod

    origins = main_mod._cors_allow_origins()
    assert "https://app.example.com" in origins
    assert "http://localhost:4173" in origins


def test_lifespan_healthz_not_blocked_by_slow_stock_map():
    release = threading.Event()

    def blocked_load():
        release.wait(timeout=30)
        return {}

    from api.main import app

    t0 = time.perf_counter()
    with patch("api.main._load_cn_stock_map", side_effect=blocked_load):
        with TestClient(app) as client:
            response = client.get("/healthz")
            elapsed = time.perf_counter() - t0
            assert response.status_code == 200
            assert response.json()["status"] == "ok"
            assert elapsed < 5
            release.set()


def test_request_code_returns_dev_code_without_mail(monkeypatch):
    monkeypatch.delenv("MAIL_HOST", raising=False)
    monkeypatch.delenv("MAIL_SERVER", raising=False)
    monkeypatch.delenv("SMTP_HOST", raising=False)
    from api.main import app

    with patch("api.main._load_cn_stock_map", return_value={}):
        with TestClient(app) as client:
            response = client.post("/v1/auth/request-code", json={"email": "startup-test@example.com"})
            assert response.status_code == 200
            body = response.json()
            assert body["message"] == "验证码已发送"
            assert body.get("dev_code")


def test_cors_regex_disabled_in_prod(monkeypatch):
    monkeypatch.delenv("CORS_ALLOW_ORIGIN_REGEX", raising=False)
    monkeypatch.setenv("ENV", "prod")
    from api import main as main_mod

    assert main_mod._cors_allow_origin_regex() is None
