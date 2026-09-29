"""Celery task: generate a schedule for a ScheduleRequest.

Pipeline (per task invocation):
    1. Acquire a per-tenant Redis lock (prevents concurrent generation for the
       same tenant).
    2. Load the ScheduleRequest row, flip status → RUNNING.
    3. load_domain_data (async) → RosterCPModel.solve (sync, CP-SAT).
    4. Persist Schedule + Assignments (async bulk insert).
    5. Flip status → COMPLETED (or FAILED on exception) + write stats/error.

The Celery task itself is synchronous; async DB/solve work is driven by an
internal asyncio loop via `asyncio.run`. CP-SAT solving runs in a thread so
it doesn't block the loop's progress callbacks.

Redelivery guard: ``acks_late=True`` can redeliver a task after a worker is
OOM-killed. Once a request has been RUNNING for twice its configured timeout,
it is failed instead of solved again. A crash before that first RUNNING write
remains a small residual race; the guard specifically protects the long solve.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
import time
import uuid
from datetime import timedelta
from typing import Any

import redis as sync_redis
from celery import Task
from sqlalchemy import (
    String,
    cast,
    create_engine,
    delete,
    func,
    select,
    text,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.core.audit import record_security_event
from app.core.config import settings
from app.core.database import (
    async_session_factory,
    session_scope,
    set_tenant_context,
    task_session_factory,
)
from app.models import Assignment, Schedule, ScheduleRequest
from app.models.enums import ScheduleStatus, SolverOutcome
from app.scheduling.diagnostics import explain_infeasibility
from app.scheduling.domain import SolveResult
from app.scheduling.engine import ProgressCallback, RosterCPModel, SolverConfig
from app.scheduling.exceptions import (
    ConcurrentGenerationError,
    InfeasibleError,
    ModelInvalidError,
    ScheduleCancelledError,
    SolverTimeoutError,
)
from app.scheduling.loader import load_domain_data
from app.tasks.celery_app import celery_app

log = logging.getLogger(__name__)

# Lock TTL: at least twice the solver timeout so a still-running long job is
# not opened to a duplicate worker; the floor handles short solve timeouts.
_LOCK_TTL_SECONDS = 600


def _lock_key(tenant_id: str) -> str:
    return f"schedule_lock:{tenant_id}"


def _lock_ttl_seconds(solver_config: dict[str, Any]) -> int:
    timeout_seconds = int(
        solver_config.get("timeout_seconds", settings.SOLVER_TIMEOUT_SECONDS)
    )
    return max(_LOCK_TTL_SECONDS, timeout_seconds * 2 + 60)


def _is_stale_running(
    status: ScheduleStatus,
    started_at: datetime.datetime | None,
    now: datetime.datetime,
    timeout_seconds: int,
) -> bool:
    """Whether a RUNNING solve is old enough to be a crash redelivery."""
    if status != ScheduleStatus.RUNNING or started_at is None:
        return False
    threshold = datetime.timedelta(seconds=max(1, timeout_seconds * 2))
    return now - started_at > threshold


def _progress_cb(request_id: str) -> ProgressCallback:
    """Build a progress callback that logs stage changes for this request."""

    def _cb(stage: str, progress: int, message: str) -> None:
        log.debug(
            "Schedule %s progress: %s %s%% %s",
            request_id,
            stage,
            progress,
            message,
        )

    return _cb


async def _persist_solution(
    tenant_id: str,
    request_id: str,
    period_start: datetime.date,
    period_days: int,
    result: SolveResult,
) -> str:
    """Write the solved Schedule + Assignments to DB. Returns the Schedule.id."""
    unique_assignments = {
        (a.nurse_id, a.date, a.shift_template_id): a for a in result.assignments
    }
    schedule = Schedule(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        request_id=request_id,
        period_start=period_start,
        period_days=period_days,
        outcome=SolverOutcome(result.outcome),
        objective_value=result.objective_value or None,
        solve_time_seconds=result.solve_time_seconds,
        summary={
            "num_nurses": result.num_nurses,
            "num_assignments": len(unique_assignments),
            "num_conflicts": result.num_conflicts,
            "num_booleans": result.num_booleans,
            "preference_stats": result.preference_stats,
        },
    )
    schedule.assignments = [
        Assignment(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            schedule_id=schedule.id,
            nurse_id=a.nurse_id,
            role_id=a.role_id or None,
            date=a.date,
            shift_template_id=a.shift_template_id,
            satisfied_preference=a.satisfied_preference,
        )
        for a in unique_assignments.values()
    ]
    async with session_scope(tenant_id=tenant_id) as session:
        session.add(schedule)
        await session.flush()
        request = await session.get(ScheduleRequest, request_id)
        if request is None:
            raise RuntimeError(f"Schedule request {request_id} not found")
        request.active_schedule_id = schedule.id
    return schedule.id


async def _set_request_status(
    request_id: str,
    tenant_id: str,
    status: ScheduleStatus,
    *,
    outcome: SolverOutcome | None = None,
    error_message: str | None = None,
    stats: dict[str, Any] | None = None,
    set_started: bool = False,
    set_completed: bool = False,
) -> None:
    """Update a non-cancelled request's status + optional fields."""
    async with session_scope(tenant_id=tenant_id) as session:
        values: dict[str, object] = {"status": status}
        if outcome is not None:
            values["outcome"] = outcome
        if error_message is not None:
            values["error_message"] = error_message
        if stats is not None:
            values["stats"] = stats
        if set_started:
            values["started_at"] = datetime.datetime.now(datetime.UTC)
        if set_completed:
            values["completed_at"] = datetime.datetime.now(datetime.UTC)
        guard = [ScheduleRequest.id == request_id]
        if status != ScheduleStatus.CANCELLED:
            guard.append(
                func.lower(cast(ScheduleRequest.status, String))
                != ScheduleStatus.CANCELLED.value
            )
        await session.execute(
            update(ScheduleRequest).where(*guard).values(**values)
        )
        if status != ScheduleStatus.CANCELLED:
            row_status = await session.execute(
                select(func.lower(cast(ScheduleRequest.status, String))).where(
                    ScheduleRequest.id == request_id
                )
            )
            if row_status.scalar_one_or_none() == ScheduleStatus.CANCELLED.value:
                await session.execute(
                    delete(Schedule).where(Schedule.request_id == request_id)
                )


