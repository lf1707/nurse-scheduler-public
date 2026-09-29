"""Security utilities — JWT creation/verification and password hashing (argon2)."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import jwt
from jwt.exceptions import InvalidTokenError
from passlib.context import CryptContext

from app.core.config import settings

# ──────────────────────────────────────────────────────────────
# Password hashing: argon2 (modern, secure, no bcrypt issues)
# ──────────────────────────────────────────────────────────────
pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")
DUMMY_PASSWORD_HASH = pwd_context.hash("login-timing-equalizer")


def hash_password(password: str) -> str:
    return cast(str, pwd_context.hash(password))


def verify_password(plain: str, hashed: str) -> bool:
    return cast(bool, pwd_context.verify(plain, hashed))


# ──────────────────────────────────────────────────────────────
# Password strength — reject weak/common passwords at the edge.
# ──────────────────────────────────────────────────────────────
# Minimal, dependency-free heuristic: require a mix of character classes
# and block the most trivially guessable strings. This runs before the
# credential is ever hashed or stored.
_COMMON_PASSWORDS = frozenset(
    {
        "123456", "123456789", "12345678", "12345", "1234567",
        "password", "password1", "passw0rd", "qwerty", "abc123",
        "111111", "000000", "123123", "admin", "letmein", "welcome",
        "monkey", "dragon", "iloveyou", "sunshine", "princess",
        "football", "baseball", "superman", "trustno1", "master",
    }
)
_MIN_LENGTH = 8
_MAX_LENGTH = 128


def validate_password_strength(password: str) -> None:
    """Raise ``ValueError`` if the password is too short, too long,
    too common, or lacks character diversity.

    Returns ``None`` when the password is acceptable.
    """
    if not isinstance(password, str):
        raise ValueError("Password must be a string")
    if len(password) < _MIN_LENGTH or len(password) > _MAX_LENGTH:
        raise ValueError(
            f"Password must be {_MIN_LENGTH}–{_MAX_LENGTH} characters long"
        )
    if password.strip().lower() in _COMMON_PASSWORDS:
        raise ValueError("Password is too common or guessable")
    # Require at least two of the four character classes.
    classes = 0
    if re.search(r"[a-z]", password):
        classes += 1
    if re.search(r"[A-Z]", password):
        classes += 1
    if re.search(r"[0-9]", password):
        classes += 1
    if re.search(r"[^A-Za-z0-9]", password):
        classes += 1
    if classes < 2:
        raise ValueError("Password must combine letters, numbers, and symbols")
    return None


# ──────────────────────────────────────────────────────────────
# JWT
# ──────────────────────────────────────────────────────────────
def create_access_token(
    subject: str,
    tenant_id: str | None,
    role: str,
    token_version: int = 0,
    extra_claims: dict[str, Any] | None = None,
    expires_minutes: int | None = None,
) -> str:
    """Create a JWT access token."""
    expire = datetime.now(UTC) + timedelta(
        minutes=expires_minutes or settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES
    )
    payload: dict[str, Any] = {
        "sub": subject,
        "tenant_id": tenant_id,
        "role": role,
        "type": "access",
        "ver": token_version,
        "iat": datetime.now(UTC),
        "exp": expire,
        "jti": str(uuid.uuid4()),
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def create_refresh_token(
    subject: str,
    token_version: int = 0,
    expires_minutes: int | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """Create a JWT refresh token.

    The TTL defaults to JWT_REFRESH_TOKEN_EXPIRE_MINUTES (the idle window).
    Impersonation grants use a shorter explicit TTL.
    """
    expire = datetime.now(UTC) + timedelta(
        minutes=expires_minutes or settings.JWT_REFRESH_TOKEN_EXPIRE_MINUTES
    )
    payload = {
        "sub": subject,
        "type": "refresh",
        "ver": token_version,
        "iat": datetime.now(UTC),
        "exp": expire,
        "jti": str(uuid.uuid4()),
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> dict[str, Any]:
    """Decode and verify a JWT. Raises JWTError on failure."""
    return jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])


class TokenError(InvalidTokenError):
    """Raised when token is invalid/expired."""


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode an access token, raising TokenError if invalid or wrong type."""
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except InvalidTokenError as e:
        raise TokenError(str(e)) from e
    if payload.get("type") != "access":
        raise TokenError("Not an access token")
    return payload
