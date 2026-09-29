"""Stage 4 end-to-end acceptance: enqueue → solve → persist.

Seeds a tenant with a balanced roster config (42 nurses: 28 RN + 14 LV,
2 shifts/day, skill mix 4RN+2LV per slot, target 4 shifts/period), inserts a
ScheduleRequest, dispatches the Celery task synchronously (worker pickup),
then polls until COMPLETED and verifies assignments landed in the DB.

Run inside the api container:
    docker compose exec api python /app/scripts/e2e_stage4.py [tenant_slug]
"""

from __future__ import annotations

import asyncio
import datetime
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, text

from app.core.database import async_session_factory, session_scope
from app.models import (
    DayGroup,
    DayGroupDay,
    Nurse,
    Role,
    Schedule,
    ScheduleRequest,
    ShiftTemplate,
    SkillMixRequirement,
    SkillMixRule,
    Tenant,
)
from app.models.enums import ScheduleStatus
from app.tasks.celery_app import celery_app

PERIOD_START = datetime.date(2026, 10, 1)
PERIOD_DAYS = 14
N_RN, N_LV = 28, 14
RN_PER, LV_PER = 4, 2
TARGET = 4
SLUG = sys.argv[1] if len(sys.argv) > 1 else "e2e-stage4"


async def seed(tenant_id: str) -> None:
    """Insert roles/shifts/nurses/skill-mix for the tenant (RLS-scoped)."""
    async with session_scope(tenant_id=tenant_id) as s:
        # DayGroup covering all 7 weekdays
        dg = DayGroup(id=str(uuid.uuid4()), tenant_id=tenant_id, name="All week")
        s.add(dg)
        await s.flush()
        for d in range(1, 8):
            s.add(DayGroupDay(id=str(uuid.uuid4()), tenant_id=tenant_id, day_group_id=dg.id, day_number=d))

        role_rn = Role(id=str(uuid.uuid4()), tenant_id=tenant_id, name="RN", code="RN")
        role_lv = Role(id=str(uuid.uuid4()), tenant_id=tenant_id, name="LV", code="LV")
        s.add_all([role_rn, role_lv])
        await s.flush()

        shift_e = ShiftTemplate(
            id=str(uuid.uuid4()), tenant_id=tenant_id, code="E", name="Early",
            start_time=datetime.time(7, 0), end_time=datetime.time(15, 0),
            duration_hours=8.0, day_group_id=dg.id,
        )
        shift_n = ShiftTemplate(
            id=str(uuid.uuid4()), tenant_id=tenant_id, code="N", name="Night",
            start_time=datetime.time(15, 0), end_time=datetime.time(23, 0),
            duration_hours=8.0, day_group_id=dg.id,
        )
        s.add_all([shift_e, shift_n])
        await s.flush()

        # Nurses with contracts
        from app.models import Contract, nurse_roles

        nurses = []
        for i in range(N_RN + N_LV):
            role = role_rn if i < N_RN else role_lv
            n = Nurse(
                id=str(uuid.uuid4()), tenant_id=tenant_id, employee_id=f"E2E-{i:03d}",
                first_name=f"J{i}", last_name="D", is_available=True,
            )
            s.add(n)
            await s.flush()
            s.add(Contract(tenant_id=tenant_id, nurse_id=n.id, shifts_per_period=TARGET, max_shifts_per_period=TARGET + 2))
            await s.execute(nurse_roles.insert().values(nurse_id=n.id, role_id=role.id))
            nurses.append((n, role))

        # Skill mix rules for both shifts
        for st in (shift_e, shift_n):
            rule = SkillMixRule(
                id=str(uuid.uuid4()), tenant_id=tenant_id, name=f"{st.code}: {RN_PER}RN+{LV_PER}LV",
                shift_template_id=st.id, priority=0, is_active=True,
            )
            s.add(rule)
            await s.flush()
            s.add_all([
                SkillMixRequirement(tenant_id=tenant_id, skill_mix_rule_id=rule.id, role_id=role_rn.id, count=RN_PER),
                SkillMixRequirement(tenant_id=tenant_id, skill_mix_rule_id=rule.id, role_id=role_lv.id, count=LV_PER),
            ])

        # The ScheduleRequest itself
        req = ScheduleRequest(
            id=str(uuid.uuid4()), tenant_id=tenant_id,
            period_start=PERIOD_START, period_days=PERIOD_DAYS,
            solver_config={"timeout_seconds": 60, "num_workers": 4},
        )
        s.add(req)
        await s.flush()
        request_id = req.id
    return request_id


async def get_or_create_tenant() -> str:
    async with async_session_factory() as s:
        # tenants table has no RLS — safe to query/insert directly.
        row = await s.execute(select(Tenant).where(Tenant.slug == SLUG))
        t = row.scalar_one_or_none()
        if t:
            return t.id
        t = Tenant(id=str(uuid.uuid4()), name=f"E2E Stage4 {SLUG}", slug=SLUG)
        s.add(t)
        await s.commit()
        return t.id


async def wait_completed(request_id: str, tenant_id: str, timeout_s: int = 90) -> dict:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        await asyncio.sleep(1.5)
        async with session_scope(tenant_id=tenant_id) as s:
            req = (
                await s.execute(select(ScheduleRequest).where(ScheduleRequest.id == request_id))
            ).scalar_one()
            if req.status in (ScheduleStatus.COMPLETED, ScheduleStatus.FAILED, ScheduleStatus.CANCELLED):
                return {"status": req.status.value, "outcome": req.outcome.value if req.outcome else None,
                        "stats": req.stats, "error": req.error_message}
    return {"status": "timeout"}


async def verify(request_id: str, tenant_id: str) -> None:
    async with session_scope(tenant_id=tenant_id) as s:
        sched = (
            await s.execute(select(Schedule).where(Schedule.request_id == request_id))
        ).scalar_one()
        count = len(sched.assignments)
        print(f"Schedule {sched.id[:8]} outcome={sched.outcome.value} "
              f"objective={sched.objective_value} solve={sched.solve_time_seconds}s assignments={count}")
        assert count == PERIOD_DAYS * 2 * (RN_PER + LV_PER), (
            f"expected {PERIOD_DAYS * 2 * (RN_PER + LV_PER)} assignments, got {count}"
        )


async def main() -> int:
    tenant_id = await get_or_create_tenant()
    print(f"Tenant: {tenant_id[:8]}")

    # Idempotency: wipe previous e2e artifacts for this tenant.
    async with session_scope(tenant_id=tenant_id) as s:
        for tbl in ("assignments", "schedules", "schedule_requests",
                    "skill_mix_requirements", "skill_mix_rules", "contracts",
                    "nurse_roles", "nurses", "shift_templates", "day_group_days",
                    "day_groups", "roles"):
            await s.execute(text(f"DELETE FROM {tbl}"))
    print("Cleaned previous e2e data")

    request_id = await seed(tenant_id)
    print(f"ScheduleRequest: {request_id[:8]}")

    # Dispatch via Celery (worker picks it up from Redis).
    result = celery_app.send_task("schedule.generate", args=[request_id], queue="scheduling")
    print(f"Dispatched task {result.id[:8]}")

    final = await wait_completed(request_id, tenant_id)
    print(f"Final status: {final}")
    if final["status"] != "completed":
        return 1

    await verify(request_id, tenant_id)
    print("E2E STAGE4 ACCEPTANCE PASSED ✓")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
