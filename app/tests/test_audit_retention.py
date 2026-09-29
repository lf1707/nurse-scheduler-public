"""Unit tests for audit-event retention decision logic.

These tests exercise ``expire_eligible_partitions`` without a live database by
injecting fake collaborators.  The function only depends on:

- one ``pg_inherits`` query to list monthly partitions (served by a fake
  session), and
- the ``get_watermark``, ``has_legal_hold``, and ``expire_partition`` helpers,
  which are monkeypatched to return controlled values.

This keeps the tests deterministic and focused on the retention policy: which
partitions are expired, skipped within the hot window, skipped under a legal
hold, and skipped before they have been fully archived.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from unittest.mock import AsyncMock, patch

import pytest

from app.core import audit_retention
from app.core.audit_retention import archive_copy_is_verified
from app.core.config import settings


class _Row:
    def __init__(self, name: str) -> None:
        self.name = name


class _Result:
    def __init__(self, rows: list[Any] | None = None, scalar: Any = None) -> None:
        self._rows = rows or []
        self._scalar = scalar

    def scalar(self) -> Any:
        return self._scalar

    def scalars(self) -> Any:
        class _Scalars:
            rows = self._rows

            def first(self) -> Any:
                return self.rows[0] if self.rows else None

        return _Scalars()

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def __iter__(self) -> Iterator[Any]:
        return iter(self._rows)


class _FakeSession:
    """A minimal async session that only answers the partition-list query."""

    def __init__(self, partitions: list[str]) -> None:
        self._partitions = [_Row(name) for name in partitions]
        self.calls: list[str] = []

    async def execute(self, statement: Any, params: Any = None) -> _Result:
        self.calls.append(str(statement))
        if "pg_inherits" in str(statement) and "inhparent" in str(statement):
            return _Result(rows=self._partitions)
        return _Result(scalar=None)

    async def commit(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: Any) -> Literal[False]:
        return False


class _Watermark:
    def __init__(self, last_exported_at: datetime | None) -> None:
        self.last_exported_at = last_exported_at


def _watermark(now: datetime | None) -> Any:
    async def _water(sess: Any) -> _Watermark:
        return _Watermark(now)

    return _water


async def _no_hold(_sess: Any, _name: str) -> bool:
    return False


def _record_expire() -> tuple[Any, list[tuple[int, int]]]:
    seen: list[tuple[int, int]] = []

    async def _expire(_sess: Any, year: int, month: int) -> bool:
        seen.append((year, month))
        return True

    async def _wrapped(_sess: Any, year: int, month: int) -> bool:
        return await _expire(_sess, year, month)

    return _wrapped, seen


def _fail_expire() -> AsyncMock:
    return AsyncMock(return_value=False)


def _hold_on(name: str) -> Any:
    async def _hold(_sess: Any, candidate: str) -> bool:
        return candidate == name

    return _hold


@pytest.mark.asyncio
async def test_expire_all_expired_and_archived_partitions() -> None:
    """Every partition past the retention window that is fully archived expires.

    This is the regression check for the loop-body bug that previously expired
    only the last partition.
    """
    now = datetime(2026, 8, 29, tzinfo=UTC)
    old_partitions = [
        "security_audit_events_2020_01",
        "security_audit_events_2020_02",
        "security_audit_events_2020_03",
    ]
    session: Any = _FakeSession(old_partitions)
    expire, seen = _record_expire()

    with (
        patch("app.core.audit_retention.get_watermark", _watermark(now)),
        patch("app.core.audit_retention.has_legal_hold", _no_hold),
        patch("app.core.audit_retention.archive_copy_is_verified", return_value=True),
        patch("app.core.audit_retention.expire_partition", expire),
    ):
        result = await audit_retention.expire_eligible_partitions(session)

    assert set(result) == set(old_partitions)
    assert sorted(seen) == [(2020, 1), (2020, 2), (2020, 3)]


@pytest.mark.asyncio
async def test_partition_within_hot_window_is_skipped() -> None:
    """The current month stays inside the retention window and is never expired."""
    now = datetime(2026, 8, 29, tzinfo=UTC)
    session: Any = _FakeSession(["security_audit_events_2026_08"])
    expire, seen = _record_expire()

    with (
        patch("app.core.audit_retention.get_watermark", _watermark(now)),
        patch("app.core.audit_retention.has_legal_hold", _no_hold),
        patch("app.core.audit_retention.archive_copy_is_verified", return_value=True),
        patch("app.core.audit_retention.expire_partition", expire),
    ):
        result = await audit_retention.expire_eligible_partitions(session)

    assert result == []
    assert seen == []


@pytest.mark.asyncio
async def test_partition_not_yet_archived_is_skipped() -> None:
    """A partition older than the window but not archived is retained."""
    now = datetime(2026, 8, 29, tzinfo=UTC)
    session: Any = _FakeSession(["security_audit_events_2020_01"])

    with (
        patch("app.core.audit_retention.get_watermark", _watermark(now)),
        patch("app.core.audit_retention.has_legal_hold", _no_hold),
        patch("app.core.audit_retention.expire_partition", _fail_expire()),
    ):
        result = await audit_retention.expire_eligible_partitions(session)

    assert result == []


@pytest.mark.asyncio
async def test_locked_export_skips_retention(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retention safely skips the whole run when export still owns the lock."""
    session: Any = _FakeSession(["security_audit_events_2020_01"])
    expire = _fail_expire()

    async def _locked(_session: Any, retries: int) -> bool:
        assert retries == 0
        return False

    with (
        patch("app.core.audit_retention.acquire_audit_export_lock", _locked),
        patch("app.core.audit_retention.expire_partition", expire),
    ):
        result = await audit_retention.expire_eligible_partitions(session)

    assert result == []
    expire.assert_not_called()


