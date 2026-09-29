"""Schedules — generate (Celery dispatch), list, get, export (tenant-scoped).

POST /schedules/generate  → create a ScheduleRequest + dispatch the Celery task
GET  /schedules           → list ScheduleRequests (status filter)
GET  /schedules/{id}      → ScheduleRequest detail
GET  /schedules/{id}/result → solved Schedule + Assignments (when completed)
GET  /schedules/{id}/export?format=csv|txt|json|pdf&view=table|calendar → schedule download
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import html
import io
import os
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, TypedDict
from typing import cast as typing_cast

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import CursorResult, String, cast, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AnyUserDep, SchedulerDep, set_tenant_context
from app.core.audit import record_security_event
from app.core.config import settings
from app.models import (
    Assignment,
    Contract,
    Nurse,
    Schedule,
    ScheduleRequest,
    SkillMixRequirement,
    SkillMixRule,
    Tenant,
    User,
    nurse_roles,
    nurse_skills,
)
from app.models.enums import ScheduleStatus, UserRole
from app.schemas import (
    AssignmentRead,
    MyScheduleRequestRead,
    Paginated,
    ScheduleActiveVersionRead,
    ScheduleActiveVersionRequest,
    ScheduleEditRequest,
    ScheduleEditResponse,
    ScheduleEditWarning,
    ScheduleGeneratePrecheckRequest,
    ScheduleGeneratePrecheckResponse,
    ScheduleGenerateRequest,
    ScheduleRead,
    ScheduleRequestRead,
    ScheduleResolveAssignment,
    ScheduleResolveCommitRequest,
    ScheduleResolveDiffItem,
    ScheduleResolveRequest,
    ScheduleResolveResponse,
    ScheduleVersionRead,
    ShiftTemplateBrief,
)
from app.tasks.celery_app import celery_app

if TYPE_CHECKING:
    from app.models.shift import ShiftTemplate

router = APIRouter(prefix="/schedules", tags=["schedules"])

WEEKDAY_HEADERS = ["日", "一", "二", "三", "四", "五", "六"]


class _AssignmentValues(TypedDict):
    nurse_id: str
    role_id: str | None
    date: date
    shift_template_id: str
    satisfied_preference: bool | None


@dataclass
class _EditedAssignment:
    nurse_id: str
    date: date
    shift_template_id: str
    satisfied_preference: bool | None = None


async def _user_names(
    session: AsyncSession,
    user_ids: set[str],
) -> dict[str, str]:
    """Batch-resolve user ids to display names."""
    if not user_ids:
        return {}
    rows = (
        await session.execute(
            select(User.id, User.first_name, User.last_name).where(User.id.in_(user_ids))
        )
    ).all()
    return {row.id: f"{row.last_name}{row.first_name}" for row in rows}


async def _user_name(
    session: AsyncSession,
    user_id: str | None,
) -> str | None:
    """Look up a user's display name by id."""
    if not user_id:
        return None
    names = await _user_names(session, {user_id})
    return names.get(user_id)


async def _enrich_schedule_requests(
    session: AsyncSession,
    rows: Sequence[ScheduleRequest],
) -> None:
    """Populate display names with one tenant and one user batch query."""
    tenant_ids = {row.tenant_id for row in rows if row.tenant_id}
    user_ids = {row.requested_by for row in rows if row.requested_by}
    tenant_names: dict[str, str] = {}
    if tenant_ids:
        tenant_names = {
            row.id: row.name
            for row in (
                await session.execute(
                    select(Tenant.id, Tenant.name).where(Tenant.id.in_(tenant_ids))
                )
            ).all()
        }
    user_names = await _user_names(session, user_ids)
    for row in rows:
        enriched_row: Any = row
        enriched_row.tenant_name = tenant_names.get(row.tenant_id)
        enriched_row.requested_by_name = (
            user_names.get(row.requested_by) if row.requested_by else None
        )


def _effective_solver_config(
    body: ScheduleGenerateRequest,
    cpu_count: int,
) -> dict[str, Any]:
    config = body.solver_config.model_dump()
    config["long_run"] = body.long_run
    if body.long_run:
        config["num_workers"] = min(config["num_workers"], cpu_count)
    return config


def _estimate_nurse_days(nurse_count: int, period_days: int) -> int:
    return nurse_count * period_days


def _ensure_within_capacity(nurse_days: int) -> None:
    if nurse_days > settings.SOLVER_MAX_NURSE_DAYS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"排班规模为 {nurse_days} nurse-days，超过当前主机上限 "
                f"{settings.SOLVER_MAX_NURSE_DAYS}。请按科室分批、缩短周期，"
                "或在具备足够 CPU 的主机上提高 SOLVER_MAX_NURSE_DAYS。"
            ),
        )


def _csv_safe(value: object) -> str:
    """Prevent spreadsheet applications from evaluating exported cells."""
    text = str(value if value is not None else "")
    if text.startswith(("=", "+", "-", "@", "\t", "\r")):
        return f"'{text}"
    return text


async def _user_email(session: AsyncSession, user_id: str | None) -> str | None:
    """Look up a user's login account (email) by id."""
    if not user_id:
        return None
    user = (
        await session.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if not user:
        return None
    return user.email


def _schedule_pdf(
    grid: list[dict[str, Any]],
    dates: list[str],
    legend: list[str],
    period_start: str,
    period_days: int,
    outcome: str,
    operator_name: str,
    view: str = "table",
    calendar_months: list[dict[str, Any]] | None = None,
) -> bytes:
    from datetime import UTC, datetime

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle

    font_name = "STSong-Light"
    if font_name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(UnicodeCIDFont(font_name))

    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        title="排班结果导出",
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ScheduleExportTitle", parent=styles["Title"], fontName=font_name, fontSize=18
    )
    meta_style = ParagraphStyle(
        "ScheduleExportMeta", parent=styles["BodyText"], fontName=font_name, fontSize=9
    )
    cell_style = ParagraphStyle(
        "ScheduleExportCell", parent=styles["BodyText"], fontName=font_name, fontSize=8
    )
    header_style = ParagraphStyle(
        "ScheduleExportHeader", parent=cell_style, fontName=font_name, fontSize=8,
        textColor=colors.white,
    )
    month_style = ParagraphStyle(
        "ScheduleExportMonth", parent=meta_style, fontName=font_name, fontSize=13
    )

    available_width = landscape(A4)[0] - document.leftMargin - document.rightMargin

    if view == "calendar":
        from reportlab.platypus import KeepTogether

        schedule_tables = []
        for month in calendar_months or []:
            table_rows = [[Paragraph(value, header_style) for value in WEEKDAY_HEADERS]]
            for offset in range(0, len(month["cells"]), 7):
                week = month["cells"][offset:offset + 7]
                table_rows.append([
                    Paragraph(_calendar_pdf_cell(cell), cell_style) for cell in week
                ])
            table = Table(
                table_rows,
                colWidths=[available_width / 7] * 7,
                repeatRows=1,
            )
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2b3a55")),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#b8c1cc")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fafbfc")]),
            ]))
            schedule_tables.append(KeepTogether([Paragraph(month["title"], month_style), table]))
    else:
        headers = ["护士"] + [day[5:] for day in dates]
        table_rows = [[Paragraph(value, header_style) for value in headers]]
        for row in grid:
            table_rows.append([
                Paragraph(row["nurse_name"] or "—", cell_style),
                *(Paragraph(cell or "·", cell_style) for cell in row["cells"]),
            ])
        date_width = (available_width - 36 * mm) / max(len(dates), 1)
        table = Table(
            table_rows,
            colWidths=[36 * mm] + [date_width] * len(dates),
            repeatRows=1,
        )
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2b3a55")),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#b8c1cc")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f3f6f9")]),
        ]))
        schedule_tables = [table]

    story = [
        Paragraph("排班详情", title_style),
        Paragraph(
            f"周期 {period_start} · {period_days} 天 · 结果 {outcome}",
            meta_style,
        ),
        Paragraph("本次排班涉及班次（名称 + 时段）：", meta_style),
        Paragraph("、".join(legend) if legend else "（无班次模板信息）", meta_style),
        *schedule_tables,
        Paragraph(
            f"操作账号：{operator_name} · 导出时间：{datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}",
            meta_style,
        ),
    ]
    document.build(story)
    return buffer.getvalue()


