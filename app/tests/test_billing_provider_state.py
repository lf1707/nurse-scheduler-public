"""Provider billing state super-admin API regression checks."""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import AsyncIterator
from typing import cast

import httpx
import pyotp
import pytest

from app.tests.conftest import SUPER_ADMIN_TEST_MFA_SECRET

BASE = os.environ.get("API_BASE_URL", "http://localhost:8000")
SUPER_EMAIL = "admin@example.com"
SUPER_PASSWORD = os.environ.get("SUPER_ADMIN_PASSWORD", "changeme123")


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
    return cast(str, response.json()["access_token"])


@pytest.fixture(scope="module")
async def tenants(super_token: str) -> AsyncIterator[tuple[str, str]]:
    suffix = f"{time.time_ns()}{uuid.uuid4().hex[:6]}"
    ids: list[str] = []
    for index in ("a", "b"):
        response = httpx.post(
            f"{BASE}/api/v1/tenants",
            headers={"Authorization": f"Bearer {super_token}"},
            json={
                "name": f"Provider State {suffix}-{index}",
                "slug": f"provider-state-{suffix}-{index}"[:60],
            },
        )
        assert response.status_code == 201, response.text
        ids.append(cast(str, response.json()["id"]))
    yield ids[0], ids[1]
    for tenant_id in ids:
        deleted = httpx.delete(
            f"{BASE}/api/v1/tenants/{tenant_id}",
            headers={"Authorization": f"Bearer {super_token}"},
        )
        assert deleted.status_code == 204, deleted.text


def _headers(super_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {super_token}",
        "Content-Type": "application/json",
    }


async def test_customer_and_subscription_validate_references(
    super_token: str,
    tenants: tuple[str, str],
) -> None:
    tenant_id, other_tenant_id = tenants
    customer_response = httpx.post(
        f"{BASE}/api/v1/billing/customers",
        headers=_headers(super_token),
        json={
            "tenant_id": tenant_id,
            "provider": "stripe",
            "provider_customer_id": f"cus-{time.time_ns()}",
            "email": "billing@example.com",
        },
    )
    assert customer_response.status_code == 201, customer_response.text
    customer = customer_response.json()

    duplicate_pair = httpx.post(
        f"{BASE}/api/v1/billing/customers",
        headers=_headers(super_token),
        json={
            "tenant_id": tenant_id,
            "provider": "stripe",
            "provider_customer_id": f"cus-other-{time.time_ns()}",
        },
    )
    assert duplicate_pair.status_code == 409, duplicate_pair.text

    subscription_response = httpx.post(
        f"{BASE}/api/v1/billing/subscriptions",
        headers=_headers(super_token),
        json={
            "tenant_id": tenant_id,
            "provider": "stripe",
            "provider_subscription_id": f"sub-{time.time_ns()}",
            "customer_id": customer["id"],
            "plan": "pro",
            "status": "active",
            "current_period_start": "2026-10-01T00:00:00Z",
            "current_period_end": "2026-11-01T00:00:00Z",
        },
    )
    assert subscription_response.status_code == 201, subscription_response.text
    subscription = subscription_response.json()

    listed = httpx.get(
        f"{BASE}/api/v1/billing/subscriptions",
        headers=_headers(super_token),
        params={"tenant_id": tenant_id, "status": "active"},
    )
    assert listed.status_code == 200, listed.text
    assert [row["id"] for row in listed.json()] == [subscription["id"]]

    mismatched_customer = httpx.post(
        f"{BASE}/api/v1/billing/subscriptions",
        headers=_headers(super_token),
        json={
            "tenant_id": other_tenant_id,
            "provider": "stripe",
            "provider_subscription_id": f"sub-mismatch-{time.time_ns()}",
            "customer_id": customer["id"],
        },
    )
    assert mismatched_customer.status_code == 422, mismatched_customer.text

    synced = httpx.patch(
        f"{BASE}/api/v1/billing/subscriptions/{subscription['id']}",
        headers=_headers(super_token),
        json={"status": "past_due"},
    )
    assert synced.status_code == 200, synced.text
    assert synced.json()["status"] == "past_due"


