"""Health readiness behavior when infrastructure dependencies fail."""

from __future__ import annotations

from pathlib import Path

import redis.asyncio
from fastapi.testclient import TestClient
from pytest import MonkeyPatch
from sqlalchemy.sql.elements import ClauseElement

import app.core.database
from app.core.config import settings
from app.main import create_app


class FailingSession:
    async def __aenter__(self) -> FailingSession:
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False

    async def execute(self, _query: ClauseElement) -> None:
        raise RuntimeError("database unavailable")


class FailingRedis:
    async def ping(self) -> None:
        raise RuntimeError("redis unavailable")

    async def aclose(self) -> None:
        return None


def test_readiness_returns_503_when_dependencies_fail(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    def failing_from_url(*_args: object, **_kwargs: object) -> FailingRedis:
        return FailingRedis()

    monkeypatch.setattr(app.core.database, "async_session_factory", FailingSession)
    monkeypatch.setattr(redis.asyncio, "from_url", failing_from_url)
    monkeypatch.setattr(settings, "OPS_STATE_DIR", str(tmp_path))

    client = TestClient(create_app())
    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "degraded",
        "checks": {
            "db": "error",
            "redis": "error",
            "disk": "unknown",
            "disk_free_gb": None,
        },
    }


def test_liveness_does_not_check_dependencies(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    def failing_from_url(*_args: object, **_kwargs: object) -> FailingRedis:
        return FailingRedis()

    monkeypatch.setattr(app.core.database, "async_session_factory", FailingSession)
    monkeypatch.setattr(redis.asyncio, "from_url", failing_from_url)
    monkeypatch.setattr(settings, "OPS_STATE_DIR", str(tmp_path))

    client = TestClient(create_app())
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
