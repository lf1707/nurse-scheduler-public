"""Super-admin tools — masquerade user listing, test-tenant bootstrapping/cleanup.

GET  /admin/tenants/{tenant_id}/users   → list a tenant's users (for the
                                          impersonate picker)
GET  /admin/test-tenant                 → list throwaway tenants and resource
                                          counts
POST /admin/test-tenant                 → create a throwaway tenant with an
                                          admin + active subscription + sample
                                          nurses/shifts/rules, return creds
DELETE /admin/test-tenant/{tenant_id}   → cascade-delete a test tenant
"""

from __future__ import annotations

import csv
import io
import json
import secrets
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from datetime import UTC, datetime, time
from pathlib import Path
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import CursorResult, delete, func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.api.deps import SuperAdminDep, set_tenant_context
from app.api.v1.tenants import TENANT_SCOPED_TABLES
from app.core.audit import record_security_event
from app.core.audit_export import get_export_activity_at, get_watermark
from app.core.audit_partitions import list_monthly_partitions
from app.core.config import settings
from app.core.host_ops import queue_beat_restart, read_host_disk_usage
from app.core.plan_limits import (
    PLAN_LIMITS_SETTING_KEY,
    SYSTEM_PLAN_KEYS,
    configured_plan_definitions,
    configured_plan_limits,
)
from app.core.refresh_tokens import revoke_refresh_tokens_for_user
from app.core.security import hash_password
from app.core.site import default_about_text
from app.models.enums import SubscriptionType, UserRole
from app.models.nurse import Nurse, Role, Skill, nurse_roles, nurse_skills
from app.models.platform import AuditLegalHold, PlatformSetting, SecurityAuditEvent
from app.models.rule import (
    ShiftSequenceRule,
    ShiftSequenceStep,
    SkillMixRequirement,
    SkillMixRule,
)
from app.models.shift import DayGroup, DayGroupDay, ShiftTemplate
from app.models.subscription import Subscription
from app.models.tenant import Tenant
from app.models.tenant_application import TenantApplication
from app.models.user import User
from app.schemas import (
    CustomPlanConfig,
    Paginated,
    PlanLimitsConfig,
    PlanLimitValues,
    SecurityAuditEventRead,
    SubscriptionPlan,
    UserRead,
)
from app.tasks.celery_app import celery_app
from scripts.seed_demo_data import ensure_demo_dataset

router = APIRouter(prefix="/admin", tags=["admin"])

TEST_TENANTS_SETTING_KEY = "test_tenants_enabled"
CONTACT_EMAIL_KEY = "contact_email"
ABOUT_TEXT_KEY = "about_text"
AUTO_APPROVE_KEY = "tenant_application_auto_approve"
AUTO_PLAN_KEY = "tenant_application_auto_plan"
ANOMALY_SCAN_SCHEDULE_KEY = "anomaly_scan_schedule_seconds"


class ContactEmailRead(BaseModel):
    email: str | None


class ContactEmailUpdate(BaseModel):
    email: str | None = None


class SiteInfoRead(BaseModel):
    contact_email: str | None = None
    about_text: str | None = None


class SiteInfoUpdate(BaseModel):
    contact_email: str | None = None
    about_text: str | None = None


class AutoApproveRead(BaseModel):
    auto_approve: bool
    auto_plan: str


class AutoApproveUpdate(BaseModel):
    auto_approve: bool
    auto_plan: str = Field(default="free", pattern="^(free|pro)$")


class AnomalyScanScheduleRead(BaseModel):
    schedule_seconds: int


class AnomalyScanScheduleUpdate(BaseModel):
    schedule_seconds: int = Field(..., ge=0)


class AuditExportStatus(BaseModel):
    status: str
    hot_retention_days: int
    cold_retention_days: int
    last_exported_at: datetime | None
    last_export_id: str | None
    event_count: int | None
    artifact_name: str | None
    artifact_exists: bool
    upload_confirmed: bool
    age_hours: float | None
    maximum_lag_hours: int


class DiskStatus(BaseModel):
    level: str
    path: str
    used_percent: float
    free_gb: float
    warn_percent: int
    alert_percent: int
    critical_percent: int
    minimum_free_gb: float
    checked_at: datetime


class OpsStatus(BaseModel):
    audit_export: AuditExportStatus
    disk: DiskStatus


class AuditExportRunAccepted(BaseModel):
    task_id: str
    state: str = "queued"


class AuditExportTaskStatus(BaseModel):
    task_id: str
    state: str
    ready: bool
    successful: bool
    detail: str | None = None


class AuditLegalHoldCreate(BaseModel):
    partition_name: str = Field(
        pattern=r"^security_audit_events_[0-9]{4}_(0[1-9]|1[0-2])$"
    )
    reason: str = Field(min_length=1, max_length=2000)


class AuditLegalHoldRead(BaseModel):
    id: int
    partition_name: str
    reason: str
    created_by: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


