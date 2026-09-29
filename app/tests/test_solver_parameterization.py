import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.v1.schedules import (
    _effective_solver_config,
    _ensure_within_capacity,
    _estimate_nurse_days,
)
from app.core.config import settings
from app.core.database import async_session_factory, clear_tenant_context, engine
from app.models.enums import ScheduleStatus
from app.models.schedule import ScheduleRequest
from app.scheduling.engine import ProgressCallback
from app.schemas import ScheduleGenerateRequest, SolverConfig
from app.tasks.schedule_tasks import (
    _fail_locked_request,
    _is_stale_running,
    _lock_ttl_seconds,
    _run_generation,
)


def test_solver_config_defaults_to_cpu_count_and_allows_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.schemas.os.cpu_count", lambda: 2)
    assert SolverConfig().num_workers == 2
    assert SolverConfig(num_workers=8).num_workers == 8


def test_capacity_guard_math_and_422(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _estimate_nurse_days(120, 30) == 3_600
    monkeypatch.setattr(settings, "SOLVER_MAX_NURSE_DAYS", 100)
    with pytest.raises(HTTPException) as exc_info:
        _ensure_within_capacity(101)
    assert exc_info.value.status_code == 422
    assert "SOLVER_MAX_NURSE_DAYS" in exc_info.value.detail
    _ensure_within_capacity(100)


def test_period_days_schema_rejects_above_62() -> None:
    with pytest.raises(ValidationError):
        ScheduleGenerateRequest(
            period_start=datetime.now().date(), period_days=63
        )
    body = ScheduleGenerateRequest(period_start=datetime.now().date(), period_days=62)
    assert body.period_days == 62


def test_long_run_caps_effective_workers_at_cpu_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("os.cpu_count", lambda: 2)
    body = ScheduleGenerateRequest(
        period_start=datetime.now().date(),
        period_days=30,
        solver_config=SolverConfig(timeout_seconds=3600, num_workers=8),
    )
    normal = _effective_solver_config(body, 2)
    assert normal["num_workers"] == 8
    assert normal["long_run"] is False

    body.long_run = True
    long_run = _effective_solver_config(body, 2)
    assert long_run["timeout_seconds"] == 3600
    assert long_run["num_workers"] == 2
    assert long_run["long_run"] is True


def test_lock_outlives_long_run_solver_timeout() -> None:
    assert _lock_ttl_seconds({"timeout_seconds": 120}) == 600
    assert _lock_ttl_seconds({"timeout_seconds": 3600}) == 7260


def test_stale_running_redelivery_state_machine() -> None:
    now = datetime.now(UTC)
    assert not _is_stale_running(ScheduleStatus.RUNNING, now - timedelta(seconds=119), now, 60)
    assert _is_stale_running(ScheduleStatus.RUNNING, now - timedelta(seconds=121), now, 60)
    assert not _is_stale_running(ScheduleStatus.PENDING, now - timedelta(seconds=999), now, 60)


async def test_run_generation_marks_request_failed_on_persist_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @pytest.fixture(autouse=True)
    async def _dispose_database_engine() -> AsyncGenerator[None, None]:
        yield
        from app.core.database import engine

        await engine.dispose()

    async def fake_load_request_header(_request_id: str) -> tuple[Any, ...]:
        return ("tenant-id", __import__("datetime").date(2026, 1, 1), 1, {}, None, None, None)

    async def fake_fail_stale_redelivery(*_args: Any) -> bool:
        return False

    async def fake_load_domain_data(*_args: Any, **_kwargs: Any) -> object:
        return SimpleNamespace(
            nurses=[object()],
            shift_templates=[object()],
        )

    class Engine:
        def __init__(self, *_args: object) -> None:
            pass

        def solve(self, _callback: Any, _should_stop: Any = None) -> object:
            return object()

    statuses: list[tuple[Any, ...]] = []

    async def fake_set_request_status(
        request_id: str,
        tenant_id: str,
        new_status: ScheduleStatus,
        **_kwargs: Any,
    ) -> None:
        statuses.append((request_id, tenant_id, new_status))

    async def fake_persist_solution(*_args: Any) -> None:
        raise RuntimeError("persist failed")

    def fake_progress_callback(request_id: str) -> ProgressCallback:
        def callback(stage: str, progress: int, message: str) -> None:
            callbacks.append((request_id, stage, progress, message))

        return callback

    callbacks: list[tuple[str, str, int, str]] = []
    monkeypatch.setattr("app.tasks.schedule_tasks._load_request_header", fake_load_request_header)
    monkeypatch.setattr(
        "app.tasks.schedule_tasks._fail_stale_redelivery", fake_fail_stale_redelivery
    )
    monkeypatch.setattr("app.tasks.schedule_tasks.load_domain_data", fake_load_domain_data)
    monkeypatch.setattr("app.tasks.schedule_tasks.RosterCPModel", Engine)
    monkeypatch.setattr("app.tasks.schedule_tasks._set_request_status", fake_set_request_status)
    monkeypatch.setattr("app.tasks.schedule_tasks._persist_solution", fake_persist_solution)
    monkeypatch.setattr("app.tasks.schedule_tasks._progress_cb", fake_progress_callback)

    await _run_generation("request-id")

    assert statuses == [
        ("request-id", "tenant-id", ScheduleStatus.RUNNING),
        ("request-id", "tenant-id", ScheduleStatus.FAILED),
    ]
    assert callbacks[-1] == (
        "request-id",
        "failed",
        100,
        "生成过程中发生系统错误，请重试或联系管理员",
    )


async def test_run_generation_marks_empty_result_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_load_request_header(_request_id: str) -> tuple[Any, ...]:
        return ("tenant-id", datetime(2026, 1, 1).date(), 7, {}, None, None, None)

    async def fake_fail_stale_redelivery(*_args: Any) -> bool:
        return False

    async def fake_load_domain_data(*_args: Any, **_kwargs: Any) -> object:
        return SimpleNamespace(nurses=[object()], shift_templates=[object()])

    class EmptyResult:
        assignments: list[Any] = []
        outcome = "optimal"

    class Engine:
        def __init__(self, *_args: object) -> None:
            pass

        def solve(self, _callback: Any, _should_stop: Any = None) -> EmptyResult:
            return EmptyResult()

    class FakeSessionScope:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_args: Any) -> bool:
            return False

    def fake_session_scope(**_kwargs: Any) -> FakeSessionScope:
        return FakeSessionScope()

    statuses: list[tuple[Any, ...]] = []
    callbacks: list[tuple[str, str, int, str]] = []

    async def fake_set_request_status(
        _request_id: str,
        _tenant_id: str,
        new_status: ScheduleStatus,
        *,
        set_started: bool = False,
        error_message: str | None = None,
        stats: dict[str, Any] | None = None,
        set_completed: bool = False,
    ) -> None:
        if set_completed:
            statuses.append((new_status, error_message, stats, set_completed))

    def fake_progress_callback(request_id: str) -> ProgressCallback:
        def callback(stage: str, progress: int, message: str) -> None:
            callbacks.append((request_id, stage, progress, message))

        return callback

    monkeypatch.setattr(
        "app.tasks.schedule_tasks._load_request_header", fake_load_request_header
    )
    monkeypatch.setattr(
        "app.tasks.schedule_tasks._fail_stale_redelivery", fake_fail_stale_redelivery
    )
    monkeypatch.setattr("app.tasks.schedule_tasks.load_domain_data", fake_load_domain_data)
    monkeypatch.setattr(
        "app.tasks.schedule_tasks.session_scope", fake_session_scope
    )
    monkeypatch.setattr("app.tasks.schedule_tasks.RosterCPModel", Engine)
    monkeypatch.setattr("app.tasks.schedule_tasks._set_request_status", fake_set_request_status)
    monkeypatch.setattr(
        "app.tasks.schedule_tasks._persist_solution",
        lambda *_args: (_ for _ in ()).throw(AssertionError("empty result must not persist")),
    )
    monkeypatch.setattr("app.tasks.schedule_tasks._progress_cb", fake_progress_callback)

    await _run_generation("request-id")

    message = "排班结果为空：当前生效约束没有要求任何班次。请检查护士合同目标班次与技能组合规则。"
    assert statuses == [(ScheduleStatus.FAILED, message, {"num_assignments": 0}, True)]
    assert callbacks[-1] == ("request-id", "failed", 100, message)


