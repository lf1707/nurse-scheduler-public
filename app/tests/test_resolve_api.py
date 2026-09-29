"""B3-11 API integration tests: local re-solve with pinned assignments."""

from __future__ import annotations

import datetime
import os
import uuid
from collections.abc import Generator

import httpx
import psycopg2
import pyotp
import pytest
from pydantic import BaseModel

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
def tenant(super_token: str) -> Generator[dict[str, str], None, None]:
    slug = f"b311-{uuid.uuid4().hex[:10]}"
    response = httpx.post(
        f"{API}/api/v1/tenants",
        headers=_headers(super_token),
        json={"name": f"B3-11 {slug}", "slug": slug},
    )
    assert response.status_code == 201, response.text
    tenant_id = response.json()["id"]
    email = f"admin@{slug}.example.com"
    response = httpx.post(
        f"{API}/api/v1/tenants/{tenant_id}/admin",
        headers=_headers(super_token),
        json={
            "email": email,
            "password": "password123",
            "first_name": "B3",
            "last_name": "Eleven",
            "role": "tenant_admin",
        },
    )
    assert response.status_code == 201, response.text
    login = httpx.post(
        f"{API}/api/v1/auth/login", json={"email": email, "password": "password123"}
    )
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    assert isinstance(token, str)
    yield {"id": tenant_id, "token": token}
    httpx.delete(f"{API}/api/v1/tenants/{tenant_id}", headers=_headers(super_token))


class Seed(BaseModel):
    request_id: str
    schedule_id: str
    role_id: str
    day_shift_id: str
    evening_shift_id: str
    nurse_ids: list[str]


def _seed_schedule(tenant_id: str, *, version: int = 1) -> Seed:
    ids = Seed(
        request_id=str(uuid.uuid4()),
        schedule_id=str(uuid.uuid4()),
        role_id=str(uuid.uuid4()),
        day_shift_id=str(uuid.uuid4()),
        evening_shift_id=str(uuid.uuid4()),
        nurse_ids=[str(uuid.uuid4()) for _ in range(3)],
    )
    period_start = datetime.date.today() + datetime.timedelta(days=1)
    with psycopg2.connect(**DB) as connection, connection.cursor() as cursor:
        cursor.execute("SET app.is_super = '1'")
        cursor.execute(
            "INSERT INTO roles (id, tenant_id, name, code) VALUES (%s, %s, 'RN', 'RN')",
            (ids.role_id, tenant_id),
        )
        day_group_id = str(uuid.uuid4())
        cursor.execute(
            "INSERT INTO day_groups (id, tenant_id, name) VALUES (%s, %s, 'Every day')",
            (day_group_id, tenant_id),
        )
        for day_number in range(1, 8):
            cursor.execute(
                "INSERT INTO day_group_days (id, tenant_id, day_group_id, day_number)"
                " VALUES (%s, %s, %s, %s)",
                (str(uuid.uuid4()), tenant_id, day_group_id, day_number),
            )
        for shift_id, code in ((ids.day_shift_id, "D"), (ids.evening_shift_id, "E")):
            cursor.execute(
                "INSERT INTO shift_templates (id, tenant_id, code, name, start_time,"
                " end_time, duration_hours, day_group_id)"
                " VALUES (%s, %s, %s, %s, %s, %s, 8, %s)",
                (
                    shift_id, tenant_id, code,
                    "Day" if code == "D" else "Evening",
                    "08:00" if code == "D" else "12:00",
                    "16:00" if code == "D" else "20:00",
                    day_group_id,
                ),
            )
        for nurse_id in ids.nurse_ids:
            cursor.execute(
                "INSERT INTO nurses (id, tenant_id, employee_id, first_name,"
                " last_name, is_available, preferences)"
                " VALUES (%s, %s, %s, 'N', 'N', true, '{}'::jsonb)",
                (nurse_id, tenant_id, f"E-{nurse_id[:8]}"),
            )
            cursor.execute(
                "INSERT INTO nurse_roles (nurse_id, role_id) VALUES (%s, %s)",
                (nurse_id, ids.role_id),
            )
        cursor.execute(
            "INSERT INTO schedule_requests (id, tenant_id, period_start, period_days,"
            " request_date, daily_sequence, status, solver_config)"
            " VALUES (%s, %s, %s, 7, %s, %s, 'COMPLETED', '{}'::jsonb)",
            (ids.request_id, tenant_id, period_start, period_start,
             uuid.uuid4().int % 1_000_000_000),
        )
        cursor.execute(
            "INSERT INTO schedules (id, tenant_id, request_id, period_start,"
            " period_days, outcome, version, summary)"
            " VALUES (%s, %s, %s, %s, 7, 'OPTIMAL', %s, '{}'::jsonb)",
            (ids.schedule_id, tenant_id, ids.request_id, period_start, version),
        )
        cursor.execute(
            "UPDATE schedule_requests SET active_schedule_id = %s WHERE id = %s",
            (ids.schedule_id, ids.request_id),
        )
        for i, nurse_id in enumerate(ids.nurse_ids[:2]):
            shift_id = ids.day_shift_id if i == 0 else ids.evening_shift_id
            cursor.execute(
                "INSERT INTO assignments (id, tenant_id, schedule_id, nurse_id,"
                " role_id, date, shift_template_id)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (str(uuid.uuid4()), tenant_id, ids.schedule_id,
                 nurse_id, ids.role_id, period_start, shift_id),
            )
        connection.commit()
    return ids


