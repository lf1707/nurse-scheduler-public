"""Audit anomaly detection and alerting.

Detects anomalous bursts in recent security-audit events and raises alerts.
This is part of the security-hardening roadmap: the audit partitioning,
retention, and export pipeline already existed, but no rule-based anomaly
detection or alert delivery was wired in.

Detection model
---------------
Each scan window buckets recent audit events by an identity and counts them. A
rule fires when a bucket\'s count crosses its configured threshold. Rules are
keyed by:

- ``by_ip``: events per source IP (brute-force / credential-stuffing signal).
- ``by_actor_id``: actions per actor identity (impersonation / super-admin
  abuse signal).
- ``by_action``: total events per action type (global rate signal).

All rules run over a rolling window (``ANOMALY_ALERT_WINDOW_SECONDS``) and only
alert once per ``(rule, bucket)`` to avoid alert spam for a sustained event: a
``audit_alert_fired`` table records the last fired bucket so a burst within one
window produces a single alert.

Alerting
--------
Alerts are delivered by email when ``ALERT_EMAIL_ENABLED`` is set and
``ALERT_EMAIL_RECIPIENT`` is configured, otherwise they are logged at ERROR
level. Email delivery failures are logged, not raised, so a broken SMTP setup
cannot break the audit pipeline.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import Column, DateTime, Integer, String, Table, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import Base
from app.core.email import EmailMessageContent, send_email
from app.core.logging import get_logger
from app.models.platform import SecurityAuditEvent

logger = get_logger("app.core.audit_alerts")

# The control table that records the last fired alert per (rule, bucket).
# Own table keeps concerns separate from the watermark and retention tables.
ALERT_FIRED_TABLE = Table(
    "audit_alert_fired",
    Base.metadata,
    Column("id", Integer, primary_key=True),
    Column("rule_name", String(64), nullable=False),
    Column("bucket_key", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    extend_existing=True,
)


@dataclass(frozen=True, slots=True)
class Rule:
    """A single anomaly-detection rule."""

    name: str
    severity: str
    threshold: int
    key_fn: Callable[[Any], str | None]
    actions: set[str] | None = None
    outcomes: set[str] | None = None
    predicate: Callable[[Any], bool] | None = None


@dataclass(frozen=True, slots=True)
class AuditEventAggregate:
    """A SQL-counted audit event shape for rule detection."""

    action: str
    outcome: str
    ip_address: str | None = None
    actor_id: str | None = None
    actor_tenant_id: str | None = None
    count: int = 1


def build_rules() -> list[Rule]:
    """Assemble the default rule set from settings.

    A threshold of 0 disables the rule.
    """
    rules: list[Rule] = []

    if settings.ALERT_LOGIN_FAILURE_RATE > 0:
        rules.append(
            Rule(
                name="login_failure_rate",
                severity="critical",
                threshold=settings.ALERT_LOGIN_FAILURE_RATE,
                key_fn=lambda e: e.ip_address,
                predicate=_is_login_failure_event,
            )
        )

    if settings.ALERT_IMPERSONATION_RATE > 0:
        rules.append(
            Rule(
                name="impersonation_rate",
                severity="critical",
                threshold=settings.ALERT_IMPERSONATION_RATE,
                key_fn=lambda e: e.actor_id or "system",
                actions={"auth.impersonate"},
            )
        )

    if settings.ALERT_SUPER_ADMIN_ACTIONS > 0:
        rules.append(
            Rule(
                name="super_admin_action_rate",
                severity="warning",
                threshold=settings.ALERT_SUPER_ADMIN_ACTIONS,
                key_fn=lambda e: e.actor_id or "system",
                predicate=_is_super_admin_actor,
            )
        )

    if settings.ALERT_SIGNUP_RATE > 0:
        rules.append(
            Rule(
                name="signup_rate",
                severity="warning",
                threshold=settings.ALERT_SIGNUP_RATE,
                key_fn=lambda e: e.ip_address,
            )
        )

    if settings.ALERT_EXPORT_RATE > 0:
        rules.append(
            Rule(
                name="export_volume",
                severity="info",
                threshold=settings.ALERT_EXPORT_RATE,
                key_fn=lambda e: e.action,
                actions={
                    "schedule.export",
                    "audit.events.exported",
                    "audit.export.triggered",
                },
            )
        )

    return rules


@dataclass
class Alert:
    """A single anomaly alert produced by a rule."""

    rule: str
    severity: str
    key: str | None
    count: int
    threshold: int
    window_start: datetime


def _bucket_window(now: datetime) -> int:
    """Return an integer bucket key for deduplication (one per window)."""
    return int(now.timestamp()) // settings.ANOMALY_ALERT_WINDOW_SECONDS


def _is_super_admin_actor(event: Any) -> bool:
    """Whether an authenticated event actor has no tenant scope."""
    return event.actor_id is not None and event.actor_tenant_id is None


def _is_login_failure_event(event: Any) -> bool:
    """Whether an event represents a failed or rate-limited login attempt."""
    return (event.action, event.outcome) in {
        ("auth.login", "failure"),
        ("auth.login.rate_limited", "denied"),
    }


def detect_anomalies(
    events: list[Any],
    rules: list[Rule],
    now: datetime,
    *,
    fired: set[tuple[str, int]],
) -> list[Alert]:
    """Pure detection: count events per bucket and raise threshold breaches.

    ``fired`` is a mutable set of ``(rule_name, bucket_key)`` already alerted
    for the current window. It is updated in place so a burst within one window
    produces a single alert.
    """
    if not rules:
        return []

    bucket_key = _bucket_window(now)
    alerts: list[Alert] = []
    for rule in rules:
        if rule.threshold <= 0:
            continue

        counts: dict[str | None, int] = {}
        for event in events:
            if rule.actions is not None and event.action not in rule.actions:
                continue
            if rule.outcomes is not None and event.outcome not in rule.outcomes:
                continue
            if rule.predicate is not None and not rule.predicate(event):
                continue
            key = rule.key_fn(event)
            counts[key] = counts.get(key, 0) + getattr(event, "count", 1)

        for key, count in counts.items():
            if count < rule.threshold:
                continue
            if (rule.name, bucket_key) in fired:
                continue

            alerts.append(
                Alert(
                    rule=rule.name,
                    severity=rule.severity,
                    key=key,
                    count=count,
                    threshold=rule.threshold,
                    window_start=now - timedelta(seconds=settings.ANOMALY_ALERT_WINDOW_SECONDS),
                )
            )
            fired.add((rule.name, bucket_key))

    return alerts


async def _ensure_fired_table(session: AsyncSession) -> None:
    """Create the fired-alert control table if it does not exist."""
    await session.execute(
        text(
            "CREATE TABLE IF NOT EXISTS audit_alert_fired ("
            "  id SERIAL PRIMARY KEY,"
            "  rule_name VARCHAR(64) NOT NULL,"
            "  bucket_key VARCHAR(128) NOT NULL,"
            "  created_at TIMESTAMPTZ NOT NULL DEFAULT now()"
            ")"
        )
    )
    await session.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_audit_alert_fired_rule_key "
            "ON audit_alert_fired (rule_name, bucket_key)"
        )
    )


async def _load_fired(session: AsyncSession) -> set[tuple[str, int]]:
    """Return the set of (rule_name, bucket_key) already fired in this window."""
    bucket_key = _bucket_window(datetime.now(UTC))
    result = await session.execute(
        select(ALERT_FIRED_TABLE.c.rule_name)
        .where(ALERT_FIRED_TABLE.c.bucket_key == str(bucket_key))
    )
    return {(row[0], bucket_key) for row in result}


async def _record_fired(
    session: AsyncSession, rule_name: str, bucket_key: int
) -> None:
    """Persist that a rule fired for the current bucket."""
    await session.execute(
        text(
            "INSERT INTO audit_alert_fired (rule_name, bucket_key) "
            "VALUES (:rule, :key)"
        ),
        {"rule": rule_name, "key": str(bucket_key)},
    )


async def scan_anomalies(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    rules: list[Rule] | None = None,
) -> list[Alert]:
    """Scan recent audit events and return any anomalies that just fired.

    Only alerts that have not fired for the current window are returned; a burst
    within one window produces a single alert.
    """
    if rules is None:
        rules = build_rules()

    now = now or datetime.now(UTC)
    async with session.begin_nested():
        await _ensure_fired_table(session)
        fired = await _load_fired(session)
        aggregates = (
            await session.execute(
                select(
                    SecurityAuditEvent.action,
                    SecurityAuditEvent.outcome,
                    SecurityAuditEvent.ip_address,
                    SecurityAuditEvent.actor_id,
                    SecurityAuditEvent.actor_tenant_id,
                    func.count().label("count"),
                ).where(
                    SecurityAuditEvent.created_at
                    >= now - timedelta(seconds=settings.ANOMALY_ALERT_WINDOW_SECONDS)
                ).group_by(
                    SecurityAuditEvent.action,
                    SecurityAuditEvent.outcome,
                    SecurityAuditEvent.ip_address,
                    SecurityAuditEvent.actor_id,
                    SecurityAuditEvent.actor_tenant_id,
                )
            )
        ).all()
        events: list[AuditEventAggregate] = []
        for aggregate in aggregates:
            event = AuditEventAggregate(
                action=aggregate.action,
                outcome=aggregate.outcome,
                ip_address=aggregate.ip_address,
                actor_id=aggregate.actor_id,
                actor_tenant_id=aggregate.actor_tenant_id,
                count=cast(int, aggregate.count),
            )
            events.append(event)

        alerts = detect_anomalies(events, rules, now, fired=fired)

    # Persist fired-bucket records after detection so dedup survives across
    # scans; without a commit the records roll back and every scan re-alerts.
    if alerts:
        async with session.begin_nested():
            bucket_key = _bucket_window(now)
            for alert in alerts:
                await _record_fired(session, alert.rule, bucket_key)

    return alerts


async def deliver_alert(alert: Alert) -> None:
    """Deliver a single alert by email (if configured) or log it."""
    if settings.ALERT_EMAIL_ENABLED and settings.ALERT_EMAIL_RECIPIENT:
        subject = f"[{alert.severity.upper()}] Audit anomaly: {alert.rule}"
        body_lines = [
            f"Audit anomaly detected: {alert.rule}",
            f"Severity: {alert.severity}",
            f"Bucket key: {alert.key!r}",
            f"Count: {alert.count} (threshold {alert.threshold})",
            f"Window: {alert.window_start.isoformat()} -> now",
        ]
        body = chr(10).join(body_lines)
        content = EmailMessageContent(
            to_email=settings.ALERT_EMAIL_RECIPIENT,
            subject=subject,
            body=body,
        )
        try:
            await send_email(content)
        except Exception as exc:  # noqa: BLE001 - log, do not raise
            logger.error("alert.delivery_failed rule=%s error=%s", alert.rule, exc)
            return

    logger.error(
        "anomaly.alert",
        extra={
            "rule": alert.rule,
            "severity": alert.severity,
            "key": alert.key,
            "count": alert.count,
            "threshold": alert.threshold,
        },
    )


async def run_anomaly_scan(session: AsyncSession) -> list[Alert]:
    """Run a full anomaly scan and deliver any alerts.

    Returns the list of newly fired alerts.
    """
    alerts = await scan_anomalies(session)
    for alert in alerts:
        await deliver_alert(alert)
    return alerts
