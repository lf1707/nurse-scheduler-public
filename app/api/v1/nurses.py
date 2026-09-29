"""Nurses CRUD — nurses, roles, skills, contracts, leaves (tenant-scoped).

Tenant context comes from the JWT via the dependency chain (TenantAdminDep /
SchedulerDep / AnyUserDep all set RLS to the caller's tenant). No /tenants/{id}
path param — the caller can only ever see their own tenant's data.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AnyUserDep, SchedulerDep, TenantAdminDep, set_tenant_context
from app.models import Contract, Leave, Nurse, Role, Skill, Tenant, nurse_roles, nurse_skills
from app.models.user import User
from app.schemas import (
    ContractCreate,
    ContractRead,
    LeaveCreate,
    LeaveRead,
    NurseCreate,
    NurseRead,
    Paginated,
    RoleCreate,
    RoleRead,
    RoleUpdate,
    SkillCreate,
    SkillRead,
    SkillUpdate,
)

router = APIRouter(prefix="/nurses", tags=["nurses"])


async def _tenant_name(session: AsyncSession, tenant_id: str | None) -> str | None:
    """Look up a tenant's display name (fast, small per-list cost)."""
    if not tenant_id:
        return None
    result = await session.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant: Tenant | None = result.scalar_one_or_none()
    return tenant.name if tenant else None


async def _nurse_by_employee_id(
    session: AsyncSession,
    employee_id: str,
) -> Nurse | None:
    """Fetch a nurse by employee_id within the session's RLS scope."""
    stmt = select(Nurse).where(Nurse.employee_id == employee_id)
    nurse: Nurse | None = (await session.execute(stmt)).scalar_one_or_none()
    return nurse

async def _nurse_read(session: AsyncSession, nurse: Nurse) -> NurseRead:
    """Build NurseRead with role_ids/skill_ids from the association tables.

    The ORM Nurse has no role_ids/skill_ids attributes, so returning it raw
    serializes them as [] — the ids must be fetched explicitly.
    """
    rids = sorted(
        (await session.execute(
            select(nurse_roles.c.role_id).where(nurse_roles.c.nurse_id == nurse.id)
        )).scalars().all()
    )
    sids = sorted(
        (await session.execute(
            select(nurse_skills.c.skill_id).where(nurse_skills.c.nurse_id == nurse.id)
        )).scalars().all()
    )
    contract = (
        await session.execute(select(Contract).where(Contract.nurse_id == nurse.id))
    ).scalar_one_or_none()
    out = NurseRead.model_validate(nurse, from_attributes=True)
    out.role_ids = rids
    out.skill_ids = sids
    out.contract = ContractRead.model_validate(contract) if contract else None
    out.tenant_id = nurse.tenant_id
    out.tenant_name = await _tenant_name(session, nurse.tenant_id)
    user = (
        await session.execute(select(User).where(User.nurse_id == nurse.id))
    ).scalar_one_or_none()
    out.user_email = user.email if user else None
    return out


async def _nurse_reads(
    session: AsyncSession,
    nurses: Sequence[Nurse],
) -> list[NurseRead]:
    """Build nurse reads with one batched query per related collection."""
    if not nurses:
        return []

    nurse_ids = [nurse.id for nurse in nurses]
    tenant_ids = {nurse.tenant_id for nurse in nurses if nurse.tenant_id}

    role_rows = (
        await session.execute(
            select(nurse_roles.c.nurse_id, nurse_roles.c.role_id).where(
                nurse_roles.c.nurse_id.in_(nurse_ids)
            )
        )
    ).all()
    skill_rows = (
        await session.execute(
            select(nurse_skills.c.nurse_id, nurse_skills.c.skill_id).where(
                nurse_skills.c.nurse_id.in_(nurse_ids)
            )
        )
    ).all()
    contracts = (
        await session.execute(select(Contract).where(Contract.nurse_id.in_(nurse_ids)))
    ).scalars().all()
    users = (
        await session.execute(
            select(User.nurse_id, User.email).where(User.nurse_id.in_(nurse_ids))
        )
    ).all()
    tenants = (
        await session.execute(
            select(Tenant.id, Tenant.name).where(Tenant.id.in_(tenant_ids))
        )
    ).all() if tenant_ids else []

    role_ids_by_nurse: dict[str, list[str]] = {}
    for nurse_id, role_id in role_rows:
        role_ids_by_nurse.setdefault(nurse_id, []).append(role_id)
    skill_ids_by_nurse: dict[str, list[str]] = {}
    for nurse_id, skill_id in skill_rows:
        skill_ids_by_nurse.setdefault(nurse_id, []).append(skill_id)
    contract_by_nurse = {contract.nurse_id: contract for contract in contracts}
    email_by_nurse = {user.nurse_id: user.email for user in users}
    tenant_names = {tenant.id: tenant.name for tenant in tenants}

    items: list[NurseRead] = []
    for nurse in nurses:
        contract = contract_by_nurse.get(nurse.id)
        out = NurseRead.model_validate(nurse, from_attributes=True)
        out.role_ids = sorted(role_ids_by_nurse.get(nurse.id, []))
        out.skill_ids = sorted(skill_ids_by_nurse.get(nurse.id, []))
        out.contract = ContractRead.model_validate(contract) if contract else None
        out.tenant_id = nurse.tenant_id
        out.tenant_name = tenant_names.get(nurse.tenant_id)
        out.user_email = email_by_nurse.get(nurse.id)
        items.append(out)
    return items


