"""Incremental exporter for security_audit_events.

Exports new audit events as compressed JSON Lines, encrypts the artifact with
the same AES-256-CBC / PBKDF2 policy as database backups, and generates a
SHA-256 manifest.  The export watermark is only advanced after the artifact
is successfully written, verified, and (optionally) uploaded.
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import json
import logging
import os
import subprocess
import tempfile
import uuid
import zlib
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_locks import acquire_audit_export_lock
from app.core.config import settings
from app.models.platform import AuditExportWatermark, SecurityAuditEvent

logger = logging.getLogger(__name__)


def _encrypt(data: bytes) -> bytes:
    """Encrypt bytes with AES-256-CBC + PBKDF2 using BACKUP_PASSPHRASE."""
    passphrase = settings.BACKUP_PASSPHRASE
    if not passphrase:
        raise ValueError("BACKUP_PASSPHRASE is required for audit export")
    proc = subprocess.run(
        [
            "openssl", "enc", "-aes-256-cbc", "-salt", "-pbkdf2",
            "-iter", "600000", "-pass", "env:BACKUP_PASSPHRASE",
        ],
        input=data,
        capture_output=True,
        env={**os.environ, "BACKUP_PASSPHRASE": passphrase},
    )
    if proc.returncode != 0:
        raise RuntimeError(f"openssl encryption failed: {proc.stderr.decode()}")
    return proc.stdout


def _decrypt(data: bytes) -> bytes:
    """Decrypt bytes encrypted with _encrypt (for verification)."""
    passphrase = settings.BACKUP_PASSPHRASE
    proc = subprocess.run(
        [
            "openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2",
            "-iter", "600000", "-pass", "env:BACKUP_PASSPHRASE",
        ],
        input=data,
        capture_output=True,
        env={**os.environ, "BACKUP_PASSPHRASE": passphrase},
    )
    if proc.returncode != 0:
        raise RuntimeError(f"openssl decryption failed: {proc.stderr.decode()}")
    return proc.stdout


def _encrypt_file(input_path: str, output_path: str) -> None:
    passphrase = settings.BACKUP_PASSPHRASE
    if not passphrase:
        raise ValueError("BACKUP_PASSPHRASE is required for audit export")
    with open(input_path, "rb") as source, open(output_path, "wb") as target:
        proc = subprocess.run(
            [
                "openssl", "enc", "-aes-256-cbc", "-salt", "-pbkdf2",
                "-iter", "600000", "-pass", "env:BACKUP_PASSPHRASE",
            ],
            stdin=source,
            stdout=target,
            stderr=subprocess.PIPE,
            env={**os.environ, "BACKUP_PASSPHRASE": passphrase},
        )
    if proc.returncode != 0:
        raise RuntimeError(f"openssl encryption failed: {proc.stderr.decode()}")
    os.chmod(output_path, 0o600)


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as artifact:
        for chunk in iter(lambda: artifact.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_file(path: str, content_digest: str) -> None:
    """Stream-decrypt and decompress an artifact, then verify its content."""
    passphrase = settings.BACKUP_PASSPHRASE
    if not passphrase:
        raise ValueError("BACKUP_PASSPHRASE is required for audit export")
    command = [
        "openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2",
        "-iter", "600000", "-pass", "env:BACKUP_PASSPHRASE", "-in", path,
    ]
    digest = hashlib.sha256()
    decompressor = zlib.decompressobj(wbits=31)
    with tempfile.TemporaryFile() as stderr_file, subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=stderr_file,
        env={**os.environ, "BACKUP_PASSPHRASE": passphrase},
    ) as proc:
        assert proc.stdout is not None
        assert stderr_file is not None
        stdout_fh = proc.stdout
        for chunk in iter(lambda: stdout_fh.read(1024 * 1024), b""):
            digest.update(decompressor.decompress(chunk))
        digest.update(decompressor.flush())
        stderr_file.seek(0)
        stderr = stderr_file.read()
        returncode = proc.wait()

    if returncode != 0:
        raise RuntimeError(f"openssl decryption failed: {stderr.decode()}")
    if not decompressor.eof or decompressor.unused_data:
        raise RuntimeError("Audit export verification failed: invalid gzip stream")
    if digest.hexdigest() != content_digest:
        raise RuntimeError("Audit export round-trip verification failed")


def _fsync_directory(path: str) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def get_watermark(session: AsyncSession) -> AuditExportWatermark | None:
    """Return the latest watermark row, or None if no export has happened."""
    result = await session.execute(
        select(AuditExportWatermark)
        .order_by(AuditExportWatermark.id.desc())
        .limit(1)
    )
    return result.scalars().first()


def get_export_activity_at(watermark: AuditExportWatermark) -> datetime:
    """Return when the export last ran, even if it found no new events."""
    return max(watermark.last_exported_at, watermark.created_at)


async def export_audit_events(
    session: AsyncSession,
    *,
    export_dir: str | None = None,
) -> dict[str, Any]:
    """Export all audit events newer than the last watermark.

    The watermark is only advanced after the encrypted artifact passes a
    round-trip decrypt + decompress verification.
    """
    export_dir = export_dir or settings.AUDIT_EXPORT_DIR
    os.makedirs(export_dir, mode=0o700, exist_ok=True)

    if not await acquire_audit_export_lock(session):
        raise TimeoutError("audit export lock is unavailable after retries")

    wm = await get_watermark(session)
    last_exported_at = wm.last_exported_at if wm else datetime(1970, 1, 1, tzinfo=UTC)

    last_export_id = wm.last_export_id if wm else ""
    stmt = (
        select(SecurityAuditEvent)
        .where(
            tuple_(SecurityAuditEvent.created_at, SecurityAuditEvent.id)
            > (last_exported_at, last_export_id)
        )
        .order_by(SecurityAuditEvent.created_at, SecurityAuditEvent.id)
        .execution_options(yield_per=500)
    )
    result = await session.stream(stmt)
    rows = result.scalars()

    export_dir = os.path.abspath(export_dir)
    artifact_name = f"audit-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.jsonl.gz.enc"
    artifact_path = os.path.join(export_dir, artifact_name)
    artifact_tmp = f"{artifact_path}.tmp"
    plain_tmp = os.path.join(export_dir, f".{artifact_name}.plain.tmp")
    content_digest_hash = hashlib.sha256()
    event_count = 0
    start_time: datetime | None = None
    end_time: datetime | None = None
    last_event_id: str | None = None

    try:
        with open(plain_tmp, "wb") as raw_file:
            os.chmod(plain_tmp, 0o600)
            with gzip.GzipFile(fileobj=raw_file, mode="wb", mtime=0) as archive:
                async for row in rows:
                    event_count += 1
                    start_time = start_time or row.created_at
                    end_time = row.created_at
                    last_event_id = row.id
                    line = json.dumps({
                        "id": row.id,
                        "action": row.action,
                        "outcome": row.outcome,
                        "actor_id": row.actor_id,
                        "actor_tenant_id": row.actor_tenant_id,
                        "target_user_id": row.target_user_id,
                        "tenant_id": row.tenant_id,
                        "ip_address": row.ip_address,
                        "user_agent": row.user_agent,
                        "details": row.details,
                        "created_at": row.created_at.isoformat(),
                    }, ensure_ascii=False)
                    line_bytes = f"{line}\n".encode()
                    content_digest_hash.update(line_bytes)
                    archive.write(line_bytes)
            raw_file.flush()
            os.fsync(raw_file.fileno())
    except Exception:
        for temp_path in (plain_tmp, artifact_tmp):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temp_path)
        raise

    if not event_count:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(plain_tmp)
        logger.info("No new audit events to export (watermark=%s)", last_exported_at)
        session.add(
            AuditExportWatermark(
                last_exported_at=last_exported_at,
                last_export_id=last_export_id or None,
                artifact_path=None,
                manifest_digest=None,
                event_count=0,
            )
        )
        await session.commit()
        return {
            "artifact_path": None,
            "manifest_digest": None,
            "event_count": 0,
            "start_time": last_exported_at,
            "end_time": last_exported_at,
        }

    assert start_time is not None and end_time is not None
    _encrypt_file(plain_tmp, artifact_tmp)
    os.unlink(plain_tmp)
    content_digest = content_digest_hash.hexdigest()
    _verify_file(artifact_tmp, content_digest)
    artifact_digest = _sha256_file(artifact_tmp)

    manifest = {
        "artifact": artifact_name,
        "event_count": event_count,
        "start_time": start_time.isoformat(),
        "end_time": end_time.isoformat(),
        "producer": "nurse-scheduler",
        "created_at": datetime.now(UTC).isoformat(),
        "artifact_digest": f"sha256:{artifact_digest}",
        "content_digest": f"sha256:{content_digest}",
    }
    manifest_bytes = json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8")
    manifest_path = artifact_path.replace(".enc", ".manifest.json")
    manifest_tmp = f"{manifest_path}.tmp"
    try:
        with open(manifest_tmp, "wb") as manifest_file:
            manifest_file.write(manifest_bytes)
            manifest_file.flush()
            os.fsync(manifest_file.fileno())
        os.chmod(manifest_tmp, 0o600)
        os.replace(artifact_tmp, artifact_path)
        os.replace(manifest_tmp, manifest_path)
        os.chmod(artifact_path, 0o600)
        os.chmod(manifest_path, 0o600)
        _fsync_directory(export_dir)
    except Exception:
        for temp_path in (plain_tmp, artifact_tmp, manifest_tmp):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temp_path)
        for final_path in (artifact_path, manifest_path):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(final_path)
        raise

    # Advance watermark only after full success.
    try:
        new_wm = AuditExportWatermark(
            last_exported_at=end_time,
            last_export_id=last_event_id,
            artifact_path=os.path.abspath(artifact_path),
            manifest_digest=f"sha256:{artifact_digest}",
            event_count=event_count,
        )
        session.add(new_wm)
        await session.commit()
    except Exception:
        for final_path in (artifact_path, manifest_path):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(final_path)
        raise

    logger.info(
        "Exported %d audit events [%s, %s] -> %s",
        event_count, start_time, end_time, artifact_path,
    )
    return {
        "artifact_path": artifact_path,
        "manifest_digest": f"sha256:{artifact_digest}",
        "event_count": event_count,
        "start_time": start_time,
        "end_time": end_time,
    }
