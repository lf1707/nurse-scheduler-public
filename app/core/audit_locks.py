"""Coordination locks for audit export and partition lifecycle.

``DETACH PARTITION`` takes an ``ACCESS EXCLUSIVE`` lock on the parent table,
while incremental exports read that parent for the whole run.  A transaction
advisory lock keeps those jobs from colliding without depending on Celery's
single-process beat scheduling.
"""

from __future__ import annotations

import logging
from asyncio import sleep as _asyncio_sleep

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings

logger = logging.getLogger(__name__)

AUDIT_EXPORT_LOCK_KEY = "security_audit_events:export-or-detach"
LOCK_TIMEOUT_SQLSTATE = "55P03"

asyncio_sleep = _asyncio_sleep


def _is_lock_timeout(error: DBAPIError) -> bool:
    original = error.orig
    return (
        getattr(original, "pgcode", None) == LOCK_TIMEOUT_SQLSTATE
        or getattr(original, "sqlstate", None) == LOCK_TIMEOUT_SQLSTATE
    )


async def acquire_audit_export_lock(
    session: AsyncSession,
    *,
    retries: int | None = None,
) -> bool:
    """Acquire the transaction advisory lock, bounded by PostgreSQL lock_timeout.

    A timeout aborts the current transaction, so each retry starts with a
    rollback. The advisory lock is released when the caller commits or rolls
    back its successful transaction.
    """
    if retries is None:
        retries = settings.AUDIT_EXPORT_LOCK_RETRIES

    timeout_ms = int(settings.AUDIT_LOCK_TIMEOUT_SECONDS * 1000)
    for attempt in range(retries + 1):
        if attempt:
            await asyncio_sleep(settings.AUDIT_LOCK_RETRY_DELAY_SECONDS)
        try:
            await session.execute(
                text("SELECT set_config('lock_timeout', :timeout, true)"),
                {"timeout": f"{timeout_ms}ms"},
            )
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
                {"lock_key": AUDIT_EXPORT_LOCK_KEY},
            )
            return True
        except DBAPIError as error:
            await session.rollback()
            if not _is_lock_timeout(error):
                raise
            logger.warning(
                "Audit export lock attempt %s timed out after %sms",
                attempt + 1,
                timeout_ms,
            )

    return False