@router.get("/audit-events", response_model=Paginated[SecurityAuditEventRead])
async def list_security_audit_events(
    ctx: SuperAdminDep,
    action: str | None = Query(None, max_length=100),
    outcome: str | None = Query(None, pattern="^(success|failure|denied)$"),
    actor_id: str | None = Query(None, min_length=1, max_length=36),
    target_user_id: str | None = Query(None, min_length=1, max_length=36),
    tenant_id: str | None = Query(None, min_length=1, max_length=36),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
) -> Paginated[SecurityAuditEventRead]:
    """List append-only security events for incident investigation."""

    _user, _own_tenant_id, session = ctx
    stmt = select(SecurityAuditEvent)
    if action:
        stmt = stmt.where(SecurityAuditEvent.action == action)
    if outcome:
        stmt = stmt.where(SecurityAuditEvent.outcome == outcome)
    if actor_id:
        stmt = stmt.where(SecurityAuditEvent.actor_id == actor_id)
    if target_user_id:
        stmt = stmt.where(SecurityAuditEvent.target_user_id == target_user_id)
    if tenant_id:
        stmt = stmt.where(SecurityAuditEvent.tenant_id == tenant_id)

    total = (
        await session.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one()
    rows = (
        await session.execute(
            stmt.order_by(SecurityAuditEvent.created_at.desc(), SecurityAuditEvent.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return Paginated(
        items=[SecurityAuditEventRead.model_validate(row) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/ops/status", response_model=OpsStatus)
async def get_ops_status(ctx: SuperAdminDep) -> OpsStatus:
    """Expose archive freshness and filesystem pressure to super admins."""
    _user, _tenant_id, session = ctx

    watermark = await get_watermark(session)
    age_hours: float | None = None
    artifact_path = Path(watermark.artifact_path) if watermark and watermark.artifact_path else None
    manifest_path = (
        Path(str(artifact_path).replace(".enc", ".manifest.json"))
        if artifact_path
        else None
    )
    marker_path = Path(f"{manifest_path}.uploaded") if manifest_path else None
    if watermark:
        age_hours = max(
            0.0,
            (datetime.now(UTC) - get_export_activity_at(watermark)).total_seconds() / 3600,
        )
    export_status = "stale" if (
        watermark is None or age_hours is None or age_hours > settings.AUDIT_EXPORT_MAX_LAG_HOURS
    ) else "ok"

    usage = read_host_disk_usage()
    used_percent = round(usage.used / usage.total * 100, 1)
    free_gb = round(usage.free / (1024**3), 2)
    if used_percent >= settings.DISK_USAGE_CRITICAL_PERCENT or free_gb <= settings.DISK_MIN_FREE_GB:
        disk_level = "critical"
    elif used_percent >= settings.DISK_USAGE_ALERT_PERCENT:
        disk_level = "alert"
    elif used_percent >= settings.DISK_USAGE_WARN_PERCENT:
        disk_level = "warn"
    else:
        disk_level = "none"

    return OpsStatus(
        audit_export=AuditExportStatus(
            status=export_status,
            hot_retention_days=settings.AUDIT_RETENTION_DAYS_HOT,
            cold_retention_days=settings.AUDIT_RETENTION_DAYS_COLD,
            last_exported_at=watermark.last_exported_at if watermark else None,
            last_export_id=watermark.last_export_id if watermark else None,
            event_count=watermark.event_count if watermark else None,
            artifact_name=artifact_path.name if artifact_path else None,
            artifact_exists=bool(artifact_path and artifact_path.is_file()),
            upload_confirmed=bool(marker_path and marker_path.is_file()),
            age_hours=age_hours,
            maximum_lag_hours=settings.AUDIT_EXPORT_MAX_LAG_HOURS,
        ),
        disk=DiskStatus(
            level=disk_level,
            path=usage.path,
            used_percent=used_percent,
            free_gb=free_gb,
            warn_percent=settings.DISK_USAGE_WARN_PERCENT,
            alert_percent=settings.DISK_USAGE_ALERT_PERCENT,
            critical_percent=settings.DISK_USAGE_CRITICAL_PERCENT,
            minimum_free_gb=settings.DISK_MIN_FREE_GB,
            checked_at=usage.checked_at,
        ),
    )


@router.post(
    "/ops/anomaly-scan",
    response_model=AuditExportRunAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def run_anomaly_scan(ctx: SuperAdminDep) -> AuditExportRunAccepted:
    """Queue a manual audit anomaly scan."""
    user, _tenant_id, session = ctx
    try:
        result = celery_app.send_task("audit.run_anomaly_scan")
    except Exception as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="任务队列不可用，无法提交异常扫描",
        ) from exc

    await record_security_event(
        session,
        action="audit.anomaly_scan.triggered",
        actor_id=user.id,
        actor_tenant_id=user.tenant_id,
        tenant_id=user.tenant_id,
        details={"task_id": result.id},
    )
    return AuditExportRunAccepted(task_id=result.id)


@router.get("/ops/anomaly-scan/tasks/{task_id}", response_model=AuditExportTaskStatus)
async def get_anomaly_scan_task(
    task_id: str,
    ctx: SuperAdminDep,
) -> AuditExportTaskStatus:
    """Inspect a queued anomaly-scan task through the Celery result backend."""
    _user, _tenant_id, _session = ctx
    result = celery_app.AsyncResult(task_id)
    detail = None
    if result.failed():
        detail = str(result.result)
    elif result.successful():
        payload = result.result
        detail = json.dumps(payload, ensure_ascii=False, default=str) if payload else None
    return AuditExportTaskStatus(
        task_id=task_id,
        state=result.state,
        ready=result.ready(),
        successful=result.successful(),
        detail=detail,
    )


@router.get("/audit-events/export.csv")
async def export_security_audit_events(
    ctx: SuperAdminDep,
    action: str | None = Query(None, max_length=100),
    outcome: str | None = Query(None, pattern="^(success|failure|denied)$"),
    actor_id: str | None = Query(None, min_length=1, max_length=36),
    target_user_id: str | None = Query(None, min_length=1, max_length=36),
    tenant_id: str | None = Query(None, min_length=1, max_length=36),
    limit: int = Query(10_000, ge=1, le=50_000),
) -> StreamingResponse:
    """Download the selected security events as UTF-8 CSV."""
    user, _own_tenant_id, session = ctx
    stmt = select(SecurityAuditEvent)
    if action:
        stmt = stmt.where(SecurityAuditEvent.action == action)
    if outcome:
        stmt = stmt.where(SecurityAuditEvent.outcome == outcome)
    if actor_id:
        stmt = stmt.where(SecurityAuditEvent.actor_id == actor_id)
    if target_user_id:
        stmt = stmt.where(SecurityAuditEvent.target_user_id == target_user_id)
    if tenant_id:
        stmt = stmt.where(SecurityAuditEvent.tenant_id == tenant_id)
    stmt = stmt.order_by(SecurityAuditEvent.created_at.desc(), SecurityAuditEvent.id).limit(limit)

    await record_security_event(
        session,
        action="audit.events.exported",
        actor_id=user.id,
        actor_tenant_id=user.tenant_id,
        tenant_id=user.tenant_id,
        details={
            "action_filter": action,
            "outcome_filter": outcome,
            "limit": limit,
        },
        commit=True,
    )

    async def _rows() -> AsyncIterator[str]:
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer)
        writer.writerow([
            "id", "created_at", "action", "outcome", "actor_id",
            "actor_tenant_id", "target_user_id", "tenant_id", "ip_address",
            "user_agent", "details",
        ])
        yield buffer.getvalue()
        result = await session.stream(stmt)
        async for row in result:
            event = row[0]
            buffer.seek(0)
            buffer.truncate()
            writer.writerow([
                event.id,
                event.created_at.isoformat(),
                event.action,
                event.outcome,
                event.actor_id,
                event.actor_tenant_id,
                event.target_user_id,
                event.tenant_id,
                event.ip_address,
                event.user_agent,
                json.dumps(event.details, ensure_ascii=False, sort_keys=True),
            ])
            yield buffer.getvalue()

    filename = f"security-audit-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.csv"
    return StreamingResponse(
        _rows(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post(
    "/audit-exports/run",
    response_model=AuditExportRunAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def run_audit_export(ctx: SuperAdminDep) -> AuditExportRunAccepted:
    """Queue an incremental compressed/encrypted audit export."""
    user, _tenant_id, session = ctx
    try:
        result = celery_app.send_task("audit.export_events")
    except Exception as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="任务队列不可用，无法提交审计归档",
        ) from exc

    await record_security_event(
        session,
        action="audit.export.triggered",
        actor_id=user.id,
        actor_tenant_id=user.tenant_id,
        tenant_id=user.tenant_id,
        details={"task_id": result.id},
    )
    return AuditExportRunAccepted(task_id=result.id)


@router.get("/audit-exports/tasks/{task_id}", response_model=AuditExportTaskStatus)
async def get_audit_export_task(
    task_id: str,
    ctx: SuperAdminDep,
) -> AuditExportTaskStatus:
    """Inspect a queued audit-export task through the Celery result backend."""
    _user, _tenant_id, _session = ctx
    result = celery_app.AsyncResult(task_id)
    detail = None
    if result.failed():
        detail = str(result.result)
    elif result.successful():
        payload = result.result
        detail = json.dumps(payload, ensure_ascii=False, default=str) if payload else None
    return AuditExportTaskStatus(
        task_id=task_id,
        state=result.state,
        ready=result.ready(),
        successful=result.successful(),
        detail=detail,
    )


@router.post(
    "/audit-retention/run",
    response_model=AuditExportRunAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def run_audit_retention(ctx: SuperAdminDep) -> AuditExportRunAccepted:
    """Queue verified, policy-driven expiry of audit-event partitions."""
    user, _tenant_id, session = ctx
    try:
        result = celery_app.send_task("audit.expire_old_partitions")
    except Exception as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="任务队列不可用，无法提交保留清理",
        ) from exc

    await record_security_event(
        session,
        action="audit.retention.triggered",
        actor_id=user.id,
        actor_tenant_id=user.tenant_id,
        tenant_id=user.tenant_id,
        details={"task_id": result.id},
    )
    return AuditExportRunAccepted(task_id=result.id)


@router.get("/audit-legal-holds", response_model=list[AuditLegalHoldRead])
async def list_audit_legal_holds(ctx: SuperAdminDep) -> list[AuditLegalHoldRead]:
    """List legal holds that prevent audit partitions from expiring."""
    _user, _tenant_id, session = ctx
    result = await session.execute(
        select(AuditLegalHold).order_by(AuditLegalHold.created_at.desc(), AuditLegalHold.id.desc())
    )
    return [
        AuditLegalHoldRead.model_validate(hold, from_attributes=True)
        for hold in result.scalars()
    ]


@router.get("/audit-partitions", response_model=list[str])
async def list_audit_partitions(ctx: SuperAdminDep) -> list[str]:
    """List monthly audit partitions available for legal holds."""
    _user, _tenant_id, session = ctx
    return await list_monthly_partitions(session)


@router.post(
    "/audit-legal-holds",
    response_model=AuditLegalHoldRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_audit_legal_hold(
    payload: AuditLegalHoldCreate,
    ctx: SuperAdminDep,
) -> AuditLegalHoldRead:
    """Place a legal hold on a monthly audit partition."""
    user, _tenant_id, session = ctx
    existing = await session.execute(
        select(AuditLegalHold).where(
            AuditLegalHold.partition_name == payload.partition_name
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status.HTTP_409_CONFLICT, detail="该审计分区已有 legal hold")

    hold = AuditLegalHold(
        partition_name=payload.partition_name,
        reason=payload.reason,
        created_by=user.id,
    )
    session.add(hold)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail="该审计分区已有 legal hold") from exc

    await session.refresh(hold)

    await record_security_event(
        session,
        action="audit.legal_hold.created",
        actor_id=user.id,
        actor_tenant_id=user.tenant_id,
        tenant_id=user.tenant_id,
        details={"partition_name": payload.partition_name},
    )
    return AuditLegalHoldRead.model_validate(hold, from_attributes=True)


@router.delete("/audit-legal-holds/{hold_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_audit_legal_hold(hold_id: int, ctx: SuperAdminDep) -> None:
    """Release a legal hold after confirming retention approval."""
    user, _tenant_id, session = ctx
    hold = await session.get(AuditLegalHold, hold_id)
    if hold is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Legal hold 不存在")

    partition_name = hold.partition_name
    await session.delete(hold)
    await record_security_event(
        session,
        action="audit.legal_hold.deleted",
        actor_id=user.id,
        actor_tenant_id=user.tenant_id,
        tenant_id=user.tenant_id,
        details={"partition_name": partition_name},
    )


async def _get_setting(session: AsyncSession, key: str) -> str | None:
    row = await session.get(PlatformSetting, key)
    return str(row.value) if row else None


async def _upsert_setting(session: AsyncSession, key: str, value: Any) -> None:
    row = await session.get(PlatformSetting, key)
    if row is None:
        session.add(PlatformSetting(key=key, value=value))
    else:
        row.value = value


async def _delete_setting(session: AsyncSession, key: str) -> None:
    row = await session.get(PlatformSetting, key)
    if row:
        await session.delete(row)


@router.get("/contact", response_model=ContactEmailRead)
async def get_contact_email(ctx: SuperAdminDep) -> ContactEmailRead:
    user, _tenant_id, session = ctx
    return ContactEmailRead(email=await _get_setting(session, CONTACT_EMAIL_KEY))


@router.put("/contact", response_model=ContactEmailRead)
async def update_contact_email(
    body: ContactEmailUpdate,
    ctx: SuperAdminDep,
) -> ContactEmailRead:
    _user, _tenant_id, session = ctx
    await _upsert_setting(session, CONTACT_EMAIL_KEY, body.email)
    await session.commit()
    return ContactEmailRead(email=body.email)


@router.get("/site-info", response_model=SiteInfoRead)
async def get_site_info(ctx: SuperAdminDep) -> SiteInfoRead:
    _user, _tenant_id, session = ctx
    return SiteInfoRead(
        contact_email=await _get_setting(session, CONTACT_EMAIL_KEY),
        about_text=(
            await _get_setting(session, ABOUT_TEXT_KEY)
            or default_about_text(settings.APP_NAME)
        ),
    )


@router.post("/users/{user_id}/mfa/reset", status_code=status.HTTP_204_NO_CONTENT)
async def reset_user_mfa(user_id: str, ctx: SuperAdminDep) -> None:
    """Disable MFA for a user after a verified administrator-assisted reset."""
    caller, _tenant_id, session = ctx
    target = await session.get(User, user_id)
    if not target:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if not target.mfa_enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "MFA is not enabled")

    target.mfa_enabled = False
    target.mfa_secret = None
    target.mfa_recovery_hash = None
    target.token_version += 1
    await revoke_refresh_tokens_for_user(session, target.id)
    await record_security_event(
        session,
        action="auth.mfa.reset",
        actor_id=caller.id,
        target_user_id=target.id,
        tenant_id=target.tenant_id,
    )
    await session.commit()
    return None


@router.put("/site-info", response_model=SiteInfoRead)
async def update_site_info(
    body: SiteInfoUpdate,
    ctx: SuperAdminDep,
) -> SiteInfoRead:
    user, _tenant_id, session = ctx
    fields_set = body.model_fields_set
    if "contact_email" in fields_set:
        if body.contact_email:
            await _upsert_setting(session, CONTACT_EMAIL_KEY, body.contact_email)
        else:
            await _delete_setting(session, CONTACT_EMAIL_KEY)
    if "about_text" in fields_set:
        if body.about_text:
            await _upsert_setting(session, ABOUT_TEXT_KEY, body.about_text)
        else:
            await _delete_setting(session, ABOUT_TEXT_KEY)
    await record_security_event(
        session,
        action="admin.site_info_update",
        actor_id=user.id,
        details={
            "contact_email_changed": "contact_email" in fields_set,
            "about_text_changed": "about_text" in fields_set,
        },
    )
    await session.commit()
    return SiteInfoRead(
        contact_email=await _get_setting(session, CONTACT_EMAIL_KEY),
        about_text=(
            await _get_setting(session, ABOUT_TEXT_KEY)
            or default_about_text(settings.APP_NAME)
        ),
    )


@router.get("/auto-approve", response_model=AutoApproveRead)
async def get_auto_approve_settings(ctx: SuperAdminDep) -> AutoApproveRead:
    _user, _tenant_id, session = ctx
    auto_row = await session.get(PlatformSetting, AUTO_APPROVE_KEY)
    plan_row = await session.get(PlatformSetting, AUTO_PLAN_KEY)
    return AutoApproveRead(
        auto_approve=bool(auto_row.value) if auto_row else False,
        auto_plan=str(plan_row.value) if plan_row else "free",
    )


@router.put("/auto-approve", response_model=AutoApproveRead)
async def update_auto_approve_settings(
    body: AutoApproveUpdate,
    ctx: SuperAdminDep,
) -> AutoApproveRead:
    user, _tenant_id, session = ctx
    active_plans = await configured_plan_limits(session)
    if body.auto_plan not in active_plans:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="自动开通套餐不存在",
        )
    await _upsert_setting(session, AUTO_APPROVE_KEY, body.auto_approve)
    await _upsert_setting(session, AUTO_PLAN_KEY, body.auto_plan)
    await record_security_event(
        session,
        action="admin.auto_approve_settings",
        actor_id=user.id,
        details={"auto_approve": body.auto_approve, "auto_plan": body.auto_plan},
    )
    await session.commit()
    return AutoApproveRead(auto_approve=body.auto_approve, auto_plan=body.auto_plan)


@router.get(
    "/plan-limits",
    response_model=PlanLimitsConfig,
    response_model_exclude_none=True,
)
async def get_plan_limits(ctx: SuperAdminDep) -> PlanLimitsConfig:
    """Return effective subscription limits configured for the platform."""
    _user, _tenant_id, session = ctx
    configured, deleted_system = await configured_plan_definitions(session)
    custom = [
        CustomPlanConfig(
            key=definition.key,
            name=definition.name,
            max_nurses=definition.max_nurses,
            max_period_days=definition.max_period_days,
        )
        for definition in configured.values()
        if not definition.is_system
    ]
    system_limits = {
        plan: PlanLimitValues(
            max_nurses=limits.max_nurses,
            max_period_days=limits.max_period_days,
        )
        for plan, limits in configured.items()
        if limits.is_system
    }
    return PlanLimitsConfig(
        **system_limits,
        deleted_system=sorted(deleted_system),
        custom=custom,
    )


@router.put(
    "/plan-limits",
    response_model=PlanLimitsConfig,
    response_model_exclude_none=True,
)
async def update_plan_limits(
    body: PlanLimitsConfig,
    ctx: SuperAdminDep,
) -> PlanLimitsConfig:
    """Configure the nurse and schedule-period limits for every plan."""
    user, _tenant_id, session = ctx
    configured, previous_deleted = await configured_plan_definitions(session)
    previous_keys = set(configured)
    custom_keys = {item.key for item in body.custom}
    invalid_deleted = set(body.deleted_system) - SYSTEM_PLAN_KEYS
    declared_system = {
        key for key in SYSTEM_PLAN_KEYS if getattr(body, key) is not None
    }
    if invalid_deleted:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="只能删除内置套餐",
        )
    if set(body.deleted_system) & declared_system:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="套餐不能同时处于启用和删除状态",
        )
    missing_decision = SYSTEM_PLAN_KEYS - declared_system - set(body.deleted_system)
    if missing_decision:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="内置套餐必须提供限额或标记为删除",
        )
    removed_keys = (previous_keys | previous_deleted) - declared_system - set(custom_keys)
    if removed_keys:
        referenced = await session.execute(
            select(Subscription.tenant_id).where(Subscription.plan.in_(removed_keys)).limit(1)
        )
        if referenced.scalar_one_or_none() is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="仍有租户使用待删除套餐，请先更换租户套餐。",
            )
    conflicts = custom_keys & SYSTEM_PLAN_KEYS
    if conflicts:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"自定义套餐 key 不能使用内置 key：{', '.join(sorted(conflicts))}",
        )
    if len(custom_keys) != len(body.custom):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="自定义套餐 key 不能重复",
        )
    await _upsert_setting(session, PLAN_LIMITS_SETTING_KEY, body.model_dump_json())
    await record_security_event(
        session,
        action="admin.plan_limits_update",
        actor_id=user.id,
        details=body.model_dump(),
    )
    await session.commit()
    return body


@router.get("/anomaly-scan-schedule", response_model=AnomalyScanScheduleRead)
async def get_anomaly_scan_schedule(ctx: SuperAdminDep) -> AnomalyScanScheduleRead:
    _user, _tenant_id, session = ctx
    row = await session.get(PlatformSetting, ANOMALY_SCAN_SCHEDULE_KEY)
    if row is not None:
        return AnomalyScanScheduleRead(schedule_seconds=int(str(row.value)))
    return AnomalyScanScheduleRead(schedule_seconds=settings.ANOMALY_SCAN_SCHEDULE_SECONDS)


@router.put("/anomaly-scan-schedule", response_model=AnomalyScanScheduleRead)
async def update_anomaly_scan_schedule(
    body: AnomalyScanScheduleUpdate,
    ctx: SuperAdminDep,
) -> AnomalyScanScheduleRead:
    user, _tenant_id, session = ctx
    await _upsert_setting(session, ANOMALY_SCAN_SCHEDULE_KEY, body.schedule_seconds)
    await record_security_event(
        session,
        action="admin.anomaly_scan_schedule_update",
        actor_id=user.id,
        details={"schedule_seconds": body.schedule_seconds},
    )
    await session.commit()
    return AnomalyScanScheduleRead(schedule_seconds=body.schedule_seconds)


@router.post("/ops/restart-beat", status_code=status.HTTP_202_ACCEPTED)
async def restart_beat(ctx: SuperAdminDep) -> dict[str, str]:
    """Queue an approved host-side restart of the Celery beat service."""
    user, _tenant_id, session = ctx
    request_id = await queue_beat_restart(user.id)

    await record_security_event(
        session,
        action="admin.beat_restart",
        actor_id=user.id,
        details={"request_id": request_id},
    )
    await session.commit()
    return {"status": "queued", "request_id": request_id}


async def get_test_tenants_enabled(session: AsyncSession) -> bool:
    setting = await session.get(PlatformSetting, TEST_TENANTS_SETTING_KEY)
    if setting is None:
        return not settings.is_prod or settings.ENABLE_TEST_TENANTS
    return bool(setting.value)


async def ensure_test_tenants_enabled(session: AsyncSession) -> None:
    if not await get_test_tenants_enabled(session):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="生产环境已禁用测试租户管理",
        )