class _CancellationChecker:
    """Rate-limited synchronous cancellation probe for the solver thread."""

    _interval_seconds = 1.0

    def __init__(self, request_id: str):
        sync_url = settings.DATABASE_URL.replace("+asyncpg", "+psycopg2")
        self._engine = create_engine(sync_url, poolclass=NullPool)
        self._request_id = request_id
        self._next_check = 0.0

    def __call__(self) -> bool:
        now = time.monotonic()
        if now < self._next_check:
            return False
        self._next_check = now + self._interval_seconds
        try:
            with Session(self._engine) as session:
                session.execute(text("SET app.is_super = '1'"))
                status = session.execute(
                    select(func.lower(cast(ScheduleRequest.status, String))).where(
                        ScheduleRequest.id == self._request_id
                    )
                ).scalar_one_or_none()
                return status == ScheduleStatus.CANCELLED.value
        except Exception:
            log.exception(
                "Unable to check cancellation state for schedule request %s",
                self._request_id,
            )
            return False

    def close(self) -> None:
        self._engine.dispose()


async def _load_request_header(
    request_id: str,
) -> (
    tuple[
        str,
        datetime.date,
        int,
        dict[str, Any],
        list[str] | None,
        list[str] | None,
        list[str] | None,
    ]
    | None
):
    """Read the immutable header fields of a ScheduleRequest (super-admin scope).

    The worker connects as the non-privileged app role, so it must enable the
    super-admin RLS bypass (app.is_super) to read any tenant's request header
    before switching to that tenant's context for the solve.
    Returns (tenant_id, period_start, period_days, solver_config, nurse_ids,
             skill_mix_rule_ids, shift_sequence_rule_ids) or None.
    """
    from app.core.database import clear_tenant_context

    async with async_session_factory() as session:
        await clear_tenant_context(session)
        req = (
            await session.execute(select(ScheduleRequest).where(ScheduleRequest.id == request_id))
        ).scalar_one_or_none()
        if not req:
            return None
        return (
            req.tenant_id,
            req.period_start,
            req.period_days,
            req.solver_config or {},
            req.nurse_ids,
            req.skill_mix_rule_ids,
            req.shift_sequence_rule_ids,
        )