async def test_subscription_reconciliation_is_idempotent_and_atomic(
    super_token: str,
    tenants: tuple[str, str],
) -> None:
    tenant_id, _other_tenant_id = tenants
    existing_customers = httpx.get(
        f"{BASE}/api/v1/billing/customers",
        headers=_headers(super_token),
        params={"tenant_id": tenant_id, "provider": "stripe"},
    )
    assert existing_customers.status_code == 200, existing_customers.text
    if existing_customers.json():
        customer_data = existing_customers.json()[0]
    else:
        customer = httpx.post(
            f"{BASE}/api/v1/billing/customers",
            headers=_headers(super_token),
            json={
                "tenant_id": tenant_id,
                "provider": "stripe",
                "provider_customer_id": f"cus-reconcile-{time.time_ns()}",
            },
        )
        assert customer.status_code == 201, customer.text
        customer_data = customer.json()
    subscription = httpx.post(
        f"{BASE}/api/v1/billing/subscriptions",
        headers=_headers(super_token),
        json={
            "tenant_id": tenant_id,
            "provider": "stripe",
            "provider_subscription_id": f"sub-reconcile-{time.time_ns()}",
            "customer_id": customer_data["id"],
            "status": "active",
            "current_period_start": "2026-10-01T00:00:00Z",
            "current_period_end": "2026-11-01T00:00:00Z",
        },
    )
    assert subscription.status_code == 201, subscription.text
    subscription_id = subscription.json()["id"]

    snapshot = {
        "provider_customer_id": customer_data["provider_customer_id"],
        "provider_subscription_id": subscription.json()["provider_subscription_id"],
        "observed_at": "2026-10-02T12:00:00Z",
        "status": "active",
        "current_period_start": "2026-10-01T00:00:00Z",
        "current_period_end": "2026-11-01T00:00:00Z",
    }

    in_sync = httpx.post(
        f"{BASE}/api/v1/billing/subscriptions/{subscription_id}/reconcile",
        headers=_headers(super_token),
        json={**snapshot, "observation_id": "evt-sync"},
    )
    assert in_sync.status_code == 200, in_sync.text
    assert in_sync.json()["outcome"] == "in_sync"
    operation_id = in_sync.json()["operation"]["id"]

    replay = httpx.post(
        f"{BASE}/api/v1/billing/subscriptions/{subscription_id}/reconcile",
        headers=_headers(super_token),
        json={**snapshot, "observation_id": "evt-sync"},
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["outcome"] == "in_sync"
    assert replay.json()["operation"]["id"] == operation_id

    forged_replay = httpx.post(
        f"{BASE}/api/v1/billing/subscriptions/{subscription_id}/reconcile",
        headers=_headers(super_token),
        json={**snapshot, "observation_id": "evt-sync", "status": "past_due"},
    )
    assert forged_replay.status_code == 409, forged_replay.text

    updated = httpx.post(
        f"{BASE}/api/v1/billing/subscriptions/{subscription_id}/reconcile",
        headers=_headers(super_token),
        json={
            **snapshot,
            "observation_id": "evt-update",
            "observed_at": "2026-10-03T12:00:00Z",
            "status": "past_due",
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["outcome"] == "updated"
    assert updated.json()["subscription"]["status"] == "past_due"
    assert updated.json()["changes"] == [
        {
            "field": "status",
            "before": "active",
            "after": "past_due",
        }
    ]

    mismatch = httpx.post(
        f"{BASE}/api/v1/billing/subscriptions/{subscription_id}/reconcile",
        headers=_headers(super_token),
        json={
            **snapshot,
            "observation_id": "evt-conflict",
            "provider_customer_id": "cus-does-not-match",
        },
    )
    assert mismatch.status_code == 409, mismatch.text

    stale = httpx.post(
        f"{BASE}/api/v1/billing/subscriptions/{subscription_id}/reconcile",
        headers=_headers(super_token),
        json={
            **snapshot,
            "observation_id": "evt-stale",
            "status": "active",
        },
    )
    assert stale.status_code == 409, stale.text

    older_replay = httpx.post(
        f"{BASE}/api/v1/billing/subscriptions/{subscription_id}/reconcile",
        headers=_headers(super_token),
        json={**snapshot, "observation_id": "evt-sync"},
    )
    assert older_replay.status_code == 200, older_replay.text

    stale_after_replay = httpx.post(
        f"{BASE}/api/v1/billing/subscriptions/{subscription_id}/reconcile",
        headers=_headers(super_token),
        json={
            **snapshot,
            "observation_id": "evt-stale-after-replay",
            "status": "active",
        },
    )
    assert stale_after_replay.status_code == 409, stale_after_replay.text

    operations = httpx.get(
        f"{BASE}/api/v1/billing/operations",
        headers=_headers(super_token),
        params={
            "tenant_id": tenant_id,
            "operation_type": "reconcile_subscription",
        },
    )
    assert operations.status_code == 200, operations.text
    assert [operation["id"] for operation in operations.json()].count(operation_id) == 1


async def test_operations_enforce_idempotency_and_lifecycle(
    super_token: str,
    tenants: tuple[str, str],
) -> None:
    tenant_id, other_tenant_id = tenants
    customers = httpx.get(
        f"{BASE}/api/v1/billing/customers",
        headers=_headers(super_token),
        params={"tenant_id": tenant_id, "provider": "stripe"},
    )
    assert customers.status_code == 200, customers.text
    customer_id = customers.json()[0]["id"]
    idempotency_key = f"op-{time.time_ns()}-{uuid.uuid4().hex}"

    created = httpx.post(
        f"{BASE}/api/v1/billing/operations",
        headers=_headers(super_token),
        json={
            "tenant_id": tenant_id,
            "provider": "stripe",
            "operation_type": "create_customer",
            "idempotency_key": idempotency_key,
            "customer_id": customer_id,
            "status": "pending",
        },
    )
    assert created.status_code == 201, created.text
    operation = created.json()

    duplicate_key = httpx.post(
        f"{BASE}/api/v1/billing/operations",
        headers=_headers(super_token),
        json={
            "tenant_id": other_tenant_id,
            "provider": "stripe",
            "operation_type": "create_customer",
            "idempotency_key": idempotency_key,
        },
    )
    assert duplicate_key.status_code == 409, duplicate_key.text

    mismatched_reference = httpx.post(
        f"{BASE}/api/v1/billing/operations",
        headers=_headers(super_token),
        json={
            "tenant_id": other_tenant_id,
            "provider": "stripe",
            "operation_type": "create_customer",
            "idempotency_key": f"op-{time.time_ns()}-{uuid.uuid4().hex}",
            "customer_id": customer_id,
        },
    )
    assert mismatched_reference.status_code == 422, mismatched_reference.text

    completed = httpx.patch(
        f"{BASE}/api/v1/billing/operations/{operation['id']}",
        headers=_headers(super_token),
        json={"status": "completed", "result": {"provider_customer_id": customer_id}},
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "completed"
    assert completed.json()["completed_at"] is not None

    reopened = httpx.patch(
        f"{BASE}/api/v1/billing/operations/{operation['id']}",
        headers=_headers(super_token),
        json={"status": "pending"},
    )
    assert reopened.status_code == 409, reopened.text

    terminal_mutation = httpx.patch(
        f"{BASE}/api/v1/billing/operations/{operation['id']}",
        headers=_headers(super_token),
        json={"result": {"changed": True}},
    )
    assert terminal_mutation.status_code == 409, terminal_mutation.text


async def test_checkout_operation_completion_requires_provider_id(
    super_token: str,
    tenants: tuple[str, str],
) -> None:
    tenant_id, _other_tenant_id = tenants
    customers = httpx.get(
        f"{BASE}/api/v1/billing/customers",
        headers=_headers(super_token),
        params={"tenant_id": tenant_id, "provider": "stripe"},
    )
    assert customers.status_code == 200, customers.text
    customer_id = customers.json()[0]["id"]
    subscription = httpx.post(
        f"{BASE}/api/v1/billing/subscriptions",
        headers=_headers(super_token),
        json={
            "tenant_id": tenant_id,
            "provider": "stripe",
            "provider_subscription_id": f"sub-checkout-{time.time_ns()}",
            "customer_id": customer_id,
            "status": "incomplete",
        },
    )
    assert subscription.status_code == 201, subscription.text
    operation = httpx.post(
        f"{BASE}/api/v1/billing/operations",
        headers=_headers(super_token),
        json={
            "tenant_id": tenant_id,
            "provider": "stripe",
            "operation_type": "change_subscription",
            "idempotency_key": f"op-{time.time_ns()}-{uuid.uuid4().hex}",
            "customer_id": customer_id,
            "subscription_id": subscription.json()["id"],
        },
    )
    assert operation.status_code == 201, operation.text
    completed = httpx.patch(
        f"{BASE}/api/v1/billing/operations/{operation.json()['id']}",
        headers=_headers(super_token),
        json={"status": "completed"},
    )
    assert completed.status_code == 422, completed.text
    completed_with_id = httpx.patch(
        f"{BASE}/api/v1/billing/operations/{operation.json()['id']}",
        headers=_headers(super_token),
        json={
            "status": "completed",
            "provider_operation_id": f"po-{time.time_ns()}",
        },
    )
    assert completed_with_id.status_code == 200, completed_with_id.text