def _calendar_months(
    dates: list[str],
    assignments: list[Assignment],
    nurse_by_id: dict[str, str],
    shift_by_id: dict[str, str],
) -> list[dict[str, Any]]:
    period_start = date.fromisoformat(dates[0])
    period_end = date.fromisoformat(dates[-1])
    assignments_by_date: dict[date, list[dict[str, str]]] = {}
    for assignment in assignments:
        assignments_by_date.setdefault(assignment.date, []).append({
            "nurse_name": nurse_by_id.get(assignment.nurse_id, assignment.nurse_id),
            "shift_name": shift_by_id.get(assignment.shift_template_id, "?"),
        })

    months: list[dict[str, Any]] = []
    cursor = period_start.replace(day=1)
    while cursor <= period_end:
        grid_start = cursor - timedelta(days=(cursor.weekday() + 1) % 7)
        cells: list[dict[str, Any]] = []
        for offset in range(42):
            day = grid_start + timedelta(days=offset)
            belongs_to_month = (
                day.year == cursor.year and day.month == cursor.month
            )
            events = (
                sorted(
                    assignments_by_date.get(day, []),
                    key=lambda item: (item["nurse_name"], item["shift_name"]),
                )
                if belongs_to_month and period_start <= day <= period_end
                else []
            )
            cells.append({
                "date": day.isoformat(),
                "day": f"{day.day:02d}",
                "outside_month": day.month != cursor.month or day.year != cursor.year,
                "outside_period": day < period_start or day > period_end,
                "assignments": events,
            })
        months.append({
            "title": f"{cursor.year}年{cursor.month}月",
            "year": cursor.year,
            "month": cursor.month,
            "cells": cells,
        })
        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)
    return months


def _calendar_pdf_cell(cell: dict[str, Any]) -> str:
    content = f"<b>{cell['day']}</b>"
    if cell["assignments"]:
        events = [
            html.escape(f"{item['nurse_name']}：{item['shift_name']}")
            for item in cell["assignments"]
        ]
        content += "<br/>" + "<br/>".join(events)
    return content


def _calendar_csv_cell(cell: dict[str, Any]) -> str:
    if not cell["assignments"]:
        return str(cell["day"])
    events = [
        f"{item['nurse_name']}：{item['shift_name']}"
        for item in cell["assignments"]
    ]
    return str(cell["day"]) + "\n" + "\n".join(events)


def _calendar_text_cell(cell: dict[str, Any]) -> str:
    if not cell["assignments"]:
        return str(cell["day"])
    events = [
        f"{item['nurse_name']}：{item['shift_name']}"
        for item in cell["assignments"]
    ]
    return str(cell["day"]) + " " + "；".join(events)


def _shift_brief(shift: ShiftTemplate) -> ShiftTemplateBrief:
    day_numbers = sorted(day.day_number for day in shift.day_group.days) if shift.day_group else []
    return ShiftTemplateBrief(
        id=shift.id,
        code=shift.code,
        name=shift.name,
        start_time=shift.start_time.isoformat() if shift.start_time else None,
        end_time=shift.end_time.isoformat() if shift.end_time else None,
        day_group_name=shift.day_group.name if shift.day_group else None,
        day_numbers=day_numbers,
    )


def _visible_assignments(user: User, schedule: Schedule) -> list[Assignment]:
    if user.role != UserRole.NURSE:
        return schedule.assignments

    if not user.nurse_id:
        return []
    return [a for a in schedule.assignments if a.nurse_id == user.nurse_id]


async def _get_latest_schedule(
    session: AsyncSession,
    request_id: str,
    *,
    for_update: bool = False,
) -> Schedule | None:
    """Fetch the Schedule with the highest version for this request.

    B3-11 local re-solve creates a new Schedule row (version + 1) instead
    of mutating the original, so callers must always target the latest.
    """
    stmt = select(Schedule).where(
        Schedule.request_id == request_id,
        Schedule.deleted_at.is_(None),
    )
    if for_update:
        stmt = stmt.with_for_update()
    stmt = stmt.order_by(Schedule.version.desc()).limit(1)
    schedule: Schedule | None = (await session.execute(stmt)).scalar_one_or_none()
    return schedule


async def _get_next_schedule_version(
    session: AsyncSession,
    request_id: str,
) -> int:
    """Allocate from all snapshots so a deleted latest version can't be reused."""
    high_water = (
        await session.execute(
            select(func.max(Schedule.version)).where(
                Schedule.request_id == request_id
            )
        )
    ).scalar_one_or_none()
    return (high_water or 0) + 1


async def _get_schedule_version(
    session: AsyncSession,
    request_id: str,
    version: int,
) -> Schedule | None:
    stmt = select(Schedule).where(
        Schedule.request_id == request_id,
        Schedule.version == version,
        Schedule.deleted_at.is_(None),
    )
    schedule: Schedule | None = (await session.execute(stmt)).scalar_one_or_none()
    return schedule


async def _get_active_schedule(
    session: AsyncSession,
    request_id: str,
) -> Schedule | None:
    stmt = (
        select(Schedule)
        .join(ScheduleRequest, Schedule.id == ScheduleRequest.active_schedule_id)
        .where(
            Schedule.request_id == request_id,
            ScheduleRequest.id == request_id,
        )
    )
    schedule: Schedule | None = (await session.execute(stmt)).scalar_one_or_none()
    return schedule


async def _set_request_version_metadata(
    session: AsyncSession,
    req: ScheduleRequest,
) -> None:
    """Expose effective/history version metadata without loading every snapshot."""
    active_version = (
        await session.execute(
            select(Schedule.version).where(Schedule.id == req.active_schedule_id)
        )
    ).scalar_one_or_none()
    available_versions = (
        await session.execute(
            select(Schedule.version)
            .where(
                Schedule.request_id == req.id,
                Schedule.deleted_at.is_(None),
            )
            .order_by(Schedule.version.asc())
        )
    ).scalars().all()
    versioned_request: Any = req
    versioned_request.active_version = active_version
    versioned_request.available_versions = list(available_versions)

