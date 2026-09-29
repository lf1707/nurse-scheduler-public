"""Tenant model — top-level organizational unit (hospital/institution).

Tenants are NOT RLS-scoped (they are the scoping dimension). The `tenants`
table is readable by super-admin only; tenant_id values propagate to all
TenantBase-derived tables.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, String, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Tenant(Base):
    """A single independent institution (hospital, clinic).

    All tenant-scoped tables reference this via `tenant_id` (UUID string).
    Settings stored as JSONB for per-tenant configuration flexibility.
    """

    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(
        String(100), unique=True, nullable=False, index=True
    )
    # Per-tenant settings: timezone, roster_period_days, default_shifts, etc.
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<Tenant {self.slug}>"


async def _tenant_name(
    session: AsyncSession, tenant_id: str | None
) -> str | None:
    """Look up a tenant's display name by id (shared helper).

    Imported by list handlers across API modules so each row gets its own
    tenant name even when a super_admin (tenant_id is null in ctx) lists
    rows from many tenants.
    """
    if not tenant_id:
        return None
    tenant = (
        await session.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one_or_none()
    return tenant.name if tenant else None
