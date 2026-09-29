"""B2-11: schedule record delete MVP tests.

Covers permission (viewer gets 403), terminal-state guard (pending/running get
409), hard delete with FK cascade (no orphan schedule/assignment), and the
`schedule.request.deleted` audit record written in the same transaction.

DB setup/verification uses sync psycopg2 (not the shared async engine) so this
module cannot leak async-pool connections across pytest-asyncio event loops.
"""

from __future__ import annotations

import datetime
import os
import time
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
DB_PARAMS = {
    "dbname": os.environ.get("POSTGRES_DB", "nurse_scheduler"),
    "user": os.environ.get("POSTGRES_USER", "nurse"),
    "password": os.environ.get("POSTGRES_PASSWORD", "nurse"),
    "host": os.environ.get("POSTGRES_HOST", "db"),
}


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _login(email: str, password: str) -> httpx.Response:
    body = {"email": email, "password": password}
    if email == SUPER_EMAIL:
        body["totp_code"] = pyotp.TOTP(SUPER_ADMIN_TEST_MFA_SECRET).now()
    return httpx.post(f"{API}/api/v1/auth/login", json=body)


@pytest.fixture(scope="module")
def super_tok() -> str:
    r = _login(SUPER_EMAIL, SUPER_PASSWORD)
    assert r.status_code == 200, r.text
    payload = r.json()
    assert isinstance(payload["access_token"], str)
    return payload["access_token"]


@pytest.fixture(scope="module")
def tenant(super_tok: str) -> Iterator[dict[str, str]]:
    """Fresh tenant + admin + viewer tokens."""
    slug = f"b211-{int(time.time())}{uuid.uuid4().hex[:4]}"
    r = httpx.post(
        f"{API}/api/v1/tenants", headers=_h(super_tok),
        json={"name": f"B2-11 {slug}", "slug": slug},
    )
    assert r.status_code == 201, r.text
    tid = r.json()["id"]

    admin_email = f"admin@{slug}.example.com"
    r = httpx.post(
        f"{API}/api/v1/tenants/{tid}/admin", headers=_h(super_tok),
        json={"email": admin_email, "password": "password123",
              "first_name": "Ad", "last_name": "Min", "role": "tenant_admin"},
    )
    assert r.status_code == 201, r.text

    viewer_email = f"viewer@{slug}.example.com"
    r = httpx.post(
        f"{API}/api/v1/auth/users",
        headers=_h(_login(admin_email, "password123").json()["access_token"]),
        json={"email": viewer_email, "password": "password123",
              "first_name": "V", "last_name": "Er", "role": "viewer"},
    )
    assert r.status_code == 201, r.text

    yield {
        "id": tid,
        "admin_token": _login(admin_email, "password123").json()["access_token"],
        "viewer_token": _login(viewer_email, "password123").json()["access_token"],
    }
    deleted = httpx.delete(f"{API}/api/v1/tenants/{tid}", headers=_h(super_tok))
    assert deleted.status_code == 204, deleted.text


