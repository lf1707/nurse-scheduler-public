"""Subscription model — one per tenant, gates schedule generation.

A tenant can generate schedules only while its subscription is active:
not canceled AND (ends_at is null OR now < ends_at). Managed by super_admin.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import TenantBase
from app.models.enums import SubscriptionType


class Subscription(TenantBase):
    """Tenant subscription (one row per tenant, enforced by unique index)."""

    __tablename__ = "subscriptions"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    # Override TenantBase's plain indexed column with a unique one-to-tenant FK.
    tenant_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    plan: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default="free",
    )
    subscription_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=SubscriptionType.MANUAL.value,
        server_default=SubscriptionType.MANUAL.value,
    )
    # Number of purchased nurse-seat packs; each pack extends the plan limit.
    seat_packs: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    starts_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # null → perpetual subscription
    ends_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_canceled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    @property
    def is_active(self) -> bool:
        """Active = not canceled and (no end date or not yet reached)."""
        if self.is_canceled:
            return False
        if self.ends_at is None:
            return True
        return datetime.now(tz=self.ends_at.tzinfo) < self.ends_at

    def __repr__(self) -> str:
        return f"<Subscription tenant={self.tenant_id} plan={self.plan} active={self.is_active}>"