@router.get("/tenants/{tenant_id}/users", response_model=list[UserRead])
async def list_tenant_users(tenant_id: str, ctx: SuperAdminDep) -> list[UserRead]:
    """List all tenant users including not-yet-activated admins (super admin)."""
    user, _own_tenant, session = ctx
    tenant = await session.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    # Switch RLS to the tenant so users (tenant-scoped) are visible.
    await set_tenant_context(session, tenant_id)
    result = await session.execute(select(User).where(User.tenant_id == tenant_id))
    return [UserRead.model_validate(user) for user in result.scalars()]


class TestTenantToggleRead(BaseModel):
    enabled: bool
    environment: str
    demo_enabled: bool


class TestTenantToggleUpdate(BaseModel):
    enabled: bool


@router.get("/test-tenant/enabled", response_model=TestTenantToggleRead)
async def get_test_tenant_toggle(ctx: SuperAdminDep) -> TestTenantToggleRead:
    _user, _tenant_id, session = ctx
    return TestTenantToggleRead(
        enabled=await get_test_tenants_enabled(session),
        environment=settings.APP_ENV,
        demo_enabled=settings.ENABLE_DEMO_TENANT,
    )


@router.put("/test-tenant/enabled", response_model=TestTenantToggleRead)
async def update_test_tenant_toggle(
    body: TestTenantToggleUpdate,
    ctx: SuperAdminDep,
) -> TestTenantToggleRead:
    user, _tenant_id, session = ctx
    setting = await session.get(PlatformSetting, TEST_TENANTS_SETTING_KEY)
    if setting is None:
        setting = PlatformSetting(
            key=TEST_TENANTS_SETTING_KEY,
            value=body.enabled,
        )
        session.add(setting)
    else:
        setting.value = body.enabled
    await record_security_event(
        session,
        action="admin.test_tenants_toggle",
        actor_id=user.id,
        details={"enabled": body.enabled},
    )
    await session.commit()
    return TestTenantToggleRead(
        enabled=body.enabled,
        environment=settings.APP_ENV,
        demo_enabled=settings.ENABLE_DEMO_TENANT,
    )


