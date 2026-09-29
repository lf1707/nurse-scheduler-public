"""Subscriptions — one per tenant, gate schedule generation.

GET  /subscriptions/me         → own tenant's subscription (any user)
GET  /subscriptions            → all subscriptions (super_admin)
PUT  /subscriptions/{tenant_id} → create/update a subscription (super_admin)
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AnyUserDep, SuperAdminDep, set_tenant_context
from app.core.audit import record_security_event
from app.core.billing_entitlements import invoice_paid_through
from app.core.plan_limits import configured_plan_limits, effective_limits_for_tenant, is_demo_tenant
from app.models.enums import SubscriptionPlan, SubscriptionType
from app.models.subscription import Subscription
from app.models.tenant import Tenant
from app.schemas import PlanLimitsRead, SubscriptionRead, SubscriptionUpsert

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])


async def ensure_active_subscription(session: AsyncSession, tenant_id: str) -> Subscription:
    """Return the tenant's active subscription or raise 403.

    Shared gate used by schedule generation. RLS scopes the SELECT to the
    tenant, so a missing row surfaces here as `not found`. A paid invoice with
    a current billing period is an equivalent paid-through entitlement while
    Stripe operations are not yet connected.
    """
    result = await session.execute(
        select(Subscription).where(Subscription.tenant_id == tenant_id)
    )
    sub = result.scalar_one_or_none()
    if sub is not None and sub.is_active:
        return sub

    paid_through = await invoice_paid_through(session, tenant_id)
    if paid_through is not None and datetime.now(UTC) < paid_through:
        return Subscription(
            tenant_id=tenant_id,
            plan=sub.plan if sub is not None else SubscriptionPlan.PRO.value,
            subscription_type=(
                sub.subscription_type if sub is not None else SubscriptionType.MANUAL.value
            ),
            seat_packs=sub.seat_packs if sub is not None else 0,
            starts_at=sub.starts_at if sub is not None else datetime.now(UTC),
            ends_at=paid_through,
            is_canceled=False,
        )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="订阅未开通或已过期,无法生成排班。请联系管理员开通订阅。",
    )


@router.get("/me", response_model=SubscriptionRead | None)
async def get_my_subscription(ctx: AnyUserDep) -> SubscriptionRead | None:
    """Current tenant's subscription (null when none exists)."""
    _user, tenant_id, session = ctx
    if not tenant_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Super admin has no tenant subscription",
        )
    result = await session.execute(
        select(Subscription).where(Subscription.tenant_id == tenant_id)
    )
    subscription = result.scalar_one_or_none()
    if subscription is None:
        return None
    return SubscriptionRead.model_validate(subscription)


@router.get("/me/limits", response_model=PlanLimitsRead)
async def get_my_plan_limits(ctx: AnyUserDep) -> PlanLimitsRead:
    """Return the effective plan limits for the current tenant."""
    _user, tenant_id, session = ctx
    if not tenant_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Super admin has no tenant subscription",
        )
    tenant = await session.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tenant not found",
        )
    sub = (
        await session.execute(
            select(Subscription).where(Subscription.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    limits = await effective_limits_for_tenant(session, tenant, sub)
    return PlanLimitsRead(
        max_nurses=limits.max_nurses,
        max_period_days=limits.max_period_days,
    )


@router.get("", response_model=list[SubscriptionRead])
async def list_subscriptions(ctx: SuperAdminDep) -> list[SubscriptionRead]:
    """All subscriptions (super admin; RLS super bypass active)."""
    _user, _tenant_id, session = ctx
    result = await session.execute(select(Subscription))
    return [
        SubscriptionRead.model_validate(subscription)
        for subscription in result.scalars().all()
    ]


@router.get("/{tenant_id}", response_model=SubscriptionRead | None)
async def get_subscription(tenant_id: str, ctx: SuperAdminDep) -> SubscriptionRead | None:
    """Single tenant's subscription (super admin only; null if none)."""
    _user, _own_tenant_id, session = ctx
    # RLS super bypass is active; read the row for the target tenant directly.
    await set_tenant_context(session, tenant_id)
    result = await session.execute(
        select(Subscription).where(Subscription.tenant_id == tenant_id)
    )
    subscription = result.scalar_one_or_none()
    if subscription is None:
        return None
    return SubscriptionRead.model_validate(subscription)


@router.get("/{tenant_id}/limits", response_model=PlanLimitsRead)
async def get_tenant_plan_limits(tenant_id: str, ctx: SuperAdminDep) -> PlanLimitsRead:
    """Return effective limits for a selected tenant."""
    _user, _own_tenant_id, session = ctx
    tenant = await session.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tenant not found",
        )
    sub = (
        await session.execute(
            select(Subscription).where(Subscription.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    limits = await effective_limits_for_tenant(session, tenant, sub)
    return PlanLimitsRead(
        max_nurses=limits.max_nurses,
        max_period_days=limits.max_period_days,
    )


@router.put("/{tenant_id}", response_model=SubscriptionRead)
async def upsert_subscription(
    tenant_id: str,
    body: SubscriptionUpsert,
    ctx: SuperAdminDep,
) -> SubscriptionRead:
    """Create or update the subscription for a tenant (super admin only)."""
    user, _own_tenant_id, session = ctx

    # tenants table is only readable cross-tenant with the super bypass set
    # (SuperAdminDep did that); verify the tenant exists.
    tenant = (await session.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tenant not found",
        )
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

    result = await session.execute(
        select(Subscription).where(Subscription.tenant_id == tenant_id)
    )
    sub = result.scalar_one_or_none()
    starts_at = body.starts_at or datetime.now(UTC)
    if sub is None:
        sub = Subscription(
            tenant_id=tenant_id,
            plan=body.plan,
            subscription_type=body.subscription_type,
            seat_packs=body.seat_packs,
            starts_at=starts_at,
            ends_at=body.ends_at,
            is_canceled=body.is_canceled,
        )
        session.add(sub)
    else:
        sub.plan = SubscriptionPlan.DEMO if tenant_is_demo else body.plan
        sub.subscription_type = body.subscription_type
        sub.seat_packs = body.seat_packs
        sub.starts_at = starts_at
        sub.ends_at = body.ends_at
        sub.is_canceled = body.is_canceled
    await session.flush()
    await session.refresh(sub)
    response = SubscriptionRead.model_validate(sub)
    await record_security_event(
        session,
        action="subscription.upsert",
        actor_id=user.id,
        tenant_id=tenant_id,
        details={
            "plan": body.plan,
            "seat_packs": body.seat_packs,
            "is_canceled": body.is_canceled,
        },
    )
    await session.commit()
    return response