# ──────────────────────────────────────────────────────────────
# Roles
# ──────────────────────────────────────────────────────────────
role_router = APIRouter(prefix="/roles", tags=["roles"])


async def _role_read(session: AsyncSession, role: Role) -> RoleRead:
    out = RoleRead.model_validate(role, from_attributes=True)
    out.tenant_name = await _tenant_name(session, role.tenant_id)
    return out


@role_router.post("", response_model=RoleRead, status_code=status.HTTP_201_CREATED)
async def create_role(
    body: RoleCreate,
    ctx: SchedulerDep,
    requested_tenant_id: str | None = Query(None, alias="tenant_id"),
) -> RoleRead:
    user, tenant_id, session = ctx
    if user.role.value == "super_admin":
        if not requested_tenant_id:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="Super admin must specify tenant_id",
            )
        tenant = await session.get(Tenant, requested_tenant_id)
        if not tenant:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Tenant not found")
        await set_tenant_context(session, requested_tenant_id)
        tenant_id = requested_tenant_id
    elif requested_tenant_id and requested_tenant_id != tenant_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail="Cannot access other tenants",
        )

    if not tenant_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="super-admin must create roles within a tenant (use the tenants API)",
        )
    role = Role(id=str(uuid.uuid4()), tenant_id=tenant_id, **body.model_dump())
    session.add(role)
    await session.flush()
    await session.refresh(role)  # in-txn refresh (RLS-safe: same connection)
    await session.commit()
    return await _role_read(session, role)


@role_router.get("", response_model=Paginated[RoleRead])
async def list_roles(
    ctx: AnyUserDep,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    requested_tenant_id: str | None = Query(None, alias="tenant_id"),
) -> Paginated[RoleRead]:
    user, tenant_id, session = ctx
    if user.role.value == "super_admin" and requested_tenant_id:
        tenant = await session.get(Tenant, requested_tenant_id)
        if not tenant:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Tenant not found")
        await set_tenant_context(session, requested_tenant_id)
        tenant_id = requested_tenant_id
    elif requested_tenant_id and requested_tenant_id != tenant_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Cannot access other tenants")

    count_query = select(func.count(Role.id))
    rows_query = select(Role)
    if tenant_id:
        count_query = count_query.where(Role.tenant_id == tenant_id)
        rows_query = rows_query.where(Role.tenant_id == tenant_id)
    total = (await session.execute(count_query)).scalar_one()
    rows = (
        await session.execute(
            rows_query.offset((page - 1) * page_size).limit(page_size)
        )
    ).scalars().all()
    # Each row belongs to its own tenant (super_admin sees all tenants), so
    # populate tenant_name per-row using row.tenant_id.
    items = [await _role_read(session, row) for row in rows]
    return Paginated(items=items, total=total, page=page, page_size=page_size)


@role_router.get("/{role_id}", response_model=RoleRead)
async def get_role(role_id: str, ctx: AnyUserDep) -> RoleRead:
    _user, _tenant_id, session = ctx
    role = await session.get(Role, role_id)
    if not role:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Role not found")
    return await _role_read(session, role)