@router.post("/generate", response_model=ScheduleRequestRead, status_code=status.HTTP_201_CREATED)
async def generate_schedule(
    body: ScheduleGenerateRequest,
    ctx: SchedulerDep,
) -> ScheduleRequestRead:
    """Create a ScheduleRequest and dispatch the Celery generate task.

    Super-admin (whose JWT tenant_id is null) must supply `body.tenant_id` to
    act on behalf of a tenant; the subscription gate then checks THAT
    tenant's subscription. Tenant users omit it (it must equal their own).
    """
    from app.api.deps import set_tenant_context
    from app.api.v1.subscriptions import ensure_active_subscription
    from app.models.enums import UserRole

    user, jwt_tenant_id, session = ctx
    is_super = user.role == UserRole.SUPER_ADMIN

    target_tenant: str
    if is_super:
        requested_tenant = body.tenant_id
        if not requested_tenant:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="super_admin 生成排班需指定 tenant_id",
            )
        target_tenant = requested_tenant
        if not target_tenant:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="super_admin 生成排班需指定 tenant_id",
            )
        # Switch RLS to the target tenant for the subscription check + insert.
        await set_tenant_context(session, target_tenant)
    else:
        if not jwt_tenant_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "tenant context required")
        target_tenant = jwt_tenant_id
        if body.tenant_id and body.tenant_id != jwt_tenant_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="租户用户只能为本租户生成排班",
            )

    # Subscription gate — 403 unless the target tenant has an active subscription.
    await ensure_active_subscription(session, target_tenant)
    # Plan limits — free tier caps the scheduling period.
    from app.core.plan_limits import effective_limits_for_tenant
    from app.models.subscription import Subscription
    from app.models.tenant import Tenant
    tenant = await session.get(Tenant, target_tenant)
    if not tenant:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tenant not found")
    sub = (
        await session.execute(
            select(Subscription).where(Subscription.tenant_id == target_tenant)
        )
    ).scalar_one_or_none()
    limits = await effective_limits_for_tenant(session, tenant, sub)
    if limits.max_period_days is not None and body.period_days > limits.max_period_days:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=(
                f"当前套餐排班周期最长 {limits.max_period_days} 天，"
                "请升级套餐后排更长的周期。"
            ),
        )
    if body.nurse_ids:
        nurse_count = len(body.nurse_ids)
    else:
        nurse_count = (
            await session.execute(
                select(func.count())
                .select_from(Nurse)
                .where(
                    Nurse.tenant_id == target_tenant,
                    Nurse.is_available.is_(True),
                )
            )
        ).scalar_one()
    if limits.max_nurses is not None and nurse_count > limits.max_nurses:
        if body.nurse_ids:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=(
                    f"当前套餐最多 {limits.max_nurses} 名护士参与排班，"
                    f"本次选择了 {nurse_count} 名，请减少护士数量或升级套餐。"
                ),
            )
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=(
                f"当前套餐最多 {limits.max_nurses} 名护士参与排班，"
                f"本租户有 {nurse_count} 名可用护士，请禁用多余护士或升级套餐。"
            ),
        )
    _ensure_within_capacity(_estimate_nurse_days(nurse_count, body.period_days))
    request_date = date.today()
    task_id = str(uuid.uuid4())
    await session.execute(
        text(
            "SELECT pg_advisory_xact_lock(hashtext(:lock_key))"
        ),
        {"lock_key": f"schedule_request:{request_date.isoformat()}"},
    )
    await session.execute(text("SET LOCAL app.is_super = '1'"))
    last_sequence = (
        await session.execute(
            select(func.coalesce(func.max(ScheduleRequest.daily_sequence), 0)).where(
                ScheduleRequest.request_date == request_date,
            )
        )
    ).scalar_one()
    await session.execute(text("SET LOCAL app.is_super = '0'"))
    req = ScheduleRequest(
        tenant_id=target_tenant,
        period_start=body.period_start,
        period_days=body.period_days,
        request_date=request_date,
        daily_sequence=last_sequence + 1,
        solver_config=_effective_solver_config(body, os.cpu_count() or 1),
        nurse_ids=body.nurse_ids or None,
        skill_mix_rule_ids=body.skill_mix_rule_ids,
        shift_sequence_rule_ids=body.shift_sequence_rule_ids,
        requested_by=user.id,
        task_id=task_id,
    )
    session.add(req)
    await session.flush()
    await record_security_event(
        session,
        action="schedule.generate",
        actor_id=user.id,
        actor_tenant_id=user.tenant_id,
        tenant_id=target_tenant,
        target_user_id=None,
        details={
            "period_start": body.period_start.isoformat(),
            "period_days": body.period_days,
        },
    )
    await session.refresh(req)  # in-txn refresh (RLS-safe: same connection)
    await session.commit()

    # Dispatch the worker task (fire-and-forget; status tracked in the row).
    celery_app.send_task(
        "schedule.generate",
        args=[req.id],
        queue="scheduling",
        task_id=task_id,
    )
    return ScheduleRequestRead.model_validate(req)


@router.post("/generate/precheck", response_model=ScheduleGeneratePrecheckResponse)
async def precheck_schedule_generation(
    body: ScheduleGeneratePrecheckRequest, ctx: SchedulerDep
) -> ScheduleGeneratePrecheckResponse:
    """Check whether the selected scope contains nurses and a scheduling demand."""
    user, jwt_tenant_id, session = ctx
    if user.role == UserRole.SUPER_ADMIN:
        target_tenant = body.tenant_id
        if not target_tenant:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="super_admin 生成排班需指定 tenant_id",
            )
        await set_tenant_context(session, target_tenant)
    else:
        target_tenant = jwt_tenant_id
        if body.tenant_id and body.tenant_id != jwt_tenant_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="租户用户只能为本租户生成排班",
            )

    nurse_filters = [
        Nurse.tenant_id == target_tenant,
        Nurse.is_available.is_(True),
    ]
    if body.nurse_ids:
        nurse_filters.append(Nurse.id.in_(body.nurse_ids))
    nurse_count = (
        await session.execute(
            select(func.count()).select_from(Nurse).where(*nurse_filters)
        )
    ).scalar_one()

    rule_filters = [
        SkillMixRule.tenant_id == target_tenant,
        SkillMixRule.is_active.is_(True),
    ]
    if body.skill_mix_rule_ids is not None:
        rule_filters.append(SkillMixRule.id.in_(body.skill_mix_rule_ids))
    demand_rule_count = (
        await session.execute(
            select(func.count(func.distinct(SkillMixRule.id)))
            .join(
                SkillMixRequirement,
                SkillMixRequirement.skill_mix_rule_id == SkillMixRule.id,
            )
            .where(
                *rule_filters,
                SkillMixRequirement.count > 0,
            )
        )
    ).scalar_one()

    contract_filters = [
        Contract.tenant_id == target_tenant,
        Contract.enforce_shifts_per_period.is_(True),
        Contract.shifts_per_period > 0,
        Nurse.is_available.is_(True),
    ]
    if body.nurse_ids:
        contract_filters.append(Nurse.id.in_(body.nurse_ids))
    contract_target_count = (
        await session.execute(
            select(func.count(func.distinct(Contract.nurse_id)))
            .join(Nurse, Nurse.id == Contract.nurse_id)
            .join(nurse_roles, nurse_roles.c.nurse_id == Nurse.id)
            .where(*contract_filters)
        )
    ).scalar_one()

    skill_exists = (
        select(nurse_skills.c.nurse_id)
        .where(
            nurse_skills.c.nurse_id == Nurse.id,
            nurse_skills.c.skill_id == SkillMixRequirement.skill_id,
        )
        .exists()
    )
    eligible_nurse_count = (
        await session.execute(
            select(func.count(func.distinct(Nurse.id)))
            .select_from(Nurse)
            .join(nurse_roles, nurse_roles.c.nurse_id == Nurse.id)
            .join(
                SkillMixRequirement,
                or_(
                    SkillMixRequirement.role_id.is_(None),
                    SkillMixRequirement.role_id == nurse_roles.c.role_id,
                ),
            )
            .join(
                SkillMixRule,
                SkillMixRule.id == SkillMixRequirement.skill_mix_rule_id,
            )
            .where(
                *nurse_filters,
                *rule_filters,
                SkillMixRequirement.count > 0,
                or_(SkillMixRequirement.skill_id.is_(None), skill_exists),
            )
        )
    ).scalar_one()

    reasons: list[str] = []
    if nurse_count == 0:
        reasons.append("无可排班护士")
    if demand_rule_count == 0 and contract_target_count == 0:
        reasons.append("无启用的技能组合需求规则，且参与护士没有合同目标")
    if demand_rule_count > 0 and eligible_nurse_count == 0:
        reasons.append(
            "技能组合需求没有符合角色/技能的护士；未分配角色的护士不会计入任意角色"
        )
    return ScheduleGeneratePrecheckResponse(
        nurse_count=nurse_count,
        demand_rule_count=demand_rule_count,
        contract_target_count=contract_target_count,
        eligible_nurse_count=eligible_nurse_count,
        can_generate=not reasons,
        reasons=reasons,
    )


