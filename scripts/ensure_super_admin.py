"""Idempotently ensure the platform super-admin exists.

The initial migration (0001) seeds a super-admin on a fresh DB, but for an
existing DB (e.g. you rotated the password via env and want to update it, or
the seed row was deleted) this script upserts it safely:

    uv run python scripts/ensure_super_admin.py

Reads SUPER_ADMIN_EMAIL / SUPER_ADMIN_PASSWORD from the app settings (env).
On existing email it updates the password hash; otherwise it inserts.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select, text

from app.core.database import async_session_factory
from app.core.security import hash_password
from app.models.user import User


async def main() -> None:
    from app.core.config import settings

    email = settings.SUPER_ADMIN_EMAIL
    password = settings.SUPER_ADMIN_PASSWORD
    if not password or password == "changeme123":
        print(
            f"[ensure_super_admin] WARNING: password is the default "
            f"({password!r}). Set SUPER_ADMIN_PASSWORD in .env before running "
            f"in production."
        )

    hashed = hash_password(password)
    async with async_session_factory() as session:
        # Super-admin rows have NULL tenant_id; the RLS bypass GUC isn't set
        # here, but this bootstrap runs inside the container as the app role.
        # We need is_super to read/write the users table (RLS-enforced).
        await session.execute(text("SET app.is_super = '1'"))
        await session.execute(text("SET app.tenant_id = ''"))
        existing = (
            await session.execute(select(User).where(User.email == email))
        ).scalar_one_or_none()
        if existing:
            existing.hashed_password = hashed
            existing.role = existing.role  # unchanged
            print(f"[ensure_super_admin] updated password for existing {email}")
        else:
            session.add(
                User(
                    email=email,
                    hashed_password=hashed,
                    first_name="Super",
                    last_name="Admin",
                    role="super_admin",  # type: ignore[arg-type]
                    is_active=True,
                    is_superuser=True,
                )
            )
            print(f"[ensure_super_admin] created super-admin {email}")
        await session.commit()


if __name__ == "__main__":
    asyncio.run(main())
