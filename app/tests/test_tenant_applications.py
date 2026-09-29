"""End-to-end coverage for the reviewed tenant acquisition flow."""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pyotp
import pytest
import redis
from httpx import AsyncClient

from app.core.config import settings
from app.tests.conftest import SUPER_ADMIN_TEST_MFA_SECRET

BASE = os.environ.get("API_BASE_URL", "http://localhost:8000")
SUPER_EMAIL = os.environ.get("SUPER_ADMIN_EMAIL", "admin@example.com")
SUPER_PASSWORD = os.environ.get("SUPER_ADMIN_PASSWORD", "changeme123")


def _email_log() -> str:
    api_log = Path("api.log")
    if api_log.exists():
        return api_log.read_text(errors="replace")
    result = subprocess.run(
        ["docker", "compose", "logs", "--tail", "1000", "api"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout + result.stderr


async def _email_token(email: str, page: str) -> str:
    token_pattern = re.compile(rf"/pages/{page}\?token=([A-Za-z0-9_-]+)")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        for line in reversed(_email_log().splitlines()):
            if email not in line:
                continue
            match = token_pattern.search(line)
            if match:
                return match.group(1)
        await asyncio.sleep(0.25)
    raise AssertionError(f"No {page} email token found for {email}")


def _started_at() -> int:
    return int(time.time()) - 10


@pytest.fixture(scope="module", autouse=True)
def _reset_tenant_application_rate_limits() -> Iterator[None]:
    with redis.Redis.from_url(settings.REDIS_URL, decode_responses=True) as client:
        keys = list(client.scan_iter(match="tenant_application:*"))
        if keys:
            client.delete(*keys)
        yield
        keys = list(client.scan_iter(match="tenant_application:*"))
        if keys:
            client.delete(*keys)


@pytest.fixture(scope="module")
def super_token() -> Iterator[str]:
    response = httpx.post(
        f"{BASE}/api/v1/auth/login",
        json={
            "email": SUPER_EMAIL,
            "password": SUPER_PASSWORD,
            "totp_code": pyotp.TOTP(SUPER_ADMIN_TEST_MFA_SECRET).now(),
        },
    )
    assert response.status_code == 200, response.text
    token = response.json()["access_token"]
    auth = {"Authorization": f"Bearer {token}"}
    original = httpx.get(f"{BASE}/api/v1/admin/auto-approve", headers=auth).json()
    updated = httpx.put(
        f"{BASE}/api/v1/admin/auto-approve",
        headers=auth,
        json={"auto_approve": False, "auto_plan": original["auto_plan"]},
    )
    assert updated.status_code == 200, updated.text
    try:
        yield token
    finally:
        restored = httpx.put(
            f"{BASE}/api/v1/admin/auto-approve",
            headers=auth,
            json={
                "auto_approve": original["auto_approve"],
                "auto_plan": original["auto_plan"],
            },
        )
        assert restored.status_code == 200, restored.text


async def test_tenant_application_to_active_tenant_admin(
    client: AsyncClient,
    super_token: str,
) -> None:
    email = f"tenant-e2e-{uuid.uuid4().hex[:12]}@example.com"
    created_tenant_id: str | None = None
    application = {
        "organization_name": "Application E2E Clinic",
        "contact_name": "Application Contact",
        "contact_email": email,
        "country": "China",
        "timezone": "Asia/Shanghai",
        "expected_nurse_count": 12,
        "use_case_summary": "An end-to-end reviewed prospective tenant application.",
        "honeypot": "",
        "form_started_at": _started_at(),
    }

    page = await client.get("/pages/apply")
    assert page.status_code == 200
    assert "申请使用" in page.text

    submitted = await client.post("/api/v1/tenant-applications", json=application)
    assert submitted.status_code == 202, submitted.text
    assert submitted.json() == {
        "message": "If the information is valid, a verification email has been sent."
    }

    duplicate = await client.post("/api/v1/tenant-applications", json=application)
    assert duplicate.status_code == 202
    assert duplicate.json() == submitted.json()

    unauthenticated = await client.get("/api/v1/tenant-applications")
    assert unauthenticated.status_code == 401

    verification_token = await _email_token(email, "verify")
    verified = await client.post(
        "/api/v1/tenant-applications/verify", json={"token": verification_token}
    )
    assert verified.status_code == 200, verified.text
    reused = await client.post(
        "/api/v1/tenant-applications/verify", json={"token": verification_token}
    )
    assert reused.status_code == 400

    listed = await client.get(
        "/api/v1/tenant-applications",
        params={"status": "pending_review", "page": 1, "page_size": 50},
        headers={"Authorization": f"Bearer {super_token}"},
    )
    assert listed.status_code == 200, listed.text
    row = next(item for item in listed.json()["items"] if item["contact_email"] == email)

    try:
        approved = await client.post(
            f"/api/v1/tenant-applications/{row['id']}/approve",
            json={"review_notes": "automated e2e approval"},
            headers={"Authorization": f"Bearer {super_token}"},
        )
        assert approved.status_code == 200, approved.text
        decision = approved.json()
        created_tenant_id = decision["application"]["created_tenant_id"]
        assert decision["email_sent"] is True
        assert decision["application"]["status"] == "approved"
        assert created_tenant_id
        assert decision["application"]["created_user_id"]

        # The not-yet-activated admin must be visible in the impersonation
        # picker (regression: is_active=False users were filtered out).
        picker = await client.get(
            f"/api/v1/admin/tenants/{created_tenant_id}/users",
            headers={"Authorization": f"Bearer {super_token}"},
        )
        assert picker.status_code == 200, picker.text
        provisioned = next(
            u for u in picker.json()
            if u["id"] == decision["application"]["created_user_id"]
        )
        assert provisioned["is_active"] is False

        setup_token = await _email_token(email, "setup")
        activated = await client.post(
            "/api/v1/tenant-applications/setup",
            json={"token": setup_token, "new_password": "Tenant-E2E-Password-123"},
        )
        assert activated.status_code == 200, activated.text
        reused_setup = await client.post(
            "/api/v1/tenant-applications/setup",
            json={"token": setup_token, "new_password": "Tenant-E2E-Password-123"},
        )
        assert reused_setup.status_code == 400

        login = await client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": "Tenant-E2E-Password-123"},
        )
        assert login.status_code == 200, login.text
        tenant_admin_token = login.json()["access_token"]

        forbidden = await client.get(
            "/api/v1/tenant-applications",
            headers={"Authorization": f"Bearer {tenant_admin_token}"},
        )
        assert forbidden.status_code == 403

        subscription = await client.get(
            "/api/v1/subscriptions/me",
            headers={"Authorization": f"Bearer {tenant_admin_token}"},
        )
        assert subscription.status_code == 200, subscription.text
        assert subscription.json()["plan"] == "pro"
        assert subscription.json()["is_active"] is True
    finally:
        if created_tenant_id:
            deleted = await client.delete(
                f"/api/v1/tenants/{created_tenant_id}",
                headers={"Authorization": f"Bearer {super_token}"},
            )
            assert deleted.status_code == 204, deleted.text


async def test_tenant_application_rejection_does_not_provision_tenant(
    client: AsyncClient,
    super_token: str,
) -> None:
    email = f"tenant-reject-{uuid.uuid4().hex[:12]}@example.com"
    auth = {"Authorization": f"Bearer {super_token}"}
    tenants_before = (
        await client.get("/api/v1/tenants", params={"page": 1, "page_size": 1}, headers=auth)
    ).json()["total"]

    submitted = await client.post(
        "/api/v1/tenant-applications",
        json={
            "organization_name": "Application Reject Clinic",
            "contact_name": "Rejected Contact",
            "contact_email": email,
            "country": "China",
            "timezone": "Asia/Shanghai",
            "expected_nurse_count": 8,
            "use_case_summary": "A rejection path that must not provision a tenant.",
            "honeypot": "",
            "form_started_at": _started_at(),
        },
    )
    assert submitted.status_code == 202, submitted.text

    verification_token = await _email_token(email, "verify")
    verified = await client.post(
        "/api/v1/tenant-applications/verify", json={"token": verification_token}
    )
    assert verified.status_code == 200, verified.text

    listed = await client.get(
        "/api/v1/tenant-applications",
        params={"status": "pending_review", "page": 1, "page_size": 50},
        headers=auth,
    )
    assert listed.status_code == 200, listed.text
    row = next(item for item in listed.json()["items"] if item["contact_email"] == email)

    rejected = await client.post(
        f"/api/v1/tenant-applications/{row['id']}/reject",
        json={"reason": "Not enough scheduling context", "review_notes": "automated"},
        headers=auth,
    )
    assert rejected.status_code == 200, rejected.text
    decision = rejected.json()
    assert decision["email_sent"] is True
    assert decision["application"]["status"] == "rejected"
    assert decision["application"]["created_tenant_id"] is None
    assert decision["application"]["created_user_id"] is None

    repeat = await client.post(
        f"/api/v1/tenant-applications/{row['id']}/reject",
        json={"reason": "Second decision must fail", "review_notes": None},
        headers=auth,
    )
    assert repeat.status_code == 409

    tenants_after = (
        await client.get("/api/v1/tenants", params={"page": 1, "page_size": 1}, headers=auth)
    ).json()["total"]
    assert tenants_after == tenants_before


async def test_auto_approve_provisions_free_tenant_on_verification(
    client: AsyncClient,
    super_token: str,
) -> None:
    """E2E: auto-approve + free plan provisions immediately on verify.

    Requires TENANT_APPLICATION_AUTO_APPROVE=true on the live server.
    """
    import os

    import pytest

    if os.environ.get("TENANT_APPLICATION_AUTO_APPROVE", "").lower() != "true":
        pytest.skip("Set TENANT_APPLICATION_AUTO_APPROVE=true to run this test")

    email = f"auto-approve-{uuid.uuid4().hex[:12]}@example.com"

    submitted = await client.post(
        "/api/v1/tenant-applications",
        json={
            "organization_name": "Auto Approve Clinic",
            "contact_name": "Auto Contact",
            "contact_email": email,
            "country": "China",
            "timezone": "Asia/Shanghai",
            "expected_nurse_count": 5,
            "use_case_summary": "Auto-approved free tier test.",
            "honeypot": "",
            "form_started_at": _started_at(),
        },
    )
    assert submitted.status_code == 202, submitted.text

    token = await _email_token(email, "verify")
    verified = await client.post(
        "/api/v1/tenant-applications/verify", json={"token": token}
    )
    assert verified.status_code == 200, verified.text
    assert "provisioned" in verified.json()["message"].lower()

    setup_token = await _email_token(email, "setup")
    activated = await client.post(
        "/api/v1/tenant-applications/setup",
        json={"token": setup_token, "new_password": "Auto-Approve-Password-123"},
    )
    assert activated.status_code == 200, activated.text

    login = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "Auto-Approve-Password-123"},
    )
    assert login.status_code == 200, login.text
    admin_token = login.json()["access_token"]

    sub = await client.get(
        "/api/v1/subscriptions/me",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert sub.status_code == 200, sub.text
    assert sub.json()["plan"] == "free"
