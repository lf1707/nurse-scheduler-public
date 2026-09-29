"""Periodic tasks for tenant application and subscription lifecycle."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy import select

from app.core.audit import record_security_event
from app.core.database import clear_tenant_context, task_session_factory
from app.models.enums import SubscriptionPlan, TenantApplicationStatus
from app.models.subscription import Subscription
from app.models.tenant_application import TenantApplication
from app.tasks.celery_app import celery_task


async def _expire_applications() -> int:
    """Expire unverified applications and append an audit event per row."""
    now = datetime.now(UTC)
    async with task_session_factory() as session_factory, session_factory() as session:
        await clear_tenant_context(session)
        rows = (
            await session.execute(
                select(TenantApplication)
                .where(
                    TenantApplication.status
                    == TenantApplicationStatus.PENDING_EMAIL_VERIFICATION,
                    TenantApplication.verification_expires_at < now,
                )
                .with_for_update(skip_locked=True)
            )
        ).scalars().all()
        for application in rows:
            application.status = TenantApplicationStatus.EXPIRED
            application.verification_token_hash = None
            await record_security_event(
                session,
                action="tenant_application.expire",
                details={
                    "application_id": application.id,
                    "state": TenantApplicationStatus.EXPIRED.value,
                },
            )
        await session.commit()
        return len(rows)


@celery_task(name="tenant_application.expire_unverified")
def expire_unverified_tenant_applications() -> int:
    return asyncio.run(_expire_applications())


async def _expire_subscriptions() -> int:
    """Downgrade expired paid/trial subscriptions to Free and audit each row."""
    now = datetime.now(UTC)
    async with task_session_factory() as session_factory, session_factory() as session:
        await clear_tenant_context(session)
        rows = (
            await session.execute(
                select(Subscription)
                .where(
                    Subscription.plan.in_(
                        [SubscriptionPlan.PRO.value, SubscriptionPlan.MAX.value]
                    ),
                    Subscription.is_canceled == False,  # noqa: E712
                    Subscription.ends_at.is_not(None),
                    Subscription.ends_at < now,
                )
                .with_for_update(skip_locked=True)
            )
        ).scalars().all()
        for subscription in rows:
            previous_plan = subscription.plan
            subscription.plan = SubscriptionPlan.FREE.value
            subscription.ends_at = None
            await record_security_event(
                session,
                action="subscription.expire",
                details={
                    "subscription_id": subscription.id,
                    "tenant_id": subscription.tenant_id,
                    "previous_plan": previous_plan,
                    "new_plan": SubscriptionPlan.FREE.value,
                },
            )
        await session.commit()
        return len(rows)


@celery_task(name="subscription.expire_trials")
def expire_trial_subscriptions() -> int:
    return asyncio.run(_expire_subscriptions())
