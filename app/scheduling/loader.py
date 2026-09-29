"""Async loader — fetches tenant domain data from DB into in-memory DomainData.

One query per entity type (nurses, shifts, rules, etc.). The solver works
purely in-memory after this. All queries are RLS-scoped (tenant_id filter
applied by set_tenant_context before calling load_domain_data).
"""

from __future__ import annotations

import datetime
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import (
    Assignment,
    DayGroup,
    Leave,
    Nurse,
    NursePreference,
    Role,
    Schedule,
    ShiftSequenceRule,
    ShiftTemplate,
    Skill,
    SkillMixRule,
)
from app.scheduling.domain import (
    AssignmentResult,
    ContractData,
    DomainData,
    NurseData,
    NursePreferenceData,
    PreviousAssignmentData,
    RoleData,
    ShiftSequenceRuleData,
    ShiftSequenceStepData,
    ShiftTemplateData,
    SkillMixRequirementData,
    SkillMixRuleData,
)


async def load_domain_data(
    session: AsyncSession,
    tenant_id: str,
    period_start: datetime.date,
    period_days: int,
    nurse_ids: list[str] | None = None,
    skill_mix_rule_ids: list[str] | None = None,
    shift_sequence_rule_ids: list[str] | None = None,
    pinned_schedule_id: str | None = None,
    pinned_assignments: list[AssignmentResult] | None = None,
) -> DomainData:
    """Load all tenant data needed for the scheduling period.

    Args:
        session: async session with RLS already set to this tenant
        tenant_id: tenant id (for record-keeping)
        period_start: first date of the roster period
        period_days: number of days to schedule
        nurse_ids: optional participant filter; None/empty = all available nurses
        skill_mix_rule_ids: optional rule filter; None = all active rules and
            an empty list explicitly applies no rules
        shift_sequence_rule_ids: optional rule filter; None = all active rules
            and an empty list explicitly applies no rules
    """
    period_end = period_start + datetime.timedelta(days=period_days - 1)
    # Extended range includes previous period for shift-sequence continuity
    ext_start = period_start - datetime.timedelta(days=period_days)

    dates = [period_start + datetime.timedelta(days=i) for i in range(period_days)]
    extended_dates = [ext_start + datetime.timedelta(days=i) for i in range(period_days * 2)]

    # ──────────────────────────────────────────────────────────
    # Roles
    # ──────────────────────────────────────────────────────────
    roles_result = await session.execute(select(Role).where(Role.tenant_id == tenant_id))
    roles = {
        r.id: RoleData(id=r.id, code=r.code, name=r.name)
        for r in roles_result.scalars()
    }

    # ──────────────────────────────────────────────────────────
    # Shift templates + day groups
    # ──────────────────────────────────────────────────────────
    shifts_result = await session.execute(
        select(ShiftTemplate)
        .options(selectinload(ShiftTemplate.day_group).selectinload(DayGroup.days))
        .where(ShiftTemplate.tenant_id == tenant_id)
    )
    shift_templates: list[ShiftTemplateData] = []
    for st in shifts_result.scalars():
        day_numbers = frozenset(d.day_number for d in st.day_group.days)
        shift_templates.append(
            ShiftTemplateData(
                id=st.id,
                code=st.code,
                name=st.name,
                start_time=st.start_time,
                end_time=st.end_time,
                duration_hours=st.duration_hours,
                day_numbers=day_numbers,
            )
        )

    # ──────────────────────────────────────────────────────────
    # Nurses + roles + contract + leave
    # ──────────────────────────────────────────────────────────
    nurse_filters = [Nurse.tenant_id == tenant_id, Nurse.is_available.is_(True)]
    if nurse_ids:
        nurse_filters.append(Nurse.id.in_(nurse_ids))
    nurses_result = await session.execute(
        select(Nurse)
        .options(
            selectinload(Nurse.roles),
            selectinload(Nurse.skills),
            selectinload(Nurse.contract),
        )
        .where(*nurse_filters)
    )
    # Pre-load leave dates for the period
    leave_result = await session.execute(
        select(Leave).where(
            Leave.tenant_id == tenant_id,
            Leave.date.between(period_start, period_end),
        )
    )
    leaves_by_nurse: dict[str, set[datetime.date]] = defaultdict(set)
    for leave in leave_result.scalars():
        leaves_by_nurse[leave.nurse_id].add(leave.date)

    nurses: list[NurseData] = []
    for n in nurses_result.scalars():
        contract_data: ContractData | None = None
        if n.contract:
            c = n.contract
            contract_data = ContractData(
                nurse_id=n.id,
                shifts_per_period=c.shifts_per_period,
                max_shifts_per_period=c.max_shifts_per_period,
                min_rest_hours=c.min_rest_hours,
                max_consecutive_days=c.max_consecutive_days,
                enforce_balanced=c.enforce_balanced,
                enforce_shifts_per_period=c.enforce_shifts_per_period,
                enforce_one_shift_per_day=c.enforce_one_shift_per_day,
            )
        nurses.append(
            NurseData(
                id=n.id,
                employee_id=n.employee_id,
                first_name=n.first_name,
                last_name=n.last_name,
                is_available=n.is_available,
                role_ids=[r.id for r in n.roles],
                skill_ids=[s.id for s in n.skills],
                contract=contract_data,
                leave_dates=frozenset(leaves_by_nurse.get(n.id, set())),
            )
        )

    # ──────────────────────────────────────────────────────────
    # Skill mix rules
    # ──────────────────────────────────────────────────────────
    sm_filters = [SkillMixRule.tenant_id == tenant_id, SkillMixRule.is_active.is_(True)]
    if skill_mix_rule_ids is not None:
        sm_filters.append(SkillMixRule.id.in_(skill_mix_rule_ids))
    smr_result = await session.execute(
        select(SkillMixRule)
        .options(selectinload(SkillMixRule.requirements))
        .where(*sm_filters)
    )
    skill_mix_rules: list[SkillMixRuleData] = []
    for rule in smr_result.scalars():
        reqs = tuple(
            SkillMixRequirementData(
                role_id=req.role_id, count=req.count, skill_id=req.skill_id
            )
            for req in rule.requirements
        )
        skill_mix_rules.append(
            SkillMixRuleData(
                id=rule.id,
                name=rule.name,
                shift_template_id=rule.shift_template_id,
                priority=rule.priority,
                requirements=reqs,
            )
        )

    # ──────────────────────────────────────────────────────────
    # Shift sequence rules
    # ──────────────────────────────────────────────────────────
    ss_filters = [ShiftSequenceRule.tenant_id == tenant_id, ShiftSequenceRule.is_active.is_(True)]
    if shift_sequence_rule_ids is not None:
        ss_filters.append(ShiftSequenceRule.id.in_(shift_sequence_rule_ids))
    ssr_result = await session.execute(
        select(ShiftSequenceRule)
        .options(
            selectinload(ShiftSequenceRule.steps),
            selectinload(ShiftSequenceRule.role_restrictions),
        )
        .where(*ss_filters)
    )
    shift_sequence_rules: list[ShiftSequenceRuleData] = []
    for ss_rule in ssr_result.scalars():
        steps = tuple(
            ShiftSequenceStepData(position=s.position, shift_template_id=s.shift_template_id)
            for s in sorted(ss_rule.steps, key=lambda x: x.position)
        )
        role_ids = frozenset(rr.role_id for rr in ss_rule.role_restrictions)
        shift_sequence_rules.append(
            ShiftSequenceRuleData(
                id=ss_rule.id,
                name=ss_rule.name,
                steps=steps,
                applies_to_role_ids=role_ids,
            )
        )

    # ──────────────────────────────────────────────────────────
    # Nurse preferences (within period)
    # ──────────────────────────────────────────────────────────
    pref_result = await session.execute(
        select(NursePreference).where(
            NursePreference.tenant_id == tenant_id,
            NursePreference.date.between(period_start, period_end),
        )
    )
    preferences: list[NursePreferenceData] = []
    for p in pref_result.scalars():
        preferences.append(
            NursePreferenceData(
                nurse_id=p.nurse_id,
                date=p.date,
                shift_template_id=p.shift_template_id,
                request_type=p.request_type.value,
                priority=p.priority,
            )
        )

    # ──────────────────────────────────────────────────────────
    # Previous period assignments (for shift-sequence continuity)
    # ──────────────────────────────────────────────────────────
    prev_start = period_start - datetime.timedelta(days=period_days)
    prev_end = period_start - datetime.timedelta(days=1)
    previous_schedule = (
        await session.execute(
            select(Schedule)
            .where(
                Schedule.tenant_id == tenant_id,
                Schedule.deleted_at.is_(None),
                Schedule.period_start <= prev_end,
                Schedule.period_start + (Schedule.period_days - 1) >= prev_start,
            )
            .order_by(
                (Schedule.period_start + Schedule.period_days).desc(),
                Schedule.created_at.desc(),
                Schedule.id.desc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    previous_assignments: list[PreviousAssignmentData] = []
    if previous_schedule:
        prev_result = await session.execute(
            select(Assignment).where(
                Assignment.tenant_id == tenant_id,
                Assignment.schedule_id == previous_schedule.id,
                Assignment.date.between(prev_start, prev_end),
            )
        )
        for assignment in prev_result.scalars():
            previous_assignments.append(
                PreviousAssignmentData(
                    nurse_id=assignment.nurse_id,
                    role_id=assignment.role_id or "",
                    date=assignment.date,
                    shift_template_id=assignment.shift_template_id,
                )
            )

    # ──────────────────────────────────────────────────────────
    # B3-11: Pinned assignments from an existing schedule
    # ──────────────────────────────────────────────────────────
    resource_pinned_assignments: list[AssignmentResult] = []
    if pinned_assignments is not None:
        resource_pinned_assignments = list(pinned_assignments)
    elif pinned_schedule_id:
        pinned_result = await session.execute(
            select(Assignment).where(
                Assignment.tenant_id == tenant_id,
                Assignment.schedule_id == pinned_schedule_id,
                Assignment.date.between(period_start, period_end),
            )
        )
        for a in pinned_result.scalars():
            resource_pinned_assignments.append(
                AssignmentResult(
                    nurse_id=a.nurse_id,
                    role_id=a.role_id or "",
                    date=a.date,
                    shift_template_id=a.shift_template_id,
                )
            )

    return DomainData(
        tenant_id=tenant_id,
        period_start=period_start,
        period_days=period_days,
        nurses=nurses,
        roles=roles,
        skills={s.id: f"{s.name}（{s.code}）" for s in (
            await session.execute(select(Skill))
        ).scalars().all()},
        shift_templates=shift_templates,
        skill_mix_rules=skill_mix_rules,
        shift_sequence_rules=shift_sequence_rules,
        preferences=preferences,
        previous_assignments=previous_assignments,
        pinned_assignments=resource_pinned_assignments,
        dates=dates,
        extended_dates=extended_dates,
    )
