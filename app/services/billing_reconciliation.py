"""Provider-neutral billing snapshot reconciliation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import record_security_event
from app.models.billing import BillingCustomer, BillingOperation, BillingSubscription
from app.models.enums import BillingOperationStatus, BillingOperationType
from app.schemas import BillingSubscriptionReconcile

_SNAPSHOT_FIELDS = (
    "status",
    "plan",
    "provider_price_id",
    "current_period_start",
    "current_period_end",
    "trial_end",
    "cancel_at_period_end",
    "canceled_at",
)


class ReconciliationConflictError(Exception):
    """A snapshot references a different provider or tenant relationship."""


class ReconciliationNotFoundError(Exception):
    """The local subscription or linked customer does not exist."""


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    outcome: str
    operation: BillingOperation
    changes: list[dict[str, str | None]]


def _utc_datetime(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _operation_key(subscription_id: str, observation_id: str) -> str:
    observation_hash = sha256(observation_id.encode()).hexdigest()
    return f"reconcile:{subscription_id}:{observation_hash}"


def _snapshot_fingerprint(
    subscription: BillingSubscription,
    snapshot: BillingSubscriptionReconcile,
) -> str:
    """Hash the exact provider snapshot accepted for an observation ID."""

    fingerprint_payload = {
        "cancel_at_period_end": snapshot.cancel_at_period_end,
        "canceled_at": (
            _utc_datetime(snapshot.canceled_at).isoformat()
            if snapshot.canceled_at is not None
            else None
        ),
        "current_period_end": (
            _utc_datetime(snapshot.current_period_end).isoformat()
            if snapshot.current_period_end is not None
            else None
        ),
        "current_period_start": (
            _utc_datetime(snapshot.current_period_start).isoformat()
            if snapshot.current_period_start is not None
            else None
        ),
        "observed_at": _utc_datetime(snapshot.observed_at).isoformat(),
        "plan": snapshot.plan,
        "provider_customer_id": snapshot.provider_customer_id,
        "provider_subscription_id": snapshot.provider_subscription_id,
        "provider_price_id": snapshot.provider_price_id,
        "status": snapshot.status,
        "subscription_provider": subscription.provider,
        "trial_end": (
            _utc_datetime(snapshot.trial_end).isoformat()
            if snapshot.trial_end is not None
            else None
        ),
    }
    canonical = json.dumps(
        fingerprint_payload,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode()).hexdigest()


def snapshot_changes(
    subscription: BillingSubscription,
    snapshot: BillingSubscriptionReconcile,
) -> list[dict[str, str | None]]:
    """Return field-level differences without mutating either input."""

    changes: list[dict[str, str | None]] = []
    for field_name in _SNAPSHOT_FIELDS:
        before = getattr(subscription, field_name)
        after = getattr(snapshot, field_name)
        if isinstance(before, datetime) and isinstance(after, datetime):
            before = _utc_datetime(before)
            after = _utc_datetime(after)
        if before != after:
            changes.append(
                {
                    "field": field_name,
                    "before": str(before) if before is not None else None,
                    "after": str(after) if after is not None else None,
                }
            )
    return changes


async def _latest_observed_at(
    session: AsyncSession,
    subscription: BillingSubscription,
) -> datetime | None:
    result = await session.execute(
        select(BillingOperation)
        .where(
            BillingOperation.tenant_id == subscription.tenant_id,
            BillingOperation.subscription_id == subscription.id,
            BillingOperation.operation_type == (
                BillingOperationType.RECONCILE_SUBSCRIPTION.value
            ),
            BillingOperation.status == BillingOperationStatus.COMPLETED.value,
        )
        .order_by(BillingOperation.created_at.desc(), BillingOperation.id.desc())
        .limit(50)
    )
    observed_at_values: list[datetime] = []
    for operation in result.scalars().all():
        observed_at = operation.result.get("observed_at") if operation.result else None
        if isinstance(observed_at, str):
            observed_at_values.append(datetime.fromisoformat(observed_at))
    return max(observed_at_values, default=None)


async def _existing_operation(
    session: AsyncSession,
    operation_key: str,
) -> BillingOperation | None:
    result = await session.execute(
        select(BillingOperation).where(BillingOperation.idempotency_key == operation_key)
    )
    return result.scalar_one_or_none()


async def reconcile_subscription(
    session: AsyncSession,
    *,
    subscription_id: str,
    actor_id: str,
    snapshot: BillingSubscriptionReconcile,
) -> ReconciliationResult:
    """Apply one verified provider snapshot and audit the outcome."""

    subscription_result = await session.execute(
        select(BillingSubscription).where(BillingSubscription.id == subscription_id).with_for_update()
    )
    subscription = subscription_result.scalar_one_or_none()
    if subscription is None:
        raise ReconciliationNotFoundError("账单订阅不存在")

    customer = await session.get(BillingCustomer, subscription.customer_id)
    if customer is None:
        raise ReconciliationNotFoundError("账单客户不存在")

    if (
        snapshot.provider_subscription_id != subscription.provider_subscription_id
        or snapshot.provider_customer_id != customer.provider_customer_id
    ):
        raise ReconciliationConflictError("provider 订阅或客户标识不匹配")

    operation_key = _operation_key(subscription.id, snapshot.observation_id)
    existing_operation = await _existing_operation(session, operation_key)
    if existing_operation is not None:
        recorded_fingerprint = (
            existing_operation.result.get("snapshot_fingerprint")
            if existing_operation.result
            else None
        )
        if recorded_fingerprint != _snapshot_fingerprint(subscription, snapshot):
            raise ReconciliationConflictError("observation_id 已用于不同的 provider 快照")
        changes = cast_snapshot_changes(existing_operation)
        outcome = "updated" if changes else "in_sync"
        await record_security_event(
            session,
            action="billing.subscription.reconcile",
            outcome="ignored",
            actor_id=actor_id,
            tenant_id=subscription.tenant_id,
            details={
                "subscription_id": subscription.id,
                "observation_id": snapshot.observation_id,
                "operation_id": existing_operation.id,
            },
        )
        return ReconciliationResult(outcome, existing_operation, changes)

    latest_observed_at = await _latest_observed_at(session, subscription)
    if latest_observed_at is not None and _utc_datetime(snapshot.observed_at) < latest_observed_at:
        raise ReconciliationConflictError("快照早于最近一次 reconciliation")

    changes = snapshot_changes(subscription, snapshot)
    operation_result = {
        "observed_at": _utc_datetime(snapshot.observed_at).isoformat(),
        "observation_id": snapshot.observation_id,
        "snapshot_fingerprint": _snapshot_fingerprint(subscription, snapshot),
        "changes": changes,
    }
    previous_status = subscription.status
    if changes:
        for field_name in _SNAPSHOT_FIELDS:
            setattr(subscription, field_name, getattr(snapshot, field_name))

    operation = BillingOperation(
        tenant_id=subscription.tenant_id,
        provider=subscription.provider,
        operation_type=BillingOperationType.RECONCILE_SUBSCRIPTION.value,
        status=BillingOperationStatus.COMPLETED.value,
        idempotency_key=operation_key,
        provider_operation_id=snapshot.observation_id[:100],
        customer_id=customer.id,
        subscription_id=subscription.id,
        result=operation_result,
        completed_at=datetime.now(UTC),
    )
    session.add(operation)
    await record_security_event(
        session,
        action="billing.subscription.reconcile",
        actor_id=actor_id,
        tenant_id=subscription.tenant_id,
        details={
            "subscription_id": subscription.id,
            "observation_id": snapshot.observation_id,
            "outcome": "updated" if changes else "in_sync",
            "previous_status": previous_status,
            "status": subscription.status,
        },
    )
    return ReconciliationResult(
        "updated" if changes else "in_sync",
        operation,
        changes,
    )


def cast_snapshot_changes(operation: BillingOperation) -> list[dict[str, str | None]]:
    """Return changes recorded in an operation result, tolerating bad legacy data."""

    result: dict[str, Any] | None = operation.result
    raw_changes = result.get("changes") if result else None
    if not isinstance(raw_changes, list):
        return []
    return [change for change in raw_changes if isinstance(change, dict)]
