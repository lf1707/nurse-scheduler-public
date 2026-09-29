"""Regression coverage for user deletion by tenant admins and super admins."""

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
NURSE_PASSWORD = "Nurse-User-2026"
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


def _track_tenant(tenant_id: str) -> None:
    _created_tenant_ids.append(tenant_id)


def _unique_slug() -> str:
    return f"user-del-{time.time_ns()}"


def _create_tenant_with_admin(
    super_token: str, slug: str
) -> tuple[str, str, str]:
    """Create a tenant and return (tenant_id, admin_id, admin_token)."""
    admin_email = f"admin@{slug}.example.com"
    response = httpx.post(
        f"{BASE}/api/v1/tenants/with-admin",
        headers={**_headers(super_token), "Content-Type": "application/json"},
        json={
            "name": f"User Delete {slug}",
            "slug": slug,
            "admin": {
                "email": admin_email,
                "password": ADMIN_PASSWORD,
                "first_name": "Delete",
                "last_name": "Test",
            },
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    _track_tenant(body["tenant"]["id"])
    login = httpx.post(
        f"{BASE}/api/v1/auth/login",
        json={"email": admin_email, "password": ADMIN_PASSWORD},
    )
    assert login.status_code == 200, login.text
    return body["tenant"]["id"], body["admin"]["id"], login.json()["access_token"]


def _create_nurse_user(
    admin_token: str, tenant_id: str, slug: str
) -> dict[str, object]:
    response = httpx.post(
        f"{BASE}/api/v1/auth/users?tenant_id={tenant_id}",
        headers={**_headers(admin_token), "Content-Type": "application/json"},
        json={
            "email": f"nurse-{slug}@example.com",
            "password": NURSE_PASSWORD,
            "first_name": "Test",
            "last_name": "Nurse",
            "role": "nurse",
        },
    )
    assert response.status_code == 201, response.text
    return cast(dict[str, object], response.json())


def test_super_admin_deletes_nurse(super_token: str) -> None:
    slug = _unique_slug()
    tenant_id, _admin_id, _admin_token = _create_tenant_with_admin(
        super_token, slug
    )
    nurse = _create_nurse_user(super_token, tenant_id, slug)
    nurse_id = cast(str, nurse["id"])

    response = httpx.delete(
        f"{BASE}/api/v1/auth/users/{nurse_id}?tenant_id={tenant_id}",
        headers=_headers(super_token),
    )
    assert response.status_code == 204, response.text

    users = httpx.get(
        f"{BASE}/api/v1/auth/users?tenant_id={tenant_id}",
        headers=_headers(super_token),
    )
    assert users.status_code == 200
    assert nurse_id not in {u["id"] for u in users.json()}


def test_tenant_admin_deletes_nurse(super_token: str) -> None:
    slug = _unique_slug()
    tenant_id, _admin_id, admin_token = _create_tenant_with_admin(
        super_token, slug
    )
    nurse = _create_nurse_user(admin_token, tenant_id, slug)
    nurse_id = cast(str, nurse["id"])

    response = httpx.delete(
        f"{BASE}/api/v1/auth/users/{nurse_id}",
        headers=_headers(admin_token),
    )
    assert response.status_code == 204, response.text


def test_cannot_delete_self(super_token: str) -> None:
    slug = _unique_slug()
    tenant_id, _admin_id, admin_token = _create_tenant_with_admin(
        super_token, slug
    )
    me = httpx.get(f"{BASE}/api/v1/auth/me", headers=_headers(admin_token))
    assert me.status_code == 200
    my_id = me.json()["id"]

    response = httpx.delete(
        f"{BASE}/api/v1/auth/users/{my_id}?tenant_id={tenant_id}",
        headers=_headers(admin_token),
    )
    assert response.status_code == 400
    assert "Cannot delete yourself" in response.text


def test_tenant_admin_cannot_delete_other_admin(super_token: str) -> None:
    slug = _unique_slug()
    tenant_id, _admin_id, admin_token = _create_tenant_with_admin(
        super_token, slug
    )
    second_slug = f"{slug}-second"
    second_email = f"admin@{second_slug}.example.com"
    create = httpx.post(
        f"{BASE}/api/v1/auth/users?tenant_id={tenant_id}",
        headers={**_headers(super_token), "Content-Type": "application/json"},
        json={
            "email": second_email,
            "password": ADMIN_PASSWORD,
            "first_name": "Second",
            "last_name": "Admin",
            "role": "tenant_admin",
        },
    )
    assert create.status_code == 201, create.text
    second_admin_id = create.json()["id"]

    response = httpx.delete(
        f"{BASE}/api/v1/auth/users/{second_admin_id}",
        headers=_headers(admin_token),
    )
    assert response.status_code == 403
    assert "other tenant admins" in response.text