class TestTenantCreate(BaseModel):
    """User-selected seed dimensions for a throwaway tenant."""

    nurse_count: int = Field(default=6, ge=1, le=200)
    role_count: int = Field(default=2, ge=1, le=50)
    day_group_count: int = Field(default=1, ge=1, le=7)
    shift_count: int = Field(default=3, ge=1, le=24)
    skill_mix_rule_count: int = Field(default=1, ge=1, le=3)
    shift_sequence_rule_count: int = Field(default=1, ge=0, le=5)
    plan: str = Field(default="pro", max_length=30, pattern=r"^[a-z][a-z0-9_-]{0,29}$")


class TestTenantCreds(BaseModel):
    """Returned after bootstrapping a test tenant — enough to log in and explore."""

    tenant_id: str
    tenant_name: str
    slug: str
    tenant_kind: str
    created_at: datetime
    admin_email: str
    admin_password: str | None
    password_source: str | None
    admin_user_id: str
    subscription_id: str
    nurse_count: int
    role_count: int
    skill_count: int
    day_group_count: int
    shift_count: int
    skill_mix_rule_count: int
    shift_sequence_rule_count: int
    nurse_email_prefix: str
    nurse_password: str | None


class DemoTenantCreateResponse(BaseModel):
    tenant_id: str
    tenant_name: str
    slug: str
    admin_email: str
    password_source: str
    plan: SubscriptionPlan = SubscriptionPlan.DEMO
    plan_locked: bool = True


