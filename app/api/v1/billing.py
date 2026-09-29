"""Super-admin manual billing records."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import SuperAdminDep
from app.core.audit import record_security_event
from app.models.billing import BillingInvoice
from app.models.enums import BillingInvoiceStatus
from app.models.tenant import Tenant
from app.schemas import BillingInvoiceCreate, BillingInvoiceRead, BillingInvoiceUpdate

router = APIRouter(prefix="/billing/invoices", tags=["billing"])

_ALLOWED_TRANSITIONS = {
    BillingInvoiceStatus.ISSUED: {
        BillingInvoiceStatus.PAID,
        BillingInvoiceStatus.VOID,
    },
}


async def _get_invoice_or_404(session: AsyncSession, invoice_id: str) -> BillingInvoice:
    invoice = await session.get(BillingInvoice, invoice_id)
    if invoice is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="账单不存在")
    return invoice


async def _get_tenant_or_404(session: AsyncSession, tenant_id: str) -> Tenant:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="租户不存在")
    return tenant


@router.get("", response_model=list[BillingInvoiceRead])
async def list_invoices(
    ctx: SuperAdminDep,
    tenant_id: str | None = Query(None, min_length=36, max_length=36),
    provider: str | None = Query(None, pattern=r"^(manual|stripe)$"),
    invoice_status: str | None = Query(None, pattern=r"^(issued|paid|void)$"),
) -> list[BillingInvoiceRead]:
    """List invoices, newest first (super admin only)."""
    _user, _tenant_id, session = ctx
    query = select(BillingInvoice).order_by(
        BillingInvoice.issued_at.desc(),
        BillingInvoice.number.desc(),
    )
    if tenant_id:
        query = query.where(BillingInvoice.tenant_id == tenant_id)
    if provider:
        query = query.where(BillingInvoice.provider == provider)
    if invoice_status:
        query = query.where(BillingInvoice.status == invoice_status)
    result = await session.execute(query)
    return [
        BillingInvoiceRead.model_validate(invoice)
        for invoice in result.scalars().all()
    ]


@router.post("", response_model=BillingInvoiceRead, status_code=status.HTTP_201_CREATED)
async def create_invoice(
    body: BillingInvoiceCreate,
    ctx: SuperAdminDep,
) -> BillingInvoiceRead:
    """Record an issued or already-paid invoice (super admin only)."""
    user, _own_tenant_id, session = ctx
    await _get_tenant_or_404(session, body.tenant_id)
    duplicate = await session.execute(
        select(BillingInvoice.id).where(BillingInvoice.number == body.number)
    )
    if duplicate.scalar_one_or_none() is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="账单编号已存在")

    issued_at = body.issued_at or datetime.now(UTC)
    invoice = BillingInvoice(
        tenant_id=body.tenant_id,
        number=body.number,
        provider=body.provider,
        provider_invoice_id=body.provider_invoice_id,
        amount_minor=body.amount_minor,
        currency=body.currency.upper(),
        status=body.status,
        period_start=body.period_start,
        period_end=body.period_end,
        issued_at=issued_at,
        due_at=body.due_at,
        paid_at=body.paid_at,
        notes=body.notes,
    )
    session.add(invoice)
    await session.flush()
    await record_security_event(
        session,
        action="billing.invoice.create",
        actor_id=user.id,
        tenant_id=invoice.tenant_id,
        details={
            "invoice_id": invoice.id,
            "number": invoice.number,
            "provider": invoice.provider,
            "amount_minor": invoice.amount_minor,
            "currency": invoice.currency,
            "status": invoice.status,
        },
    )
    await session.commit()
    await session.refresh(invoice)
    return BillingInvoiceRead.model_validate(invoice)


@router.patch("/{invoice_id}", response_model=BillingInvoiceRead)
async def update_invoice(
    invoice_id: str,
    body: BillingInvoiceUpdate,
    ctx: SuperAdminDep,
) -> BillingInvoiceRead:
    """Advance lifecycle state or edit due date/notes (super admin only)."""
    user, _own_tenant_id, session = ctx
    invoice = await _get_invoice_or_404(session, invoice_id)
    previous_status = invoice.status
    next_status = (
        BillingInvoiceStatus(body.status)
        if body.status and body.status != previous_status
        else None
    )
    if next_status == BillingInvoiceStatus.PAID:
        effective_period_end = body.period_end or invoice.period_end
        if effective_period_end is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="标记付款前必须设置账期结束时间",
            )
    if invoice.status != BillingInvoiceStatus.ISSUED and (
        body.period_start is not None or body.period_end is not None
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="已付款或已作废账单不能修改账期",
        )
    if body.period_start is not None:
        invoice.period_start = body.period_start
    if body.period_end is not None:
        invoice.period_end = body.period_end
    if (
        invoice.period_start is not None
        and invoice.period_end is not None
        and invoice.period_start >= invoice.period_end
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="账期开始时间必须早于结束时间",
        )
    if next_status is not None:
        allowed = _ALLOWED_TRANSITIONS.get(BillingInvoiceStatus(previous_status), set())
        if next_status not in allowed:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail=f"账单状态不允许从 {previous_status} 变更为 {body.status}",
            )
        invoice.status = next_status.value
        invoice.paid_at = datetime.now(UTC) if next_status == BillingInvoiceStatus.PAID else None
    if body.due_at is not None:
        if invoice.issued_at and body.due_at < invoice.issued_at:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="到期时间不能早于开票时间",
            )
        invoice.due_at = body.due_at
    if body.notes is not None:
        invoice.notes = body.notes

    await record_security_event(
        session,
        action="billing.invoice.update",
        actor_id=user.id,
        tenant_id=invoice.tenant_id,
        details={
            "invoice_id": invoice.id,
            "number": invoice.number,
            "previous_status": previous_status,
            "status": invoice.status,
        },
    )
    await session.commit()
    await session.refresh(invoice)
    return BillingInvoiceRead.model_validate(invoice)
