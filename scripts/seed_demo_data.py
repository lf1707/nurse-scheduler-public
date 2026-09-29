"""Idempotently seed a production-safe demo tenant.

The seed never deletes existing rows. It creates missing demo records only and
refreshes the passwords of accounts owned by the demo tenant.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from datetime import date, time, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import async_session_factory
from app.core.security import hash_password
from app.models.enums import (
    RequestType,
    ScheduleStatus,
    SubscriptionPlan,
    UserRole,
)
from app.models.nurse import (
    Contract,
    Nurse,
    NursePreference,
    Role,
    Skill,
    nurse_roles,
    nurse_skills,
)
from app.models.rule import (
    ShiftSequenceRule,
    ShiftSequenceStep,
    SkillMixRequirement,
    SkillMixRule,
)
from app.models.schedule import ScheduleRequest
from app.models.shift import DayGroup, DayGroupDay, ShiftTemplate
from app.models.subscription import Subscription
from app.models.tenant import Tenant
from app.models.user import User
from app.tasks.celery_app import celery_app

DEMO_SLUG = "demo"
DEVELOPMENT_DEFAULT_PASSWORD = "demo-password-change-me"
LEGACY_DEMO_EMAILS = {
    "demo-admin@nurse-scheduler.local": "demo-admin@nurse-scheduler.dev",
    "demo-scheduler@nurse-scheduler.local": "demo-scheduler@nurse-scheduler.dev",
    "demo-viewer@nurse-scheduler.local": "demo-viewer@nurse-scheduler.dev",
    "demo-nurse@nurse-scheduler.local": "demo-nurse@nurse-scheduler.dev",
}


async def _enable_demo_context(
    session: AsyncSession,
    tenant_id: str,
) -> None:
    await session.execute(text("SET app.is_super = '1'"))
    await session.execute(
        text("SELECT set_config('app.tenant_id', :tenant_id, false)"),
        {"tenant_id": tenant_id},
    )


async def _ensure_tenant(session: AsyncSession) -> Tenant:
    tenant: Tenant | None = (
        await session.execute(select(Tenant).where(Tenant.slug == DEMO_SLUG))
    ).scalar_one_or_none()
    if tenant:
        if tenant.settings.get("kind") not in {"demo", "test"}:
            raise RuntimeError(f"Tenant slug {DEMO_SLUG!r} exists but is not a demo tenant")
        tenant.settings = {
            **tenant.settings,
            "kind": "test",
            "dataset": "demo",
            "description": "Production demo dataset managed as a test tenant",
        }
        return tenant

    tenant = Tenant(
        name="演示医院",
        slug=DEMO_SLUG,
        settings={
            "kind": "test",
            "dataset": "demo",
            "description": "Production demo dataset managed as a test tenant",
        },
        is_active=True,
    )
    session.add(tenant)
    await session.flush()
    return tenant


async def _ensure_subscription(
    session: AsyncSession,
    tenant_id: str,
) -> None:
    subscription = (
        await session.execute(select(Subscription).where(Subscription.tenant_id == tenant_id))
    ).scalar_one_or_none()
    if subscription:
        subscription.plan = SubscriptionPlan.DEMO
        subscription.is_canceled = False
        subscription.ends_at = None
        return
    session.add(
        Subscription(
            tenant_id=tenant_id,
            plan=SubscriptionPlan.DEMO,
            starts_at=func.now(),
            ends_at=None,
            is_canceled=False,
        )
    )


async def _ensure_roles(
    session: AsyncSession,
    tenant_id: str,
) -> dict[str, Role]:
    definitions = {
        "rn": ("Registered Nurse", "rn", "Registered nurse"),
        "en": ("Enrolled Nurse", "en", "Enrolled nurse"),
    }
    roles: dict[str, Role] = {}
    for code, (name, normalized_code, description) in definitions.items():
        role = (
            await session.execute(
                select(Role).where(Role.tenant_id == tenant_id, Role.code == normalized_code)
            )
        ).scalar_one_or_none()
        if not role:
            role = Role(tenant_id=tenant_id, name=name, code=code, description=description)
            session.add(role)
            await session.flush()
        roles[code] = role
    return roles


async def _ensure_skills(
    session: AsyncSession,
    tenant_id: str,
) -> dict[str, Skill]:
    definitions = {
        "bls": "Basic Life Support",
        "icu": "ICU Care",
        "triage": "Triage",
    }
    skills: dict[str, Skill] = {}
    for code, name in definitions.items():
        skill = (
            await session.execute(
                select(Skill).where(Skill.tenant_id == tenant_id, Skill.code == code)
            )
        ).scalar_one_or_none()
        if not skill:
            skill = Skill(tenant_id=tenant_id, name=name, code=code)
            session.add(skill)
            await session.flush()
        skills[code] = skill
    return skills


async def _ensure_day_group(session: AsyncSession, tenant_id: str) -> DayGroup:
    day_group: DayGroup | None = (
        await session.execute(
            select(DayGroup).where(DayGroup.tenant_id == tenant_id, DayGroup.name == "每天")
        )
    ).scalar_one_or_none()
    if not day_group:
        day_group = DayGroup(
            tenant_id=tenant_id,
            name="每天",
            description="Demo daily shift group",
        )
        session.add(day_group)
        await session.flush()
    existing_days = set(
        (
            await session.execute(
                select(DayGroupDay.day_number).where(
                    DayGroupDay.day_group_id == day_group.id
                )
            )
        )
        .scalars()
        .all()
    )
    for day_number in range(1, 8):
        if day_number not in existing_days:
            session.add(
                DayGroupDay(
                    tenant_id=tenant_id,
                    day_group_id=day_group.id,
                    day_number=day_number,
                )
            )
    await session.flush()
    return day_group


async def _ensure_shifts(
    session: AsyncSession,
    tenant_id: str,
    day_group: DayGroup,
) -> dict[str, ShiftTemplate]:
    definitions = {
        "E": ("早班", time(7, 0), time(15, 0), "#2a9d8f"),
        "L": ("晚班", time(15, 0), time(23, 0), "#e9c46a"),
        "N": ("夜班", time(23, 0), time(7, 0), "#4a5899"),
    }
    shifts: dict[str, ShiftTemplate] = {}
    for code, (name, start, end, color) in definitions.items():
        shift = (
            await session.execute(
                select(ShiftTemplate).where(
                    ShiftTemplate.tenant_id == tenant_id,
                    ShiftTemplate.code == code,
                )
            )
        ).scalar_one_or_none()
        if not shift:
            shift = ShiftTemplate(
                tenant_id=tenant_id,
                code=code,
                name=name,
                start_time=start,
                end_time=end,
                duration_hours=8.0,
                color=color,
                day_group_id=day_group.id,
            )
            session.add(shift)
            await session.flush()
        shifts[code] = shift
    return shifts


async def _ensure_nurses(
    session: AsyncSession,
    tenant_id: str,
    roles: dict[str, Role],
    skills: dict[str, Skill],
) -> list[Nurse]:
    nurses: list[Nurse] = []
    for index in range(8):
        employee_id = f"DEMO-{index:03d}"
        nurse = (
            await session.execute(
                select(Nurse).where(
                    Nurse.tenant_id == tenant_id,
                    Nurse.employee_id == employee_id,
                )
            )
        ).scalar_one_or_none()
        if not nurse:
            nurse = Nurse(
                tenant_id=tenant_id,
                employee_id=employee_id,
                first_name="Demo",
                last_name=f"Nurse {index}",
                department="Demo Ward",
                is_available=True,
                preferences={},
            )
            session.add(nurse)
            await session.flush()

        role_code = "rn" if index < 4 else "en"
        role_link = (
            await session.execute(
                nurse_roles.select().where(
                    nurse_roles.c.nurse_id == nurse.id,
                    nurse_roles.c.role_id == roles[role_code].id,
                )
            )
        ).first()
        if not role_link:
            await session.execute(
                nurse_roles.insert().values(nurse_id=nurse.id, role_id=roles[role_code].id)
            )
        nurse_skill_codes = ["bls"]
        if index in (0, 1, 4):
            nurse_skill_codes.append("icu")
        if index in (2, 6):
            nurse_skill_codes.append("triage")
        for skill_code in nurse_skill_codes:
            skill_link = (
                await session.execute(
                    nurse_skills.select().where(
                        nurse_skills.c.nurse_id == nurse.id,
                        nurse_skills.c.skill_id == skills[skill_code].id,
                    )
                )
            ).first()
            if not skill_link:
                await session.execute(
                    nurse_skills.insert().values(
                        nurse_id=nurse.id, skill_id=skills[skill_code].id
                    )
                )

        contract = (
            await session.execute(select(Contract).where(Contract.nurse_id == nurse.id))
        ).scalar_one_or_none()
        if not contract:
            session.add(
                Contract(
                    tenant_id=tenant_id,
                    nurse_id=nurse.id,
                    shifts_per_period=5,
                    max_shifts_per_period=7,
                    min_rest_hours=11,
                    max_consecutive_days=5,
                    enforce_balanced=True,
                    enforce_shifts_per_period=False,
                    enforce_one_shift_per_day=True,
                )
            )
        else:
            contract.shifts_per_period = 5
            contract.max_shifts_per_period = 7
            contract.min_rest_hours = 11
            contract.max_consecutive_days = 5
            contract.enforce_balanced = True
            contract.enforce_shifts_per_period = False
            contract.enforce_one_shift_per_day = True
        nurses.append(nurse)
    await session.flush()
    return nurses


async def _ensure_demo_user(
    session: AsyncSession,
    tenant_id: str,
    email: str,
    first_name: str,
    last_name: str,
    role: UserRole,
    hashed_password: str,
    nurse: Nurse | None = None,
) -> User:
    user: User | None = (
        await session.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    if user and user.tenant_id != tenant_id:
        raise RuntimeError(f"Demo email is already used by another tenant: {email}")
    if not user:
        user = User(
            tenant_id=tenant_id,
            email=email,
            first_name=first_name,
            last_name=last_name,
            role=role,
            is_active=True,
            is_superuser=False,
        )
        session.add(user)
    user.hashed_password = hashed_password
    user.first_name = first_name
    user.last_name = last_name
    user.role = role
    user.is_active = True
    await session.flush()
    if nurse:
        existing_link = (
            await session.execute(select(User).where(User.nurse_id == nurse.id))
        ).scalar_one_or_none()
        if existing_link and existing_link.id != user.id:
            raise RuntimeError(f"Nurse {nurse.employee_id} is already linked to another user")
        user.nurse_id = nurse.id
    return user


async def _ensure_users(
    session: AsyncSession,
    tenant_id: str,
    nurses: Sequence[Nurse],
    hashed_password: str,
) -> User:
    await _ensure_demo_user(
        session,
        tenant_id,
        "demo-admin@nurse-scheduler.dev",
        "Demo",
        "Admin",
        UserRole.TENANT_ADMIN,
        hashed_password,
    )
    scheduler = await _ensure_demo_user(
        session,
        tenant_id,
        "demo-scheduler@nurse-scheduler.dev",
        "Demo",
        "Scheduler",
        UserRole.SCHEDULER,
        hashed_password,
    )
    await _ensure_demo_user(
        session,
        tenant_id,
        "demo-viewer@nurse-scheduler.dev",
        "Demo",
        "Viewer",
        UserRole.VIEWER,
        hashed_password,
    )
    await _ensure_demo_user(
        session,
        tenant_id,
        "demo-nurse@nurse-scheduler.dev",
        "Demo",
        "Nurse",
        UserRole.NURSE,
        hashed_password,
        nurses[0],
    )
    return scheduler


async def _repair_legacy_demo_emails(
    session: AsyncSession,
    tenant_id: str,
) -> None:
    """Repair the unusable `.local` addresses emitted by the first seed."""
    for legacy_email, current_email in LEGACY_DEMO_EMAILS.items():
        current = (
            await session.execute(select(User).where(User.email == current_email))
        ).scalar_one_or_none()
        legacy = (
            await session.execute(select(User).where(User.email == legacy_email))
        ).scalar_one_or_none()
        if legacy and legacy.tenant_id == tenant_id and not current:
            legacy.email = current_email


async def _ensure_skill_mix_rules(
    session: AsyncSession,
    tenant_id: str,
    roles: dict[str, Role],
    shifts: dict[str, ShiftTemplate],
) -> list[SkillMixRule]:
    rules: list[SkillMixRule] = []
    for code, shift in shifts.items():
        name = f"Demo {code} staffing"
        rule = (
            await session.execute(
                select(SkillMixRule).where(
                    SkillMixRule.tenant_id == tenant_id,
                    SkillMixRule.name == name,
                )
            )
        ).scalar_one_or_none()
        if not rule:
            rule = SkillMixRule(
                tenant_id=tenant_id,
                name=name,
                shift_template_id=shift.id,
                priority=0,
                is_active=True,
            )
            session.add(rule)
            await session.flush()
            session.add_all([
                SkillMixRequirement(
                    tenant_id=tenant_id,
                    skill_mix_rule_id=rule.id,
                    role_id=roles["rn"].id,
                    count=1,
                ),
                SkillMixRequirement(
                    tenant_id=tenant_id,
                    skill_mix_rule_id=rule.id,
                    role_id=roles["en"].id,
                    count=1,
                ),
            ])
        rules.append(rule)
    await session.flush()
    return rules


async def _ensure_sequence_rules(
    session: AsyncSession,
    tenant_id: str,
    shifts: dict[str, ShiftTemplate],
) -> None:
    name = "Demo night-to-early restriction"
    rule = (
        await session.execute(
            select(ShiftSequenceRule).where(
                ShiftSequenceRule.tenant_id == tenant_id,
                ShiftSequenceRule.name == name,
            )
        )
    ).scalar_one_or_none()
    if rule:
        return
    rule = ShiftSequenceRule(
        tenant_id=tenant_id,
        name=name,
        description="Forbid night shift followed by early shift",
        is_active=True,
    )
    session.add(rule)
    await session.flush()
    session.add_all([
        ShiftSequenceStep(
            tenant_id=tenant_id,
            rule_id=rule.id,
            position=0,
            shift_template_id=shifts["N"].id,
        ),
        ShiftSequenceStep(
            tenant_id=tenant_id,
            rule_id=rule.id,
            position=1,
            shift_template_id=shifts["E"].id,
        ),
    ])


async def _ensure_preference(
    session: AsyncSession,
    tenant_id: str,
    nurse: Nurse,
    shift: ShiftTemplate,
) -> None:
    period_start = _next_monday()
    existing = (
        await session.execute(
            select(NursePreference).where(
                NursePreference.tenant_id == tenant_id,
                NursePreference.nurse_id == nurse.id,
                NursePreference.date == period_start,
                NursePreference.shift_template_id == shift.id,
            )
        )
    ).scalar_one_or_none()
    if not existing:
        session.add(
            NursePreference(
                tenant_id=tenant_id,
                nurse_id=nurse.id,
                date=period_start,
                shift_template_id=shift.id,
                request_type=RequestType.LIKE,
                priority=3,
            )
        )


def _next_monday() -> date:
    today = date.today()
    return today + timedelta(days=(7 - today.weekday()) % 7 or 7)


async def _queue_initial_schedule(
    session: AsyncSession,
    tenant_id: str,
    scheduler: User,
    rules: Sequence[SkillMixRule],
) -> None:
    requests = (
        await session.execute(
            select(ScheduleRequest).where(ScheduleRequest.tenant_id == tenant_id)
        )
    ).scalars().all()
    if any(request.status != ScheduleStatus.FAILED for request in requests):
        return

    request_date = date.today()
    task_id = str(uuid.uuid4())
    last_sequence = (
        await session.execute(
            select(func.coalesce(func.max(ScheduleRequest.daily_sequence), 0)).where(
                ScheduleRequest.request_date == request_date
            )
        )
    ).scalar_one()
    request = ScheduleRequest(
        tenant_id=tenant_id,
        period_start=_next_monday(),
        period_days=14,
        request_date=request_date,
        daily_sequence=last_sequence + 1,
        status=ScheduleStatus.PENDING,
        task_id=task_id,
        solver_config={"timeout_seconds": 120, "num_workers": 8},
        nurse_ids=None,
        skill_mix_rule_ids=[rule.id for rule in rules],
        shift_sequence_rule_ids=None,
        requested_by=scheduler.id,
    )
    session.add(request)
    await session.commit()
    celery_app.send_task(
        "schedule.generate", args=[request.id], queue="scheduling", task_id=task_id
    )
    print(f"[demo-seed] queued schedule request {request.display_id}")


async def ensure_demo_dataset(session: AsyncSession) -> tuple[Tenant, User]:
    """Create or repair the demo dataset on a super-admin session."""
    tenant = await _ensure_tenant(session)
    tenant_id = tenant.id
    await _enable_demo_context(session, tenant_id)
    if (
        settings.DEMO_TENANT_PASSWORD == DEVELOPMENT_DEFAULT_PASSWORD
        and settings.APP_ENV == "development"
    ):
        raise RuntimeError(
            "Refusing to seed development demo tenant with the default DEMO_TENANT_PASSWORD"
        )
    await _repair_legacy_demo_emails(session, tenant_id)
    await _ensure_subscription(session, tenant_id)
    roles = await _ensure_roles(session, tenant_id)
    skills = await _ensure_skills(session, tenant_id)
    day_group = await _ensure_day_group(session, tenant_id)
    shifts = await _ensure_shifts(session, tenant_id, day_group)
    nurses = await _ensure_nurses(session, tenant_id, roles, skills)
    scheduler = await _ensure_users(
        session,
        tenant_id,
        nurses,
        hash_password(settings.DEMO_TENANT_PASSWORD),
    )
    rules = await _ensure_skill_mix_rules(session, tenant_id, roles, shifts)
    await _ensure_sequence_rules(session, tenant_id, shifts)
    await _ensure_preference(session, tenant_id, nurses[0], shifts["E"])
    await session.commit()
    if settings.DEMO_TENANT_GENERATE_SCHEDULE:
        await _queue_initial_schedule(session, tenant_id, scheduler, rules)
    users = (
        await session.execute(select(User).where(User.tenant_id == tenant_id))
    ).scalars().all()
    admin: User | None = next(
        (user for user in users if user.role == UserRole.TENANT_ADMIN),
        None,
    )
    if admin is None:
        raise RuntimeError("Demo tenant is missing an administrator account")
    return tenant, admin


async def main() -> None:
    if not settings.ENABLE_DEMO_TENANT:
        print("[demo-seed] disabled (ENABLE_DEMO_TENANT=false)")
        return

    async with async_session_factory() as session:
        await session.execute(text("SET app.is_super = '1'"))
        tenant, _admin = await ensure_demo_dataset(session)
        tenant_id = tenant.id
        print(f"[demo-seed] tenant {DEMO_SLUG} is ready (id={tenant_id})")


if __name__ == "__main__":
    asyncio.run(main())