@router.get("", response_model=Paginated[ScheduleRequestRead])
async def list_schedule_requests(
    ctx: AnyUserDep,
    status_filter: ScheduleStatus | None = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    sort_by: str = Query(
        "created_at",
        pattern="^(display_id|period_start|created_at)$",
    ),
    sort_order: str = Query("desc", pattern="^(asc|desc)$"),
) -> Paginated[ScheduleRequestRead]:
    from sqlalchemy import func

    user, _tenant_id, session = ctx
    latest_version = (
        select(func.max(Schedule.version))
        .where(
            Schedule.request_id == ScheduleRequest.id,
            Schedule.deleted_at.is_(None),
        )
        .scalar_subquery()
    )
    active_version = (
        select(Schedule.version)
        .where(Schedule.id == ScheduleRequest.active_schedule_id)
        .scalar_subquery()
    )
    available_versions = (
        select(func.array_agg(Schedule.version))
        .where(
            Schedule.request_id == ScheduleRequest.id,
            Schedule.deleted_at.is_(None),
        )
        .scalar_subquery()
    )
    stmt = select(
        ScheduleRequest,
        latest_version.label("latest_version"),
        active_version.label("active_version"),
        available_versions.label("available_versions"),
    )
    if status_filter:
        stmt = stmt.where(cast(ScheduleRequest.status, String) == status_filter.name)
    order_columns = {
        "display_id": (
            ScheduleRequest.request_date,
            ScheduleRequest.daily_sequence,
        ),
        "period_start": (ScheduleRequest.period_start,),
        "created_at": (ScheduleRequest.created_at,),
    }[sort_by]
    ordered_columns = (
        [column.asc() for column in order_columns]
        if sort_order == "asc"
        else [column.desc() for column in order_columns]
    )
    stmt = stmt.order_by(*ordered_columns, ScheduleRequest.id)
    total = (
        await session.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one()
    versioned_rows = (
        await session.execute(
            stmt.offset((page - 1) * page_size).limit(page_size)
        )
    ).all()
    rows: list[ScheduleRequest] = []
    for row in versioned_rows:
        schedule_request = row.ScheduleRequest
        schedule_request.latest_version = row.latest_version
        schedule_request.active_version = row.active_version
        schedule_request.available_versions = sorted(row.available_versions or [])
        rows.append(schedule_request)
    await _enrich_schedule_requests(session, rows)
    items = [ScheduleRequestRead.model_validate(row) for row in rows]
    return Paginated(items=items, total=total, page=page, page_size=page_size)


@router.get("/mine", response_model=Paginated[MyScheduleRequestRead])
async def list_my_schedule_requests(
    ctx: AnyUserDep,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
) -> Paginated[MyScheduleRequestRead]:
    """List completed schedules that contain assignments for the nurse user."""
    user, _tenant_id, session = ctx
    if user.role != UserRole.NURSE:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only nurse users can list their schedules")
    if not user.nurse_id:
        return Paginated[MyScheduleRequestRead](items=[], total=0, page=page, page_size=page_size)

    stmt = (
        select(
            ScheduleRequest,
            Schedule.version.label("active_version"),
            func.count(Assignment.id).label("assignment_count"),
        )
        .join(
            Schedule,
            Schedule.id == ScheduleRequest.active_schedule_id,
        )
        .join(Assignment, Assignment.schedule_id == Schedule.id)
        .where(
            cast(ScheduleRequest.status, String) == ScheduleStatus.COMPLETED.name,
            Assignment.nurse_id == user.nurse_id,
        )
        .group_by(ScheduleRequest.id, Schedule.version)
        .order_by(ScheduleRequest.period_start.desc(), ScheduleRequest.id)
    )
    total = (
        await session.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one()
    rows = (
        await session.execute(stmt.offset((page - 1) * page_size).limit(page_size))
    ).all()
    items: list[MyScheduleRequestRead] = []
    for row in rows:
        request_data = ScheduleRequestRead.model_validate(row.ScheduleRequest)
        request_data.active_version = row.active_version
        request_data.available_versions = (
            [row.active_version] if row.active_version else []
        )
        items.append(
            MyScheduleRequestRead(
                **request_data.model_dump(),
                assignment_count=row.assignment_count,
            )
        )
    return Paginated(items=items, total=total, page=page, page_size=page_size)


@router.get("/{request_id}", response_model=ScheduleRequestRead)
async def get_schedule_request(
    request_id: str,
    ctx: AnyUserDep,
) -> ScheduleRequestRead:
    user, _tenant_id, session = ctx
    req = await session.get(ScheduleRequest, request_id)
    if not req:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Schedule request not found")
    await _set_request_version_metadata(session, req)
    request_read = ScheduleRequestRead.model_validate(req)
    request_read.requested_by_name = await _user_name(session, req.requested_by)
    return request_read


@router.post("/{request_id}/cancel", response_model=ScheduleRequestRead)
async def cancel_schedule_request(
    request_id: str,
    ctx: SchedulerDep,
) -> ScheduleRequestRead:
    """Cancel a queued or running schedule request.

    Tenant admins may cancel requests in their tenant. Schedulers may cancel
    requests they initiated; super admins may cancel any request. Running
    solves are stopped cooperatively by the solver's CP-SAT callback.
    """
    user, _tenant_id, session = ctx
    req = await session.get(ScheduleRequest, request_id)
    if not req:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Schedule request not found")
    if user.role != UserRole.SUPER_ADMIN:
        if req.tenant_id != user.tenant_id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "不能取消其他租户的排班任务")
        if user.role == UserRole.SCHEDULER and req.requested_by != user.id:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "Scheduler 只能取消自己发起的排班任务",
            )
    if req.status not in (ScheduleStatus.PENDING, ScheduleStatus.RUNNING):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"排班任务当前为 {req.status.value}，不能取消。",
        )

    result = await session.execute(
        update(ScheduleRequest)
        .where(
            ScheduleRequest.id == request_id,
            func.lower(cast(ScheduleRequest.status, String)).in_([
                ScheduleStatus.PENDING.value,
                ScheduleStatus.RUNNING.value,
            ]),
        )
        .values(
            status=ScheduleStatus.CANCELLED,
            error_message="排班任务已被取消",
            stats={"cancelled_by": user.id},
            completed_at=func.now(),
        )
    )
    if typing_cast(CursorResult[Any], result).rowcount != 1:
        await session.rollback()
        fresh_req = await session.get(ScheduleRequest, request_id)
        current_status = fresh_req.status.value if fresh_req else "not_found"
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"排班任务当前为 {current_status}，不能取消。",
        )

    await record_security_event(
        session,
        action="schedule.cancel",
        actor_id=user.id,
        actor_tenant_id=user.tenant_id,
        tenant_id=req.tenant_id,
        target_user_id=None,
        details={
            "request_id": req.id,
            "display_id": req.display_id,
            "period_start": req.period_start.isoformat(),
            "period_days": req.period_days,
            "status": req.status.value,
        },
    )
    await session.commit()
    if req.task_id:
        with contextlib.suppress(Exception):
            celery_app.control.revoke(req.task_id, terminate=False)

    await session.refresh(req)
    request_read = ScheduleRequestRead.model_validate(req)
    request_read.requested_by_name = await _user_name(session, req.requested_by)
    await _set_request_version_metadata(session, req)
    return request_read


