"""Tenant management endpoints (super-admin only)."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import SuperAdminDep
from app.core.audit import record_security_event
from app.core.plan_limits import configured_plan_limits, is_demo_tenant
from app.core.refresh_tokens import revoke_refresh_tokens_for_user
from app.core.security import hash_password
from app.models.enums import SubscriptionPlan, UserRole
from app.models.nurse import Role
from app.models.subscription import Subscription
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas import (
    Paginated,
    SubscriptionUpsert,
    TenantCreate,
    TenantRead,
    TenantUpdate,
    TenantWithAdminCreate,
    TenantWithAdminRead,
    UserCreate,
    UserRead,
)

router = APIRouter(prefix="/tenants", tags=["tenants"])

TENANT_SCOPED_TABLES = (
    "assignments",
    "schedules",
    "schedule_requests",
    "nurse_preferences",
    "leaves",
    "contracts",
    "nurses",
    "skill_mix_requirements",
    "skill_mix_rules",
    "shift_sequence_role_restrictions",
    "shift_sequence_steps",
    "shift_sequence_rules",
    "shift_templates",
    "day_group_days",
    "day_groups",
    "roles",
    "skills",
    "subscriptions",
    "refresh_tokens",
    "users",
)


async def _create_initial_subscription(
    session: AsyncSession,
    tenant: Tenant,
    body: SubscriptionUpsert,
    actor_id: str,
) -> Subscription:
    plans = await configured_plan_limits(session)
    if body.plan not in plans:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="套餐不存在",
        )
    tenant_is_demo = is_demo_tenant(tenant)
    if tenant_is_demo and body.plan != SubscriptionPlan.DEMO:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Demo 租户固定使用 demo 套餐，不能修改。",
        )
    if not tenant_is_demo and body.plan == SubscriptionPlan.DEMO:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="demo 套餐仅用于 Demo 租户。",
        )
    subscription = Subscription(
        tenant_id=tenant.id,
        plan=body.plan,
        subscription_type=body.subscription_type,
        seat_packs=body.seat_packs,
        starts_at=body.starts_at or datetime.now(UTC),
        ends_at=body.ends_at,
        is_canceled=body.is_canceled,
    )
    session.add(subscription)
    await session.flush()
    await session.refresh(subscription)
    await record_security_event(
        session,
        action="subscription.upsert",
        actor_id=actor_id,
        tenant_id=tenant.id,
        details={
            "plan": body.plan,
            "seat_packs": body.seat_packs,
            "is_canceled": body.is_canceled,
        },
    )
    return subscription


def _seed_default_role(session: AsyncSession, tenant_id: str) -> None:
    """Create the built-in 'any role' so nurses can always be assigned."""
    session.add(Role(tenant_id=tenant_id, name="任意角色", code="any"))


@router.post(
    "/with-admin",
    response_model=TenantWithAdminRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_tenant_with_admin(
    body: TenantWithAdminCreate,
    ctx: SuperAdminDep,
) -> TenantWithAdminRead:
    """Atomically create a tenant, its first administrator, and subscription."""
    user, _tenant_id, session = ctx

    existing_tenant = await session.execute(
        select(Tenant).where(Tenant.slug == body.slug)
    )
    if existing_tenant.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Tenant slug '{body.slug}' already exists",
        )
    existing_admin = await session.execute(
        select(User).where(func.lower(User.email) == body.admin.email.lower())
    )
    if existing_admin.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already registered",
        )

    if body.subscription is not None:
        plans = await configured_plan_limits(session)
        if body.subscription.plan not in plans:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="套餐不存在",
            )

    tenant = Tenant(
        name=body.name,
        slug=body.slug,
        settings=body.settings,
    )
    session.add(tenant)
    await session.flush()
    _seed_default_role(session, tenant.id)

    admin = User(
        tenant_id=tenant.id,
        email=body.admin.email.lower(),
        hashed_password=hash_password(body.admin.password),
        first_name=body.admin.first_name,
        last_name=body.admin.last_name,
        role=UserRole.TENANT_ADMIN,
        is_active=True,
    )
    session.add(admin)
    await session.flush()

    subscription = None
    if body.subscription is not None:
        subscription = await _create_initial_subscription(
            session, tenant, body.subscription, actor_id=user.id
        )

    await record_security_event(
        session,
        action="tenant.create",
        actor_id=user.id,
        tenant_id=tenant.id,
        details={"slug": tenant.slug, "source": "tenant_with_admin"},
    )
    await record_security_event(
        session,
        action="tenant.admin_create",
        actor_id=user.id,
        tenant_id=tenant.id,
        target_user_id=admin.id,
    )

    await session.refresh(tenant)
    await session.refresh(admin)
    response = TenantWithAdminRead.model_validate(
        {
            "tenant": tenant,
            "admin": admin,
            "subscription": subscription,
        }
    )
    await session.commit()
    return response


@router.post("", response_model=TenantRead, status_code=status.HTTP_201_CREATED)
async def create_tenant(
    body: TenantCreate,
    ctx: SuperAdminDep,
) -> TenantRead:
    """Create a new tenant (super-admin only)."""
    user, _tenant_id, session = ctx

    # Slug uniqueness
    existing = await session.execute(select(Tenant).where(Tenant.slug == body.slug))
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Tenant slug '{body.slug}' already exists",
        )

    tenant = Tenant(
        name=body.name,
        slug=body.slug,
        settings=body.settings,
    )
    session.add(tenant)
    await session.flush()
    _seed_default_role(session, tenant.id)
    await record_security_event(
        session,
        action="tenant.create",
        actor_id=user.id,
        tenant_id=tenant.id,
        details={"slug": tenant.slug},
    )
    await session.flush()
    await session.refresh(tenant)  # in-txn refresh (RLS-safe: same connection)
    await session.commit()
    return TenantRead.model_validate(tenant)


@router.get("", response_model=Paginated[TenantRead])
async def list_tenants(
    ctx: SuperAdminDep,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> Paginated[TenantRead]:
    """List all tenants (super-admin only)."""
    _user, _tenant_id, session = ctx

    total_result = await session.execute(select(func.count(Tenant.id)))
    total = total_result.scalar_one()

    result = await session.execute(
        select(Tenant)
        .offset((page - 1) * page_size)
        .limit(page_size)
        .order_by(Tenant.created_at.desc())
    )
    tenants = list(result.scalars().all())
    return Paginated(
        items=[TenantRead.model_validate(tenant) for tenant in tenants],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{tenant_id}", response_model=TenantRead)
async def get_tenant(
    tenant_id: str,
    ctx: SuperAdminDep,
) -> TenantRead:
    _user, _tenant_id, session = ctx
    tenant = await session.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    return TenantRead.model_validate(tenant)


@router.get("/{tenant_id}/admin", response_model=UserRead)
async def get_tenant_admin(
    tenant_id: str,
    ctx: SuperAdminDep,
) -> UserRead:
    """Load the first tenant administrator for the tenant edit form."""
    _user, _tenant_id, session = ctx
    tenant = await session.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    users = (
        await session.execute(
            select(User)
            .where(User.tenant_id == tenant_id)
            .order_by(User.created_at, User.id)
        )
    ).scalars()
    admin = next((user for user in users if user.role == UserRole.TENANT_ADMIN), None)
    if not admin:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant admin not found")
    return UserRead.model_validate(admin)


@router.patch("/{tenant_id}", response_model=TenantRead)
async def update_tenant(
    tenant_id: str,
    body: TenantUpdate,
    ctx: SuperAdminDep,
) -> TenantRead:
    user, _tenant_id, session = ctx
    tenant = await session.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    admin = None
    if body.admin_email is not None:
        tenant_users = (
            await session.execute(
                select(User).where(User.tenant_id == tenant_id).order_by(User.created_at, User.id)
            )
        ).scalars()
        admin = next(
            (user for user in tenant_users if user.role == UserRole.TENANT_ADMIN),
            None,
        )
        if not admin:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Tenant admin not found")
        normalized_email = body.admin_email.lower()
        if normalized_email != admin.email.lower():
            existing_user = (
                await session.execute(
                    select(User).where(func.lower(User.email) == normalized_email)
                )
            ).scalar_one_or_none()
            if existing_user and existing_user.id != admin.id:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    detail="Email already registered",
                )
        admin.email = normalized_email
        admin.token_version += 1
        await revoke_refresh_tokens_for_user(session, admin.id)

    if body.name is not None:
        tenant.name = body.name
    if body.settings is not None:
        tenant.settings = body.settings
    if body.is_active is not None:
        tenant.is_active = body.is_active
    await session.flush()
    await record_security_event(
        session,
        action="tenant.update",
        actor_id=user.id,
        tenant_id=tenant_id,
        target_user_id=admin.id if admin is not None else None,
        details={
            "admin_email_changed": body.admin_email is not None,
            "activation_changed": body.is_active is not None,
        },
    )
    await session.refresh(tenant)  # in-txn refresh (RLS-safe: same connection)
    await session.commit()
    return TenantRead.model_validate(tenant)


@router.delete("/{tenant_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tenant(
    tenant_id: str,
    ctx: SuperAdminDep,
) -> None:
    user, _tenant_id, session = ctx
    tenant = await session.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    await session.execute(
        text("UPDATE users SET nurse_id = NULL WHERE tenant_id = :tenant_id"),
        {"tenant_id": tenant_id},
    )
    for table_name in TENANT_SCOPED_TABLES:
        await session.execute(
            text(f"DELETE FROM {table_name} WHERE tenant_id = :tenant_id"),
            {"tenant_id": tenant_id},
        )
    await session.delete(tenant)
    await record_security_event(
        session,
        action="tenant.delete",
        actor_id=user.id,
        tenant_id=tenant_id,
        details={"slug": tenant.slug},
    )
    await session.commit()


@router.post(
    "/{tenant_id}/admin",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_tenant_admin(
    tenant_id: str,
    body: UserCreate,
    ctx: SuperAdminDep,
) -> UserRead:
    """Create the first admin user for a tenant (super-admin bootstraps tenants)."""
    user, _tenant_id, session = ctx

    tenant = await session.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    existing = await session.execute(
        select(User).where(func.lower(User.email) == body.email.lower())
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already registered",
        )

    admin = User(
        tenant_id=tenant_id,
        email=body.email.lower(),
        hashed_password=hash_password(body.password),
        first_name=body.first_name,
        last_name=body.last_name,
        role=UserRole.TENANT_ADMIN,
        is_active=True,
    )
    session.add(admin)
    await session.flush()
    await record_security_event(
        session,
        action="tenant.admin_create",
        actor_id=user.id,
        tenant_id=tenant_id,
        target_user_id=admin.id,
    )
    await session.flush()
    await session.refresh(admin)  # in-txn refresh (RLS-safe: same connection)
    await session.commit()
    return UserRead.model_validate(admin)