@role_router.patch("/{role_id}", response_model=RoleRead)
async def update_role(
    role_id: str,
    body: RoleUpdate,
    ctx: SchedulerDep,
) -> RoleRead:
    _user, _tenant_id, session = ctx
    role = await session.get(Role, role_id)
    if not role:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Role not found")
    if body.name is not None:
        role.name = body.name
    if body.code is not None:
        role.code = body.code
    if body.description is not None:
        role.description = body.description
    await session.flush()
    await session.refresh(role)  # in-txn refresh (RLS-safe: same connection)
    await session.commit()
    return await _role_read(session, role)


@role_router.delete("/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_role(role_id: str, ctx: SchedulerDep) -> None:
    _user, _tenant_id, session = ctx
    role = await session.get(Role, role_id)
    if not role:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Role not found")
    await session.delete(role)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "无法删除：该角色被规则、护士或排班引用",
        ) from None


# ──────────────────────────────────────────────────────────────
# Skills
# ──────────────────────────────────────────────────────────────
skill_router = APIRouter(prefix="/skills", tags=["skills"])


async def _skill_read(session: AsyncSession, skill: Skill) -> SkillRead:
    out = SkillRead.model_validate(skill, from_attributes=True)
    out.tenant_name = await _tenant_name(session, skill.tenant_id)
    return out


@skill_router.post("", response_model=SkillRead, status_code=status.HTTP_201_CREATED)
async def create_skill(
    body: SkillCreate,
    ctx: SchedulerDep,
    requested_tenant_id: str | None = Query(None, alias="tenant_id"),
) -> SkillRead:
    user, tenant_id, session = ctx
    if user.role.value == "super_admin":
        if not requested_tenant_id:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="Super admin must specify tenant_id",
            )
        tenant = await session.get(Tenant, requested_tenant_id)
        if not tenant:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Tenant not found")
        await set_tenant_context(session, requested_tenant_id)
        tenant_id = requested_tenant_id
    elif requested_tenant_id and requested_tenant_id != tenant_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail="Cannot access other tenants",
        )

    if not tenant_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="super-admin must create skills within a tenant (use the tenants API)",
        )
    skill = Skill(id=str(uuid.uuid4()), tenant_id=tenant_id, **body.model_dump())
    session.add(skill)
    await session.flush()
    await session.refresh(skill)  # in-txn refresh (RLS-safe: same connection)
    await session.commit()
    return await _skill_read(session, skill)


@skill_router.get("", response_model=Paginated[SkillRead])
async def list_skills(
    ctx: AnyUserDep,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    requested_tenant_id: str | None = Query(None, alias="tenant_id"),
) -> Paginated[SkillRead]:
    user, tenant_id, session = ctx
    if user.role.value == "super_admin" and requested_tenant_id:
        tenant = await session.get(Tenant, requested_tenant_id)
        if not tenant:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Tenant not found")
        await set_tenant_context(session, requested_tenant_id)
        tenant_id = requested_tenant_id
    elif requested_tenant_id and requested_tenant_id != tenant_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Cannot access other tenants")

    count_query = select(func.count(Skill.id))
    rows_query = select(Skill)
    if tenant_id:
        count_query = count_query.where(Skill.tenant_id == tenant_id)
        rows_query = rows_query.where(Skill.tenant_id == tenant_id)
    total = (await session.execute(count_query)).scalar_one()
    rows = (
        await session.execute(
            rows_query.offset((page - 1) * page_size).limit(page_size)
        )
    ).scalars().all()
    items = [await _skill_read(session, row) for row in rows]
    return Paginated(items=items, total=total, page=page, page_size=page_size)


@skill_router.patch("/{skill_id}", response_model=SkillRead)
async def update_skill(
    skill_id: str,
    body: SkillUpdate,
    ctx: SchedulerDep,
) -> SkillRead:
    _user, _tenant_id, session = ctx
    skill = await session.get(Skill, skill_id)
    if not skill:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Skill not found")
    if body.name is not None:
        skill.name = body.name
    if body.code is not None:
        skill.code = body.code
    await session.flush()
    await session.refresh(skill)  # in-txn refresh (RLS-safe: same connection)
    await session.commit()
    return await _skill_read(session, skill)


