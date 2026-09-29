"""Tenant context extraction + RLS session setting.

Strategy: tenant_id comes from the JWT (for tenant users) or the URL path
(for super-admin acting on a specific tenant). The middleware sets the
PostgreSQL session variable `app.tenant_id` which RLS policies read.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable, Coroutine
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import (
    async_session_factory,
    clear_tenant_context,
    set_tenant_context,
)
from app.core.impersonation import pending_tenant_admin_can_be_impersonated
from app.core.refresh_tokens import refresh_family_is_active
from app.core.security import TokenError, decode_access_token
from app.models.enums import UserRole
from app.models.tenant import Tenant
from app.models.user import User

__all__ = [
    "AnyUserDep",
    "SchedulerDep",
    "SuperAdminDep",
    "TenantAdminDep",
    "TenantIdDep",
    "get_current_user",
    "get_session_with_user",
    "get_tenant_id_from_path",
    "set_tenant_context",
]


async def _extract_token(request: Request) -> str | None:
    """Extract the bearer token or HttpOnly browser session token."""
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return request.cookies.get("ns_access")
    return auth[7:] or request.cookies.get("ns_access")


async def get_current_user(
    request: Request,
) -> AsyncGenerator[tuple[User, str | None, AsyncSession], None]:
    """Resolve the current user from JWT and return (user, tenant_id, session).

    The session has RLS context already set (or cleared for super_admin).
    This is a dependency that manages its own session lifecycle because the
    RLS setting must happen before any query and within the same transaction.

    Yields a tuple so endpoints get both the user and a ready-to-use session.
    """
    token = await _extract_token(request)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token",
        )

    try:
        payload = decode_access_token(token)
    except TokenError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {e}",
        ) from e

    user_id: str = payload["sub"]
    token_tenant_id: str | None = payload.get("tenant_id")
    token_role: str = payload.get("role", UserRole.VIEWER.value)
    token_version = int(payload.get("ver", 0))

    async with async_session_factory() as session:
        # Resolve users with RLS bypassed, then ensure the token still matches
        # the authoritative role and tenant in the database.
        await clear_tenant_context(session)
        result = await session.execute(
            select(User, Tenant)
            .outerjoin(Tenant, User.tenant_id == Tenant.id)
            .where(User.id == user_id)
        )
        row = result.one_or_none()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found or inactive",
            )
        user, tenant = row
        if not user or (
            not user.is_active
            and not (
                payload.get("impersonated_by")
                and await pending_tenant_admin_can_be_impersonated(session, user)
            )
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found or inactive",
            )
        if user.role.value != token_role or user.tenant_id != token_tenant_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token context no longer matches the user",
            )
        if user.token_version != token_version:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session was revoked",
            )

        mfa_enrollment_path = request.url.path in {
            "/api/v1/auth/me",
            "/api/v1/auth/me/mfa/setup",
            "/api/v1/auth/me/mfa/confirm",
            "/api/v1/auth/logout",
            "/api/v1/auth/refresh",
        } or request.url.path.startswith("/pages/")
        if (
            user.role == UserRole.SUPER_ADMIN
            and not user.mfa_enabled
            and not mfa_enrollment_path
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"reason": "mfa_enrollment_required"},
            )
        family_id = payload.get("family_id")
        if family_id and not await refresh_family_is_active(session, user.id, str(family_id)):
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                detail="Session was revoked",
            )
        impersonator_id = payload.get("impersonated_by")
        if impersonator_id:
            impersonator = (
                await session.execute(select(User).where(User.id == impersonator_id))
            ).scalar_one_or_none()
            if not impersonator or not impersonator.is_active:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Impersonator is no longer active",
                )
            if impersonator.role != UserRole.SUPER_ADMIN:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Impersonator is no longer a super admin",
                )
        if user.tenant_id and (not tenant or not tenant.is_active):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Tenant inactive",
            )

        if user.role != UserRole.SUPER_ADMIN:
            await set_tenant_context(session, user.tenant_id)

        yield user, user.tenant_id, session
        await session.commit()


async def get_session_with_user(
    request: Request,
) -> AsyncGenerator[tuple[User, str | None, AsyncSession], None]:
    """Dependency yielding (user, tenant_id, session) with RLS set."""
    async for item in get_current_user(request):
        yield item


# ──────────────────────────────────────────────────────────────
# Role guards
# ──────────────────────────────────────────────────────────────
def require_role(
    *allowed_roles: UserRole,
) -> Callable[..., Coroutine[object, object, tuple[User, str | None, AsyncSession]]]:
    """Dependency factory: require the user to have one of the allowed roles.

    Super-admin bypasses all role checks.
    """
    allowed_values = {r.value for r in allowed_roles}

    async def _check(
        ctx: tuple[User, str | None, AsyncSession] = Depends(get_session_with_user),
    ) -> tuple[User, str | None, AsyncSession]:
        user, tenant_id, session = ctx
        if user.role == UserRole.SUPER_ADMIN:
            return ctx
        if user.role.value not in allowed_values:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role {user.role.value} not permitted (need one of {allowed_values})",
            )
        return ctx

    return _check


# Convenience dependencies
SuperAdminDep = Annotated[
    tuple[User, str | None, AsyncSession],
    Depends(require_role(UserRole.SUPER_ADMIN)),
]
TenantAdminDep = Annotated[
    tuple[User, str | None, AsyncSession],
    Depends(require_role(UserRole.TENANT_ADMIN)),
]
SchedulerDep = Annotated[
    tuple[User, str | None, AsyncSession],
    Depends(require_role(UserRole.TENANT_ADMIN, UserRole.SCHEDULER)),
]
AnyUserDep = Annotated[
    tuple[User, str | None, AsyncSession],
    Depends(require_role(
        UserRole.SUPER_ADMIN,
        UserRole.TENANT_ADMIN,
        UserRole.SCHEDULER,
        UserRole.NURSE,
        UserRole.VIEWER,
    )),
]


async def get_tenant_id_from_path(
    request: Request,
    ctx: tuple[User, str | None, AsyncSession] = Depends(get_session_with_user),
) -> str:
    """Extract tenant_id from path param /tenants/{tenant_id}/...

    For super-admin operating on a different tenant, this switches the RLS
    context to the path tenant. For tenant users, it must match their own tenant.
    """
    user, own_tenant_id, session = ctx
    path_tenant_id_value = request.path_params.get("tenant_id")
    if not isinstance(path_tenant_id_value, str):
        path_tenant_id_value = ""
    path_tenant_id = path_tenant_id_value

    if not path_tenant_id:
        if not own_tenant_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="tenant_id required",
            )
        return own_tenant_id

    # Super-admin can access any tenant — re-set RLS to path tenant
    if user.role == UserRole.SUPER_ADMIN:
        await set_tenant_context(session, path_tenant_id)
        return path_tenant_id

    # Tenant user: must match own tenant
    if path_tenant_id != own_tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot access other tenants",
        )
    return path_tenant_id


# Aliased for cleaner endpoint signatures
TenantIdDep = Annotated[str, Depends(get_tenant_id_from_path)]