async def _fail_stale_redelivery(
    request_id: str,
    tenant_id: str,
    solver_config: dict[str, Any],
    progress_cb: ProgressCallback,
) -> bool:
    """Fail a RUNNING request whose solve age exceeds twice its timeout."""
    timeout_seconds = int(
        solver_config.get("timeout_seconds", settings.SOLVER_TIMEOUT_SECONDS)
    )
    now = datetime.datetime.now(datetime.UTC)
    stale = False
    async with session_scope(tenant_id=tenant_id) as session:
        request = (
            await session.execute(
                select(ScheduleRequest).where(ScheduleRequest.id == request_id)
            )
        ).scalar_one_or_none()
        stale = request is not None and _is_stale_running(
            request.status,
            request.started_at,
            now,
            timeout_seconds,
        )
    if not stale:
        return False

    message = "检测到 worker 中断后重投递；已停止本次任务，请重新发起排班。"
    progress_cb("failed", 100, message)
    await _set_request_status(
        request_id,
        tenant_id,
        ScheduleStatus.FAILED,
        error_message=message,
        set_completed=True,
    )
    return True


async def _fail_locked_request(request_id: str, tenant_id: str) -> str:
    message = "该租户已有正在进行的排班任务"
    with contextlib.suppress(Exception):
        await _set_request_status(
            request_id,
            tenant_id,
            ScheduleStatus.FAILED,
            error_message=message,
            set_completed=True,
        )
    return message


def _stale_cutoff(now: datetime.datetime, timeout_seconds: int) -> datetime.datetime:
    return now - timedelta(seconds=max(1, timeout_seconds * 2))


async def _fail_stale_schedule_requests() -> list[str]:
    """Mark stale RUNNING requests failed so crashed workers self-heal."""
    now = datetime.datetime.now(datetime.UTC)
    async with task_session_factory() as task_sessions:
        async with task_sessions() as session:
            from app.core.database import clear_tenant_context

            await clear_tenant_context(session)
            requests = (
                await session.execute(
                    select(ScheduleRequest).where(
                        func.lower(cast(ScheduleRequest.status, String))
                        == ScheduleStatus.RUNNING.value,
                        ScheduleRequest.started_at.is_not(None),
                    )
                )
            ).scalars().all()
            stale = [
                request for request in requests
                if _is_stale_running(
                    request.status,
                    request.started_at,
                    now,
                    int(request.solver_config.get("timeout_seconds", settings.SOLVER_TIMEOUT_SECONDS)),
                )
            ]
        failed_ids: list[str] = []
        for request in stale:
            message = request.error_message or "排班任务因 worker 中断未完成，且未记录具体失败原因"
            if await _fail_stale_request(
                request.id,
                request.tenant_id,
                message,
                session_factory=task_sessions,
            ):
                failed_ids.append(request.id)
        return failed_ids


