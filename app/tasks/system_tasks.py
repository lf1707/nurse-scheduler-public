"""Celery tasks for host-level system health monitoring."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from sqlalchemy import CursorResult

from app.core.config import settings
from app.core.email import EmailMessageContent, send_email
from app.core.host_ops import read_host_disk_usage
from app.core.logging import get_logger
from app.tasks.celery_app import celery_task

logger = get_logger("app.tasks.system")

ALERT_LEVELS = {"none": 0, "warn": 1, "alert": 2, "critical": 3}


@celery_task(name="system.cleanup_refresh_tokens")
def cleanup_refresh_tokens() -> dict[str, Any]:
    """Delete expired and revoked refresh tokens past their retention window."""
    return asyncio.run(_cleanup_refresh_tokens())


async def _cleanup_refresh_tokens() -> dict[str, Any]:
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.database import clear_tenant_context
    from app.models.refresh_token import RefreshToken

    now = datetime.now(UTC)
    expired_before = now - timedelta(days=30)
    revoked_before = now - timedelta(days=7)
    engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    session_factory = async_sessionmaker(
        engine,
        expire_on_commit=False,
        autoflush=False,
    )
    try:
        async with session_factory() as session:
            await clear_tenant_context(session)
            result = await session.execute(
                delete(RefreshToken).where(
                    (RefreshToken.expires_at < expired_before)
                    | (RefreshToken.revoked_at.is_not(None) & (RefreshToken.revoked_at < revoked_before))
                )
            )
            await session.commit()
            deleted = cast(CursorResult[Any], result).rowcount or 0
        return {"deleted": deleted}
    finally:
        await engine.dispose()


@celery_task(name="system.check_disk_usage")
def check_disk_usage() -> dict[str, Any]:
    """Check the monitored filesystem and deliver tiered alerts."""
    return asyncio.run(_check_disk_usage())


async def _check_disk_usage() -> dict[str, Any]:
    if settings.DISK_USAGE_ALERT_PERCENT == 0:
        return {"triggered": False, "disabled": True}

    usage = read_host_disk_usage()
    percent = round(usage.used / usage.total * 100, 1)
    free_gb = round(usage.free / (1024**3), 2)
    if percent >= settings.DISK_USAGE_CRITICAL_PERCENT or free_gb <= settings.DISK_MIN_FREE_GB:
        level = "critical"
    elif percent >= settings.DISK_USAGE_ALERT_PERCENT:
        level = "alert"
    elif percent >= settings.DISK_USAGE_WARN_PERCENT:
        level = "warn"
    else:
        level = "none"

    should_alert = await _should_alert(level)
    result = {
        "triggered": level != "none",
        "level": level,
        "path": usage.path,
        "used_percent": percent,
        "threshold_percent": settings.DISK_USAGE_ALERT_PERCENT,
        "free_gb": free_gb,
        "minimum_free_gb": settings.DISK_MIN_FREE_GB,
        "alert_triggered": should_alert,
    }
    await _persist_result(result)
    if should_alert:
        result["alert_delivery"] = await _deliver_alert(result)
    else:
        result["alert_delivery"] = "skipped"
    return result


def _state_path() -> Path:
    return Path(settings.OPS_STATE_DIR) / "disk-alert-state.json"


def _result_path() -> Path:
    return Path(settings.OPS_STATE_DIR) / "disk-usage.json"


def _read_alert_state() -> dict[str, Any] | None:
    try:
        with _state_path().open(encoding="utf-8") as state_file:
            state: object = json.load(state_file)
            if isinstance(state, dict):
                return state
            return None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


async def _should_alert(level: str) -> bool:
    if level == "none":
        with contextlib.suppress(FileNotFoundError):
            _state_path().unlink()
        return False

    state = _read_alert_state()
    if not state:
        return True
    try:
        previous_level = str(state["level"])
        previous_at = datetime.fromisoformat(str(state["alerted_at"]))
    except (KeyError, ValueError):
        return True

    escalated = ALERT_LEVELS[level] > ALERT_LEVELS.get(previous_level, 0)
    cooldown = timedelta(hours=settings.DISK_ALERT_COOLDOWN_HOURS)
    return escalated or datetime.now(UTC) - previous_at >= cooldown


async def _persist_result(result: dict[str, Any]) -> None:
    directory = Path(settings.OPS_STATE_DIR)
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    checked_at = datetime.now(UTC).isoformat()
    payload = {**result, "checked_at": checked_at}

    if result["level"] != "none":
        alert_state = {"level": result["level"], "alerted_at": checked_at}
        temporary = _state_path().with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as state_file:
            json.dump(alert_state, state_file)
            state_file.flush()
            os.fsync(state_file.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, _state_path())
    else:
        with contextlib.suppress(FileNotFoundError):
            _state_path().unlink()

    temporary = _result_path().with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as result_file:
        json.dump(payload, result_file)
        result_file.flush()
        os.fsync(result_file.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, _result_path())


async def _write_dead_letter(result: dict[str, Any], error: str) -> None:
    directory = Path(settings.OPS_STATE_DIR)
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    path = directory / "disk-alert-dead-letter.jsonl"
    record = {
        "result": result,
        "error": error,
        "failed_at": datetime.now(UTC).isoformat(),
    }
    with path.open("a", encoding="utf-8") as dead_letter:
        dead_letter.write(json.dumps(record, ensure_ascii=False) + "\n")
        dead_letter.flush()
        os.fsync(dead_letter.fileno())
    os.chmod(path, 0o600)


async def _deliver_alert(result: dict[str, Any]) -> str:
    message = (
        f"Disk usage is {result['used_percent']}% at {result['path']} "
        f"(threshold {result['threshold_percent']}%, free {result['free_gb']} GiB, "
        f"level {result['level']})."
    )
    if not settings.ALERT_EMAIL_ENABLED or not settings.ALERT_EMAIL_RECIPIENT:
        logger.error("disk_usage_alert.no_recipient detail=%s", message)
        return "logged"

    content = EmailMessageContent(
        to_email=settings.ALERT_EMAIL_RECIPIENT,
        subject=f"[{result['level'].upper()}] System health: disk usage watermark",
        body=message,
    )
    last_error = ""
    for attempt in range(1, settings.ALERT_EMAIL_MAX_ATTEMPTS + 1):
        try:
            await send_email(content)
            return "email"
        except Exception as exc:  # noqa: BLE001 - alert failures need dead-lettering
            last_error = str(exc)
            logger.warning(
                "disk_usage_alert.delivery_attempt_failed attempt=%s error=%s",
                attempt,
                exc,
            )
            if attempt < settings.ALERT_EMAIL_MAX_ATTEMPTS:
                await asyncio.sleep(settings.ALERT_EMAIL_RETRY_SECONDS)

    await _write_dead_letter(result, last_error)
    logger.error("disk_usage_alert.delivery_failed error=%s", last_error)
    return "dead_letter"