def _insert_request(
    tenant_id: str, status: str, with_result: bool = False
) -> dict[str, str]:
    """Insert a ScheduleRequest (and optional Schedule + Assignment chain) directly."""
    role_id = str(uuid.uuid4())
    day_group_id = str(uuid.uuid4())
    shift_id = str(uuid.uuid4())
    nurse_id = str(uuid.uuid4())
    request_id = str(uuid.uuid4())
    schedule_id = str(uuid.uuid4())
    assignment_id = str(uuid.uuid4())
    today = datetime.date.today().isoformat()
    period = datetime.date(2026, 9, 1).isoformat()

    with psycopg2.connect(**DB_PARAMS) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET app.is_super = '1'")
            cursor.execute(
                "INSERT INTO roles (id, tenant_id, name, code) VALUES (%s, %s, 'RN', 'RN')",
                (role_id, tenant_id),
            )
            cursor.execute(
                "INSERT INTO day_groups (id, tenant_id, name) VALUES (%s, %s, 'All week')",
                (day_group_id, tenant_id),
            )
            cursor.execute(
                "INSERT INTO day_group_days (id, tenant_id, day_group_id, day_number)"
                " VALUES (%s, %s, %s, 1)",
                (str(uuid.uuid4()), tenant_id, day_group_id),
            )
            cursor.execute(
                "INSERT INTO shift_templates (id, tenant_id, code, name, start_time,"
                " end_time, duration_hours, day_group_id)"
                " VALUES (%s, %s, 'E', 'Early', '07:00', '15:00', 8.0, %s)",
                (shift_id, tenant_id, day_group_id),
            )
            cursor.execute(
                "INSERT INTO nurses (id, tenant_id, employee_id, first_name, last_name,"
                " is_available, preferences)"
                " VALUES (%s, %s, %s, 'A', 'B', true, '{}'::jsonb)",
                (nurse_id, tenant_id, f"E-{uuid.uuid4().hex[:8]}"),
            )
            cursor.execute(
                "INSERT INTO schedule_requests (id, tenant_id, period_start, period_days,"
                " request_date, daily_sequence, status, solver_config)"
                " VALUES (%s, %s, %s, 1, %s, %s, %s, '{\"timeout_seconds\": 60}'::jsonb)",
                (
                    request_id, tenant_id, period, today,
                    uuid.uuid4().int % 1_000_000_000, status,
                ),
            )
            if with_result:
                cursor.execute(
                    "INSERT INTO schedules (id, tenant_id, request_id, period_start,"
                    " period_days, outcome, version)"
                    " VALUES (%s, %s, %s, %s, 1, 'OPTIMAL', 1)",
                    (schedule_id, tenant_id, request_id, period),
                )
                cursor.execute(
                    "UPDATE schedule_requests SET active_schedule_id = %s"
                    " WHERE id = %s",
                    (schedule_id, request_id),
                )
                cursor.execute(
                    "INSERT INTO assignments (id, tenant_id, schedule_id, nurse_id,"
                    " role_id, date, shift_template_id)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (
                        assignment_id, tenant_id, schedule_id, nurse_id,
                        role_id, period, shift_id,
                    ),
                )
        connection.commit()

    return {
        "request_id": request_id,
        "schedule_id": schedule_id,
        "assignment_id": assignment_id,
    }


def test_viewer_cannot_delete(super_tok: str, tenant: dict[str, str]) -> None:
    r = httpx.delete(
        f"{API}/api/v1/schedules/nonexistent-id",
        headers=_h(tenant["viewer_token"]),
    )
    assert r.status_code == 403


def test_pending_and_running_delete_return_409(tenant: dict[str, str]) -> None:
    for status in ("PENDING", "RUNNING"):
        info = _insert_request(tenant["id"], status)
        r = httpx.delete(
            f"{API}/api/v1/schedules/{info['request_id']}",
            headers=_h(tenant["admin_token"]),
        )
        assert r.status_code == 409, r.text
        # request still exists (not deleted)
        assert (
            httpx.get(
                f"{API}/api/v1/schedules/{info['request_id']}",
                headers=_h(tenant["admin_token"]),
            ).status_code
            == 200
        )


def test_completed_delete_cascades_and_audits(tenant: dict[str, str]) -> None:
    info = _insert_request(tenant["id"], "COMPLETED", with_result=True)
    request_id = info["request_id"]

    # Sanity: result exists before the delete.
    assert (
        httpx.get(
            f"{API}/api/v1/schedules/{request_id}/result",
            headers=_h(tenant["admin_token"]),
        ).status_code
        == 200
    )

    r = httpx.delete(
        f"{API}/api/v1/schedules/{request_id}",
        headers=_h(tenant["admin_token"]),
    )
    assert r.status_code == 204, r.text

    # Request + result are gone (no orphan schedule/assignments, lists empty).
    assert (
        httpx.get(
            f"{API}/api/v1/schedules/{request_id}",
            headers=_h(tenant["admin_token"]),
        ).status_code
        == 404
    )
    assert (
        httpx.get(
            f"{API}/api/v1/schedules/{request_id}/result",
            headers=_h(tenant["admin_token"]),
        ).status_code
        == 404
    )

    with psycopg2.connect(**DB_PARAMS) as connection, connection.cursor() as cursor:
        cursor.execute("SET app.is_super = '1'")
        cursor.execute(
            "SELECT count(*) FROM schedules WHERE request_id = %s", (request_id,)
        )
        assert cursor.fetchone()[0] == 0
        cursor.execute(
            "SELECT count(*) FROM assignments WHERE schedule_id = %s",
            (info["schedule_id"],),
        )
        assert cursor.fetchone()[0] == 0
        cursor.execute(
            "SELECT details FROM security_audit_events"
            " WHERE action = 'schedule.request.deleted'"
        )
        rows = cursor.fetchall()
        assert any(row[0] and row[0].get("request_id") == request_id for row in rows)