@skill_router.delete("/{skill_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_skill(skill_id: str, ctx: SchedulerDep) -> None:
    _user, _tenant_id, session = ctx
    skill = await session.get(Skill, skill_id)
    if not skill:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Skill not found")
    await session.delete(skill)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "无法删除：该技能被护士引用",
        ) from None


# ──────────────────────────────────────────────────────────────
# Nurses (with roles/skills/contract created inline)
# ──────────────────────────────────────────────────────────────
@router.post("", response_model=NurseRead, status_code=status.HTTP_201_CREATED)
async def create_nurse(
    body: NurseCreate,
    ctx: TenantAdminDep,
    requested_tenant_id: str | None = Query(None, alias="tenant_id"),
) -> NurseRead:
    """Create a nurse with optional role_ids/skill_ids and an inline contract."""
    user, tenant_id, session = ctx
    if user.role.value == "super_admin":
        if not requested_tenant_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Super admin must specify tenant_id",
            )
        tenant = await session.get(Tenant, requested_tenant_id)
        if not tenant:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Tenant not found")
        await set_tenant_context(session, requested_tenant_id)
        tenant_id = requested_tenant_id
    elif requested_tenant_id and requested_tenant_id != tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot access other tenants",
        )
    if not tenant_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="super-admin must create nurses within a tenant (use the tenants API)",
        )
    from app.core.plan_limits import effective_limits_for_tenant
    from app.models.subscription import Subscription

    tenant = await session.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tenant not found")
    sub = (
        await session.execute(
            select(Subscription).where(Subscription.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    limits = await effective_limits_for_tenant(session, tenant, sub)
    if limits.max_nurses is not None:
        current = (
            await session.execute(
                select(func.count(Nurse.id)).where(
                    Nurse.tenant_id == tenant_id,
                    Nurse.is_available.is_(True),
                )
            )
        ).scalar_one()
        if current >= limits.max_nurses:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=(
                    f"当前套餐最多 {limits.max_nurses} 名护士，"
                    "请升级套餐后再增加。"
                ),
            )

    # employee_id must be unique per tenant (names may repeat).
    existing = await _nurse_by_employee_id(session, body.employee_id)
    if existing is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"工号 {body.employee_id} 已存在",
        )

    # Validate role_ids belong to this tenant (RLS already scopes, but 404 is nicer)
    if body.role_ids:
        roles = (
            await session.execute(select(Role).where(Role.id.in_(body.role_ids)))
        ).scalars().all()
        if len(roles) != len(set(body.role_ids)):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "One or more role_ids not found")
    if body.skill_ids:
        skills = (
            await session.execute(select(Skill).where(Skill.id.in_(body.skill_ids)))
        ).scalars().all()
        if len(skills) != len(set(body.skill_ids)):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "One or more skill_ids not found")

    nurse = Nurse(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        employee_id=body.employee_id,
        first_name=body.first_name,
        last_name=body.last_name,
        department=body.department,
        is_available=body.is_available,
        preferences=body.preferences,
    )
    if body.contract:
        nurse.contract = Contract(
            id=str(uuid.uuid4()), tenant_id=tenant_id,
            nurse_id=nurse.id, **{k: v for k, v in body.contract.model_dump().items() if k != "nurse_id"},
        )
    session.add(nurse)
    try:
        await session.flush()  # nurse.id populated
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"工号 {body.employee_id} 已存在",
        ) from None
    if body.role_ids:
        await session.execute(
            nurse_roles.insert().values(
                [{"nurse_id": nurse.id, "role_id": rid} for rid in body.role_ids]
            )
        )
    if body.skill_ids:
        await session.execute(
            nurse_skills.insert().values(
                [{"nurse_id": nurse.id, "skill_id": sid} for sid in body.skill_ids]
            )
        )
    await session.flush()
    await session.refresh(nurse)  # in-txn refresh (RLS-safe: same connection)
    await session.commit()
    return await _nurse_read(session, nurse)


