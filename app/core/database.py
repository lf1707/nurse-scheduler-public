"""Async database engine, session factory, and Base model.

Multi-tenant via PostgreSQL Row-Level Security (RLS).
The tenant context is set per-request via `SET LOCAL app.tenant_id = '<uuid>'`.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator
from typing import Annotated

from sqlalchemy import ForeignKey, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import NullPool

from app.core.config import settings


class Base(DeclarativeBase):
    """Declarative base for all models."""


class TenantBase(Base):
    """Abstract base for all tenant-scoped tables.

    Adds `tenant_id` column. RLS policy enforced via Alembic migrations
    ensures queries only return rows where tenant_id matches the session's
    `app.tenant_id` setting.
    """

    __abstract__ = True

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
        comment="Owning tenant UUID (RLS-scoped)",
    )


# ──────────────────────────────────────────────────────────────
# Async engine + session factory
# ──────────────────────────────────────────────────────────────
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.APP_DEBUG and settings.APP_ENV != "production",
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_recycle=settings.DB_POOL_RECYCLE,
    pool_pre_ping=True,
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: yield an async session.

    RLS tenant context must be set by the tenant middleware BEFORE any query
    is executed on this session. See `app.core.tenants.set_tenant_context`.
    """
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# Type alias for dependency injection
SessionDep = Annotated[AsyncSession, "depends"]  # placeholder, overridden in deps.py


@contextlib.asynccontextmanager
async def session_scope(tenant_id: str | None = None) -> AsyncGenerator[AsyncSession, None]:
    """Context manager for non-request-bound sessions (e.g., Celery tasks).

    Args:
        tenant_id: If provided, sets RLS context for the session.
    """
    async with async_session_factory() as session:
        if tenant_id:
            # asyncpg doesn't support parameterized SET — string interpolation.
            # tenant_id is an internal UUID, not user input.
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tenant_id, false)"),
                {"tenant_id": tenant_id},
            )
            await session.execute(text("SET app.is_super = ''"))
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@contextlib.asynccontextmanager
async def task_session_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    """Yield a short-lived session factory isolated to one Celery event loop.

    asyncpg connections are event-loop bound. Periodic tasks intentionally use
    a fresh engine with no pool so every connection is created and disposed in
    the same ``asyncio.run`` invocation without touching the shared API pool.
    """
    engine = create_async_engine(
        settings.DATABASE_URL,
        echo=settings.APP_DEBUG and settings.APP_ENV != "production",
        poolclass=NullPool,
        pool_pre_ping=True,
    )
    session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    try:
        yield session_factory
    finally:
        await engine.dispose()


async def set_tenant_context(session: AsyncSession, tenant_id: str) -> None:
    """Set RLS tenant context on a session.

    Must be called before any tenant-scoped query.
    Uses `set_config` (not SET LOCAL) because SQLAlchemy sessions don't start
    a transaction until the first query executes. This setting persists
    for the session lifetime or until RESET is called.
    Both context values share one SELECT to avoid a second round trip.
    """
    await session.execute(
        text(
            "SELECT set_config('app.tenant_id', :tenant_id, false), "
            "set_config('app.is_super', :is_super, false)"
        ),
        {"tenant_id": tenant_id, "is_super": ""},
    )


async def clear_tenant_context(session: AsyncSession) -> None:
    """Enable super-admin (cross-tenant) RLS bypass on a session.

    Sets app.is_super='1' which the tenant_isolation policies honour.
    Always clears app.tenant_id so the stale value can't leak on pooled
    connections reused by a later request. Both values share one round trip.
    """
    await session.execute(
        text(
            "SELECT set_config('app.tenant_id', '', false), "
            "set_config('app.is_super', '1', false)"
        )
    )
