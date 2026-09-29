"""Regression coverage for stale RUNNING schedule requests."""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.database import clear_tenant_context
from app.models import ScheduleRequest, Tenant
from app.models.enums import ScheduleStatus
from app.tasks.schedule_tasks import _fail_stale_schedule_requests


async def _create_running_request(
    *,
    tenant_id: str,
    age_seconds: int,
    timeout_seconds: int = 60,
    error_message: str | None = None,
    session_factory: async_sessionmaker[AsyncSession],
) -> ScheduleRequest:
    request_id = str(uuid.uuid4())
    sequence = uuid.uuid4().int % 1_000_000_000
    request = ScheduleRequest(
        id=request_id,
        tenant_id=tenant_id,
        period_start=datetime.datetime.now(datetime.UTC).date(),
        period_days=1,
        request_date=datetime.datetime.now(datetime.UTC).date(),
        daily_sequence=sequence,
        status=ScheduleStatus.RUNNING,
        solver_config={"timeout_seconds": timeout_seconds},
        started_at=datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=age_seconds),
        error_message=error_message,
)
    async with session_factory() as session:
        await clear_tenant_context(session)
        session.add(request)
        await session.commit()
    return request


async def _delete_request(
    request_id: str,
    *,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.models import SecurityAuditEvent

    async with session_factory() as session:
        await clear_tenant_context(session)
        request = await session.get(ScheduleRequest, request_id)
        if request:
            await session.delete(request)
        events = (
            await session.execute(
                select(SecurityAuditEvent).where(
                    SecurityAuditEvent.action == "schedule.generate.stale_failure",
                )
            )
        ).scalars().all()
        for event in events:
            if event.details.get("request_id") == request_id:
                await session.delete(event)
        await session.commit()


async def test_stale_running_requests_are_failed_and_messages_preserved() -> None:
    tenant_id = str(uuid.uuid4())
    engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    session_factory = async_sessionmaker(
        engine,
        expire_on_commit=False,
        autoflush=False,
    )
    try:
        async with session_factory() as session:
            await clear_tenant_context(session)
            session.add(Tenant(id=tenant_id, name="Stale Schedule Test", slug=f"stale-{tenant_id[:12]}"))
            await session.commit()

        stale_id = (await _create_running_request(
            tenant_id=tenant_id,
            age_seconds=121,
            error_message="原有失败原因",
            session_factory=session_factory,
        )).id
        fresh_id = (await _create_running_request(
            tenant_id=tenant_id,
            age_seconds=1,
            session_factory=session_factory,
        )).id

        try:
            failed_ids = await _fail_stale_schedule_requests()

            assert stale_id in failed_ids
            assert fresh_id not in failed_ids
            async with session_factory() as session:
                await clear_tenant_context(session)
                stale = await session.get(ScheduleRequest, stale_id)
                fresh = await session.get(ScheduleRequest, fresh_id)
                assert stale is not None
                assert fresh is not None
                assert stale.status == ScheduleStatus.FAILED
                assert stale.error_message == "原有失败原因"
                assert stale.completed_at is not None
                assert fresh.status == ScheduleStatus.RUNNING
                assert fresh.completed_at is None
        finally:
            await _delete_request(stale_id, session_factory=session_factory)
            await _delete_request(fresh_id, session_factory=session_factory)
            async with session_factory() as session:
                await clear_tenant_context(session)
                tenant = await session.get(Tenant, tenant_id)
                if tenant:
                    await session.delete(tenant)
                await session.commit()
    finally:
        await engine.dispose()
