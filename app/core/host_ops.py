"""File-based bridge to the narrowly scoped host operations helper.

The application never receives Docker credentials or a Docker socket.  In
production a root-owned host service consumes approved request files from a
small bind-mounted directory and emits host metrics back to the same directory.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.config import settings

HOST_METRICS_FILE = "host-metrics.json"
BEAT_RESTART_REQUEST_FILE = "beat-restart.request.json"
BEAT_RESTART_RESULT_FILE = "beat-restart.result.json"


@dataclass(frozen=True)
class HostDiskUsage:
    path: str
    total: int
    used: int
    free: int
    checked_at: datetime


def read_host_disk_usage() -> HostDiskUsage:
    """Read host metrics when available, otherwise this container's filesystem."""
    metrics_path = (
        Path(settings.HOST_METRICS_PATH)
        if settings.HOST_METRICS_PATH
        else Path(settings.OPS_CONTROL_DIR) / HOST_METRICS_FILE
    )
    try:
        with metrics_path.open(encoding="utf-8") as metrics_file:
            payload = json.load(metrics_file)
        total = int(payload["total_bytes"])
        used = int(payload["used_bytes"])
        free = int(payload["free_bytes"])
        checked_at = datetime.fromisoformat(str(payload["checked_at"]))
        if total <= 0 or used < 0 or free < 0 or checked_at.tzinfo is None:
            raise ValueError("invalid host metrics")
        return HostDiskUsage(
            path=str(payload.get("path", settings.DISK_USAGE_PATH)),
            total=total,
            used=used,
            free=free,
            checked_at=checked_at,
        )
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        usage = shutil.disk_usage(settings.DISK_USAGE_PATH)
        return HostDiskUsage(
            path=settings.DISK_USAGE_PATH,
            total=usage.total,
            used=usage.used,
            free=usage.free,
            checked_at=datetime.now(UTC),
        )


async def queue_beat_restart(actor_id: str) -> str:
    """Atomically publish one host-owned restart request and return its id."""
    directory = Path(settings.OPS_CONTROL_DIR)
    directory.mkdir(parents=True, mode=0o750, exist_ok=True)
    request_id = uuid.uuid4().hex
    request_path = directory / BEAT_RESTART_REQUEST_FILE
    temporary = request_path.with_suffix(".tmp")
    payload = {
        "request_id": request_id,
        "action": "restart_beat",
        "actor_id": actor_id,
        "requested_at": datetime.now(UTC).isoformat(),
    }
    with temporary.open("w", encoding="utf-8") as request_file:
        json.dump(payload, request_file, separators=(",", ":"))
        request_file.write("\n")
        request_file.flush()
        os.fsync(request_file.fileno())
    os.chmod(temporary, 0o640)
    os.replace(temporary, request_path)
    return request_id


def read_beat_restart_result() -> dict[str, Any] | None:
    result_path = Path(settings.OPS_CONTROL_DIR) / BEAT_RESTART_RESULT_FILE
    try:
        with result_path.open(encoding="utf-8") as result_file:
            payload = json.load(result_file)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None
