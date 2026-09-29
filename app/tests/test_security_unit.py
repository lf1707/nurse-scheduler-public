"""Unit tests for security controls that do not require live services."""

from __future__ import annotations

import smtplib
import time
from typing import Any, Literal

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.testclient import TestClient

from app.api.v1 import auth as auth_api
from app.api.v1.schedules import _csv_safe
from app.core import database as database_module
from app.core import rate_limit
from app.core.config import settings
from app.core.email import EmailMessageContent, send_email
from app.core.rate_limit import (
    RateLimitUnavailableError,
    client_ip,
    login_is_blocked,
    record_login_failure,
    should_audit_rate_limited_login,
)
from app.core.security import create_access_token, create_refresh_token, decode_token
from app.main import create_app
from app.schemas import LoginRequest


def _request_with_forwarded_for(value: str) -> Request:
    return Request(
        scope={
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"x-forwarded-for", value.encode())],
            "client": ("203.0.113.10", 50000),
            "query_string": b"",
            "server": ("testserver", 80),
        }
    )


def test_client_ip_ignores_forwarded_for_unless_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request_with_forwarded_for("198.51.100.7, 192.0.2.8")

    monkeypatch.setattr(settings, "TRUST_PROXY_FOR_CLIENT_IP", False)
    assert client_ip(request) == "203.0.113.10"

    monkeypatch.setattr(settings, "TRUST_PROXY_FOR_CLIENT_IP", True)
    assert client_ip(request) == "192.0.2.8"


async def test_smtp_delivery_starts_tls_before_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class FakeSmtp:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def __enter__(self) -> FakeSmtp:
            return self

        def __exit__(self, *_args: Any) -> Literal[False]:
            return False

        def starttls(self) -> None:
            calls.append("starttls")

        def login(self, username: str, password: str) -> None:
            calls.extend(("login", username, password))

        def send_message(self, message: object) -> None:
            calls.append("send_message")

    monkeypatch.setattr(settings, "EMAIL_TRANSPORT", "smtp")
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(settings, "SMTP_PORT", 587)
    monkeypatch.setattr(settings, "SMTP_USERNAME", "notification")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "secret")
    monkeypatch.setattr(smtplib, "SMTP", FakeSmtp)

    await send_email(
        EmailMessageContent(
            to_email="recipient@example.com",
            subject="TLS test",
            body="message body",
        )
    )

    assert calls == ["starttls", "login", "notification", "secret", "send_message"]


async def test_login_failures_block_email_and_ip_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values: dict[str, str] = {}

    class FakeRedis:
        async def mget(self, *keys: str) -> list[str | None]:
            return [values.get(key) for key in keys]

        def pipeline(self, transaction: bool = False) -> FakePipeline:
            return FakePipeline(values)

        async def delete(self, *keys: str) -> None:
            for key in keys:
                values.pop(key, None)

        async def aclose(self) -> None:
            return None

    class FakeAioredis:
        @staticmethod
        def from_url(*_args: Any, **_kwargs: Any) -> FakeRedis:
            return FakeRedis()

    class FakePipeline:
        def __init__(self, values: dict[str, str]) -> None:
            self.values = values
            self.commands: list[tuple[Any, ...]] = []

        def incr(self, key: str) -> None:
            self.commands.append(("incr", key))

        def expire(self, key: str, seconds: int) -> None:
            self.commands.append(("expire", key, seconds))

        async def execute(self) -> None:
            for command in self.commands:
                if command[0] == "incr":
                    self.values[command[1]] = str(int(self.values.get(command[1], "0")) + 1)

    monkeypatch.setattr(settings, "LOGIN_MAX_FAILURES_PER_EMAIL_IP", 5)
    monkeypatch.setattr(settings, "LOGIN_MAX_FAILURES_PER_IP", 20)
    monkeypatch.setattr(rate_limit, "aioredis", FakeAioredis)

    ip = "198.51.100.23"
    email = "security-unit@example.com"
    for _ in range(4):
        await record_login_failure(ip, email)
        assert not await login_is_blocked(ip, email)

    await record_login_failure(ip, email)
    assert await login_is_blocked(ip, email)


