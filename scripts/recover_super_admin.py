"""Controlled break-glass recovery for the configured platform super admin."""

from __future__ import annotations

import asyncio
import os

from sqlalchemy import select, text

from app.core.audit import record_security_event
from app.core.config import settings
from app.core.database import async_session_factory
from app.core.refresh_tokens import revoke_refresh_tokens_for_user
from app.core.security import hash_password
from app.models.enums import UserRole
from app.models.user import User

CONFIRMATION = "I-UNDERSTAND-THIS-LOCKS-OUT-EXISTING-SESSIONS"


async def recover_super_admin() -> None:
    """Reset password/MFA for exactly the configured super-admin identity."""
    email = settings.SUPER_ADMIN_EMAIL
    password = settings.SUPER_ADMIN_PASSWORD
    ticket = os.environ.get("SUPER_ADMIN_RECOVERY_TICKET", "").strip()
    confirmation = os.environ.get("SUPER_ADMIN_RECOVERY_CONFIRM", "")

    if confirmation != CONFIRMATION:
        raise SystemExit(
            f"Refusing recovery: set SUPER_ADMIN_RECOVERY_CONFIRM={CONFIRMATION}"
        )
    if len(ticket) < 6:
        raise SystemExit("Refusing recovery: SUPER_ADMIN_RECOVERY_TICKET is required")
    if not email or not password or password == "changeme123" or len(password) < 12:
        raise SystemExit("Refusing recovery: SUPER_ADMIN_EMAIL/PASSWORD are not safe")

    async with async_session_factory() as session:
        await session.execute(text("SET app.is_super = '1'"))
        await session.execute(text("SET app.tenant_id = ''"))
        target = (
            await session.execute(
                select(User).where(
                    User.email == email,
                    User.is_superuser.is_(True),
                    User.role == UserRole.SUPER_ADMIN,
                )
            )
        ).scalar_one_or_none()
        if target is None:
            raise SystemExit(f"No super admin found for {email}")

        previous_version = target.token_version
        target.hashed_password = hash_password(password)
        target.is_active = True
        target.mfa_enabled = False
        target.mfa_secret = None
        target.mfa_recovery_hash = None
        target.token_version += 1
        await revoke_refresh_tokens_for_user(session, target.id)
        await record_security_event(
            session,
            action="auth.super_admin.break_glass_recovery",
            actor_id=target.id,
            target_user_id=target.id,
            details={
                "ticket": ticket,
                "previous_token_version": previous_version,
                "mfa_reset": True,
            },
        )
        await session.commit()
        print(f"Recovered super admin {email}; previous sessions are revoked")


if __name__ == "__main__":
    asyncio.run(recover_super_admin())
