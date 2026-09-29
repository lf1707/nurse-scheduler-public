"""Prospective-tenant application intake and super-admin review."""

from __future__ import annotations

import hashlib
import re
import secrets
import time
import unicodedata
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import SuperAdminDep
from app.core.audit import record_security_event
from app.core.config import settings
from app.core.database import async_session_factory, clear_tenant_context
from app.core.email import EmailDeliveryError, EmailMessageContent, send_email
from app.core.rate_limit import (
    RateLimitUnavailableError,
    client_ip,
    consume_tenant_application_quota,
    email_fingerprint,
)
from app.core.security import hash_password
from app.models.enums import (
    SubscriptionPlan,
    SubscriptionType,
    TenantApplicationStatus,
    UserRole,
)
from app.models.nurse import Role
from app.models.platform import PlatformSetting
from app.models.subscription import Subscription
from app.models.tenant import Tenant
from app.models.tenant_application import TenantApplication
from app.models.user import User
from app.schemas import (
    Paginated,
    TenantApplicationApproveRequest,
    TenantApplicationCreate,
    TenantApplicationDecisionResponse,
    TenantApplicationRead,
    TenantApplicationReceipt,
    TenantApplicationRejectRequest,
    TenantApplicationResendRequest,
    TenantApplicationSetupRequest,
    TenantApplicationSetupResponse,
    TenantApplicationVerifyRequest,
)

router = APIRouter(prefix="/tenant-applications", tags=["tenant-applications"])

GENERIC_RECEIPT = "If the information is valid, a verification email has been sent."


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _new_token() -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    return token, _token_hash(token)


async def _auto_approve_settings(session: AsyncSession) -> tuple[bool, SubscriptionPlan]:
    """Read auto-approve settings from platform_settings (DB), falling
    back to the static config defaults when the keys are absent."""
    auto_row = await session.get(PlatformSetting, "tenant_application_auto_approve")
    plan_row = await session.get(PlatformSetting, "tenant_application_auto_plan")
    auto_approve = bool(auto_row.value) if auto_row else settings.TENANT_APPLICATION_AUTO_APPROVE
    plan_value = str(plan_row.value) if plan_row else settings.TENANT_APPLICATION_AUTO_PLAN
    plan_str = (plan_value or "free").lower()
    return auto_approve, SubscriptionPlan(plan_str)


def _public_base_url(request: Request) -> str:
    return (settings.PUBLIC_BASE_URL or str(request.base_url)).rstrip("/")


async def _application_read(
    session: AsyncSession, application: TenantApplication
) -> TenantApplicationRead:
    """Refresh database-generated timestamps after the state-transition commit."""

    await session.refresh(application)
    return TenantApplicationRead.model_validate(application)


def _audit_details(application: TenantApplication, state: str) -> dict[str, Any]:
    return {
        "application_id": application.id,
        "state": state,
        "email_fingerprint": email_fingerprint(application.contact_email),
    }


async def _email_failure(
    session: AsyncSession,
    *,
    action: str,
    application: TenantApplication,
    request: Request | None = None,
) -> None:
    await record_security_event(
        session,
        action=action,
        outcome="failure",
        tenant_id=application.created_tenant_id,
        target_user_id=application.created_user_id,
        details=_audit_details(application, application.status.value),
        request=request,
    )
    await session.commit()


async def _send_verification(
    application: TenantApplication, token: str, request: Request
) -> None:
    await send_email(
        EmailMessageContent(
            to_email=application.contact_email,
            subject=f"验证您的 {settings.APP_NAME} 租户申请",
            body=(
                f"您好，{application.contact_name}：\n\n"
                f"我们收到了 {application.organization_name} 的租户申请。"
                "请打开以下链接完成邮箱验证：\n\n"
                f"{_public_base_url(request)}/pages/verify?token={token}\n\n"
                "链接为一次性使用，逾期后申请会自动失效。\n"
            ),
        )
    )


