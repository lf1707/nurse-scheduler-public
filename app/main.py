"""FastAPI application factory.

Entry point: `uvicorn app.main:app --reload`
"""

from __future__ import annotations

import json
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.requests import Request
from starlette.responses import Response

from app.api.pages import router as pages_router
from app.api.v1 import api_router
from app.core.config import settings
from app.core.database import engine
from app.core.logging import setup_logging
from app.core.session_cookies import (
    ACCESS_COOKIE,
    CSRF_COOKIE,
    CSRF_HEADER,
    CSRF_ISSUED_HEADER,
    CSRF_SAFE_METHODS,
    IMPERSONATOR_REFRESH_COOKIE,
    REFRESH_COOKIE,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application startup/shutdown lifecycle."""
    setup_logging()
    import structlog

    log = structlog.get_logger("app.main")
    log.info("app.starting", env=settings.APP_ENV, name=settings.APP_NAME)

    yield

    log.info("app.stopping")
    await engine.dispose()
    log.info("app.stopped")


def create_app() -> FastAPI:
    """Create the FastAPI application."""
    app = FastAPI(
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
        description="Multi-tenant nurse scheduling SaaS",
        docs_url=None if settings.is_prod else "/docs",
        redoc_url=None if settings.is_prod else "/redoc",
        openapi_url=None if settings.is_prod else "/openapi.json",
        lifespan=lifespan,
        debug=settings.APP_DEBUG,
    )

    @app.middleware("http")
    async def enforce_allowed_host(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if settings.is_prod and request.url.path not in {"/health", "/health/ready"}:
            host_header = request.headers.get("Host", "")
            host = urlsplit(f"//{host_header}").hostname
            if not host or host.lower() not in settings.allowed_hosts:
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Invalid host header"},
                )
        return await call_next(request)

    @app.middleware("http")
    async def enforce_cookie_csrf(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        has_session_cookie = any(
            request.cookies.get(name)
            for name in (ACCESS_COOKIE, REFRESH_COOKIE, IMPERSONATOR_REFRESH_COOKIE)
        )
        login_request = request.url.path == "/api/v1/auth/login"
        authorization = request.headers.get("Authorization", "")
        bearer_client = authorization.startswith("Bearer ") and bool(authorization[7:])
        if (
            request.url.path.startswith("/api/v1/")
            and has_session_cookie
            and not bearer_client
            and not login_request
            and request.method not in CSRF_SAFE_METHODS
            and request.headers.get(CSRF_HEADER) != request.cookies.get(CSRF_COOKIE)
        ):
            return JSONResponse(status_code=403, content={"detail": "CSRF token missing or invalid"})
        if request.method in {"GET", "HEAD"} and not request.cookies.get(CSRF_COOKIE):
            response = await call_next(request)
            if not response.headers.get(CSRF_ISSUED_HEADER):
                response.set_cookie(
                    CSRF_COOKIE,
                    secrets.token_urlsafe(32),
                    max_age=60 * 60 * 24 * 30,
                    path="/",
                    httponly=False,
                    secure=settings.is_prod,
                    samesite="lax",
                )
            return response
        return await call_next(request)

    @app.middleware("http")
    async def add_security_headers(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        if request.url.path.startswith(("/pages/", "/static/js/")):
            response.headers.setdefault("Cache-Control", "no-cache")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; font-src 'self' data:; "
            "object-src 'none'; frame-ancestors 'none'; base-uri 'self'; "
            "form-action 'self'",
        )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        return response

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Routers
    app.include_router(api_router)
    app.include_router(pages_router)

    # Static assets for the HTMX/Alpine frontend (CSS, JS).
    static_dir = Path(__file__).resolve().parents[1] / "frontend" / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Root → login page.
    @app.get("/", include_in_schema=False)
    async def root_redirect() -> RedirectResponse:
        return RedirectResponse("/pages/login")

    # Health
    @app.get("/health", tags=["health"])
    async def health() -> dict[str, Any]:
        """Liveness probe."""
        return {
            "status": "ok",
            "name": settings.APP_NAME,
            "version": settings.APP_VERSION,
            "env": settings.APP_ENV,
        }

    @app.get("/health/ready", tags=["health"], response_model=None)
    async def readiness() -> dict[str, Any] | JSONResponse:
        """Readiness probe — checks DB + Redis connectivity."""
        result: dict[str, Any] = {"status": "ok", "checks": {}}

        # DB
        try:
            from datetime import UTC, datetime, timedelta

            from sqlalchemy import text

            from app.core.audit_export import get_export_activity_at, get_watermark
            from app.core.database import async_session_factory, clear_tenant_context

            async with async_session_factory() as session:
                await session.execute(text("SELECT 1"))
                await clear_tenant_context(session)
                result["checks"]["db"] = "ok"
                watermark = await get_watermark(session)
                if watermark is None:
                    result["checks"]["audit_export"] = "unknown"
                else:
                    maximum_age = timedelta(hours=settings.AUDIT_EXPORT_MAX_LAG_HOURS)
                    age = datetime.now(UTC) - get_export_activity_at(watermark)
                    result["checks"]["audit_export"] = "ok" if age <= maximum_age else "stale"
        except Exception:
            result["checks"]["db"] = "error"
            result["status"] = "degraded"

        # Redis
        try:
            import redis.asyncio as aioredis

            r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
            await r.ping()
            await r.aclose()
            result["checks"]["redis"] = "ok"
        except Exception:
            result["checks"]["redis"] = "error"
            result["status"] = "degraded"

        try:
            from pathlib import Path

            state_path = Path(settings.OPS_STATE_DIR) / "disk-usage.json"
            with state_path.open(encoding="utf-8") as state_file:
                disk_state: object = json.load(state_file)
                if isinstance(disk_state, dict):
                    result["checks"]["disk"] = disk_state.get("level", "unknown")
                    result["checks"]["disk_free_gb"] = disk_state.get("free_gb")
                else:
                    result["checks"]["disk"] = "unknown"
                    result["checks"]["disk_free_gb"] = None
        except (OSError, ValueError):
            result["checks"]["disk"] = "unknown"
            result["checks"]["disk_free_gb"] = None

        if result["status"] != "ok":
            return JSONResponse(result, status_code=503)
        return result

    return app


app = create_app()
