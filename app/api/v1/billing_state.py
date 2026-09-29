"""Super-admin provider billing state, isolated from entitlement decisions."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import SuperAdminDep
from app.core.audit import record_security_event
from app.models.billing import (
    BillingCustomer,
    BillingOperation,
    BillingSubscription,
)
from app.models.enums import BillingOperationStatus
from app.models.tenant import Tenant
from app.schemas import (
    BillingCustomerCreate,
    BillingCustomerRead,
    BillingOperationCreate,
    BillingOperationRead,
    BillingOperationUpdate,
    BillingReconciliationRead,
    BillingSubscriptionCreate,
    BillingSubscriptionRead,
    BillingSubscriptionReconcile,
    BillingSubscriptionUpdate,
)
from app.services.billing_reconciliation import (
    ReconciliationConflictError,
    ReconciliationNotFoundError,
    reconcile_subscription,
)

router = APIRouter(prefix="/billing", tags=["billing"])

_PROVIDERS = frozenset({"manual", "stripe"})
_OPERATION_TERMINAL_STATUSES = frozenset(
    {BillingOperationStatus.COMPLETED, BillingOperationStatus.CANCELED}
)
_ALLOWED_OPERATION_TRANSITIONS = {
    BillingOperationStatus.PENDING: {
        BillingOperationStatus.COMPLETED,
        BillingOperationStatus.FAILED,
        BillingOperationStatus.CANCELED,
    },
    BillingOperationStatus.FAILED: {
        BillingOperationStatus.PENDING,
        BillingOperationStatus.CANCELED,
    },
    BillingOperationStatus.COMPLETED: set(),
    BillingOperationStatus.CANCELED: set(),
}


async def _get_tenant_or_404(session: AsyncSession, tenant_id: str) -> Tenant:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="租户不存在")
    return tenant


async def _get_customer_or_404(
    session: AsyncSession,
    customer_id: str,
) -> BillingCustomer:
    customer = await session.get(BillingCustomer, customer_id)
    if customer is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="账单客户不存在")
    return customer


async def _get_subscription_or_404(
    session: AsyncSession,
    subscription_id: str,
) -> BillingSubscription:
    subscription = await session.get(BillingSubscription, subscription_id)
    if subscription is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="账单订阅不存在")
    return subscription


async def _get_operation_or_404(
    session: AsyncSession,
    operation_id: str,
) -> BillingOperation:
    operation = await session.get(BillingOperation, operation_id)
    if operation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="账单操作不存在")
    return operation


async def _validate_provider_reference(
    session: AsyncSession,
    *,
    tenant_id: str,
    provider: str,
    customer: BillingCustomer | None,
    subscription: BillingSubscription | None,
) -> None:
    if customer is not None and (
        customer.tenant_id != tenant_id or customer.provider != provider
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="账单客户不属于目标租户或 provider",
        )
    if subscription is not None and (
        subscription.tenant_id != tenant_id
        or subscription.provider != provider
        or (customer is not None and subscription.customer_id != customer.id)
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="账单订阅与租户、provider 或客户不匹配",
        )


@router.get("/customers", response_model=list[BillingCustomerRead])
async def list_customers(
    ctx: SuperAdminDep,
    tenant_id: str | None = Query(None, min_length=36, max_length=36),
    provider: str | None = Query(None, pattern=r"^(manual|stripe)$"),
) -> list[BillingCustomerRead]:
    """List local provider customer identifiers, newest first."""
    _user, _own_tenant_id, session = ctx
    query = select(BillingCustomer).order_by(
        BillingCustomer.created_at.desc(),
        BillingCustomer.id.desc(),
    )
    if tenant_id:
        query = query.where(BillingCustomer.tenant_id == tenant_id)
    if provider:
        query = query.where(BillingCustomer.provider == provider)
    result = await session.execute(query)
    return [
        BillingCustomerRead.model_validate(customer)
        for customer in result.scalars().all()
    ]


@router.post("/customers", response_model=BillingCustomerRead, status_code=status.HTTP_201_CREATED)
async def create_customer(body: BillingCustomerCreate, ctx: SuperAdminDep) -> BillingCustomerRead:
    """Register one provider customer ID for a tenant and provider pair."""
    user, _own_tenant_id, session = ctx
    await _get_tenant_or_404(session, body.tenant_id)
    duplicate = await session.execute(
        select(BillingCustomer.id).where(
            BillingCustomer.provider == body.provider,
            BillingCustomer.tenant_id == body.tenant_id,
        )
    )
    if duplicate.scalar_one_or_none() is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="该租户已存在此 provider 客户")
    duplicate_id = await session.execute(
        select(BillingCustomer.id).where(
            BillingCustomer.provider_customer_id == body.provider_customer_id
        )
    )
    if duplicate_id.scalar_one_or_none() is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="provider 客户 ID 已存在")

    customer = BillingCustomer(
        tenant_id=body.tenant_id,
        provider=body.provider,
        provider_customer_id=body.provider_customer_id,
        email=body.email,
    )
    session.add(customer)
    await session.flush()
    await record_security_event(
        session,
        action="billing.customer.create",
        actor_id=user.id,
        tenant_id=customer.tenant_id,
        details={
            "customer_id": customer.id,
            "provider": customer.provider,
        },
    )
    await session.commit()
    await session.refresh(customer)
    return BillingCustomerRead.model_validate(customer)


@router.get("/subscriptions", response_model=list[BillingSubscriptionRead])
async def list_subscriptions(
    ctx: SuperAdminDep,
    tenant_id: str | None = Query(None, min_length=36, max_length=36),
    provider: str | None = Query(None, pattern=r"^(manual|stripe)$"),
    subscription_status: str | None = Query(
        None,
        pattern=r"^(trialing|active|past_due|canceled|incomplete|unpaid|paused)$",
    ),
) -> list[BillingSubscriptionRead]:
    """List locally synchronized provider subscriptions, newest first."""
    _user, _own_tenant_id, session = ctx
    query = select(BillingSubscription).order_by(
        BillingSubscription.created_at.desc(),
        BillingSubscription.id.desc(),
    )
    if tenant_id:
        query = query.where(BillingSubscription.tenant_id == tenant_id)
    if provider:
        query = query.where(BillingSubscription.provider == provider)
    if subscription_status:
        query = query.where(BillingSubscription.status == subscription_status)
    result = await session.execute(query)
    return [
        BillingSubscriptionRead.model_validate(subscription)
        for subscription in result.scalars().all()
    ]


@router.post(
    "/subscriptions",
    response_model=BillingSubscriptionRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_subscription(
    body: BillingSubscriptionCreate,
    ctx: SuperAdminDep,
) -> BillingSubscriptionRead:
    """Record provider subscription state after the provider has accepted it."""
    user, _own_tenant_id, session = ctx
    await _get_tenant_or_404(session, body.tenant_id)
    customer = await _get_customer_or_404(session, body.customer_id)
    if (
        customer.tenant_id != body.tenant_id
        or customer.provider != body.provider
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="账单客户不属于目标租户或 provider",
        )
    duplicate = await session.execute(
        select(BillingSubscription.id).where(
            BillingSubscription.provider_subscription_id == body.provider_subscription_id
        )
    )
    if duplicate.scalar_one_or_none() is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="provider 订阅 ID 已存在")

    subscription = BillingSubscription(
        tenant_id=body.tenant_id,
        provider=body.provider,
        provider_subscription_id=body.provider_subscription_id,
        customer_id=customer.id,
        plan=body.plan,
        provider_price_id=body.provider_price_id,
        status=body.status,
        current_period_start=body.current_period_start,
        current_period_end=body.current_period_end,
        trial_end=body.trial_end,
        cancel_at_period_end=body.cancel_at_period_end,
        canceled_at=body.canceled_at,
    )
    session.add(subscription)
    await session.flush()
    await record_security_event(
        session,
        action="billing.subscription.create",
        actor_id=user.id,
        tenant_id=subscription.tenant_id,
        details={
            "subscription_id": subscription.id,
            "provider": subscription.provider,
            "status": subscription.status,
            "plan": subscription.plan,
        },
    )
    await session.commit()
    await session.refresh(subscription)
    return BillingSubscriptionRead.model_validate(subscription)


@router.patch("/subscriptions/{subscription_id}", response_model=BillingSubscriptionRead)
async def update_subscription(
    subscription_id: str,
    body: BillingSubscriptionUpdate,
    ctx: SuperAdminDep,
) -> BillingSubscriptionRead:
    """Synchronize locally stored provider subscription state."""
    user, _own_tenant_id, session = ctx
    subscription = await _get_subscription_or_404(session, subscription_id)
    previous_status = subscription.status
    if body.status is not None:
        subscription.status = body.status
    if body.plan is not None:
        subscription.plan = body.plan
    if body.provider_price_id is not None:
        subscription.provider_price_id = body.provider_price_id
    if body.current_period_start is not None:
        subscription.current_period_start = body.current_period_start
    if body.current_period_end is not None:
        subscription.current_period_end = body.current_period_end
    if body.trial_end is not None:
        subscription.trial_end = body.trial_end
    if body.cancel_at_period_end is not None:
        subscription.cancel_at_period_end = body.cancel_at_period_end
    if body.canceled_at is not None:
        subscription.canceled_at = body.canceled_at

    if (
        subscription.current_period_start is not None
        and subscription.current_period_end is not None
        and subscription.current_period_start >= subscription.current_period_end
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="账期开始时间必须早于结束时间",
        )
    if subscription.canceled_at is not None and (
        not subscription.cancel_at_period_end and subscription.status != "canceled"
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="取消时间需要取消标记或 canceled 状态",
        )

    await record_security_event(
        session,
        action="billing.subscription.update",
        actor_id=user.id,
        tenant_id=subscription.tenant_id,
        details={
            "subscription_id": subscription.id,
            "previous_status": previous_status,
            "status": subscription.status,
        },
    )
    await session.commit()
    await session.refresh(subscription)
    return BillingSubscriptionRead.model_validate(subscription)


@router.post(
    "/subscriptions/{subscription_id}/reconcile",
    response_model=BillingReconciliationRead,
)
async def reconcile_subscription_snapshot(
    subscription_id: str,
    body: BillingSubscriptionReconcile,
    ctx: SuperAdminDep,
) -> BillingReconciliationRead:
    """Apply one verified, idempotent provider snapshot to local state."""
    user, _own_tenant_id, session = ctx
    try:
        reconciliation = await reconcile_subscription(
            session,
            subscription_id=subscription_id,
            actor_id=user.id,
            snapshot=body,
        )
    except ReconciliationNotFoundError as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ReconciliationConflictError as exc:
        conflict_subscription = await session.get(BillingSubscription, subscription_id)
        await record_security_event(
            session,
            action="billing.subscription.reconcile",
            outcome="blocked",
            actor_id=user.id,
            tenant_id=(
                conflict_subscription.tenant_id if conflict_subscription is not None else None
            ),
            details={
                "subscription_id": subscription_id,
                "observation_id": body.observation_id,
                "reason": str(exc),
            },
        )
        await session.commit()
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    await session.commit()
    await session.refresh(reconciliation.operation)
    subscription = await session.get(BillingSubscription, subscription_id)
    if subscription is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="账单订阅不存在")
    return BillingReconciliationRead(
        subscription=BillingSubscriptionRead.model_validate(subscription),
        outcome=reconciliation.outcome,
        operation=BillingOperationRead.model_validate(reconciliation.operation),
        changes=reconciliation.changes,
    )


@router.get("/operations", response_model=list[BillingOperationRead])
async def list_operations(
    ctx: SuperAdminDep,
    tenant_id: str | None = Query(None, min_length=36, max_length=36),
    provider: str | None = Query(None, pattern=r"^(manual|stripe)$"),
    operation_type: str | None = Query(
        None,
        pattern=r"^(create_customer|create_checkout_session|create_portal_session"
        r"|change_subscription|cancel_subscription|reconcile_subscription)$",
    ),
    operation_status: str | None = Query(
        None,
        pattern=r"^(pending|completed|failed|canceled)$",
    ),
) -> list[BillingOperationRead]:
    """List provider operation state, newest first."""
    _user, _own_tenant_id, session = ctx
    query = select(BillingOperation).order_by(
        BillingOperation.created_at.desc(),
        BillingOperation.id.desc(),
    )
    if tenant_id:
        query = query.where(BillingOperation.tenant_id == tenant_id)
    if provider:
        query = query.where(BillingOperation.provider == provider)
    if operation_type:
        query = query.where(BillingOperation.operation_type == operation_type)
    if operation_status:
        query = query.where(BillingOperation.status == operation_status)
    result = await session.execute(query)
    return [
        BillingOperationRead.model_validate(operation)
        for operation in result.scalars().all()
    ]


@router.post(
    "/operations",
    response_model=BillingOperationRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_operation(
    body: BillingOperationCreate,
    ctx: SuperAdminDep,
) -> BillingOperationRead:
    """Record one idempotent provider operation and its current local state."""
    user, _own_tenant_id, session = ctx
    await _get_tenant_or_404(session, body.tenant_id)
    customer = (
        await _get_customer_or_404(session, body.customer_id) if body.customer_id else None
    )
    subscription = (
        await _get_subscription_or_404(session, body.subscription_id)
        if body.subscription_id
        else None
    )
    await _validate_provider_reference(
        session,
        tenant_id=body.tenant_id,
        provider=body.provider,
        customer=customer,
        subscription=subscription,
    )
    duplicate = await session.execute(
        select(BillingOperation.id).where(
            BillingOperation.idempotency_key == body.idempotency_key
        )
    )
    if duplicate.scalar_one_or_none() is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="幂等键已存在")

    operation = BillingOperation(
        tenant_id=body.tenant_id,
        provider=body.provider,
        operation_type=body.operation_type,
        status=body.status,
        idempotency_key=body.idempotency_key,
        provider_operation_id=body.provider_operation_id,
        customer_id=customer.id if customer else None,
        subscription_id=subscription.id if subscription else None,
        error_code=body.error_code,
        error_message=body.error_message,
        result=body.result,
        completed_at=body.completed_at,
    )
    session.add(operation)
    await session.flush()
    await record_security_event(
        session,
        action="billing.operation.create",
        actor_id=user.id,
        tenant_id=operation.tenant_id,
        details={
            "operation_id": operation.id,
            "operation_type": operation.operation_type,
            "status": operation.status,
            "provider": operation.provider,
        },
    )
    await session.commit()
    await session.refresh(operation)
    return BillingOperationRead.model_validate(operation)


@router.patch("/operations/{operation_id}", response_model=BillingOperationRead)
async def update_operation(
    operation_id: str,
    body: BillingOperationUpdate,
    ctx: SuperAdminDep,
) -> BillingOperationRead:
    """Advance a provider operation using a controlled state machine."""
    user, _own_tenant_id, session = ctx
    operation = await _get_operation_or_404(session, operation_id)
    previous_status = BillingOperationStatus(operation.status)
    if previous_status in _OPERATION_TERMINAL_STATUSES and (
        body.model_dump(exclude_unset=True)
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="终态操作不可变更",
        )
    next_status = BillingOperationStatus(body.status) if body.status else previous_status
    allowed = _ALLOWED_OPERATION_TRANSITIONS.get(previous_status, set())
    if next_status not in allowed and next_status != previous_status:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"操作状态不允许从 {previous_status.value} 变更为 {next_status.value}",
        )
    if next_status == BillingOperationStatus.PENDING and any(
        [body.error_code, body.error_message]
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="pending 操作不能携带错误结果",
        )
    if (
        next_status == BillingOperationStatus.COMPLETED
        and not operation.operation_type == "create_customer"
        and not (body.provider_operation_id or operation.provider_operation_id)
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="完成该操作需要 provider 操作 ID",
        )

    previous_status_value = operation.status
    operation.status = next_status.value
    if body.provider_operation_id is not None:
        operation.provider_operation_id = body.provider_operation_id
    if body.error_code is not None:
        operation.error_code = body.error_code
    if body.error_message is not None:
        operation.error_message = body.error_message
    if body.result is not None:
        operation.result = body.result
    if body.completed_at is not None:
        operation.completed_at = body.completed_at
    elif next_status == BillingOperationStatus.COMPLETED:
        operation.completed_at = datetime.now(UTC)
    if next_status in _OPERATION_TERMINAL_STATUSES and body.error_code is None:
        operation.error_code = None
        operation.error_message = None

    await record_security_event(
        session,
        action="billing.operation.update",
        actor_id=user.id,
        tenant_id=operation.tenant_id,
        details={
            "operation_id": operation.id,
            "previous_status": previous_status_value,
            "status": operation.status,
        },
    )
    await session.commit()
    await session.refresh(operation)
    return BillingOperationRead.model_validate(operation)


__all__ = ["router", "_PROVIDERS"]
