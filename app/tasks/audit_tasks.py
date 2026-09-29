"""Celery tasks for audit-event partition management and incremental export."""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.audit_alerts import run_anomaly_scan
from app.core.audit_export import export_audit_events
from app.core.audit_partitions import ensure_future_partitions
from app.core.audit_retention import expire_eligible_partitions
from app.core.database import clear_tenant_context, task_session_factory
from app.tasks.celery_app import celery_task


async def _create_partitions() -> int:
    async with task_session_factory() as session_factory, session_factory() as session:
        await clear_tenant_context(session)
        count = await ensure_future_partitions(session)
        await session.commit()
        return count


@celery_task(name="audit.create_partitions")
def create_audit_partitions() -> int:
    """Ensure monthly partitions exist for the next few months."""
    return asyncio.run(_create_partitions())


async def _export_audit() -> dict[str, Any]:
    async with task_session_factory() as session_factory, session_factory() as session:
        await clear_tenant_context(session)
        return await export_audit_events(session)


@celery_task(name="audit.export_events")
def export_audit_events_task() -> dict[str, Any]:
    """Incrementally export audit events to an encrypted archive."""
    return asyncio.run(_export_audit())


async def _expire_old_partitions() -> list[str]:
    async with task_session_factory() as session_factory, session_factory() as session:
        await clear_tenant_context(session)
        expired = await expire_eligible_partitions(session)
        await session.commit()
        return expired


@celery_task(name="audit.expire_old_partitions")
def expire_old_audit_partitions() -> list[str]:
    """Expire (detach + drop) partitions older than the hot retention window."""
    return asyncio.run(_expire_old_partitions())


async def _run_anomaly_scan() -> list[dict[str, Any]]:
    async with task_session_factory() as session_factory, session_factory() as session:
        await clear_tenant_context(session)
        alerts = await run_anomaly_scan(session)
        await session.commit()
        return [
            {
                "rule": alert.rule,
                "key": alert.key,
                "count": alert.count,
                "threshold": alert.threshold,
            }
            for alert in alerts
        ]


@celery_task(name="audit.run_anomaly_scan")
def run_anomaly_scan_task() -> list[dict[str, Any]]:
    """Scan recent audit events for anomalies and fire alerts."""
    return asyncio.run(_run_anomaly_scan())
