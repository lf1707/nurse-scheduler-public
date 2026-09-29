"""Shared authorization rules for audited super-admin impersonation."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import UserRole
from app.models.subscription import Subscription
from app.models.tenant import Tenant
from app.models.user import User


async def pending_tenant_admin_can_be_impersonated(
    session: AsyncSession,
    user: User,
    tenant: Tenant | None = None,
) -> bool:
    """Allow support access to a pending admin only while the tenant is live."""
    if user.role != UserRole.TENANT_ADMIN:
        return False

    if tenant is None:
        if not user.tenant_id:
            return False
        tenant = await session.get(Tenant, user.tenant_id)
    if not tenant or not tenant.is_active:
        return False

    subscription = (
        await session.execute(
            select(Subscription).where(Subscription.tenant_id == tenant.id)
        )
    ).scalar_one_or_none()
    return bool(subscription and subscription.is_active)
