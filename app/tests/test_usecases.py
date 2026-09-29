"""Comprehensive use-case suite: walks every REST endpoint through its
happy path AND its error/permission paths.

Grouped by domain: auth, tenants, roles/skills, day-groups/shifts,
nurses (CRUD + contract + leaves), rules (skill-mix + shift-sequence),
schedules (validation + 404s + pagination + full generate), permission
matrix, and frontend pages.

Runs against the live API (same mode as test_api_e2e.py).
"""

from __future__ import annotations

import asyncio
import csv
import os
import re
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import psycopg2
import pyotp
import pytest

from app.api.v1.schedules import _calendar_months
from app.core.mfa import generate_mfa_secret, generate_recovery_codes
from app.core.rate_limit import email_fingerprint
from app.core.security import hash_password
from app.tests.conftest import SUPER_ADMIN_TEST_MFA_SECRET

BASE = os.environ.get("API_BASE_URL", "http://localhost:8000")
API = f"{BASE}/api/v1"
SUPER_EMAIL = "admin@example.com"
SUPER_PASSWORD = os.environ.get("SUPER_ADMIN_PASSWORD", "changeme123")
UNIQ = f"uc{int(time.time())}{uuid.uuid4().hex[:4]}"


def _h(token: Any) -> dict[str, str]:

    return {"Authorization": f"Bearer {token}"}


def _login(email: str, password: str) -> httpx.Response:
    body = {"email": email, "password": password}
    if email == SUPER_EMAIL:
        body["totp_code"] = pyotp.TOTP(SUPER_ADMIN_TEST_MFA_SECRET).now()
    return httpx.post(f"{API}/auth/login", json=body)


def _db_connection() -> Any:

    return psycopg2.connect(
        dbname=os.environ.get("POSTGRES_DB", "nurse_scheduler"),
        user=os.environ.get("APP_DB_USER", "nurse_app"),
        password=os.environ.get("APP_DB_PASSWORD", "nurse_app_dev_pass"),
        host=os.environ.get("POSTGRES_HOST", "db"),
    )


def _create_mfa_user(role: str) -> tuple[str, str, str, list[str]]:
    email = f"{role}-{UNIQ}-{uuid.uuid4().hex[:6]}@example.com"
    user_id = str(uuid.uuid4())
    secret = generate_mfa_secret()
    codes, digest = generate_recovery_codes()
    with _db_connection() as connection, connection.cursor() as cursor:
        cursor.execute("SET app.is_super = '1'")
        cursor.execute(
            """
                INSERT INTO users (
                    id, email, hashed_password, first_name, last_name, role,
                    is_active, mfa_enabled, mfa_secret, mfa_recovery_hash
                ) VALUES (%s, %s, %s, 'Mfa', 'Test', %s, true, true, %s, %s)
                """,
            (user_id, email, hash_password("password123"), role, secret, digest),
        )
    return user_id, email, secret, codes


def _delete_user(user_id: str) -> None:
    with _db_connection() as connection, connection.cursor() as cursor:
        cursor.execute("SET app.is_super = '1'")
        cursor.execute("DELETE FROM users WHERE id = %s", (user_id,))


def _clear_login_failures(email: str) -> None:
    import redis

    client = redis.Redis.from_url(
        os.environ.get("REDIS_URL", "redis://redis:6379/0"), decode_responses=True
    )
    client.delete(
        "login_failure:ip:127.0.0.1",
        f"login_failure:ip_email:127.0.0.1:{email_fingerprint(email)}",
    )
    client.close()


# ---------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def super_tok() -> str:
    r = _login(SUPER_EMAIL, SUPER_PASSWORD)
    assert r.status_code == 200, r.text
    return cast(str, r.json()["access_token"])


@pytest.fixture(scope="module")
def tenant(super_tok: Any) -> Iterator[dict[str, Any]]:

    """Fresh isolated tenant + admin + viewer tokens."""
    slug = UNIQ
    r = httpx.post(f"{API}/tenants", headers=_h(super_tok),
                   json={"name": f"UC {UNIQ}", "slug": slug})
    assert r.status_code == 201, r.text
    tid = r.json()["id"]

    admin_email = f"admin@{UNIQ}.example.com"
    r = httpx.post(f"{API}/tenants/{tid}/admin", headers=_h(super_tok),
                   json={"email": admin_email, "password": "password123",
                         "first_name": "Ad", "last_name": "Min", "role": "tenant_admin"})
    assert r.status_code == 201, r.text

    viewer_email = f"viewer@{UNIQ}.example.com"
    r = httpx.post(f"{API}/auth/users", headers=_h(_login(admin_email, "password123").json()["access_token"]),
                   json={"email": viewer_email, "password": "password123",
                         "first_name": "V", "last_name": "Er", "role": "viewer"})
    assert r.status_code == 201, r.text

    # Active subscription so schedule generation passes the gate.
    r = httpx.put(f"{API}/subscriptions/{tid}", headers=_h(super_tok),
                  json={"plan": "max"})
    assert r.status_code == 200, r.text

    admin_token = _login(admin_email, "password123").json()["access_token"]
    r = httpx.post(f"{API}/roles", headers=_h(admin_token),
                   json={"name": "RN", "code": "rn"})
    assert r.status_code == 201, r.text

    yield {
        "id": tid, "slug": slug,
        "admin_token": admin_token,
        "viewer_token": _login(viewer_email, "password123").json()["access_token"],
        "role_id": r.json()["id"],
    }
    deleted = httpx.delete(f"{API}/tenants/{tid}", headers=_h(super_tok))
    assert deleted.status_code == 204, deleted.text


# ---------------------------------------------------------------- auth