async def _fail_stale_request(
    request_id: str,
    tenant_id: str,
    message: str,
    *,
    session_factory: async_sessionmaker[AsyncSession],
) -> bool:
    async with session_factory() as session:
        await set_tenant_context(session, tenant_id)
        request = (
            await session.execute(
                select(ScheduleRequest).where(
                    ScheduleRequest.id == request_id,
                    ScheduleRequest.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if not request or request.status != ScheduleStatus.RUNNING:
            return False
        request.status = ScheduleStatus.FAILED
        request.error_message = message[:1000]
        request.completed_at = datetime.datetime.now(datetime.UTC)
        await record_security_event(
            session,
            action="schedule.generate.stale_failure",
            outcome="failure",
            actor_id=request.requested_by,
            actor_tenant_id=tenant_id,
            tenant_id=tenant_id,
            details={"request_id": request_id, "reason": "stale_running"},
        )
        await session.commit()
        return True


async def _run_generation(request_id: str) -> None:
    """The async core: load → solve → persist, with status transitions."""
    header = await _load_request_header(request_id)
    if not header:
        log.error("ScheduleRequest %s not found", request_id)
        return
    tenant_id, period_start, period_days, solver_cfg, nurse_ids, sm_rule_ids, ss_rule_ids = header
    cb = _progress_cb(request_id)

    if await _fail_stale_redelivery(request_id, tenant_id, solver_cfg, cb):
        return

    try:
        # 1. Flip status → RUNNING.
        await _set_request_status(request_id, tenant_id, ScheduleStatus.RUNNING, set_started=True)

        # 2. Load domain data (async, RLS-scoped).
        cb("loading", 5, "Loading tenant data")
        async with session_scope(tenant_id=tenant_id) as session:
            domain = await load_domain_data(
                session, tenant_id, period_start, period_days,
                nurse_ids=nurse_ids,
                skill_mix_rule_ids=sm_rule_ids,
                shift_sequence_rule_ids=ss_rule_ids,
            )

        # Guard against an empty/under-configured tenant: with no nurses or no
        # shift templates the solver would "succeed" with 0 assignments (nothing
        # forces any), producing a misleading completed-but-empty result.
        if not domain.nurses:
            msg = "该租户没有可排班的护士，无法生成排班"
            cb("failed", 100, msg)
            await _set_request_status(
                request_id, tenant_id, ScheduleStatus.FAILED,
                error_message=msg, set_completed=True,
            )
            return
        if not domain.shift_templates:
            msg = "该租户没有班次模板，无法生成排班"
            cb("failed", 100, msg)
            await _set_request_status(
                request_id, tenant_id, ScheduleStatus.FAILED,
                error_message=msg, set_completed=True,
            )
            return

        # 3. Solve (sync CP-SAT) in a thread so progress callbacks can fire.
        config = SolverConfig(
            timeout_seconds=solver_cfg.get("timeout_seconds", settings.SOLVER_TIMEOUT_SECONDS),
            num_workers=solver_cfg.get("num_workers", settings.SOLVER_NUM_WORKERS),
        )
        engine = RosterCPModel(domain, config)

        try:
            cancellation_checker = _CancellationChecker(request_id)
            try:
                result = await asyncio.to_thread(engine.solve, cb, cancellation_checker)
            finally:
                cancellation_checker.close()
        except ScheduleCancelledError:
            message = "排班任务已被取消"
            cb("failed", 100, message)
            await _set_request_status(
                request_id, tenant_id, ScheduleStatus.CANCELLED,
                error_message=message, set_completed=True,
            )
            return
        except (InfeasibleError, SolverTimeoutError, ModelInvalidError) as e:
            failure_reasons: list[str] | None = None
            message = str(e)
            if isinstance(e, InfeasibleError):
                failure_reasons = explain_infeasibility(domain)
                if not failure_reasons:
                    # Structural heuristics found nothing — run CP-SAT relaxation
                    # analysis to pinpoint the bottleneck constraint group.
                    relaxation_reasons = engine.diagnose_infeasibility_by_relaxation()
                    if relaxation_reasons:
                        failure_reasons = relaxation_reasons
                    else:
                        failure_reasons = [
                            "CP-SAT 判定约束不可满足，但未识别出明确的结构性冲突"
                        ]
                message = f"排班约束不可满足：{'；'.join(failure_reasons)}"
            cb("failed", 100, message)
            await _set_request_status(
                request_id, tenant_id, ScheduleStatus.FAILED,
                error_message=message[:1000],
                stats={"failure_reasons": failure_reasons} if failure_reasons else None,
                set_completed=True,
            )
            return

        if not result.assignments:
            message = (
                "排班结果为空：当前生效约束没有要求任何班次。"
                "请检查护士合同目标班次与技能组合规则。"
            )
            cb("failed", 100, message)
            await _set_request_status(
                request_id, tenant_id, ScheduleStatus.FAILED,
                error_message=message,
                stats={"num_assignments": 0},
                set_completed=True,
            )
            return

        # 4. Persist Schedule + Assignments.
        cb("persisting", 95, "Saving schedule")
        schedule_id = await _persist_solution(
            tenant_id, request_id, period_start, period_days, result
        )

        # 5. Flip status → COMPLETED.
        await _set_request_status(
            request_id, tenant_id, ScheduleStatus.COMPLETED,
            outcome=SolverOutcome(result.outcome),
            stats={
                "schedule_id": schedule_id,
                "objective_value": result.objective_value,
                "solve_time_seconds": result.solve_time_seconds,
                "num_assignments": len(
                    {
                        (assignment.nurse_id, assignment.date, assignment.shift_template_id)
                        for assignment in result.assignments
                    }
                ),
                "num_conflicts": result.num_conflicts,
                "nurse_days": len(domain.nurses) * period_days,
            },
            set_completed=True,
        )
        cb("done", 100, f"Schedule {schedule_id[:8]} ready")
    except ConcurrentGenerationError as e:
        message = str(e)
        cb("failed", 100, message)
        with contextlib.suppress(Exception):
            await _set_request_status(
                request_id, tenant_id, ScheduleStatus.FAILED,
                error_message=message, set_completed=True,
            )
    except Exception:
        message = "生成过程中发生系统错误，请重试或联系管理员"
        log.exception("Schedule generation failed unexpectedly for request %s", request_id)
        cb("failed", 100, message)
        with contextlib.suppress(Exception):
            await _set_request_status(
                request_id, tenant_id, ScheduleStatus.FAILED,
                error_message=message, set_completed=True,
            )


class ScheduleTask(Task):  # type: ignore[misc]
    """Base task marker — per-tenant locking is handled in the task body."""
    pass


@celery_app.task(
    bind=True,
    base=ScheduleTask,
    name="schedule.generate",
    max_retries=0,
    acks_late=True,
    soft_time_limit=3900,
    time_limit=3930,
)  # type: ignore[untyped-decorator]
def generate_schedule_task(self: Task, request_id: str) -> dict[str, Any]:
    """Celery entrypoint: generate the schedule for ``request_id``.

    All async work (peek + generate) runs inside ONE asyncio.run / event loop —
    the SQLAlchemy async engine's pooled connections are loop-bound and break
    if reused across separate asyncio.run calls.
    """
    r = sync_redis.from_url(settings.REDIS_URL, decode_responses=True)

    async def _main() -> dict[str, Any]:
        from app.core.database import engine

        try:
            await engine.dispose()
            header = await _load_request_header(request_id)
            if not header:
                log.error("ScheduleRequest %s not found; nothing to do", request_id)
                return {"request_id": request_id, "status": "not_found"}
            tenant_id = header[0]

            lock = r.lock(
                _lock_key(tenant_id), timeout=_lock_ttl_seconds(header[3])
            )
            if not lock.acquire(blocking=False):
                msg = await _fail_locked_request(request_id, tenant_id)
                log.warning(msg)
                return {"request_id": request_id, "status": "locked", "detail": msg}

            try:
                await _run_generation(request_id)
                return {"request_id": request_id, "status": "done"}
            finally:
                with contextlib.suppress(Exception):
                    lock.release()  # lock may have expired
        finally:
            await engine.dispose()

    try:
        return asyncio.run(_main())
    finally:
        r.close()


@celery_app.task(name="schedule.fail_stale_requests")  # type: ignore[untyped-decorator]
def fail_stale_requests_task() -> dict[str, Any]:
    """Periodically fail RUNNING requests abandoned by lost workers."""
    failed_ids = asyncio.run(_fail_stale_schedule_requests())
    return {"failed_request_ids": failed_ids}
