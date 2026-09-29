"""B3-9 cancellation endpoint and worker state-machine tests."""

from __future__ import annotations

import datetime
import os
import uuid
from collections.abc import AsyncGenerator, Callable, Iterator
from types import SimpleNamespace

import httpx
import psycopg2
import pyotp
import pytest
from pytest import MonkeyPatch

from app.core.database import engine
from app.models.enums import ScheduleStatus
from app.scheduling.exceptions import ScheduleCancelledError
from app.tasks.schedule_tasks import (
    _CancellationChecker,
    _run_generation,
    _set_request_status,
)
from app.tests.conftest import SUPER_ADMIN_TEST_MFA_SECRET

API = os.environ.get("API_BASE_URL", "http://localhost:8000")
SUPER_EMAIL = "admin@example.com"
DB = {
    "dbname": os.environ.get("POSTGRES_DB", "nurse_scheduler"),
    "user": os.environ.get("POSTGRES_USER", "nurse"),
    "password": os.environ.get("POSTGRES_PASSWORD", "nurse"),
    "host": os.environ.get("POSTGRES_HOST", "db"),
}


@pytest.fixture(autouse=True)
async def _dispose_database_engine() -> AsyncGenerator[None, None]:
    await engine.dispose()
    yield
    await engine.dispose()


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def super_token() -> str:
    response = httpx.post(
        f"{API}/api/v1/auth/login",
        json={
            "email": SUPER_EMAIL,
            "password": os.environ.get("SUPER_ADMIN_PASSWORD", "changeme123"),
            "totp_code": pyotp.TOTP(SUPER_ADMIN_TEST_MFA_SECRET).now(),
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert isinstance(payload["access_token"], str)
    return payload["access_token"]


@pytest.fixture(scope="module")
def tenant(super_token: str) -> Iterator[dict[str, str]]:
    slug = f"b39-{uuid.uuid4().hex[:10]}"
    response = httpx.post(
        f"{API}/api/v1/tenants",
        headers=_h(super_token),
        json={"name": f"B3-9 {slug}", "slug": slug},
    )
    assert response.status_code == 201, response.text
    tenant_id = response.json()["id"]
    email = f"admin@{slug}.example.com"
    response = httpx.post(
        f"{API}/api/v1/tenants/{tenant_id}/admin",
        headers=_h(super_token),
        json={
            "email": email,
            "password": "password123",
            "first_name": "Cancel",
            "last_name": "Admin",
            "role": "tenant_admin",
        },
    )
    assert response.status_code == 201, response.text
    login = httpx.post(
        f"{API}/api/v1/auth/login", json={"email": email, "password": "password123"}
    )
    assert login.status_code == 200, login.text
    admin_token = login.json()["access_token"]
    assert isinstance(admin_token, str)

    scheduler_emails = [f"s1@{slug}.example.com", f"s2@{slug}.example.com"]
    scheduler_tokens: list[str] = []
    for email in scheduler_emails:
        response = httpx.post(
            f"{API}/api/v1/auth/users",
            headers=_h(admin_token),
            json={
                "email": email,
                "password": "password123",
                "first_name": "Schedule",
                "last_name": "Owner",
                "role": "scheduler",
            },
        )
        assert response.status_code == 201, response.text
        login = httpx.post(
            f"{API}/api/v1/auth/login", json={"email": email, "password": "password123"}
        )
        assert login.status_code == 200, login.text
        token = login.json()["access_token"]
        assert isinstance(token, str)
        scheduler_tokens.append(token)

    me = httpx.get(f"{API}/api/v1/auth/me", headers=_h(scheduler_tokens[0]))
    assert me.status_code == 200, me.text
    scheduler_id = me.json()["id"]
    assert isinstance(scheduler_id, str)

    yield {
        "id": tenant_id,
        "token": admin_token,
        "scheduler_token": scheduler_tokens[0],
        "other_scheduler_token": scheduler_tokens[1],
        "scheduler_id": scheduler_id,
    }
    response = httpx.delete(
        f"{API}/api/v1/tenants/{tenant_id}", headers=_h(super_token)
    )
    assert response.status_code == 204, response.text


def _insert_request(tenant_id: str, status: str, requested_by: str | None = None) -> str:
    request_id = str(uuid.uuid4())
    with psycopg2.connect(**DB) as connection, connection.cursor() as cursor:
        cursor.execute("SET app.is_super = '1'")
        cursor.execute(
            "INSERT INTO schedule_requests (id, tenant_id, period_start, period_days,"
            " request_date, daily_sequence, status, task_id, requested_by)"
            " VALUES (%s, %s, %s, 1, %s, %s, %s, %s, %s)",
            (
                request_id,
                tenant_id,
                datetime.date(2026, 10, 1),
                datetime.date.today(),
                uuid.uuid4().int % 1_000_000_000,
                status,
                str(uuid.uuid4()),
                requested_by,
            ),
        )
    return request_id


async def test_worker_status_guard_preserves_cancellation(
    tenant: dict[str, str],
) -> None:
    request_id = _insert_request(tenant["id"], "PENDING")
    await _set_request_status(
        request_id, tenant["id"], ScheduleStatus.RUNNING, set_started=True
    )
    schedule_id = str(uuid.uuid4())
    with psycopg2.connect(**DB) as connection, connection.cursor() as cursor:
        cursor.execute("SET app.is_super = '1'")
        cursor.execute(
            "INSERT INTO schedules (id, tenant_id, request_id, period_start,"
            " period_days, outcome, version)"
            " VALUES (%s, %s, %s, %s, 1, 'OPTIMAL', 1)",
            (
                schedule_id,
                tenant["id"],
                request_id,
                datetime.date(2026, 10, 1),
            ),
        )
    await _set_request_status(
        request_id,
        tenant["id"],
        ScheduleStatus.CANCELLED,
        error_message="排班任务已被取消",
    )
    await _set_request_status(
        request_id,
        tenant["id"],
        ScheduleStatus.FAILED,
        error_message="late worker result",
        set_completed=True,
    )

    with psycopg2.connect(**DB) as connection, connection.cursor() as cursor:
        cursor.execute("SET app.is_super = '1'")
        cursor.execute(
            "SELECT status, error_message FROM schedule_requests WHERE id = %s",
            (request_id,),
        )
        status, error_message = cursor.fetchone()
    assert status == "CANCELLED"
    assert error_message == "排班任务已被取消"

    with psycopg2.connect(**DB) as connection, connection.cursor() as cursor:
        cursor.execute("SET app.is_super = '1'")
        cursor.execute(
            "SELECT count(*) FROM schedules WHERE request_id = %s",
            (request_id,),
        )
        assert cursor.fetchone()[0] == 0


async def test_run_generation_preserves_cancelled_result(
    tenant: dict[str, str],
    monkeypatch: MonkeyPatch,
) -> None:
    request_id = _insert_request(tenant["id"], "RUNNING")
    statuses: list[ScheduleStatus] = []

    class CancelledEngine:
        def __init__(self, *_args: object) -> None:
            pass

        def solve(
            self,
            _progress: object,
            should_stop: Callable[[], bool] | None = None,
        ) -> None:
            assert should_stop is not None
            if should_stop():
                raise ScheduleCancelledError("Schedule generation was cancelled")

    class Checker:
        def __init__(self, _request_id: str) -> None:
            pass

        def __call__(self) -> bool:
            return True

        def close(self) -> None:
            return None

    async def fake_header(
        _request_id: str,
    ) -> tuple[str, datetime.date, int, dict[str, object], None, None, None]:
        return ("tenant-id", datetime.date(2026, 10, 1), 1, {}, None, None, None)

    async def fake_load_domain_data(
        *_args: object,
        **_kwargs: object,
    ) -> SimpleNamespace:
        return SimpleNamespace(nurses=[object()], shift_templates=[object()])

    async def fake_set_status(
        _request_id: str,
        _tenant_id: str,
        status_value: ScheduleStatus,
        **_kwargs: object,
    ) -> None:
        statuses.append(status_value)

    monkeypatch.setattr(
        "app.tasks.schedule_tasks._load_request_header", fake_header
    )
    async def fake_fail_stale_redelivery(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(
        "app.tasks.schedule_tasks._fail_stale_redelivery",
        fake_fail_stale_redelivery,
    )
    monkeypatch.setattr(
        "app.tasks.schedule_tasks.load_domain_data", fake_load_domain_data
    )
    monkeypatch.setattr("app.tasks.schedule_tasks._CancellationChecker", Checker)
    monkeypatch.setattr("app.tasks.schedule_tasks.RosterCPModel", CancelledEngine)
    monkeypatch.setattr(
        "app.tasks.schedule_tasks._set_request_status", fake_set_status
    )

    await _run_generation(request_id)
    assert statuses == [ScheduleStatus.RUNNING, ScheduleStatus.CANCELLED]


def test_admin_can_cancel_pending_and_terminal_is_conflict(
    tenant: dict[str, str],
    super_token: str,
) -> None:
    request_id = _insert_request(tenant["id"], "PENDING")
    cancelled = httpx.post(
        f"{API}/api/v1/schedules/{request_id}/cancel",
        headers=_h(tenant["token"]),
    )
    assert cancelled.status_code == 200, cancelled.text
    body = cancelled.json()
    assert body["status"] == "cancelled"
    assert body["error_message"] == "排班任务已被取消"
    assert body["stats"]["cancelled_by"]

    repeat = httpx.post(
        f"{API}/api/v1/schedules/{request_id}/cancel",
        headers=_h(tenant["token"]),
    )
    assert repeat.status_code == 409, repeat.text

    completed_id = _insert_request(tenant["id"], "COMPLETED")
    completed = httpx.post(
        f"{API}/api/v1/schedules/{completed_id}/cancel",
        headers=_h(super_token),
    )
    assert completed.status_code == 409, completed.text


def test_scheduler_can_cancel_only_own_request(tenant: dict[str, str]) -> None:
    own_request_id = _insert_request(
        tenant["id"], "PENDING", requested_by=tenant["scheduler_id"]
    )
    other_request_id = _insert_request(tenant["id"], "PENDING")

    denied = httpx.post(
        f"{API}/api/v1/schedules/{other_request_id}/cancel",
        headers=_h(tenant["scheduler_token"]),
    )
    assert denied.status_code == 403, denied.text

    allowed = httpx.post(
        f"{API}/api/v1/schedules/{own_request_id}/cancel",
        headers=_h(tenant["scheduler_token"]),
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["status"] == "cancelled"


def test_cancellation_checker_reads_cancelled_state(tenant: dict[str, str]) -> None:
    request_id = _insert_request(tenant["id"], "RUNNING")
    checker = _CancellationChecker(request_id)
    try:
        assert checker() is False

        with psycopg2.connect(**DB) as connection, connection.cursor() as cursor:
            cursor.execute("SET app.is_super = '1'")
            cursor.execute(
                "UPDATE schedule_requests SET status = 'CANCELLED' WHERE id = %s",
                (request_id,),
            )

        checker._next_check = 0.0
        assert checker() is True
    finally:
        checker.close()
