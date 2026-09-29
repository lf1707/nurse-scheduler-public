"""Tests for the advisory lock that separates audit export from DETACH."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock

from pytest import MonkeyPatch
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit_locks
from app.core.audit_locks import acquire_audit_export_lock
from app.core.config import settings
from app.core.database import (
    async_session_factory,
    clear_tenant_context,
    engine,
)


class _FakeLockTimeoutError(Exception):
    sqlstate = "55P03"


class _TimeoutSession:
    def __init__(self) -> None:
        self.execute_calls: list[tuple[str, dict[str, str] | None]] = []
        self.rollback_count = 0

    async def execute(
        self,
        statement: object,
        params: dict[str, str] | None = None,
    ) -> None:
        self.execute_calls.append((str(statement), params))
        if "pg_advisory_xact_lock" in str(statement):
            raise DBAPIError(
                "SELECT pg_advisory_xact_lock", params, _FakeLockTimeoutError()
            )

    async def rollback(self) -> None:
        self.rollback_count += 1


async def test_lock_timeout_rolls_back_and_retries() -> None:
    fake_session = _TimeoutSession()
    session = cast(AsyncSession, fake_session)
    sleep = AsyncMock()

    original_sleep = audit_locks.asyncio_sleep
    audit_locks.asyncio_sleep = sleep
    try:
        acquired = await acquire_audit_export_lock(session, retries=2)
    finally:
        audit_locks.asyncio_sleep = original_sleep

    assert acquired is False
    assert fake_session.rollback_count == 3
    assert sleep.await_count == 2
    assert len(fake_session.execute_calls) == 6
    assert all(
        "lock_timeout" in statement or "pg_advisory_xact_lock" in statement
        for statement, _params in fake_session.execute_calls
    )
    assert fake_session.execute_calls[0][1] == {"timeout": "2000ms"}


async def test_lock_release_allows_a_new_holder(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "AUDIT_LOCK_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(settings, "AUDIT_LOCK_RETRY_DELAY_SECONDS", 0)

    async with async_session_factory() as holder:
        await clear_tenant_context(holder)
        assert await acquire_audit_export_lock(holder) is True

        async with async_session_factory() as blocked:
            await clear_tenant_context(blocked)
            assert await acquire_audit_export_lock(blocked, retries=0) is False

        await holder.rollback()

    async with async_session_factory() as next_holder:
        await clear_tenant_context(next_holder)
        assert await acquire_audit_export_lock(next_holder, retries=0) is True
        await next_holder.rollback()

    await engine.dispose()