@router.delete("/{request_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schedule_request(
    request_id: str,
    ctx: SchedulerDep,
) -> None:
    """Hard-delete a schedule record that is in a terminal state.

    Only super_admin / tenant_admin / scheduler may delete. PENDING/RUNNING
    requests return 409; cancel them first with the cancellation endpoint.
    Deleting the request relies on the schedules.request_id and
    assignments.schedule_id FK CASCADE to clean the Schedule and its
    Assignments in the same transaction.
    """
    user, _tenant_id, session = ctx
    req = await session.get(ScheduleRequest, request_id)
    if not req:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Schedule request not found")
    if req.status in (ScheduleStatus.PENDING, ScheduleStatus.RUNNING):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"排班任务当前为 {req.status.value}，不能删除。"
            "请等待其完成、失败或取消后再删除。",
        )
    await record_security_event(
        session,
        action="schedule.request.deleted",
        actor_id=user.id,
        actor_tenant_id=user.tenant_id,
        tenant_id=req.tenant_id,
        details={
            "request_id": req.id,
            "display_id": req.display_id,
            "period_start": req.period_start.isoformat(),
            "period_days": req.period_days,
            "status": req.status.value,
        },
    )
    await session.delete(req)
    await session.commit()
    return None


@router.get("/{request_id}/result", response_model=ScheduleRead)
async def get_schedule_result(
    request_id: str,
    ctx: AnyUserDep,
    version: int | None = Query(None, ge=1),
) -> ScheduleRead:
    """Return a schedule version; omit `version` to return the effective one."""
    from app.models import Nurse, ShiftTemplate

    user, _tenant_id, session = ctx
    if version is not None and user.role == UserRole.NURSE:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "护士端只能访问生效版本")
    schedule = (
        await _get_schedule_version(session, request_id, version)
        if version is not None
        else await _get_active_schedule(session, request_id)
    )
    if not schedule:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "Schedule version not found or not ready",
        )
    assignments = _visible_assignments(user, schedule)
    # Enrich assignments with nurse names + shift codes for display.
    nurse_ids = {a.nurse_id for a in assignments}
    shift_ids = {a.shift_template_id for a in assignments}
    nurse_names: dict[str, str] = {}
    shift_codes: dict[str, str] = {}
    shift_briefs: list[ShiftTemplateBrief] = []
    if nurse_ids:
        for n in (
            await session.execute(select(Nurse).where(Nurse.id.in_(nurse_ids)))
        ).scalars().all():
            nurse_names[n.id] = f"{n.last_name}{n.first_name}"
    if shift_ids:
        for st in (
            await session.execute(
                select(ShiftTemplate).where(ShiftTemplate.id.in_(shift_ids))
            )
        ).scalars().all():
            shift_codes[st.id] = st.code
            shift_briefs.append(_shift_brief(st))
    assignment_reads: list[AssignmentRead] = []
    for assignment in assignments:
        assignment_read = AssignmentRead.model_validate(
            assignment,
            from_attributes=True,
        )
        assignment_read.nurse_name = nurse_names.get(assignment.nurse_id)
        assignment_read.shift_template_code = shift_codes.get(
            assignment.shift_template_id,
        )
        assignment_reads.append(assignment_read)
    # Attach the shift-template legend for the frontend (dedup by code, keep order).
    seen: set[str] = set()
    unique_shift_briefs: list[ShiftTemplateBrief] = []
    for brief in shift_briefs:
        if brief.code not in seen:
            seen.add(brief.code)
            unique_shift_briefs.append(brief)
    schedule_read = ScheduleRead.model_validate(schedule)
    schedule_read.shift_templates = unique_shift_briefs
    schedule_read.assignments = assignment_reads
    return schedule_read


@router.get("/{request_id}/versions", response_model=list[ScheduleVersionRead])
async def list_schedule_versions(
    request_id: str,
    ctx: SchedulerDep,
) -> list[ScheduleVersionRead]:
    """List immutable schedule snapshots for a schedule request."""
    user, _tenant_id, session = ctx
    req = await session.get(ScheduleRequest, request_id)
    if not req:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Schedule request not found")
    schedules = (
        await session.execute(
            select(Schedule)
            .where(
                Schedule.request_id == request_id,
                Schedule.deleted_at.is_(None),
            )
            .order_by(Schedule.version.desc())
        )
    ).scalars().all()
    return [
        ScheduleVersionRead(
            id=schedule.id,
            version=schedule.version,
            outcome=schedule.outcome,
            summary=schedule.summary or {},
            created_at=schedule.created_at,
            assignment_count=len(_visible_assignments(user, schedule)),
        )
        for schedule in schedules
    ]


@router.put(
    "/{request_id}/active-version",
    response_model=ScheduleActiveVersionRead,
)
async def set_active_schedule_version(
    request_id: str,
    body: ScheduleActiveVersionRequest,
    ctx: SchedulerDep,
) -> ScheduleActiveVersionRead:
    """Choose the immutable version used as the tenant's effective roster."""
    user, _tenant_id, session = ctx
    request_row = await session.execute(
        select(ScheduleRequest)
        .where(ScheduleRequest.id == request_id)
        .with_for_update()
    )
    req = request_row.scalar_one_or_none()
    if not req:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Schedule request not found")
    if req.status != ScheduleStatus.COMPLETED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Only completed schedules can have an active version",
        )
    schedule = await _get_schedule_version(session, request_id, body.version)
    if not schedule:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Schedule version not found")

    previous_schedule_id = req.active_schedule_id
    req.active_schedule_id = schedule.id
    await record_security_event(
        session,
        action="schedule.active_version.changed",
        actor_id=user.id,
        actor_tenant_id=user.tenant_id,
        tenant_id=req.tenant_id,
        details={
            "request_id": request_id,
            "previous_schedule_id": previous_schedule_id,
            "active_schedule_id": schedule.id,
            "active_version": schedule.version,
        },
    )
    await session.commit()
    return ScheduleActiveVersionRead(
        request_id=request_id,
        active_schedule_id=schedule.id,
        active_version=schedule.version,
    )


@router.delete(
    "/{request_id}/versions/{version}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_schedule_version(
    request_id: str,
    version: int,
    ctx: SchedulerDep,
) -> None:
    """Soft-delete a non-effective schedule snapshot."""
    user, _tenant_id, session = ctx
    request_row = await session.execute(
        select(ScheduleRequest)
        .where(ScheduleRequest.id == request_id)
        .with_for_update()
    )
    req = request_row.scalar_one_or_none()
    if not req:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Schedule request not found")
    if req.status != ScheduleStatus.COMPLETED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Only completed schedule versions can be deleted",
        )

    schedule = await _get_schedule_version(session, request_id, version)
    if not schedule:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Schedule version not found")
    if req.active_schedule_id == schedule.id:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "生效版本不能删除，请先切换到其他版本",
        )
    visible_count = (
        await session.execute(
            select(func.count())
            .select_from(Schedule)
            .where(
                Schedule.request_id == request_id,
                Schedule.deleted_at.is_(None),
            )
        )
    ).scalar_one()
    if visible_count <= 1:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "至少需要保留一个排班版本",
        )

    schedule.deleted_at = datetime.now(UTC)
    await record_security_event(
        session,
        action="schedule.version.deleted",
        actor_id=user.id,
        actor_tenant_id=user.tenant_id,
        tenant_id=req.tenant_id,
        details={
            "request_id": request_id,
            "schedule_id": schedule.id,
            "version": schedule.version,
        },
    )
    await session.commit()
    return None


