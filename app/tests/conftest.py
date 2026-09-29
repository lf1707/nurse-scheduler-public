"""Pytest configuration and shared fixtures for integration tests.

Uses the live API server at http://localhost:8000 (inside docker container).
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator, Iterator

import psycopg2
import pyotp
import pytest
from httpx import AsyncClient

SUPER_ADMIN_TEST_MFA_SECRET = pyotp.random_base32()


@pytest.fixture(scope="function")
async def client() -> AsyncGenerator[AsyncClient, None]:
    """Create an async test client against the live API server."""
    base_url = os.environ.get("API_BASE_URL", "http://localhost:8000")
    async with AsyncClient(base_url=base_url) as ac:
        yield ac


@pytest.fixture(scope="session", autouse=True)
def _mfa_enabled_for_live_tests() -> Iterator[None]:
    """Enable deterministic MFA for the shared live-API super-admin fixture."""
    super_email = os.environ.get("SUPER_ADMIN_EMAIL", "admin@example.com")
    connection = psycopg2.connect(
        dbname=os.environ.get("POSTGRES_DB", "nurse_scheduler"),
        user=os.environ.get("POSTGRES_USER", "nurse"),
        password=os.environ.get("POSTGRES_PASSWORD", "nurse"),
        host=os.environ.get("POSTGRES_HOST", "db"),
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT mfa_enabled, mfa_secret, mfa_recovery_hash FROM users WHERE email = %s",
                (super_email,),
            )
            row = cursor.fetchone()
            if row is None:
                raise AssertionError(f"Super admin not found: {super_email}")
        original: dict[str, object] = {
            "mfa_enabled": row[0],
            "mfa_secret": row[1],
            "mfa_recovery_hash": row[2],
        }
        with connection.cursor() as cursor:
            cursor.execute("SET app.is_super = '1'")
            cursor.execute(
                "UPDATE users SET mfa_enabled = true, mfa_secret = %s, "
                "mfa_recovery_hash = NULL WHERE email = %s",
                (SUPER_ADMIN_TEST_MFA_SECRET, super_email),
            )
        connection.commit()
        os.environ["SUPER_ADMIN_TEST_MFA_SECRET"] = SUPER_ADMIN_TEST_MFA_SECRET
        try:
            yield None
        finally:
            _restore_super_admin_mfa(connection, original, super_email)
    finally:
        connection.close()


def _restore_super_admin_mfa(
    connection: psycopg2.extensions.connection,
    original: dict[str, object],
    super_email: str,
) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SET app.is_super = '1'")
        cursor.execute(
            "UPDATE users SET mfa_enabled = %s, mfa_secret = %s, "
            "mfa_recovery_hash = %s WHERE email = %s",
            (
                original["mfa_enabled"],
                original["mfa_secret"],
                original["mfa_recovery_hash"],
                super_email,
            ),
        )
    connection.commit()
