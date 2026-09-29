"""Production-only hardening guards."""

from __future__ import annotations

import pathlib
import re
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.core.config import settings as app_settings
from app.main import create_app

PRODUCTION_EMAIL_SETTINGS: dict[str, Any] = {
    "PUBLIC_BASE_URL": "https://scheduler.example.com",
    "EMAIL_TRANSPORT": "smtp",
    "SMTP_HOST": "smtp.example.com",
}


def test_production_disables_api_documentation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_settings, "APP_ENV", "production")
    monkeypatch.setattr(app_settings, "APP_DEBUG", False)
    app = create_app()
    assert app.docs_url is None
    assert app.redoc_url is None
    assert app.openapi_url is None


def test_production_allows_configured_and_derived_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_settings, "APP_ENV", "production")
    monkeypatch.setattr(app_settings, "PUBLIC_BASE_URL", "https://scheduler.example.com")
    monkeypatch.setattr(app_settings, "ALLOWED_HOSTS", ["www.example.com"])
    assert app_settings.allowed_hosts == {"localhost", "scheduler.example.com", "www.example.com"}
    client = TestClient(create_app())
    allowed = client.get("/pages/login", headers={"Host": "Scheduler.example.com:443"})
    extra = client.get("/pages/login", headers={"Host": "www.example.com"})
    assert allowed.status_code == 200
    assert extra.status_code == 200


def test_production_rejects_unexpected_host_but_keeps_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_settings, "APP_ENV", "production")
    monkeypatch.setattr(app_settings, "PUBLIC_BASE_URL", "https://scheduler.example.com")
    monkeypatch.setattr(app_settings, "ALLOWED_HOSTS", [])
    client = TestClient(create_app())
    application = client.get("/pages/login", headers={"Host": "evil.example.com"})
    api = client.get("/api/v1/site-info", headers={"Host": "evil.example.com"})
    liveness = client.get("/health", headers={"Host": "localhost:8000"})
    assert application.status_code == 403
    assert api.status_code == 403
    assert liveness.status_code == 200


def test_production_rejects_invalid_allowed_host_configuration() -> None:
    with pytest.raises(ValidationError, match="ALLOWED_HOSTS"):
        cast(Any, Settings)(
            _env_file=None,
            APP_ENV="production",
            APP_DEBUG=False,
            JWT_SECRET="x" * 32,
            SUPER_ADMIN_PASSWORD="strong-enough-password",
            ALLOWED_HOSTS=["https://scheduler.example.com"],
            **PRODUCTION_EMAIL_SETTINGS,
        )


def test_production_rejects_insecure_configuration() -> None:
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        cast(Any, Settings)(
            _env_file=None,
            APP_ENV="production",
            APP_DEBUG=False,
            JWT_SECRET="too-short",
            SUPER_ADMIN_PASSWORD="strong-enough-password",
            **PRODUCTION_EMAIL_SETTINGS,
        )


def test_production_rejects_placeholder_demo_password() -> None:
    with pytest.raises(ValidationError, match="DEMO_TENANT_PASSWORD"):
        cast(Any, Settings)(
            _env_file=None,
            APP_ENV="production",
            APP_DEBUG=False,
            JWT_SECRET="x" * 32,
            SUPER_ADMIN_PASSWORD="strong-enough-password",
            ENABLE_DEMO_TENANT=True,
            DEMO_TENANT_PASSWORD="change-me-demo-password",
            **PRODUCTION_EMAIL_SETTINGS,
        )


def test_production_rejects_overlong_impersonation_sessions() -> None:
    with pytest.raises(ValidationError, match="IMPERSONATION_REFRESH"):
        cast(Any, Settings)(
            _env_file=None,
            APP_ENV="production",
            APP_DEBUG=False,
            JWT_SECRET="x" * 32,
            SUPER_ADMIN_PASSWORD="strong-enough-password",
            IMPERSONATION_REFRESH_TOKEN_EXPIRE_MINUTES=61,
            **PRODUCTION_EMAIL_SETTINGS,
        )


def test_admin_page_renders_simulation_toggle() -> None:
    client = TestClient(create_app())
    response = client.get("/pages/admin")
    assert response.status_code == 200
    assert "启用仿真测试" in response.text
    assert "创建 / 修复 Demo 租户" in response.text
    assert "安全审计" in response.text


def test_application_sets_security_headers() -> None:
    client = TestClient(create_app())
    response = client.get("/pages/login")
    assert response.status_code == 200
    policy = response.headers["Content-Security-Policy"]
    assert policy.startswith("default-src 'self'")
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    script_source = next(
        part.strip() for part in policy.split(";") if part.strip().startswith("script-src")
    )
    assert script_source == "script-src 'self'"
    assert "'unsafe-inline'" not in script_source
    assert "'unsafe-eval'" not in script_source
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert response.headers["Cross-Origin-Opener-Policy"] == "same-origin"
    assert response.headers["Permissions-Policy"] == "camera=(), microphone=(), geolocation=()"
    assert "cdn.jsdelivr.net" not in response.headers["Content-Security-Policy"]


def test_page_scripts_are_external_and_csp_safe() -> None:
    for template in pathlib.Path("frontend/templates").glob("*.html"):
        content = template.read_text()
        assert "<script>" not in content, template
        assert re.search("\\son[a-z]+\\s*=", content) is None, template


def test_release_version_is_rendered_from_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_settings, "APP_VERSION", "v9.9.9")
    app = create_app()
    client = TestClient(app)
    page = client.get("/pages/login")
    health = client.get("/health")
    assert app.version == "v9.9.9"
    assert page.status_code == 200
    assert "Nurse Scheduler · v9.9.9" in page.text
    assert "v0.1.0" not in page.text
    assert health.status_code == 200
    assert health.json()["version"] == "v9.9.9"


def test_security_audit_table_is_append_only_by_database_policy() -> None:
    migration = pathlib.Path(
        "alembic/versions/e7b9d2f6a8c1_add_security_audit_events.py"
    ).read_text()
    assert "ENABLE ROW LEVEL SECURITY" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "BEFORE UPDATE OR DELETE" in migration
    assert "BEFORE TRUNCATE" in migration
    assert "FOR UPDATE" not in migration
    assert "FOR DELETE" not in migration