@router.patch("/{request_id}/assignments", response_model=ScheduleEditResponse)
async def edit_schedule_assignments(
    request_id: str,
    body: ScheduleEditRequest,
    ctx: SchedulerDep,
) -> ScheduleEditResponse:
    """Atomically edit assignments on a completed schedule (B3-10).

    Validates all hard constraints (leave, rest, consecutive days, contract
    limits, qualification) and rejects on violation. Soft violations (skill
    mix) require an explicit override_reason.
    """
    from datetime import UTC, datetime

    from app.scheduling.domain import DomainData
    from app.scheduling.edit_validation import (
        ProposedAssignment,
        summarize_preferences,
        validate_assignments,
    )
    from app.scheduling.loader import load_domain_data

    user, _tenant_id, session = ctx
    request_row = await session.execute(
        select(ScheduleRequest)
        .where(ScheduleRequest.id == request_id)
        .with_for_update()
    )
    req = request_row.scalar_one_or_none()
    if not req:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Schedule request not found")
    if req.status != ScheduleStatus.COMPLETED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Only completed schedules can be edited (current: {req.status.value})",
        )

    schedule = await _get_latest_schedule(session, request_id, for_update=True)
    if not schedule:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Schedule not found")
    if schedule.version != body.base_version:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"版本冲突: expected base_version={schedule.version}, got {body.base_version}",
        )
    tenant_id = schedule.tenant_id
    await set_tenant_context(session, tenant_id)

    # Past-date guard
    today = datetime.now(UTC).date()
    for op in body.operations:
        op_date = op.date
        if op_date < today:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=[{"code": "past_date", "message": "不能修改过去的日期", "date": op_date.isoformat()}],
            )

    # Load current assignments into a mutable list of dicts
    existing: list[_AssignmentValues] = [
        {"nurse_id": a.nurse_id, "role_id": a.role_id, "date": a.date,
         "shift_template_id": a.shift_template_id,
         "satisfied_preference": a.satisfied_preference}
        for a in schedule.assignments
    ]

    def _find(
        nurse_id: str,
        d: date,
        shift_id: str,
    ) -> _AssignmentValues | None:
        return next(
            (a for a in existing if a["nurse_id"] == nurse_id
             and a["date"] == d and a["shift_template_id"] == shift_id),
            None,
        )

    # Apply operations in order; duplicate same-cell add = no-op
    affected_slots: set[tuple[date, str]] = set()
    for op in body.operations:
        if op.action == "add":
            affected_slots.add((op.date, op.shift_template_id))
            if not _find(op.nurse_id, op.date, op.shift_template_id):
                existing.append({
                    "nurse_id": op.nurse_id, "role_id": op.role_id or None,
                    "date": op.date, "shift_template_id": op.shift_template_id,
                    "satisfied_preference": None,
                })
        elif op.action == "replace":
            target = _find(op.nurse_id, op.date, op.shift_template_id)
            if target:
                affected_slots.add((op.date, op.shift_template_id))
                target["nurse_id"] = op.new_nurse_id
                target["role_id"] = op.new_role_id or target["role_id"]
        elif op.action == "remove":
            affected_slots.add((op.date, op.shift_template_id))
            existing = [
                a for a in existing
                if not (a["nurse_id"] == op.nurse_id and a["date"] == op.date
                        and a["shift_template_id"] == op.shift_template_id)
            ]

    proposed = [
        ProposedAssignment(a["nurse_id"], a["role_id"], a["date"], a["shift_template_id"])
        for a in existing
    ]

    # Load domain for validation
    domain: DomainData = await load_domain_data(
        session, schedule.tenant_id,
        schedule.period_start, schedule.period_days,
    )
    validation = validate_assignments(
        proposed, domain, schedule.period_start, schedule.period_days,
        affected_slots=affected_slots,
    )

    if not validation.ok:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=validation.hard_violations)
    if validation.needs_override and not body.override_reason:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[{"code": "override_required", "soft_violations": validation.soft_violations}],
        )

    summary = dict(schedule.summary or {})
    summary["num_nurses"] = len({a["nurse_id"] for a in existing})
    summary["num_assignments"] = len(existing)
    preference_assignments = [
        _EditedAssignment(
            nurse_id=a["nurse_id"],
            date=a["date"],
            shift_template_id=a["shift_template_id"],
        )
        for a in existing
    ]
    summary["preference_stats"] = summarize_preferences(domain, preference_assignments)
    for values, preference_assignment in zip(existing, preference_assignments, strict=True):
        values["satisfied_preference"] = preference_assignment.satisfied_preference

    new_schedule = Schedule(
        tenant_id=schedule.tenant_id,
        request_id=request_id,
        period_start=schedule.period_start,
        period_days=schedule.period_days,
        outcome=schedule.outcome,
        objective_value=schedule.objective_value,
        solve_time_seconds=schedule.solve_time_seconds,
        version=await _get_next_schedule_version(session, request_id),
        summary=summary,
    )
    session.add(new_schedule)
    await session.flush()
    for a in existing:
        session.add(Assignment(
            schedule_id=new_schedule.id,
            nurse_id=a["nurse_id"],
            role_id=a["role_id"],
            date=a["date"],
            shift_template_id=a["shift_template_id"],
            satisfied_preference=a["satisfied_preference"],
            tenant_id=schedule.tenant_id,
        ))

    req.active_schedule_id = new_schedule.id
    await record_security_event(
        session,
        action="schedule.assignments.edited",
        actor_id=user.id,
        actor_tenant_id=user.tenant_id,
        tenant_id=schedule.tenant_id,
        details={
            "request_id": request_id,
            "schedule_id": new_schedule.id,
            "base_version": body.base_version,
            "new_version": new_schedule.version,
            "activated": True,
            "operations": [op.model_dump(mode="json") for op in body.operations],
            "override_reason": body.override_reason,
            "warnings": validation.soft_violations,
        },
    )
    await session.commit()

    warnings = [
        ScheduleEditWarning(
            code=v["code"], message=v["message"],
            field=v.get("date"), index=v.get("nurse_id") and hash(v["nurse_id"]) % 10000,
        )
        for v in validation.soft_violations
    ]
    return ScheduleEditResponse(
        version=new_schedule.version,
        warnings=warnings,
        summary=summary,
    )


