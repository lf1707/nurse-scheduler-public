"""Tests for audit-event partitioning, export, and retention.

These tests run against the live dev database (http://localhost:9000) to
verify that partitioning, export, watermark, and legal-hold behavior work
end-to-end after the migration.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path

import pytest
from pytest import MonkeyPatch
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_export import export_audit_events, get_watermark
from app.core.audit_partitions import (
    ensure_future_partitions,
    ensure_partition,
    expire_partition,
    partition_exists,
)
from app.core.config import settings
from app.core.database import async_session_factory, clear_tenant_context

pytestmark = pytest.mark.asyncio(loop_scope='module')

async def _get_session() -> AsyncSession:
    session = async_session_factory()
    await clear_tenant_context(session)
    return session

async def test_audit_table_is_partitioned() -> None:
    """Verify the table is now a partitioned table with child partitions."""
    async with async_session_factory() as session:
        await clear_tenant_context(session)
        result = await session.execute(text("\n            SELECT\n                c.relkind::text,\n                (SELECT count(*) FROM pg_inherits\n                 WHERE inhparent = 'security_audit_events'::regclass) AS child_count\n            FROM pg_class c\n            WHERE c.relname = 'security_audit_events'\n        "))
        row = result.first()
        assert row is not None, 'security_audit_events table not found'
        assert row.relkind == 'p', f'Expected partitioned table, got relkind={row.relkind}'
        assert row.child_count >= 1, 'Expected at least 1 child partition'

async def test_audit_table_has_composite_primary_key() -> None:
    """The partitioned table must have (id, created_at) as PK."""
    async with async_session_factory() as session:
        await clear_tenant_context(session)
        result = await session.execute(text("\n            SELECT array_agg(a.attname ORDER BY k.n)\n            FROM pg_index i\n            JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)\n            JOIN generate_subscripts(i.indkey, 1) AS k(n) ON true\n            WHERE i.indrelid = 'security_audit_events'::regclass\n            AND i.indisprimary\n        "))
        pk_cols = result.scalar()
        assert pk_cols is not None
        assert set(pk_cols) == {'id', 'created_at'}, f'PK columns: {pk_cols}'

async def test_audit_table_still_has_rls_and_triggers() -> None:
    """RLS and append-only triggers must survive the migration."""
    async with async_session_factory() as session:
        await clear_tenant_context(session)
        result = await session.execute(text("\n            SELECT relrowsecurity, relforcerowsecurity\n            FROM pg_class WHERE relname = 'security_audit_events'\n        "))
        row = result.first()
        assert row is not None, 'security_audit_events table not found'
        assert row.relrowsecurity is True
        assert row.relforcerowsecurity is True
        result = await session.execute(text("\n            SELECT 1 FROM pg_proc WHERE proname = 'security_audit_events_append_only'\n        "))
        assert result.scalar() == 1

async def test_truncate_on_audit_event_is_rejected() -> None:
    """The append-only trigger must reject TRUNCATE."""
    async with async_session_factory() as session:
        await clear_tenant_context(session)
        await session.execute(text("\n            INSERT INTO security_audit_events (id, action, outcome, details, created_at)\n            VALUES (gen_random_uuid()::text, 'test.action', 'success', '{}'::jsonb, now())\n        "))
        await session.commit()
        with pytest.raises(Exception, match='append-only'):
            await session.execute(text('\n                TRUNCATE security_audit_events\n            '))
            await session.commit()
        await session.rollback()
        result = await session.execute(text("\n            SELECT count(*) FROM security_audit_events WHERE action = 'test.action'\n        "))
        count = result.scalar()
        assert count is not None and count >= 1

async def test_watermark_table_exists() -> None:
    """The audit_export_watermarks table must exist."""
    async with async_session_factory() as session:
        await clear_tenant_context(session)
        result = await session.execute(text("\n            SELECT 1 FROM pg_tables\n            WHERE schemaname = 'public' AND tablename = 'audit_export_watermarks'\n        "))
        assert result.scalar() == 1

async def test_ensure_partition_creates_new_month() -> None:
    """ensure_partition should create a partition for a future month."""
    future = datetime.now(UTC) + timedelta(days=365)
    year, month = (future.year, future.month)
    async with async_session_factory() as session:
        await clear_tenant_context(session)
        created = await ensure_partition(session, year, month)
        await session.commit()
    assert created is True
    assert await _check_partition_exists(year, month)
    async with async_session_factory() as session:
        await clear_tenant_context(session)
        await expire_partition(session, year, month)
        await session.commit()
    assert not await _check_partition_exists(year, month)

async def test_ensure_partition_rescues_rows_in_default_partition() -> None:
    """Rows that landed in the default partition are preserved when the
    partition for their month is created later (B2-8 defensive retry)."""
    future = datetime.now(UTC) + timedelta(days=400)
    year, month = (future.year, future.month)
    if await _check_partition_exists(year, month):
        async with async_session_factory() as session:
            await clear_tenant_context(session)
            await expire_partition(session, year, month)
            await session.commit()
    async with async_session_factory() as session:
        await clear_tenant_context(session)
        await session.execute(text("INSERT INTO security_audit_events (id, action, outcome, details, created_at) VALUES (gen_random_uuid()::text, 'test.rescue', 'success', '{}'::jsonb, :ts)"), {'ts': future})
        await session.commit()
    async with async_session_factory() as session:
        await clear_tenant_context(session)
        created = await ensure_partition(session, year, month)
        await session.commit()
    assert created is True
    async with async_session_factory() as session:
        await clear_tenant_context(session)
        rescued = await session.execute(text("SELECT count(*) FROM security_audit_events WHERE action = 'test.rescue'"))
        rescued_count = rescued.scalar()
        assert rescued_count is not None and rescued_count >= 1
        await session.commit()
    async with async_session_factory() as session:
        await clear_tenant_context(session)
        await expire_partition(session, year, month)
        await session.commit()

@pytest.mark.parametrize('now', [datetime(2026, 1, 29, tzinfo=UTC), datetime(2026, 3, 31, tzinfo=UTC), datetime(2026, 5, 31, tzinfo=UTC), datetime(2026, 8, 31, tzinfo=UTC), datetime(2026, 10, 31, tzinfo=UTC), datetime(2026, 12, 29, tzinfo=UTC)])
async def test_future_partitions_use_calendar_months(monkeypatch: MonkeyPatch, now: datetime) -> None:
    expected = []
    year, month = (now.year, now.month - 1)
    for _ in range(5):
        month += 1
        if month > 12:
            year += 1
            month = 1
        expected.append((year, month))
    created = []

    async def fake_ensure_partition(session: AsyncSession, target_year: int, target_month: int) -> None:
        created.append((target_year, target_month))
    monkeypatch.setattr('app.core.audit_partitions.ensure_partition', fake_ensure_partition)

    class FixedDatetime:

        @classmethod
        def now(cls, tz: tzinfo | None=None) -> datetime:
            return now if tz is None else now.astimezone(tz)
    monkeypatch.setattr('app.core.audit_partitions.datetime', FixedDatetime)
    monkeypatch.setattr(settings, 'AUDIT_PARTITION_MONTHS_AHEAD', 4)
    async with async_session_factory() as session:
        count = await ensure_future_partitions(session)
    assert count == 5
    assert created == expected

async def _check_partition_exists(year: int, month: int) -> bool:
    async with async_session_factory() as session:
        await clear_tenant_context(session)
        return await partition_exists(session, year, month)

async def test_export_watermark_advances_only_on_success(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    """Export should create an artifact and advance the watermark."""
    monkeypatch.setattr(settings, 'BACKUP_PASSPHRASE', 'test-passphrase-for-export')
    monkeypatch.setattr(settings, 'AUDIT_EXPORT_DIR', str(tmp_path))
    async with async_session_factory() as session:
        await clear_tenant_context(session)
        await session.execute(text("\n            INSERT INTO security_audit_events (id, action, outcome, details, created_at)\n            VALUES ('test-export-1', 'test.export', 'success', '{}'::jsonb, now())\n        "))
        await session.commit()
    async with async_session_factory() as session:
        await clear_tenant_context(session)
        result = await export_audit_events(session, export_dir=str(tmp_path))
    assert result['event_count'] >= 1
    assert result['artifact_path'] is not None
    assert os.path.exists(result['artifact_path'])
    async with async_session_factory() as session:
        await clear_tenant_context(session)
        wm = await get_watermark(session)
    assert wm is not None
    assert isinstance(wm.event_count, int)
    assert wm.event_count >= 1
    async with async_session_factory() as session:
        await clear_tenant_context(session)
        result2 = await export_audit_events(session, export_dir=str(tmp_path))
    assert result2['event_count'] == 0
    async with async_session_factory() as session:
        await clear_tenant_context(session)
        heartbeat = await get_watermark(session)
    assert heartbeat is not None
    assert heartbeat.event_count == 0
    assert heartbeat.artifact_path is None
    assert heartbeat.last_exported_at == wm.last_exported_at
    assert heartbeat.created_at >= wm.created_at