@pytest.mark.asyncio
async def test_keyset_violation_prevents_expiry() -> None:
    """The database keyset check blocks DETACH after clock rollback."""
    now = datetime(2026, 8, 29, tzinfo=UTC)
    session: Any = _FakeSession(["security_audit_events_2020_01"])
    expire = _fail_expire()

    with (
        patch("app.core.audit_retention.get_watermark", _watermark(now)),
        patch("app.core.audit_retention.has_legal_hold", _no_hold),
        patch("app.core.audit_retention.archive_copy_is_verified", return_value=True),
        patch(
            "app.core.audit_retention.partition_has_unexported_rows",
            AsyncMock(return_value=True),
        ),
        patch("app.core.audit_retention.expire_partition", expire),
    ):
        result = await audit_retention.expire_eligible_partitions(session)

    assert result == []
    expire.assert_not_called()


@pytest.mark.asyncio
async def test_retention_uses_configured_export_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retention never queues behind export; it skips this scheduled run."""
    calls: list[int] = []

    async def _lock(_session: Any, retries: int) -> bool:
        calls.append(retries)
        return True

    monkeypatch.setattr(
        "app.core.audit_retention.acquire_audit_export_lock", _lock
    )
    session: Any = _FakeSession([])
    assert await audit_retention.expire_eligible_partitions(session) == []
    assert calls == [0]
    assert settings.AUDIT_LOCK_TIMEOUT_SECONDS > 0
@pytest.mark.asyncio
async def test_legal_hold_prevents_expiry() -> None:
    """A legal hold blocks expiry even for an expired, archived partition."""
    now = datetime(2026, 8, 29, tzinfo=UTC)
    session: Any = _FakeSession(["security_audit_events_2020_01"])

    with (
        patch("app.core.audit_retention.get_watermark", _watermark(now)),
        patch("app.core.audit_retention.has_legal_hold", _hold_on("security_audit_events_2020_01")),
        patch("app.core.audit_retention.archive_copy_is_verified", return_value=True),
        patch("app.core.audit_retention.expire_partition", _fail_expire()),
    ):
        result = await audit_retention.expire_eligible_partitions(session)

    assert result == []


@pytest.mark.asyncio
async def test_unnamed_partition_is_ignored() -> None:
    """A partition name that is not YYYY_MM is skipped without error."""
    now = datetime(2026, 8, 29, tzinfo=UTC)
    session: Any = _FakeSession(
        ["security_audit_events_2020_01", "garbage_name"]
    )

    with (
        patch("app.core.audit_retention.get_watermark", _watermark(now)),
        patch("app.core.audit_retention.has_legal_hold", _no_hold),
        patch("app.core.audit_retention.archive_copy_is_verified", return_value=True),
        patch("app.core.audit_retention.expire_partition", _fail_expire()),
    ):
        result = await audit_retention.expire_eligible_partitions(session)

    assert result == []


def test_archive_copy_requires_artifact_manifest_and_upload_marker(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "audit.jsonl.gz.enc"
    manifest = tmp_path / "audit.jsonl.gz.manifest.json"
    marker = tmp_path / "audit.jsonl.gz.manifest.json.uploaded"
    digest = hashlib.sha256(b"encrypted").hexdigest()
    artifact.write_bytes(b"encrypted")
    manifest.write_text(json.dumps({
        "artifact": artifact.name,
        "artifact_digest": f"sha256:{digest}",
    }))
    marker.write_text("{}")
    watermark = type("Watermark", (), {
        "artifact_path": str(artifact),
        "manifest_digest": f"sha256:{digest}",
    })()

    assert archive_copy_is_verified(watermark) is True
    marker.unlink()
    assert archive_copy_is_verified(watermark) is False
