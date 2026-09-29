"""Rules CRUD — SkillMixRule + ShiftSequenceRule (tenant-scoped).

Both rule types own child collections (requirements / steps+role_restrictions)
created inline with the rule, replaced wholesale on update.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AnyUserDep, SchedulerDep, set_tenant_context
from app.models import (
    Role,
    ShiftSequenceRoleRestriction,
    ShiftSequenceRule,
    ShiftSequenceStep,
    ShiftTemplate,
    Skill,
    SkillMixRequirement,
    SkillMixRule,
    Tenant,
    _tenant_name,
)
from app.schemas import (
    Paginated,
    ShiftSequenceRuleCreate,
    ShiftSequenceRuleRead,
    ShiftSequenceStepRead,
    SkillMixRequirementRead,
    SkillMixRuleCreate,
    SkillMixRuleRead,
)

skill_mix_router = APIRouter(prefix="/skill-mix-rules", tags=["rules"])
shift_sequence_router = APIRouter(prefix="/shift-sequence-rules", tags=["rules"])


async def _role_lookup(
    session: AsyncSession,
    role_ids: set[str],
) -> dict[str, tuple[str, str]]:
    """Batch-resolve role_id -> (name, code)."""
    if not role_ids:
        return {}
    rows = (
        await session.execute(
            select(Role.id, Role.name, Role.code).where(Role.id.in_(role_ids))
        )
    ).all()
    return {r.id: (r.name, r.code) for r in rows}


async def _skill_lookup(
    session: AsyncSession,
    skill_ids: set[str],
) -> dict[str, tuple[str, str]]:
    """Batch-resolve skill_id -> (name, code)."""
    if not skill_ids:
        return {}
    rows = (
        await session.execute(
            select(Skill.id, Skill.name, Skill.code).where(Skill.id.in_(skill_ids))
        )
    ).all()
    return {r.id: (r.name, r.code) for r in rows}


async def _shift_lookup(
    session: AsyncSession,
    shift_ids: set[str],
) -> dict[str, tuple[str, str]]:
    """Batch-resolve shift_template_id -> (name, code)."""
    if not shift_ids:
        return {}
    rows = (
        await session.execute(
            select(ShiftTemplate.id, ShiftTemplate.name, ShiftTemplate.code).where(
                ShiftTemplate.id.in_(shift_ids)
            )
        )
    ).all()
    return {r.id: (r.name, r.code) for r in rows}


async def _enrich_skill_mix(
    session: AsyncSession,
    rows: Sequence[SkillMixRule],
) -> list[SkillMixRuleRead]:
    """Populate tenant/shift/role/skill display names on skill-mix rule rows."""
    shift_ids = {r.shift_template_id for r in rows if r.shift_template_id}
    role_ids = {req.role_id for r in rows for req in r.requirements if req.role_id}
    skill_ids = {req.skill_id for r in rows for req in r.requirements if req.skill_id}
    shifts = await _shift_lookup(session, shift_ids)
    roles = await _role_lookup(session, role_ids)
    skills = await _skill_lookup(session, skill_ids)
    items: list[SkillMixRuleRead] = []
    for row in rows:
        item = SkillMixRuleRead.model_validate(row, from_attributes=True)
        item.tenant_name = await _tenant_name(session, row.tenant_id)
        s_name, s_code = shifts.get(row.shift_template_id, (None, None))
        item.shift_template_name = s_name
        item.shift_template_code = s_code
        item.requirements = []
        for req in row.requirements:
            requirement = SkillMixRequirementRead.model_validate(
                req,
                from_attributes=True,
            )
            if req.role_id is None:
                r_name = r_code = None
            else:
                r_name, r_code = roles.get(req.role_id, (None, None))
            requirement.role_name = r_name
            requirement.role_code = r_code
            if req.skill_id is None:
                sk_name = sk_code = None
            else:
                sk_name, sk_code = skills.get(req.skill_id, (None, None))
            requirement.skill_name = sk_name
            requirement.skill_code = sk_code
            item.requirements.append(requirement)
        items.append(item)
    return items


async def _enrich_shift_sequence(
    session: AsyncSession,
    rows: Sequence[ShiftSequenceRule],
) -> list[ShiftSequenceRuleRead]:
    """Populate tenant/shift/role display names on shift-sequence rule rows."""
    shift_ids = {s.shift_template_id for r in rows for s in r.steps if s.shift_template_id}
    role_ids = {rr.role_id for r in rows for rr in r.role_restrictions if rr.role_id}
    shifts = await _shift_lookup(session, shift_ids)
    roles = await _role_lookup(session, role_ids)
    items: list[ShiftSequenceRuleRead] = []
    for row in rows:
        item = ShiftSequenceRuleRead.model_validate(row, from_attributes=True)
        item.tenant_name = await _tenant_name(session, row.tenant_id)
        item.steps = []
        for step in row.steps:
            step_read = ShiftSequenceStepRead.model_validate(
                step,
                from_attributes=True,
            )
            if step.shift_template_id is None:
                s_name = s_code = None
            else:
                s_name, s_code = shifts.get(step.shift_template_id, (None, None))
            step_read.shift_template_name = s_name
            step_read.shift_template_code = s_code
            item.steps.append(step_read)
        rids = [rr.role_id for rr in row.role_restrictions]
        item.role_ids = rids
        item.role_names = [roles[rid][0] for rid in rids if rid in roles]
        item.role_codes = [roles[rid][1] for rid in rids if rid in roles]
        items.append(item)
    return items


# ──────────────────────────────────────────────────────────────
# Skill Mix Rules
# ──────────────────────────────────────────────────────────────
@skill_mix_router.post("", response_model=SkillMixRuleRead, status_code=status.HTTP_201_CREATED)
async def create_skill_mix_rule(
    body: SkillMixRuleCreate,
    ctx: SchedulerDep,
    requested_tenant_id: str | None = Query(None, alias="tenant_id"),
) -> SkillMixRuleRead:
    user, ctx_tenant_id, session = ctx
    tenant_id: str
    if user.role.value == "super_admin":
        if not requested_tenant_id:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="Super admin must specify tenant_id",
            )
        tenant = await session.get(Tenant, requested_tenant_id)
        if not tenant:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Tenant not found")
        tenant_id = requested_tenant_id
    else:
        if requested_tenant_id and requested_tenant_id != ctx_tenant_id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Cannot access other tenants")
        if ctx_tenant_id is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "tenant context required")
        tenant_id = ctx_tenant_id
    # The shift template must already belong to the explicit target tenant.
    st = await session.get(ShiftTemplate, body.shift_template_id)
    if not st:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "shift_template_id not found")
    if st.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "shift_template_id not found in this tenant")
    await set_tenant_context(session, tenant_id)
    # validate role_ids exist (RLS-scoped)
    selection_keys = [(r.role_id, r.skill_id) for r in body.requirements]
    if len(selection_keys) != len(set(selection_keys)):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Duplicate role/skill requirement in skill mix rule",
        )
    role_ids = {r.role_id for r in body.requirements if r.role_id}
    if role_ids:
        found = (
            await session.execute(
                select(Role.id).where(
                    Role.tenant_id == tenant_id,
                    Role.id.in_(role_ids),
                )
            )
        ).scalars().all()
        if set(found) != role_ids:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "One or more role_ids not found")
    skill_ids = {r.skill_id for r in body.requirements if r.skill_id}
    if skill_ids:
        found = (
            await session.execute(
                select(Skill.id).where(
                    Skill.tenant_id == tenant_id,
                    Skill.id.in_(skill_ids),
                )
            )
        ).scalars().all()
        if set(found) != skill_ids:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "One or more skill_ids not found")

    rule = SkillMixRule(
        id=str(uuid.uuid4()), tenant_id=tenant_id,
        name=body.name, shift_template_id=body.shift_template_id,
        priority=body.priority, is_active=True,
    )
    rule.requirements = [
        SkillMixRequirement(
            id=str(uuid.uuid4()), tenant_id=tenant_id,
            skill_mix_rule_id=rule.id, role_id=req.role_id,
            skill_id=req.skill_id, count=req.count,
        )
        for req in body.requirements
    ]
    session.add(rule)
    await session.flush()
    await session.refresh(rule)  # in-txn refresh (RLS-safe: same connection)
    await session.commit()
    return (await _enrich_skill_mix(session, [rule]))[0]


@skill_mix_router.get("", response_model=Paginated[SkillMixRuleRead])
async def list_skill_mix_rules(
    ctx: AnyUserDep,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    requested_tenant_id: str | None = Query(None, alias="tenant_id"),
) -> Paginated[SkillMixRuleRead]:
    user, ctx_tenant_id, session = ctx
    tenant_id = ctx_tenant_id
    if user.role.value == "super_admin" and requested_tenant_id:
        tenant = await session.get(Tenant, requested_tenant_id)
        if not tenant:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Tenant not found")
        await set_tenant_context(session, requested_tenant_id)
        tenant_id = requested_tenant_id
    elif requested_tenant_id and requested_tenant_id != ctx_tenant_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Cannot access other tenants")

    count_query = select(func.count(SkillMixRule.id))
    rows_query = select(SkillMixRule)
    if tenant_id:
        count_query = count_query.where(SkillMixRule.tenant_id == tenant_id)
        rows_query = rows_query.where(SkillMixRule.tenant_id == tenant_id)
    total = (await session.execute(count_query)).scalar_one()
    rows = (
        await session.execute(
            rows_query.offset((page - 1) * page_size).limit(page_size)
        )
    ).scalars().all()
    items = await _enrich_skill_mix(session, rows)
    return Paginated(items=items, total=total, page=page, page_size=page_size)


@skill_mix_router.get("/{rule_id}", response_model=SkillMixRuleRead)
async def get_skill_mix_rule(rule_id: str, ctx: AnyUserDep) -> SkillMixRuleRead:
    _user, _tenant_id, session = ctx
    rule = await session.get(SkillMixRule, rule_id)
    if not rule:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Skill mix rule not found")
    return (await _enrich_skill_mix(session, [rule]))[0]


@skill_mix_router.patch("/{rule_id}", response_model=SkillMixRuleRead)
async def update_skill_mix_rule(
    rule_id: str,
    body: SkillMixRuleCreate,
    ctx: SchedulerDep,
) -> SkillMixRuleRead:
    _user, _ctx_tenant_id, session = ctx
    rule = await session.get(SkillMixRule, rule_id)
    if not rule:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Skill mix rule not found")
    tenant_id = rule.tenant_id
    st = await session.get(ShiftTemplate, body.shift_template_id)
    if not st or st.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "shift_template_id not found in this tenant")
    await set_tenant_context(session, tenant_id)
    rule.name = body.name
    rule.shift_template_id = body.shift_template_id
    rule.priority = body.priority
    selection_keys = [(r.role_id, r.skill_id) for r in body.requirements]
    if len(selection_keys) != len(set(selection_keys)):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Duplicate role/skill requirement in skill mix rule",
        )
    role_ids = {r.role_id for r in body.requirements if r.role_id}
    if role_ids:
        found = (
            await session.execute(
                select(Role.id).where(
                    Role.tenant_id == tenant_id,
                    Role.id.in_(role_ids),
                )
            )
        ).scalars().all()
        if set(found) != role_ids:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "One or more role_ids not found")
    # Validate skill_ids before replacing requirements
    skill_ids = {r.skill_id for r in body.requirements if r.skill_id}
    if skill_ids:
        found = (
            await session.execute(
                select(Skill.id).where(
                    Skill.tenant_id == tenant_id,
                    Skill.id.in_(skill_ids),
                )
            )
        ).scalars().all()
        if set(found) != skill_ids:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "One or more skill_ids not found")
    # Replace requirements wholesale (keep the rule's own tenant_id)
    for req in list(rule.requirements):
        await session.delete(req)
    await session.flush()
    rule.requirements = [
        SkillMixRequirement(
            id=str(uuid.uuid4()), tenant_id=rule.tenant_id,
            skill_mix_rule_id=rule.id, role_id=req.role_id,
            skill_id=req.skill_id, count=req.count,
        )
        for req in body.requirements
    ]
    await session.flush()
    await session.refresh(rule)  # in-txn refresh (RLS-safe: same connection)
    await session.commit()
    return (await _enrich_skill_mix(session, [rule]))[0]


@skill_mix_router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_skill_mix_rule(rule_id: str, ctx: SchedulerDep) -> None:
    _user, _tenant_id, session = ctx
    rule = await session.get(SkillMixRule, rule_id)
    if not rule:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Skill mix rule not found")
    await session.delete(rule)  # cascade requirements
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "无法删除：该技能混合规则被引用",
        ) from None


# ──────────────────────────────────────────────────────────────
# Shift Sequence Rules
# ──────────────────────────────────────────────────────────────
@shift_sequence_router.post("", response_model=ShiftSequenceRuleRead, status_code=status.HTTP_201_CREATED)
async def create_shift_sequence_rule(
    body: ShiftSequenceRuleCreate,
    ctx: SchedulerDep,
    requested_tenant_id: str | None = Query(None, alias="tenant_id"),
) -> ShiftSequenceRuleRead:
    user, ctx_tenant_id, session = ctx
    tenant_id: str
    if user.role.value == "super_admin":
        if not requested_tenant_id:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="Super admin must specify tenant_id",
            )
        tenant = await session.get(Tenant, requested_tenant_id)
        if not tenant:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Tenant not found")
        tenant_id = requested_tenant_id
    else:
        if requested_tenant_id and requested_tenant_id != ctx_tenant_id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Cannot access other tenants")
        if ctx_tenant_id is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "tenant context required")
        tenant_id = ctx_tenant_id
    # Validate role_ids exist (RLS-scoped)
    if body.role_ids:
        found = (
            await session.execute(
                select(Role.id).where(
                    Role.tenant_id == tenant_id,
                    Role.id.in_(set(body.role_ids)),
                )
            )
        ).scalars().all()
        if set(found) != set(body.role_ids):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "One or more role_ids not found")
    st_ids = [s.shift_template_id for s in body.steps if s.shift_template_id]
    if st_ids:
        shifts = (
            await session.execute(
                select(ShiftTemplate).where(ShiftTemplate.id.in_(st_ids))
            )
        ).scalars().all()
        if len(shifts) != len(set(st_ids)) or any(st.tenant_id != tenant_id for st in shifts):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "shift_template_id not found in this tenant")
    await set_tenant_context(session, tenant_id)

    rule = ShiftSequenceRule(
        id=str(uuid.uuid4()), tenant_id=tenant_id,
        name=body.name, description=body.description, is_active=True,
    )
    rule.steps = [
        ShiftSequenceStep(
            id=str(uuid.uuid4()), tenant_id=tenant_id, rule_id=rule.id,
            position=s.position, shift_template_id=s.shift_template_id,
        )
        for s in body.steps
    ]
    rule.role_restrictions = [
        ShiftSequenceRoleRestriction(
            id=str(uuid.uuid4()), tenant_id=tenant_id, rule_id=rule.id, role_id=rid,
        )
        for rid in body.role_ids
    ]
    session.add(rule)
    await session.flush()
    await session.refresh(rule)  # in-txn refresh (RLS-safe: same connection)
    await session.commit()
    return (await _enrich_shift_sequence(session, [rule]))[0]


@shift_sequence_router.get("", response_model=Paginated[ShiftSequenceRuleRead])
async def list_shift_sequence_rules(
    ctx: AnyUserDep,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    requested_tenant_id: str | None = Query(None, alias="tenant_id"),
) -> Paginated[ShiftSequenceRuleRead]:
    user, ctx_tenant_id, session = ctx
    tenant_id = ctx_tenant_id
    if user.role.value == "super_admin" and requested_tenant_id:
        tenant = await session.get(Tenant, requested_tenant_id)
        if not tenant:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Tenant not found")
        await set_tenant_context(session, requested_tenant_id)
        tenant_id = requested_tenant_id
    elif requested_tenant_id and requested_tenant_id != ctx_tenant_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Cannot access other tenants")

    count_query = select(func.count(ShiftSequenceRule.id))
    rows_query = select(ShiftSequenceRule)
    if tenant_id:
        count_query = count_query.where(ShiftSequenceRule.tenant_id == tenant_id)
        rows_query = rows_query.where(ShiftSequenceRule.tenant_id == tenant_id)
    total = (await session.execute(count_query)).scalar_one()
    rows = (
        await session.execute(
            rows_query.offset((page - 1) * page_size).limit(page_size)
        )
    ).scalars().all()
    items = await _enrich_shift_sequence(session, rows)
    return Paginated(items=items, total=total, page=page, page_size=page_size)


@shift_sequence_router.get("/{rule_id}", response_model=ShiftSequenceRuleRead)
async def get_shift_sequence_rule(
    rule_id: str,
    ctx: AnyUserDep,
) -> ShiftSequenceRuleRead:
    _user, _tenant_id, session = ctx
    rule = await session.get(ShiftSequenceRule, rule_id)
    if not rule:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Shift sequence rule not found")
    return (await _enrich_shift_sequence(session, [rule]))[0]


@shift_sequence_router.patch("/{rule_id}", response_model=ShiftSequenceRuleRead)
async def update_shift_sequence_rule(
    rule_id: str,
    body: ShiftSequenceRuleCreate,
    ctx: SchedulerDep,
) -> ShiftSequenceRuleRead:
    _user, _ctx_tenant_id, session = ctx
    rule = await session.get(ShiftSequenceRule, rule_id)
    if not rule:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Shift sequence rule not found")
    tenant_id = rule.tenant_id
    await set_tenant_context(session, tenant_id)
    if body.role_ids:
        found = (
            await session.execute(
                select(Role.id).where(
                    Role.tenant_id == tenant_id,
                    Role.id.in_(set(body.role_ids)),
                )
            )
        ).scalars().all()
        if set(found) != set(body.role_ids):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "One or more role_ids not found")
    rule.name = body.name
    rule.description = body.description
    # Replace steps + role_restrictions wholesale (keep the rule's own tenant_id)
    for step in list(rule.steps):
        await session.delete(step)
    for rr in list(rule.role_restrictions):
        await session.delete(rr)
    await session.flush()
    rule.steps = [
        ShiftSequenceStep(
            id=str(uuid.uuid4()), tenant_id=rule.tenant_id, rule_id=rule.id,
            position=s.position, shift_template_id=s.shift_template_id,
        )
        for s in body.steps
    ]
    rule.role_restrictions = [
        ShiftSequenceRoleRestriction(
            id=str(uuid.uuid4()), tenant_id=rule.tenant_id, rule_id=rule.id, role_id=rid,
        )
        for rid in body.role_ids
    ]
    await session.flush()
    await session.refresh(rule)  # in-txn refresh (RLS-safe: same connection)
    await session.commit()
    return (await _enrich_shift_sequence(session, [rule]))[0]


@shift_sequence_router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_shift_sequence_rule(rule_id: str, ctx: SchedulerDep) -> None:
    _user, _tenant_id, session = ctx
    rule = await session.get(ShiftSequenceRule, rule_id)
    if not rule:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Shift sequence rule not found")
    await session.delete(rule)  # cascade steps + role_restrictions
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "无法删除：该班次序列规则被引用",
        ) from None
