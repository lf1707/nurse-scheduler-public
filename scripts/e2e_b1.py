"""End-to-end verification for the v0.2.15 B1 remediation batch."""

from __future__ import annotations

import os
import time
import uuid

import httpx
import psycopg2
import pyotp

from app.core.security import hash_password

SUPER_ADMIN_TEST_MFA_SECRET = os.environ["SUPER_ADMIN_TEST_MFA_SECRET"]

API = "http://localhost:8000/api/v1"
SUPER_EMAIL = os.environ.get("SUPER_ADMIN_EMAIL", "admin@example.com")
SUPER_PASSWORD = os.environ.get("SUPER_ADMIN_PASSWORD", "changeme123")


def db_connection():
    return psycopg2.connect(
        dbname=os.environ.get("POSTGRES_DB", "nurse_scheduler"),
        user=os.environ.get("APP_DB_USER", "nurse_app"),
        password=os.environ.get("APP_DB_PASSWORD", "nurse_app_dev_pass"),
        host=os.environ.get("POSTGRES_HOST", "db"),
    )


def create_unenrolled_super_admin() -> tuple[str, str, str]:
    email = f"unenrolled-{uuid.uuid4().hex[:8]}@example.com"
    user_id = str(uuid.uuid4())
    with db_connection() as connection, connection.cursor() as cursor:
        cursor.execute("SET app.is_super = '1'")
        cursor.execute(
            """
            INSERT INTO users (
                id, email, hashed_password, first_name, last_name, role, is_active
            ) VALUES (%s, %s, %s, 'Unenrolled', 'Super', 'SUPER_ADMIN', true)
            """,
            (user_id, email, hash_password("password123")),
        )
    return user_id, email, "password123"


def delete_user(user_id: str) -> None:
    with db_connection() as connection, connection.cursor() as cursor:
        cursor.execute("SET app.is_super = '1'")
        cursor.execute("DELETE FROM users WHERE id = %s", (user_id,))


def login(email: str, password: str, **extra: str) -> httpx.Response:
    return httpx.post(f"{API}/auth/login", json={"email": email, "password": password, **extra})


def test_tenant(super_headers: dict[str, str]) -> dict[str, object]:
    response = httpx.post(
        f"{API}/admin/test-tenant",
        headers=super_headers,
        json={
            "nurse_count": 3,
            "role_count": 1,
            "day_group_count": 1,
            "shift_count": 1,
            "skill_mix_rule_count": 1,
            "shift_sequence_rule_count": 0,
        },
    )
    response.raise_for_status()
    return response.json()


def generate_schedule(headers: dict[str, str]) -> tuple[str, dict[str, object]]:
    response = httpx.post(
        f"{API}/schedules/generate",
        headers=headers,
        json={"period_start": "2026-11-01", "period_days": 7},
    )
    response.raise_for_status()
    request_id = response.json()["id"]
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        response = httpx.get(f"{API}/schedules/{request_id}", headers=headers)
        response.raise_for_status()
        payload = response.json()
        if payload["status"] in {"completed", "failed", "cancelled"}:
            return request_id, payload
        time.sleep(0.2)
    raise AssertionError("schedule did not finish")


def main() -> None:
    super_login = login(
        SUPER_EMAIL,
        SUPER_PASSWORD,
        totp_code=pyotp.TOTP(SUPER_ADMIN_TEST_MFA_SECRET).now(),
    )
    super_login.raise_for_status()
    super_headers = {"Authorization": f"Bearer {super_login.json()['access_token']}"}

    user_id, email, password = create_unenrolled_super_admin()
    try:
        response = login(email, password)
        assert response.status_code == 200
        headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
        blocked = httpx.get(f"{API}/tenants", headers=headers)
        assert blocked.status_code == 403
        setup = httpx.post(f"{API}/auth/me/mfa/setup", headers=headers)
        assert setup.status_code == 200
        secret = setup.json()["provisioning_uri"].split("secret=", 1)[1].split("&", 1)[0]
        confirm = httpx.post(
            f"{API}/auth/me/mfa/confirm",
            headers=headers,
            json={"code": pyotp.TOTP(secret).now()},
        )
        assert confirm.status_code == 200
        assert blocked.status_code == 403
        print("unenrolled-super-admin-mfa-gate: ok")

    finally:
        delete_user(user_id)

    tenant = test_tenant(super_headers)
    tenant_id = str(tenant["tenant_id"])
    try:
        admin_login = login(str(tenant["admin_email"]), str(tenant["admin_password"]))
        admin_login.raise_for_status()
        admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}
        request_id, schedule = generate_schedule(admin_headers)
        assert schedule["status"] == "completed", schedule
        result = httpx.get(
            f"{API}/schedules/{request_id}/result", headers=admin_headers
        )
        result.raise_for_status()
        assert result.json()["assignments"]
        print("schedule-generation: ok")
    finally:
        cleanup = httpx.delete(
            f"{API}/admin/test-tenant/{tenant_id}", headers=super_headers
        )
        cleanup.raise_for_status()
        print("cleanup:", cleanup.status_code)


if __name__ == "__main__":
    main()
