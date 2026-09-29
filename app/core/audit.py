"""Persistent security audit event helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.core.rate_limit import client_ip
from app.models.platform import SecurityAuditEvent


async def record_security_event(
    session: AsyncSession,
    *,
    action: str,
    outcome: str = "success",
    actor_id: str | None = None,
    actor_tenant_id: str | None = None,
    target_user_id: str | None = None,
    tenant_id: str | None = None,
    details: dict[str, Any] | None = None,
    request: Request | None = None,
    commit: bool = False,
) -> SecurityAuditEvent:
    """Append a security event in the current database transaction.

    Audit writes deliberately remain in the caller's transaction. A failed
    write must fail the security-sensitive action instead of silently losing
    its accountability record.
    """

    event = SecurityAuditEvent(
        action=action,
        outcome=outcome,
        actor_id=actor_id,
        actor_tenant_id=actor_tenant_id,
        target_user_id=target_user_id,
        tenant_id=tenant_id,
        ip_address=client_ip(request) if request is not None else None,
        user_agent=(
            request.headers.get("User-Agent", "")[:512] or None
            if request is not None
            else None
        ),
        details=details or {},
        # Supplying the timestamp avoids INSERT ... RETURNING, which PostgreSQL
        # additionally checks against SELECT policies under force RLS.
        created_at=datetime.now(UTC),
    )
    session.add(event)
    if commit:
        await session.commit()
    return event
