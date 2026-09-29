"""Regression coverage for atomic tenant and administrator provisioning."""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from typing import cast

import httpx
import pyotp
import pytest

from app.tests.conftest import SUPER_ADMIN_TEST_MFA_SECRET

BASE = os.environ.get("API_BASE_URL", "http://localhost:8000")
ADMIN_PASSWORD = "Tenant-Admin-2026"
_created_tenant_ids: list[str] = []


@pytest.fixture(scope="module")
def super_token() -> str:
    response = httpx.post(
        f"{BASE}/api/v1/auth/login",
        json={
            "email": "admin@example.com",
            "password": os.environ.get("SUPER_ADMIN_PASSWORD", "changeme123"),
            "totp_code": pyotp.TOTP(SUPER_ADMIN_TEST_MFA_SECRET).now(),
        },
    )
    assert response.status_code == 200, response.text
    return cast(str, response.json()["access_token"])


@pytest.fixture(scope="module", autouse=True)
def _cleanup_tenants(super_token: str) -> Iterator[None]:
    yield
    for tenant_id in _created_tenant_ids:
        httpx.delete(
            f"{BASE}/api/v1/tenants/{tenant_id}", headers=_headers(super_token)
        )
    _created_tenant_ids.clear()


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _unique_slug() -> str:
    return f"with-admin-{time.time_ns()}"


def _tenant_slugs(token: str) -> set[str]:
    response = httpx.get(
        f"{BASE}/api/v1/tenants", headers=_headers(token), params={"page_size": 100}
    )
    assert response.status_code == 200, response.text
    return {item["slug"] for item in response.json()["items"]}


def _track_tenant(tenant_id: str) -> None:
    _created_tenant_ids.append(tenant_id)


def test_create_tenant_admin_and_subscription_atomically(super_token: str) -> None:
    slug = _unique_slug()
    email = f"admin@{slug}.example.com"
    response = httpx.post(
        f"{BASE}/api/v1/tenants/with-admin",
        headers={**_headers(super_token), "Content-Type": "application/json"},
        json={
            "name": f"With Admin {slug}",
            "slug": slug,
            "admin": {
                "email": email,
                "password": ADMIN_PASSWORD,
                "first_name": "First",
                "last_name": "Admin",
            },
            "subscription": {
                "plan": "free",
                "subscription_type": "manual",
                "is_canceled": False,
            },
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    tenant_id = body["tenant"]["id"]
    _track_tenant(tenant_id)
    assert body["admin"]["email"] == email
    assert body["admin"]["role"] == "tenant_admin"
    assert body["subscription"]["plan"] == "free"

    admin_response = httpx.get(
        f"{BASE}/api/v1/tenants/{tenant_id}/admin", headers=_headers(super_token)
    )
    assert admin_response.status_code == 200, admin_response.text
    assert admin_response.json()["id"] == body["admin"]["id"]

    subscription_response = httpx.get(
        f"{BASE}/api/v1/subscriptions/{tenant_id}", headers=_headers(super_token)
    )
    assert subscription_response.status_code == 200, subscription_response.text
    assert subscription_response.json()["id"] == body["subscription"]["id"]

    login = httpx.post(
        f"{BASE}/api/v1/auth/login", json={"email": email, "password": ADMIN_PASSWORD}
    )
    assert login.status_code == 200, login.text

    deleted = httpx.delete(
        f"{BASE}/api/v1/tenants/{tenant_id}", headers=_headers(super_token)
    )
    assert deleted.status_code == 204, deleted.text


def test_duplicate_admin_email_does_not_create_tenant(super_token: str) -> None:
    email = f"conflict-{time.time_ns()}@example.com"
    first = httpx.post(
        f"{BASE}/api/v1/tenants/with-admin",
        headers={**_headers(super_token), "Content-Type": "application/json"},
        json={
            "name": "Conflict Seed",
            "slug": _unique_slug(),
            "admin": {
                "email": email,
                "password": ADMIN_PASSWORD,
                "first_name": "Conflict",
                "last_name": "Seed",
            },
        },
    )
    assert first.status_code == 201, first.text
    tenant_id = first.json()["tenant"]["id"]
    _track_tenant(tenant_id)

    slug = _unique_slug()
    conflict = httpx.post(
        f"{BASE}/api/v1/tenants/with-admin",
        headers={**_headers(super_token), "Content-Type": "application/json"},
        json={
            "name": "Conflict Tenant",
            "slug": slug,
            "admin": {
                "email": email,
                "password": ADMIN_PASSWORD,
                "first_name": "Conflict",
                "last_name": "Duplicate",
            },
        },
    )
    assert conflict.status_code == 409, conflict.text
    assert slug not in _tenant_slugs(super_token)

    deleted = httpx.delete(
        f"{BASE}/api/v1/tenants/{tenant_id}", headers=_headers(super_token)
    )
    assert deleted.status_code == 204, deleted.text


def test_invalid_subscription_does_not_create_tenant(super_token: str) -> None:
    slug = _unique_slug()
    response = httpx.post(
        f"{BASE}/api/v1/tenants/with-admin",
        headers={**_headers(super_token), "Content-Type": "application/json"},
        json={
            "name": "Invalid Subscription",
            "slug": slug,
            "admin": {
                "email": f"admin@{slug}.example.com",
                "password": ADMIN_PASSWORD,
                "first_name": "Invalid",
                "last_name": "Subscription",
            },
            "subscription": {"plan": "missing-plan"},
        },
    )
    assert response.status_code == 422, response.text
    assert slug not in _tenant_slugs(super_token)
