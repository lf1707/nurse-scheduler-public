"""Shifts CRUD — DayGroup + ShiftTemplate (tenant-scoped)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AnyUserDep, SchedulerDep, set_tenant_context
from app.models import DayGroup, DayGroupDay, ShiftTemplate, Tenant, _tenant_name
from app.schemas import (
    DayGroupCreate,
    DayGroupRead,
    DayGroupUpdate,
    Paginated,
    ShiftTemplateCreate,
    ShiftTemplateRead,
)

day_group_router = APIRouter(prefix="/day-groups", tags=["shifts"])
shift_router = APIRouter(prefix="/shift-templates", tags=["shifts"])


async def _dg_read(session: AsyncSession, dg: DayGroup) -> DayGroupRead:
    """Build DayGroupRead with day_numbers from the day_group_days table.

    The ORM DayGroup has no day_numbers attribute, so returning it raw
    serializes it as [].
    """
    nums = sorted(
        (await session.execute(
            select(DayGroupDay.day_number).where(DayGroupDay.day_group_id == dg.id)
        )).scalars().all()
    )
    out = DayGroupRead.model_validate(dg, from_attributes=True)
    out.day_numbers = nums
    out.tenant_id = dg.tenant_id
    out.tenant_name = await _tenant_name(session, dg.tenant_id)
    return out


# ──────────────────────────────────────────────────────────────
# Day Groups
# ──────────────────────────────────────────────────────────────
@day_group_router.post("", response_model=DayGroupRead, status_code=status.HTTP_201_CREATED)
async def create_day_group(
    body: DayGroupCreate,
    ctx: SchedulerDep,
    requested_tenant_id: str | None = Query(None, alias="tenant_id"),
) -> DayGroupRead:
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
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Cannot access other tenants")
    if not tenant_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "super-admin must create day groups within a tenant (use the tenants API)",
        )
    dg = DayGroup(id=str(uuid.uuid4()), tenant_id=tenant_id, name=body.name, description=body.description)
    session.add(dg)
    await session.flush()
    for d in body.day_numbers:
        session.add(DayGroupDay(id=str(uuid.uuid4()), tenant_id=tenant_id, day_group_id=dg.id, day_number=d))
    await session.flush()
    await session.refresh(dg)  # in-txn refresh (RLS-safe: same connection)
    await session.commit()
    return await _dg_read(session, dg)


@day_group_router.get("", response_model=Paginated[DayGroupRead])
async def list_day_groups(
    ctx: AnyUserDep,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    requested_tenant_id: str | None = Query(None, alias="tenant_id"),
) -> Paginated[DayGroupRead]:
    user, tenant_id, session = ctx
    if user.role.value == "super_admin" and requested_tenant_id:
        tenant = await session.get(Tenant, requested_tenant_id)
        if not tenant:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Tenant not found")
        await set_tenant_context(session, requested_tenant_id)
        tenant_id = requested_tenant_id
    elif requested_tenant_id and requested_tenant_id != tenant_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Cannot access other tenants")

    count_query = select(func.count(DayGroup.id))
    rows_query = select(DayGroup)
    if tenant_id:
        count_query = count_query.where(DayGroup.tenant_id == tenant_id)
        rows_query = rows_query.where(DayGroup.tenant_id == tenant_id)
    total = (await session.execute(count_query)).scalar_one()
    rows = (
        await session.execute(rows_query.offset((page - 1) * page_size).limit(page_size))
    ).scalars().all()
    items = [await _dg_read(session, dg) for dg in rows]
    return Paginated(items=items, total=total, page=page, page_size=page_size)


@day_group_router.get("/{day_group_id}", response_model=DayGroupRead)
async def get_day_group(day_group_id: str, ctx: AnyUserDep) -> DayGroupRead:
    _user, _tenant_id, session = ctx
    dg = await session.get(DayGroup, day_group_id)
    if not dg:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Day group not found")
    return await _dg_read(session, dg)


@day_group_router.patch("/{day_group_id}", response_model=DayGroupRead)
async def update_day_group(
    day_group_id: str,
    body: DayGroupUpdate,
    ctx: SchedulerDep,
) -> DayGroupRead:
    _user, _tenant_id, session = ctx
    dg = await session.get(DayGroup, day_group_id)
    if not dg:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Day group not found")
    dg.name = body.name
    dg.description = body.description
    dg.days = [
        DayGroupDay(
            id=str(uuid.uuid4()),
            tenant_id=dg.tenant_id,
            day_group_id=dg.id,
            day_number=day_number,
        )
        for day_number in body.day_numbers
    ]
    await session.flush()
    await session.refresh(dg)  # in-txn refresh (RLS-safe: same connection)
    await session.commit()
    return await _dg_read(session, dg)


@day_group_router.delete("/{day_group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_day_group(day_group_id: str, ctx: SchedulerDep) -> None:
    _user, _tenant_id, session = ctx
    dg = await session.get(DayGroup, day_group_id)
    if not dg:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Day group not found")
    await session.delete(dg)  # cascade removes days
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "无法删除：该日组被班次模板引用，请先删除关联的班次",
        ) from None


# ──────────────────────────────────────────────────────────────
# Shift Templates
# ──────────────────────────────────────────────────────────────
@shift_router.post("", response_model=ShiftTemplateRead, status_code=status.HTTP_201_CREATED)
async def create_shift_template(
    body: ShiftTemplateCreate,
    ctx: SchedulerDep,
    requested_tenant_id: str | None = Query(None, alias="tenant_id"),
) -> ShiftTemplateRead:
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
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Cannot access other tenants")
    if not tenant_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "super-admin must create shift templates within a tenant (use the tenants API)",
        )
    # validate day_group exists in this tenant
    dg = await session.get(DayGroup, body.day_group_id)
    if not dg:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "day_group_id not found")
    # code must be unique within the tenant (E/N/D etc.), so a template name
    # can never collide with an existing one.
    existing = await session.execute(
        select(ShiftTemplate).where(
            ShiftTemplate.tenant_id == tenant_id,
            ShiftTemplate.code == body.code,
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"班次 code {body.code} 在该租户已存在",
        )
    st = ShiftTemplate(
        id=str(uuid.uuid4()), tenant_id=tenant_id,
        code=body.code, name=body.name, start_time=body.start_time, end_time=body.end_time,
        duration_hours=body.duration_hours, color=body.color, day_group_id=body.day_group_id,
    )
    session.add(st)
    await session.flush()
    await session.refresh(st)  # in-txn refresh (RLS-safe: same connection)
    await session.commit()
    return ShiftTemplateRead.model_validate(st, from_attributes=True)


@shift_router.get("", response_model=Paginated[ShiftTemplateRead])
async def list_shift_templates(
    ctx: AnyUserDep,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    requested_tenant_id: str | None = Query(None, alias="tenant_id"),
) -> Paginated[ShiftTemplateRead]:
    user, tenant_id, session = ctx
    if user.role.value == "super_admin" and requested_tenant_id:
        tenant = await session.get(Tenant, requested_tenant_id)
        if not tenant:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Tenant not found")
        await set_tenant_context(session, requested_tenant_id)
        tenant_id = requested_tenant_id
    elif requested_tenant_id and requested_tenant_id != tenant_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Cannot access other tenants")

    count_query = select(func.count(ShiftTemplate.id))
    rows_query = select(ShiftTemplate)
    if tenant_id:
        count_query = count_query.where(ShiftTemplate.tenant_id == tenant_id)
        rows_query = rows_query.where(ShiftTemplate.tenant_id == tenant_id)
    total = (await session.execute(count_query)).scalar_one()
    rows = (
        await session.execute(
            rows_query.offset((page - 1) * page_size).limit(page_size)
        )
    ).scalars().all()
    # super_admin sees all tenants, so populate tenant_name per-row.
    items = []
    for row in rows:
        item = ShiftTemplateRead.model_validate(row, from_attributes=True)
        item.tenant_name = await _tenant_name(session, row.tenant_id)
        items.append(item)
    return Paginated(items=items, total=total, page=page, page_size=page_size)


@shift_router.get("/{shift_id}", response_model=ShiftTemplateRead)
async def get_shift_template(shift_id: str, ctx: AnyUserDep) -> ShiftTemplateRead:
    _user, _tenant_id, session = ctx
    st = await session.get(ShiftTemplate, shift_id)
    if not st:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Shift template not found")
    return ShiftTemplateRead.model_validate(st, from_attributes=True)


@shift_router.patch("/{shift_id}", response_model=ShiftTemplateRead)
async def update_shift_template(
    shift_id: str, body: ShiftTemplateCreate, ctx: SchedulerDep
) -> ShiftTemplateRead:
    _user, _tenant_id, session = ctx
    st = await session.get(ShiftTemplate, shift_id)
    if not st:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Shift template not found")
    if body.day_group_id != st.day_group_id:
        dg = await session.get(DayGroup, body.day_group_id)
        if not dg:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "day_group_id not found")
        st.day_group_id = body.day_group_id
    st.code = body.code
    st.name = body.name
    st.start_time = body.start_time
    st.end_time = body.end_time
    st.duration_hours = body.duration_hours
    st.color = body.color
    await session.flush()
    await session.refresh(st)  # in-txn refresh (RLS-safe: same connection)
    await session.commit()
    return ShiftTemplateRead.model_validate(st, from_attributes=True)


@shift_router.delete("/{shift_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_shift_template(shift_id: str, ctx: SchedulerDep) -> None:
    _user, _tenant_id, session = ctx
    st = await session.get(ShiftTemplate, shift_id)
    if not st:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Shift template not found")
    await session.delete(st)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "无法删除：该班次被排班、规则或护士偏好引用",
        ) from None