@pytest.mark.asyncio(loop_scope="function")
async def test_run_generation_marks_request_failed_on_unexpected_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_load_request_header(_request_id: str) -> tuple[Any, ...]:
        return ("tenant-id", __import__("datetime").date(2026, 1, 1), 1, {}, None, None, None)

    async def fake_fail_stale_redelivery(*_args: Any) -> bool:
        return False

    async def fake_set_request_status(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("status must not be updated for this focused unit test")

    monkeypatch.setattr("app.tasks.schedule_tasks._load_request_header", fake_load_request_header)
    monkeypatch.setattr(
        "app.tasks.schedule_tasks._fail_stale_redelivery", fake_fail_stale_redelivery
    )
    monkeypatch.setattr(
        "app.tasks.schedule_tasks._set_request_status", fake_set_request_status
    )
    monkeypatch.setattr(
        "app.tasks.schedule_tasks.RosterCPModel",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("load model failed")),
    )

    await _run_generation("request-id")


@pytest.mark.asyncio(loop_scope="function")
async def test_locked_request_writes_failed_status_to_database() -> None:
    request_id = "locked-test-request"
    tenant_id = "locked-test-tenant"
    from app.models import Tenant

    await engine.dispose()
    tenant = None
    request = ScheduleRequest(
        id=request_id,
        tenant_id=tenant_id,
        period_start=datetime.now(UTC).date(),
        period_days=1,
        request_date=datetime.now(UTC).date(),
        daily_sequence=uuid.uuid4().int % 1_000_000_000,
        solver_config={},
    )
    try:
        async with async_session_factory() as session:
            await clear_tenant_context(session)
            tenant = await session.get(Tenant, tenant_id)
            if tenant is not None:
                await session.delete(tenant)
                await session.commit()
            session.add(Tenant(id=tenant_id, name="Locked Test Tenant", slug=tenant_id))
            await session.commit()

        async with async_session_factory() as session:
            await clear_tenant_context(session)
            session.add(request)
            await session.commit()

        message = await _fail_locked_request(request_id, tenant_id)

        assert message == "该租户已有正在进行的排班任务"
        async with async_session_factory() as session:
            await clear_tenant_context(session)
            stored_request = await session.get(ScheduleRequest, request_id)
            assert stored_request is not None
            assert stored_request.status == ScheduleStatus.FAILED
            assert stored_request.error_message == message
            assert stored_request.completed_at is not None
    finally:
        async with async_session_factory() as session:
            await clear_tenant_context(session)
            stored_request = await session.get(ScheduleRequest, request_id)
            if stored_request is not None:
                await session.delete(stored_request)
            await session.commit()
        async with async_session_factory() as session:
            await clear_tenant_context(session)
            tenant = await session.get(Tenant, tenant_id)
            if tenant is not None:
                await session.delete(tenant)
            await session.commit()


async def test_fail_locked_request_tolerates_database_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unavailable(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(
        "app.tasks.schedule_tasks._set_request_status",
        unavailable,
    )

    message = await _fail_locked_request("request-id", "tenant-id")

    assert message == "该租户已有正在进行的排班任务"