async def test_login_rate_limit_fails_closed_when_redis_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("redis unavailable")

    class UnavailableAioredis:
        @staticmethod
        def from_url(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("redis unavailable")

    monkeypatch.setattr(rate_limit, "aioredis", UnavailableAioredis)

    try:
        await login_is_blocked("198.51.100.24", "unavailable@example.com")
    except RateLimitUnavailableError:
        pass
    else:
        raise AssertionError("Redis outages must not disable login rate limiting")


async def test_rate_limited_login_audit_is_deduped_per_minute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: set[str] = set()

    class FakeRedis:
        async def set(self, key: str, value: str, *, nx: bool, ex: int) -> bool:
            assert (value, nx, ex) == ("1", True, 60)
            if key in seen:
                return False
            seen.add(key)
            return True

        async def aclose(self) -> None:
            return None

    class FakeAioredis:
        @staticmethod
        def from_url(*_args: Any, **_kwargs: Any) -> FakeRedis:
            return FakeRedis()

    monkeypatch.setattr(rate_limit, "aioredis", FakeAioredis)
    fingerprint = rate_limit.email_fingerprint("audit@example.com")
    epoch = int(time.time()) // 60

    assert await should_audit_rate_limited_login("198.51.100.30", "audit@example.com")
    assert not await should_audit_rate_limited_login("198.51.100.30", "audit@example.com")
    assert seen == {f"audit:auth_rate_limited:198.51.100.30:{fingerprint}:{epoch}"}


async def test_rate_limited_login_audit_tolerates_redis_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnavailableAioredis:
        @staticmethod
        def from_url(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("redis unavailable")

    monkeypatch.setattr(rate_limit, "aioredis", UnavailableAioredis)

    assert not await should_audit_rate_limited_login(
        "198.51.100.31", "outage@example.com"
    )


@pytest.mark.parametrize("audit_allowed", [True, False])
async def test_blocked_login_persists_one_audit_event(
    monkeypatch: pytest.MonkeyPatch,
    audit_allowed: bool,
) -> None:
    async def blocked(*_args: Any) -> bool:
        return True

    async def reserve(*_args: Any) -> bool:
        return audit_allowed

    class FakeSession:
        def __init__(self) -> None:
            self.context_cleared = False
            self.committed = False
            self.events: list[Any] = []

        def add(self, event: Any) -> None:
            self.events.append(event)

        async def commit(self) -> None:
            self.committed = True

        async def execute(self, *_args: Any, **_kwargs: Any) -> None:
            self.context_cleared = True

        async def __aenter__(self) -> FakeSession:
            return self

        async def __aexit__(self, *_args: Any) -> bool:
            return False

    session = FakeSession()

    class Factory:
        def __call__(self) -> FakeSession:
            return session

    monkeypatch.setattr(auth_api, "login_is_blocked", blocked)
    monkeypatch.setattr(
        auth_api, "should_audit_rate_limited_login", reserve
    )
    monkeypatch.setattr(database_module, "async_session_factory", Factory())

    request = Request(
        scope={
            "type": "http",
            "method": "POST",
            "path": "/api/v1/auth/login",
            "headers": [],
            "client": ("198.51.100.32", 50000),
            "query_string": b"",
            "server": ("testserver", 80),
        }
    )
    body = LoginRequest(
        email="blocked@example.com",
        password="wrong-password",
        totp_code=None,
        recovery_codes=None,
    )

    try:
        await auth_api.login(body, request)
    except HTTPException as exc:
        assert exc.status_code == 429
    else:
        raise AssertionError("Blocked login must raise 429")

    assert session.context_cleared is audit_allowed
    assert session.committed is audit_allowed
    if audit_allowed:
        assert len(session.events) == 1
        event = session.events[0]
        assert event.action == "auth.login.rate_limited"
        assert event.outcome == "denied"
        assert event.ip_address == "198.51.100.32"
        assert event.details["reason"] == "rate_limit"
        assert event.details["email_fingerprint"] == rate_limit.email_fingerprint(
            body.email
        )
    else:
        assert session.events == []


async def test_blocked_login_returns_429_when_audit_write_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def blocked(*_args: Any) -> bool:
        return True

    async def reserve(*_args: Any) -> bool:
        return True

    class FailingFactory:
        def __call__(self) -> None:
            raise RuntimeError("audit database unavailable")

    monkeypatch.setattr(auth_api, "login_is_blocked", blocked)
    monkeypatch.setattr(auth_api, "should_audit_rate_limited_login", reserve)
    monkeypatch.setattr(database_module, "async_session_factory", FailingFactory())

    request = Request(
        scope={
            "type": "http",
            "method": "POST",
            "path": "/api/v1/auth/login",
            "headers": [],
            "client": ("198.51.100.33", 50000),
            "query_string": b"",
            "server": ("testserver", 80),
        }
    )

    try:
        await auth_api.login(
            LoginRequest(
                email="blocked@example.com",
                password="wrong-password",
                totp_code=None,
                recovery_codes=None,
            ),
            request,
        )
    except HTTPException as exc:
        assert exc.status_code == 429
    else:
        raise AssertionError("Audit failure must not change the blocked response")


def test_jwt_claims_include_session_version_and_impersonation_scope() -> None:
    access = create_access_token(
        subject="user-id",
        tenant_id="tenant-id",
        role="viewer",
        token_version=7,
        extra_claims={"impersonated_by": "super-admin-id"},
        expires_minutes=15,
    )
    refresh = create_refresh_token(
        subject="user-id",
        token_version=7,
        expires_minutes=15,
        extra_claims={"impersonated_by": "super-admin-id"},
    )

    for token in (access, refresh):
        payload = decode_token(token)
        assert payload["ver"] == 7
        assert payload["impersonated_by"] == "super-admin-id"
        assert payload["exp"] - payload["iat"] <= 15 * 60 + 1


def test_login_page_has_no_default_credentials_and_uses_autocomplete() -> None:
    client = TestClient(create_app())
    response = client.get("/pages/login")

    assert response.status_code == 200
    assert "admin@example.com" not in response.text
    assert "changeme123" not in response.text
    assert 'autocomplete="username"' in response.text
    assert 'autocomplete="current-password"' in response.text


def test_frontend_dependencies_are_self_hosted() -> None:
    client = TestClient(create_app())

    assets = [
        "/static/vendor/pico/2/pico.min.css",
        "/static/vendor/htmx/2.0.4/htmx.min.js",
        "/static/vendor/alpine/3.14.8/cdn.min.js",
    ]
    for asset in assets:
        response = client.get(asset)
        assert response.status_code == 200
        assert len(response.content) > 10_000


def test_csv_export_neutralizes_spreadsheet_formulas() -> None:
    for value in ("=1+1", "+1", "-1", "@cmd", "\t=1", "\r=1"):
        assert _csv_safe(value).startswith("'")

    assert _csv_safe("Normal") == "Normal"
    assert _csv_safe(None) == ""