def test_resolve_with_pin_all_returns_diff(
    tenant: dict[str, str],
    super_token: str,
) -> None:
    seed = _seed_schedule(tenant["id"])
    response = httpx.post(
        f"{API}/api/v1/schedules/{seed.request_id}/resolve",
        headers=_headers(tenant["token"]),
        json={"base_version": 1, "pin_all": True},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert "diff" in data
    assert data["base_version"] == 1
    assert data["assignments"]
    with psycopg2.connect(**DB) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM schedules WHERE request_id = %s",
            (seed.request_id,),
        )
        assert cursor.fetchone()[0] == 1, "Preview must not create a Schedule row"


def _commit_resolve(
    seed: Seed,
    token: str,
    preview: dict[str, object],
) -> httpx.Response:
    return httpx.post(
        f"{API}/api/v1/schedules/{seed.request_id}/resolve/commit",
        headers=_headers(token),
        json={
            "base_version": preview["base_version"],
            "assignments": preview["assignments"],
        },
    )


def test_resolve_preserves_original_schedule(
    tenant: dict[str, str],
    super_token: str,
) -> None:
    seed = _seed_schedule(tenant["id"])
    # Count original assignments
    with psycopg2.connect(**DB) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM assignments WHERE schedule_id = %s",
            (seed.schedule_id,),
        )
        original_count = cur.fetchone()[0]
        cur.execute(
            "SELECT nurse_id, date::text, shift_template_id"
            " FROM assignments WHERE schedule_id = %s ORDER BY date, nurse_id",
            (seed.schedule_id,),
        )
        original_cells = cur.fetchall()

    response = httpx.post(
        f"{API}/api/v1/schedules/{seed.request_id}/resolve",
        headers=_headers(tenant["token"]),
        json={"base_version": 1, "pin_all": True},
    )
    assert response.status_code == 200
    preview = response.json()
    preview_cells = {
        (a["nurse_id"], a["date"]): a["shift_template_id"]
        for a in preview["assignments"]
    }
    for nurse_id, schedule_date, shift_template_id in original_cells:
        assert preview_cells[(nurse_id, schedule_date)] == shift_template_id, (
            "Pinned assignments must be passed into the solver and preserved"
        )
    commit = _commit_resolve(seed, tenant["token"], preview)
    assert commit.status_code == 200, commit.text
    assert commit.json()["version"] == 2

    # Original schedule and its assignments must still exist
    with psycopg2.connect(**DB) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM schedules WHERE id = %s AND version = 1",
            (seed.schedule_id,),
        )
        assert cur.fetchone()[0] == 1, "Original schedule row must be preserved"
        cur.execute(
            "SELECT count(*) FROM assignments WHERE schedule_id = %s",
            (seed.schedule_id,),
        )
        assert cur.fetchone()[0] == original_count, "Original assignments must be preserved"

        # A new schedule with version 2 must exist
        cur.execute(
            "SELECT id FROM schedules WHERE request_id = %s AND version = 2",
            (seed.request_id,),
        )
        new_row = cur.fetchone()
        assert new_row is not None, "A new Schedule (v2) must be created"

    # Result endpoint returns the latest version
    result = httpx.get(
        f"{API}/api/v1/schedules/{seed.request_id}/result",
        headers=_headers(tenant["token"]),
    )
    assert result.status_code == 200
    assert result.json()["version"] == 2

    request_detail = httpx.get(
        f"{API}/api/v1/schedules/{seed.request_id}",
        headers=_headers(tenant["token"]),
    )
    assert request_detail.status_code == 200
    assert request_detail.json()["active_version"] == 2
    assert request_detail.json()["available_versions"] == [1, 2]


def test_resolve_commit_rejects_stale_preview(
    tenant: dict[str, str],
    super_token: str,
) -> None:
    seed = _seed_schedule(tenant["id"])
    preview_response = httpx.post(
        f"{API}/api/v1/schedules/{seed.request_id}/resolve",
        headers=_headers(tenant["token"]),
        json={"base_version": 1, "pin_all": True},
    )
    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    first = _commit_resolve(seed, tenant["token"], preview)
    assert first.status_code == 200, first.text
    second = _commit_resolve(seed, tenant["token"], preview)
    assert second.status_code == 409


def test_resolve_as_super_admin_uses_schedule_tenant(
    tenant: dict[str, str],
    super_token: str,
) -> None:
    seed = _seed_schedule(tenant["id"])
    response = httpx.post(
        f"{API}/api/v1/schedules/{seed.request_id}/resolve",
        headers=_headers(super_token),
        json={"base_version": 1, "pin_all": True},
    )
    assert response.status_code == 200, response.text
    commit = _commit_resolve(seed, super_token, response.json())
    assert commit.status_code == 200, commit.text

    with psycopg2.connect(**DB) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*), count(*) FILTER (WHERE tenant_id IS NULL),"
            " count(DISTINCT tenant_id) FROM assignments"
            " WHERE schedule_id IN ("
            "SELECT id FROM schedules WHERE request_id = %s)",
            (seed.request_id,),
        )
        count, null_count, tenant_count = cursor.fetchone()
    assert count > 0
    assert null_count == 0
    assert tenant_count == 1


def test_resolve_version_conflict(tenant: dict[str, str]) -> None:
    seed = _seed_schedule(tenant["id"])
    response = httpx.post(
        f"{API}/api/v1/schedules/{seed.request_id}/resolve",
        headers=_headers(tenant["token"]),
        json={"base_version": 99, "pin_all": True},
    )
    assert response.status_code == 409


def test_resolve_nonexistent_request(tenant: dict[str, str]) -> None:
    fake_id = str(uuid.uuid4())
    response = httpx.post(
        f"{API}/api/v1/schedules/{fake_id}/resolve",
        headers=_headers(tenant["token"]),
        json={"base_version": 1, "pin_all": True},
    )
    assert response.status_code == 404
