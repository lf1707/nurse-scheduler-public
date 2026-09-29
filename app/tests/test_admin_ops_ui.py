"""Offline checks for admin archive and disk-status controls."""

from __future__ import annotations

import csv
import inspect
import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from app.api.v1 import admin
from app.core.config import settings
from app.core.host_ops import HostDiskUsage
from app.tasks import audit_tasks, schedule_tasks, tenant_tasks


class _DiskUsage:
    total = 100 * 1024**3
    used = 82 * 1024**3
    free = 18 * 1024**3


class _SentTask:
    def __init__(self, task_id: str) -> None:
        self.id = task_id


class _CommittingSession:
    async def commit(self) -> None:
        return None


def _principal(session: Any, tenant_id: str | None = None) -> tuple[Any, Any, Any]:
    return SimpleNamespace(id="admin-id", tenant_id=tenant_id), None, session


@pytest.mark.asyncio
async def test_ops_status_reports_upload_and_disk_pressure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "audit.jsonl.gz.enc"
    manifest = tmp_path / "audit.jsonl.gz.manifest.json"
    marker = tmp_path / "audit.jsonl.gz.manifest.json.uploaded"
    artifact.write_bytes(b"encrypted")
    manifest.write_text("{}")
    marker.write_text("{}")
    watermark = SimpleNamespace(
        last_exported_at=datetime.now(UTC) - timedelta(hours=1),
        created_at=datetime.now(UTC) - timedelta(minutes=30),
        last_export_id="event-id",
        event_count=7,
        artifact_path=str(artifact),
    )

    async def _watermark(_session: Any) -> Any:
        return watermark

    monkeypatch.setattr(settings, "DISK_USAGE_ALERT_PERCENT", 80)
    monkeypatch.setattr(settings, "DISK_USAGE_PATH", str(tmp_path))
    monkeypatch.setattr(
        admin,
        "read_host_disk_usage",
        lambda: HostDiskUsage(
            path="/",
            total=_DiskUsage.total,
            used=_DiskUsage.used,
            free=_DiskUsage.free,
            checked_at=datetime.now(UTC),
        ),
    )
    monkeypatch.setattr(admin, "get_watermark", _watermark)

    status = await admin.get_ops_status(_principal(object()))

    assert status.audit_export.status == "ok"
    assert status.audit_export.hot_retention_days == settings.AUDIT_RETENTION_DAYS_HOT
    assert status.audit_export.cold_retention_days == settings.AUDIT_RETENTION_DAYS_COLD
    assert status.audit_export.artifact_name == artifact.name
    assert status.audit_export.artifact_exists is True
    assert status.audit_export.upload_confirmed is True
    assert status.disk.level == "alert"
    assert status.disk.used_percent == 82.0


