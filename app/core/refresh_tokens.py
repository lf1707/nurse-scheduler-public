"""Helpers for the persistent refresh-token lifecycle."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_refresh_token, decode_token
from app.models.refresh_token import RefreshToken
from app.models.user import User


@dataclass(frozen=True)
class IssuedRefreshToken:
    token: str
    token_id: str
    family_id: str


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_refresh_token(
    session: AsyncSession,
    user: User,
    *,
    impersonated_by_user_id: str | None = None,
    expires_minutes: int | None = None,
    family_id: str | None = None,
) -> IssuedRefreshToken:
    family = family_id or str(uuid.uuid4())
    claims: dict[str, str] = {"family_id": family}
    if impersonated_by_user_id:
        claims["impersonated_by"] = impersonated_by_user_id

    token = create_refresh_token(
        subject=user.id,
        token_version=user.token_version,
        expires_minutes=expires_minutes,
        extra_claims=claims,
    )
    payload = decode_token(token)
    token_id = str(payload["jti"])
    session.add(
        RefreshToken(
            id=token_id,
            user_id=user.id,
            tenant_id=user.tenant_id,
            family_id=family,
            token_hash=hash_refresh_token(token),
            token_version=user.token_version,
            impersonated_by_user_id=impersonated_by_user_id,
            expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
        )
    )
    return IssuedRefreshToken(token=token, token_id=token_id, family_id=family)


async def claim_refresh_token(
    session: AsyncSession, token: str, token_id: str
) -> RefreshToken | None:
    token_hash = hash_refresh_token(token)
    result = await session.execute(
        update(RefreshToken)
        .where(
            RefreshToken.id == token_id,
            RefreshToken.token_hash == token_hash,
            RefreshToken.revoked_at.is_(None),
        )
        .values(
            revoked_at=datetime.now(UTC),
            last_used_at=datetime.now(UTC),
        )
        .returning(RefreshToken)
    )
    return result.scalar_one_or_none()


async def revoke_refresh_family(session: AsyncSession, family_id: str) -> None:
    await session.execute(
        update(RefreshToken)
        .where(
            RefreshToken.family_id == family_id,
            RefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(UTC))
    )


async def revoke_refresh_tokens_for_user(
    session: AsyncSession, user_id: str
) -> None:
    await session.execute(
        update(RefreshToken)
        .where(
            RefreshToken.user_id == user_id,
            RefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(UTC))
    )


async def refresh_family_is_active(
    session: AsyncSession, user_id: str, family_id: str
) -> bool:
    result = await session.execute(
        select(RefreshToken.id)
        .where(
            RefreshToken.user_id == user_id,
            RefreshToken.family_id == family_id,
            RefreshToken.revoked_at.is_(None),
            RefreshToken.expires_at > datetime.now(UTC),
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None