class TestAuth:
    def test_login_wrong_password(self) -> None:

        assert _login(SUPER_EMAIL, "wrong-password").status_code == 401

    def test_login_unknown_email(self) -> None:

        assert _login(f"nobody-{UNIQ}@x.example.com", "password123").status_code == 401

    def test_login_malformed_body(self) -> None:

        r = httpx.post(f"{API}/auth/login", json={"email": "not-an-email", "password": "x"})
        assert r.status_code == 422

    def test_invalid_totp_reaches_login_lockout(self, tenant: Any) -> None:

        user_id, email, _secret, _codes = _create_mfa_user("TENANT_ADMIN")
        payload = {
            "email": email,
            "password": "password123",
            "totp_code": "000000",
        }
        try:
            for _ in range(5):
                response = httpx.post(f"{API}/auth/login", json=payload)
                assert response.status_code == 401, response.text

            blocked = httpx.post(f"{API}/auth/login", json=payload)
            assert blocked.status_code == 429
            login = httpx.post(f"{API}/auth/login", json={"email": email, "password": "password123"})
            assert login.status_code == 429
        finally:
            _delete_user(user_id)
            _clear_login_failures(email)

    def test_recovery_codes_rotate_and_reject_old_set(self, tenant: Any) -> None:

        user_id, email, _secret, recovery_codes = _create_mfa_user("TENANT_ADMIN")
        try:
            required = _login(email, "password123")
            assert required.status_code == 428

            invalid = httpx.post(
                f"{API}/auth/login",
                json={
                    "email": email,
                    "password": "password123",
                    "recovery_codes": [recovery_codes[0]],
                },
            )
            assert invalid.status_code == 401

            valid = httpx.post(
                f"{API}/auth/login",
                json={
                    "email": email,
                    "password": "password123",
                    "recovery_codes": recovery_codes,
                },
            )
            assert valid.status_code == 200, valid.text
            new_codes = valid.json()["recovery_codes"]
            assert new_codes

            old = httpx.post(
                f"{API}/auth/login",
                json={
                    "email": email,
                    "password": "password123",
                    "recovery_codes": recovery_codes,
                },
            )
            assert old.status_code == 401
        finally:
            _delete_user(user_id)
            _clear_login_failures(email)

    def test_super_admin_mfa_reset_endpoint(self, tenant: Any, super_tok: Any) -> None:

        user_id, email, _secret, _codes = _create_mfa_user("TENANT_ADMIN")
        try:
            forbidden = httpx.post(
                f"{API}/admin/users/{user_id}/mfa/reset",
                headers=_h(tenant["admin_token"]),
            )
            assert forbidden.status_code == 403

            reset = httpx.post(f"{API}/admin/users/{user_id}/mfa/reset", headers=_h(super_tok))
            assert reset.status_code == 204, reset.text
            assert _login(email, "password123").status_code == 200
        finally:
            _delete_user(user_id)
            _clear_login_failures(email)

    def test_unenrolled_super_admin_must_enroll_before_business_endpoints(self) -> None:

        user_id, email, secret, _codes = _create_mfa_user("TENANT_ADMIN")
        with _db_connection() as connection, connection.cursor() as cursor:
            cursor.execute("SET app.is_super = '1'")
            cursor.execute(
                "UPDATE users SET tenant_id = NULL, role = 'SUPER_ADMIN' WHERE id = %s",
                (user_id,),
            )
            cursor.execute("UPDATE users SET mfa_enabled = false WHERE id = %s", (user_id,))
        try:
            session = _login(email, "password123")
            assert session.status_code == 200, session.text
            headers = _h(session.json()["access_token"])

            blocked = httpx.get(f"{API}/admin/site-info", headers=headers)
            assert blocked.status_code == 403
            assert blocked.json()["detail"]["reason"] == "mfa_enrollment_required"

            setup = httpx.post(f"{API}/auth/me/mfa/setup", headers=headers)
            assert setup.status_code == 200, setup.text
            active_secret = setup.json()["provisioning_uri"].split("secret=", 1)[1].split("&", 1)[0]
            confirm = httpx.post(
                f"{API}/auth/me/mfa/confirm",
                headers=headers,
                json={"code": pyotp.TOTP(active_secret).now()},
            )
            assert confirm.status_code == 200, confirm.text

            allowed = httpx.get(f"{API}/admin/site-info", headers=headers)
            assert allowed.status_code == 200, allowed.text
        finally:
            _delete_user(user_id)

    def test_me(self, tenant: Any) -> None:

        r = httpx.get(f"{API}/auth/me", headers=_h(tenant["admin_token"]))
        assert r.status_code == 200
        assert r.json()["role"] == "tenant_admin"
        assert r.json()["tenant_id"] == tenant["id"]

    def test_me_no_token(self) -> None:

        assert httpx.get(f"{API}/auth/me").status_code == 401

    def test_me_garbage_token(self) -> None:

        r = httpx.get(f"{API}/auth/me", headers={"Authorization": "Bearer garbage"})
        assert r.status_code == 401

    def test_refresh_flow(self, tenant: Any) -> None:

        email = f"admin@{UNIQ}.example.com"
        old_refresh = _login(email, "password123").json()["refresh_token"]
        refreshed = httpx.post(
            f"{API}/auth/refresh", json={"refresh_token": old_refresh}
        )
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["access_token"]
        assert refreshed.json()["refresh_token"]

        replay = httpx.post(
            f"{API}/auth/refresh", json={"refresh_token": old_refresh}
        )
        family_after_replay = httpx.post(
            f"{API}/auth/refresh",
            json={"refresh_token": refreshed.json()["refresh_token"]},
        )
        assert replay.status_code == 401
        assert family_after_replay.status_code == 401
        assert httpx.post(f"{API}/auth/refresh", json={"refresh_token": "bad"}).status_code == 401

    def test_concurrent_refresh_grants_exactly_one_token(self, tenant: Any) -> None:

        email = f"admin@{UNIQ}.example.com"
        old_refresh = _login(email, "password123").json()["refresh_token"]

        async def refresh_once() -> tuple[int, str]:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{API}/auth/refresh", json={"refresh_token": old_refresh}
                )
                return response.status_code, response.text

        async def refresh_twice() -> list[tuple[int, str]]:
            return await asyncio.gather(
                *(refresh_once() for _ in range(2)), return_exceptions=False
            )

        responses = asyncio.run(refresh_twice())
        success_responses = [response for response in responses if response[0] == 200]
        replay_responses = [response for response in responses if response[0] == 401]

        assert len(success_responses) == 1, responses
        assert len(replay_responses) == 1, responses

    def test_logout_revokes_only_its_device_family(
    self,
    tenant: Any,
    super_tok: Any,
) -> None:

        email = f"admin@{UNIQ}.example.com"
        session_a = _login(email, "password123").json()
        session_b = _login(email, "password123").json()
        session_c = _login(email, "password123").json()
        actor = httpx.get(
            f"{API}/auth/me", headers=_h(session_a["access_token"])
        ).json()["id"]

        logout = httpx.post(
            f"{API}/auth/logout",
            json={"refresh_token": session_a["refresh_token"]},
        )
        assert logout.status_code == 204, logout.text

        assert (
            httpx.get(f"{API}/auth/me", headers=_h(session_a["access_token"])).status_code
            == 401
        )
        assert (
            httpx.post(
                f"{API}/auth/refresh",
                json={"refresh_token": session_a["refresh_token"]},
            ).status_code
            == 401
        )

        rotated_b = httpx.post(
            f"{API}/auth/refresh", json={"refresh_token": session_b["refresh_token"]}
        )
        assert rotated_b.status_code == 200, rotated_b.text
        replay_b = httpx.post(
            f"{API}/auth/refresh",
            json={"refresh_token": session_b["refresh_token"]},
        )
        rotated_b_descendant = httpx.post(
            f"{API}/auth/refresh",
            json={"refresh_token": rotated_b.json()["refresh_token"]},
        )
        assert replay_b.status_code == 401
        assert rotated_b_descendant.status_code == 401
        assert (
            httpx.get(
                f"{API}/auth/me", headers=_h(rotated_b.json()["access_token"])
            ).status_code
            == 401
        )
        assert (
            httpx.get(f"{API}/auth/me", headers=_h(session_c["access_token"])).status_code
            == 200
        )

        audit = httpx.get(
            f"{API}/admin/audit-events",
            headers=_h(super_tok),
            params={"actor_id": actor, "page_size": 50},
        )
        assert audit.status_code == 200, audit.text
        actions = {event["action"] for event in audit.json()["items"]}
        assert "auth.logout" in actions
        assert "auth.refresh.reuse_detected" in actions

    def test_password_change_revokes_access_and_refresh_tokens(self, tenant: Any) -> None:

        email = f"revoke-{UNIQ}@example.com"
        created = httpx.post(
            f"{API}/auth/users",
            headers=_h(tenant["admin_token"]),
            json={
                "email": email,
                "password": "password123",
                "first_name": "Revoke",
                "last_name": "Me",
                "role": "viewer",
            },
        )
        assert created.status_code == 201, created.text

        session = _login(email, "password123").json()
        changed = httpx.patch(
            f"{API}/auth/users/{created.json()['id']}",
            headers=_h(tenant["admin_token"]),
            json={"password": "password456"},
        )
        assert changed.status_code == 200, changed.text

        me = httpx.get(f"{API}/auth/me", headers=_h(session["access_token"]))
        refresh = httpx.post(
            f"{API}/auth/refresh", json={"refresh_token": session["refresh_token"]}
        )
        assert me.status_code == 401
        assert refresh.status_code == 401
        assert _login(email, "password456").status_code == 200

    def test_create_user_duplicate_email(self, tenant: Any) -> None:

        r = httpx.post(f"{API}/auth/users", headers=_h(tenant["admin_token"]),
                       json={"email": f"viewer@{UNIQ}.example.com", "password": "password123",
                             "first_name": "D", "last_name": "Up", "role": "viewer"})
        assert r.status_code == 409

    def test_create_user_short_password(self, tenant: Any) -> None:

        r = httpx.post(f"{API}/auth/users", headers=_h(tenant["admin_token"]),
                       json={"email": f"short-{UNIQ}@x.example.com", "password": "short",
                             "first_name": "S", "last_name": "P", "role": "viewer"})
        assert r.status_code == 422

    def test_tenant_admin_can_list_users(self, tenant: Any) -> None:

        r = httpx.get(f"{API}/auth/users", headers=_h(tenant["admin_token"]))
        assert r.status_code == 200, r.text
        emails = {user["email"] for user in r.json()}
        assert f"admin@{UNIQ}.example.com" in emails
        assert f"viewer@{UNIQ}.example.com" in emails
        assert all(user["tenant_id"] == tenant["id"] for user in r.json())

    def test_user_listing_and_super_admin_creation_permissions(
    self,
    tenant: Any,
    super_tok: Any,
) -> None:

        assert httpx.get(f"{API}/auth/users", headers=_h(tenant["viewer_token"])).status_code == 403
        assert httpx.get(f"{API}/auth/users", headers=_h(super_tok)).status_code == 400

        listed = httpx.get(f"{API}/auth/users", headers=_h(super_tok),
                           params={"tenant_id": tenant["id"]})
        assert listed.status_code == 200, listed.text
        assert all(user["tenant_id"] == tenant["id"] for user in listed.json())
        assert any(user["role"] == "tenant_admin" for user in listed.json())

        created = httpx.post(
            f"{API}/auth/users",
            headers=_h(super_tok),
            params={"tenant_id": tenant["id"]},
            json={"email": f"super-created-{UNIQ}@example.com", "password": "password123",
                  "first_name": "Super", "last_name": "Created", "role": "viewer"},
        )
        assert created.status_code == 201, created.text
        assert created.json()["tenant_id"] == tenant["id"]

        patched = httpx.patch(
            f"{API}/auth/users/{created.json()['id']}",
            headers=_h(super_tok),
            params={"tenant_id": tenant["id"]},
            json={"first_name": "Renamed"},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["first_name"] == "Renamed"

        r = httpx.post(f"{API}/auth/users", headers=_h(tenant["admin_token"]),
                       json={"email": f"super-{UNIQ}@x.example.com", "password": "password123",
                             "first_name": "Super", "last_name": "Denied", "role": "super_admin"})
        assert r.status_code == 403

    def test_create_nurse_user_linked_to_nurse_record(
    self,
    tenant: Any,
    super_tok: Any,
    nurses: Any,
) -> None:

        email = f"nurse-login@{UNIQ}.example.com"
        r = httpx.post(f"{API}/auth/users", headers=_h(tenant["admin_token"]),
                       json={"email": email, "password": "password123",
                             "first_name": nurses[1]["first_name"],
                             "last_name": nurses[1]["last_name"], "role": "nurse",
                      "nurse_id": nurses[1]["id"]})
        assert r.status_code == 201, r.text
        assert r.json()["nurse_id"] == nurses[1]["id"]
        nurse_user_id = r.json()["id"]

        me = _login(email, "password123")
        assert me.status_code == 200

        renamed = httpx.patch(
            f"{API}/auth/users/{nurse_user_id}",
            headers=_h(super_tok),
            params={"tenant_id": tenant["id"]},
            json={"first_name": "Renamed", "nurse_id": nurses[1]["id"]},
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["first_name"] == "Renamed"
        assert renamed.json()["nurse_id"] == nurses[1]["id"]

        duplicate = httpx.post(f"{API}/auth/users", headers=_h(tenant["admin_token"]),
                               json={"email": f"nurse-login-2@{UNIQ}.example.com",
                                     "password": "password123", "first_name": "Second",
                                     "last_name": "Account", "role": "nurse",
                                     "nurse_id": nurses[1]["id"]})
        assert duplicate.status_code == 409

    def test_nurse_user_link_validation(self, tenant: Any, nurses: Any) -> None:

        missing_link = httpx.post(f"{API}/auth/users", headers=_h(tenant["admin_token"]),
                                  json={"email": f"unlinked-nurse@{UNIQ}.example.com",
                                        "password": "password123", "first_name": "Unlinked",
                                        "last_name": "Nurse", "role": "nurse"})
        assert missing_link.status_code == 201, missing_link.text
        assert missing_link.json()["nurse_id"]

        created_nurse = httpx.get(
            f"{API}/nurses/{missing_link.json()['nurse_id']}",
            headers=_h(tenant["admin_token"]),
        )
        assert created_nurse.status_code == 200
        assert created_nurse.json()["employee_id"] == "UNLINKED-NURSE"
        assert created_nurse.json()["user_email"] == f"unlinked-nurse@{UNIQ}.example.com"

        wrong_role = httpx.post(f"{API}/auth/users", headers=_h(tenant["admin_token"]),
                                json={"email": f"viewer-with-nurse@{UNIQ}.example.com",
                                      "password": "password123", "first_name": "Viewer",
                                      "last_name": "Nurse", "role": "viewer",
                              "nurse_id": nurses[0]["id"]})
        assert wrong_role.status_code == 400

    def test_update_user_profile_status_and_nurse_link(
    self,
    tenant: Any,
    nurses: Any,
) -> None:

        h = _h(tenant["admin_token"])
        email = f"editable-{UNIQ}@example.com"
        created = httpx.post(f"{API}/auth/users", headers=h, json={
            "email": email, "password": "password123", "first_name": "Edit",
            "last_name": "Me", "role": "scheduler"})
        assert created.status_code == 201, created.text
        user_id = created.json()["id"]

        updated = httpx.patch(f"{API}/auth/users/{user_id}", headers=h, json={
            "first_name": "Edited", "last_name": "User", "is_active": False,
            "password": "password456"})
        assert updated.status_code == 200, updated.text
        assert updated.json()["first_name"] == "Edited"
        assert updated.json()["is_active"] is False
        assert _login(email, "password456").status_code == 403

        reactivated = httpx.patch(f"{API}/auth/users/{user_id}", headers=h,
                                  json={"is_active": True})
        assert reactivated.status_code == 200
        assert reactivated.json()["is_active"] is True

        # System role is immutable after creation: sending role is ignored
        # (extra field) and the stored role never changes.
        changed_role = httpx.patch(f"{API}/auth/users/{user_id}", headers=h, json={
            "role": "nurse"})
        assert changed_role.status_code == 200
        assert changed_role.json()["role"] == "scheduler"
        after = httpx.get(f"{API}/auth/users", headers=h).json()
        assert next(user["role"] for user in after if user["id"] == user_id) == "scheduler"

        viewer = httpx.patch(f"{API}/auth/users/{user_id}",
                             headers=_h(tenant["viewer_token"]), json={"first_name": "No"})
        assert viewer.status_code == 403

        me = httpx.get(f"{API}/auth/me", headers=h).json()
        self_update = httpx.patch(f"{API}/auth/users/{me['id']}", headers=h,
                                  json={"is_active": False})
        assert self_update.status_code == 400

    def test_update_rejects_conflicting_or_missing_nurse_link(self, tenant: Any) -> None:

        h = _h(tenant["admin_token"])
        nurse_ids = []
        for index in range(3):
            created_nurse = httpx.post(f"{API}/nurses", headers=h, json={
                "employee_id": f"link-{UNIQ}-{index}",
                "first_name": f"Link{index}", "last_name": "Nurse",
                "role_ids": [tenant["role_id"]]})
            assert created_nurse.status_code == 201, created_nurse.text
            nurse_ids.append(created_nurse.json()["id"])

        users = []
        for index in range(2):
            created_user = httpx.post(f"{API}/auth/users", headers=h, json={
                "email": f"link-user-{UNIQ}-{index}@example.com",
                "password": "password123", "first_name": f"Link{index}",
                "last_name": "User", "role": "nurse",
                "nurse_id": nurse_ids[index]})
            assert created_user.status_code == 201, created_user.text
            users.append(created_user.json()["id"])

        conflict = httpx.patch(f"{API}/auth/users/{users[1]}", headers=h,
                               json={"nurse_id": nurse_ids[0]})
        assert conflict.status_code == 400
        assert "不可更改" in conflict.json()["detail"]

        cleared = httpx.patch(f"{API}/auth/users/{users[1]}", headers=h,
                              json={"nurse_id": None})
        assert cleared.status_code == 400
        assert "不可更改" in cleared.json()["detail"]

        changed = httpx.patch(f"{API}/auth/users/{users[1]}", headers=h,
                              json={"nurse_id": nurse_ids[2]})
        assert changed.status_code == 400
        assert "不可更改" in changed.json()["detail"]


# ---------------------------------------------------------------- tenants

class TestTenants:
    def test_create_duplicate_slug(self, super_tok: Any, tenant: Any) -> None:

        r = httpx.post(f"{API}/tenants", headers=_h(super_tok),
                       json={"name": "dup", "slug": tenant["slug"]})
        assert r.status_code == 409

    def test_create_invalid_slug(self, super_tok: Any) -> None:

        r = httpx.post(f"{API}/tenants", headers=_h(super_tok),
                       json={"name": "bad", "slug": "Invalid_Slug!"})
        assert r.status_code == 422

    def test_get_and_patch(self, super_tok: Any, tenant: Any) -> None:

        r = httpx.get(f"{API}/tenants/{tenant['id']}", headers=_h(super_tok))
        assert r.status_code == 200
        assert r.json()["slug"] == tenant["slug"]

        r = httpx.patch(f"{API}/tenants/{tenant['id']}", headers=_h(super_tok),
                        json={"name": f"UC {UNIQ} renamed"})
        assert r.status_code == 200
        assert r.json()["name"].endswith("renamed")

    def test_get_404(self, super_tok: Any) -> None:

        assert httpx.get(f"{API}/tenants/{uuid.uuid4()}", headers=_h(super_tok)).status_code == 404

    def test_patch_updates_tenant_admin_email(self, super_tok: Any) -> None:

        slug = f"email-{uuid.uuid4().hex[:10]}"
        tenant = httpx.post(f"{API}/tenants", headers=_h(super_tok), json={
            "name": "Email Change Tenant", "slug": slug})
        assert tenant.status_code == 201, tenant.text
        tenant_id = tenant.json()["id"]
        old_email = f"old-{slug}@example.com"
        new_email = f"new-{slug}@example.com"
        created = httpx.post(f"{API}/tenants/{tenant_id}/admin", headers=_h(super_tok), json={
            "email": old_email, "password": "password123",
            "first_name": "Email", "last_name": "Admin", "role": "tenant_admin"})
        assert created.status_code == 201, created.text

        loaded = httpx.get(f"{API}/tenants/{tenant_id}/admin", headers=_h(super_tok))
        assert loaded.status_code == 200, loaded.text
        assert loaded.json()["email"] == old_email

        patched = httpx.patch(f"{API}/tenants/{tenant_id}", headers=_h(super_tok), json={
            "name": "Email Change Tenant Renamed", "admin_email": new_email})
        assert patched.status_code == 200, patched.text
        assert patched.json()["name"] == "Email Change Tenant Renamed"
        loaded = httpx.get(f"{API}/tenants/{tenant_id}/admin", headers=_h(super_tok))
        assert loaded.status_code == 200, loaded.text
        assert loaded.json()["email"] == new_email
        assert _login(new_email, "password123").status_code == 200
        assert _login(old_email, "password123").status_code == 401

        deleted = httpx.delete(f"{API}/tenants/{tenant_id}", headers=_h(super_tok))
        assert deleted.status_code == 204

    def test_list_contains_ours(self, super_tok: Any, tenant: Any) -> None:

        r = httpx.get(f"{API}/tenants", headers=_h(super_tok), params={"page_size": 100})
        assert r.status_code == 200
        assert any(t["id"] == tenant["id"] for t in r.json()["items"])

    def test_tenant_admin_cannot_manage_tenants(self, tenant: Any) -> None:

        assert httpx.post(f"{API}/tenants", headers=_h(tenant["admin_token"]),
                          json={"name": "x", "slug": f"x-{UNIQ}"}).status_code == 403
        assert httpx.get(f"{API}/tenants", headers=_h(tenant["admin_token"])).status_code == 403

    def test_create_and_delete_tenant_cascades_data(self, super_tok: Any) -> None:

        slug=f"delete-{UNIQ}"
        r=httpx.post(f"{API}/tenants", headers=_h(super_tok),
                     json={"name":"Delete Tenant", "slug":slug})
        assert r.status_code==201, r.text
        tenant_id=r.json()["id"]

        admin_email=f"admin@{slug}.example.com"
        r=httpx.post(f"{API}/tenants/{tenant_id}/admin", headers=_h(super_tok),
                     json={"email":admin_email, "password":"password123",
                           "first_name":"Delete", "last_name":"Admin"})
        assert r.status_code==201, r.text
        admin_token=_login(admin_email, "password123").json()["access_token"]

        r=httpx.post(f"{API}/roles", headers=_h(admin_token),
                     json={"name":"Delete Role", "code":"DEL"})
        assert r.status_code==201, r.text

        r=httpx.patch(f"{API}/tenants/{tenant_id}", headers=_h(super_tok),
                      json={"is_active":False})
        assert r.status_code==200, r.text
        assert r.json()["is_active"] is False
        assert _login(admin_email, "password123").status_code==403
        assert httpx.get(f"{API}/roles", headers=_h(admin_token)).status_code==403

        r=httpx.patch(f"{API}/tenants/{tenant_id}", headers=_h(super_tok),
                      json={"is_active":True})
        assert r.status_code==200, r.text

        assert httpx.delete(f"{API}/tenants/{tenant_id}",
                            headers=_h(admin_token)).status_code==403
        r=httpx.delete(f"{API}/tenants/{tenant_id}", headers=_h(super_tok))
        assert r.status_code==204, r.text
        assert httpx.get(f"{API}/tenants/{tenant_id}",
                         headers=_h(super_tok)).status_code==404
        assert httpx.get(f"{API}/roles", headers=_h(admin_token)).status_code==401


# ---------------------------------------------------------------- roles / skills

@pytest.fixture(scope="module")
def roles(tenant: Any) -> dict[str, Any]:

    h = _h(tenant["admin_token"])
    out = {}
    for code in ("RN", "LV"):
        r = httpx.post(f"{API}/roles", headers=h, json={"name": code, "code": code})
        assert r.status_code == 201, r.text
        out[code] = r.json()["id"]
    return out


class TestRolesSkills:
    def test_role_create_and_code_non_unique(self, tenant: Any) -> None:

        r = httpx.post(f"{API}/roles", headers=_h(tenant["admin_token"]),
                       json={"name": "Head Nurse", "code": "HN"})
        assert r.status_code == 201
        r2 = httpx.post(f"{API}/roles", headers=_h(tenant["admin_token"]),
                        json={"name": "Head Nurse", "code": "HN"})
        assert r2.status_code == 201
        assert r2.json()["id"] != r.json()["id"]

    def test_role_list(self, tenant: Any, roles: Any) -> None:

        r = httpx.get(f"{API}/roles", headers=_h(tenant["admin_token"]))
        assert r.status_code == 200
        codes = {x["code"] for x in r.json()["items"]}
        assert {"RN", "LV"} <= codes

    def test_role_list_tenant_filter(self, tenant: Any, super_tok: Any) -> None:
        filtered = httpx.get(
            f"{API}/roles",
            headers=_h(super_tok),
            params={"tenant_id": tenant["id"]},
        )
        assert filtered.status_code == 200, filtered.text
        items = filtered.json()["items"]
        assert items
        assert {item["tenant_id"] for item in items} == {tenant["id"]}
        assert {"RN", "LV"} <= {item["code"] for item in items}

        forbidden = httpx.get(
            f"{API}/roles",
            headers=_h(tenant["admin_token"]),
            params={"tenant_id": str(uuid.uuid4())},
        )
        assert forbidden.status_code == 403

    def test_skill_crud(self, tenant: Any) -> None:

        h = _h(tenant["admin_token"])
        r = httpx.post(f"{API}/skills", headers=h, json={"name": "ICU", "code": "ICU"})
        assert r.status_code == 201
        r = httpx.get(f"{API}/skills", headers=h)
        assert r.status_code == 200
        assert any(s["code"] == "ICU" for s in r.json()["items"])

    def test_skill_list_tenant_filter(self, tenant: Any, super_tok: Any) -> None:
        filtered = httpx.get(
            f"{API}/skills",
            headers=_h(super_tok),
            params={"tenant_id": tenant["id"]},
        )
        assert filtered.status_code == 200, filtered.text
        items = filtered.json()["items"]
        assert items
        assert {item["tenant_id"] for item in items} == {tenant["id"]}
        assert "ICU" in {item["code"] for item in items}

        forbidden = httpx.get(
            f"{API}/skills",
            headers=_h(tenant["admin_token"]),
            params={"tenant_id": str(uuid.uuid4())},
        )
        assert forbidden.status_code == 403

    def test_super_admin_can_create_skill_for_tenant(
    self,
    tenant: Any,
    super_tok: Any,
) -> None:

        code = f"SUPER-{UNIQ}"
        missing = httpx.post(
            f"{API}/skills",
            headers=_h(super_tok),
            json={"name": "Missing tenant", "code": code},
        )
        assert missing.status_code == 400, missing.text

        created = httpx.post(
            f"{API}/skills",
            headers=_h(super_tok),
            params={"tenant_id": tenant["id"]},
            json={"name": "Super ICU", "code": code},
        )
        assert created.status_code == 201, created.text
        assert created.json()["tenant_id"] == tenant["id"]

        cross_tenant = httpx.post(
            f"{API}/skills",
            headers=_h(tenant["admin_token"]),
            params={"tenant_id": str(uuid.uuid4())},
            json={"name": "Forbidden", "code": f"X-{UNIQ}"},
        )
        assert cross_tenant.status_code == 403

        deleted = httpx.delete(
            f"{API}/skills/{created.json()['id']}", headers=_h(super_tok)
        )
        assert deleted.status_code == 204

    def test_viewer_cannot_create_role(self, tenant: Any) -> None:

        r = httpx.post(f"{API}/roles", headers=_h(tenant["viewer_token"]),
                       json={"name": "V", "code": "V"})
        assert r.status_code == 403

    def test_super_admin_can_create_role_for_tenant(
    self,
    tenant: Any,
    super_tok: Any,
) -> None:

        code = f"SR-{UNIQ[:8]}"
        missing = httpx.post(
            f"{API}/roles",
            headers=_h(super_tok),
            json={"name": "Missing tenant", "code": code},
        )
        assert missing.status_code == 400, missing.text

        created = httpx.post(
            f"{API}/roles",
            headers=_h(super_tok),
            params={"tenant_id": tenant["id"]},
            json={"name": "Super Role", "code": code},
        )
        assert created.status_code == 201, created.text
        assert created.json()["tenant_id"] == tenant["id"]

        cross_tenant = httpx.post(
            f"{API}/roles",
            headers=_h(tenant["admin_token"]),
            params={"tenant_id": str(uuid.uuid4())},
            json={"name": "Forbidden", "code": f"X-{UNIQ[:8]}"},
        )
        assert cross_tenant.status_code == 403

        deleted = httpx.delete(
            f"{API}/roles/{created.json()['id']}", headers=_h(super_tok)
        )
        assert deleted.status_code == 204


# ---------------------------------------------------------------- day-groups / shifts

@pytest.fixture(scope="module")
def shifts(tenant: Any) -> dict[str, Any]:

    h = _h(tenant["admin_token"])
    r = httpx.post(f"{API}/day-groups", headers=h,
                   json={"name": "All week", "day_numbers": [1, 2, 3, 4, 5, 6, 7]})
    assert r.status_code == 201, r.text
    dg = r.json()["id"]

    out = {"day_group": dg}
    for code, start, end in (("E", "07:00", "15:00"), ("N", "15:00", "23:00")):
        r = httpx.post(f"{API}/shift-templates", headers=h, json={
            "code": code, "name": code, "start_time": start, "end_time": end,
            "duration_hours": 8.0, "day_group_id": dg})
        # 409 is acceptable: this module-scoped fixture may already exist on the
        # shared tenant from a prior run/test (code is now unique per tenant).
        assert r.status_code in (201, 409), r.text
        if r.status_code == 201:
            out[code] = r.json()["id"]
        else:
            # 409 -> the shift already exists; fetch its id.
            r2 = httpx.get(f"{API}/shift-templates?page_size=200", headers=h)
            out[code] = next(t["id"] for t in r2.json()["items"] if t["code"] == code)
    return out


class TestShifts:
    def test_day_group_crud(self, tenant: Any) -> None:

        h = _h(tenant["admin_token"])
        r = httpx.post(f"{API}/day-groups", headers=h, json={"name": "Weekday", "day_numbers": [1, 2, 3, 4, 5]})
        assert r.status_code == 201
        dg_id = r.json()["id"]
        assert r.json()["day_numbers"] == [1, 2, 3, 4, 5]

        r = httpx.get(f"{API}/day-groups", headers=h)
        assert r.status_code == 200

        r = httpx.patch(
            f"{API}/day-groups/{dg_id}",
            headers=h,
            json={"name": "Weekend", "description": "Updated", "day_numbers": [6, 7]},
        )
        assert r.status_code == 200, r.text
        assert r.json()["name"] == "Weekend"
        assert r.json()["description"] == "Updated"
        assert r.json()["day_numbers"] == [6, 7]
        r = httpx.get(f"{API}/day-groups/{dg_id}", headers=h)
        assert r.status_code == 200
        assert r.json()["day_numbers"] == [6, 7]

        r = httpx.delete(f"{API}/day-groups/{dg_id}", headers=h)
        assert r.status_code == 204

    def test_day_group_404(self, tenant: Any) -> None:

        assert httpx.delete(f"{API}/day-groups/{uuid.uuid4()}",
                            headers=_h(tenant["admin_token"])).status_code == 404

    def test_shift_template_list_and_delete(self, tenant: Any) -> None:

        h = _h(tenant["admin_token"])
        # day_group_id required → 422 when omitted/None.
        r = httpx.post(f"{API}/shift-templates", headers=h, json={
            "code": "X", "name": "Extra", "start_time": "23:00", "end_time": "07:00",
            "duration_hours": 8.0, "day_group_id": None})
        assert r.status_code == 422

        # Create our own day group + template, then verify the template shows up.
        r = httpx.post(f"{API}/day-groups", headers=h, json={"name": "dg-list", "day_numbers": [1]})
        assert r.status_code == 201
        dg = r.json()["id"]
        r = httpx.post(f"{API}/shift-templates", headers=h, json={
            "code": "L", "name": "List", "start_time": "07:00", "end_time": "15:00",
            "duration_hours": 8.0, "day_group_id": dg})
        assert r.status_code == 201
        r = httpx.get(f"{API}/shift-templates", headers=h)
        assert r.status_code == 200
        assert "L" in {s["code"] for s in r.json()["items"]}

    def test_shift_template_duplicate_code_rejected(self, tenant: Any) -> None:

        h = _h(tenant["admin_token"])
        r = httpx.post(f"{API}/day-groups", headers=h, json={"name": "dg-dup", "day_numbers": [1]})
        assert r.status_code == 201
        dg = r.json()["id"]
        r = httpx.post(f"{API}/shift-templates", headers=h, json={
            "code": "E", "name": "Early", "start_time": "07:00", "end_time": "15:00",
            "duration_hours": 8.0, "day_group_id": dg})
        assert r.status_code == 201
        # Same code in the same tenant is rejected.
        r2 = httpx.post(f"{API}/shift-templates", headers=h, json={
            "code": "E", "name": "Early Again", "start_time": "06:00", "end_time": "14:00",
            "duration_hours": 8.0, "day_group_id": dg})
        assert r2.status_code == 409
        assert "存在" in r2.json()["detail"]

    def test_read_only_roles_cannot_manage_shifts(
    self,
    tenant: Any,
    shifts: Any,
    restricted_users: Any,
) -> None:

        payload = {
            "code": f"{UNIQ}-restricted-shift",
            "name": "Restricted shift",
            "start_time": "07:00",
            "end_time": "15:00",
            "duration_hours": 8.0,
            "day_group_id": shifts["day_group"],
        }
        for role in ("viewer", "nurse"):
            headers = _h(restricted_users[role])
            assert httpx.post(
                f"{API}/day-groups", headers=headers,
                json={"name": f"Restricted {role}", "day_numbers": [1]},
            ).status_code == 403
            assert httpx.post(
                f"{API}/shift-templates", headers=headers, json=payload,
            ).status_code == 403
            assert httpx.patch(
                f"{API}/shift-templates/{shifts['E']}", headers=headers, json=payload,
            ).status_code == 403
            assert httpx.delete(
                f"{API}/day-groups/{uuid.uuid4()}", headers=headers,
            ).status_code == 403
            assert httpx.delete(
                f"{API}/shift-templates/{uuid.uuid4()}", headers=headers,
            ).status_code == 403


# ---------------------------------------------------------------- nurses

@pytest.fixture(scope="module")
def nurses(tenant: Any, roles: Any) -> list[dict[str, Any]]:

    h = _h(tenant["admin_token"])
    out = []
    for i in range(3):
        r = httpx.post(f"{API}/nurses", headers=h, json={
            "employee_id": f"{UNIQ}-{i}", "first_name": f"N{i}", "last_name": "T",
            "is_available": True, "role_ids": [roles["RN"]],
            "contract": {"shifts_per_period": 4, "max_shifts_per_period": 6,
                         "min_rest_hours": 11, "max_consecutive_days": 5,
                         "enforce_balanced": False},
        })
        assert r.status_code == 201, r.text
        out.append(r.json())
    return out


@pytest.fixture(scope="module")
def restricted_users(tenant: Any, nurses: Any) -> dict[str, str]:

    users = {"viewer": tenant["viewer_token"]}
    accounts = (
        ("scheduler", f"restricted-scheduler-{UNIQ}@example.com", None),
        ("nurse", f"restricted-nurse-{UNIQ}@example.com", nurses[0]["id"]),
    )
    for role, email, nurse_id in accounts:
        created = httpx.post(
            f"{API}/auth/users",
            headers=_h(tenant["admin_token"]),
            json={
                "email": email,
                "password": "password123",
                "first_name": "Restricted",
                "last_name": role.title(),
                "role": role,
                "nurse_id": nurse_id,
            },
        )
        assert created.status_code == 201, created.text
        users[role] = _login(email, "password123").json()["access_token"]
    return users


class TestNurses:
    def test_create_and_read_back(self, tenant: Any, nurses: Any, roles: Any) -> None:

        n = nurses[0]
        assert n["role_ids"] == [roles["RN"]]
        assert n["contract"]["shifts_per_period"] == 4

        r = httpx.get(f"{API}/nurses/{n['id']}", headers=_h(tenant["admin_token"]))
        assert r.status_code == 200
        assert r.json()["employee_id"] == n["employee_id"]

    def test_duplicate_employee_id_rejected_within_tenant(self, tenant: Any) -> None:

        r = httpx.post(f"{API}/nurses", headers=_h(tenant["admin_token"]), json={
            "employee_id": "UNI-Q1", "first_name": "A", "last_name": "Dup",
            "role_ids": [tenant["role_id"]]})
        assert r.status_code == 201
        r2 = httpx.post(f"{API}/nurses", headers=_h(tenant["admin_token"]), json={
            "employee_id": "UNI-Q1", "first_name": "B", "last_name": "Same",
            "role_ids": [tenant["role_id"]]})
        assert r2.status_code == 409
        assert "工号" in r2.json()["detail"]

    def test_patch_changes_employee_id(self, tenant: Any) -> None:

        nid = httpx.post(f"{API}/nurses", headers=_h(tenant["admin_token"]), json={
            "employee_id": f"CHG-{uuid.uuid4().hex[:8]}", "first_name": "N0", "last_name": "T",
            "is_available": True, "role_ids": [tenant["role_id"]]}).json()["id"]
        r = httpx.patch(f"{API}/nurses/{nid}", headers=_h(tenant["admin_token"]), json={
            "employee_id": "CHANGED-1", "first_name": "N0", "last_name": "T",
            "is_available": True, "role_ids": [tenant["role_id"]]})
        assert r.status_code == 200
        assert r.json()["employee_id"] == "CHANGED-1"
        # Re-applying the same (now-existing) id is a no-op, not a conflict.
        r2 = httpx.patch(f"{API}/nurses/{nid}", headers=_h(tenant["admin_token"]), json={
            "employee_id": "CHANGED-1", "first_name": "N0", "last_name": "T",
            "is_available": True, "role_ids": [tenant["role_id"]]})
        assert r2.status_code == 200

    def test_patch_rejects_collision(self, tenant: Any) -> None:

        h = _h(tenant["admin_token"])
        a = httpx.post(f"{API}/nurses", headers=h, json={
            "employee_id": "TGT-1", "first_name": "A", "last_name": "One",
            "role_ids": [tenant["role_id"]]}).json()
        b = httpx.post(f"{API}/nurses", headers=h, json={
            "employee_id": "TGT-2", "first_name": "B", "last_name": "Two",
            "role_ids": [tenant["role_id"]]}).json()
        r = httpx.patch(f"{API}/nurses/{b['id']}", headers=h, json={
            "employee_id": "TGT-1", "first_name": "B", "last_name": "Two",
            "is_available": True, "role_ids": [tenant["role_id"]]})
        assert r.status_code == 409
        # Original nurse still exists; TGT-1 was never transferred.
        assert httpx.get(f"{API}/nurses/{a['id']}", headers=h).json()["employee_id"] == "TGT-1"

    def test_get_404(self, tenant: Any) -> None:

        assert httpx.get(f"{API}/nurses/{uuid.uuid4()}",
                         headers=_h(tenant["admin_token"])).status_code == 404

    def test_patch_full_replace(self, tenant: Any, nurses: Any, roles: Any) -> None:

        nid = nurses[1]["id"]
        r = httpx.patch(f"{API}/nurses/{nid}", headers=_h(tenant["admin_token"]), json={
            "employee_id": nurses[1]["employee_id"], "first_name": "N1b", "last_name": "T",
            "is_available": False, "preferences": {"note": "on leave soon"},
            "role_ids": [roles["RN"]]})
        assert r.status_code == 200
        assert r.json()["is_available"] is False
        assert r.json()["preferences"]["note"] == "on leave soon"
        assert r.json()["first_name"] == "N1b"

    def test_contract_get_and_put(self, tenant: Any, nurses: Any) -> None:

        nid = nurses[0]["id"]
        h = _h(tenant["admin_token"])
        r = httpx.get(f"{API}/nurses/{nid}/contract", headers=h)
        assert r.status_code == 200
        assert r.json()["min_rest_hours"] == 11

        r = httpx.put(f"{API}/nurses/{nid}/contract", headers=h,
                      json={"shifts_per_period": 5, "min_rest_hours": 12})
        assert r.status_code == 200
        assert r.json()["shifts_per_period"] == 5
        assert r.json()["min_rest_hours"] == 12

    def test_contract_404_when_absent(self, tenant: Any) -> None:

        r = httpx.post(f"{API}/nurses", headers=_h(tenant["admin_token"]), json={
            "employee_id": f"{UNIQ}-nc", "first_name": "No", "last_name": "Contract",
            "role_ids": [tenant["role_id"]]})
        assert r.status_code == 201
        assert httpx.get(f"{API}/nurses/{r.json()['id']}/contract",
                         headers=_h(tenant["admin_token"])).status_code == 404

    def test_leaves_crud(self, tenant: Any, nurses: Any) -> None:

        h = _h(tenant["admin_token"])
        nid = nurses[2]["id"]
        r = httpx.post(f"{API}/nurses/{nid}/leaves", headers=h,
                       json={"nurse_id": nid, "date": "2026-12-25", "description": "Christmas"})
        assert r.status_code == 201, r.text
        leave_id = r.json()["id"]

        r = httpx.get(f"{API}/nurses/{nid}/leaves", headers=h)
        assert r.status_code == 200
        assert any(lv["id"] == leave_id for lv in r.json())

        assert httpx.delete(f"{API}/nurses/{nid}/leaves/{leave_id}", headers=h).status_code == 204
        r = httpx.get(f"{API}/nurses/{nid}/leaves", headers=h)
        assert not any(lv["id"] == leave_id for lv in r.json())

    def test_delete_nurse(self, tenant: Any) -> None:

        h = _h(tenant["admin_token"])
        r = httpx.post(f"{API}/nurses", headers=h, json={
            "employee_id": f"{UNIQ}-del", "first_name": "Del", "last_name": "Me",
            "role_ids": [tenant["role_id"]]})
        assert r.status_code == 201
        nid = r.json()["id"]
        assert httpx.delete(f"{API}/nurses/{nid}", headers=h).status_code == 204
        assert httpx.get(f"{API}/nurses/{nid}", headers=h).status_code == 404

    def test_delete_department_clears_it_for_nurses(self, tenant: Any) -> None:

        h = _h(tenant["admin_token"])
        department = f"待删除科室-{UNIQ}"
        nurse_ids = []
        for index in range(2):
            r = httpx.post(f"{API}/nurses", headers=h, json={
                "employee_id": f"dept-{UNIQ}-{index}", "first_name": "Dept", "last_name": str(index),
                "department": department, "role_ids": [tenant["role_id"]]})
            assert r.status_code == 201, r.text
            nurse_ids.append(r.json()["id"])

        r = httpx.delete(f"{API}/nurses/departments", headers=h,
                         params={"department": department})
        assert r.status_code == 200, r.text
        assert r.json()["updated"] == 2
        for nurse_id in nurse_ids:
            assert httpx.get(f"{API}/nurses/{nurse_id}", headers=h).json()["department"] is None

        assert httpx.delete(
            f"{API}/nurses/departments",
            headers=_h(tenant["viewer_token"]),
            params={"department": department},
        ).status_code == 403

    def test_list_pagination(self, tenant: Any, nurses: Any) -> None:

        h = _h(tenant["admin_token"])
        r = httpx.get(f"{API}/nurses", headers=h, params={"page": 1, "page_size": 2})
        assert r.status_code == 200
        body = r.json()
        assert len(body["items"]) == 2
        assert body["total"] >= 3
        r2 = httpx.get(f"{API}/nurses", headers=h, params={"page": 2, "page_size": 2})
        assert r2.status_code == 200
        ids1 = {n["id"] for n in body["items"]}
        ids2 = {n["id"] for n in r2.json()["items"]}
        assert not (ids1 & ids2)  # pages don't overlap

    def test_viewer_cannot_create_nurse(self, tenant: Any) -> None:

        r = httpx.post(f"{API}/nurses", headers=_h(tenant["viewer_token"]), json={
            "employee_id": f"{UNIQ}-v", "first_name": "V", "last_name": "N",
            "role_ids": [tenant["role_id"]]})
        assert r.status_code == 403

    def test_restricted_roles_cannot_manage_nurse_profiles(
    self,
    tenant: Any,
    nurses: Any,
    restricted_users: Any,
) -> None:

        payload = {
            "employee_id": f"{UNIQ}-restricted",
            "first_name": "Restricted",
            "last_name": "Nurse",
            "is_available": True,
        }
        for role, token in restricted_users.items():
            created = httpx.post(f"{API}/nurses", headers=_h(token), json=payload)
            assert created.status_code == 403, (role, created.text)

            patched = httpx.patch(
                f"{API}/nurses/{nurses[0]['id']}",
                headers=_h(token),
                json={
                    **nurses[0],
                    "department": f"forbidden-{role}",
                },
            )
            assert patched.status_code == 403, (role, patched.text)

            deleted = httpx.delete(
                f"{API}/nurses/{nurses[0]['id']}", headers=_h(token)
            )
            assert deleted.status_code == 403, (role, deleted.text)

    def test_viewer_can_read(self, tenant: Any, nurses: Any) -> None:

        r = httpx.get(f"{API}/nurses/{nurses[0]['id']}", headers=_h(tenant["viewer_token"]))
        assert r.status_code == 200


# ---------------------------------------------------------------- rules

class TestRules:
    def test_skill_mix_crud(self, tenant: Any, roles: Any, shifts: Any) -> None:

        h = _h(tenant["admin_token"])
        r = httpx.post(f"{API}/skill-mix-rules", headers=h, json={
            "name": "E-mix", "shift_template_id": shifts["E"], "priority": 1,
            "requirements": [{"role_id": roles["RN"], "count": 2},
                             {"role_id": roles["LV"], "count": 1}]})
        assert r.status_code == 201, r.text
        rule_id = r.json()["id"]
        assert len(r.json()["requirements"]) == 2

        r = httpx.get(f"{API}/skill-mix-rules/{rule_id}", headers=h)
        assert r.status_code == 200

        r = httpx.patch(f"{API}/skill-mix-rules/{rule_id}", headers=h, json={
            "name": "E-mix v2", "shift_template_id": shifts["E"], "priority": 2,
            "requirements": [{"role_id": roles["RN"], "count": 3}]})
        assert r.status_code == 200
        assert r.json()["priority"] == 2

        r = httpx.get(f"{API}/skill-mix-rules", headers=h)
        assert r.status_code == 200

        assert httpx.delete(f"{API}/skill-mix-rules/{rule_id}", headers=h).status_code == 204
        assert httpx.get(f"{API}/skill-mix-rules/{rule_id}", headers=h).status_code == 404

    def test_shift_sequence_crud(self, tenant: Any, shifts: Any, roles: Any) -> None:

        h = _h(tenant["admin_token"])
        r = httpx.post(f"{API}/shift-sequence-rules", headers=h, json={
            "name": "EN-off cycle", "description": "E then N then off",
            "steps": [{"position": 0, "shift_template_id": shifts["E"]},
                      {"position": 1, "shift_template_id": shifts["N"]},
                      {"position": 2, "shift_template_id": None}],
            "role_ids": [roles["RN"]]})
        assert r.status_code == 201, r.text
        rule_id = r.json()["id"]
        assert len(r.json()["steps"]) == 3

        r = httpx.get(f"{API}/shift-sequence-rules/{rule_id}", headers=h)
        assert r.status_code == 200

        r = httpx.get(f"{API}/shift-sequence-rules", headers=h)
        assert r.status_code == 200

        assert httpx.delete(f"{API}/shift-sequence-rules/{rule_id}", headers=h).status_code == 204

    def test_super_admin_can_create_rules_with_tenant_context(
    self,
    super_tok: Any,
    tenant: Any,
    roles: Any,
    shifts: Any,
) -> None:

        h = _h(super_tok)
        params = {"tenant_id": tenant["id"]}
        skill_mix = httpx.post(
            f"{API}/skill-mix-rules",
            headers=h,
            params=params,
            json={
                "name": f"Super mix {uuid.uuid4().hex[:6]}",
                "shift_template_id": shifts["E"],
                "priority": 1,
                "requirements": [{"role_id": roles["RN"], "count": 1}],
            },
        )
        assert skill_mix.status_code == 201, skill_mix.text
        assert skill_mix.json()["tenant_id"] == tenant["id"]

        sequence = httpx.post(
            f"{API}/shift-sequence-rules",
            headers=h,
            params=params,
            json={
                "name": f"Super sequence {uuid.uuid4().hex[:6]}",
                "description": None,
                "steps": [{"position": 0, "shift_template_id": shifts["E"]}],
                "role_ids": [roles["RN"]],
            },
        )
        assert sequence.status_code == 201, sequence.text
        assert sequence.json()["tenant_id"] == tenant["id"]
        assert httpx.delete(
            f"{API}/skill-mix-rules/{skill_mix.json()['id']}", headers=h
        ).status_code == 204
        assert httpx.delete(
            f"{API}/shift-sequence-rules/{sequence.json()['id']}", headers=h
        ).status_code == 204

    def test_skill_mix_404(self, tenant: Any) -> None:

        assert httpx.get(f"{API}/skill-mix-rules/{uuid.uuid4()}",
                         headers=_h(tenant["admin_token"])).status_code == 404

    def test_read_only_roles_cannot_manage_rules(
    self,
    tenant: Any,
    roles: Any,
    shifts: Any,
    restricted_users: Any,
) -> None:

        skill_mix_payload = {
            "name": "Restricted skill mix",
            "shift_template_id": shifts["E"],
            "priority": 0,
            "requirements": [{"role_id": roles["RN"], "count": 1}],
        }
        sequence_payload = {
            "name": "Restricted sequence",
            "description": None,
            "steps": [
                {"position": 0, "shift_template_id": shifts["E"]},
                {"position": 1, "shift_template_id": shifts["N"]},
            ],
            "role_ids": [],
        }
        for role in ("viewer", "nurse"):
            headers = _h(restricted_users[role])
            assert httpx.get(
                f"{API}/skill-mix-rules", headers=headers,
            ).status_code == 200
            assert httpx.post(
                f"{API}/skill-mix-rules", headers=headers, json=skill_mix_payload,
            ).status_code == 403
            assert httpx.patch(
                f"{API}/skill-mix-rules/{uuid.uuid4()}",
                headers=headers,
                json=skill_mix_payload,
            ).status_code == 403
            assert httpx.delete(
                f"{API}/skill-mix-rules/{uuid.uuid4()}", headers=headers,
            ).status_code == 403
            assert httpx.post(
                f"{API}/shift-sequence-rules",
                headers=headers,
                json=sequence_payload,
            ).status_code == 403
            assert httpx.patch(
                f"{API}/shift-sequence-rules/{uuid.uuid4()}",
                headers=headers,
                json=sequence_payload,
            ).status_code == 403
            assert httpx.delete(
                f"{API}/shift-sequence-rules/{uuid.uuid4()}", headers=headers,
            ).status_code == 403


# ---------------------------------------------------------------- nurse self-service

class TestNurseSelfService:
    def test_me_includes_linked_nurse_and_skills(
    self,
    tenant: Any,
    nurses: Any,
    roles: Any,
    restricted_users: Any,
) -> None:

        skill = httpx.post(
            f"{API}/skills",
            headers=_h(tenant["admin_token"]),
            json={"name": "重症护理", "code": f"ICU-{UNIQ}"},
        )
        assert skill.status_code == 201, skill.text

        linked = nurses[0]
        updated = httpx.patch(
            f"{API}/nurses/{linked['id']}",
            headers=_h(tenant["admin_token"]),
            json={
                "employee_id": linked["employee_id"],
                "first_name": linked["first_name"],
                "last_name": linked["last_name"],
                "is_available": linked["is_available"],
                "role_ids": linked["role_ids"],
                "skill_ids": [skill.json()["id"]],
            },
        )
        assert updated.status_code == 200, updated.text

        me = httpx.get(f"{API}/auth/me", headers=_h(restricted_users["nurse"]))
        assert me.status_code == 200, me.text
        body = me.json()
        assert body["nurse_id"] == linked["id"]
        assert body["nurse_name"] == linked["last_name"] + linked["first_name"]
        assert body["nurse_skill_ids"] == [skill.json()["id"]]
        assert body["nurse_skill_names"] == [f"重症护理（ICU-{UNIQ}）"]

        renamed = httpx.patch(
            f"{API}/auth/me",
            headers=_h(restricted_users["nurse"]),
            json={"first_name": linked["first_name"], "last_name": "自助理"},
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["nurse_name"] == linked["last_name"] + linked["first_name"]
        assert renamed.json()["nurse_skill_ids"] == [skill.json()["id"]]
        assert renamed.json()["nurse_skill_names"] == [f"重症护理（ICU-{UNIQ}）"]

    def test_nurse_updates_own_skills(self, tenant: Any, restricted_users: Any) -> None:

        headers = _h(restricted_users["nurse"])
        initial = httpx.get(f"{API}/auth/me", headers=headers).json()
        original_skill_ids = initial["nurse_skill_ids"]

        created_skills = []
        for index in range(2):
            created = httpx.post(
                f"{API}/skills",
                headers=_h(tenant["admin_token"]),
                json={"name": f"自助技能{index}", "code": f"SELF{index}-{UNIQ}"},
            )
            assert created.status_code == 201, created.text
            created_skills.append(created.json()["id"])

        updated = httpx.patch(
            f"{API}/auth/me/skills",
            headers=headers,
            json={"skill_ids": [created_skills[0]]},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["nurse_skill_ids"] == [created_skills[0]]
        assert updated.json()["nurse_skill_names"] == [f"自助技能0（SELF0-{UNIQ}）"]

        cleared = httpx.patch(
            f"{API}/auth/me/skills", headers=headers, json={"skill_ids": []}
        )
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["nurse_skill_ids"] == []

        restored = httpx.patch(
            f"{API}/auth/me/skills",
            headers=headers,
            json={"skill_ids": original_skill_ids},
        )
        assert restored.status_code == 200, restored.text
        assert restored.json()["nurse_skill_ids"] == original_skill_ids

        forbidden = httpx.patch(
            f"{API}/auth/me/skills",
            headers=_h(restricted_users["viewer"]),
            json={"skill_ids": created_skills},
        )
        assert forbidden.status_code == 403

        invalid = httpx.patch(
            f"{API}/auth/me/skills",
            headers=headers,
            json={"skill_ids": [str(uuid.uuid4())]},
        )
        assert invalid.status_code == 404

    def test_nurse_manages_schedule_preferences(
    self,
    tenant: Any,
    shifts: Any,
    restricted_users: Any,
) -> None:

        headers = _h(restricted_users["nurse"])
        assert httpx.get(
            f"{API}/auth/me/preferences", headers=_h(restricted_users["viewer"])
        ).status_code == 403

        payload = {
            "date": "2026-12-25",
            "shift_template_id": shifts["E"],
            "request_type": "like",
        }
        created = httpx.post(f"{API}/auth/me/preferences", headers=headers, json=payload)
        assert created.status_code == 201, created.text
        assert created.json()["request_type"] == "like"
        preference_id = created.json()["id"]

        payload["request_type"] = "avoid"
        updated = httpx.post(f"{API}/auth/me/preferences", headers=headers, json=payload)
        assert updated.status_code == 201, updated.text
        assert updated.json()["id"] == preference_id
        assert updated.json()["request_type"] == "avoid"

        listed = httpx.get(f"{API}/auth/me/preferences", headers=headers)
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()] == [preference_id]

        assert httpx.delete(
            f"{API}/auth/me/preferences/{preference_id}", headers=headers
        ).status_code == 204
        assert httpx.get(f"{API}/auth/me/preferences", headers=headers).json() == []


# ---------------------------------------------------------------- schedules (lifecycle)

class TestScheduleValidation:
    def test_generate_validation_errors(self, tenant: Any) -> None:

        h = _h(tenant["admin_token"])
        # period_days out of range
        r = httpx.post(f"{API}/schedules/generate", headers=h, json={
            "period_start": "2026-11-01", "period_days": 0})
        assert r.status_code == 422
        # bad date
        r = httpx.post(f"{API}/schedules/generate", headers=h, json={
            "period_start": "not-a-date", "period_days": 7})
        assert r.status_code == 422

    def test_get_404(self, tenant: Any) -> None:

        assert httpx.get(f"{API}/schedules/{uuid.uuid4()}",
                         headers=_h(tenant["admin_token"])).status_code == 404

    def test_result_404(self, tenant: Any) -> None:

        assert httpx.get(f"{API}/schedules/{uuid.uuid4()}/result",
                         headers=_h(tenant["admin_token"])).status_code == 404

    def test_export_404(self, tenant: Any) -> None:

        r = httpx.get(f"{API}/schedules/{uuid.uuid4()}/export?format=csv",
                      headers=_h(tenant["admin_token"]))
        assert r.status_code == 404

    def test_generate_requires_auth(self) -> None:

        r = httpx.post(f"{API}/schedules/generate",
                       json={"period_start": "2026-11-01", "period_days": 7})
        assert r.status_code == 401


class TestCalendarExport:
    def test_month_grid_events_belong_to_their_natural_month(self) -> None:

        start = date(2026, 8, 31)
        dates = [(start + timedelta(days=offset)).isoformat() for offset in range(14)]
        assignments = [
            SimpleNamespace(
                date=date(2026, 9, 1), nurse_id="nurse-1", shift_template_id="shift-1"
            )
        ]

        months = _calendar_months(
            dates,
            cast(Any, assignments),
            {"nurse-1": "护士一"},
            {"shift-1": "早班"},
        )

        assert len(months) == 2
        assert sum(
            len(cell["assignments"])
            for month in months
            for cell in month["cells"]
        ) == 1
        september_first = next(
            cell for cell in months[1]["cells"] if cell["date"] == "2026-09-01"
        )
        assert september_first["assignments"] == [
            {"nurse_name": "护士一", "shift_name": "早班"}
        ]


class TestFullGenerateFlow:
    """Small-scale full flow: 6 nurses / 7 days / 2 shifts, leave honored."""

    def test_generate_happy_path_with_leave(
    self,
    tenant: Any,
    roles: Any,
    super_tok: Any,
) -> None:

        h = _h(tenant["admin_token"])
        # Use a private day-group + shift templates so this test is isolated
        # from skill-mix-rules created by other tests on the shared shifts
        # fixture (which would stack demand and break feasibility).
        r = httpx.post(f"{API}/day-groups", headers=h,
                       json={"name": "gen-dg", "day_numbers": [1, 2, 3, 4, 5, 6, 7]})
        assert r.status_code == 201
        dg = r.json()["id"]
        sh = {}
        for code, start, end in (("E", "07:00", "15:00"), ("N", "15:00", "23:00")):
            r = httpx.post(f"{API}/shift-templates", headers=h, json={
                "code": f"{UNIQ}-{code}", "name": code, "start_time": start, "end_time": end,
                "duration_hours": 8.0, "day_group_id": dg})
            assert r.status_code == 201, r.text
            sh[f"{UNIQ}-{code}"] = r.json()["id"]

        # Small feasible instance with headroom so a leave day stays solvable:
        # demand 7d × 2 slots × 3 = 42; supply 8 nurses × 7d = 56 (one nurse
        # takes a leave day → 49 available ≥ 42).
        nurse_ids = []
        for i in range(8):
            r = httpx.post(f"{API}/nurses", headers=h, json={
                "employee_id": f"{UNIQ}-g{i}", "first_name": f"G{i}", "last_name": "S",
                "is_available": True,
                "role_ids": [roles["RN"]] if i < 5 else [roles["LV"]],
                "contract": {"shifts_per_period": 7, "max_shifts_per_period": 7,
                             "min_rest_hours": 11, "max_consecutive_days": 7,
                             "enforce_balanced": False, "enforce_shifts_per_period": False,
                             "enforce_one_shift_per_day": True},
            })
            assert r.status_code == 201, r.text
            nurse_ids.append(r.json()["id"])

        # Nurse 0 takes leave on day 1.
        r = httpx.post(f"{API}/nurses/{nurse_ids[0]}/leaves", headers=h,
                       json={"nurse_id": nurse_ids[0], "date": "2026-11-01"})
        assert r.status_code == 201

        # Demand: E needs 2 RN + 1 LV, N needs 2 RN + 1 LV → 3/slot.
        for sid in (sh[f"{UNIQ}-E"], sh[f"{UNIQ}-N"]):
            r = httpx.post(f"{API}/skill-mix-rules", headers=h, json={
                "name": f"demand {sid[:4]}", "shift_template_id": sid, "priority": 0,
                "requirements": [{"role_id": roles["RN"], "count": 2},
                                 {"role_id": roles["LV"], "count": 1}]})
            assert r.status_code == 201, r.text

        r = httpx.post(f"{API}/schedules/generate", headers=h, json={
            "period_start": "2026-11-01", "period_days": 7,
            "nurse_ids": nurse_ids,
            "solver_config": {"timeout_seconds": 60, "num_workers": 4}})
        assert r.status_code == 201, r.text
        request_id = r.json()["id"]
        assert r.json()["status"] == "pending"
        assert re.fullmatch(r"\d{8}-\d{4}", r.json()["display_id"])

        deadline = time.monotonic() + 90
        final = None
        while time.monotonic() < deadline:
            final = httpx.get(f"{API}/schedules/{request_id}", headers=h).json()
            if final["status"] in ("completed", "failed", "cancelled"):
                break
            time.sleep(1.0)
        assert final is not None
        assert final["status"] == "completed", f"unexpected: {final}"

        # Result: 7 days × 2 slots × 3 = 42 assignments.
        r = httpx.get(f"{API}/schedules/{request_id}/result", headers=h)
        assert r.status_code == 200, r.text
        sched = r.json()
        assert len(sched["assignments"]) == 42
        assert sched["outcome"] in ("optimal", "feasible")
        assert len(sched["shift_templates"]) == 2
        assert all(
            template["day_group_name"] == "gen-dg"
            and template["day_numbers"] == [1, 2, 3, 4, 5, 6, 7]
            for template in sched["shift_templates"]
        )

        # Leave honored: nurse 0 has no assignment on 2026-11-01.
        assert not any(a["nurse_id"] == nurse_ids[0] and a["date"] == "2026-11-01"
                       for a in sched["assignments"])

        # One shift per day honored.
        from collections import Counter
        per_nurse_day = Counter((a["nurse_id"], a["date"]) for a in sched["assignments"])
        assert max(per_nurse_day.values()) == 1

        nurse_email = f"my-schedule@{UNIQ}.example.com"
        r = httpx.post(f"{API}/auth/users", headers=h, json={
            "email": nurse_email, "password": "password123",
            "first_name": "G0", "last_name": "S",
            "role": "nurse", "nurse_id": nurse_ids[0]})
        assert r.status_code == 201, r.text
        nurse_token = _login(nurse_email, "password123").json()["access_token"]

        mine = httpx.get(f"{API}/schedules/mine?page_size=100", headers=_h(nurse_token))
        assert mine.status_code == 200, mine.text
        mine_items = mine.json()["items"]
        assert [item["id"] for item in mine_items] == [request_id]
        assert mine_items[0]["assignment_count"] > 0
        assert httpx.get(
            f"{API}/schedules/mine", headers=_h(tenant["admin_token"])
        ).status_code == 403

        r = httpx.get(f"{API}/schedules/{request_id}/result",
                      headers=_h(nurse_token))
        assert r.status_code == 200, r.text
        my_assignments = r.json()["assignments"]
        assert my_assignments
        assert {a["nurse_id"] for a in my_assignments} == {nurse_ids[0]}
        assert mine_items[0]["assignment_count"] == len(my_assignments)

        r = httpx.get(f"{API}/schedules/{request_id}/export?format=csv",
                      headers=_h(nurse_token))
        assert r.status_code == 200
        nurse_csv_rows = [
            row for row in csv.reader(r.text.splitlines())
            if row and not row[0].startswith("#")
        ]
        assert len(nurse_csv_rows) == 2
        assert nurse_csv_rows[1][0] == "SG0"

        # Export CSV is a nurse×date grid: header + one row per nurse in this
        # tenant (the shared dev DB accumulates nurses from other test runs,
        # so assert structure rather than an exact row count).
        r = httpx.get(f"{API}/schedules/{request_id}/export?format=csv", headers=h)
        assert r.status_code == 200
        assert "text/csv" in r.headers.get("content-type", "")
        # Skip the leading "# " metadata comment rows + blank row.
        data_rows = [
            row for row in csv.reader(r.text.splitlines())
            if row and not row[0].startswith("#")
        ]
        assert len(data_rows) >= 9  # 8 nurses + 1 header row
        assert data_rows[0][0] == "护士"
        assert "E" in r.text and "N" in r.text

        # Default exports remain the table (nurse × date) layout.
        expected_types = {
            "txt": "text/plain",
            "json": "application/json",
            "pdf": "application/pdf",
        }
        format_responses = {}
        for export_format, media_type in expected_types.items():
            r = httpx.get(
                f"{API}/schedules/{request_id}/export?format={export_format}",
                headers=h,
            )
            assert r.status_code == 200, r.text
            assert media_type in r.headers.get("content-type", "")
            assert f"schedule_{request_id[:8]}.{export_format}" in r.headers.get(
                "content-disposition", ""
            )
            format_responses[export_format] = r

        assert format_responses["txt"].text.startswith("排班结果导出")
        assert "日组 gen-dg[1,2,3,4,5,6,7]" in format_responses["txt"].text
        assert format_responses["json"].json()["request_id"] == request_id
        assert "日组 gen-dg[1,2,3,4,5,6,7]" in "、".join(
            format_responses["json"].json()["legend"]
        )
        assert format_responses["pdf"].content.startswith(b"%PDF-")

        # Calendar exports use complete natural-month grids in every file format.
        calendar_csv = httpx.get(
            f"{API}/schedules/{request_id}/export?format=csv&view=calendar",
            headers=h,
        )
        assert calendar_csv.status_code == 200, calendar_csv.text
        assert "# 显示格式: 日历模式" in calendar_csv.text
        calendar_rows = [
            row for row in csv.reader(calendar_csv.text.splitlines())
            if row and not row[0].startswith("#")
        ]
        assert calendar_rows[0] == ["日", "一", "二", "三", "四", "五", "六"]
        assert len(calendar_rows) == 7  # weekday header + six week rows
        assert all(len(row) == 7 for row in calendar_rows)
        assert not any(row[0] == "护士" for row in calendar_rows)

        calendar_txt = httpx.get(
            f"{API}/schedules/{request_id}/export?format=txt&view=calendar",
            headers=h,
        )
        assert calendar_txt.status_code == 200, calendar_txt.text
        assert "【2026年11月】" in calendar_txt.text
        assert "日 | 一 | 二 | 三 | 四 | 五 | 六" in calendar_txt.text

        calendar_json = httpx.get(
            f"{API}/schedules/{request_id}/export?format=json&view=calendar",
            headers=h,
        )
        assert calendar_json.status_code == 200, calendar_json.text
        calendar_payload = calendar_json.json()
        assert calendar_payload["view"] == "calendar"
        assert len(calendar_payload["calendar_months"]) == 1
        assert len(calendar_payload["calendar_months"][0]["cells"]) == 42
        assert "grid" not in calendar_payload

        calendar_pdf = httpx.get(
            f"{API}/schedules/{request_id}/export?format=pdf&view=calendar",
            headers=h,
        )
        assert calendar_pdf.status_code == 200, calendar_pdf.text
        assert calendar_pdf.content.startswith(b"%PDF-")

        invalid_view = httpx.get(
            f"{API}/schedules/{request_id}/export?format=csv&view=list",
            headers=h,
        )
        assert invalid_view.status_code == 400

        # List shows the request.
        r = httpx.get(f"{API}/schedules", headers=h, params={"page_size": 100})
        assert any(x["id"] == request_id for x in r.json()["items"])

        # Records can be sorted by display ID or period start.
        sort_cases: tuple[
            tuple[str, Callable[[dict[str, Any]], Any]],
            ...,
        ] = (
            ("display_id", lambda row: row["display_id"]),
            ("period_start", lambda row: row["period_start"]),
        )
        for sort_by, value in sort_cases:
            ascending = httpx.get(
                f"{API}/schedules",
                headers=_h(super_tok),
                params={"page_size": 200, "sort_by": sort_by, "sort_order": "asc"},
            )
            descending = httpx.get(
                f"{API}/schedules",
                headers=_h(super_tok),
                params={"page_size": 200, "sort_by": sort_by, "sort_order": "desc"},
            )
            assert ascending.status_code == 200, ascending.text
            assert descending.status_code == 200, descending.text
            ascending_values = [value(row) for row in ascending.json()["items"]]
            descending_values = [value(row) for row in descending.json()["items"]]
            assert ascending_values == sorted(ascending_values)
            assert descending_values == sorted(descending_values, reverse=True)

        invalid = httpx.get(
            f"{API}/schedules",
            headers=_h(super_tok),
            params={"sort_by": "id", "sort_order": "asc"},
        )
        assert invalid.status_code == 422


# ---------------------------------------------------------------- cross-tenant isolation

class TestCrossTenant:
    @pytest.fixture(scope="module")
    def other_tenant(self, super_tok: Any) -> Iterator[dict[str, Any]]:

        slug = f"{UNIQ}b"
        r = httpx.post(f"{API}/tenants", headers=_h(super_tok),
                       json={"name": f"UC B {UNIQ}", "slug": slug})
        assert r.status_code == 201, r.text
        tid = r.json()["id"]
        email = f"admin@{UNIQ}b.example.com"
        r = httpx.post(f"{API}/tenants/{tid}/admin", headers=_h(super_tok),
                       json={"email": email, "password": "password123",
                             "first_name": "B", "last_name": "Ad", "role": "tenant_admin"})
        assert r.status_code == 201, r.text
        tok_b = _login(email, "password123").json()["access_token"]
        r = httpx.post(f"{API}/roles", headers=_h(tok_b),
                       json={"name": "RN", "code": "rn"})
        assert r.status_code == 201, r.text
        yield {"id": tid, "token": tok_b, "role_id": r.json()["id"]}
        deleted = httpx.delete(f"{API}/tenants/{tid}", headers=_h(super_tok))
        assert deleted.status_code == 204, deleted.text

    def test_cannot_read_other_tenants_nurse(
    self,
    tenant: Any,
    other_tenant: Any,
    nurses: Any,
) -> None:

        r = httpx.get(f"{API}/nurses/{nurses[0]['id']}", headers=_h(other_tenant["token"]))
        assert r.status_code == 404

    def test_cannot_list_other_tenants_nurses(
    self,
    tenant: Any,
    other_tenant: Any,
    nurses: Any,
) -> None:

        r = httpx.get(f"{API}/nurses", headers=_h(other_tenant["token"]),
                      params={"page_size": 200})
        assert r.status_code == 200
        ours = {n["id"] for n in nurses}
        assert not ours & {n["id"] for n in r.json()["items"]}

    def test_cannot_delete_other_tenants_shift(
    self,
    tenant: Any,
    other_tenant: Any,
    shifts: Any,
) -> None:

        r = httpx.delete(f"{API}/shift-templates/{shifts['E']}",
                         headers=_h(other_tenant["token"]))
        assert r.status_code == 404

    def test_cannot_create_user_into_other_tenant(
    self,
    other_tenant: Any,
    tenant: Any,
) -> None:

        r = httpx.post(f"{API}/auth/users", headers=_h(other_tenant["token"]),
                       json={"email": f"b-user@{UNIQ}b.example.com", "password": "password123",
                             "first_name": "B", "last_name": "U", "role": "viewer"})
        assert r.status_code == 201
        assert r.json()["tenant_id"] == other_tenant["id"]

    def test_user_email_is_globally_unique(self, tenant: Any, other_tenant: Any) -> None:

        duplicate = httpx.post(
            f"{API}/auth/users",
            headers=_h(other_tenant["token"]),
            json={"email": f"ADMIN@{UNIQ}.example.com", "password": "password123",
                  "first_name": "Duplicate", "last_name": "Email", "role": "viewer"},
        )
        assert duplicate.status_code == 409

    def test_same_employee_id_allowed_across_tenants(
    self,
    tenant: Any,
    other_tenant: Any,
) -> None:

        r = httpx.post(f"{API}/nurses", headers=_h(tenant["admin_token"]), json={
            "employee_id": "SHARED-1", "first_name": "A", "last_name": "TenA",
            "role_ids": [tenant["role_id"]]})
        assert r.status_code == 201
        r2 = httpx.post(f"{API}/nurses", headers=_h(other_tenant["token"]), json={
            "employee_id": "SHARED-1", "first_name": "B", "last_name": "TenB",
            "role_ids": [other_tenant["role_id"]]})
        assert r2.status_code == 201
        assert r2.json()["id"] != r.json()["id"]

    def test_same_shift_code_allowed_across_tenants(
    self,
    tenant: Any,
    other_tenant: Any,
) -> None:

        dg = httpx.post(f"{API}/day-groups", headers=_h(tenant["admin_token"]),
                        json={"name": "cross-dg", "day_numbers": [1]}).json()["id"]
        r = httpx.post(f"{API}/shift-templates", headers=_h(tenant["admin_token"]), json={
            "code": "SHARED", "name": "Shared A", "start_time": "07:00", "end_time": "15:00",
            "duration_hours": 8.0, "day_group_id": dg})
        assert r.status_code == 201
        dg2 = httpx.post(f"{API}/day-groups", headers=_h(other_tenant["token"]),
                         json={"name": "cross-dg2", "day_numbers": [1]}).json()["id"]
        r2 = httpx.post(f"{API}/shift-templates", headers=_h(other_tenant["token"]), json={
            "code": "SHARED", "name": "Shared B", "start_time": "07:00", "end_time": "15:00",
            "duration_hours": 8.0, "day_group_id": dg2})
        assert r2.status_code == 201
        assert r.json()["id"] != r2.json()["id"]

    def test_super_admin_cannot_create_shift_without_tenant(self, super_tok: Any) -> None:

        r = httpx.post(f"{API}/shift-templates", headers=_h(super_tok), json={
            "code": "S", "name": "Super", "start_time": "07:00", "end_time": "15:00",
            "duration_hours": 8.0, "day_group_id": "00000000-0000-0000-0000-000000000000"})
        assert r.status_code == 400, r.text
        assert "tenant" in r.json()["detail"].lower()

    def test_super_admin_can_create_day_group_and_shift_in_tenant(
    self,
    super_tok: Any,
    tenant: Any,
) -> None:

        unique = uuid.uuid4().hex[:8]
        dg = httpx.post(
            f"{API}/day-groups",
            headers=_h(super_tok),
            params={"tenant_id": tenant["id"]},
            json={"name": f"super-{unique}", "day_numbers": [1, 2]},
        )
        assert dg.status_code == 201, dg.text
        assert dg.json()["tenant_id"] == tenant["id"]

        shift = httpx.post(
            f"{API}/shift-templates",
            headers=_h(super_tok),
            params={"tenant_id": tenant["id"]},
            json={
                "code": f"S{unique[:6]}",
                "name": "Super shift",
                "start_time": "07:00",
                "end_time": "15:00",
                "duration_hours": 8.0,
                "day_group_id": dg.json()["id"],
            },
        )
        assert shift.status_code == 201, shift.text
        assert shift.json()["tenant_id"] == tenant["id"]

    def test_tenant_admin_cannot_create_day_group_in_other_tenant(
    self,
    tenant: Any,
    other_tenant: Any,
) -> None:

        r = httpx.post(
            f"{API}/day-groups",
            headers=_h(tenant["admin_token"]),
            params={"tenant_id": other_tenant["id"]},
            json={"name": "Forbidden", "day_numbers": [1]},
        )
        assert r.status_code == 403


# ---------------------------------------------------------------- frontend pages

class TestPages:
    @pytest.mark.parametrize("path", [
        "/pages/login", "/pages/dashboard", "/pages/nurses", "/pages/shifts",
        "/pages/rules", "/pages/generate", "/pages/schedules", "/pages/my-schedules",
        "/pages/expectations", "/pages/tenants", "/pages/users", "/pages/nav",
        "/pages/billing", "/pages/admin", "/pages/tenant-applications",
        "/pages/users?tenant_id=some-id",
        "/pages/schedules/some-id",
    ])
    def test_page_renders(self, path: Any) -> None:

        r = httpx.get(f"{BASE}{path}")
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "")

    def test_schedule_pages_group_batches_and_offer_detail_views(self) -> None:

        detail_script = Path("frontend/static/js/schedule_detail.js").read_text(encoding="utf-8")
        my_schedules = httpx.get(f"{BASE}/pages/my-schedules")
        assert my_schedules.status_code == 200
        assert "scheduleBatches" in my_schedules.text
        assert "assignment_count" in my_schedules.text
        assert "我的班次" in my_schedules.text

        detail = httpx.get(f"{BASE}/pages/schedules/some-id")
        assert detail.status_code == 200
        assert "表格模式" in detail.text
        assert "日历模式" in detail.text
        assert "schedule-resolve-save" in detail.text
        assert "局部重排预览 · 尚未保存" in detail.text
        assert "预览新表（未保存）" in detail.text
        assert "calendarMonths" in detail.text
        assert "view='+encodeURIComponent(this.viewMode)" in detail_script

    def test_boost_navigation_preloads_alpine_page_components(self) -> None:

        base = Path("frontend/templates/base.html").read_text(encoding="utf-8")
        assert '<meta name="htmx-config" content=\'{"allowScriptTags":false}\'>' in base
        assert "{% if not request.headers.get('HX-Request') %}" in base
        alpine_position = base.index("/static/vendor/alpine/")
        page_scripts = [
            "admin.js", "apply.js", "generate.js", "my_schedules.js",
            "nurses.js", "profile.js", "roles.js", "rules.js",
            "schedule_detail.js", "schedules.js", "shifts.js", "skills.js",
            "login.js", "setup_admin.js", "tenant_applications.js",
            "tenants.js", "users.js", "verify_application.js",
        ]
        for script in page_scripts:
            position = base.index(f"/static/js/{script}")
            assert position < alpine_position
            assert base.count(f"/static/js/{script}") == 1
            assert f"/static/js/{script}?v={{" in base

    async def test_static_js_revalidates_cached_assets(self, client: Any) -> None:

        response = await client.get("/static/js/nurses.js")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-cache"

    async def test_page_shell_revalidates_cached_html(self, client: Any) -> None:

        response = await client.get("/pages/login")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-cache"

        nurses = Path("frontend/static/js/nurses.js").read_text(encoding="utf-8")
        shifts = Path("frontend/static/js/shifts.js").read_text(encoding="utf-8")
        assert "function nursesPage(" in nurses
        assert "function shiftsPage(" in shifts
        assert "function page(" not in nurses + shifts

        nurses_template = Path("frontend/templates/nurses.html").read_text(encoding="utf-8")
        shifts_template = Path("frontend/templates/shifts.html").read_text(encoding="utf-8")
        assert "/static/js/nurses.js" not in nurses_template
        assert "/static/js/shifts.js" not in shifts_template

    def test_boost_navigation_reinitializes_form_pages(self) -> None:

        expectations = {
            "login.js": ("initializeLoginPage", "login-form"),
            "setup_admin.js": ("initializeSetupAdminPage", "setup-form"),
            "verify_application.js": ("initializeVerifyApplicationPage", "verify-state"),
        }
        for filename, (initializer, target_id) in expectations.items():
            source = Path("frontend/static/js").joinpath(filename).read_text(encoding="utf-8")
            assert f"function {initializer}(" in source
            assert f"getElementById('{target_id}')" in source
            assert "htmx:afterSettle" in source
            assert "dataset.initialized" in source

    def test_subscriptions_page_redirects_to_tenants(self) -> None:

        r=httpx.get(f"{BASE}/pages/subscriptions", follow_redirects=False)
        assert r.status_code==303
        assert r.headers["location"].endswith("/pages/tenants")

    def test_tenant_nav_visible_only_to_super_admin(
    self,
    super_tok: Any,
    tenant: Any,
) -> None:

        super_r=httpx.get(f"{BASE}/pages/nav?logged_in=1",
                          headers={"Authorization":f"Bearer {super_tok}"})
        assert super_r.status_code==200
        assert "租户管理" in super_r.text
        assert "用户管理" in super_r.text
        assert "租户申请" in super_r.text
        assert "仿真管理" in super_r.text

        tenant_r=httpx.get(f"{BASE}/pages/nav?logged_in=1",
                           headers={"Authorization":f"Bearer {tenant['admin_token']}"})
        assert tenant_r.status_code==200
        assert "租户管理" not in tenant_r.text
        assert "用户管理" in tenant_r.text

    def test_root_redirects_to_login(self) -> None:

        r = httpx.get(f"{BASE}/", follow_redirects=False)
        assert r.status_code in (307, 302)
        assert r.headers["location"].endswith("/pages/login")

    def test_static_css_served(self) -> None:

        r = httpx.get(f"{BASE}/static/css/app.css")
        assert r.status_code == 200
        assert "text/css" in r.headers.get("content-type", "")

    def test_health(self) -> None:

        r = httpx.get(f"{BASE}/health")
        assert r.status_code == 200 and r.json()["status"] == "ok"

    def test_health_ready(self) -> None:

        r = httpx.get(f"{BASE}/health/ready")
        assert r.status_code == 200 and r.json()["status"] == "ok"
