"""Regression coverage for configurable subscription plans."""

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
SUPER_EMAIL = "admin@example.com"
SUPER_PASSWORD = os.environ.get("SUPER_ADMIN_PASSWORD", "changeme123")
ADMIN_PASSWORD = "password123"
CUSTOM_KEY = f"unit{int(time.time()) % 100000}"
CUSTOM_LIMITS = {"max_nurses": 3, "max_period_days": 5}
JSONDict = dict[str, object]


@pytest.fixture(scope="module")
def super_token() -> str:
    response = httpx.post(
        f"{BASE}/api/v1/auth/login",
        json={
            "email": SUPER_EMAIL,
            "password": SUPER_PASSWORD,
            "totp_code": pyotp.TOTP(SUPER_ADMIN_TEST_MFA_SECRET).now(),
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert isinstance(payload["access_token"], str)
    return payload["access_token"]


@pytest.fixture(scope="module")
def tenant_and_admin(super_token: str) -> Iterator[tuple[str, str]]:
    slug = f"plan-limits-{time.time_ns()}"
    response = httpx.post(
        f"{BASE}/api/v1/tenants",
        headers={"Authorization": f"Bearer {super_token}"},
        json={"name": f"Plan Limits {slug}", "slug": slug},
    )
    assert response.status_code == 201, response.text
    tenant_id = response.json()["id"]
    admin_email = f"admin@{slug}.com"
    response = httpx.post(
        f"{BASE}/api/v1/tenants/{tenant_id}/admin",
        headers={"Authorization": f"Bearer {super_token}"},
        json={
            "email": admin_email,
            "password": ADMIN_PASSWORD,
            "first_name": "Plan",
            "last_name": "Limits",
            "role": "tenant_admin",
        },
    )
    assert response.status_code == 201, response.text
    response = httpx.post(
        f"{BASE}/api/v1/auth/login",
        json={"email": admin_email, "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 200, response.text
    admin_token = response.json()["access_token"]
    assert isinstance(admin_token, str)
    yield tenant_id, admin_token
    deleted = httpx.delete(
        f"{BASE}/api/v1/tenants/{tenant_id}",
        headers={"Authorization": f"Bearer {super_token}"},
    )
    assert deleted.status_code == 204, deleted.text


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _json_headers(token: str) -> dict[str, str]:
    return {**_headers(token), "Content-Type": "application/json"}


def _original_plan_limits(super_token: str) -> JSONDict:
    response = httpx.get(
        f"{BASE}/api/v1/admin/plan-limits", headers=_headers(super_token)
    )
    assert response.status_code == 200, response.text
    return cast(JSONDict, response.json())


def _restore_plan_limits(super_token: str, original: JSONDict) -> None:
    response = httpx.put(
        f"{BASE}/api/v1/admin/plan-limits",
        headers=_json_headers(super_token),
        json=original,
    )
    assert response.status_code == 200, response.text


def _delete_stale_test_tenants(super_token: str, protected_tenant_id: str) -> None:
    listed = httpx.get(f"{BASE}/api/v1/admin/test-tenant", headers=_headers(super_token))
    assert listed.status_code == 200, listed.text
    for item in listed.json():
        if item["tenant_id"] == protected_tenant_id:
            continue
        if item["tenant_kind"] == "demo":
            continue
        deleted = httpx.delete(
            f"{BASE}/api/v1/admin/test-tenant/{item['tenant_id']}",
            headers=_headers(super_token),
        )
        if deleted.status_code not in {204, 404}:
            raise AssertionError(
                f"failed to clean stale test tenant {item['tenant_id']}: {deleted.text}"
        )


def _pro_subscribers(super_token: str) -> list[tuple[str, str]]:
    response = httpx.get(f"{BASE}/api/v1/subscriptions", headers=_headers(super_token))
    assert response.status_code == 200, response.text
    return [
        (cast(str, item["tenant_id"]), cast(str, item["plan"]))
        for item in response.json()
        if item["plan"] == "pro"
    ]


def test_plan_limits_require_super_admin(tenant_and_admin: tuple[str, str]) -> None:
    _tenant_id, admin_token = tenant_and_admin
    response = httpx.get(f"{BASE}/api/v1/admin/plan-limits", headers=_headers(admin_token))
    assert response.status_code == 403, response.text


def test_plan_limits_reject_invalid_values(super_token: str) -> None:
    response = httpx.put(
        f"{BASE}/api/v1/admin/plan-limits",
        headers=_json_headers(super_token),
        json={
            "free": {"max_nurses": 0, "max_period_days": 0},
            "pro": {"max_nurses": 20, "max_period_days": 30},
            "max": {"max_nurses": None, "max_period_days": None},
            "demo": {"max_nurses": 10, "max_period_days": 14},
        },
    )
    assert response.status_code == 422, response.text


def test_custom_plan_conflicts_are_rejected(super_token: str) -> None:
    original = _original_plan_limits(super_token)
    conflict: JSONDict = {
        **original,
        "custom": [{
            "key": "free",
            "name": "Invalid",
            "max_nurses": 1,
            "max_period_days": 1,
        }],
    }
    response = httpx.put(
        f"{BASE}/api/v1/admin/plan-limits",
        headers=_json_headers(super_token),
        json=conflict,
    )
    assert response.status_code == 422, response.text


def test_custom_plan_is_usable(super_token: str, tenant_and_admin: tuple[str, str]) -> None:
    tenant_id, admin_token = tenant_and_admin
    original = _original_plan_limits(super_token)
    try:
        custom: JSONDict = {
            "key": CUSTOM_KEY,
            "name": "Unit Plan",
            **CUSTOM_LIMITS,
        }
        configured = {**original, "custom": [custom]}
        response = httpx.put(
            f"{BASE}/api/v1/admin/plan-limits",
            headers=_json_headers(super_token),
            json=configured,
        )
        assert response.status_code == 200, response.text
        assert response.json()["custom"] == [custom]

        listed = httpx.get(
            f"{BASE}/api/v1/admin/plan-limits", headers=_headers(super_token)
        )
        assert listed.status_code == 200, listed.text
        assert [item["key"] for item in listed.json()["custom"]] == [CUSTOM_KEY]

        subscription = httpx.put(
            f"{BASE}/api/v1/subscriptions/{tenant_id}",
            headers=_json_headers(super_token),
            json={"plan": CUSTOM_KEY, "seat_packs": 1},
        )
        assert subscription.status_code == 200, subscription.text
        assert subscription.json()["plan"] == CUSTOM_KEY
        assert subscription.json()["seat_packs"] == 1

        limits = httpx.get(
            f"{BASE}/api/v1/subscriptions/me/limits", headers=_headers(admin_token)
        )
        assert limits.status_code == 200, limits.text
        assert limits.json() == {**CUSTOM_LIMITS, "max_nurses": 13}

        allowed = httpx.post(
            f"{BASE}/api/v1/schedules/generate",
            headers=_json_headers(admin_token),
            json={
                "period_start": "2026-11-01",
                "period_days": 5,
                "solver_config": {"timeout_seconds": 10, "num_workers": 4},
            },
        )
        assert allowed.status_code == 201, allowed.text

        blocked = httpx.post(
            f"{BASE}/api/v1/schedules/generate",
            headers=_json_headers(admin_token),
            json={
                "period_start": "2026-12-01",
                "period_days": 6,
                "solver_config": {"timeout_seconds": 10, "num_workers": 4},
            },
        )
        assert blocked.status_code == 402, blocked.text
        assert "5" in blocked.json()["detail"], blocked.text
    finally:
        subscription = httpx.put(
            f"{BASE}/api/v1/subscriptions/{tenant_id}",
            headers=_json_headers(super_token),
            json={"plan": "free"},
        )
        assert subscription.status_code == 200, subscription.text
        _restore_plan_limits(super_token, original)


def test_used_custom_plan_cannot_be_deleted(
    super_token: str,
    tenant_and_admin: tuple[str, str],
) -> None:
    tenant_id, _admin_token = tenant_and_admin
    original = _original_plan_limits(super_token)
    try:
        custom: JSONDict = {
            "key": f"kept{int(time.time()) % 100000}",
            "name": "Kept Plan",
            "max_nurses": 2,
            "max_period_days": 3,
        }
        response = httpx.put(
            f"{BASE}/api/v1/admin/plan-limits",
            headers=_json_headers(super_token),
            json={**original, "custom": [custom]},
        )
        assert response.status_code == 200, response.text
        subscription = httpx.put(
            f"{BASE}/api/v1/subscriptions/{tenant_id}",
            headers=_json_headers(super_token),
            json={"plan": custom["key"]},
        )
        assert subscription.status_code == 200, subscription.text

        response = httpx.put(
            f"{BASE}/api/v1/admin/plan-limits",
            headers=_json_headers(super_token),
            json=original,
        )
        assert response.status_code == 409, response.text
    finally:
        subscription = httpx.put(
            f"{BASE}/api/v1/subscriptions/{tenant_id}",
            headers=_json_headers(super_token),
            json={"plan": "free"},
        )
        assert subscription.status_code == 200, subscription.text
        _restore_plan_limits(super_token, original)


def test_unused_system_plan_can_be_deleted(
    super_token: str,
    tenant_and_admin: tuple[str, str],
) -> None:
    tenant_id, _admin_token = tenant_and_admin
    _delete_stale_test_tenants(super_token, protected_tenant_id=tenant_id)
    pro_tenants = _pro_subscribers(super_token)
    for subscriber_id, _plan in pro_tenants:
        response = httpx.put(
            f"{BASE}/api/v1/subscriptions/{subscriber_id}",
            headers=_json_headers(super_token),
            json={"plan": "max"},
        )
        assert response.status_code == 200, response.text
    original = _original_plan_limits(super_token)
    original_subscription = httpx.get(
        f"{BASE}/api/v1/subscriptions/{tenant_id}",
        headers=_json_headers(super_token),
    )
    original_plan = (original_subscription.json() or {}).get("plan") or "free"
    try:
        subscription = httpx.put(
            f"{BASE}/api/v1/subscriptions/{tenant_id}",
            headers=_json_headers(super_token),
            json={"plan": "pro"},
        )
        assert subscription.status_code == 200, subscription.text

        configured: JSONDict = {
            key: value
            for key, value in original.items()
            if key != "pro"
        }
        configured["deleted_system"] = ["pro"]
        response = httpx.put(
            f"{BASE}/api/v1/admin/plan-limits",
            headers=_json_headers(super_token),
            json=configured,
        )
        assert response.status_code == 409, response.text

        subscription = httpx.put(
            f"{BASE}/api/v1/subscriptions/{tenant_id}",
            headers=_json_headers(super_token),
            json={"plan": "max"},
        )
        assert subscription.status_code == 200, subscription.text

        response = httpx.put(
            f"{BASE}/api/v1/admin/plan-limits",
            headers=_json_headers(super_token),
            json=configured,
        )
        assert response.status_code == 200, response.text
        assert response.json()["deleted_system"] == ["pro"]

        listed = httpx.get(
            f"{BASE}/api/v1/admin/plan-limits", headers=_headers(super_token)
        )
        assert listed.status_code == 200, listed.text
        assert listed.json().get("pro") is None
        assert listed.json()["deleted_system"] == ["pro"]

        subscription = httpx.put(
            f"{BASE}/api/v1/subscriptions/{tenant_id}",
            headers=_json_headers(super_token),
            json={"plan": "pro"},
        )
        assert subscription.status_code == 422, subscription.text
    finally:
        subscription = httpx.put(
            f"{BASE}/api/v1/subscriptions/{tenant_id}",
            headers=_json_headers(super_token),
            json={"plan": original_plan},
        )
        assert subscription.status_code == 200, subscription.text
        _restore_plan_limits(super_token, original)
        for subscriber_id, plan in pro_tenants:
            response = httpx.put(
                f"{BASE}/api/v1/subscriptions/{subscriber_id}",
                headers=_json_headers(super_token),
                json={"plan": plan},
            )
            assert response.status_code == 200, response.text
