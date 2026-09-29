"""Tests for system-health Celery tasks."""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pytest import MonkeyPatch

from app.core.config import settings
from app.tasks import system_tasks
from app.tasks.celery_app import celery_app


class _DiskUsage:
    def __init__(self, used_percent: float, free_gb: float = 20.0):
        self.total = 1_000
        self.used = int(used_percent * 10)
        self.free = int(free_gb * 1024**3)


def test_disk_task_registered_and_scheduled() -> None:
    assert "system.check_disk_usage" in celery_app.tasks
    assert celery_app.conf.beat_schedule["system-check-disk-usage"] == {
        "task": "system.check_disk_usage",
        "schedule": 3600.0,
    }


def test_beat_state_uses_configured_persistent_path() -> None:
    assert celery_app.conf.beat_schedule_filename == (
        settings.CELERY_BEAT_SCHEDULE_FILENAME
    )


def test_disk_check_is_async_helper() -> None:
    assert inspect.iscoroutinefunction(system_tasks._check_disk_usage)


@pytest.mark.asyncio
async def test_disk_check_below_threshold_does_not_alert(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "DISK_USAGE_ALERT_PERCENT", 80)
    monkeypatch.setattr(settings, "OPS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        settings, "HOST_METRICS_PATH", str(tmp_path / "missing-host-metrics.json")
    )
    monkeypatch.setattr(
        "app.core.host_ops.shutil.disk_usage", lambda _path: _DiskUsage(69)
    )
    delivery = AsyncMock()
    monkeypatch.setattr(system_tasks, "send_email", delivery)

    result = await system_tasks._check_disk_usage()

    assert result["level"] == "none"
    assert result["triggered"] is False
    assert result["alert_delivery"] == "skipped"
    delivery.assert_not_called()
    assert json.loads((tmp_path / "disk-usage.json").read_text())["level"] == "none"


@pytest.mark.asyncio
async def test_disk_check_at_threshold_sends_alert(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "DISK_USAGE_ALERT_PERCENT", 80)
    monkeypatch.setattr(settings, "ALERT_EMAIL_ENABLED", True)
    monkeypatch.setattr(settings, "ALERT_EMAIL_RECIPIENT", "ops@example.com")
    monkeypatch.setattr(settings, "OPS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        settings, "HOST_METRICS_PATH", str(tmp_path / "missing-host-metrics.json")
    )
    monkeypatch.setattr(
        "app.core.host_ops.shutil.disk_usage", lambda _path: _DiskUsage(80.5)
    )
    delivery = AsyncMock()
    monkeypatch.setattr(system_tasks, "send_email", delivery)

    result = await system_tasks._check_disk_usage()

    assert result["level"] == "alert"
    assert result["alert_delivery"] == "email"
    delivery.assert_awaited_once()
    assert delivery.await_args is not None
    assert (
        delivery.await_args.args[0].subject
        == "[ALERT] System health: disk usage watermark"
    )


@pytest.mark.asyncio
async def test_disk_check_retries_then_dead_letters(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "DISK_USAGE_ALERT_PERCENT", 80)
    monkeypatch.setattr(settings, "ALERT_EMAIL_ENABLED", True)
    monkeypatch.setattr(settings, "ALERT_EMAIL_RECIPIENT", "ops@example.com")
    monkeypatch.setattr(settings, "ALERT_EMAIL_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(settings, "ALERT_EMAIL_RETRY_SECONDS", 0)
    monkeypatch.setattr(settings, "OPS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        settings, "HOST_METRICS_PATH", str(tmp_path / "missing-host-metrics.json")
    )
    monkeypatch.setattr(
        "app.core.host_ops.shutil.disk_usage", lambda _path: _DiskUsage(91)
    )
    delivery = AsyncMock(side_effect=RuntimeError("smtp unavailable"))
    monkeypatch.setattr(system_tasks, "send_email", delivery)

    result = await system_tasks._check_disk_usage()

    assert result["level"] == "critical"
    assert result["alert_delivery"] == "dead_letter"
    assert delivery.await_count == 3
    dead_letter = json.loads((tmp_path / "disk-alert-dead-letter.jsonl").read_text())
    assert dead_letter["error"] == "smtp unavailable"


@pytest.mark.asyncio
async def test_low_free_space_escalates_to_critical(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "DISK_USAGE_ALERT_PERCENT", 80)
    monkeypatch.setattr(settings, "DISK_MIN_FREE_GB", 5)
    monkeypatch.setattr(settings, "ALERT_EMAIL_ENABLED", True)
    monkeypatch.setattr(settings, "ALERT_EMAIL_RECIPIENT", "ops@example.com")
    monkeypatch.setattr(settings, "OPS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        settings, "HOST_METRICS_PATH", str(tmp_path / "missing-host-metrics.json")
    )
    monkeypatch.setattr(
        "app.core.host_ops.shutil.disk_usage", lambda _path: _DiskUsage(85, 1)
    )
    delivery = AsyncMock()
    monkeypatch.setattr(system_tasks, "send_email", delivery)

    result = await system_tasks._check_disk_usage()

    assert result["level"] == "critical"
    assert result["free_gb"] == 1.0
    delivery.assert_awaited_once()


@pytest.mark.asyncio
async def test_disk_alert_cooldown_suppresses_same_level(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "DISK_ALERT_COOLDOWN_HOURS", 24)
    state = tmp_path / "disk-alert-state.json"
    state.write_text(json.dumps({
        "level": "alert",
        "alerted_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
    }))
    monkeypatch.setattr(settings, "OPS_STATE_DIR", str(tmp_path))

    assert await system_tasks._should_alert("alert") is False
    assert await system_tasks._should_alert("critical") is True


@pytest.mark.asyncio
async def test_disk_check_can_be_disabled(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "DISK_USAGE_ALERT_PERCENT", 0)
    monkeypatch.setattr(settings, "OPS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        "app.core.host_ops.shutil.disk_usage", lambda _path: _DiskUsage(99)
    )

    assert await system_tasks._check_disk_usage() == {
        "triggered": False,
        "disabled": True,
    }