async def _provision_tenant(
    session: AsyncSession,
    application: TenantApplication,
    *,
    plan: SubscriptionPlan,
    trial_days: int | None = None,
    actor_id: str | None = None,
    request: Request | None = None,
) -> str:
    """Create tenant, admin, subscription, and mark application approved.

    Returns the setup token (plaintext) for the invitation email.
    Caller is responsible for committing the session and sending the email.
    """
    existing_user = (
        await session.execute(
            select(User).where(
                func.lower(User.email) == application.contact_email.lower()
            )
        )
    ).scalar_one_or_none()
    if existing_user:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="Contact email is already registered",
        )

    now = datetime.now(UTC)
    slug = await _unique_tenant_slug(session, application.organization_name)
    tenant = Tenant(
        name=application.organization_name,
        slug=slug,
        settings={
            "country": application.country,
            "timezone": application.timezone,
            "source": "tenant_application",
        },
    )
    session.add(tenant)
    await session.flush()
    session.add(Role(tenant_id=tenant.id, name="任意角色", code="any"))

    admin = User(
        tenant_id=tenant.id,
        email=application.contact_email,
        hashed_password=hash_password(secrets.token_urlsafe(32)),
        first_name=application.contact_name,
        last_name="",
        role=UserRole.TENANT_ADMIN,
        is_active=False,
    )
    session.add(admin)
    await session.flush()

    starts_at = now
    if trial_days and plan != SubscriptionPlan.FREE:
        ends_at = now + timedelta(days=trial_days)
    else:
        ends_at = None  # Free plan has no expiry
    subscription = Subscription(
        tenant_id=tenant.id,
        plan=plan,
        subscription_type=SubscriptionType.TRIAL.value,
        starts_at=starts_at,
        ends_at=ends_at,
        is_canceled=False,
    )
    session.add(subscription)

    setup_token, setup_digest = _new_token()
    application.status = TenantApplicationStatus.APPROVED
    application.reviewed_by_user_id = actor_id
    application.reviewed_at = now
    application.created_tenant_id = tenant.id
    application.created_user_id = admin.id
    application.setup_token_hash = setup_digest
    application.setup_expires_at = now + timedelta(
        hours=settings.TENANT_APPLICATION_SETUP_HOURS
    )

    audit_details = _audit_details(application, application.status.value)
    await record_security_event(
        session,
        action="tenant_application.approve",
        actor_id=actor_id,
        tenant_id=tenant.id,
        target_user_id=admin.id,
        details=audit_details,
        request=request,
    )
    await record_security_event(
        session,
        action="tenant.create",
        actor_id=actor_id,
        tenant_id=tenant.id,
        details={"slug": slug, "source": "tenant_application"},
        request=request,
    )
    await record_security_event(
        session,
        action="tenant.admin_create",
        actor_id=actor_id,
        tenant_id=tenant.id,
        target_user_id=admin.id,
        details={"inactive_until_setup": True},
        request=request,
    )
    await record_security_event(
        session,
        action="subscription.upsert",
        actor_id=actor_id,
        tenant_id=tenant.id,
        details={"plan": plan.value, "trial_days": trial_days or 0},
        request=request,
    )
    return setup_token


async def _send_free_welcome(
    application: TenantApplication, token: str, request: Request
) -> None:
    await send_email(
        EmailMessageContent(
            to_email=application.contact_email,
            subject=f"您的 {settings.APP_NAME} 租户已开通",
            body=(
                f"您好，{application.contact_name}：\n\n"
                f"{application.organization_name} 的租户申请已通过，"
                "已为您开通 Free 套餐。请设置管理员密码：\n\n"
                f"{_public_base_url(request)}/pages/setup?token={token}\n\n"
                "设置完成后即可登录并管理团队成员。\n"
            ),
        )
    )


