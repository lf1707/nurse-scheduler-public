"""Celery application — broker + result backend on Redis.

Worker runs schedule generation tasks (CPU-bound CP-SAT solve in a thread).
Beat runs periodic cleanup tasks (future).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar, cast

from celery import Celery
from celery.signals import worker_ready

from app.core.config import settings

celery_app = Celery(
    "nurse_scheduler",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=[
        "app.tasks.schedule_tasks",
        "app.tasks.tenant_tasks",
        "app.tasks.audit_tasks",
        "app.tasks.system_tasks",
    ],
)

_TaskCallable = TypeVar("_TaskCallable", bound=Callable[..., object])


def celery_task(name: str) -> Callable[[_TaskCallable], _TaskCallable]:
    """Preserve task function types across Celery's untyped decorator."""
    return cast(
        Callable[[_TaskCallable], _TaskCallable],
        celery_app.task(name=name),
    )


_SignalCallable = TypeVar("_SignalCallable", bound=Callable[..., object])


def worker_ready_connect(
    receiver: _SignalCallable,
) -> _SignalCallable:
    """Preserve receiver types across Celery's untyped signal registry."""
    return cast(
        _SignalCallable,
        worker_ready.connect(receiver),
    )

def _anomaly_beat_entry() -> dict[str, Any]:
    schedule_seconds = _read_anomaly_schedule()
    if schedule_seconds == 0:
        return {}
    return {
        "audit-run-anomaly-scan": {
            "task": "audit.run_anomaly_scan",
            "schedule": float(schedule_seconds),
        },
    }


def _read_anomaly_schedule() -> int:
    """Read anomaly scan schedule from PlatformSetting, fall back to env."""
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    from app.models.platform import PlatformSetting

    sync_url = settings.DATABASE_URL.replace("+asyncpg", "+psycopg2")
    engine = create_engine(sync_url)
    try:
        with Session(engine) as session:
            result = session.execute(
                select(PlatformSetting).where(
                    PlatformSetting.key == "anomaly_scan_schedule_seconds"
                )
            )
            row = result.scalar_one_or_none()
            setting_value: object = (
                row.value if row else settings.ANOMALY_SCAN_SCHEDULE_SECONDS
            )
            return int(str(setting_value))
    except Exception:
        return settings.ANOMALY_SCAN_SCHEDULE_SECONDS
    finally:
        engine.dispose()


celery_app.conf.update(
    # Serialization
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    # Timezone
    timezone="UTC",
    enable_utc=True,
    beat_schedule_filename=settings.CELERY_BEAT_SCHEDULE_FILENAME,
    # Reliability
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,  # one task at a time per process (CP-SAT is heavy)
    # Result expiry (keep results 1h for polling)
    result_expires=3600,
    # Task routing (default queue)
    task_default_queue="scheduling",
    task_default_routing_key="scheduling",
    task_routes={
        "tenant_application.*": {"queue": "ops"},
        "subscription.*": {"queue": "ops"},
        "audit.*": {"queue": "ops"},
        "system.*": {"queue": "ops"},
    },
    beat_schedule={
        "expire-unverified-tenant-applications": {
            "task": "tenant_application.expire_unverified",
            "schedule": 86_400.0,
        },
        # Subscription expiry: downgrade lapsed paid/trial subscriptions hourly.
        "subscription-expire-trials": {
            "task": "subscription.expire_trials",
            "schedule": 3600.0,
        },
        # Audit partition management: create future partitions weekly.
        "audit-create-partitions": {
            "task": "audit.create_partitions",
            "schedule": 604_800.0,  # 7 days
        },
        # Audit export: daily incremental encrypted export.
        "audit-export-events": {
            "task": "audit.export_events",
            "schedule": 86_400.0,  # 24 hours
        },
        # Audit retention: expire old archived partitions monthly.
        "audit-expire-old-partitions": {
            "task": "audit.expire_old_partitions",
            "schedule": 2_592_000.0,  # 30 days
        },
        # Anomaly scan: burst detection across recent audit events.
        **_anomaly_beat_entry(),
        # System health: check the host filesystem usage hourly.
        "system-check-disk-usage": {
            "task": "system.check_disk_usage",
            "schedule": 3600.0,
        },
        # Failed worker processes can leave a solve stuck in RUNNING.
        "schedule-fail-stale-requests": {
            "task": "schedule.fail_stale_requests",
            "schedule": 3600.0,
        },
        # Refresh tokens: drop expired (30d) and revoked (7d) records daily.
        "system-cleanup-refresh-tokens": {
            "task": "system.cleanup_refresh_tokens",
            "schedule": 86_400.0,
        },
    },
)


@worker_ready_connect
def on_worker_ready(sender: Any, **_kwargs: object) -> None:
    """Log when a worker comes up — helps confirm config in dev."""
    print(f"[celery] worker ready: {sender.hostname}", flush=True)
