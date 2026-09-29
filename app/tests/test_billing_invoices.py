"""Manual billing invoice super-admin API regression checks."""

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
def tenant_id(super_token: str) -> Iterator[str]:
    slug = f"billing-{int(time.time())}"
    response = httpx.post(
        f"{BASE}/api/v1/tenants",
        headers={"Authorization": f"Bearer {super_token}"},
        json={"name": f"Billing {slug}", "slug": slug},
    )
    assert response.status_code == 201, response.text
    created_id = cast(str, response.json()["id"])
    yield created_id
    deleted = httpx.delete(
        f"{BASE}/api/v1/tenants/{created_id}",
        headers={"Authorization": f"Bearer {super_token}"},
    )
    assert deleted.status_code == 204, deleted.text


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_invoice_create_pay_and_filter_lifecycle(
    super_token: str,
    tenant_id: str,
) -> None:
    number = f"INV-{int(time.time())}"
    created = httpx.post(
        f"{BASE}/api/v1/billing/invoices",
        headers={**_headers(super_token), "Content-Type": "application/json"},
        json={
            "tenant_id": tenant_id,
            "number": number,
            "amount_minor": 12900,
            "currency": "usd",
            "period_start": "2026-10-01T00:00:00Z",
            "period_end": "2026-10-31T00:00:00Z",
            "due_at": "2026-10-10T00:00:00Z",
            "notes": "manual bank transfer",
        },
    )
    assert created.status_code == 201, created.text
    invoice = created.json()
    assert invoice["status"] == "issued"
    assert invoice["currency"] == "USD"
    invoice_id = invoice["id"]

    listed = httpx.get(
        f"{BASE}/api/v1/billing/invoices",
        headers=_headers(super_token),
        params={"tenant_id": tenant_id, "status": "issued"},
    )
    assert listed.status_code == 200, listed.text
    assert [row["id"] for row in listed.json()] == [invoice_id]

    paid = httpx.patch(
        f"{BASE}/api/v1/billing/invoices/{invoice_id}",
        headers={**_headers(super_token), "Content-Type": "application/json"},
        json={"status": "paid"},
    )
    assert paid.status_code == 200, paid.text
    assert paid.json()["status"] == "paid"
    assert paid.json()["paid_at"] is not None

    voided = httpx.patch(
        f"{BASE}/api/v1/billing/invoices/{invoice_id}",
        headers={**_headers(super_token), "Content-Type": "application/json"},
        json={"status": "void"},
    )
    assert voided.status_code == 409, voided.text


def test_invoice_validates_provider_and_period(
    super_token: str,
    tenant_id: str,
) -> None:
    invalid_period = httpx.post(
        f"{BASE}/api/v1/billing/invoices",
        headers={**_headers(super_token), "Content-Type": "application/json"},
        json={
            "tenant_id": tenant_id,
            "number": f"BAD-{int(time.time())}",
            "amount_minor": 1000,
            "period_start": "2026-10-02T00:00:00Z",
            "period_end": "2026-10-01T00:00:00Z",
        },
    )
    assert invalid_period.status_code == 422, invalid_period.text

    invalid_provider = httpx.post(
        f"{BASE}/api/v1/billing/invoices",
        headers={**_headers(super_token), "Content-Type": "application/json"},
        json={
            "tenant_id": tenant_id,
            "number": f"STRIPE-{int(time.time())}",
            "provider": "stripe",
            "amount_minor": 1000,
        },
    )
    assert invalid_provider.status_code == 422, invalid_provider.text


def test_paid_invoice_requires_billing_period(
    super_token: str,
    tenant_id: str,
) -> None:
    response = httpx.post(
        f"{BASE}/api/v1/billing/invoices",
        headers={**_headers(super_token), "Content-Type": "application/json"},
        json={
            "tenant_id": tenant_id,
            "number": f"PAID-NO-PERIOD-{int(time.time())}",
            "amount_minor": 1000,
            "status": "paid",
        },
    )
    assert response.status_code == 422, response.text


def test_current_paid_invoice_entitles_tenant(
    super_token: str,
    tenant_id: str,
) -> None:
    created = httpx.post(
        f"{BASE}/api/v1/billing/invoices",
        headers={**_headers(super_token), "Content-Type": "application/json"},
        json={
            "tenant_id": tenant_id,
            "number": f"ENTITLE-{int(time.time())}",
            "amount_minor": 12900,
            "period_start": "2026-01-01T00:00:00Z",
            "period_end": "2099-01-01T00:00:00Z",
            "status": "paid",
        },
    )
    assert created.status_code == 201, created.text

    limits = httpx.get(
        f"{BASE}/api/v1/subscriptions/{tenant_id}/limits",
        headers=_headers(super_token),
    )
    assert limits.status_code == 200, limits.text
    assert limits.json() == {"max_nurses": 20, "max_period_days": 30}

    generation = httpx.post(
        f"{BASE}/api/v1/schedules/generate",
        headers={**_headers(super_token), "Content-Type": "application/json"},
        json={
            "tenant_id": tenant_id,
            "period_start": "2026-11-01",
            "period_days": 31,
            "solver_config": {"timeout_seconds": 10, "num_workers": 4},
        },
    )
    assert generation.status_code == 402, generation.text
    assert generation.json()["detail"] != (
        "订阅未开通或已过期,无法生成排班。请联系管理员开通订阅。"
    )