@router.post("", response_model=TenantApplicationReceipt, status_code=status.HTTP_202_ACCEPTED)
async def submit_tenant_application(
    body: TenantApplicationCreate,
    request: Request,
) -> TenantApplicationReceipt:
    ip = client_ip(request)
    email = body.contact_email.lower()
    try:
        allowed = await consume_tenant_application_quota(ip, email)
    except RateLimitUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Application submission is temporarily unavailable",
        ) from exc
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many applications. Please try later.",
        )

    elapsed_ms = int(time.time() * 1000) - body.form_started_at
    if body.honeypot or elapsed_ms < settings.TENANT_APPLICATION_MIN_SUBMIT_SECONDS * 1000:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Automatic submission rejected",
        )

    async with async_session_factory() as session:
        await clear_tenant_context(session)
        existing_user = (
            await session.execute(select(User).where(func.lower(User.email) == email))
        ).scalar_one_or_none()

        active_application = (
            await session.execute(
                select(TenantApplication)
                .where(
                    TenantApplication.contact_email == email,
                    TenantApplication.status.in_(
                        [
                            TenantApplicationStatus.PENDING_EMAIL_VERIFICATION,
                            TenantApplicationStatus.PENDING_REVIEW,
                            TenantApplicationStatus.APPROVED,
                        ]
                    ),
                )
                .order_by(TenantApplication.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        rejected_recently = False

        if existing_user is None and active_application is None:
            latest = (
                await session.execute(
                    select(TenantApplication)
                    .where(
                        TenantApplication.contact_email == email,
                        TenantApplication.status == TenantApplicationStatus.REJECTED,
                    )
                    .order_by(TenantApplication.reviewed_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            cooldown = timedelta(
                days=settings.TENANT_APPLICATION_REJECTION_COOLDOWN_DAYS
            )
            rejected_recently = bool(
                latest
                and latest.reviewed_at
                and datetime.now(tz=latest.reviewed_at.tzinfo) - latest.reviewed_at < cooldown
            )

        if existing_user is not None or active_application is not None or rejected_recently:
            await record_security_event(
                session,
                action="tenant_application.submit",
                outcome="denied",
                details={
                    "email_fingerprint": email_fingerprint(email),
                    "reason": (
                        "existing_user"
                        if existing_user
                        else "active_application"
                        if active_application
                        else "rejection_cooldown"
                    ),
                },
                request=request,
            )
            await session.commit()
            return TenantApplicationReceipt(message=GENERIC_RECEIPT)

        token, token_digest = _new_token()
        application = TenantApplication(
            organization_name=body.organization_name.strip(),
            contact_name=body.contact_name.strip(),
            contact_email=email,
            country=body.country.strip(),
            timezone=body.timezone.strip(),
            expected_nurse_count=body.expected_nurse_count,
            use_case_summary=body.use_case_summary.strip(),
            status=TenantApplicationStatus.PENDING_EMAIL_VERIFICATION,
            verification_token_hash=token_digest,
            verification_expires_at=datetime.now(UTC)
            + timedelta(hours=settings.TENANT_APPLICATION_VERIFICATION_HOURS),
            ip_address=ip[:64],
            user_agent=(request.headers.get("User-Agent", "")[:512] or None),
        )
        session.add(application)
        await session.flush()
        await record_security_event(
            session,
            action="tenant_application.submit",
            details=_audit_details(
                application, TenantApplicationStatus.PENDING_EMAIL_VERIFICATION.value
            ),
            request=request,
        )
        await session.commit()

        try:
            await _send_verification(application, token, request)
        except EmailDeliveryError as exc:
            await _email_failure(
                session,
                action="tenant_application.verification_email",
                application=application,
                request=request,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Verification email could not be sent",
            ) from exc

    return TenantApplicationReceipt(message=GENERIC_RECEIPT)


@router.post("/verify", response_model=TenantApplicationReceipt)
async def verify_tenant_application(
    body: TenantApplicationVerifyRequest, request: Request
) -> TenantApplicationReceipt:
    async with async_session_factory() as session:
        await clear_tenant_context(session)
        application = (
            await session.execute(
                select(TenantApplication).where(
                    TenantApplication.verification_token_hash == _token_hash(body.token)
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if (
            not application
            or application.status != TenantApplicationStatus.PENDING_EMAIL_VERIFICATION
            or not application.verification_expires_at
            or datetime.now(tz=application.verification_expires_at.tzinfo)
            >= application.verification_expires_at
        ):
            await record_security_event(
                session,
                action="tenant_application.verify",
                outcome="failure",
                details={"token_hash_prefix": _token_hash(body.token)[:12]},
                request=request,
            )
            await session.commit()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Verification link is invalid, used, or expired",
            )

        application.verification_token_hash = None
        application.verification_expires_at = None
        application.verified_at = datetime.now(UTC)

        auto_approve, plan = await _auto_approve_settings(session)
        if auto_approve:
            setup_token = await _provision_tenant(
                session,
                application,
                plan=plan,
                trial_days=14 if plan == SubscriptionPlan.PRO else None,
                actor_id=None,  # auto-approve has no human actor
                request=request,
            )
            await record_security_event(
                session,
                action="tenant_application.verify",
                details=_audit_details(application, application.status.value),
                request=request,
            )
            await session.commit()

            try:
                if plan == SubscriptionPlan.FREE:
                    await _send_free_welcome(application, setup_token, request)
                else:
                    await _send_invitation(application, setup_token, request)
            except EmailDeliveryError:
                await _email_failure(
                    session,
                    action="tenant_application.invitation_email",
                    application=application,
                    request=request,
                )
            return TenantApplicationReceipt(
                message="Email verified. Your tenant has been provisioned. "
                "Check your email for setup instructions."
            )
        else:
            application.status = TenantApplicationStatus.PENDING_REVIEW
            await record_security_event(
                session,
                action="tenant_application.verify",
                details=_audit_details(application, application.status.value),
                request=request,
            )
            await session.commit()
    return TenantApplicationReceipt(message="Email verified. The application is pending review.")


@router.post("/resend", response_model=TenantApplicationReceipt)
async def resend_tenant_application_verification(
    body: TenantApplicationResendRequest, request: Request
) -> TenantApplicationReceipt:
    email = body.email.lower()
    try:
        allowed = await consume_tenant_application_quota(client_ip(request), email)
    except RateLimitUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Application submission is temporarily unavailable",
        ) from exc
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many applications. Please try later.",
        )

    async with async_session_factory() as session:
        await clear_tenant_context(session)
        application = (
            await session.execute(
                select(TenantApplication)
                .where(
                    TenantApplication.contact_email == email,
                    TenantApplication.status
                    == TenantApplicationStatus.PENDING_EMAIL_VERIFICATION,
                )
                .with_for_update()
                .order_by(TenantApplication.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if (
            not application
            or not application.verification_expires_at
            or datetime.now(tz=application.verification_expires_at.tzinfo)
            >= application.verification_expires_at
        ):
            await record_security_event(
                session,
                action="tenant_application.verification_resend",
                outcome="denied",
                details={"email_fingerprint": email_fingerprint(email)},
                request=request,
            )
            await session.commit()
            return TenantApplicationReceipt(message=GENERIC_RECEIPT)

        token, token_digest = _new_token()
        application.verification_token_hash = token_digest
        await record_security_event(
            session,
            action="tenant_application.verification_resend",
            details=_audit_details(application, application.status.value),
            request=request,
        )
        await session.commit()
        try:
            await _send_verification(application, token, request)
        except EmailDeliveryError as exc:
            await _email_failure(
                session,
                action="tenant_application.verification_email",
                application=application,
                request=request,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Verification email could not be sent",
            ) from exc
    return TenantApplicationReceipt(message=GENERIC_RECEIPT)


@router.post("/setup", response_model=TenantApplicationSetupResponse)
async def setup_approved_tenant_admin(
    body: TenantApplicationSetupRequest, request: Request
) -> TenantApplicationSetupResponse:
    async with async_session_factory() as session:
        await clear_tenant_context(session)
        application = (
            await session.execute(
                select(TenantApplication).where(
                    TenantApplication.setup_token_hash == _token_hash(body.token)
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if (
            not application
            or application.status != TenantApplicationStatus.APPROVED
            or not application.setup_expires_at
            or application.setup_completed_at
            or datetime.now(tz=application.setup_expires_at.tzinfo)
            >= application.setup_expires_at
        ):
            await record_security_event(
                session,
                action="tenant_application.setup",
                outcome="failure",
                details={"token_hash_prefix": _token_hash(body.token)[:12]},
                request=request,
            )
            await session.commit()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Setup link is invalid, used, or expired",
            )

        user = (
            await session.execute(select(User).where(User.id == application.created_user_id))
        ).scalar_one_or_none()
        if not user or user.is_active:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Setup link is invalid or has already been used",
            )

        user.hashed_password = hash_password(body.new_password)
        user.is_active = True
        application.setup_token_hash = None
        application.setup_expires_at = None
        application.setup_completed_at = datetime.now(UTC)
        await record_security_event(
            session,
            action="tenant_application.setup",
            actor_id=user.id,
            tenant_id=application.created_tenant_id,
            target_user_id=user.id,
            details=_audit_details(application, application.status.value),
            request=request,
        )
        await session.commit()
    return TenantApplicationSetupResponse(
        message="Administrator account activated. Please sign in."
    )


@router.get("", response_model=Paginated[TenantApplicationRead])
async def list_tenant_applications(
    ctx: SuperAdminDep,
    application_status: TenantApplicationStatus | None = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> Paginated[TenantApplicationRead]:
    _user, _tenant_id, session = ctx
    stmt = select(TenantApplication)
    if application_status:
        stmt = stmt.where(TenantApplication.status == application_status)
    total = (
        await session.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one()
    rows = (
        await session.execute(
            stmt.order_by(TenantApplication.created_at.desc(), TenantApplication.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return Paginated(
        items=[TenantApplicationRead.model_validate(row) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{application_id}", response_model=TenantApplicationRead)
async def get_tenant_application(
    application_id: str,
    ctx: SuperAdminDep,
) -> TenantApplicationRead:
    _user, _tenant_id, session = ctx
    application = await session.get(TenantApplication, application_id)
    if not application:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Application not found")
    return TenantApplicationRead.model_validate(application)


def _slugify_organization(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    normalized = re.sub(r"[^a-z0-9\s-]", "", normalized.lower())
    normalized = re.sub(r"[\s-]+", "-", normalized).strip("-")
    return normalized[:72] or "tenant"


async def _unique_tenant_slug(session: AsyncSession, organization_name: str) -> str:
    base = _slugify_organization(organization_name)
    while True:
        slug = f"{base}-{uuid.uuid4().hex[:8]}"
        existing = (
            await session.execute(select(Tenant).where(Tenant.slug == slug))
        ).scalar_one_or_none()
        if existing is None:
            return slug


async def _send_invitation(
    application: TenantApplication, token: str, request: Request
) -> None:
    await send_email(
        EmailMessageContent(
            to_email=application.contact_email,
            subject=f"您的 {settings.APP_NAME} 租户已开通",
            body=(
                f"您好，{application.contact_name}：\n\n"
                f"{application.organization_name} 的租户申请已通过审核，"
                "并获得 14 天 Pro 试用。请设置管理员密码：\n\n"
                f"{_public_base_url(request)}/pages/setup?token={token}\n\n"
                "设置完成后即可登录并管理团队成员。\n"
            ),
        )
    )


async def _send_rejection(application: TenantApplication) -> None:
    await send_email(
        EmailMessageContent(
            to_email=application.contact_email,
            subject=f"您的 {settings.APP_NAME} 租户申请未通过",
            body=(
                f"您好，{application.contact_name}：\n\n"
                f"很抱歉，{application.organization_name} 本次租户申请未通过审核。"
                "如机构情况发生变化，欢迎后续重新提交申请。\n"
            ),
        )
    )


@router.post(
    "/{application_id}/approve",
    response_model=TenantApplicationDecisionResponse,
)
async def approve_tenant_application(
    application_id: str,
    body: TenantApplicationApproveRequest,
    request: Request,
    ctx: SuperAdminDep,
) -> TenantApplicationDecisionResponse:
    user, _tenant_id, session = ctx
    application = (
        await session.execute(
            select(TenantApplication)
            .where(TenantApplication.id == application_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not application:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Application not found")
    if application.status != TenantApplicationStatus.PENDING_REVIEW:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="Only a pending-review application can be approved",
        )

    setup_token = await _provision_tenant(
        session,
        application,
        plan=SubscriptionPlan.PRO,
        trial_days=14,
        actor_id=user.id,
        request=request,
    )
    application.review_notes = body.review_notes
    await session.commit()

    email_sent = True
    try:
        await _send_invitation(application, setup_token, request)
    except EmailDeliveryError:
        email_sent = False
        await _email_failure(
            session,
            action="tenant_application.invitation_email",
            application=application,
            request=request,
        )

    return TenantApplicationDecisionResponse(
        application=await _application_read(session, application),
        email_sent=email_sent,
    )


@router.post(
    "/{application_id}/reject",
    response_model=TenantApplicationDecisionResponse,
)
async def reject_tenant_application(
    application_id: str,
    body: TenantApplicationRejectRequest,
    request: Request,
    ctx: SuperAdminDep,
) -> TenantApplicationDecisionResponse:
    user, _tenant_id, session = ctx
    application = (
        await session.execute(
            select(TenantApplication)
            .where(TenantApplication.id == application_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not application:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Application not found")
    if application.status != TenantApplicationStatus.PENDING_REVIEW:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="Only a pending-review application can be rejected",
        )

    application.status = TenantApplicationStatus.REJECTED
    application.reviewed_by_user_id = user.id
    application.reviewed_at = datetime.now(UTC)
    application.rejection_reason = body.reason
    application.review_notes = body.review_notes
    await record_security_event(
        session,
        action="tenant_application.reject",
        actor_id=user.id,
        details=_audit_details(application, application.status.value),
        request=request,
    )
    await session.commit()
    email_sent = True
    try:
        await _send_rejection(application)
    except EmailDeliveryError:
        email_sent = False
        await _email_failure(
            session,
            action="tenant_application.rejection_email",
            application=application,
            request=request,
        )
    return TenantApplicationDecisionResponse(
        application=await _application_read(session, application),
        email_sent=email_sent,
    )


@router.post(
    "/{application_id}/resend-invitation",
    response_model=TenantApplicationDecisionResponse,
)
async def resend_tenant_application_invitation(
    application_id: str,
    request: Request,
    ctx: SuperAdminDep,
) -> TenantApplicationDecisionResponse:
    user, _tenant_id, session = ctx
    application = (
        await session.execute(
            select(TenantApplication)
            .where(TenantApplication.id == application_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not application:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Application not found")
    if application.status != TenantApplicationStatus.APPROVED or application.setup_completed_at:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="Invitation can only be resent for an unactivated approved application",
        )

    token, token_digest = _new_token()
    application.setup_token_hash = token_digest
    application.setup_expires_at = datetime.now(UTC) + timedelta(
        hours=settings.TENANT_APPLICATION_SETUP_HOURS
    )
    await record_security_event(
        session,
        action="tenant_application.invitation_resend",
        actor_id=user.id,
        tenant_id=application.created_tenant_id,
        target_user_id=application.created_user_id,
        details=_audit_details(application, application.status.value),
        request=request,
    )
    await session.commit()

    email_sent = True
    try:
        await _send_invitation(application, token, request)
    except EmailDeliveryError:
        email_sent = False
        await _email_failure(
            session,
            action="tenant_application.invitation_email",
            application=application,
            request=request,
        )
    return TenantApplicationDecisionResponse(
        application=await _application_read(session, application),
        email_sent=email_sent,
    )