class DeletedTestTenant(BaseModel):
    tenant_id: str
    slug: str
    name: str


class TestTenantCleanupResponse(BaseModel):
    deleted_test_tenants: list[DeletedTestTenant]
    deleted_test_applications: int
    deleted_orphan_rows: dict[str, int]
    total_deleted_orphan_rows: int


TEST_APPLICATION_EMAIL_PATTERNS = (
    "tenant-e2e-%@example.com",
    "tenant-reject-%@example.com",
    "auto-approve-%@example.com",
)
TEST_TENANT_SLUG_PREFIXES = (
    "b39-",
    "b211-",
    "b310-",
    "b311-",
    "e2e-api-",
    "e2e-sub-",
    "e2e-edit-",
    "e2e-seq-",
    "plan-limits-",
    "test-",
    "rls",
    "uc",
    "x-",
    "email-",
    "delete-",
    "stale-",
    "pending-",
    "sec-",
    "super-ctx-",
    "provider-state-",
    "billing-",
)


def is_test_tenant(tenant: Tenant) -> bool:
    return tenant.slug.startswith(TEST_TENANT_SLUG_PREFIXES) or tenant.settings.get("kind") in {
        "demo",
        "test",
    }


def _test_tenant_slug_filters() -> list[Any]:
    return [
        Tenant.slug.like(f"{prefix}%")
        for prefix in TEST_TENANT_SLUG_PREFIXES
    ]