@router.post("/{request_id}/resolve", response_model=ScheduleResolveResponse)
async def resolve_schedule(
    request_id: str,
    body: ScheduleResolveRequest,
    ctx: SchedulerDep,
) -> ScheduleResolveResponse:
    """Preview a local re-solve while keeping pinned assignments fixed.

    Runs the CP-SAT solver with existing assignments as hard constraints
    for pinned cells; only unlocked cells are re-optimised. On failure the
    original schedule is left untouched. Nothing is persisted until the
    preview is posted to ``/resolve/commit``.
    """
    from app.scheduling.domain import AssignmentResult
    from app.scheduling.engine import RosterCPModel, SolverConfig
    from app.scheduling.exceptions import InfeasibleError, SolverTimeoutError
    from app.scheduling.loader import load_domain_data

    user, _tenant_id, session = ctx
    req = await session.get(ScheduleRequest, request_id)
    if not req:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Schedule request not found")
    if req.status != ScheduleStatus.COMPLETED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Only completed schedules can be re-solved (current: {req.status.value})",
        )

    schedule = await _get_latest_schedule(session, request_id, for_update=True)
    if not schedule:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Schedule not found")
    if schedule.version != body.base_version:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"版本冲突: expected base_version={schedule.version}, got {body.base_version}",
        )
    tenant_id = schedule.tenant_id
    await set_tenant_context(session, tenant_id)

    old_assignments = {
        (a.nurse_id, a.date): a.shift_template_id
        for a in schedule.assignments
    }

    # Determine which existing assignments to pin
    pin_nurse_set = set(body.pin_nurse_ids)
    pin_date_set = set(body.pin_dates)
    def _should_pin(a: Assignment) -> bool:
        if body.pin_all:
            return True
        nurse_ok = a.nurse_id in pin_nurse_set if pin_nurse_set else True
        date_ok = a.date in pin_date_set if pin_date_set else True
        return nurse_ok and date_ok

    # Snapshot pinned assignments before loading the domain so CP-SAT can
    # enforce them while searching for a feasible roster.
    pinned = [
        AssignmentResult(
            nurse_id=a.nurse_id, role_id=a.role_id or "",
            date=a.date, shift_template_id=a.shift_template_id,
        )
        for a in schedule.assignments if _should_pin(a)
    ]
    domain = await load_domain_data(
        session,
        tenant_id,
        schedule.period_start,
        schedule.period_days,
        pinned_assignments=pinned,
    )

    engine = RosterCPModel(domain, SolverConfig())
    try:
        result = engine.solve()
    except (InfeasibleError, SolverTimeoutError) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"局部重排失败，原排班已保留: {exc}",
        ) from exc

    # Safety merge: preserve the stored role exactly, even if the solver
    # normalizes an assignment through a different eligible role.
    by_key = {(a.nurse_id, a.date): a for a in result.assignments}
    for p in pinned:
        key = (p.nurse_id, p.date)
        if key not in by_key:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "局部重排无法保留全部锁定班次，原排班未被修改。",
            )
        merged = AssignmentResult(
            nurse_id=p.nurse_id, role_id=p.role_id,
            date=p.date, shift_template_id=p.shift_template_id,
        )
        if key in by_key:
            by_key[key] = merged
        else:
            result.assignments.append(merged)
            by_key[key] = merged
    result.assignments = list(by_key.values())

    # Compute diff
    new_map = {
        (a.nurse_id, a.date): a.shift_template_id
        for a in result.assignments
    }
    diff: list[ScheduleResolveDiffItem] = []
    all_keys = set(old_assignments) | set(new_map)
    for key in sorted(all_keys):
        old_shift = old_assignments.get(key)
        new_shift = new_map.get(key)
        if old_shift == new_shift:
            continue
        diff.append(ScheduleResolveDiffItem(
            action="changed" if old_shift and new_shift else (
                "added" if new_shift else "removed"
            ),
            nurse_id=key[0],
            date=key[1],
            old_shift_template_id=old_shift,
            new_shift_template_id=new_shift,
        ))

    return ScheduleResolveResponse(
        base_version=schedule.version,
        assignments=[
            ScheduleResolveAssignment(
                nurse_id=a.nurse_id,
                role_id=a.role_id,
                date=a.date,
                shift_template_id=a.shift_template_id,
            )
            for a in result.assignments
        ],
        diff=diff,
        summary=result.preference_stats,
    )


@router.post("/{request_id}/resolve/commit", response_model=ScheduleEditResponse)
async def commit_resolve_schedule(
    request_id: str,
    body: ScheduleResolveCommitRequest,
    ctx: SchedulerDep,
) -> ScheduleEditResponse:
    """Save a previewed local re-solve as a new, immutable Schedule version."""
    from datetime import UTC, datetime

    from app.scheduling.edit_validation import (
        ProposedAssignment,
        summarize_preferences,
        validate_assignments,
    )
    from app.scheduling.loader import load_domain_data

    user, _tenant_id, session = ctx
    req = await session.get(ScheduleRequest, request_id)
    if not req:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Schedule request not found")
    if req.status != ScheduleStatus.COMPLETED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Only completed schedules can be re-solved (current: {req.status.value})",
        )

    schedule = await _get_latest_schedule(session, request_id, for_update=True)
    if not schedule:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Schedule not found")
    if schedule.version != body.base_version:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"版本冲突: expected base_version={schedule.version}, got {body.base_version}",
        )
    tenant_id = schedule.tenant_id
    await set_tenant_context(session, tenant_id)

    # Past-date guard matches manual assignment editing.
    today = datetime.now(UTC).date()
    period_end = schedule.period_start + timedelta(days=schedule.period_days - 1)
    if period_end < today:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "排班周期已结束，不能局部重排。",
        )

    domain = await load_domain_data(
        session,
        tenant_id,
        schedule.period_start,
        schedule.period_days,
    )
    proposed = [
        ProposedAssignment(
            nurse_id=a.nurse_id,
            role_id=a.role_id,
            date=a.date,
            shift_template_id=a.shift_template_id,
        )
        for a in body.assignments
    ]
    validation = validate_assignments(
        proposed,
        domain,
        schedule.period_start,
        schedule.period_days,
    )
    if not validation.ok:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=validation.hard_violations)
    if validation.needs_override and not body.override_reason:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[{"code": "override_required", "soft_violations": validation.soft_violations}],
        )

    new_schedule = Schedule(
        tenant_id=tenant_id,
        request_id=request_id,
        period_start=schedule.period_start,
        period_days=schedule.period_days,
        outcome=schedule.outcome,
        version=await _get_next_schedule_version(session, request_id),
        summary={
            **(schedule.summary or {}),
            "num_nurses": len({a.nurse_id for a in proposed}),
            "num_assignments": len(proposed),
            "preference_stats": summarize_preferences(domain, proposed),
        },
    )
    session.add(new_schedule)
    await session.flush()
    for a in proposed:
        session.add(Assignment(
            tenant_id=tenant_id,
            schedule_id=new_schedule.id,
            nurse_id=a.nurse_id,
            role_id=a.role_id,
            date=a.date,
            shift_template_id=a.shift_template_id,
        ))
    req.active_schedule_id = new_schedule.id
    await session.commit()

    await record_security_event(
        session,
        action="schedule.resolve.committed",
        actor_id=user.id,
        actor_tenant_id=user.tenant_id,
        tenant_id=req.tenant_id,
        details={
            "request_id": request_id,
            "base_version": body.base_version,
            "new_version": new_schedule.version,
            "activated": True,
            "assignment_count": len(proposed),
            "override_reason": body.override_reason,
            "warnings": validation.soft_violations,
        },
    )
    return ScheduleEditResponse(
        version=new_schedule.version,
        warnings=[
            ScheduleEditWarning(
                code=v["code"], message=v["message"],
                field=v.get("date"), index=v.get("nurse_id") and hash(v["nurse_id"]) % 10000,
            )
            for v in validation.soft_violations
        ],
        summary=new_schedule.summary,
    )


