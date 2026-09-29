"""Offline tests for streaming audit-export verification."""

from __future__ import annotations

import gzip
import hashlib
from pathlib import Path

import pytest
from pytest import MonkeyPatch

from app.core import audit_export
from app.core.config import settings


def test_verify_file_accepts_matching_round_trip(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "BACKUP_PASSPHRASE", "test-passphrase")
    content = b'{"id":"event-1"}\n{"id":"event-2"}\n'
    plain = tmp_path / "events.jsonl.gz"
    artifact = tmp_path / "events.jsonl.gz.enc"
    with gzip.open(plain, "wb") as archive:
        archive.write(content)
    audit_export._encrypt_file(str(plain), str(artifact))

    audit_export._verify_file(str(artifact), hashlib.sha256(content).hexdigest())


def test_verify_file_rejects_content_mismatch(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "BACKUP_PASSPHRASE", "test-passphrase")
    plain = tmp_path / "events.jsonl.gz"
    artifact = tmp_path / "events.jsonl.gz.enc"
    with gzip.open(plain, "wb") as archive:
        archive.write(b'{"id":"event-1"}\n')
    audit_export._encrypt_file(str(plain), str(artifact))

    with pytest.raises(RuntimeError, match="round-trip verification failed"):
        audit_export._verify_file(str(artifact), hashlib.sha256(b"other").hexdigest())
