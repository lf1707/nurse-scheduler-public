"""Security controls that require the live API and Redis."""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Iterator
from typing import TypedDict

import httpx
import pyotp
import pytest
from redis import Redis

from app.core.rate_limit import email_fingerprint
from app.tests.conftest import SUPER_ADMIN_TEST_MFA_SECRET

BASE = os.environ.get("API_BASE_URL", "http://localhost:8000")
API = f"{BASE}/api/v1"
WS_BASE = BASE.replace("http://", "ws://").replace("https://", "wss://")


class TenantContext(TypedDict):
    tenant_id: str
    token: str


class ScheduleContext(TypedDict, total=False):
    a: TenantContext
    b: TenantContext
    request_id: str


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _login(email: str, password: str) -> str:
    body = {"email": email, "password": password}
    if email == "admin@example.com":
        body["totp_code"] = pyotp.TOTP(SUPER_ADMIN_TEST_MFA_SECRET).now()
    response = httpx.post(f"{API}/auth/login", json=body)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert isinstance(payload["access_token"], str)
    return payload["access_token"]


@pytest.fixture(autouse=True)
def isolate_local_login_rate_limits() -> Iterator[None]:
    """Keep local live runs from inheriting earlier failure counters."""
    if not BASE.startswith("http://localhost:") and not BASE.startswith(
        "http://127.0.0.1:"
    ):
        yield
        return

    client = Redis.from_url(
        os.environ.get(
            "SECURITY_TEST_REDIS_URL",
            os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
        ),
        decode_responses=True,
    )
    for pattern in ("login_failure:*", "audit:auth_rate_limited:*"):
        keys = list(client.scan_iter(pattern))
        if keys:
            client.delete(*keys)
    yield
    for pattern in ("login_failure:*", "audit:auth_rate_limited:*"):
        keys = list(client.scan_iter(pattern))
        if keys:
            client.delete(*keys)


def test_login_failures_are_rate_limited_by_email_and_ip_pair() -> None:
    unique = uuid.uuid4().hex[:10]
    email = f"rate-limit-{unique}@example.com"
    responses = []

    for _ in range(6):
        responses.append(
            httpx.post(
                f"{API}/auth/login",
                json={"email": email, "password": "wrong-password"},
            )
        )

    assert [response.status_code for response in responses[:5]] == [401] * 5
    assert responses[5].status_code == 429

    token = _login(
        "admin@example.com",
        os.environ.get("SUPER_ADMIN_PASSWORD", "changeme123"),
    )
    audit_response = httpx.get(
        f"{API}/admin/audit-events?action=auth.login.rate_limited&outcome=denied&page_size=100",
        headers=_auth(token),
    )

    assert audit_response.status_code == 200, audit_response.text
    rate_limited_events = [
        event
        for event in audit_response.json()["items"]
        if event["details"].get("email_fingerprint")
        == email_fingerprint(email)
    ]
    assert len(rate_limited_events) == 1
    assert rate_limited_events[0]["ip_address"]
    assert rate_limited_events[0]["details"]["reason"] == "rate_limit"


def test_security_audit_events_record_login_without_storing_email() -> None:
    token = _login("admin@example.com", os.environ.get("SUPER_ADMIN_PASSWORD", "changeme123"))

    response = httpx.get(
        f"{API}/admin/audit-events?action=auth.login&outcome=success&page_size=200",
        headers=_auth(token),
    )

    assert response.status_code == 200, response.text
    events = response.json()["items"]
    assert any(
        event["outcome"] == "success" and event["actor_id"]
        for event in events
    )
    assert all("email" not in event["details"] for event in events)
    assert all("email_fingerprint" in event["details"] for event in events)


def test_anomaly_scan_is_super_admin_triggered_and_audited() -> None:
    super_token = _login(
        "admin@example.com", os.environ.get("SUPER_ADMIN_PASSWORD", "changeme123")
    )

    accepted = httpx.post(
        f"{API}/admin/ops/anomaly-scan",
        headers=_auth(super_token),
    )

    assert accepted.status_code == 202, accepted.text
    task_id = accepted.json()["task_id"]
    final_status = None
    for _ in range(30):
        final_status = httpx.get(
            f"{API}/admin/audit-exports/tasks/{task_id}",
            headers=_auth(super_token),
        )
        assert final_status.status_code == 200, final_status.text
        if final_status.json()["ready"]:
            break
        time.sleep(0.2)

    assert final_status is not None and final_status.json()["ready"]
    assert final_status.json()["successful"], final_status.text
    audit_events = httpx.get(
        f"{API}/admin/audit-events?action=audit.anomaly_scan.triggered&page_size=1",
        headers=_auth(super_token),
    )
    assert audit_events.status_code == 200, audit_events.text
    assert audit_events.json()["items"][0]["actor_id"]


@pytest.fixture
def schedule_context() -> Iterator[ScheduleContext]:
    super_token = _login("admin@example.com", os.environ.get("SUPER_ADMIN_PASSWORD", "changeme123"))
    unique = uuid.uuid4().hex[:8]
    context: ScheduleContext = {}

    try:
        for key in ("a", "b"):
            created = httpx.post(
                f"{API}/tenants",
                headers=_auth(super_token),
                json={"name": f"Security {unique} {key}", "slug": f"sec-{unique}-{key}"},
            )
            assert created.status_code == 201, created.text
            tenant_id = created.json()["id"]
            email = f"admin-sec-{unique}-{key}@example.com"
            admin = httpx.post(
                f"{API}/tenants/{tenant_id}/admin",
                headers=_auth(super_token),
                json={
                    "email": email,
                    "password": "password123",
                    "first_name": "Security",
                    "last_name": key.upper(),
                },
            )
            assert admin.status_code == 201, admin.text
            subscription = httpx.put(
                f"{API}/subscriptions/{tenant_id}",
                headers=_auth(super_token),
                json={"plan": "pro"},
            )
            assert subscription.status_code == 200, subscription.text
            context[key] = {
                "tenant_id": tenant_id,
                "token": _login(email, "password123"),
            }

        generated = httpx.post(
            f"{API}/schedules/generate",
            headers=_auth(context["b"]["token"]),
            json={"period_start": "2026-11-01", "period_days": 1, "nurse_ids": None},
        )
        assert generated.status_code == 201, generated.text
        request_id = generated.json()["id"]
        context["request_id"] = request_id
        yield context
    finally:
        if "b" in context:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                detail = httpx.get(
                    f"{API}/schedules/{context['request_id']}",
                    headers=_auth(super_token),
                )
                if detail.status_code != 200 or detail.json()["status"] != "pending":
                    break
                time.sleep(0.2)
        for item in context.values():
            if isinstance(item, dict) and "tenant_id" in item:
                httpx.delete(
                    f"{API}/tenants/{item['tenant_id']}", headers=_auth(super_token)
                )


def test_tenant_admin_cannot_access_admin_controls(
    schedule_context: ScheduleContext,
) -> None:
    denied = httpx.get(
        f"{API}/admin/audit-events",
        headers=_auth(schedule_context["b"]["token"]),
    )
    assert denied.status_code == 403
    denied = httpx.post(
        f"{API}/admin/ops/anomaly-scan",
        headers=_auth(schedule_context["b"]["token"]),
    )
    assert denied.status_code == 403
