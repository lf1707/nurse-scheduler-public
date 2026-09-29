"""Billing records that reconcile provider state with tenant entitlements."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import TenantBase
from app.models.enums import (
    BillingInvoiceStatus,
    BillingOperationStatus,
    BillingProvider,
    BillingSubscriptionStatus,
)


class BillingInvoice(TenantBase):
    """A tenant invoice without storing any payment-method data."""

    __tablename__ = "billing_invoices"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    number: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    provider: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=BillingProvider.MANUAL.value,
        server_default=BillingProvider.MANUAL.value,
        index=True,
    )
    provider_invoice_id: Mapped[str | None] = mapped_column(
        String(100),
        unique=True,
        nullable=True,
    )
    amount_minor: Mapped[int] = mapped_column(nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=BillingInvoiceStatus.ISSUED.value,
        server_default=BillingInvoiceStatus.ISSUED.value,
        index=True,
    )
    period_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    paid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        onupdate=func.now(),
    )


class BillingCustomer(TenantBase):
    """A provider customer identifier for one tenant and provider."""

    __tablename__ = "billing_customers"
    __table_args__ = (
        UniqueConstraint("provider", "tenant_id", name="uq_billing_customer_provider_tenant"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    provider: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        index=True,
    )
    provider_customer_id: Mapped[str] = mapped_column(
        String(100),
        unique=True,
        nullable=False,
    )
    email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        onupdate=func.now(),
    )


class BillingSubscription(TenantBase):
    """Provider subscription state; never an entitlement authority by itself."""

    __tablename__ = "billing_subscriptions"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    provider: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        index=True,
    )
    provider_subscription_id: Mapped[str] = mapped_column(
        String(100),
        unique=True,
        nullable=False,
    )
    customer_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("billing_customers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    plan: Mapped[str | None] = mapped_column(String(30), nullable=True)
    provider_price_id: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=BillingSubscriptionStatus.INCOMPLETE.value,
        server_default=BillingSubscriptionStatus.INCOMPLETE.value,
        index=True,
    )
    current_period_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    current_period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    trial_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_at_period_end: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        onupdate=func.now(),
    )


class BillingOperation(TenantBase):
    """Idempotency state for a provider billing operation."""

    __tablename__ = "billing_operations"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    provider: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    operation_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=BillingOperationStatus.PENDING.value,
        server_default=BillingOperationStatus.PENDING.value,
        index=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    provider_operation_id: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )
    customer_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("billing_customers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    subscription_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("billing_subscriptions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        onupdate=func.now(),
    )
