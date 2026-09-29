"""Guardrails for batched schedule-list serialization."""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.schedules import _enrich_schedule_requests
from app.models import ScheduleRequest


class _EnrichedScheduleRequest:
    tenant_name: str
    requested_by_name: str | None


class _FakeResult:
    def __init__(self, rows: Sequence[object]):
        self._rows = rows

    def all(self) -> Sequence[object]:
        return self._rows


class _FakeSession:
    def __init__(self) -> None:
        self.execute_count = 0

    async def execute(self, statement: object) -> _FakeResult:
        self.execute_count += 1
        sql = str(statement)
        if "tenants" in sql:
            rows = [type("Tenant", (), {"id": "tenant-1", "name": "Tenant One"})()]
        elif "users" in sql:
            rows = [type("User", (), {"id": "user-1", "first_name": "A", "last_name": "N"})()]
        else:
            raise AssertionError(f"unexpected schedule list query: {sql}")
        return _FakeResult(rows)


def _schedule_request(request_id: str) -> _EnrichedScheduleRequest:
    request = type(
        "ScheduleRequest",
        (),
        {"id": request_id, "tenant_id": "tenant-1", "requested_by": "user-1"},
    )()
    return cast(_EnrichedScheduleRequest, request)


async def test_schedule_request_enrichment_uses_two_batched_queries() -> None:
    fake_session = _FakeSession()
    session = cast(AsyncSession, fake_session)
    rows: list[_EnrichedScheduleRequest] = [
        _schedule_request("request-1"),
        _schedule_request("request-2"),
    ]

    await _enrich_schedule_requests(
        session,
        cast(Sequence[ScheduleRequest], rows),
    )

    assert fake_session.execute_count == 2
    assert all(row.tenant_name == "Tenant One" for row in rows)
    assert all(row.requested_by_name == "NA" for row in rows)


async def test_schedule_request_enrichment_empty_list_without_queries() -> None:
    fake_session = _FakeSession()
    session = cast(AsyncSession, fake_session)

    await _enrich_schedule_requests(
        session,
        cast(Sequence[ScheduleRequest], []),
    )

    assert fake_session.execute_count == 0