@router.get("", response_model=Paginated[NurseRead])
async def list_nurses(
    ctx: AnyUserDep,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    available_only: bool = Query(False),
    tenant_id: str | None = Query(None, description="Filter by tenant (super-admin only)"),
    department: str | None = Query(None, min_length=1, max_length=100),
    department_missing: bool = Query(False),
) -> Paginated[NurseRead]:
    _user, _tenant_id, session = ctx
    stmt = select(Nurse)
    if available_only:
        stmt = stmt.where(Nurse.is_available.is_(True))
    if tenant_id:
        stmt = stmt.where(Nurse.tenant_id == tenant_id)
    if department:
        stmt = stmt.where(Nurse.department == department)
    if department_missing:
        stmt = stmt.where(Nurse.department.is_(None))
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await session.execute(stmt.offset((page - 1) * page_size).limit(page_size))).scalars().all()
    items = await _nurse_reads(session, rows)
    return Paginated(items=items, total=total, page=page, page_size=page_size)


@router.get("/departments", response_model=list[str])
async def list_departments(
    ctx: AnyUserDep,
    requested_tenant_id: str | None = Query(None, alias="tenant_id"),
) -> list[str]:
    """List every distinct department available to the caller's nurse scope."""
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

    count_query = select(Nurse.department).where(Nurse.department.is_not(None))
    if tenant_id:
        count_query = count_query.where(Nurse.tenant_id == tenant_id)
    rows = (
        await session.execute(
            count_query.distinct().order_by(Nurse.department)
        )
    ).scalars().all()
    return [department for department in rows if department is not None]


@router.delete("/departments")
async def delete_department(
    ctx: TenantAdminDep,
    department: str = Query(min_length=1, max_length=100),
    requested_tenant_id: str | None = Query(None, alias="tenant_id"),
) -> dict[str, int]:
    """Clear a derived department value from nurses in the current tenant."""
    user, ctx_tenant_id, session = ctx
    tenant_id = ctx_tenant_id
    if user.role.value == "super_admin":
        if not requested_tenant_id:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="Super admin must specify tenant_id",
            )
        tenant = await session.get(Tenant, requested_tenant_id)
        if not tenant:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Tenant not found")
        await set_tenant_context(session, requested_tenant_id)
        tenant_id = requested_tenant_id
    elif requested_tenant_id and requested_tenant_id != ctx_tenant_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Cannot access other tenants")
    if not tenant_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "super-admin must update nurses within a tenant",
        )
    result = await session.execute(
        update(Nurse)
        .where(
            Nurse.tenant_id == tenant_id,
            Nurse.department == department.strip(),
        )
        .values(department=None)
    )
    await session.commit()
    return {"updated": cast(CursorResult[Any], result).rowcount}


@router.get("/{nurse_id}", response_model=NurseRead)
async def get_nurse(nurse_id: str, ctx: AnyUserDep) -> NurseRead:
    _user, _tenant_id, session = ctx
    nurse = await session.get(Nurse, nurse_id)
    if not nurse:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Nurse not found")
    return await _nurse_read(session, nurse)


@router.patch("/{nurse_id}", response_model=NurseRead)
async def update_nurse(
    nurse_id: str,
    body: NurseCreate,
    ctx: TenantAdminDep,
) -> NurseRead:
    """Full replace of nurse fields (roles/skills/contract re-applied)."""
    _user, tenant_id, session = ctx
    nurse = await session.get(Nurse, nurse_id)
    if not nurse:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Nurse not found")

    # Reject if the new employee_id collides with a *different* nurse in
    # this tenant (self is allowed — unchanged or re-submitted).
    if body.employee_id != nurse.employee_id:
        clash = await _nurse_by_employee_id(session, body.employee_id)
        if clash is not None and clash.id != nurse.id:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"工号 {body.employee_id} 已存在",
            )

    nurse.employee_id = body.employee_id
    nurse.first_name = body.first_name
    nurse.last_name = body.last_name
    nurse.department = body.department
    nurse.is_available = body.is_available
    nurse.preferences = body.preferences
    # Replace contract
    if nurse.contract:
        await session.delete(nurse.contract)
        await session.flush()
    if body.contract:
        nurse.contract = Contract(
            id=str(uuid.uuid4()), tenant_id=nurse.tenant_id,
            nurse_id=nurse.id, **{k: v for k, v in body.contract.model_dump().items() if k != "nurse_id"},
        )
    # Replace roles/skills
    await session.execute(nurse_roles.delete().where(nurse_roles.c.nurse_id == nurse.id))
    await session.execute(nurse_skills.delete().where(nurse_skills.c.nurse_id == nurse.id))
    if body.role_ids:
        await session.execute(
            nurse_roles.insert().values(
                [{"nurse_id": nurse.id, "role_id": rid} for rid in body.role_ids]
            )
        )
    if body.skill_ids:
        await session.execute(
            nurse_skills.insert().values(
                [{"nurse_id": nurse.id, "skill_id": sid} for sid in body.skill_ids]
            )
        )
    await session.flush()
    await session.refresh(nurse)  # in-txn refresh (RLS-safe: same connection)
    await session.commit()
    return await _nurse_read(session, nurse)


