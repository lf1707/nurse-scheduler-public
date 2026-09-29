"""Authentication endpoints — login, refresh, register (tenant-scoped)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.api.deps import (
    AnyUserDep,
    TenantAdminDep,
)
from app.core.audit import record_security_event
from app.core.config import settings
from app.core.impersonation import pending_tenant_admin_can_be_impersonated
from app.core.logging import get_logger
from app.core.mfa import (
    generate_mfa_secret,
    generate_recovery_codes,
    provisioning_uri,
    qr_code_svg,
    verify_recovery_codes,
    verify_totp,
)
from app.core.rate_limit import (
    RateLimitUnavailableError,
    clear_login_failures,
    client_ip,
    email_fingerprint,
    login_is_blocked,
    record_login_failure,
    should_audit_rate_limited_login,
)
from app.core.refresh_tokens import (
    claim_refresh_token,
    issue_refresh_token,
    revoke_refresh_family,
    revoke_refresh_tokens_for_user,
)
from app.core.security import (
    DUMMY_PASSWORD_HASH,
    create_access_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.core.session_cookies import (
    IMPERSONATOR_REFRESH_COOKIE,
    REFRESH_COOKIE,
    clear_session_cookies,
    set_session_cookies,
)
from app.models.enums import UserRole
from app.models.nurse import (
    Nurse,
    NursePreference,
    Role,
    Skill,
    nurse_roles,
    nurse_skills,
)
from app.models.shift import ShiftTemplate
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas import (
    LoginRequest,
    LoginResponse,
    MfaEnrollmentConfirm,
    MfaEnrollmentRead,
    MfaRecoveryCodes,
    NursePreferenceCreate,
    NursePreferenceRead,
    PasswordChange,
    RefreshRequest,
    TokenResponse,
    UserCreate,
    UserProfileUpdate,
    UserRead,
    UserSkillUpdate,
    UserUpdate,
)

router = APIRouter(prefix="/auth", tags=["auth"])
logger = get_logger("app.auth")


async def _pending_admin_can_be_impersonated(
    session: AsyncSession,
    target: User,
) -> bool:
    return await pending_tenant_admin_can_be_impersonated(session, target)


def _invalid_refresh_token() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid refresh token",
    )


async def _revoke_replayed_family(
    session: AsyncSession,
    payload: dict[str, Any],
) -> None:
    family_id = payload.get("family_id")
    if not family_id:
        return
    await revoke_refresh_family(session, str(family_id))
    await record_security_event(
        session,
        action="auth.refresh.reuse_detected",
        outcome="denied",
        actor_id=str(payload["sub"]),
        target_user_id=str(payload["sub"]),
        tenant_id=payload.get("tenant_id"),
        details={"reason": "revoked_or_unknown_token"},
        commit=True,
    )


async def _user_read_with_nurse(user: User, session: AsyncSession) -> UserRead:
    data = UserRead.model_validate(user).model_dump()
    if user.tenant_id:
        tenant = await session.get(Tenant, user.tenant_id)
        if tenant:
            data["tenant_name"] = tenant.name
            data["tenant_slug"] = tenant.slug
    if user.nurse_id:
        linked_nurse = await session.get(Nurse, user.nurse_id)
        if linked_nurse:
            data["nurse_name"] = f"{linked_nurse.last_name}{linked_nurse.first_name}"
            skills = (
                await session.execute(
                    select(Skill)
                    .join(nurse_skills, nurse_skills.c.skill_id == Skill.id)
                    .where(nurse_skills.c.nurse_id == linked_nurse.id)
                    .order_by(Skill.code)
                )
            ).scalars().all()
            data["nurse_skill_ids"] = [skill.id for skill in skills]
            data["nurse_skill_names"] = [
                f"{skill.name}（{skill.code}）" for skill in skills
            ]
            roles = (
                await session.execute(
                    select(Role)
                    .join(nurse_roles, nurse_roles.c.role_id == Role.id)
                    .where(nurse_roles.c.nurse_id == linked_nurse.id)
                    .order_by(Role.code)
                )
            ).scalars().all()
            data["nurse_role_ids"] = [role.id for role in roles]
            data["nurse_role_names"] = [
                f"{role.name}（{role.code}）" for role in roles
            ]
    return UserRead(**data)


async def _email_taken(session: AsyncSession, email: str) -> User | None:
    """Check email globally, temporarily bypassing tenant RLS."""
    await session.execute(text("SET LOCAL app.is_super = '1'"))
    existing = await session.execute(
        select(User).where(func.lower(User.email) == email.lower())
    )
    await session.execute(text("SET LOCAL app.is_super = ''"))
    existing_user: User | None = existing.scalar_one_or_none()
    return existing_user


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest, request: Request) -> Response:
    """Login with email + password. Returns access + refresh JWT."""
    # Login runs outside the per-user RLS context; query users table directly.
    from app.core.database import async_session_factory, clear_tenant_context

    ip = client_ip(request)
    try:
        if await login_is_blocked(ip, body.email):
            logger.warning(
                "auth.login.rate_limited",
                ip=ip,
                email_fingerprint=email_fingerprint(body.email),
            )
            fingerprint = email_fingerprint(body.email)
            if await should_audit_rate_limited_login(ip, body.email):
                try:
                    async with async_session_factory() as audit_session:
                        await clear_tenant_context(audit_session)
                        await record_security_event(
                            audit_session,
                            action="auth.login.rate_limited",
                            outcome="denied",
                            details={
                                "reason": "rate_limit",
                                "email_fingerprint": fingerprint,
                            },
                            request=request,
                            commit=True,
                        )
                except Exception as exc:  # noqa: BLE001 - keep the 429 observable
                    logger.error(
                        "auth.login.rate_limited.audit_failed",
                        ip=ip,
                        error=str(exc),
                    )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many failed login attempts. Try again later.",
            )
    except RateLimitUnavailableError:
        logger.error("auth.login.rate_limit_unavailable", ip=ip)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication temporarily unavailable",
        ) from None

    async with async_session_factory() as session:
        await clear_tenant_context(session)  # login looks up across tenants
        result = await session.execute(
            select(User).where(func.lower(User.email) == body.email.lower())
        )
        user = result.scalar_one_or_none()
        recovery_codes: list[str] = []
        password_ok = await run_in_threadpool(
            verify_password,
            body.password,
            user.hashed_password if user else DUMMY_PASSWORD_HASH,
        )
        if not user or not password_ok:
            try:
                await record_login_failure(ip, body.email)
            except RateLimitUnavailableError:
                logger.error("auth.login.rate_limit_write_failed", ip=ip)
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Authentication temporarily unavailable",
            ) from None
            await record_security_event(
                session,
                action="auth.login",
                outcome="failure",
                actor_id=user.id if user else None,
                target_user_id=user.id if user else None,
                tenant_id=user.tenant_id if user else None,
                details={"email_fingerprint": email_fingerprint(body.email)},
                request=request,
                commit=True,
            )
            logger.warning(
                "auth.login.failed",
                ip=ip,
                email_fingerprint=email_fingerprint(body.email),
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password",
            )
        if not user.is_active:
            await record_security_event(
                session,
                action="auth.login",
                outcome="denied",
                actor_id=user.id,
                target_user_id=user.id,
                tenant_id=user.tenant_id,
                details={
                    "email_fingerprint": email_fingerprint(body.email),
                    "reason": "inactive_user",
                },
                request=request,
                commit=True,
            )
            logger.warning("auth.login.inactive_user", ip=ip, user_id=user.id)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Account inactive",
            )
        if user.mfa_enabled and user.mfa_secret:
            has_totp = body.totp_code is not None
            recovery_codes = body.recovery_codes or []
            has_recovery_codes = bool(recovery_codes)
            if not has_totp and not has_recovery_codes:
                await record_security_event(
                    session,
                    action="auth.login",
                    outcome="denied",
                    actor_id=user.id,
                    target_user_id=user.id,
                    tenant_id=user.tenant_id,
                    details={
                        "email_fingerprint": email_fingerprint(body.email),
                        "reason": "mfa_required",
                    },
                    request=request,
                    commit=True,
                )
                raise HTTPException(
                    status_code=status.HTTP_428_PRECONDITION_REQUIRED,
                    detail="MFA verification required",
                )
            recovery_valid = (
                has_recovery_codes
                and bool(user.mfa_recovery_hash)
                and verify_recovery_codes(
                    recovery_codes,
                    user.mfa_recovery_hash or "",
                )
            )
            if has_recovery_codes and not recovery_valid:
                try:
                    await record_login_failure(ip, body.email)
                except RateLimitUnavailableError:
                    logger.error("auth.login.rate_limit_write_failed", ip=ip)
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="Authentication temporarily unavailable",
                    ) from None
                await record_security_event(
                    session,
                    action="auth.login",
                    outcome="denied",
                    actor_id=user.id,
                    target_user_id=user.id,
                    tenant_id=user.tenant_id,
                    details={
                        "email_fingerprint": email_fingerprint(body.email),
                        "reason": "mfa_invalid_recovery_code",
                    },
                    request=request,
                    commit=True,
                )
                logger.warning("auth.login.mfa_recovery_invalid", ip=ip, user_id=user.id)
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid recovery code",
                )
            if not has_recovery_codes and not verify_totp(
                user.mfa_secret, body.totp_code or ""
            ):
                try:
                    await record_login_failure(ip, body.email)
                except RateLimitUnavailableError:
                    logger.error("auth.login.rate_limit_write_failed", ip=ip)
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="Authentication temporarily unavailable",
                    ) from None
                await record_security_event(
                    session,
                    action="auth.login",
                    outcome="denied",
                    actor_id=user.id,
                    target_user_id=user.id,
                    tenant_id=user.tenant_id,
                    details={
                        "email_fingerprint": email_fingerprint(body.email),
                        "reason": "mfa_invalid_code",
                    },
                    request=request,
                    commit=True,
                )
                logger.warning("auth.login.mfa_invalid", ip=ip, user_id=user.id)
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid MFA code",
                )
            if has_recovery_codes:
                codes, digest = generate_recovery_codes()
                user.mfa_recovery_hash = digest
                recovery_codes = codes
                await record_security_event(
                    session,
                    action="auth.mfa.recovery_used",
                    actor_id=user.id,
                    target_user_id=user.id,
                    tenant_id=user.tenant_id,
                )
        if user.tenant_id:
            tenant = (
                await session.execute(select(Tenant).where(Tenant.id == user.tenant_id))
            ).scalar_one_or_none()
            if not tenant or not tenant.is_active:
                logger.warning(
                    "auth.login.inactive_tenant",
                    ip=ip,
                    user_id=user.id,
                    tenant_id=user.tenant_id,
                )
                await record_security_event(
                    session,
                    action="auth.login",
                    outcome="denied",
                    actor_id=user.id,
                    target_user_id=user.id,
                    tenant_id=user.tenant_id,
                    details={
                        "email_fingerprint": email_fingerprint(body.email),
                        "reason": "inactive_tenant",
                    },
                    request=request,
                    commit=True,
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                detail="Tenant inactive",
            )

        issued_refresh = issue_refresh_token(session, user)
        access = create_access_token(
            subject=user.id,
            tenant_id=user.tenant_id,
            role=user.role.value,
            token_version=user.token_version,
            extra_claims={
                "name": f"{user.first_name} {user.last_name}",
                "family_id": issued_refresh.family_id,
                "mfa_enabled": user.mfa_enabled,
            },
        )
        try:
            await clear_login_failures(ip, body.email)
        except RateLimitUnavailableError:
            logger.error("auth.login.rate_limit_clear_failed", ip=ip)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Authentication temporarily unavailable",
            ) from None
        logger.info(
            "auth.login.succeeded",
            ip=ip,
            user_id=user.id,
            tenant_id=user.tenant_id,
        )
        await record_security_event(
            session,
            action="auth.login",
            outcome="success",
            actor_id=user.id,
            target_user_id=user.id,
            tenant_id=user.tenant_id,
            details={"email_fingerprint": email_fingerprint(body.email)},
            request=request,
        )
        await session.commit()
        payload = LoginResponse(
            access_token=access,
            refresh_token=issued_refresh.token,
            recovery_codes=recovery_codes,
        )
        response = JSONResponse(payload.model_dump())
        set_session_cookies(
            response,
            access_token=access,
            refresh_token=issued_refresh.token,
            access_max_age=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            refresh_max_age=settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60,
        )
        return response


@router.post("/refresh", response_model=TokenResponse)
async def refresh(body: RefreshRequest, request: Request) -> Response:
    """Exchange a refresh token for a new access token."""
    refresh_token = body.refresh_token or request.cookies.get(REFRESH_COOKIE)
    if not refresh_token:
        raise _invalid_refresh_token()
    try:
        payload = decode_token(refresh_token)
    except Exception as exc:
        raise _invalid_refresh_token() from exc
    if payload.get("type") != "refresh":
        raise _invalid_refresh_token()
    token_id = payload.get("jti")
    family_id = payload.get("family_id")
    if not token_id or not family_id:
        raise _invalid_refresh_token()

    from app.core.database import async_session_factory, clear_tenant_context

    user_id = payload["sub"]
    token_version = int(payload.get("ver", 0))
    async with async_session_factory() as session:
        await clear_tenant_context(session)
        record = await claim_refresh_token(session, refresh_token, str(token_id))
        if record is None:
            await _revoke_replayed_family(session, payload)
            raise _invalid_refresh_token()

        user = (
            await session.execute(select(User).where(User.id == user_id))
        ).scalar_one_or_none()
        if not user or (
            not user.is_active
            and not (
                record.impersonated_by_user_id
                and await pending_tenant_admin_can_be_impersonated(session, user)
            )
        ):
            await revoke_refresh_family(session, str(family_id))
            await session.commit()
            raise _invalid_refresh_token()
        if user.token_version != token_version:
            await revoke_refresh_tokens_for_user(session, user.id)
            await session.commit()
            raise _invalid_refresh_token()
        now = datetime.now(UTC)
        if (
            record.user_id != user.id
            or record.family_id != family_id
            or record.token_version != token_version
            or record.expires_at <= now
        ):
            record.revoked_at = now
            await session.commit()
            raise _invalid_refresh_token()
        if user.tenant_id:
            tenant = (
                await session.execute(select(Tenant).where(Tenant.id == user.tenant_id))
            ).scalar_one_or_none()
            if not tenant or not tenant.is_active:
                await revoke_refresh_family(session, str(family_id))
                await session.commit()
                raise _invalid_refresh_token()

        impersonator_id = payload.get("impersonated_by")
        if bool(impersonator_id) != bool(record.impersonated_by_user_id):
            record.revoked_at = datetime.now(UTC)
            await session.commit()
            raise _invalid_refresh_token()
        access_claims = {
            "name": f"{user.first_name} {user.last_name}",
            "family_id": str(family_id),
            "mfa_enabled": user.mfa_enabled,
        }
        if impersonator_id:
            impersonator = (
                await session.execute(
                    select(User).where(User.id == impersonator_id)
                )
            ).scalar_one_or_none()
            if not impersonator or not impersonator.is_active:
                await revoke_refresh_family(session, str(family_id))
                await session.commit()
                raise _invalid_refresh_token()
            if impersonator.role != UserRole.SUPER_ADMIN:
                await revoke_refresh_family(session, str(family_id))
                await session.commit()
                raise _invalid_refresh_token()
            if record.impersonated_by_user_id != impersonator_id:
                record.revoked_at = datetime.now(UTC)
                await session.commit()
                raise _invalid_refresh_token()
            access_claims["impersonated_by"] = impersonator_id

        issued_refresh = issue_refresh_token(
            session,
            user,
            impersonated_by_user_id=(
                str(impersonator_id) if impersonator_id else None
            ),
            expires_minutes=(
                settings.IMPERSONATION_REFRESH_TOKEN_EXPIRE_MINUTES
                if impersonator_id
                else None
            ),
            family_id=str(family_id),
        )
        await session.flush()
        access = create_access_token(
            subject=user.id,
            tenant_id=user.tenant_id,
            role=user.role.value,
            token_version=user.token_version,
            extra_claims=access_claims,
            expires_minutes=(
                settings.IMPERSONATION_ACCESS_TOKEN_EXPIRE_MINUTES
                if impersonator_id
                else None
            ),
        )
        record.revoked_at = datetime.now(UTC)
        record.replaced_by_id = issued_refresh.token_id
        await session.commit()
        response = JSONResponse(
            TokenResponse(access_token=access, refresh_token=issued_refresh.token).model_dump()
        )
        set_session_cookies(
            response,
            access_token=access,
            refresh_token=issued_refresh.token,
            access_max_age=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            refresh_max_age=max(
                1,
                int(
                    datetime.fromtimestamp(
                        decode_token(issued_refresh.token)["exp"], tz=UTC
                    ).timestamp()
                    - datetime.now(UTC).timestamp()
                ),
            ),
        )
        return response


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(body: RefreshRequest, request: Request) -> Response:
    """Revoke one device login and its descendant refresh tokens."""
    refresh_token = body.refresh_token or request.cookies.get(REFRESH_COOKIE)
    if not refresh_token:
        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        clear_session_cookies(response)
        return response
    try:
        payload = decode_token(refresh_token)
    except Exception as exc:
        raise _invalid_refresh_token() from exc
    if payload.get("type") != "refresh":
        raise _invalid_refresh_token()
    token_id = payload.get("jti")
    family_id = payload.get("family_id")
    if not token_id or not family_id:
        raise _invalid_refresh_token()

    from app.core.database import async_session_factory, clear_tenant_context

    async with async_session_factory() as session:
        await clear_tenant_context(session)
        record = await claim_refresh_token(session, refresh_token, str(token_id))
        if record is None:
            await _revoke_replayed_family(session, payload)
            raise _invalid_refresh_token()
        if (
            record.user_id != payload["sub"]
            or record.family_id != family_id
            or record.expires_at <= datetime.now(UTC)
        ):
            record.revoked_at = datetime.now(UTC)
            await session.commit()
            raise _invalid_refresh_token()

        await revoke_refresh_family(session, str(family_id))
        await record_security_event(
            session,
            action="auth.logout",
            actor_id=str(payload["sub"]),
            target_user_id=str(payload["sub"]),
            tenant_id=record.tenant_id,
            request=request,
            commit=True,
        )
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_session_cookies(response)
    return response


@router.get("/me", response_model=UserRead)
async def me(ctx: AnyUserDep) -> UserRead:
    """Return the current authenticated user (with tenant name)."""
    user, _tenant_id, session = ctx
    return await _user_read_with_nurse(user, session)


@router.patch("/me", response_model=UserRead)
async def update_profile(body: UserProfileUpdate, ctx: AnyUserDep) -> UserRead:
    """Self-service profile update (first/last name)."""
    user, _tenant_id, _session = ctx
    user.first_name = body.first_name
    user.last_name = body.last_name
    return await _user_read_with_nurse(user, _session)


@router.patch("/me/skills", response_model=UserRead)
async def update_my_skills(body: UserSkillUpdate, ctx: AnyUserDep) -> UserRead:
    user, _tenant_id, session = ctx
    if user.role != UserRole.NURSE or not user.nurse_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only nurse users can update their skills")

    skill_ids = list(dict.fromkeys(body.skill_ids))
    skills = (
        await session.execute(select(Skill).where(Skill.id.in_(skill_ids)))
    ).scalars().all()
    if len(skills) != len(skill_ids):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "One or more skills not found")

    await session.execute(
        nurse_skills.delete().where(nurse_skills.c.nurse_id == user.nurse_id)
    )
    if skill_ids:
        await session.execute(
            nurse_skills.insert().values(
                [{"nurse_id": user.nurse_id, "skill_id": skill_id} for skill_id in skill_ids]
            )
        )
    await session.flush()
    await session.commit()
    return await _user_read_with_nurse(user, session)


@router.get("/me/preferences", response_model=list[NursePreferenceRead])
async def list_my_preferences(ctx: AnyUserDep) -> list[NursePreferenceRead]:
    user, _tenant_id, session = ctx
    if user.role != UserRole.NURSE or not user.nurse_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only nurse users can use preferences")
    result = await session.execute(
        select(NursePreference)
        .where(NursePreference.nurse_id == user.nurse_id)
        .order_by(NursePreference.date, NursePreference.shift_template_id)
    )
    return [
        NursePreferenceRead.model_validate(preference)
        for preference in result.scalars()
    ]


@router.post("/me/preferences", response_model=NursePreferenceRead, status_code=status.HTTP_201_CREATED)
async def create_my_preference(
    body: NursePreferenceCreate,
    ctx: AnyUserDep,
) -> NursePreferenceRead:
    user, _tenant_id, session = ctx
    if user.role != UserRole.NURSE or not user.nurse_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only nurse users can use preferences")
    shift = await session.get(ShiftTemplate, body.shift_template_id)
    if not shift:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Shift template not found")

    preference = (
        await session.execute(
            select(NursePreference).where(
                NursePreference.nurse_id == user.nurse_id,
                NursePreference.date == body.date,
                NursePreference.shift_template_id == body.shift_template_id,
            )
        )
    ).scalar_one_or_none()
    if preference:
        preference.request_type = body.request_type
        preference.priority = body.priority
    else:
        preference = NursePreference(
            tenant_id=user.tenant_id,
            nurse_id=user.nurse_id,
            date=body.date,
            shift_template_id=body.shift_template_id,
            request_type=body.request_type,
            priority=body.priority,
        )
        session.add(preference)
    await session.flush()
    await session.commit()
    return NursePreferenceRead.model_validate(preference)


@router.delete("/me/preferences/{preference_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_my_preference(
    preference_id: str,
    ctx: AnyUserDep,
) -> None:
    user, _tenant_id, session = ctx
    if user.role != UserRole.NURSE or not user.nurse_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only nurse users can use preferences")
    preference = await session.get(NursePreference, preference_id)
    if not preference or preference.nurse_id != user.nurse_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Preference not found")
    await session.delete(preference)
    await session.flush()
    await session.commit()


@router.post("/me/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(body: PasswordChange, ctx: AnyUserDep) -> None:
    """Self-service password change (requires old password)."""
    user, _tenant_id, _session = ctx
    if not await run_in_threadpool(verify_password, body.old_password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="旧密码不正确",
        )
    user.hashed_password = await run_in_threadpool(hash_password, body.new_password)
    user.token_version += 1
    await revoke_refresh_tokens_for_user(_session, user.id)
    await record_security_event(
        _session,
        action="auth.password_change",
        actor_id=user.id,
        target_user_id=user.id,
        tenant_id=user.tenant_id,
    )
    await _session.commit()
    return None


@router.post("/me/mfa/setup", response_model=MfaEnrollmentRead)
async def start_mfa_enrollment(ctx: AnyUserDep) -> MfaEnrollmentRead:
    """Generate a fresh TOTP secret (not yet active until confirmed)."""
    user, _tenant_id, session = ctx
    if user.mfa_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="MFA already enabled",
        )
    secret = generate_mfa_secret()
    user.mfa_secret = secret
    await session.commit()
    uri = provisioning_uri(secret, user.email, settings.APP_NAME)
    return MfaEnrollmentRead(
        provisioning_uri=uri,
        qr_code_svg=qr_code_svg(uri),
    )


@router.post("/me/mfa/confirm", response_model=MfaRecoveryCodes)
async def confirm_mfa_enrollment(
    body: MfaEnrollmentConfirm,
    ctx: AnyUserDep,
) -> MfaRecoveryCodes:
    """Activate MFA after verifying the first valid code; issue recovery codes."""
    user, _tenant_id, session = ctx
    if not user.mfa_secret:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="MFA setup not started",
        )
    if user.mfa_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="MFA already enabled",
        )
    if not verify_totp(user.mfa_secret, body.code):
        await record_security_event(
            session,
            action="auth.mfa_confirm",
            outcome="failure",
            actor_id=user.id,
            target_user_id=user.id,
            tenant_id=user.tenant_id,
        )
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid MFA code",
        )
    user.mfa_enabled = True
    codes, digest = generate_recovery_codes()
    user.mfa_recovery_hash = digest
    await record_security_event(
        session,
        action="auth.mfa_confirm",
        actor_id=user.id,
        target_user_id=user.id,
        tenant_id=user.tenant_id,
    )
    await session.commit()
    return MfaRecoveryCodes(codes=codes)


@router.post("/users", response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: UserCreate,
    ctx: TenantAdminDep,
    requested_tenant_id: str | None = Query(None, alias="tenant_id"),
) -> UserRead:
    """Create a new user within the current tenant (tenant_admin only).

    Super-admin can create users in any tenant via the tenants API.
    """
    user, tenant_id, session = ctx
    target_tenant_id = tenant_id
    if user.role == UserRole.SUPER_ADMIN:
        if not requested_tenant_id:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="Super admin must specify tenant_id",
            )
        from app.api.deps import set_tenant_context

        target_tenant_id = requested_tenant_id
        await set_tenant_context(session, target_tenant_id)
    elif target_tenant_id is not None and target_tenant_id != tenant_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail="Cannot access other tenants",
        )
    if not target_tenant_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tenant admin must belong to a tenant",
        )
    if body.role == UserRole.SUPER_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tenant admins cannot create super admins",
        )
    if body.role != UserRole.NURSE and body.nurse_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only nurse users can be linked to a nurse record",
        )

    if await _email_taken(session, body.email):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already registered",
        )
    linked_nurse = None
    if body.role == UserRole.NURSE:
        from app.core.plan_limits import effective_limits_for_tenant
        from app.models.subscription import Subscription

        tenant = await session.get(Tenant, target_tenant_id)
        if not tenant:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Tenant not found")
        sub = (
            await session.execute(
                select(Subscription).where(Subscription.tenant_id == target_tenant_id)
            )
        ).scalar_one_or_none()
        limits = await effective_limits_for_tenant(session, tenant, sub)
        if limits.max_nurses is not None:
            current = (
                await session.execute(
                    select(func.count(Nurse.id)).where(
                    Nurse.tenant_id == target_tenant_id,
                    Nurse.is_available.is_(True),
                    )
                )
            ).scalar_one()
            if current >= limits.max_nurses:
                raise HTTPException(
                    status_code=status.HTTP_402_PAYMENT_REQUIRED,
                    detail=(
                        f"当前套餐最多 {limits.max_nurses} 名护士，"
                        "请升级套餐后再增加。"
                    ),
                )
        if body.nurse_id:
            # Link to an existing nurse record.
            linked_nurse = await session.get(Nurse, body.nurse_id)
            if not linked_nurse:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Nurse not found in this tenant",
                )
            nurse_user = (
                await session.execute(select(User).where(User.nurse_id == body.nurse_id))
            ).scalar_one_or_none()
            if nurse_user:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="This nurse already has a login account",
                )
        else:
            # No nurse_id provided — auto-create a Nurse record from the
            # user's name so the admin doesn't have to visit nurse management
            # separately. The employee_id is derived from the email local-part
            # to stay unique-per-tenant without extra input.
            employee_id = body.email.split("@")[0].upper()
            existing = (
                await session.execute(
                    select(Nurse).where(
                        Nurse.tenant_id == target_tenant_id,
                        Nurse.employee_id == employee_id,
                    )
                )
            ).scalar_one_or_none()
            if existing:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"工号 {employee_id} 已存在，请直接关联该护士",
                )
            linked_nurse = Nurse(
                tenant_id=target_tenant_id,
                employee_id=employee_id,
                first_name=body.first_name,
                last_name=body.last_name,
                is_available=True,
                preferences={},
            )
            session.add(linked_nurse)
            await session.flush()

    new_user = User(
        tenant_id=target_tenant_id,
        email=body.email.lower(),
        hashed_password=hash_password(body.password),
        first_name=body.first_name,
        last_name=body.last_name,
        role=body.role,
        nurse_id=linked_nurse.id if linked_nurse else body.nurse_id,
        is_active=True,
        is_superuser=False,
    )
    session.add(new_user)
    await session.flush()
    await record_security_event(
        session,
        action="user.create",
        actor_id=user.id,
        actor_tenant_id=user.tenant_id,
        target_user_id=new_user.id,
        tenant_id=target_tenant_id,
        details={"role": body.role.value},
    )
    await session.commit()
    return UserRead.model_validate(new_user)


@router.get("/users", response_model=list[UserRead])
async def list_users(
    ctx: TenantAdminDep,
    requested_tenant_id: str | None = Query(None, alias="tenant_id"),
) -> list[UserRead]:
    """List users in the current tenant (tenant_admin only)."""
    user, tenant_id, session = ctx
    target_tenant_id = requested_tenant_id
    if user.role == UserRole.SUPER_ADMIN:
        if not target_tenant_id:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="Super admin must specify tenant_id",
            )
        from app.api.deps import set_tenant_context

        await set_tenant_context(session, target_tenant_id)
        tenant_id = target_tenant_id
    elif target_tenant_id is not None and target_tenant_id != tenant_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail="Cannot access other tenants",
        )
    if not tenant_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tenant admin must belong to a tenant",
        )
    result = await session.execute(
        select(User)
        .where(User.tenant_id == tenant_id)
        .order_by(User.created_at.desc(), User.id)
    )
    return [UserRead.model_validate(user) for user in result.scalars()]


@router.patch("/users/{user_id}", response_model=UserRead)
async def update_user(
    user_id: str,
    body: UserUpdate,
    ctx: TenantAdminDep,
    requested_tenant_id: str | None = Query(None, alias="tenant_id"),
) -> UserRead:
    """Update a user in the current tenant (tenant_admin only)."""
    caller, tenant_id, session = ctx
    target_tenant_id = requested_tenant_id
    if caller.role == UserRole.SUPER_ADMIN:
        if not target_tenant_id:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="Super admin must specify tenant_id",
            )
        from app.api.deps import set_tenant_context

        await set_tenant_context(session, target_tenant_id)
        tenant_id = target_tenant_id
    elif target_tenant_id is not None and target_tenant_id != tenant_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail="Cannot access other tenants",
        )
    if not tenant_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="Tenant admin must belong to a tenant",
        )
    target = await session.get(User, user_id)
    if not target or target.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")

    self_update = target.id == caller.id
    if self_update and body.is_active is not None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="Tenant admins cannot change their own status",
        )

    if target.role != UserRole.NURSE and body.nurse_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="Only nurse users can be linked to a nurse record",
        )
    if (
        "nurse_id" in body.model_fields_set
        and body.nurse_id != target.nurse_id
    ):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="关联护士在创建后不可更改，请停用此账号并为护士创建新账号",
        )

    next_nurse_id = target.nurse_id

    credential_or_status_changed = bool(body.password) or body.is_active is not None
    if body.password:
        target.hashed_password = hash_password(body.password)
    if body.first_name is not None:
        target.first_name = body.first_name
    if body.last_name is not None:
        target.last_name = body.last_name
    target.nurse_id = next_nurse_id
    if body.is_active is not None:
        target.is_active = body.is_active
    if credential_or_status_changed:
        target.token_version += 1
        await revoke_refresh_tokens_for_user(session, target.id)
    if credential_or_status_changed:
        await record_security_event(
            session,
            action="user.update",
            actor_id=caller.id,
            actor_tenant_id=caller.tenant_id,
            target_user_id=target.id,
            tenant_id=tenant_id,
            details={
                "credential_changed": bool(body.password),
                "activation_changed": body.is_active is not None,
            },
        )
    await session.flush()
    await session.commit()
    return UserRead.model_validate(target)


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: str,
    ctx: TenantAdminDep,
    requested_tenant_id: str | None = Query(None, alias="tenant_id"),
) -> None:
    """Delete a user in the current tenant (tenant_admin or super_admin)."""
    caller, tenant_id, session = ctx
    target_tenant_id = requested_tenant_id
    if caller.role == UserRole.SUPER_ADMIN:
        if not target_tenant_id:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="Super admin must specify tenant_id",
            )
        from app.api.deps import set_tenant_context

        await set_tenant_context(session, target_tenant_id)
        tenant_id = target_tenant_id
    elif target_tenant_id is not None and target_tenant_id != tenant_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail="Cannot access other tenants",
        )
    if not tenant_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="Tenant admin must belong to a tenant",
        )
    target = await session.get(User, user_id)
    if not target or target.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")

    if target.id == caller.id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete yourself",
        )
    if (
        caller.role == UserRole.TENANT_ADMIN
        and target.role == UserRole.TENANT_ADMIN
    ):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail="Tenant admins cannot delete other tenant admins",
        )

    await record_security_event(
        session,
        action="user.delete",
        actor_id=caller.id,
        actor_tenant_id=caller.tenant_id,
        target_user_id=target.id,
        tenant_id=tenant_id,
        details={"email": target.email, "role": target.role},
    )
    await session.delete(target)
    await session.commit()


@router.post("/impersonate/{user_id}", response_model=TokenResponse)
async def impersonate(
    user_id: str,
    request: Request,
    ctx: AnyUserDep,
) -> Response:
    """Super-admin masquerade: mint a short-lived access token for another user.

    The minted token carries the TARGET user's identity (sub/tenant/role/name)
    plus an `impersonated_by` claim naming the super-admin, so audit logs can
    distinguish a real session from a masquerade. Only super-admin may call.
    The caller is expected to save its own token client-side before swapping
    in the returned one (the UI does this; see subscriptions/admin page).
    """
    from app.models.enums import UserRole

    caller, _caller_tenant, session = ctx
    if caller.role != UserRole.SUPER_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only super admin may impersonate",
        )
    # Read the target user cross-tenant (caller is super → RLS bypass already set).
    target = await session.get(User, user_id)
    if not target:
        await record_security_event(
            session,
            action="auth.impersonate",
            outcome="denied",
            actor_id=caller.id,
            target_user_id=user_id,
            tenant_id=target.tenant_id if target else None,
            details={"reason": "target_not_found_or_inactive"},
            commit=True,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Target user not found",
        )
    if not target.is_active and not await _pending_admin_can_be_impersonated(
        session, target
    ):
        await record_security_event(
            session,
            action="auth.impersonate",
            outcome="denied",
            actor_id=caller.id,
            target_user_id=user_id,
            tenant_id=target.tenant_id,
            details={"reason": "target_inactive"},
            commit=True,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="目标账号尚未完成邮箱验证/初始设置，不能仿真登录",
        )
    issued_refresh = issue_refresh_token(
        session,
        target,
        impersonated_by_user_id=caller.id,
        expires_minutes=settings.IMPERSONATION_REFRESH_TOKEN_EXPIRE_MINUTES,
    )
    access = create_access_token(
        subject=target.id,
        tenant_id=target.tenant_id,
        role=target.role.value,
        token_version=target.token_version,
        extra_claims={
            "name": f"{target.first_name} {target.last_name}",
            "impersonated_by": caller.id,
            "family_id": issued_refresh.family_id,
            "mfa_enabled": target.mfa_enabled,
        },
        expires_minutes=settings.IMPERSONATION_ACCESS_TOKEN_EXPIRE_MINUTES,
    )
    await record_security_event(
        session,
        action="auth.impersonate",
        actor_id=caller.id,
        target_user_id=target.id,
        tenant_id=target.tenant_id,
        details={"token_ttl_minutes": settings.IMPERSONATION_ACCESS_TOKEN_EXPIRE_MINUTES},
    )
    await session.commit()
    response = JSONResponse(
        TokenResponse(access_token=access, refresh_token=issued_refresh.token).model_dump()
    )
    set_session_cookies(
        response,
        access_token=access,
        refresh_token=issued_refresh.token,
        access_max_age=settings.IMPERSONATION_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        refresh_max_age=settings.IMPERSONATION_REFRESH_TOKEN_EXPIRE_MINUTES * 60,
        impersonator_refresh_token=request.cookies.get(REFRESH_COOKIE),
    )
    return response


@router.post("/impersonation-exit", response_model=TokenResponse)
async def exit_impersonation(
    request: Request,
    ctx: AnyUserDep,
) -> Response:
    """Restore the saved HttpOnly super-admin session and rotate its refresh token."""
    from app.core.database import async_session_factory, clear_tenant_context
    from app.models.enums import UserRole

    saved_refresh = request.cookies.get(IMPERSONATOR_REFRESH_COOKIE)
    if not saved_refresh:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No impersonation restore session",
        )
    try:
        payload = decode_token(saved_refresh)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid impersonation restore session",
        ) from exc
    if payload.get("type") != "refresh" or not payload.get("jti"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid impersonation restore session",
        )
    async with async_session_factory() as restore_session:
        await clear_tenant_context(restore_session)
        record = await claim_refresh_token(
            restore_session, saved_refresh, str(payload["jti"])
        )
        admin = None
        if record:
            admin = (
                await restore_session.execute(
                    select(User).where(User.id == record.user_id)
                )
            ).scalar_one_or_none()
        if (
            record is None
            or admin is None
            or not admin.is_active
            or admin.role != UserRole.SUPER_ADMIN
            or record.expires_at <= datetime.now(UTC)
            or record.token_version != int(payload.get("ver", -1))
        ):
            await restore_session.commit()
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid impersonation restore session",
            )
        issued_refresh = issue_refresh_token(restore_session, admin)
        await restore_session.flush()
        access = create_access_token(
            subject=admin.id,
            tenant_id=admin.tenant_id,
            role=admin.role.value,
            token_version=admin.token_version,
            extra_claims={
                "name": f"{admin.first_name} {admin.last_name}",
                "family_id": issued_refresh.family_id,
                "mfa_enabled": admin.mfa_enabled,
            },
        )
        record.revoked_at = datetime.now(UTC)
        record.replaced_by_id = issued_refresh.token_id
        await restore_session.commit()
    response = JSONResponse(
        TokenResponse(access_token=access, refresh_token=issued_refresh.token).model_dump()
    )
    set_session_cookies(
        response,
        access_token=access,
        refresh_token=issued_refresh.token,
        access_max_age=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        refresh_max_age=settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60,
    )
    return response
