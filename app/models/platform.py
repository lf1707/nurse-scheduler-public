"""Platform-wide settings editable by super administrators."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class PlatformSetting(Base):
    """A persistent platform configuration key/value."""

    __tablename__ = "platform_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[object] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class SecurityAuditEvent(Base):
    """Append-only security and administrator action event."""

    __tablename__ = "security_audit_events"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    # created_at is part of the composite PK because this table is
    # range-partitioned by created_at; PostgreSQL requires the partition
    # key in the primary key.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        primary_key=True,
        index=True,
    )
    action: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(36), index=True)
    actor_tenant_id: Mapped[str | None] = mapped_column(String(36))
    target_user_id: Mapped[str | None] = mapped_column(String(36), index=True)
    tenant_id: Mapped[str | None] = mapped_column(String(36), index=True)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    details: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    # created_at moved above — composite PK with id for partitioned table.


class AuditExportWatermark(Base):
    """Watermark for incremental audit-event exports."""

    __tablename__ = "audit_export_watermarks"

    id: Mapped[int] = mapped_column(primary_key=True)
    last_exported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_export_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    artifact_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    manifest_digest: Mapped[str | None] = mapped_column(String(128), nullable=True)
    event_count: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class AuditLegalHold(Base):
    """A retention hold on one monthly security-audit partition."""

    __tablename__ = "audit_legal_holds"

    id: Mapped[int] = mapped_column(primary_key=True)
    partition_name: Mapped[str] = mapped_column(String(80), unique=True)
    reason: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