def is_demo_tenant(tenant: Tenant) -> bool:
    return tenant.slug == "demo" or tenant.settings.get("dataset") == "demo"


async def _delete_tenant_rows(session: AsyncSession, tenant_id: str) -> None:
    await session.execute(
        text("UPDATE users SET nurse_id = NULL WHERE tenant_id = :tenant_id"),
        {"tenant_id": tenant_id},
    )
    for table_name in TENANT_SCOPED_TABLES:
        await session.execute(
            text(f"DELETE FROM {table_name} WHERE tenant_id = :tenant_id"),
            {"tenant_id": tenant_id},
        )


async def _delete_orphan_tenant_rows(session: AsyncSession) -> dict[str, int]:
    deleted: dict[str, int] = {}
    for table_name in TENANT_SCOPED_TABLES:
        result = await session.execute(
            text(
                f"DELETE FROM {table_name} WHERE NOT EXISTS ("
                "SELECT 1 FROM tenants WHERE tenants.id = "
                f"{table_name}.tenant_id) AND {table_name}.tenant_id IS NOT NULL"
            )
        )
        deleted[table_name] = cast(CursorResult[Any], result).rowcount or 0
    return deleted


@router.post(
    "/test-tenant/demo",
    response_model=DemoTenantCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_demo_tenant(ctx: SuperAdminDep) -> DemoTenantCreateResponse:
    """Create or repair the standard demo tenant from the simulation console."""
    user, _tenant_id, session = ctx
    await ensure_test_tenants_enabled(session)
    if not settings.ENABLE_DEMO_TENANT:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Demo tenant is disabled (ENABLE_DEMO_TENANT=false)",
        )
    tenant, admin = await ensure_demo_dataset(session)
    await record_security_event(
        session,
        action="admin.demo_tenant_create",
        actor_id=user.id,
        tenant_id=tenant.id,
        target_user_id=admin.id if admin else None,
    )
    return DemoTenantCreateResponse(
        tenant_id=tenant.id,
        tenant_name=tenant.name,
        slug=tenant.slug,
        admin_email=admin.email if admin else "demo-admin@nurse-scheduler.dev",
        password_source="environment_variable:DEMO_TENANT_PASSWORD",
        plan=SubscriptionPlan.DEMO,
        plan_locked=True,
    )


@router.post("/test-tenant/cleanup", response_model=TestTenantCleanupResponse)
async def cleanup_test_data(ctx: SuperAdminDep) -> TestTenantCleanupResponse:
    """Delete generated test tenants, their applications, and orphan rows."""
    user, _own_tenant, session = ctx
    await ensure_test_tenants_enabled(session)

    test_applications = (
        await session.execute(
            select(TenantApplication).where(
                or_(
                    *(
                        TenantApplication.contact_email.ilike(pattern)
                        for pattern in TEST_APPLICATION_EMAIL_PATTERNS
                    )
                )
            )
        )
    ).scalars().all()
    application_tenant_ids = {
        application.created_tenant_id
        for application in test_applications
        if application.created_tenant_id
    }
    application_tenants = (
        (
            await session.execute(
                select(Tenant).where(Tenant.id.in_(application_tenant_ids))
            )
        ).scalars().all()
        if application_tenant_ids
        else []
    )
    labeled_tenants = (
        await session.execute(
            select(Tenant).where(
                or_(
                    *_test_tenant_slug_filters(),
                    Tenant.settings["kind"].as_string().in_(["test"]),
                )
            )
        )
    ).scalars().all()
    tenants_by_id = {
        tenant.id: tenant
        for tenant in [*labeled_tenants, *application_tenants]
        if not is_demo_tenant(tenant)
    }

    delete_result = await session.execute(
        delete(TenantApplication).where(
            TenantApplication.id.in_(
                [application.id for application in test_applications]
            )
        )
    )
    deleted_applications = (
        cast(CursorResult[Any], delete_result).rowcount or 0
    )

    deleted_tenants: list[DeletedTestTenant] = []
    for tenant in tenants_by_id.values():
        await _delete_tenant_rows(session, tenant.id)
        await session.delete(tenant)
        deleted_tenants.append(
            DeletedTestTenant(tenant_id=tenant.id, slug=tenant.slug, name=tenant.name)
        )

    await session.flush()
    deleted_orphans = await _delete_orphan_tenant_rows(session)
    total_orphans = sum(deleted_orphans.values())
    await record_security_event(
        session,
        action="admin.test_data_cleanup",
        actor_id=user.id,
        details={
            "deleted_test_tenants": len(deleted_tenants),
            "deleted_test_applications": deleted_applications,
            "deleted_orphan_rows": total_orphans,
        },
    )
    await session.commit()
    return TestTenantCleanupResponse(
        deleted_test_tenants=sorted(deleted_tenants, key=lambda item: item.slug),
        deleted_test_applications=deleted_applications,
        deleted_orphan_rows=deleted_orphans,
        total_deleted_orphan_rows=total_orphans,
    )