@router.delete("/{nurse_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_nurse(nurse_id: str, ctx: TenantAdminDep) -> None:
    _user, _tenant_id, session = ctx
    nurse = await session.get(Nurse, nurse_id)
    if not nurse:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Nurse not found")
    await session.delete(nurse)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "无法删除：该护士被排班或偏好引用",
        ) from None


# ──────────────────────────────────────────────────────────────
# Leaves (nested under a nurse)
# ──────────────────────────────────────────────────────────────
@router.post("/{nurse_id}/leaves", response_model=LeaveRead, status_code=status.HTTP_201_CREATED)
async def create_leave(
    nurse_id: str,
    body: LeaveCreate,
    ctx: SchedulerDep,
) -> LeaveRead:
    _user, tenant_id, session = ctx
    nurse = await session.get(Nurse, nurse_id)
    if not nurse:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Nurse not found")
    tenant_id = nurse.tenant_id
    await set_tenant_context(session, tenant_id)
    leave = Leave(
        id=str(uuid.uuid4()), tenant_id=tenant_id,
        nurse_id=nurse_id, date=body.date, description=body.description,
    )
    session.add(leave)
    await session.flush()
    await session.refresh(leave)  # in-txn refresh (RLS-safe: same connection)
    await session.commit()
    return LeaveRead.model_validate(leave)


@router.get("/{nurse_id}/leaves", response_model=list[LeaveRead])
async def list_leaves(nurse_id: str, ctx: AnyUserDep) -> list[LeaveRead]:
    _user, _tenant_id, session = ctx
    rows = (
        await session.execute(select(Leave).where(Leave.nurse_id == nurse_id))
    ).scalars().all()
    return [LeaveRead.model_validate(row) for row in rows]


@router.delete("/{nurse_id}/leaves/{leave_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_leave(
    nurse_id: str,
    leave_id: str,
    ctx: SchedulerDep,
) -> None:
    _user, _tenant_id, session = ctx
    leave = await session.get(Leave, leave_id)
    if not leave or leave.nurse_id != nurse_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Leave not found")
    await session.delete(leave)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "无法删除：该请假记录被引用",
        ) from None


# ──────────────────────────────────────────────────────────────
# Contracts (read/update — created inline with nurse)
# ──────────────────────────────────────────────────────────────
@router.get("/{nurse_id}/contract", response_model=ContractRead)
async def get_contract(nurse_id: str, ctx: AnyUserDep) -> ContractRead:
    _user, _tenant_id, session = ctx
    row = await session.execute(select(Contract).where(Contract.nurse_id == nurse_id))
    contract = row.scalar_one_or_none()
    if not contract:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contract not found")
    return ContractRead.model_validate(contract)


@router.put("/{nurse_id}/contract", response_model=ContractRead)
async def upsert_contract(
    nurse_id: str,
    body: ContractCreate,
    ctx: SchedulerDep,
) -> ContractRead:
    _user, tenant_id, session = ctx
    nurse = await session.get(Nurse, nurse_id)
    if not nurse:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Nurse not found")
    tenant_id = nurse.tenant_id
    await set_tenant_context(session, tenant_id)
    if nurse.contract:
        for f, v in body.model_dump().items():
            if f == "nurse_id":
                continue
            setattr(nurse.contract, f, v)
    else:
        nurse.contract = Contract(
            id=str(uuid.uuid4()), tenant_id=tenant_id,
            nurse_id=nurse_id,
            **{k: v for k, v in body.model_dump().items() if k != "nurse_id"},
        )
    await session.flush()
    await session.flush()
    await session.refresh(nurse.contract)  # in-txn refresh (RLS-safe: same connection)
    await session.commit()
    return ContractRead.model_validate(nurse.contract)
