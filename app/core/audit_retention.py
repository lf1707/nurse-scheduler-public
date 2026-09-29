"""Retention policy for audit-event partitions.

Determines which monthly partitions are eligible for expiry (detach + drop)
based on the configured hot retention period.  Supports legal holds that
prevent a partition from being expired even after its retention window.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_export import get_watermark
from app.core.audit_locks import acquire_audit_export_lock
from app.core.audit_partitions import expire_partition, list_monthly_partitions
from app.core.config import settings

logger = logging.getLogger(__name__)

LEGAL_HOLD_TABLE = "audit_legal_holds"
UPLOAD_MARKER_SUFFIX = ".uploaded"


async def has_legal_hold(
    session: AsyncSession, partition_name: str
) -> bool:
    """Return True if a legal hold exists for the given partition."""
    result = await session.execute(
        text(
            f"SELECT 1 FROM {LEGAL_HOLD_TABLE} "
            f"WHERE partition_name = :name LIMIT 1"
        ),
        {"name": partition_name},
    )
    return result.scalar() == 1


def archive_copy_is_verified(watermark: object | None) -> bool:
    """Verify the local watermark artifact and its completed-upload marker.

    The upload marker is written only after the external upload process has
    confirmed that the artifact and manifest were copied to another failure
    domain. Retention therefore never trusts a database watermark alone.
    """
    if watermark is None:
        return False
    artifact_path = getattr(watermark, "artifact_path", None)
    manifest_digest = getattr(watermark, "manifest_digest", None)
    if not artifact_path or not manifest_digest:
        return False

    manifest_path = str(artifact_path).replace(".enc", ".manifest.json")
    marker_path = f"{manifest_path}{UPLOAD_MARKER_SUFFIX}"
    if not os.path.isfile(manifest_path) or not os.path.isfile(marker_path):
        return False
    if not os.path.isfile(artifact_path):
        return False

    try:
        with open(manifest_path, encoding="utf-8") as manifest_file:
            manifest = json.load(manifest_file)
        digest = hashlib.sha256()
        with open(artifact_path, "rb") as artifact:
            for chunk in iter(lambda: artifact.read(1024 * 1024), b""):
                digest.update(chunk)
    except (OSError, ValueError, json.JSONDecodeError):
        return False

    return cast(
        bool,
        (
            manifest.get("artifact") == os.path.basename(artifact_path)
            and manifest.get("artifact_digest") == manifest_digest
            and manifest.get("artifact_digest") == f"sha256:{digest.hexdigest()}"
        ),
    )


async def partition_has_unexported_rows(
    session: AsyncSession,
    year: int,
    month: int,
    watermark: object | None,
) -> bool:
    """Check the candidate partition against the export keyset watermark.

    The watermark timestamp alone can hide events if the clock moves backwards
    after an export. Rechecking the ``(created_at, id)`` keyset in PostgreSQL
    prevents those later-inserted rows from being detached and dropped.
    """
    watermark_at = getattr(watermark, "last_exported_at", None) or datetime(
        1970, 1, 1, tzinfo=UTC
    )
    watermark_id = getattr(watermark, "last_export_id", None) or ""
    part_end = datetime(year, month, 1, tzinfo=UTC) + timedelta(days=32)
    part_end = part_end.replace(day=1)
    result = await session.execute(
        text(
            "SELECT 1 FROM security_audit_events "
            "WHERE created_at >= :start AND created_at < :end "
            "AND (created_at, id) > (:watermark_at, :watermark_id) LIMIT 1"
        ),
        {
            "start": datetime(year, month, 1, tzinfo=UTC),
            "end": part_end,
            "watermark_at": watermark_at,
            "watermark_id": watermark_id,
        },
    )
    return result.scalar() == 1


async def expire_eligible_partitions(
    session: AsyncSession,
) -> list[str]:
    """Expire (detach + drop) partitions older than the hot retention window.

    A partition is only expired if:
    - Its month is older than AUDIT_RETENTION_DAYS_HOT
    - It has been archived (watermark covers its entire date range)
    - No legal hold exists

    Returns the list of partition names that were expired.
    """
    if not await acquire_audit_export_lock(session, retries=0):
        logger.warning("Skipping audit retention: export lock is unavailable")
        return []

    cutoff = datetime.now(UTC) - timedelta(days=settings.AUDIT_RETENTION_DAYS_HOT)

    all_partitions = await list_monthly_partitions(session)

    expired: list[str] = []
    for part_name in all_partitions:
        # Parse YYYY_MM from partition name.
        suffix = part_name.replace("security_audit_events_", "")
        try:
            year, month = map(int, suffix.split("_"))
        except ValueError:
            continue

        part_end = datetime(year, month, 1, tzinfo=UTC) + timedelta(days=32)
        part_end = part_end.replace(day=1)
        if part_end >= cutoff:
            continue  # Still within hot retention.

        if await has_legal_hold(session, part_name):
            logger.info("Skipping partition %s due to legal hold", part_name)
            continue

        # Only expire a partition that has been fully archived. The export
        # watermark is advanced to the last exported event's timestamp, so a
        # partition is safe to drop once the watermark is at or beyond the end
        # of its date range. This guards against dropping data that has not yet
        # been copied off-host.
        wm = await get_watermark(session)
        wm_end = wm.last_exported_at if wm else datetime(1970, 1, 1, tzinfo=UTC)
        if wm_end < part_end:
            logger.info(
                "Skipping partition %s: not yet archived (watermark=%s < %s)",
                part_name, wm_end, part_end,
            )
            continue

        if not archive_copy_is_verified(wm):
            logger.warning(
                "Skipping partition %s: watermark artifact is missing or not confirmed off-host",
                part_name,
            )
            continue

        if await partition_has_unexported_rows(session, year, month, wm):
            logger.warning(
                "Skipping partition %s: keyset check found rows after the export watermark",
                part_name,
            )
            continue

        if await expire_partition(session, year, month):
            expired.append(part_name)

    return expired
