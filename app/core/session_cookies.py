"""HttpOnly browser session cookie helpers and CSRF constants."""

from __future__ import annotations

import secrets

from fastapi import Response

from app.core.config import settings

ACCESS_COOKIE = "ns_access"
REFRESH_COOKIE = "ns_refresh"
IMPERSONATOR_REFRESH_COOKIE = "ns_impersonator_refresh"
CSRF_COOKIE = "ns_csrf"
CSRF_HEADER = "X-CSRF-Token"
CSRF_ISSUED_HEADER = "X-NS-CSRF-Issued"
CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


def _cookie_parameters(max_age: int) -> dict[str, object]:
    return {
        "max_age": max_age,
        "path": "/",
        "httponly": True,
        "secure": settings.is_prod,
        "samesite": "lax",
    }


def set_session_cookies(
    response: Response,
    *,
    access_token: str,
    refresh_token: str,
    access_max_age: int,
    refresh_max_age: int,
    impersonator_refresh_token: str | None = None,
) -> None:
    response.set_cookie(
        ACCESS_COOKIE,
        access_token,
        **_cookie_parameters(access_max_age),  # type: ignore[arg-type]
    )
    response.set_cookie(
        REFRESH_COOKIE,
        refresh_token,
        **_cookie_parameters(refresh_max_age),  # type: ignore[arg-type]
    )
    if impersonator_refresh_token:
        response.set_cookie(
            IMPERSONATOR_REFRESH_COOKIE,
            impersonator_refresh_token,
            **_cookie_parameters(refresh_max_age),  # type: ignore[arg-type]
        )
    else:
        response.delete_cookie(IMPERSONATOR_REFRESH_COOKIE, path="/")
    response.set_cookie(
        CSRF_COOKIE,
        secrets.token_urlsafe(32),
        max_age=refresh_max_age,
        path="/",
        httponly=False,
        secure=settings.is_prod,
        samesite="lax",
    )
    response.headers[CSRF_ISSUED_HEADER] = "1"


def clear_session_cookies(response: Response) -> None:
    for name in (ACCESS_COOKIE, REFRESH_COOKIE, IMPERSONATOR_REFRESH_COOKIE):
        response.delete_cookie(name, path="/", secure=settings.is_prod, httponly=True)
    response.delete_cookie(CSRF_COOKIE, path="/", secure=settings.is_prod)
