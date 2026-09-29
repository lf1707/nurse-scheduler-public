"""User model — system users with system-wide role.

Users belong to a tenant (except super_admin). User also acts as a Nurse
when they have the NURSE role and have a linked Nurse record.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.enums import UserRole

if TYPE_CHECKING:
    from app.models.nurse import Nurse


class User(Base):
    """Authenticated user (admin, scheduler, nurse, viewer, super_admin)."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    # super_admin has NULL tenant_id (cross-tenant)
    tenant_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    role: Mapped[UserRole] = mapped_column(nullable=False, default=UserRole.VIEWER)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_superuser: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    token_version: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
    )
    # TOTP MFA state (nullable = not enrolled; encrypted secret, not plaintext)
    mfa_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mfa_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    mfa_recovery_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Optional: link to Nurse record (for nurses who log in)
    nurse_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("nurses.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    nurse: Mapped[Nurse | None] = relationship(
        "Nurse", back_populates="user", foreign_keys=[nurse_id]
    )

    def __repr__(self) -> str:
        return f"<User {self.email} ({self.role.value})>"


# Email is the global login identifier and is immutable after creation.
