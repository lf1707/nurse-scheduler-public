"""Super-admin resource writes must inherit the target resource tenant."""

from __future__ import annotations

import datetime
import os
import uuid
from collections.abc import Iterator

import httpx
import psycopg2
import pyotp
import pytest

from app.tests.conftest import SUPER_ADMIN_TEST_MFA_SECRET

API = os.environ.get("API_BASE_URL", "http://localhost:8000")
SUPER_EMAIL = "admin@example.com"
SUPER_PASSWORD = os.environ.get("SUPER_ADMIN_PASSWORD", "changeme123")
DB = {
    "dbname": os.environ.get("POSTGRES_DB", "nurse_scheduler"),
    "user": os.environ.get("POSTGRES_USER", "nurse"),
    "password": os.environ.get("POSTGRES_PASSWORD", "nurse"),
    "host": os.environ.get("POSTGRES_HOST", "db"),
}


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def super_token() -> str:
    response = httpx.post(
        f"{API}/api/v1/auth/login",
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


@pytest.fixture
def nurse(super_token: str) -> Iterator[dict[str, str]]:
    slug = f"super-ctx-{uuid.uuid4().hex[:10]}"
    response = httpx.post(
        f"{API}/api/v1/tenants",
        headers=_headers(super_token),
        json={"name": f"Super Context {slug}", "slug": slug},
    )
    assert response.status_code == 201, response.text
    tenant_id = response.json()["id"]
    nurse_id = str(uuid.uuid4())
    with psycopg2.connect(**DB) as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO nurses (id, tenant_id, employee_id, first_name,"
            " last_name, is_available, preferences)"
            " VALUES (%s, %s, %s, 'Super', 'Context', true, '{}'::jsonb)",
            (nurse_id, tenant_id, f"S-{nurse_id[:8]}"),
        )
        connection.commit()
    yield {"id": nurse_id, "tenant_id": tenant_id}
    httpx.delete(
        f"{API}/api/v1/tenants/{tenant_id}", headers=_headers(super_token)
    )


def _single_tenant(nurse_id: str) -> tuple[int, int, int]:
    with psycopg2.connect(**DB) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*), count(*) FILTER (WHERE tenant_id IS NULL),"
            " count(DISTINCT tenant_id) FROM leaves WHERE nurse_id = %s",
            (nurse_id,),
        )
        row = cursor.fetchone()
        assert row is not None and len(row) == 3
        return int(row[0]), int(row[1]), int(row[2])


def test_super_admin_create_leave_uses_nurse_tenant(
    super_token: str,
    nurse: dict[str, str],
) -> None:
    response = httpx.post(
        f"{API}/api/v1/nurses/{nurse['id']}/leaves",
        headers=_headers(super_token),
        json={"nurse_id": nurse["id"], "date": datetime.date.today().isoformat()},
    )
    assert response.status_code == 201, response.text
    count, null_count, tenant_count = _single_tenant(nurse["id"])
    assert count == 1
    assert null_count == 0
    assert tenant_count == 1


def test_super_admin_create_contract_uses_nurse_tenant(
    super_token: str,
    nurse: dict[str, str],
) -> None:
    response = httpx.put(
        f"{API}/api/v1/nurses/{nurse['id']}/contract",
        headers=_headers(super_token),
        json={"shifts_per_period": 10, "min_rest_hours": 11},
    )
    assert response.status_code == 200, response.text
    with psycopg2.connect(**DB) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT tenant_id FROM contracts WHERE nurse_id = %s",
            (nurse["id"],),
        )
        assert cursor.fetchone()[0] == nurse["tenant_id"]
