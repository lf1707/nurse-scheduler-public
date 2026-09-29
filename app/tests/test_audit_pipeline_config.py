"""Offline config tests for the audit-event pipeline.

Covers the three changes in this security-hardening pass that touch model,
celery application wiring, and the audit Celery tasks:

- ``app.models.platform``: ``AuditExportWatermark`` and the ``created_at``
  composite primary key on ``SecurityAuditEvent`` (required for range
  partitioning).
- ``app.tasks.celery_app``: audit task module registration and the three
  audit beat schedules.
- ``app.tasks.audit_tasks``: Celery task registration and the async wrapper
  functions they build on.

These tests never connect to a broker, a result backend, or a database.  They
assert only on declarative metadata, Celery configuration, and task
registration, which is enough to catch regressions in the wiring itself.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Literal, cast

import pytest
from pytest import MonkeyPatch
from sqlalchemy import DateTime, Table

from app.core.config import settings
from app.models import AuditExportWatermark, SecurityAuditEvent
from app.models.platform import AuditExportWatermark as PlatformAuditExportWatermark
from app.tasks import audit_tasks
from app.tasks import celery_app as celery_module
from app.tasks.celery_app import celery_app


class TestSecurityAuditEventModel:
    """The parent audit table must be partition-friendly."""

    def test_created_at_is_in_composite_primary_key(self) -> None:
        """Partitioning requires the partition key to be part of the PK."""
        table = cast(Table, SecurityAuditEvent.__table__)
        pk_names = {c.name for c in table.primary_key.columns}
        assert "created_at" in pk_names
        assert "id" in pk_names

    def test_created_at_is_not_null_and_tz_aware(self) -> None:
        table = cast(Table, SecurityAuditEvent.__table__)
        col = table.columns["created_at"]
        assert col.nullable is False
        assert cast(DateTime, col.type).timezone is True

    def test_core_audit_columns_present(self) -> None:
        table = cast(Table, SecurityAuditEvent.__table__)
        names = {c.name for c in table.columns}
        assert {"id", "action", "outcome", "details", "created_at"} <= names


class TestAuditExportWatermarkModel:
    """The watermark row must track the last successful audit export."""

    def test_model_is_reexported_from_models(self) -> None:
        assert AuditExportWatermark is PlatformAuditExportWatermark

    def test_watermark_columns_present(self) -> None:
        table = cast(Table, AuditExportWatermark.__table__)
        names = {c.name for c in table.columns}
        assert {
            "last_exported_at",
            "last_export_id",
            "artifact_path",
            "manifest_digest",
            "event_count",
        } <= names

    def test_last_exported_at_is_not_null(self) -> None:
        table = cast(Table, AuditExportWatermark.__table__)
        col = table.columns["last_exported_at"]
        assert col.nullable is False

    def test_optional_fields_allow_null(self) -> None:
        table = cast(Table, AuditExportWatermark.__table__)
        for field in ("last_export_id", "artifact_path", "manifest_digest", "event_count"):
            assert table.columns[field].nullable is True


class TestCeleryApplicationWiring:
    """The audit task module must be wired into the Celery application."""

    def test_audit_tasks_registered_on_import(self) -> None:
        """Importing the audit task module registers its Celery tasks."""
        for name in ("audit.create_partitions", "audit.export_events", "audit.expire_old_partitions"):
            assert name in celery_app.tasks, f"{name} not registered after import"

    def test_audit_beat_schedules_exist(self) -> None:
        keys = set(celery_app.conf.beat_schedule)
        assert {
            "audit-create-partitions",
            "audit-export-events",
            "audit-expire-old-partitions",
        } <= keys

    def test_anomaly_beat_schedule_defaults_to_hourly(self) -> None:
        assert celery_app.conf.beat_schedule["audit-run-anomaly-scan"] == {
            "task": "audit.run_anomaly_scan",
            "schedule": 3600.0,
        }

    def test_anomaly_beat_schedule_can_be_disabled(self, monkeypatch: MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "ANOMALY_SCAN_SCHEDULE_SECONDS", 0)

        assert celery_module._anomaly_beat_entry() == {}

    def test_anomaly_beat_reads_database_value_and_disposes_engine(
        self,
        monkeypatch: MonkeyPatch,
    ) -> None:
        class FakeResult:
            def scalar_one_or_none(self) -> SimpleNamespace:
                return SimpleNamespace(value=123)

        class FakeSession:
            def __enter__(self) -> FakeSession:
                return self

            def execute(self, _query: object) -> FakeResult:
                return FakeResult()

            def __exit__(self, *_args: object) -> Literal[False]:
                return False

        class FakeEngine:
            def __init__(self) -> None:
                self.disposed = False

            def dispose(self) -> None:
                self.disposed = True

        engine = FakeEngine()
        monkeypatch.setattr("sqlalchemy.create_engine", lambda _url: engine)
        monkeypatch.setattr("sqlalchemy.orm.Session", lambda _engine: FakeSession())

        assert celery_module._read_anomaly_schedule() == 123
        assert engine.disposed is True

    def test_beat_schedule_periods(self) -> None:
        schedule = celery_app.conf.beat_schedule
        assert schedule["audit-create-partitions"]["schedule"] == 604_800.0  # weekly
        assert schedule["audit-export-events"]["schedule"] == 86_400.0  # daily
        assert schedule["audit-expire-old-partitions"]["schedule"] == 2_592_000.0  # monthly

    def test_beat_schedule_task_names(self) -> None:
        schedule = celery_app.conf.beat_schedule
        assert schedule["audit-create-partitions"]["task"] == "audit.create_partitions"
        assert schedule["audit-export-events"]["task"] == "audit.export_events"
        assert schedule["audit-expire-old-partitions"]["task"] == "audit.expire_old_partitions"


class TestAuditTasks:
    """The audit Celery tasks must be registered and backed by async wrappers."""

    @pytest.mark.parametrize(
        "task, name",
        [
            (audit_tasks.create_audit_partitions, "audit.create_partitions"),
            (audit_tasks.export_audit_events_task, "audit.export_events"),
            (audit_tasks.expire_old_audit_partitions, "audit.expire_old_partitions"),
        ],
    )
    def test_tasks_are_registered(self, task: object, name: str) -> None:
        assert hasattr(task, "delay")
        registered = celery_app.tasks.get(name)
        assert registered is not None
        assert registered.name == name

    def test_async_wrappers_are_coroutines(self) -> None:
        for wrapper in ("_create_partitions", "_export_audit", "_expire_old_partitions"):
            fn = getattr(audit_tasks, wrapper)
            assert inspect.iscoroutinefunction(fn), wrapper


def test_celery_app_main_name() -> None:
    assert celery_app.main == "nurse_scheduler"
