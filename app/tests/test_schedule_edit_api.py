"""B3-10 API persistence and concurrency regression tests."""

from __future__ import annotations

import asyncio
import datetime
import os
import uuid
from collections.abc import Iterator

import httpx
import psycopg2
import pyotp
import pytest
from httpx import AsyncClient
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
def tenant(super_token: str) -> Iterator[dict[str, str]]:
    slug = f"b310-{uuid.uuid4().hex[:10]}"
    response = httpx.post(
        f"{API}/api/v1/tenants",
        headers=_headers(super_token),
        json={"name": f"B3-10 {slug}", "slug": slug},
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
            "last_name": "Ten",
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
    deleted = httpx.delete(f"{API}/api/v1/tenants/{tenant_id}", headers=_headers(super_token))
    assert deleted.status_code == 204, deleted.text


class Seed(BaseModel):
    request_id: str
    schedule_id: str
    assignment_id: str
    role_id: str
    day_shift_id: str
    evening_shift_id: str
    nurse_ids: list[str]


async def test_deleted_schedule_version_is_hidden_and_number_is_not_reused(
    tenant: dict[str, str],
    client: AsyncClient,
) -> None:
    seed = _seed_schedule(tenant["id"])
    deleted_schedule_id = str(uuid.uuid4())
    with psycopg2.connect(**DB) as connection, connection.cursor() as cursor:
        cursor.execute("SET app.is_super = '1'")
        cursor.execute(
            "INSERT INTO schedules (id, tenant_id, request_id, period_start,"
            " period_days, outcome, version, summary)"
            " VALUES (%s, %s, %s, %s, 7, 'OPTIMAL', 8, '{}'::jsonb)",
            (
                deleted_schedule_id,
                tenant["id"],
                seed.request_id,
                datetime.date.today(),
            ),
        )

    conflict = await client.delete(
        f"{API}/api/v1/schedules/{seed.request_id}/versions/7",
        headers=_headers(tenant["token"]),
    )
    assert conflict.status_code == 409, conflict.text
    missing = await client.delete(
        f"{API}/api/v1/schedules/{seed.request_id}/versions/99",
        headers=_headers(tenant["token"]),
    )
    assert missing.status_code == 404, missing.text

    deleted = await client.delete(
        f"{API}/api/v1/schedules/{seed.request_id}/versions/8",
        headers=_headers(tenant["token"]),
    )
    assert deleted.status_code == 204, deleted.text
    repeat_delete = await client.delete(
        f"{API}/api/v1/schedules/{seed.request_id}/versions/8",
        headers=_headers(tenant["token"]),
    )
    assert repeat_delete.status_code == 404, repeat_delete.text

    versions = await client.get(
        f"{API}/api/v1/schedules/{seed.request_id}/versions",
        headers=_headers(tenant["token"]),
    )
    assert versions.status_code == 200, versions.text
    assert [item["version"] for item in versions.json()] == [7]
    deleted_result = await client.get(
        f"{API}/api/v1/schedules/{seed.request_id}/result?version=8",
        headers=_headers(tenant["token"]),
    )
    assert deleted_result.status_code == 404, deleted_result.text
    request_detail = await client.get(
        f"{API}/api/v1/schedules/{seed.request_id}",
        headers=_headers(tenant["token"]),
    )
    assert request_detail.status_code == 200, request_detail.text
    assert request_detail.json()["available_versions"] == [7]

    edited = await client.patch(
        f"{API}/api/v1/schedules/{seed.request_id}/assignments",
        headers=_headers(tenant["token"]),
        json={
            "base_version": 7,
            "operations": [
                {
                    "action": "add",
                    "nurse_id": seed.nurse_ids[2],
                    "date": _tomorrow(),
                    "shift_template_id": seed.day_shift_id,
                    "role_id": seed.role_id,
                }
            ],
        },
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["version"] == 9

    with psycopg2.connect(**DB) as connection, connection.cursor() as cursor:
        cursor.execute("SET app.is_super = '1'")
        cursor.execute(
            "SELECT version, deleted_at FROM schedules"
            " WHERE request_id = %s ORDER BY version",
            (seed.request_id,),
        )
        rows = cursor.fetchall()
        assert [version for version, _deleted_at in rows] == [7, 8, 9]
        assert rows[1][1] is not None
        cursor.execute(
            "SELECT details FROM security_audit_events"
            " WHERE action = 'schedule.version.deleted'"
        )
        audits = cursor.fetchall()
        assert any(
            row[0]
            and row[0].get("request_id") == seed.request_id
            and row[0].get("schedule_id") == deleted_schedule_id
            and row[0].get("version") == 8
            for row in audits
        )


def _seed_schedule(
    tenant_id: str,
    *,
    status: str = "COMPLETED",
    skill_required: int | None = None,
    first_assignment_role_id: str | None = None,
) -> Seed:
    ids = Seed(
        request_id=str(uuid.uuid4()),
        schedule_id=str(uuid.uuid4()),
        assignment_id=str(uuid.uuid4()),
        role_id=str(uuid.uuid4()),
        day_shift_id=str(uuid.uuid4()),
        evening_shift_id=str(uuid.uuid4()),
        nurse_ids=[str(uuid.uuid4()) for _ in range(3)],
    )
    period_start = datetime.date.today()
    tomorrow = period_start + datetime.timedelta(days=1)
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
        for shift_id, code in (
            (ids.day_shift_id, "D"),
            (ids.evening_shift_id, "E"),
        ):
            cursor.execute(
                "INSERT INTO shift_templates (id, tenant_id, code, name, start_time,"
                " end_time, duration_hours, day_group_id)"
                " VALUES (%s, %s, %s, %s, %s, %s, 8, %s)",
                (
                    shift_id,
                    tenant_id,
                    code,
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
            " VALUES (%s, %s, %s, 7, %s, %s, %s, '{}'::jsonb)",
            (
                ids.request_id,
                tenant_id,
                period_start,
                period_start,
                uuid.uuid4().int % 1_000_000_000,
                status,
            ),
        )
        if status != "COMPLETED":
            connection.commit()
            return ids
        cursor.execute(
            "INSERT INTO schedules (id, tenant_id, request_id, period_start,"
            " period_days, outcome, version, summary)"
            " VALUES (%s, %s, %s, %s, 7, 'OPTIMAL', 7, %s::jsonb)",
            (
                ids.schedule_id,
                tenant_id,
                ids.request_id,
                period_start,
                '{"num_conflicts": 4, "num_assignments": 2}',
            ),
        )
        cursor.execute(
            "UPDATE schedule_requests SET active_schedule_id = %s WHERE id = %s",
            (ids.schedule_id, ids.request_id),
        )
        cursor.execute(
            "INSERT INTO assignments (id, tenant_id, schedule_id, nurse_id, role_id,"
            " date, shift_template_id) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                ids.assignment_id,
                tenant_id,
                ids.schedule_id,
                ids.nurse_ids[0],
                first_assignment_role_id,
                tomorrow,
                ids.day_shift_id,
            ),
        )
        if skill_required is None:
            cursor.execute(
                "INSERT INTO nurse_preferences (id, tenant_id, nurse_id, date,"
                " shift_template_id, request_type, priority)"
                " VALUES (%s, %s, %s, %s, %s, 'AVOID', 1)",
                (str(uuid.uuid4()), tenant_id, ids.nurse_ids[1], tomorrow, ids.evening_shift_id),
            )
            cursor.execute(
                "INSERT INTO nurse_preferences (id, tenant_id, nurse_id, date,"
                " shift_template_id, request_type, priority)"
                " VALUES (%s, %s, %s, %s, %s, 'LIKE', 1)",
                (str(uuid.uuid4()), tenant_id, ids.nurse_ids[2], tomorrow, ids.day_shift_id),
            )
            cursor.execute(
                "INSERT INTO assignments (id, tenant_id, schedule_id, nurse_id, role_id,"
                " date, shift_template_id) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    str(uuid.uuid4()),
                    tenant_id,
                    ids.schedule_id,
                    ids.nurse_ids[1],
                    ids.role_id,
                    tomorrow,
                    ids.evening_shift_id,
                ),
            )
        else:
            rule_id = str(uuid.uuid4())
            cursor.execute(
                "INSERT INTO skill_mix_rules (id, tenant_id, name, shift_template_id,"
                " priority, is_active) VALUES (%s, %s, 'Two RN', %s, 1, true)",
                (rule_id, tenant_id, ids.day_shift_id),
            )
            cursor.execute(
                "INSERT INTO skill_mix_requirements (id, tenant_id, skill_mix_rule_id,"
                " role_id, skill_id, count) VALUES (%s, %s, %s, %s, NULL, %s)",
                (str(uuid.uuid4()), tenant_id, rule_id, ids.role_id, skill_required),
            )
    return ids


def _patch(
    token: str,
    seed: Seed,
    version: int,
    operations: list[dict[str, object]],
) -> httpx.Response:
    return httpx.patch(
        f"{API}/api/v1/schedules/{seed.request_id}/assignments",
        headers=_headers(token),
        json={"base_version": version, "operations": operations},
    )


def test_non_completed_schedule_is_rejected(tenant: dict[str, str]) -> None:
    seed = _seed_schedule(tenant["id"], status="PENDING")
    response = _patch(
        tenant["token"], seed, 1,
        [{"action": "add", "nurse_id": seed.nurse_ids[2], "date": _tomorrow(),
          "shift_template_id": seed.day_shift_id, "role_id": seed.role_id}],
    )
    assert response.status_code == 409, response.text


def test_past_date_is_rejected(tenant: dict[str, str]) -> None:
    seed = _seed_schedule(tenant["id"])
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    response = _patch(
        tenant["token"], seed, 7,
        [{"action": "add", "nurse_id": seed.nurse_ids[2], "date": yesterday,
          "shift_template_id": seed.day_shift_id, "role_id": seed.role_id}],
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"][0]["code"] == "past_date"


def test_stale_version_is_rejected(tenant: dict[str, str]) -> None:
    seed = _seed_schedule(tenant["id"])
    response = _patch(
        tenant["token"], seed, 6,
        [{"action": "add", "nurse_id": seed.nurse_ids[2], "date": _tomorrow(),
          "shift_template_id": seed.day_shift_id, "role_id": seed.role_id}],
    )
    assert response.status_code == 409, response.text


def test_hard_constraint_is_rejected_without_writing(tenant: dict[str, str]) -> None:
    seed = _seed_schedule(tenant["id"])
    response = _patch(
        tenant["token"], seed, 7,
        [{"action": "add", "nurse_id": seed.nurse_ids[0], "date": _tomorrow(),
          "shift_template_id": seed.evening_shift_id, "role_id": seed.role_id}],
    )
    assert response.status_code == 422, response.text
    assert any(
        violation["code"] == "multiple_shifts_same_day"
        for violation in response.json()["detail"]
    )
    with psycopg2.connect(**DB) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM assignments WHERE schedule_id = %s",
            (seed.schedule_id,),
        )
        assert cursor.fetchone()[0] == 2


async def test_successful_edit_creates_assignments_summary_audit_and_version(
    tenant: dict[str, str],
    client: AsyncClient,
) -> None:
    seed = _seed_schedule(tenant["id"])
    response = await client.patch(
        f"/api/v1/schedules/{seed.request_id}/assignments",
        headers=_headers(tenant["token"]),
        json={
            "base_version": 7,
            "operations": [
                {
                    "action": "add",
                    "nurse_id": seed.nurse_ids[2],
                    "date": _tomorrow(),
                    "shift_template_id": seed.day_shift_id,
                    "role_id": seed.role_id,
                }
            ],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["version"] == 8
    assert body["warnings"] == []
    assert body["summary"]["num_conflicts"] == 4
    assert body["summary"]["num_assignments"] == 3
    assert body["summary"]["num_nurses"] == 3
    assert body["summary"]["preference_stats"] == {"loaded": 2, "satisfied": 1, "violated": 1}

    result = await client.get(
        f"/api/v1/schedules/{seed.request_id}/result",
        headers=_headers(tenant["token"]),
    )
    assert result.status_code == 200, result.text
    assert result.json()["version"] == 8
    assignments = result.json()["assignments"]
    assert len(assignments) == 3
    added_assignment = next(
        assignment
        for assignment in assignments
        if assignment["nurse_id"] == seed.nurse_ids[2]
    )
    assert added_assignment["satisfied_preference"] is True

    versions = await client.get(
        f"/api/v1/schedules/{seed.request_id}/versions",
        headers=_headers(tenant["token"]),
    )
    assert versions.status_code == 200, versions.text
    assert [item["version"] for item in versions.json()] == [8, 7]
    assert [item["assignment_count"] for item in versions.json()] == [3, 2]

    old_result = await client.get(
        f"/api/v1/schedules/{seed.request_id}/result?version=7",
        headers=_headers(tenant["token"]),
    )
    assert old_result.status_code == 200, old_result.text
    assert old_result.json()["version"] == 7
    assert len(old_result.json()["assignments"]) == 2

    request_detail = await client.get(
        f"/api/v1/schedules/{seed.request_id}",
        headers=_headers(tenant["token"]),
    )
    assert request_detail.status_code == 200, request_detail.text
    assert request_detail.json()["active_version"] == 8
    assert request_detail.json()["available_versions"] == [7, 8]

    schedule_list = await client.get(
        "/api/v1/schedules?status=completed",
        headers=_headers(tenant["token"]),
    )
    assert schedule_list.status_code == 200, schedule_list.text
    listed = next(
        item for item in schedule_list.json()["items"]
        if item["id"] == seed.request_id
    )
    assert listed["active_version"] == 8
    assert listed["available_versions"] == [7, 8]

    with psycopg2.connect(**DB) as connection, connection.cursor() as cursor:
        cursor.execute("SET app.is_super = '1'")
        cursor.execute(
            "SELECT id, version FROM schedules"
            " WHERE request_id = %s ORDER BY version",
            (seed.request_id,),
        )
        schedule_rows = cursor.fetchall()
        assert [version for _schedule_id, version in schedule_rows] == [7, 8]
        new_schedule_id, _version = schedule_rows[-1]
        assert new_schedule_id != seed.schedule_id

        for schedule_id, assignment_count in (
            (seed.schedule_id, 2),
            (new_schedule_id, 3),
        ):
            cursor.execute(
                "SELECT count(*) FROM assignments WHERE schedule_id = %s",
                (schedule_id,),
            )
            assert cursor.fetchone()[0] == assignment_count
        cursor.execute(
            "SELECT details FROM security_audit_events"
            " WHERE action = 'schedule.assignments.edited'"
        )
        rows = cursor.fetchall()
        assert any(
            row[0] and row[0].get("request_id") == seed.request_id
            and row[0].get("new_version") == 8
            and row[0].get("activated") is True
            for row in rows
        )


def test_effective_version_can_be_restored_and_scopes_nurse_schedule(
    tenant: dict[str, str],
) -> None:
    seed = _seed_schedule(tenant["id"])
    created = httpx.post(
        f"{API}/api/v1/auth/users",
        headers=_headers(tenant["token"]),
        json={
            "email": f"nurse-{uuid.uuid4().hex[:10]}@example.com",
            "password": "password123",
            "first_name": "One",
            "last_name": "Nurse",
            "role": "nurse",
            "nurse_id": seed.nurse_ids[0],
        },
    )
    assert created.status_code == 201, created.text
    nurse_login = httpx.post(
        f"{API}/api/v1/auth/login",
        json={"email": created.json()["email"], "password": "password123"},
    )
    assert nurse_login.status_code == 200, nurse_login.text
    nurse_headers = _headers(nurse_login.json()["access_token"])

    edited = _patch(
        tenant["token"], seed, 7,
        [{"action": "add", "nurse_id": seed.nurse_ids[2], "date": _tomorrow(),
          "shift_template_id": seed.day_shift_id, "role_id": seed.role_id}],
    )
    assert edited.status_code == 200, edited.text

    restored = httpx.put(
        f"{API}/api/v1/schedules/{seed.request_id}/active-version",
        headers=_headers(tenant["token"]),
        json={"version": 7},
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["active_version"] == 7

    result = httpx.get(
        f"{API}/api/v1/schedules/{seed.request_id}/result",
        headers=_headers(tenant["token"]),
    )
    assert result.status_code == 200
    assert result.json()["version"] == 7
    assert len(result.json()["assignments"]) == 2

    nurse_history = httpx.get(
        f"{API}/api/v1/schedules/{seed.request_id}/result?version=8",
        headers=nurse_headers,
    )
    assert nurse_history.status_code == 403

    mine = httpx.get(f"{API}/api/v1/schedules/mine", headers=nurse_headers)
    assert mine.status_code == 200, mine.text
    assert mine.json()["total"] == 1
    assert mine.json()["items"][0]["active_version"] == 7
    assert mine.json()["items"][0]["assignment_count"] == 1


def test_skill_mix_override_is_required_then_persisted(tenant: dict[str, str]) -> None:
    seed = _seed_schedule(tenant["id"], skill_required=3)
    operation: list[dict[str, object]] = [{
        "action": "add", "nurse_id": seed.nurse_ids[2], "date": _tomorrow(),
        "shift_template_id": seed.day_shift_id, "role_id": seed.role_id,
    }]
    without_override = _patch(tenant["token"], seed, 7, operation)
    assert without_override.status_code == 422, without_override.text
    assert without_override.json()["detail"][0]["code"] == "override_required"

    with_override = httpx.patch(
        f"{API}/api/v1/schedules/{seed.request_id}/assignments",
        headers=_headers(tenant["token"]),
        json={
            "base_version": 7,
            "operations": operation,
            "override_reason": "Approved by charge nurse",
        },
    )
    assert with_override.status_code == 200, with_override.text
    assert with_override.json()["warnings"][0]["code"] == "skill_mix_not_met"


async def test_concurrent_edits_serialize_on_version(
    tenant: dict[str, str],
    client: AsyncClient,
) -> None:
    seed = _seed_schedule(tenant["id"])
    operation = [{
        "action": "add", "nurse_id": seed.nurse_ids[2], "date": _tomorrow(),
        "shift_template_id": seed.day_shift_id, "role_id": seed.role_id,
    }]
    responses = await asyncio.gather(*[
        client.patch(
            f"/api/v1/schedules/{seed.request_id}/assignments",
            headers=_headers(tenant["token"]),
            json={"base_version": 7, "operations": operation},
        )
        for _ in range(2)
    ])
    statuses = sorted(response.status_code for response in responses)
    assert statuses == [200, 409], [response.text for response in responses]
    with psycopg2.connect(**DB) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT array_agg(version ORDER BY version)"
            " FROM schedules WHERE request_id = %s",
            (seed.request_id,),
        )
        assert cursor.fetchone()[0] == [7, 8]


async def test_successful_edit_preserves_null_assignment_role(
    tenant: dict[str, str],
    client: AsyncClient,
) -> None:
    seed = _seed_schedule(tenant["id"], first_assignment_role_id=None)
    response = await client.patch(
        f"/api/v1/schedules/{seed.request_id}/assignments",
        headers=_headers(tenant["token"]),
        json={
            "base_version": 7,
            "operations": [
                {
                    "action": "add",
                    "nurse_id": seed.nurse_ids[2],
                    "date": _tomorrow(),
                    "shift_template_id": seed.day_shift_id,
                    "role_id": "",
                }
            ],
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["version"] == 8
    result = await client.get(
        f"/api/v1/schedules/{seed.request_id}/result?version=8",
        headers=_headers(tenant["token"]),
    )
    assert result.status_code == 200, result.text
    assert len(result.json()["assignments"]) == 3
    result_assignments = sorted(
        result.json()["assignments"],
        key=lambda assignment: assignment["nurse_id"],
    )
    roles_by_nurse = {
        assignment["nurse_id"]: assignment["role_id"]
        for assignment in result_assignments
    }
    assert roles_by_nurse == {
        seed.nurse_ids[0]: None,
        seed.nurse_ids[1]: seed.role_id,
        seed.nurse_ids[2]: None,
    }


def _tomorrow() -> str:
    return (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
