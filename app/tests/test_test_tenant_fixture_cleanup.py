"""Regression coverage for integration-fixture tenant cleanup."""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from typing import cast

import httpx
import pyotp
import pytest

from app.api.v1.admin import TEST_TENANT_SLUG_PREFIXES
from app.tests.conftest import SUPER_ADMIN_TEST_MFA_SECRET

BASE = os.environ.get("API_BASE_URL", "http://localhost:8000")


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
    token = response.json()["access_token"]
    assert isinstance(token, str)
    return token


@pytest.fixture(scope="module")
def fixture_tenants(super_token: str) -> Iterator[list[tuple[str, str]]]:
    headers = {"Authorization": f"Bearer {super_token}"}
    created: list[tuple[str, str]] = []
    for prefix in TEST_TENANT_SLUG_PREFIXES:
        slug = f"{prefix}-cleanup-{time.time_ns()}"
        response = httpx.post(
            f"{BASE}/api/v1/tenants",
            headers=headers,
            json={"name": f"Fixture Cleanup {slug}", "slug": slug},
        )
        assert response.status_code == 201, response.text
        created.append((cast(str, response.json()["id"]), slug))
    yield created


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_fixture_tenants_are_listed_and_bulk_cleaned(
    super_token: str,
    fixture_tenants: list[tuple[str, str]],
) -> None:
    expected_slugs = {slug for _tenant_id, slug in fixture_tenants}
    listed = httpx.get(
        f"{BASE}/api/v1/admin/test-tenant", headers=_headers(super_token)
    )
    assert listed.status_code == 200, listed.text
    listed_slugs = {cast(str, item["slug"]) for item in listed.json()}
    assert expected_slugs <= listed_slugs

    cleaned = httpx.post(
        f"{BASE}/api/v1/admin/test-tenant/cleanup",
        headers=_headers(super_token),
    )
    assert cleaned.status_code == 200, cleaned.text
    cleaned_slugs = {
        cast(str, item["slug"])
        for item in cleaned.json()["deleted_test_tenants"]
    }
    assert expected_slugs <= cleaned_slugs
