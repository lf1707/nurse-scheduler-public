"""Server-rendered pages (HTMX/Alpine). Pages are unauthenticated shells —
the browser holds the JWT (localStorage) and attaches it as a Bearer header
to every htmx/fetch call (see base.html). All data access goes through the
authenticated /api/v1 endpoints; these handlers only render templates.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.core.config import settings
from app.core.database import async_session_factory
from app.core.security import decode_access_token
from app.core.session_cookies import ACCESS_COOKIE
from app.core.site import default_about_text
from app.models.platform import PlatformSetting

router = APIRouter(prefix="/pages", tags=["pages"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[2] / "frontend" / "templates"))


def _ctx(request: Request, **extra: Any) -> dict[str, Any]:
    """Common template context.

    Starlette's modern TemplateResponse signature is (request, name, context);
    the request MUST be the first positional arg so it isn't folded into the
    Jinja cache key (which would raise "unhashable type: 'dict'").
    """
    user_role = ""
    session_token = request.cookies.get(ACCESS_COOKIE, "")
    if session_token:
        try:
            payload = decode_access_token(session_token)
            role_value = payload.get("role", "")
            user_role = role_value if isinstance(role_value, str) else ""
        except Exception:
            user_role = ""
    ctx = {
        "request": request,
        "app_name": settings.APP_NAME,
        "app_version": settings.APP_VERSION,
        "user_role": user_role,
    }
    ctx.update(extra)
    return ctx


@router.get("/login", response_class=HTMLResponse)
async def page_login(request: Request) -> Response:
    return templates.TemplateResponse(request, "login.html", _ctx(request))


@router.get("/apply", response_class=HTMLResponse)
async def page_apply(request: Request) -> Response:
    return templates.TemplateResponse(request, "apply.html", _ctx(request))


@router.get("/verify", response_class=HTMLResponse)
async def page_verify_tenant_application(request: Request) -> Response:
    return templates.TemplateResponse(request, "verify_application.html", _ctx(request))


@router.get("/setup", response_class=HTMLResponse)
async def page_setup_tenant_admin(request: Request) -> Response:
    return templates.TemplateResponse(request, "setup_admin.html", _ctx(request))


@router.get("/dashboard", response_class=HTMLResponse)
async def page_dashboard(request: Request) -> Response:
    return templates.TemplateResponse(request, "dashboard.html", _ctx(request))


@router.get("/nurses", response_class=HTMLResponse)
async def page_nurses(request: Request) -> Response:
    return templates.TemplateResponse(request, "nurses.html", _ctx(request))


@router.get("/roles", response_class=HTMLResponse)
async def page_roles(request: Request) -> Response:
    return templates.TemplateResponse(request, "roles.html", _ctx(request))


@router.get("/skills", response_class=HTMLResponse)
async def page_skills(request: Request) -> Response:
    return templates.TemplateResponse(request, "skills.html", _ctx(request))


@router.get("/shifts", response_class=HTMLResponse)
async def page_shifts(request: Request) -> Response:
    return templates.TemplateResponse(request, "shifts.html", _ctx(request))


@router.get("/rules", response_class=HTMLResponse)
async def page_rules(request: Request) -> Response:
    return templates.TemplateResponse(request, "rules.html", _ctx(request))


@router.get("/generate", response_class=HTMLResponse)
async def page_generate(request: Request) -> Response:
    return templates.TemplateResponse(request, "generate.html", _ctx(request))


@router.get("/schedules", response_class=HTMLResponse)
async def page_schedules(request: Request) -> Response:
    return templates.TemplateResponse(request, "schedules.html", _ctx(request))


@router.get("/schedules/{request_id}", response_class=HTMLResponse)
async def page_schedule_detail(request: Request, request_id: str) -> Response:
    return templates.TemplateResponse(request, "schedule_detail.html", _ctx(request, request_id=request_id))


@router.get("/my-schedules", response_class=HTMLResponse)
async def page_my_schedules(request: Request) -> Response:
    return templates.TemplateResponse(request, "my_schedules.html", _ctx(request))


@router.get("/expectations", response_class=HTMLResponse)
async def page_expectations(request: Request) -> Response:
    return templates.TemplateResponse(request, "my_expectations.html", _ctx(request))


@router.get("/profile", response_class=HTMLResponse)
async def page_profile(request: Request) -> Response:
    return templates.TemplateResponse(request, "profile.html", _ctx(request))


@router.get("/tenants", response_class=HTMLResponse)
async def page_tenants(request: Request) -> Response:
    return templates.TemplateResponse(request, "tenants.html", _ctx(request))


@router.get("/tenant-applications", response_class=HTMLResponse)
async def page_tenant_applications(request: Request) -> Response:
    return templates.TemplateResponse(request, "tenant_applications.html", _ctx(request))


@router.get("/billing", response_class=HTMLResponse)
async def page_billing(request: Request) -> Response:
    return templates.TemplateResponse(request, "billing.html", _ctx(request))


@router.get("/users", response_class=HTMLResponse)
async def page_users(request: Request) -> Response:
    return templates.TemplateResponse(request, "users.html", _ctx(request))


@router.get("/subscriptions", response_class=HTMLResponse)
async def page_subscriptions_redirect() -> RedirectResponse:
    return RedirectResponse("/pages/tenants", status_code=303)


@router.get("/admin", response_class=HTMLResponse)
async def page_admin(request: Request) -> Response:
    return templates.TemplateResponse(request, "admin.html", _ctx(request))


@router.get("/about", response_class=HTMLResponse)
async def page_about(request: Request) -> Response:
    contact_email = None
    about_text = default_about_text(settings.APP_NAME)
    async with async_session_factory() as session:
        email_row = await session.get(PlatformSetting, "contact_email")
        if email_row and isinstance(email_row.value, str):
            contact_email = email_row.value
        about_row = await session.get(PlatformSetting, "about_text")
        if about_row and isinstance(about_row.value, str):
            about_text = about_row.value
    return templates.TemplateResponse(request, "about.html", _ctx(request, contact_email=contact_email, about_text=about_text))


@router.get("/nav", response_class=HTMLResponse)
async def nav_partial(request: Request) -> Response:
    """Render nav links based on whether the client has a token.

    The browser can't send the Authorization header on a navigation GET, so we
    read an optional `?logged_in=1` hint from htmx. Logged-in state is also
    client-side toggled; this is best-effort UX, not a security boundary.
    """
    logged_in = request.query_params.get("logged_in") == "1"
    user_role = ""
    if logged_in:
        # Browser navigation sends the HttpOnly session cookie; Bearer remains
        # compatible with API clients that fetch the nav partial directly.
        auth_header = request.headers.get("Authorization", "")
        session_token = (
            auth_header[7:]
            if auth_header.startswith("Bearer ")
            else request.cookies.get(ACCESS_COOKIE, "")
        )
        if session_token:
            try:
                from app.core.security import decode_access_token

                payload = decode_access_token(session_token)
                user_role_value = payload.get("role", "")
                user_role = user_role_value if isinstance(user_role_value, str) else ""
            except Exception:
                pass
    return templates.TemplateResponse(
        request, "partials/nav.html", _ctx(request, logged_in=logged_in, user_role=user_role)
    )


@router.post("/logout")
async def page_logout() -> RedirectResponse:
    """Clear the client token hint and redirect to login.

    The actual token lives in localStorage and is cleared client-side via JS
    in the nav click handler. This endpoint just redirects.
    """
    return RedirectResponse("/pages/login", status_code=303)