@router.get("/test-tenant", response_model=list[TestTenantCreds])
async def list_test_tenants(ctx: SuperAdminDep) -> list[TestTenantCreds]:
    """List throwaway tenants and their seeded resource counts."""
    _user, _own_tenant, session = ctx
    await ensure_test_tenants_enabled(session)
    tenants = (
        await session.execute(
            select(Tenant)
            .where(
                or_(
                    *_test_tenant_slug_filters(),
                    Tenant.settings["kind"].as_string().in_(["demo", "test"]),
                )
            )
            .order_by(Tenant.created_at.desc(), Tenant.id)
            .limit(100)
        )
    ).scalars().all()
    tenant_ids = [tenant.id for tenant in tenants]
    if not tenant_ids:
        return []

    count_models = {
        "nurse_count": Nurse,
        "role_count": Role,
        "skill_count": Skill,
        "day_group_count": DayGroup,
        "shift_count": ShiftTemplate,
        "skill_mix_rule_count": SkillMixRule,
        "shift_sequence_rule_count": ShiftSequenceRule,
    }
    counts: dict[str, Counter[str]] = {
        field: Counter() for field in count_models
    }
    for field, model in count_models.items():
        rows = (
            await session.execute(
                select(model.tenant_id, func.count())
                .where(model.tenant_id.in_(tenant_ids))
                .group_by(model.tenant_id)
            )
        ).all()
        counts[field].update({tenant_id: count for tenant_id, count in rows})

    admins = (
        await session.execute(
            select(User)
            .where(User.tenant_id.in_(tenant_ids))
            .order_by(User.created_at, User.id)
        )
    ).scalars().all()
    admins_by_tenant: dict[str, User] = {}
    for admin_user in admins:
        if admin_user.role == UserRole.TENANT_ADMIN:
            admins_by_tenant.setdefault(admin_user.tenant_id or "", admin_user)

    subscriptions = (
        await session.execute(
            select(Subscription).where(Subscription.tenant_id.in_(tenant_ids))
        )
    ).scalars().all()
    subscriptions_by_tenant = {subscription.tenant_id: subscription for subscription in subscriptions}

    records: list[TestTenantCreds] = []
    for tenant in tenants:
        admin: User | None = admins_by_tenant.get(tenant.id)
        is_demo = tenant.settings.get("dataset") == "demo"
        records.append(
            TestTenantCreds(
                tenant_id=tenant.id,
                tenant_name=tenant.name,
                slug=tenant.slug,
                tenant_kind="demo" if is_demo else "test",
                created_at=tenant.created_at,
                admin_email=admin.email if admin else "",
                admin_password=None,
                password_source=(
                    "environment_variable:DEMO_TENANT_PASSWORD" if is_demo else "one_time_response"
                ),
                admin_user_id=admin.id if admin else "",
                subscription_id=(
                    subscriptions_by_tenant[tenant.id].id
                    if tenant.id in subscriptions_by_tenant
                    else ""
                ),
                **{
                    field: values[tenant.id]
                    for field, values in counts.items()
                },
                nurse_email_prefix=(
                    "demo-nurse@nurse-scheduler.dev"
                    if is_demo
                    else f"nurse00@{tenant.slug}.example.com"
                ),
                nurse_password=None,
            )
        )
    return records