@pytest.mark.asyncio
async def test_beat_restart_queues_host_request_without_docker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The API emits an intent file; it never speaks directly to Docker."""
    recorded: list[dict[str, Any]] = []

    async def _record(_session: Any, **kwargs: Any) -> None:
        recorded.append(kwargs)

    monkeypatch.setattr(settings, "OPS_CONTROL_DIR", str(tmp_path))
    monkeypatch.setattr(admin, "record_security_event", _record)
    response = await admin.restart_beat(_principal(_CommittingSession()))

    request_path = tmp_path / "beat-restart.request.json"
    request = json.loads(request_path.read_text())
    assert response["status"] == "queued"
    assert response["request_id"] == request["request_id"]
    assert request["action"] == "restart_beat"
    assert request["actor_id"] == "admin-id"
    assert recorded[0]["action"] == "admin.beat_restart"
    assert recorded[0]["details"] == {"request_id": request["request_id"]}


@pytest.mark.asyncio
async def test_run_audit_export_queues_task_and_records_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[str] = []
    recorded: list[dict[str, Any]] = []

    class _Celery:
        def send_task(self, name: str) -> _SentTask:
            sent.append(name)
            return _SentTask("task-1")

    async def _record(_session: Any, **kwargs: Any) -> None:
        recorded.append(kwargs)

    monkeypatch.setattr(admin, "celery_app", _Celery())
    monkeypatch.setattr(admin, "record_security_event", _record)

    accepted = await admin.run_audit_export(_principal(object()))

    assert sent == ["audit.export_events"]
    assert accepted.task_id == "task-1"
    assert recorded[0]["action"] == "audit.export.triggered"
    assert recorded[0]["details"] == {"task_id": "task-1"}


@pytest.mark.asyncio
async def test_run_audit_retention_queues_policy_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[str] = []
    recorded: list[dict[str, Any]] = []

    class _Celery:
        def send_task(self, name: str) -> _SentTask:
            sent.append(name)
            return _SentTask("retention-1")

    async def _record(_session: Any, **kwargs: Any) -> None:
        recorded.append(kwargs)

    monkeypatch.setattr(admin, "celery_app", _Celery())
    monkeypatch.setattr(admin, "record_security_event", _record)

    accepted = await admin.run_audit_retention(_principal(object()))

    assert sent == ["audit.expire_old_partitions"]
    assert accepted.task_id == "retention-1"
    assert recorded[0]["action"] == "audit.retention.triggered"


@pytest.mark.asyncio
async def test_create_audit_legal_hold_rejects_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []

    class _Result:
        def scalar_one_or_none(self) -> object:
            return object()

    class _Session:
        async def execute(self, stmt: Any) -> _Result:
            calls.append(stmt)
            return _Result()

    async def _fail_record(_session: Any, **_kwargs: Any) -> None:
        raise AssertionError("duplicate hold must not create an audit event")

    monkeypatch.setattr(admin, "record_security_event", _fail_record)

    with pytest.raises(HTTPException) as exc_info:
        await admin.create_audit_legal_hold(
            admin.AuditLegalHoldCreate(
                partition_name="security_audit_events_2026_01",
                reason="active investigation",
            ),
            _principal(_Session()),
        )

    assert exc_info.value.status_code == 409
    assert calls


@pytest.mark.asyncio
async def test_create_audit_legal_hold_records_partition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[dict[str, Any]] = []
    flushed: list[Any] = []
    rolled_back = False

    class _Result:
        def scalar_one_or_none(self) -> None:
            return None

    class _Session:
        async def execute(self, _stmt: Any) -> _Result:
            return _Result()

        def add(self, hold: Any) -> None:
            flushed.append(hold)

        async def flush(self) -> None:
            pass

        async def refresh(self, hold: Any) -> None:
            if hold.id is None:
                hold.id = 1
            if hold.created_at is None:
                hold.created_at = datetime.now(UTC)

        async def rollback(self) -> None:
            nonlocal rolled_back
            rolled_back = True

    async def _record(_session: Any, **kwargs: Any) -> None:
        recorded.append(kwargs)

    monkeypatch.setattr(admin, "record_security_event", _record)
    hold = await admin.create_audit_legal_hold(
        admin.AuditLegalHoldCreate(
            partition_name="security_audit_events_2026_01",
            reason="active investigation",
        ),
        _principal(_Session()),
    )

    assert [item.partition_name for item in flushed] == ["security_audit_events_2026_01"]
    assert hold.created_by == "admin-id"
    assert recorded[0]["action"] == "audit.legal_hold.created"
    assert recorded[0]["details"] == {"partition_name": "security_audit_events_2026_01"}
    assert rolled_back is False


@pytest.mark.asyncio
async def test_create_audit_legal_hold_maps_unique_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Result:
        def scalar_one_or_none(self) -> None:
            return None

    class _Session:
        async def execute(self, _stmt: Any) -> _Result:
            return _Result()

        def add(self, _hold: Any) -> None:
            pass

        async def flush(self) -> None:
            raise IntegrityError("duplicate", None, Exception("unique"))

        async def rollback(self) -> None:
            pass

    async def _fail_record(_session: Any, **_kwargs: Any) -> None:
        raise AssertionError("raced duplicate must not create an audit event")

    monkeypatch.setattr(admin, "record_security_event", _fail_record)

    with pytest.raises(HTTPException) as exc_info:
        await admin.create_audit_legal_hold(
            admin.AuditLegalHoldCreate(
                partition_name="security_audit_events_2026_01",
                reason="active investigation",
            ),
            _principal(_Session()),
        )

    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_delete_audit_legal_hold_records_released_partition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deleted: list[Any] = []
    recorded: list[dict[str, Any]] = []
    hold = SimpleNamespace(id=7, partition_name="security_audit_events_2026_01")

    class _Session:
        async def get(self, _model: Any, hold_id: int) -> Any:
            return hold if hold_id == 7 else None

        async def delete(self, item: Any) -> None:
            deleted.append(item)

    async def _record(_session: Any, **kwargs: Any) -> None:
        recorded.append(kwargs)

    monkeypatch.setattr(admin, "record_security_event", _record)
    await admin.delete_audit_legal_hold(
        7,
        _principal(_Session()),
    )

    assert deleted == [hold]
    assert recorded[0]["action"] == "audit.legal_hold.deleted"
    assert recorded[0]["details"] == {"partition_name": "security_audit_events_2026_01"}


@pytest.mark.asyncio
async def test_delete_missing_audit_legal_hold_returns_404() -> None:
    class _Session:
        async def get(self, _model: Any, _hold_id: int) -> None:
            return None

    with pytest.raises(HTTPException) as exc_info:
        await admin.delete_audit_legal_hold(
            7,
            _principal(_Session()),
        )

    assert exc_info.value.status_code == 404


def test_audit_legal_hold_partition_is_validated() -> None:
    valid = admin.AuditLegalHoldCreate(
        partition_name="security_audit_events_2026_12",
        reason="active investigation",
    )
    assert valid.partition_name == "security_audit_events_2026_12"

    with pytest.raises(ValidationError):
        admin.AuditLegalHoldCreate(
            partition_name="users; drop table audit_legal_holds",
            reason="active investigation",
        )


@pytest.mark.asyncio
async def test_list_audit_partitions_returns_newest_first() -> None:
    partitions = ["security_audit_events_2026_09", "security_audit_events_2026_08"]

    class _Result:
        def __iter__(self) -> Any:
            return iter(SimpleNamespace(name=name) for name in partitions)

    class _Session:
        async def execute(self, _stmt: Any) -> _Result:
            return _Result()

    result = await admin.list_audit_partitions(
        _principal(_Session()),
    )

    assert result == partitions


@pytest.mark.asyncio
async def test_run_anomaly_scan_queues_task_and_records_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[str] = []
    recorded: list[dict[str, Any]] = []

    class _Celery:
        def send_task(self, name: str) -> _SentTask:
            sent.append(name)
            return _SentTask("anomaly-1")

    async def _record(_session: Any, **kwargs: Any) -> None:
        recorded.append(kwargs)

    monkeypatch.setattr(admin, "celery_app", _Celery())
    monkeypatch.setattr(admin, "record_security_event", _record)

    accepted = await admin.run_anomaly_scan(_principal(object()))

    assert sent == ["audit.run_anomaly_scan"]
    assert accepted.task_id == "anomaly-1"
    assert recorded[0]["actor_id"] == "admin-id"
    assert recorded[0]["action"] == "audit.anomaly_scan.triggered"
    assert recorded[0]["details"] == {"task_id": "anomaly-1"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result_value", "expected_detail"),
    [
        ([{"rule": "login_failure_rate", "count": 21}], '[{"rule": "login_failure_rate", "count": 21}]'),
        ([], None),
    ],
)
async def test_anomaly_scan_task_status_serializes_alerts(
    monkeypatch: pytest.MonkeyPatch,
    result_value: Any,
    expected_detail: str | None,
) -> None:
    class _Result:
        state = "SUCCESS"
        result = result_value

        def failed(self) -> bool:
            return False

        def successful(self) -> bool:
            return True

        def ready(self) -> bool:
            return True

    monkeypatch.setattr(admin, "celery_app", SimpleNamespace(AsyncResult=lambda _task_id: _Result()))
    status = await admin.get_anomaly_scan_task(
        "anomaly-1", _principal(object())
    )

    assert status.ready is True
    assert status.successful is True
    assert status.detail == expected_detail


def test_admin_page_exposes_anomaly_scan_task_lookup() -> None:
    html = Path("frontend/templates/admin.html").read_text()
    js = Path("frontend/static/js/admin.js").read_text()

    assert "扫描任务 ID" in html
    assert "查询任务状态" in html
    assert "audit.anomaly_scan.triggered" in html
    assert "queryAnomalyScanTask" in html
    assert "async queryAnomalyScanTask" in js
    assert "/ops/anomaly-scan/tasks/" in js


@pytest.mark.asyncio
async def test_anomaly_scan_schedule_get_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Session:
        async def get(self, _model: Any, _key: Any) -> None:
            return None

    monkeypatch.setattr(
        settings, "ANOMALY_SCAN_SCHEDULE_SECONDS", 3600
    )
    result = await admin.get_anomaly_scan_schedule(
        _principal(_Session())
    )
    assert result.schedule_seconds == 3600


@pytest.mark.asyncio
async def test_anomaly_scan_schedule_put_and_get(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[Any] = []

    class _Row:
        value = 0

    class _Session:
        async def get(self, _model: Any, _key: Any) -> _Row:
            return _Row()

        def add(self, _obj: Any) -> None:
            pass

        async def commit(self) -> None:
            recorded.append("commit")

    async def _record(_session: Any, **kwargs: Any) -> None:
        recorded.append(kwargs)

    monkeypatch.setattr(admin, "record_security_event", _record)

    body = admin.AnomalyScanScheduleUpdate(schedule_seconds=0)
    result = await admin.update_anomaly_scan_schedule(body, _principal(_Session()))
    assert result.schedule_seconds == 0
    audit_entries = [entry for entry in recorded if isinstance(entry, dict)]
    assert audit_entries[-1]["action"] == "admin.anomaly_scan_schedule_update"
    assert "commit" in recorded


def test_test_tenant_create_plan_field() -> None:
    body = admin.TestTenantCreate(plan="free")
    assert body.plan == "free"
    body = admin.TestTenantCreate(plan="demo")
    assert body.plan == "demo"


@pytest.mark.asyncio
async def test_audit_export_task_status_serializes_datetime_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Result:
        state = "SUCCESS"
        result = {"completed_at": datetime(2026, 9, 1, tzinfo=UTC)}

        def failed(self) -> bool:
            return False

        def successful(self) -> bool:
            return True

        def ready(self) -> bool:
            return True

    class _Celery:
        AsyncResult = staticmethod(lambda task_id: _Result())

    monkeypatch.setattr(admin, "celery_app", _Celery())
    status = await admin.get_audit_export_task("task-1", _principal(object()))

    assert status.ready is True
    assert status.successful is True
    assert "2026-09-01" in (status.detail or "")


@pytest.mark.asyncio
async def test_security_audit_csv_download_streams_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = SimpleNamespace(
        id="event-1",
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
        action="auth.login",
        outcome="success",
        actor_id="actor-1",
        actor_tenant_id=None,
        target_user_id=None,
        tenant_id="tenant-1",
        ip_address="127.0.0.1",
        user_agent="pytest",
        details={"email_fingerprint": "abc"},
    )
    recorded: list[dict[str, Any]] = []

    class _StreamResult:
        def __init__(self, rows: list[Any]) -> None:
            self.rows = rows

        def __aiter__(self) -> _StreamResult:
            return self

        async def __anext__(self) -> tuple[Any, ...]:
            if not self.rows:
                raise StopAsyncIteration
            return (self.rows.pop(0),)

    class _Session:
        async def stream(self, _stmt: Any) -> _StreamResult:
            return _StreamResult([event])

    async def _record(_session: Any, **kwargs: Any) -> None:
        recorded.append(kwargs)

    monkeypatch.setattr(admin, "record_security_event", _record)
    response = await admin.export_security_audit_events(
        _principal(_Session()),
        action="auth.login",
        limit=10,
    )
    content = ""
    async for chunk in response.body_iterator:
        content += cast(str, chunk)

    rows = list(csv.reader(io.StringIO(content)))
    assert response.media_type == "text/csv; charset=utf-8"
    assert rows[0][2] == "action"
    assert rows[1][2] == "auth.login"
    assert recorded[0]["action"] == "audit.events.exported"


def test_admin_page_has_archive_and_disk_controls() -> None:
    html = Path("frontend/templates/admin.html").read_text(encoding="utf-8")
    javascript = Path("frontend/static/js/admin.js").read_text(encoding="utf-8")
    assert html.index("创建 / 修复 Demo 租户") < html.index("清理测试数据")
    assert 'x-show="demoMsg"' in html
    assert "flex-basis:100%" in html
    assert "立即压缩归档" in html
    assert "归档与磁盘状态" in html
    assert html.index("归档与磁盘状态") < html.index("Anomaly Scan 调度")
    assert "现在开始扫描" in html
    assert "/api/v1/admin/ops/anomaly-scan" in javascript
    assert "runAnomalyScan" in javascript
    assert "/api/v1/admin/ops/anomaly-scan/tasks/" in javascript
    assert "pollAnomalyScan" in javascript
    assert "/api/v1/admin/ops/status" in javascript
    assert "/api/v1/admin/audit-exports/run" in javascript
    assert "/api/v1/admin/audit-retention/run" in javascript
    assert "运行保留清理" in html
    assert "热保留" in html
    assert "下载 CSV" in html
    assert "/api/v1/admin/audit-events/export.csv" in javascript


def test_admin_page_has_audit_legal_hold_controls() -> None:
    html = Path("frontend/templates/admin.html").read_text(encoding="utf-8")
    javascript = Path("frontend/static/js/admin.js").read_text(encoding="utf-8")
    migration = Path(
        "alembic/versions/e8f2a4c6b9d1_add_audit_legal_holds.py"
    ).read_text(encoding="utf-8")

    assert "审计 Legal Hold" in html
    assert '<dialog x-ref="legalHoldModal"' in html
    assert 'data-testid="legal-hold-modal"' in html
    assert "当前数据库的月度分区" in html
    assert 'id="legal-hold-partition-select"' in html
    assert '<input type="text" x-model="legalHoldForm.partition_name"' not in html
    assert "openLegalHoldForm" in javascript
    assert "closeLegalHoldForm" in javascript
    assert "syncLegalHoldControls" in javascript
    assert html.index("新增 Legal Hold") < html.index("data-testid=\"legal-hold-modal\"")
    assert "createLegalHold" in javascript
    assert "deleteLegalHold" in javascript
    assert "/api/v1/admin/audit-legal-holds" in javascript
    assert "/api/v1/admin/audit-partitions" in javascript
    assert "window.confirm" in javascript
    assert "CREATE TABLE IF NOT EXISTS audit_legal_holds" in migration
    assert "legal_hold_super_all" in migration


def test_my_schedules_loader_uses_server_filtered_mine() -> None:
    javascript = Path("frontend/static/js/my_schedules.js").read_text(encoding="utf-8")
    assert "/api/v1/schedules/mine" in javascript
    assert "assignment_count: item.assignment_count" in javascript
    assert "/result'" not in javascript


def test_expectation_page_explains_same_day_alternatives() -> None:
    html = Path("frontend/templates/my_expectations.html").read_text(encoding="utf-8")
    javascript = Path("frontend/static/js/my_schedules.js").read_text(encoding="utf-8")

    assert "互斥候选" in html
    assert "优先级相同时" in html
    assert "按班次开始时间排序" in html
    assert "改选下一个可行候选" in html
    assert 'x-model.number="preferenceForm.priority"' in html
    assert "preferenceForm: {date:'', shift_template_id:'', request_type:'like', priority:1}" in javascript


@pytest.mark.parametrize(
    ("module_name", "task_name"), [
        (audit_tasks, "_create_partitions"),
        (audit_tasks, "_export_audit"),
        (audit_tasks, "_expire_old_partitions"),
        (audit_tasks, "_run_anomaly_scan"),
        (tenant_tasks, "_expire_applications"),
        (tenant_tasks, "_expire_subscriptions"),
        (schedule_tasks, "_fail_stale_schedule_requests"),
    ],
)
def test_periodic_tasks_use_loop_local_sessions(module_name: Any, task_name: str) -> None:
    source = inspect.getsource(getattr(module_name, task_name))
    assert "task_session_factory()" in source
    assert "engine.dispose()" not in source


def test_legal_hold_schema_is_migration_owned() -> None:
    assert not hasattr(
        __import__("app.core.audit_retention", fromlist=["ensure_legal_holds_table"]),
        "ensure_legal_holds_table",
    )
