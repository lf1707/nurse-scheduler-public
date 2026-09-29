"""Partition management for the security_audit_events table.

Provides functions to create future monthly partitions and to expire (detach
+ drop) old partitions after they have been archived.  All operations are
idempotent and designed to run from Celery beat.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings

logger = logging.getLogger(__name__)

PARTITION_PREFIX = "security_audit_events_"


def _partition_name(year: int, month: int) -> str:
    return f"{PARTITION_PREFIX}{year:04d}_{month:02d}"


def _month_range(year: int, month: int) -> tuple[str, str]:
    """Return ISO start and exclusive-end dates for a month partition."""
    start = datetime(year, month, 1, tzinfo=UTC)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=UTC)
    else:
        end = datetime(year, month + 1, 1, tzinfo=UTC)
    return start.date().isoformat(), end.date().isoformat()


async def ensure_partition(
    session: AsyncSession, year: int, month: int
) -> bool:
    """Create a monthly partition if it does not already exist."""
    name = _partition_name(year, month)
    exists = (
        await session.execute(
            text("SELECT 1 FROM pg_tables WHERE schemaname='public' AND tablename=:name"),
            {"name": name},
        )
    ).scalar() == 1
    if exists:
        return True

    start_date, end_date = _month_range(year, month)
    create_sql = (
        f"CREATE TABLE {name} "
        f"PARTITION OF security_audit_events "
        f"FOR VALUES FROM ('{start_date}') TO ('{end_date}')"
    )
    try:
        await session.execute(text(create_sql))
    except IntegrityError:
        # The default partition may hold rows for this month if the partition
        # was skipped (e.g. clock drift).  Move them aside, create the real
        # partition, then move them back with the append-only trigger disabled.
        logger.warning(
            "Default partition holds rows for %s; relocating before retry", name,
        )
        await session.rollback()
        temp_name = f"{name}_rescue"
        await session.execute(text(f"DROP TABLE IF EXISTS {temp_name}"))
        await session.execute(
            text(
                f"CREATE TABLE {temp_name} (LIKE security_audit_events INCLUDING ALL)"
            )
        )
        await session.execute(
            text("SET LOCAL app.audit_rescue = '1'")
        )
        await session.execute(
            text(
                f"INSERT INTO {temp_name} SELECT * FROM security_audit_events_default "
                f"WHERE created_at >= '{start_date}' AND created_at < '{end_date}'"
            )
        )
        await session.execute(
            text(
                f"DELETE FROM security_audit_events_default "
                f"WHERE created_at >= '{start_date}' AND created_at < '{end_date}'"
            )
        )
        await session.execute(
            text("SET LOCAL app.audit_rescue = ''")
        )
        await session.execute(text(create_sql))
        await _create_no_rewrite_trigger(session, name)
        await session.execute(
            text(f"ALTER TABLE {name} DISABLE TRIGGER {name}_no_rewrite")
        )
        await session.execute(
            text(f"INSERT INTO {name} SELECT * FROM {temp_name}")
        )
        await session.execute(
            text(f"ALTER TABLE {name} ENABLE TRIGGER {name}_no_rewrite")
        )
        await session.execute(text(f"DROP TABLE {temp_name}"))
        logger.info("Created audit partition %s [%s, %s) with rescued rows", name, start_date, end_date)
        return True

    await _create_no_rewrite_trigger(session, name)
    logger.info("Created audit partition %s [%s, %s)", name, start_date, end_date)
    return True


async def _create_no_rewrite_trigger(
    session: AsyncSession, name: str
) -> None:
    await session.execute(
        text(
            f"CREATE TRIGGER {name}_no_rewrite "
            f"BEFORE UPDATE OR DELETE ON {name} "
            f"FOR EACH ROW EXECUTE FUNCTION security_audit_events_append_only()"
        )
    )


async def ensure_future_partitions(
    session: AsyncSession, *, months_ahead: int | None = None
) -> int:
    """Create partitions from the current month through N months ahead."""
    if months_ahead is None:
        months_ahead = settings.AUDIT_PARTITION_MONTHS_AHEAD
    now = datetime.now(UTC)
    count = 0
    for i in range(months_ahead + 1):
        month_index = now.month - 1 + i
        await ensure_partition(session, now.year + month_index // 12, month_index % 12 + 1)
        count += 1
    return count


async def partition_exists(
    session: AsyncSession, year: int, month: int
) -> bool:
    name = _partition_name(year, month)
    return (
        await session.execute(
            text("SELECT 1 FROM pg_tables WHERE schemaname='public' AND tablename=:name"),
            {"name": name},
        )
    ).scalar() == 1


async def list_monthly_partitions(session: AsyncSession) -> list[str]:
    """List non-default monthly partitions of audit events, newest first."""
    result = await session.execute(text("""
        SELECT inhrelid::regclass::text AS name
        FROM pg_inherits
        WHERE inhparent = 'security_audit_events'::regclass
        AND inhrelid::regclass::text != 'security_audit_events_default'
        ORDER BY inhrelid::regclass::text DESC
    """))
    return [row.name for row in result]


async def expire_partition(
    session: AsyncSession, year: int, month: int
) -> bool:
    """Detach and drop a monthly partition.

    The caller MUST verify the partition has been archived before calling
    this function.  This operation is irreversible.
    """
    name = _partition_name(year, month)
    if not await partition_exists(session, year, month):
        logger.warning("Partition %s does not exist, skipping", name)
        return False

    await session.execute(
        text(f"ALTER TABLE security_audit_events DETACH PARTITION {name}")
    )
    await session.execute(text(f"DROP TABLE {name}"))
    logger.info("Expired (detached + dropped) partition %s", name)
    return True
