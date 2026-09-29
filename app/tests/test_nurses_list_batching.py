"""Guardrails for batched nurse-list serialization."""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.nurses import _nurse_reads
from app.models import Contract, Nurse


class _FakeResult:
    def __init__(self, rows: Sequence[object]):
        self._rows = rows

    def scalars(self) -> _FakeResult:
        return self

    def all(self) -> Sequence[object]:
        return self._rows


class _FakeSession:
    def __init__(self) -> None:
        self.execute_count = 0

    async def execute(self, statement: object) -> _FakeResult:
        self.execute_count += 1
        sql = str(statement)
        rows: Sequence[object]
        if "nurse_roles" in sql:
            rows = [("nurse-1", "role-b"), ("nurse-1", "role-a"), ("nurse-2", "role-c")]
        elif "nurse_skills" in sql:
            rows = [("nurse-1", "skill-b"), ("nurse-1", "skill-a")]
        elif "contracts" in sql:
            rows = [
                Contract(
                    id="contract-1",
                    tenant_id="tenant-1",
                    nurse_id="nurse-1",
                    shifts_per_period=4,
                    max_shifts_per_period=5,
                    min_rest_hours=11,
                    max_consecutive_days=5,
                    enforce_balanced=True,
                    enforce_shifts_per_period=True,
                    enforce_one_shift_per_day=True,
                ),
            ]
        elif "users" in sql:
            rows = [
                type("User", (), {"nurse_id": "nurse-1", "email": "n1@example.com"})(),
                type("User", (), {"nurse_id": "nurse-2", "email": "n2@example.com"})(),
            ]
        elif "tenants" in sql:
            rows = [type("Tenant", (), {"id": "tenant-1", "name": "Tenant One"})()]
        else:
            raise AssertionError(f"unexpected list query: {sql}")
        return _FakeResult(rows)


def _nurse(nurse_id: str) -> Nurse:
    return Nurse(
        id=nurse_id,
        tenant_id="tenant-1",
        employee_id=nurse_id.upper(),
        first_name="First",
        last_name="Last",
        is_available=True,
        preferences={},
        created_at=datetime.datetime.now(datetime.UTC),
    )


async def test_nurse_reads_use_five_batched_queries_for_two_nurses() -> None:
    fake_session = _FakeSession()
    session = cast(AsyncSession, fake_session)
    rows = [_nurse("nurse-1"), _nurse("nurse-2")]

    items = await _nurse_reads(session, rows)

    assert fake_session.execute_count == 5
    assert [item.id for item in items] == ["nurse-1", "nurse-2"]
    assert items[0].role_ids == ["role-a", "role-b"]
    assert items[0].skill_ids == ["skill-a", "skill-b"]
    assert items[0].contract is not None
    assert items[0].contract.shifts_per_period == 4
    assert items[0].tenant_name == "Tenant One"
    assert items[0].user_email == "n1@example.com"
    assert items[1].contract is None
    assert items[1].user_email == "n2@example.com"


async def test_nurse_reads_empty_list_without_queries() -> None:
    fake_session = _FakeSession()
    session = cast(AsyncSession, fake_session)

    assert await _nurse_reads(session, []) == []
    assert fake_session.execute_count == 0
