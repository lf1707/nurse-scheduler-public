"""B2-10: refresh-token beat cleanup regression tests."""

from __future__ import annotations

import datetime
import os
import uuid

import psycopg2

from app.tasks.system_tasks import cleanup_refresh_tokens

DB_PARAMS = {
    "dbname": os.environ.get("POSTGRES_DB", "nurse_scheduler"),
    "user": os.environ.get("POSTGRES_USER", "nurse"),
    "password": os.environ.get("POSTGRES_PASSWORD", "nurse"),
    "host": os.environ.get("POSTGRES_HOST", "db"),
}


def _admin_user_id() -> str:
    with psycopg2.connect(**DB_PARAMS) as connection, connection.cursor() as cursor:
        cursor.execute("SET app.is_super = '1'")
        cursor.execute(
            "SELECT id FROM users WHERE email = 'admin@example.com' LIMIT 1"
        )
        row = cursor.fetchone()
    assert row, "super admin must exist"
    assert isinstance(row[0], str)
    return row[0]


def _insert_token(user_id: str, *, expired_days: int | None, revoked_days: int | None) -> str:
    token_id = str(uuid.uuid4())
    now = datetime.datetime.now(datetime.UTC)
    expires = now - datetime.timedelta(days=expired_days) if expired_days is not None else now + datetime.timedelta(days=30)
    revoked = now - datetime.timedelta(days=revoked_days) if revoked_days is not None else None
    with psycopg2.connect(**DB_PARAMS) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET app.is_super = '1'")
            cursor.execute(
                "INSERT INTO refresh_tokens (id, user_id, family_id, token_hash,"
                " token_version, expires_at, revoked_at)"
                " VALUES (%s, %s, %s, %s, 1, %s, %s)",
                (
                    token_id, user_id, str(uuid.uuid4()),
                    uuid.uuid4().hex, expires, revoked,
                ),
            )
        connection.commit()
    return token_id


def _token_exists(token_id: str) -> bool:
    with psycopg2.connect(**DB_PARAMS) as connection, connection.cursor() as cursor:
        cursor.execute("SET app.is_super = '1'")
        cursor.execute(
            "SELECT 1 FROM refresh_tokens WHERE id = %s", (token_id,)
        )
        return cursor.fetchone() is not None


def _delete_token(token_id: str) -> None:
    with psycopg2.connect(**DB_PARAMS) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET app.is_super = '1'")
            cursor.execute("DELETE FROM refresh_tokens WHERE id = %s", (token_id,))
        connection.commit()


def test_cleanup_refresh_tokens_deletes_expired_and_stale_revoked() -> None:
    user_id = _admin_user_id()
    expired = _insert_token(user_id, expired_days=31, revoked_days=None)
    stale_revoked = _insert_token(user_id, expired_days=10, revoked_days=8)
    fresh_revoked = _insert_token(user_id, expired_days=10, revoked_days=1)
    active = _insert_token(user_id, expired_days=10, revoked_days=None)

    try:
        result = cleanup_refresh_tokens()
        assert result["deleted"] >= 2
        assert not _token_exists(expired)
        assert not _token_exists(stale_revoked)
        assert _token_exists(fresh_revoked)
        assert _token_exists(active)
    finally:
        for token_id in (expired, stale_revoked, fresh_revoked, active):
            _delete_token(token_id)
