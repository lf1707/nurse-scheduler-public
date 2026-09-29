"""Paid-invoice fallback for subscription entitlement checks."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing import BillingInvoice
from app.models.enums import BillingInvoiceStatus


async def invoice_paid_through(
    session: AsyncSession,
    tenant_id: str,
) -> datetime | None:
    """Return the latest paid invoice period end for a tenant."""

    result = await session.execute(
        select(BillingInvoice.period_end)
        .where(
            BillingInvoice.tenant_id == tenant_id,
            BillingInvoice.status == BillingInvoiceStatus.PAID.value,
            BillingInvoice.period_end.is_not(None),
        )
        .order_by(BillingInvoice.period_end.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()
