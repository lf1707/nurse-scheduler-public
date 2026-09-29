"""Subscription gating tests — schedule generation requires an active subscription.

Cases: no subscription → 403; active → passes gate (201); expired/canceled → 403.
Follows test_api_e2e.py's HTTP-only style (live API at API_BASE_URL).
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from typing import Any, cast

import httpx
import pyotp
import pytest

from app.core.config import settings
from app.tests.conftest import SUPER_ADMIN_TEST_MFA_SECRET

BASE = os.environ.get("API_BASE_URL", "http://localhost:8000")
SUPER_EMAIL = "admin@example.com"
SUPER_PASSWORD = os.environ.get("SUPER_ADMIN_PASSWORD", "changeme123")
ADMIN_PASSWORD = "password123"


@pytest.fixture(scope="module")
def super_token() -> str:
    r = httpx.post(
        f"{BASE}/api/v1/auth/login",
        json={
            "email": SUPER_EMAIL,
            "password": SUPER_PASSWORD,
            "totp_code": pyotp.TOTP(SUPER_ADMIN_TEST_MFA_SECRET).now(),
        },
    )
    assert r.status_code == 200, r.text
    return cast(str, r.json()["access_token"])


@pytest.fixture(scope="module")
def tenant_and_admin(super_token: str) -> Iterator[tuple[str, str]]:
    slug = f"e2e-sub-{int(time.time())}"
    r = httpx.post(
        f"{BASE}/api/v1/tenants",
        headers={"Authorization": f"Bearer {super_token}"},
        json={"name": f"Sub Test {slug}", "slug": slug},
    )
    assert r.status_code == 201, r.text
    tenant_id = r.json()["id"]
    admin_email = f"admin@{slug}.com"
    r = httpx.post(
        f"{BASE}/api/v1/tenants/{tenant_id}/admin",
        headers={"Authorization": f"Bearer {super_token}"},
        json={"email": admin_email, "password": ADMIN_PASSWORD, "first_name": "S", "last_name": "T",
              "role": "tenant_admin"},
    )
    assert r.status_code == 201, r.text
    r = httpx.post(f"{BASE}/api/v1/auth/login", json={"email": admin_email, "password": ADMIN_PASSWORD})
    assert r.status_code == 200, r.text
    yield tenant_id, r.json()["access_token"]
    deleted = httpx.delete(
        f"{BASE}/api/v1/tenants/{tenant_id}",
        headers={"Authorization": f"Bearer {super_token}"},
    )
    assert deleted.status_code == 204, deleted.text


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _generate(token: str) -> httpx.Response:
    return httpx.post(
        f"{BASE}/api/v1/schedules/generate",
        headers={**_h(token), "Content-Type": "application/json"},
        json={"period_start": "2026-11-01", "period_days": 7,
              "solver_config": {"timeout_seconds": 10, "num_workers": 4}},
    )


def _precheck(token: str, **payload: Any) -> httpx.Response:
    return httpx.post(
        f"{BASE}/api/v1/schedules/generate/precheck",
        headers={**_h(token), "Content-Type": "application/json"},
        json=payload,
    )


def _put_sub(super_token: str, tenant_id: str, **overrides: Any) -> dict[str, Any]:
    body = {"plan": "free", **overrides}
    r = httpx.put(
        f"{BASE}/api/v1/subscriptions/{tenant_id}",
        headers={**_h(super_token), "Content-Type": "application/json"},
        json=body,
    )
    assert r.status_code == 200, r.text
    return cast(dict[str, Any], r.json())


def _any_role_id(super_token: str, tenant_id: str) -> str:
    roles = httpx.get(
        f"{BASE}/api/v1/roles?tenant_id={tenant_id}",
        headers=_h(super_token),
    )
    assert roles.status_code == 200, roles.text
    items = roles.json()
    if isinstance(items, dict):
        items = items.get("items", [])
    for role in items:
        if role["code"] == "any":
            return cast(str, role["id"])
    raise AssertionError("built-in 'any' role not found")


def test_no_subscription_blocks_generate(tenant_and_admin: tuple[str, str]) -> None:
    _tenant_id, admin_token = tenant_and_admin
    r = _generate(admin_token)
    assert r.status_code == 403, r.text


def test_me_returns_null_without_subscription(
    tenant_and_admin: tuple[str, str],
) -> None:
    _tenant_id, admin_token = tenant_and_admin
    r = httpx.get(f"{BASE}/api/v1/subscriptions/me", headers=_h(admin_token))
    assert r.status_code == 200, r.text
    assert r.json() is None


def test_active_subscription_allows_generate(
    super_token: str,
    tenant_and_admin: tuple[str, str],
) -> None:
    tenant_id, admin_token = tenant_and_admin
    sub = _put_sub(super_token, tenant_id)
    assert sub["is_active"] is True

    r = httpx.get(f"{BASE}/api/v1/subscriptions/me", headers=_h(admin_token))
    assert r.status_code == 200 and r.json()["plan"] == "free"

    r = _generate(admin_token)
    assert r.status_code == 201, r.text


def test_precheck_blocks_when_no_nurses_or_scheduling_demand(
    super_token: str, tenant_and_admin: tuple[str, str]
) -> None:
    tenant_id, admin_token = tenant_and_admin
    _put_sub(super_token, tenant_id)
    r = _precheck(admin_token)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["nurse_count"] == 0
    assert body["demand_rule_count"] == 0
    assert body["contract_target_count"] == 0
    assert body["can_generate"] is False
    assert body["reasons"] == [
        "无可排班护士",
        "无启用的技能组合需求规则，且参与护士没有合同目标",
    ]


def test_precheck_blocks_invalid_selected_nurse_scope(
    super_token: str, tenant_and_admin: tuple[str, str]
) -> None:
    tenant_id, admin_token = tenant_and_admin
    _put_sub(super_token, tenant_id)
    r = _precheck(admin_token, nurse_ids=["00000000-0000-4000-8000-000000000000"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["nurse_count"] == 0
    assert body["can_generate"] is False
    assert body["reasons"] == [
        "无可排班护士",
        "无启用的技能组合需求规则，且参与护士没有合同目标",
    ]


def test_expired_subscription_blocks_generate(
    super_token: str,
    tenant_and_admin: tuple[str, str],
) -> None:
    tenant_id, admin_token = tenant_and_admin
    sub = _put_sub(super_token, tenant_id, ends_at="2020-01-01T00:00:00Z")
    assert sub["is_active"] is False
    r = _generate(admin_token)
    assert r.status_code == 403, r.text


def test_canceled_subscription_blocks_generate(
    super_token: str,
    tenant_and_admin: tuple[str, str],
) -> None:
    tenant_id, admin_token = tenant_and_admin
    sub = _put_sub(super_token, tenant_id, is_canceled=True)
    assert sub["is_active"] is False
    r = _generate(admin_token)
    assert r.status_code == 403, r.text


def test_free_plan_blocks_long_schedule(
    super_token: str,
    tenant_and_admin: tuple[str, str],
) -> None:
    tenant_id, admin_token = tenant_and_admin
    _put_sub(super_token, tenant_id, plan="free")
    r = httpx.post(
        f"{BASE}/api/v1/schedules/generate",
        headers={**_h(admin_token), "Content-Type": "application/json"},
        json={"period_start": "2026-11-01", "period_days": 15,
              "solver_config": {"timeout_seconds": 10, "num_workers": 4}},
    )
    assert r.status_code == 402, r.text
    assert "14" in r.json()["detail"], r.text


def test_free_plan_blocks_over_limit_nurse_ids(
    super_token: str,
    tenant_and_admin: tuple[str, str],
) -> None:
    tenant_id, admin_token = tenant_and_admin
    _put_sub(super_token, tenant_id, plan="free")
    fake_ids = [f"00000000-0000-4000-8000-{i:012d}" for i in range(9)]
    r = httpx.post(
        f"{BASE}/api/v1/schedules/generate",
        headers={**_h(admin_token), "Content-Type": "application/json"},
        json={"period_start": "2026-11-01", "period_days": 7,
              "nurse_ids": fake_ids,
              "solver_config": {"timeout_seconds": 10, "num_workers": 4}},
    )
    assert r.status_code == 402, r.text
    assert "8" in r.json()["detail"], r.text


def test_subscription_type_defaults_to_manual(
    super_token: str,
    tenant_and_admin: tuple[str, str],
) -> None:
    tenant_id, _admin_token = tenant_and_admin
    sub = _put_sub(super_token, tenant_id, plan="pro")
    assert sub["subscription_type"] == "manual"


def test_subscription_type_accepts_trial(
    super_token: str,
    tenant_and_admin: tuple[str, str],
) -> None:
    tenant_id, _admin_token = tenant_and_admin
    sub = _put_sub(super_token, tenant_id, plan="pro", subscription_type="trial")
    assert sub["subscription_type"] == "trial"


def test_subscription_type_rejects_unknown(
    super_token: str,
    tenant_and_admin: tuple[str, str],
) -> None:
    tenant_id, _admin_token = tenant_and_admin
    r = httpx.put(
        f"{BASE}/api/v1/subscriptions/{tenant_id}",
        headers={**_h(super_token), "Content-Type": "application/json"},
        json={"plan": "pro", "subscription_type": "stripe"},
    )
    assert r.status_code == 422, r.text


def test_pro_plan_caps_schedule_at_30_days(
    super_token: str,
    tenant_and_admin: tuple[str, str],
) -> None:
    tenant_id, admin_token = tenant_and_admin
    _put_sub(super_token, tenant_id, plan="pro")
    allowed = httpx.post(
        f"{BASE}/api/v1/schedules/generate",
        headers={**_h(admin_token), "Content-Type": "application/json"},
        json={"period_start": "2026-11-01", "period_days": 30,
              "solver_config": {"timeout_seconds": 10, "num_workers": 4}},
    )
    assert allowed.status_code == 201, allowed.text
    blocked = httpx.post(
        f"{BASE}/api/v1/schedules/generate",
        headers={**_h(admin_token), "Content-Type": "application/json"},
        json={"period_start": "2026-12-01", "period_days": 31,
              "solver_config": {"timeout_seconds": 10, "num_workers": 4}},
    )
    assert blocked.status_code == 402, blocked.text
    assert "30" in blocked.json()["detail"], blocked.text


def test_max_plan_allows_long_schedule(
    super_token: str,
    tenant_and_admin: tuple[str, str],
) -> None:
    tenant_id, admin_token = tenant_and_admin
    _put_sub(super_token, tenant_id, plan="max")
    r = httpx.post(
        f"{BASE}/api/v1/schedules/generate",
        headers={**_h(admin_token), "Content-Type": "application/json"},
        json={"period_start": "2026-11-01", "period_days": 31,
              "solver_config": {"timeout_seconds": 10, "num_workers": 4}},
    )
    assert r.status_code == 201, r.text


def test_free_plan_caps_nurse_creation(
    super_token: str,
    tenant_and_admin: tuple[str, str],
) -> None:
    tenant_id, admin_token = tenant_and_admin
    role_id = _any_role_id(super_token, tenant_id)
    _put_sub(super_token, tenant_id, plan="free")
    h = {**_h(admin_token), "Content-Type": "application/json"}
    _put_sub(super_token, tenant_id, plan="free")
    # Create up to the limit (8).
    for i in range(8):
        r = httpx.post(
            f"{BASE}/api/v1/nurses",
            headers=h,
        json={"employee_id": f"FREE-{i}", "first_name": "F", "last_name": str(i), "role_ids": [role_id]},
        )
        assert r.status_code == 201, r.text
    # The 7th nurse must be rejected.
    r = httpx.post(
        f"{BASE}/api/v1/nurses",
        headers=h,
        json={"employee_id": "FREE-8", "first_name": "F", "last_name": "8", "role_ids": [role_id]},
    )
    assert r.status_code == 402, r.text
    assert "8" in r.json()["detail"], r.text


def test_free_plan_blocks_generate_when_all_available_nurses_exceed_limit(
    super_token: str, tenant_and_admin: tuple[str, str]
) -> None:
    tenant_id, admin_token = tenant_and_admin
    role_id = _any_role_id(super_token, tenant_id)
    h = {**_h(admin_token), "Content-Type": "application/json"}
    # Start on Pro so we can create more than the Free cap.
    _put_sub(super_token, tenant_id, plan="pro")
    created_ids: list[str] = []
    for i in range(9):
        r = httpx.post(
            f"{BASE}/api/v1/nurses",
            headers=h,
            json={"employee_id": f"GEN-{i}", "first_name": "G", "last_name": str(i), "role_ids": [role_id]},
        )
        assert r.status_code == 201, r.text
        created_ids.append(r.json()["id"])
    # Downgrade and confirm generation is blocked with all available nurses.
    _put_sub(super_token, tenant_id, plan="free")

    # Generate without nurse_ids → all 9 available nurses → 402.
    r = _generate(admin_token)
    assert r.status_code == 402, r.text
    assert "8" in r.json()["detail"], r.text

    # Clean up so subsequent tests sharing this tenant see a fresh state.
    _put_sub(super_token, tenant_id, plan="pro")
    for nurse_id in created_ids:
        httpx.delete(
            f"{BASE}/api/v1/nurses/{nurse_id}",
            headers=h,
        )


def test_demo_plan_is_rejected_for_regular_tenant(
    super_token: str,
    tenant_and_admin: tuple[str, str],
) -> None:
    tenant_id, _admin_token = tenant_and_admin
    r = httpx.put(
        f"{BASE}/api/v1/subscriptions/{tenant_id}",
        headers={**_h(super_token), "Content-Type": "application/json"},
        json={"plan": "demo"},
    )
    assert r.status_code == 422, r.text


def test_profile_endpoints(tenant_and_admin: tuple[str, str]) -> None:
    _tenant_id, admin_token = tenant_and_admin
    # /me includes tenant_name
    r = httpx.get(f"{BASE}/api/v1/auth/me", headers=_h(admin_token))
    assert r.status_code == 200 and r.json()["tenant_name"], r.text
    # rename
    r = httpx.patch(
        f"{BASE}/api/v1/auth/me",
        headers={**_h(admin_token), "Content-Type": "application/json"},
        json={"first_name": "新", "last_name": "名字"},
    )
    assert r.status_code == 200 and r.json()["first_name"] == "新", r.text
    # password change: wrong old → 400, right old → 204
    r = httpx.post(
        f"{BASE}/api/v1/auth/me/password",
        headers={**_h(admin_token), "Content-Type": "application/json"},
        json={"old_password": "wrong-password", "new_password": "newpass12345"},
    )
    assert r.status_code == 400, r.text

    email = f"profile-revoke-{int(time.time())}@example.com"
    created = httpx.post(
        f"{BASE}/api/v1/auth/users",
        headers={**_h(admin_token), "Content-Type": "application/json"},
        json={
            "email": email,
            "password": ADMIN_PASSWORD,
            "first_name": "Profile",
            "last_name": "User",
            "role": "viewer",
        },
    )
    assert created.status_code == 201, created.text
    own_session = httpx.post(
        f"{BASE}/api/v1/auth/login",
        json={"email": email, "password": ADMIN_PASSWORD},
    ).json()

    r = httpx.post(
        f"{BASE}/api/v1/auth/me/password",
        headers={**_h(own_session["access_token"]), "Content-Type": "application/json"},
        json={"old_password": ADMIN_PASSWORD, "new_password": "newpass12345"},
    )
    assert r.status_code == 204, r.text
    assert (
        httpx.get(
            f"{BASE}/api/v1/auth/me", headers=_h(own_session["access_token"])
        ).status_code
        == 401
    )
    login = httpx.post(
        f"{BASE}/api/v1/auth/login",
        json={"email": email, "password": "newpass12345"},
    )
    assert login.status_code == 200, login.text


def test_super_admin_generate_requires_tenant_id(super_token: str) -> None:
    """Super admin without an explicit tenant_id → 400 (JWT tenant_id is null)."""
    r = httpx.post(
        f"{BASE}/api/v1/schedules/generate",
        headers={**_h(super_token), "Content-Type": "application/json"},
        json={"period_start": "2026-11-01", "period_days": 7,
              "solver_config": {"timeout_seconds": 10, "num_workers": 4}},
    )
    assert r.status_code == 400, r.text


def test_super_admin_generate_for_tenant_with_subscription(
    super_token: str, tenant_and_admin: tuple[str, str],
) -> None:
    """Super admin acting for a tenant with an active subscription → 201."""
    tenant_id, _admin_token = tenant_and_admin
    _put_sub(super_token, tenant_id)  # ensure active
    r = httpx.post(
        f"{BASE}/api/v1/schedules/generate",
        headers={**_h(super_token), "Content-Type": "application/json"},
        json={"period_start": "2026-11-01", "period_days": 7, "tenant_id": tenant_id,
              "solver_config": {"timeout_seconds": 10, "num_workers": 4}},
    )
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "pending"


def test_super_admin_generate_for_unsubscribed_tenant(
    super_token: str, tenant_and_admin: tuple[str, str],
) -> None:
    """Super admin acting for a tenant whose subscription is canceled → 403."""
    tenant_id, _admin_token = tenant_and_admin
    _put_sub(super_token, tenant_id, is_canceled=True)
    r = httpx.post(
        f"{BASE}/api/v1/schedules/generate",
        headers={**_h(super_token), "Content-Type": "application/json"},
        json={"period_start": "2026-11-01", "period_days": 7, "tenant_id": tenant_id,
              "solver_config": {"timeout_seconds": 10, "num_workers": 4}},
    )
    assert r.status_code == 403, r.text


def test_tenant_user_cannot_target_other_tenant(
    super_token: str, tenant_and_admin: tuple[str, str],
) -> None:
    """Tenant user supplying a foreign tenant_id → 403."""
    _tenant_id, admin_token = tenant_and_admin
    r = httpx.post(
        f"{BASE}/api/v1/schedules/generate",
        headers={**_h(admin_token), "Content-Type": "application/json"},
        json={"period_start": "2026-11-01", "period_days": 7,
              "tenant_id": "00000000-0000-0000-0000-000000000000",
              "solver_config": {"timeout_seconds": 10, "num_workers": 4}},
    )
    assert r.status_code == 403, r.text


# ──────────────────────────────────────────────────────────────
# Super-admin masquerade + test-tenant bootstrap/cleanup
# ──────────────────────────────────────────────────────────────
def test_test_tenant_toggle_requires_super_admin(
    tenant_and_admin: tuple[str, str],
) -> None:
    _tenant_id, admin_token = tenant_and_admin
    r = httpx.put(
        f"{BASE}/api/v1/admin/test-tenant/enabled",
        headers={**_h(admin_token), "Content-Type": "application/json"},
        json={"enabled": True},
    )
    assert r.status_code == 403, r.text


def test_super_admin_can_toggle_test_tenants(super_token: str) -> None:
    initial = httpx.get(
        f"{BASE}/api/v1/admin/test-tenant/enabled",
        headers=_h(super_token),
    )
    assert initial.status_code == 200, initial.text
    original = initial.json()["enabled"]

    changed = httpx.put(
        f"{BASE}/api/v1/admin/test-tenant/enabled",
        headers={**_h(super_token), "Content-Type": "application/json"},
        json={"enabled": not original},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["enabled"] is (not original)

    listed = httpx.get(f"{BASE}/api/v1/admin/test-tenant", headers=_h(super_token))
    expected = 200 if not original else 403
    assert listed.status_code == expected, listed.text

    restored = httpx.put(
        f"{BASE}/api/v1/admin/test-tenant/enabled",
        headers={**_h(super_token), "Content-Type": "application/json"},
        json={"enabled": original},
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["enabled"] is original


def test_demo_tenant_is_preserved_by_repair(super_token: str) -> None:
    initial = httpx.get(
        f"{BASE}/api/v1/admin/test-tenant/enabled",
        headers=_h(super_token),
    )
    assert initial.status_code == 200, initial.text
    assert initial.json()["demo_enabled"] is True
    original_enabled = initial.json()["enabled"]

    enabled = httpx.put(
        f"{BASE}/api/v1/admin/test-tenant/enabled",
        headers={**_h(super_token), "Content-Type": "application/json"},
        json={"enabled": True},
    )
    assert enabled.status_code == 200, enabled.text

    listed = httpx.get(f"{BASE}/api/v1/admin/test-tenant", headers=_h(super_token))
    assert listed.status_code == 200, listed.text
    demo = next((item for item in listed.json() if item["slug"] == "demo"), None)
    if not demo:
        created = httpx.post(
            f"{BASE}/api/v1/admin/test-tenant/demo",
            headers=_h(super_token),
        )
        assert created.status_code == 201, created.text
        seeded = httpx.get(f"{BASE}/api/v1/admin/test-tenant", headers=_h(super_token))
        assert seeded.status_code == 200, seeded.text
        demo = next(item for item in seeded.json() if item["slug"] == "demo")

    assert demo["tenant_kind"] == "demo"
    assert demo["admin_password"] is None
    assert demo["password_source"] == "environment_variable:DEMO_TENANT_PASSWORD"

    original_tenant_id = demo["tenant_id"]
    repaired = httpx.post(
        f"{BASE}/api/v1/admin/test-tenant/demo",
        headers=_h(super_token),
    )
    assert repaired.status_code == 201, repaired.text
    assert repaired.json()["tenant_id"] == original_tenant_id

    listed_again = httpx.get(f"{BASE}/api/v1/admin/test-tenant", headers=_h(super_token))
    assert listed_again.status_code == 200, listed_again.text
    record = next(item for item in listed_again.json() if item["slug"] == "demo")
    assert record["tenant_kind"] == "demo"
    assert record["nurse_count"] == demo["nurse_count"]

    restored = httpx.put(
        f"{BASE}/api/v1/admin/test-tenant/enabled",
        headers={**_h(super_token), "Content-Type": "application/json"},
        json={"enabled": original_enabled},
    )
    assert restored.status_code == 200, restored.text


def _ensure_demo_and_login(super_token: str) -> tuple[str, str]:
    """Ensure the demo tenant exists and return (tenant_id, admin_token)."""
    httpx.put(
        f"{BASE}/api/v1/admin/test-tenant/enabled",
        headers={**_h(super_token), "Content-Type": "application/json"},
        json={"enabled": True},
    )
    # This endpoint is idempotent and refreshes demo credentials to the
    # configured DEMO_TENANT_PASSWORD before the test signs in.
    created = httpx.post(
        f"{BASE}/api/v1/admin/test-tenant/demo",
        headers=_h(super_token),
    )
    assert created.status_code == 201, created.text
    demo = created.json()
    tenant_id = demo["tenant_id"]
    password = os.environ.get("DEMO_TENANT_PASSWORD", settings.DEMO_TENANT_PASSWORD)
    r = httpx.post(
        f"{BASE}/api/v1/auth/login",
        json={"email": "demo-admin@nurse-scheduler.dev", "password": password},
    )
    assert r.status_code == 200, r.text
    return tenant_id, r.json()["access_token"]


def test_demo_tenant_blocks_schedule_over_14_days(super_token: str) -> None:
    """Demo tenant is capped at 14 scheduling days on its locked plan."""
    tenant_id, admin_token = _ensure_demo_and_login(super_token)
    sub = httpx.get(
        f"{BASE}/api/v1/subscriptions/{tenant_id}",
        headers=_h(super_token),
    )
    assert sub.status_code == 200, sub.text
    assert sub.json()["plan"] == "demo"
    r = httpx.post(
        f"{BASE}/api/v1/schedules/generate",
        headers={**_h(admin_token), "Content-Type": "application/json"},
        json={"period_start": "2026-11-01", "period_days": 15,
              "solver_config": {"timeout_seconds": 10, "num_workers": 4}},
    )
    assert r.status_code == 402, r.text
    assert "14" in r.json()["detail"], r.text


def test_demo_tenant_blocks_11th_nurse(super_token: str) -> None:
    """Demo tenant is capped at 10 nurses on its locked plan."""
    tenant_id, admin_token = _ensure_demo_and_login(super_token)
    role = httpx.post(
        f"{BASE}/api/v1/roles",
        headers={**_h(admin_token), "Content-Type": "application/json"},
        json={"name": "Demo Nurse", "code": "demo-nurse"},
    )
    role_id = role.json()["id"] if role.status_code == 201 else role.json().get("id", "")
    listed = httpx.get(f"{BASE}/api/v1/nurses?page_size=200", headers=_h(admin_token))
    assert listed.status_code == 200, listed.text
    current = listed.json()["total"]
    for i in range(current, 10):
        r = httpx.post(
            f"{BASE}/api/v1/nurses",
            headers={**_h(admin_token), "Content-Type": "application/json"},
            json={"employee_id": f"DEMO-{i:03d}", "first_name": f"N{i}", "last_name": "Demo",
                  "is_available": True, "role_ids": [role_id]},
        )
        assert r.status_code == 201, r.text
    r = httpx.post(
        f"{BASE}/api/v1/nurses",
        headers={**_h(admin_token), "Content-Type": "application/json"},
        json={"employee_id": "DEMO-OVR", "first_name": "Over", "last_name": "Limit",
              "is_available": True, "role_ids": [role_id]},
    )
    assert r.status_code == 402, r.text
    assert "10" in r.json()["detail"], r.text


def test_demo_plan_cannot_be_changed(super_token: str) -> None:
    tenant_id, _admin_token = _ensure_demo_and_login(super_token)
    r = httpx.put(
        f"{BASE}/api/v1/subscriptions/{tenant_id}",
        headers={**_h(super_token), "Content-Type": "application/json"},
        json={"plan": "max"},
    )
    assert r.status_code == 409, r.text
    sub = httpx.get(f"{BASE}/api/v1/subscriptions/{tenant_id}", headers=_h(super_token))
    assert sub.status_code == 200, sub.text
    assert sub.json()["plan"] == "demo"


def test_impersonate_requires_super_admin(
    tenant_and_admin: tuple[str, str],
) -> None:
    """Non-super-admin calling /auth/impersonate → 403."""
    _tenant_id, admin_token = tenant_and_admin
    r = httpx.post(
        f"{BASE}/api/v1/auth/impersonate/00000000-0000-0000-0000-000000000000",
        headers=_h(admin_token),
    )
    assert r.status_code == 403, r.text


def test_active_subscription_allows_pending_setup_admin_impersonation(
    super_token: str, tenant_and_admin: tuple[str, str]
) -> None:
    """A paid-but-not-setup tenant can still be supported via impersonation."""
    tenant_id, _admin_token = tenant_and_admin
    slug = f"pending-{tenant_id[:8]}"
    create = httpx.post(
        f"{BASE}/api/v1/auth/users",
        headers={**_h(super_token), "Content-Type": "application/json"},
        params={"tenant_id": tenant_id},
        json={
            "email": f"pending-admin-{slug}@example.com",
            "password": ADMIN_PASSWORD,
            "first_name": "Pending",
            "last_name": "Setup",
            "role": "tenant_admin",
        },
    )
    assert create.status_code == 201, create.text
    admin_id = create.json()["id"]

    deactivate = httpx.patch(
        f"{BASE}/api/v1/auth/users/{admin_id}",
        headers={**_h(super_token), "Content-Type": "application/json"},
        params={"tenant_id": tenant_id},
        json={"is_active": False},
    )
    assert deactivate.status_code == 200, deactivate.text
    _put_sub(super_token, tenant_id, plan="free")

    allowed = httpx.post(
        f"{BASE}/api/v1/auth/impersonate/{admin_id}",
        headers=_h(super_token),
    )
    assert allowed.status_code == 200, allowed.text
    impersonated = allowed.json()
    me = httpx.get(f"{BASE}/api/v1/auth/me", headers=_h(impersonated["access_token"]))
    assert me.status_code == 200, me.text
    assert me.json()["email"].startswith("pending-admin-")

    refreshed = httpx.post(
        f"{BASE}/api/v1/auth/refresh",
        json={"refresh_token": impersonated["refresh_token"]},
    )
    assert refreshed.status_code == 200, refreshed.text
    me = httpx.get(f"{BASE}/api/v1/auth/me", headers=_h(refreshed.json()["access_token"]))
    assert me.status_code == 200, me.text

    _put_sub(super_token, tenant_id, plan="free", is_canceled=True)
    denied = httpx.post(
        f"{BASE}/api/v1/auth/impersonate/{admin_id}",
        headers=_h(super_token),
    )
    assert denied.status_code == 409, denied.text
    assert "初始设置" in denied.json()["detail"]


def test_test_tenant_bootstrap_and_impersonate_and_cleanup(
    super_token: str,
) -> None:
    """One-button: create test tenant → impersonate its admin → delete."""
    # 1. Bootstrap
    r = httpx.post(f"{BASE}/api/v1/admin/test-tenant", headers=_h(super_token))
    assert r.status_code == 201, r.text
    creds = r.json()
    tid = creds["tenant_id"]
    assert creds["slug"].startswith("test-")
    assert creds["admin_password"] != "testpass123"
    assert creds["nurse_count"] == 6
    assert creds["role_count"] == 2
    assert creds["skill_count"] == 2
    assert creds["day_group_count"] == 1
    assert creds["shift_count"] == 3
    assert creds["skill_mix_rule_count"] == 3
    assert creds["shift_sequence_rule_count"] == 1

    # 2. List the tenant's users (super admin)
    r = httpx.get(f"{BASE}/api/v1/admin/tenants/{tid}/users", headers=_h(super_token))
    assert r.status_code == 200, r.text
    users = r.json()
    assert any(u["email"] == creds["admin_email"] for u in users)

    # 3. Impersonate the admin
    r = httpx.post(
        f"{BASE}/api/v1/auth/impersonate/{creds['admin_user_id']}",
        headers=_h(super_token),
    )
    assert r.status_code == 200, r.text
    imp_token = r.json()["access_token"]
    # The impersonated token acts as the tenant admin
    r = httpx.get(f"{BASE}/api/v1/auth/me", headers=_h(imp_token))
    assert r.status_code == 200 and r.json()["email"] == creds["admin_email"], r.text
    # And the tenant admin can see the subscription created for the test tenant
    r = httpx.get(f"{BASE}/api/v1/subscriptions/me", headers=_h(imp_token))
    assert r.status_code == 200 and r.json()["is_active"] is True, r.text
    r = httpx.get(f"{BASE}/api/v1/day-groups", headers=_h(imp_token))
    assert r.status_code == 200 and r.json()["total"] == 1, r.text
    assert r.json()["items"][0]["day_numbers"] == list(range(1, 8))
    r = httpx.get(f"{BASE}/api/v1/shift-templates", headers=_h(imp_token))
    assert r.status_code == 200 and r.json()["total"] == 3, r.text
    r = httpx.get(f"{BASE}/api/v1/skills", headers=_h(imp_token))
    assert r.status_code == 200 and r.json()["total"] == 2, r.text
    skills = {item["code"]: item["id"] for item in r.json()["items"]}
    r = httpx.get(f"{BASE}/api/v1/nurses", headers=_h(imp_token))
    assert r.status_code == 200 and r.json()["total"] == creds["nurse_count"], r.text
    nurses = r.json()["items"]
    assert all(skills["BLS"] in (item["skill_ids"] or []) for item in nurses)
    assert any(skills["ICU"] in (item["skill_ids"] or []) for item in nurses)

    r = httpx.get(f"{BASE}/api/v1/skill-mix-rules", headers=_h(imp_token))
    assert r.status_code == 200 and r.json()["total"] == 3, r.text
    rules = r.json()["items"]
    assert all(
        requirement["skill_id"] == skills["BLS"]
        for rule in rules
        for requirement in rule["requirements"]
    )
    r = httpx.get(f"{BASE}/api/v1/shift-sequence-rules", headers=_h(imp_token))
    assert r.status_code == 200 and r.json()["total"] == 1, r.text

    generation = _generate(imp_token)
    assert generation.status_code == 201, generation.text
    deadline = time.monotonic() + 90
    final = None
    while time.monotonic() < deadline:
        poll = httpx.get(
            f"{BASE}/api/v1/schedules/{generation.json()['id']}",
            headers=_h(imp_token),
        )
        assert poll.status_code == 200, poll.text
        final = poll.json()
        if final["status"] in {"completed", "failed", "cancelled"}:
            break
        time.sleep(0.2)
    assert final and final["status"] == "completed", f"unexpected: {final}"
    assert final["stats"]["num_assignments"] == 42

    r = httpx.get(f"{BASE}/api/v1/admin/test-tenant", headers=_h(super_token))
    assert r.status_code == 200, r.text
    listed = next(item for item in r.json() if item["tenant_id"] == tid)
    assert listed["admin_email"] == creds["admin_email"]
    assert listed["admin_password"] is None
    assert listed["shift_count"] == 3
    assert listed["skill_count"] == 2
    assert listed["skill_mix_rule_count"] == 3

    # 4. Cleanup
    r = httpx.delete(f"{BASE}/api/v1/admin/test-tenant/{tid}", headers=_h(super_token))
    assert r.status_code == 204, r.text
    # Tenant is gone
    r = httpx.get(f"{BASE}/api/v1/admin/tenants/{tid}/users", headers=_h(super_token))
    assert r.status_code == 404, r.text


def test_test_tenant_accepts_custom_seed_configuration(super_token: str) -> None:
    invalid = httpx.post(
        f"{BASE}/api/v1/admin/test-tenant",
        headers={**_h(super_token), "Content-Type": "application/json"},
        json={
            "nurse_count": 1,
            "role_count": 1,
            "day_group_count": 1,
            "shift_count": 1,
            "skill_mix_rule_count": 1,
            "shift_sequence_rule_count": 1,
        },
    )
    assert invalid.status_code == 400, invalid.text

    configuration = {
        "nurse_count": 8,
        "role_count": 3,
        "day_group_count": 2,
        "shift_count": 4,
        "skill_mix_rule_count": 2,
        "shift_sequence_rule_count": 2,
    }
    r = httpx.post(
        f"{BASE}/api/v1/admin/test-tenant",
        headers={**_h(super_token), "Content-Type": "application/json"},
        json=configuration,
    )
    assert r.status_code == 201, r.text
    creds = r.json()
    tenant_id = creds["tenant_id"]
    try:
        assert creds["nurse_count"] == 8
        assert creds["role_count"] == 3
        assert creds["day_group_count"] == 2
        assert creds["shift_count"] == 4
        assert creds["skill_mix_rule_count"] == 8
        assert creds["shift_sequence_rule_count"] == 2

        r = httpx.post(
            f"{BASE}/api/v1/auth/login",
            json={"email": creds["admin_email"], "password": creds["admin_password"]},
        )
        assert r.status_code == 200, r.text
        headers = _h(r.json()["access_token"])
        for endpoint, expected in (
            ("day-groups", 2),
            ("shift-templates", 4),
            ("skill-mix-rules", 8),
            ("shift-sequence-rules", 2),
        ):
            r = httpx.get(f"{BASE}/api/v1/{endpoint}", headers=headers)
            assert r.status_code == 200 and r.json()["total"] == expected, r.text

        generation = _generate(headers["Authorization"].removeprefix("Bearer "))
        assert generation.status_code == 201, generation.text
        deadline = time.monotonic() + 90
        final = None
        while time.monotonic() < deadline:
            polled = httpx.get(
                f"{BASE}/api/v1/schedules/{generation.json()['id']}",
                headers=headers,
            )
            assert polled.status_code == 200, polled.text
            final = polled.json()
            if final["status"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.2)
        assert final and final["status"] == "completed", f"unexpected: {final}"
        assert final["stats"]["num_assignments"] == 56, final
    finally:
        deleted = httpx.delete(
            f"{BASE}/api/v1/admin/test-tenant/{tenant_id}",
            headers=_h(super_token),
        )
        assert deleted.status_code == 204, deleted.text


def test_test_tenant_cleanup_refuses_real_tenants(
    super_token: str,
) -> None:
    """Deleting a tenant outside known test classifications → 400."""
    slug = f"real-not-test-{time.time_ns()}"
    created = httpx.post(
        f"{BASE}/api/v1/tenants",
        headers={**_h(super_token), "Content-Type": "application/json"},
        json={"name": "Real Tenant Guard", "slug": slug},
    )
    assert created.status_code == 201, created.text
    tenant_id = created.json()["id"]
    try:
        r = httpx.delete(
            f"{BASE}/api/v1/admin/test-tenant/{tenant_id}", headers=_h(super_token)
        )
        assert r.status_code == 400, r.text
    finally:
        deleted = httpx.delete(
            f"{BASE}/api/v1/tenants/{tenant_id}", headers=_h(super_token)
        )
        assert deleted.status_code == 204, deleted.text


def test_test_tenant_cleanup_refuses_demo_tenant(super_token: str) -> None:
    """Demo-labeled test tenants are repairable data, but never deletable."""
    slug = f"test-demo-guard-{int(time.time())}"
    created = httpx.post(
        f"{BASE}/api/v1/tenants",
        headers={**_h(super_token), "Content-Type": "application/json"},
        json={
            "name": "Demo Delete Guard",
            "slug": slug,
            "settings": {"kind": "test", "dataset": "demo"},
        },
    )
    assert created.status_code == 201, created.text
    tenant_id = created.json()["id"]
    try:
        deleted = httpx.delete(
            f"{BASE}/api/v1/admin/test-tenant/{tenant_id}",
            headers=_h(super_token),
        )
        assert deleted.status_code == 400, deleted.text
        assert deleted.json()["detail"] == "演示租户不能删除"
    finally:
        cleanup = httpx.delete(
            f"{BASE}/api/v1/tenants/{tenant_id}",
            headers=_h(super_token),
        )
        assert cleanup.status_code == 204, cleanup.text


def test_infeasible_schedule_records_structured_reason(super_token: str) -> None:
    r = httpx.post(f"{BASE}/api/v1/admin/test-tenant", headers=_h(super_token))
    assert r.status_code == 201, r.text
    creds = r.json()
    tenant_id = creds["tenant_id"]
    try:
        r = httpx.post(
            f"{BASE}/api/v1/auth/login",
            json={"email": creds["admin_email"], "password": creds["admin_password"]},
        )
        assert r.status_code == 200, r.text
        token = r.json()["access_token"]
        headers = _h(token)

        r = httpx.get(f"{BASE}/api/v1/skill-mix-rules", headers=headers)
        assert r.status_code == 200, r.text
        for rule in r.json()["items"]:
            requirements = [
                {
                    "role_id": requirement["role_id"],
                    "skill_id": requirement["skill_id"],
                    "count": 2 if requirement["role_code"] == "NURSE" else requirement["count"],
                }
                for requirement in rule["requirements"]
            ]
            patched = httpx.patch(
                f"{BASE}/api/v1/skill-mix-rules/{rule['id']}",
                headers={**headers, "Content-Type": "application/json"},
                json={
                    "name": rule["name"],
                    "shift_template_id": rule["shift_template_id"],
                    "priority": rule["priority"],
                    "requirements": requirements,
                },
            )
            assert patched.status_code == 200, patched.text

        r = _generate(token)
        assert r.status_code == 201, r.text
        request_id = r.json()["id"]
        final = None
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            polled = httpx.get(f"{BASE}/api/v1/schedules/{request_id}", headers=headers)
            assert polled.status_code == 200, polled.text
            final = polled.json()
            if final["status"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.2)
        assert final and final["status"] == "failed", f"unexpected: {final}"
        assert "当日至少需要 9 人" in final["error_message"], final
        assert "只有 6 名可排班护士" in final["error_message"], final
        assert final["stats"]["failure_reasons"], final
    finally:
        deleted = httpx.delete(
            f"{BASE}/api/v1/admin/test-tenant/{tenant_id}",
            headers=_h(super_token),
        )
        assert deleted.status_code == 204, deleted.text