@router.get("/{request_id}/export")
async def export_schedule(
    request_id: str,
    ctx: AnyUserDep,
    format: str = Query("csv"),
    view: str = Query("table"),
    version: int | None = Query(None, ge=1),
) -> StreamingResponse:
    """Export the solved schedule as a downloadable file (csv, txt, json or pdf)."""
    if format not in ("csv", "txt", "json", "pdf"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Only 'csv', 'txt', 'json' or 'pdf' format supported",
        )
    if view not in ("table", "calendar"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Only 'table' or 'calendar' view supported",
        )
    user, _tenant_id, session = ctx
    if version is not None and user.role == UserRole.NURSE:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "护士端只能导出生效版本")
    schedule = (
        await _get_schedule_version(session, request_id, version)
        if version is not None
        else await _get_active_schedule(session, request_id)
    )
    if not schedule:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Schedule not ready")
    req = await session.get(ScheduleRequest, request_id)
    operator_name = await _user_email(session, req.requested_by if req else None) or "—"
    await record_security_event(
        session,
        action="schedule.export",
        actor_id=user.id,
        actor_tenant_id=user.tenant_id,
        tenant_id=req.tenant_id if req else _tenant_id,
        details={
            "format": format,
            "view": view,
            "request_id": request_id,
        },
    )

    from app.models import Nurse, ShiftTemplate

    assignments = _visible_assignments(user, schedule)
    shift_ids = {a.shift_template_id for a in assignments}
    nurse_ids = {a.nurse_id for a in assignments}
    shifts = (
        await session.execute(select(ShiftTemplate).where(ShiftTemplate.id.in_(shift_ids)))
    ).scalars().all()
    nurses = (
        await session.execute(select(Nurse).where(Nurse.id.in_(nurse_ids)))
    ).scalars().all()
    shift_by_id = {shift.id: shift.name for shift in shifts}
    nurse_by_id = {nurse.id: f"{nurse.last_name}{nurse.first_name}" for nurse in nurses}

    dates = [
        (schedule.period_start + timedelta(days=offset)).isoformat()
        for offset in range(schedule.period_days)
    ]
    nurse_map: dict[str, dict[str, Any]] = {
        nurse_id: {
            "nurse_name": nurse_by_id.get(nurse_id, nurse_id),
            "cells": ["" for _ in dates],
        }
        for nurse_id in nurse_ids
    }
    date_index = {day: index for index, day in enumerate(dates)}
    for assignment in assignments:
        index = date_index.get(assignment.date.isoformat())
        if index is None:
            continue
        cells = nurse_map[assignment.nurse_id]["cells"]
        cells[index] = shift_by_id.get(assignment.shift_template_id, "?")
    grid = [
        {"nurse_name": nurse_map[nurse_id]["nurse_name"], "cells": nurse_map[nurse_id]["cells"]}
        for nurse_id in sorted(nurse_ids)
    ]
    calendar_months = (
        _calendar_months(dates, assignments, nurse_by_id, shift_by_id)
        if view == "calendar"
        else []
    )
    def _legend_text(shift: ShiftTemplate) -> str:
        brief = _shift_brief(shift)
        day_group = (
            f"，日组 {brief.day_group_name}[{','.join(str(day) for day in brief.day_numbers)}]"
            if brief.day_group_name
            else ""
        )
        return (
            f"{brief.name}（{(brief.start_time or '')[:5]}-"
            f"{(brief.end_time or '')[:5]}{day_group}）"
        )

    legend = [_legend_text(shift) for shift in shifts]

    # Title / metadata block prepended to every export format.
    from datetime import datetime
    period_end = (schedule.period_start).toordinal() + schedule.period_days - 1
    import datetime as _dt
    period_end_date = _dt.date.fromordinal(period_end)
    meta = [
        ("排班结果导出", ""),
        ("操作账号", operator_name),
        ("排班周期", f"{schedule.period_start.isoformat()} ~ {period_end_date.isoformat()}"),
        ("天数", str(schedule.period_days)),
        ("班次安排数", str(len(assignments))),
        ("显示格式", "日历模式" if view == "calendar" else "表格模式"),
        ("涉及班次", "、".join(legend) if legend else "—"),
        ("求解结果", str(schedule.outcome)),
        ("导出时间", datetime.now(_dt.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")),
    ]

    if format == "json":
        import json
        payload = json.dumps(
            {
                "title": "排班结果导出",
                "meta": {k: v for k, v in meta if k},
                "request_id": request_id,
                "period_start": schedule.period_start.isoformat(),
                "period_end": period_end_date.isoformat(),
                "period_days": schedule.period_days,
                "assignment_count": len(assignments),
                "legend": legend,
                "view": view,
                **(
                    {"calendar_months": calendar_months}
                    if view == "calendar"
                    else {"dates": dates, "grid": grid}
                ),
            },
            ensure_ascii=False, indent=2, default=str,
        )
        fname = f"schedule_{request_id[:8]}.json"
        return StreamingResponse(
            iter([payload]),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    if format == "pdf":
        pdf_payload = await asyncio.to_thread(
            _schedule_pdf,
            grid,
            dates,
            legend,
            schedule.period_start.isoformat(),
            schedule.period_days,
            str(schedule.outcome),
            operator_name,
            view,
            calendar_months,
        )
        fname = f"schedule_{request_id[:8]}.pdf"
        return StreamingResponse(
            iter([pdf_payload]),
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    if format == "txt":
        # Aligned plain-text table with a title header, readable / editable in any text editor.
        title_lines = ["排班结果导出", ""]
        for k, v in meta:
            if k == "排班结果导出":
                continue
            title_lines.append(f"{k}: {v}")
        title_lines.append("")
        if view == "calendar":
            for month in calendar_months:
                title_lines.extend([f"【{month['title']}】", " | ".join(WEEKDAY_HEADERS)])
                title_lines.append("-" * 56)
                for offset in range(0, len(month["cells"]), 7):
                    week = month["cells"][offset:offset + 7]
                    title_lines.append(" | ".join(_calendar_text_cell(cell) for cell in week))
                title_lines.append("")
        else:
            headers = ["护士"] + dates
            all_rows = [headers] + [
                [row["nurse_name"]] + [cell or "·" for cell in row["cells"]]
                for row in grid
            ]
            widths = [
                max(len(str(all_rows[i][j])) for i in range(len(all_rows)))
                for j in range(len(headers))
            ]
            lines = []
            for i, r in enumerate(all_rows):
                line = "  ".join(str(r[j]).ljust(widths[j]) for j in range(len(headers)))
                lines.append(line)
                if i == 0:
                    lines.append("  ".join("-" * widths[j] for j in range(len(headers))))
            title_lines.extend(lines)
        payload = "\n".join(title_lines) + "\n"
        fname = f"schedule_{request_id[:8]}.txt"
        return StreamingResponse(
            iter([payload]),
            media_type="text/plain",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    buf = io.StringIO()
    writer = csv.writer(buf)
    # Title / metadata as leading comment rows (prefixed with #) so the data
    # table below stays valid CSV while the file still carries a header block.
    writer.writerow(["# 排班结果导出"])
    for k, v in meta:
        if k == "排班结果导出":
            continue
        writer.writerow([_csv_safe(f"# {k}: {v}")])
    writer.writerow([])
    if view == "calendar":
        for month in calendar_months:
            writer.writerow([_csv_safe(f"# {month['title']}")])
            writer.writerow([_csv_safe(value) for value in WEEKDAY_HEADERS])
            for offset in range(0, len(month["cells"]), 7):
                week = month["cells"][offset:offset + 7]
                writer.writerow([_csv_safe(_calendar_csv_cell(cell)) for cell in week])
            writer.writerow([])
    else:
        writer.writerow([_csv_safe(value) for value in ["护士"] + dates])
        for row in grid:
            writer.writerow(
                [_csv_safe(row["nurse_name"])]
                + [_csv_safe(cell or "·") for cell in row["cells"]]
            )
    buf.seek(0)
    fname = f"schedule_{request_id[:8]}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