@router.post("/test-tenant", response_model=TestTenantCreds, status_code=status.HTTP_201_CREATED)
async def create_test_tenant(
    ctx: SuperAdminDep,
    body: TestTenantCreate = TestTenantCreate(),
) -> TestTenantCreds:
    """Spin up a throwaway tenant + admin + active subscription + sample nurses.

    Everything is randomised so repeated calls never collide; clean up with
    DELETE /admin/test-tenant/{tenant_id}.
    """
    user, _own_tenant, session = ctx
    await ensure_test_tenants_enabled(session)
    plans = await configured_plan_limits(session)
    if body.plan not in plans:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="套餐不存在",
        )
    max_parallel_shifts = (
        body.shift_count + body.day_group_count - 1
    ) // body.day_group_count
    if body.nurse_count < max_parallel_shifts:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"护士人数不足：当前配置每天最多同时运行 {max_parallel_shifts} 个班次，"
                f"至少需要 {max_parallel_shifts} 名护士"
            ),
        )
    if body.role_count > body.nurse_count:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="角色数量不能超过护士人数，否则会存在未绑定护士的角色",
        )
    if body.shift_sequence_rule_count and body.nurse_count < 2:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="连续班次规则至少需要 2 名护士，否则会生成不可排班的数据",
        )
    tag = uuid.uuid4().hex[:10]
    slug = f"test-{tag}"
    test_password = secrets.token_urlsafe(24)
    tenant = Tenant(
        name=f"测试租户 {tag}",
        slug=slug,
        settings={"kind": "test"},
    )
    session.add(tenant)
    await session.flush()
    tid = tenant.id
    session.add(Role(tenant_id=tid, name="任意角色", code="any"))
    await session.flush()

    # Admin (.example.com is a reserved-but-valid email domain; .test is not)
    admin_email = f"admin@{slug}.example.com"
    admin = User(
        tenant_id=tid,
        email=admin_email,
        hashed_password=await run_in_threadpool(hash_password, test_password),
        first_name="测试",
        last_name="管理员",
        role=UserRole.TENANT_ADMIN,
        is_active=True,
    )
    session.add(admin)

    # Active subscription (user-selected plan, perpetual) so generation works.
    sub = Subscription(
        tenant_id=tid,
        plan=body.plan,
        subscription_type=SubscriptionType.MANUAL.value,
        is_canceled=False,
    )
    session.add(sub)

    role_names = {
        1: "责任护士",
        2: "高级责任护士",
    }
    role_codes = {
        1: "NURSE",
        2: "SENIOR",
    }
    roles = [
        Role(
            tenant_id=tid,
            name=role_names.get(index + 1, f"专属角色 {index + 1}"),
            code=role_codes.get(index + 1, f"ROLE{index + 1:02d}"),
            description="测试租户自动生成角色",
        )
        for index in range(body.role_count)
    ]
    session.add_all(roles)

    skills = [
        Skill(
            tenant_id=tid,
            name="基础急救",
            code="BLS",
        ),
        Skill(
            tenant_id=tid,
            name="重症监护",
            code="ICU",
        ),
    ]
    session.add_all(skills)

    day_groups = [
        DayGroup(
            tenant_id=tid,
            name=f"日组 {index + 1}",
            description=f"自动划分的第 {index + 1} 组日期",
        )
        for index in range(body.day_group_count)
    ]
    session.add_all(day_groups)
    await session.flush()
    for day_number in range(1, 8):
        group_index = (day_number - 1) % body.day_group_count
        session.add(
            DayGroupDay(
                tenant_id=tid,
                day_group_id=day_groups[group_index].id,
                day_number=day_number,
            )
        )

    default_shifts = (
        ("D", "白班", time(7, 0), time(15, 0), "#4c6ef5"),
        ("L", "晚班", time(15, 0), time(23, 0), "#f08c00"),
        ("N", "夜班", time(23, 0), time(7, 0), "#7048e8"),
    )
    shift_colors = ("#4c6ef5", "#f08c00", "#7048e8", "#40c057", "#fa5252")
    shifts: list[ShiftTemplate] = []
    for index in range(body.shift_count):
        if index < len(default_shifts):
            code, name, start_time, end_time, color = default_shifts[index]
        else:
            start_hour = (7 + 8 * index) % 24
            code = f"S{index + 1:02d}"
            name = f"自定义班次 {index + 1}"
            start_time = time(start_hour, 0)
            end_time = time((start_hour + 8) % 24, 0)
            color = shift_colors[index % len(shift_colors)]
        shifts.append(
            ShiftTemplate(
                tenant_id=tid,
                code=code,
                name=name,
                start_time=start_time,
                end_time=end_time,
                duration_hours=8,
                color=color,
                day_group_id=day_groups[index % body.day_group_count].id,
            )
        )
    session.add_all(shifts)
    await session.flush()

    # Switch RLS to the tenant for the tenant-scoped inserts.
    await set_tenant_context(session, tid)
    nurses = []
    for i in range(body.nurse_count):
        nurse = Nurse(
            tenant_id=tid,
            employee_id=f"T-{i:02d}",
            first_name=f"护士{i}",
            last_name="测",
            department="测试科",
            is_available=True,
            preferences={},
        )
        nurses.append(nurse)
        session.add(nurse)
    await session.flush()

    # Create a login User for each nurse so they can self-serve
    # (view schedules, submit preferences, manage skills).
    nurse_hashed_password = await run_in_threadpool(hash_password, test_password)
    for nurse in nurses:
        session.add(
            User(
                tenant_id=tid,
                email=f"nurse{nurse.employee_id.split('-')[1]}@{slug}.example.com",
                hashed_password=nurse_hashed_password,
                first_name=nurse.first_name,
                last_name=nurse.last_name,
                role=UserRole.NURSE,
                is_active=True,
                nurse_id=nurse.id,
            )
        )

    nurse_role_links = [
        {"nurse_id": nurse.id, "role_id": roles[0].id} for nurse in nurses
    ]
    for nurse_index, nurse in enumerate(nurses):
        role_index = (nurse_index + 1) % body.role_count
        if role_index:
            role = roles[role_index]
            nurse_role_links.append({"nurse_id": nurse.id, "role_id": role.id})
    await session.execute(
        nurse_roles.insert().values(nurse_role_links)
    )
    nurse_skill_links = [
        {"nurse_id": nurse.id, "skill_id": skills[0].id} for nurse in nurses
    ]
    for nurse_index, nurse in enumerate(nurses):
        if nurse_index % 3 == 0:
            nurse_skill_links.append(
                {"nurse_id": nurse.id, "skill_id": skills[1].id}
            )
    await session.execute(nurse_skills.insert().values(nurse_skill_links))

    staff_per_shift = max(1, body.nurse_count // max_parallel_shifts)
    for shift in shifts:
        for rule_index in range(body.skill_mix_rule_count):
            rule = SkillMixRule(
                tenant_id=tid,
                name=f"{shift.name}配置 {rule_index + 1}",
                shift_template_id=shift.id,
                priority=rule_index,
                is_active=True,
            )
            session.add(rule)
            await session.flush()
            requirements = [
                SkillMixRequirement(
                    tenant_id=tid,
                    skill_mix_rule_id=rule.id,
                    role_id=roles[0].id,
                    skill_id=skills[0].id,
                    count=staff_per_shift if body.role_count == 1 else staff_per_shift - 1,
                )
            ]
            if body.role_count > 1 and staff_per_shift > 1:
                requirements.append(
                    SkillMixRequirement(
                        tenant_id=tid,
                        skill_mix_rule_id=rule.id,
                        role_id=roles[1].id,
                        skill_id=skills[0].id,
                        count=1,
                    )
                )
            session.add_all(requirements)

    for rule_index in range(body.shift_sequence_rule_count):
        first_shift = shifts[rule_index % len(shifts)]
        second_shift = shifts[(rule_index + 1) % len(shifts)]
        sequence_rule = ShiftSequenceRule(
            tenant_id=tid,
            name=f"禁排序列 {rule_index + 1}",
            description=f"禁止 {first_shift.name} 后接 {second_shift.name}。",
            is_active=True,
        )
        session.add(sequence_rule)
        await session.flush()
        session.add_all(
            [
                ShiftSequenceStep(
                    tenant_id=tid,
                    rule_id=sequence_rule.id,
                    position=0,
                    shift_template_id=first_shift.id,
                ),
                ShiftSequenceStep(
                    tenant_id=tid,
                    rule_id=sequence_rule.id,
                    position=1,
                    shift_template_id=second_shift.id,
                ),
            ]
        )
    await session.flush()
    await record_security_event(
        session,
        action="admin.test_tenant_create",
        actor_id=user.id,
        tenant_id=tid,
        target_user_id=admin.id,
        details=body.model_dump(),
    )
    await session.commit()
    return TestTenantCreds(
        tenant_id=tid,
        tenant_name=tenant.name,
        slug=slug,
        tenant_kind="test",
        created_at=tenant.created_at,
        admin_email=admin_email,
        admin_password=test_password,
        password_source="one_time_response",
        admin_user_id=admin.id,
        subscription_id=sub.id,
        nurse_count=len(nurses),
        role_count=len(roles),
        skill_count=len(skills),
        day_group_count=len(day_groups),
        shift_count=len(shifts),
        skill_mix_rule_count=len(shifts) * body.skill_mix_rule_count,
        shift_sequence_rule_count=body.shift_sequence_rule_count,
        nurse_email_prefix=f"nurse00@{slug}.example.com",
        nurse_password=test_password,
    )


@router.delete("/test-tenant/{tenant_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_test_tenant(tenant_id: str, ctx: SuperAdminDep) -> None:
    """Cascade-delete a test tenant and all its data (super admin only).

    Only tenants classified as test tenants may be deleted here, to make
    accidental deletion of real tenants impossible.
    """
    user, _own_tenant, session = ctx
    await ensure_test_tenants_enabled(session)
    # Super bypass is active; read the tenant directly.
    tenant = await session.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    if is_demo_tenant(tenant):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="演示租户不能删除",
        )
    if not is_test_tenant(tenant):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="只能删除测试租户",
        )
    await session.execute(
        text("UPDATE users SET nurse_id = NULL WHERE tenant_id = :tenant_id"),
        {"tenant_id": tenant_id},
    )
    for table_name in TENANT_SCOPED_TABLES:
        await session.execute(
            text(f"DELETE FROM {table_name} WHERE tenant_id = :tenant_id"),
            {"tenant_id": tenant_id},
        )
    await session.delete(tenant)
    await record_security_event(
        session,
        action="admin.test_tenant_delete",
        actor_id=user.id,
        tenant_id=tenant_id,
        details={"slug": tenant.slug},
    )
    await session.commit()
    return None
