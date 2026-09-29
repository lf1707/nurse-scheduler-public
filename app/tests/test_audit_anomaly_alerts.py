"""Unit tests for audit anomaly detection and alerting.

These tests exercise the pure detection logic (``detect_anomalies``) and the
rule builder (``build_rules``) without a live database or broker, keeping the
tests deterministic and dependency-free.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Literal, cast

import pytest

from app.core import audit_alerts
from app.core.config import settings
from app.models.platform import SecurityAuditEvent


def _event(
    *, ip: str, actor_id: str | None, action: str, outcome: str = "success"
) -> SecurityAuditEvent:
    """Build a minimal SecurityAuditEvent for detection tests."""
    return SecurityAuditEvent(
        ip_address=ip,
        actor_id=actor_id,
        action=action,
        outcome=outcome,
    )


class _FakeNested:
    async def __aenter__(self) -> _FakeNested:
        return self

    async def __aexit__(self, *_args: Any) -> Literal[False]:
        return False


class _FakeScanSession:
    def __init__(self, aggregates: list[Any]) -> None:
        self.aggregates = aggregates
        self.queries: list[Any] = []

    def begin_nested(self) -> _FakeNested:
        return _FakeNested()

    async def execute(self, query: Any, *_args: Any, **_kwargs: Any) -> _FakeScanSession:
        self.queries.append(query)
        return self

    def all(self) -> list[Any]:
        return self.aggregates


def _login_events(ip: str, n: int) -> list[SecurityAuditEvent]:
    return [
        _event(ip=ip, actor_id=None, action="auth.login", outcome="failure")
        for _ in range(n)
    ]


class TestBuildRules:
    """Default rules are assembled from settings."""

    def test_expected_rules_present(self) -> None:
        names = {r.name for r in audit_alerts.build_rules()}
        assert {
            "login_failure_rate",
            "impersonation_rate",
            "super_admin_action_rate",
            "signup_rate",
            "export_volume",
        } <= names

    def test_rule_thresholds_match_settings(self) -> None:
        rules = {r.name: r for r in audit_alerts.build_rules()}
        assert rules["login_failure_rate"].threshold == (
            settings.ALERT_LOGIN_FAILURE_RATE
        )
        assert rules["signup_rate"].threshold == (
            settings.ALERT_SIGNUP_RATE
        )

    def test_rule_disabled_when_threshold_zero(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            settings, "ALERT_SIGNUP_RATE", 0
        )
        names = {r.name for r in audit_alerts.build_rules()}
        assert "signup_rate" not in names


class TestBucketWindow:
    """Dedup buckets collapse to one window per ANOMALY_ALERT_WINDOW_SECONDS."""

    def test_window_uses_config_seconds(self) -> None:
        now = datetime(2026, 8, 29, 12, 0, 0, tzinfo=UTC)
        assert audit_alerts._bucket_window(now) == int(
            now.timestamp()
        ) // settings.ANOMALY_ALERT_WINDOW_SECONDS

    def test_two_minutes_apart_in_same_bucket(self) -> None:
        now = datetime(2026, 8, 29, 12, 0, 0, tzinfo=UTC)
        later = now + timedelta(
            seconds=settings.ANOMALY_ALERT_WINDOW_SECONDS - 1
        )
        assert audit_alerts._bucket_window(now) == audit_alerts._bucket_window(later)


class TestDetectAnomalies:
    """Detection counts per bucket and raises threshold breaches."""

    def test_below_threshold_no_alert(self) -> None:
        now = datetime(2026, 8, 29, 12, 0, 0, tzinfo=UTC)
        events = _login_events("1.1.1.1", settings.ALERT_LOGIN_FAILURE_RATE - 1)
        alerts = audit_alerts.detect_anomalies(events, audit_alerts.build_rules(), now, fired=set())
        assert all(a.rule != "login_failure_rate" for a in alerts)

    def test_at_threshold_raises_login_failure(self) -> None:
        now = datetime(2026, 8, 29, 12, 0, 0, tzinfo=UTC)
        threshold = settings.ALERT_LOGIN_FAILURE_RATE
        events = _login_events("1.1.1.1", threshold)
        alerts = [
            a for a in audit_alerts.detect_anomalies(events, audit_alerts.build_rules(), now, fired=set())
            if a.rule == "login_failure_rate"
        ]
        assert len(alerts) == 1
        assert alerts[0].count == threshold
        assert alerts[0].key == "1.1.1.1"
        assert alerts[0].severity == "critical"

    def test_dedup_within_window(self) -> None:
        now = datetime(2026, 8, 29, 12, 0, 0, tzinfo=UTC)
        threshold = settings.ALERT_LOGIN_FAILURE_RATE
        events = _login_events("1.1.1.1", threshold)
        fired: set[tuple[str, int]] = set()
        first = audit_alerts.detect_anomalies(events, audit_alerts.build_rules(), now, fired=fired)
        assert any(a.rule == "login_failure_rate" for a in first)
        # Same window, same fired set -> no duplicate alert.
        second = audit_alerts.detect_anomalies(events, audit_alerts.build_rules(), now, fired=fired)
        assert not any(a.rule == "login_failure_rate" for a in second)

    def test_cross_bucket_realert(self) -> None:
        now = datetime(2026, 8, 29, 12, 0, 0, tzinfo=UTC)
        threshold = settings.ALERT_LOGIN_FAILURE_RATE
        events = _login_events("1.1.1.1", threshold)
        fired: set[tuple[str, int]] = set()
        first = audit_alerts.detect_anomalies(events, audit_alerts.build_rules(), now, fired=fired)
        later = now + timedelta(
            seconds=settings.ANOMALY_ALERT_WINDOW_SECONDS + 1
        )
        second = audit_alerts.detect_anomalies(events, audit_alerts.build_rules(), later, fired=set())
        assert any(a.rule == "login_failure_rate" for a in first)
        assert any(a.rule == "login_failure_rate" for a in second)

    def test_impersonation_keyed_by_actor(self) -> None:
        now = datetime(2026, 8, 29, 12, 0, 0, tzinfo=UTC)
        threshold = settings.ALERT_IMPERSONATION_RATE
        # Same actor id many times -> impersonation burst.
        events = [
            _event(ip="1.1.1.1", actor_id="actor-xyz", action="auth.impersonate")
            for _ in range(threshold)
        ]
        alerts = [
            a for a in audit_alerts.detect_anomalies(events, audit_alerts.build_rules(), now, fired=set())
            if a.rule == "impersonation_rate"
        ]
        assert len(alerts) == 1
        assert alerts[0].key == "actor-xyz"

    def test_login_rule_filters_action_and_outcome(self) -> None:
        now = datetime(2026, 8, 29, 12, 0, 0, tzinfo=UTC)
        threshold = settings.ALERT_LOGIN_FAILURE_RATE
        events = [
            *_login_events("1.1.1.1", threshold - 1),
            _event(ip="1.1.1.1", actor_id=None, action="auth.login", outcome="success"),
            _event(ip="1.1.1.1", actor_id=None, action="schedule.generate", outcome="failure"),
        ]
        alerts = audit_alerts.detect_anomalies(
            events, audit_alerts.build_rules(), now, fired=set()
        )
        assert all(a.rule != "login_failure_rate" for a in alerts)

    def test_login_rule_requires_all_failures(self) -> None:
        now = datetime(2026, 8, 29, 12, 0, 0, tzinfo=UTC)
        threshold = settings.ALERT_LOGIN_FAILURE_RATE
        events = [
            *_login_events("1.1.1.1", threshold - 1),
            _event(ip="1.1.1.1", actor_id=None, action="auth.login", outcome="success"),
        ]
        alerts = audit_alerts.detect_anomalies(
            events, audit_alerts.build_rules(), now, fired=set()
        )
        assert all(a.rule != "login_failure_rate" for a in alerts)

    def test_impersonation_rule_filters_other_actions(self) -> None:
        now = datetime(2026, 8, 29, 12, 0, 0, tzinfo=UTC)
        threshold = settings.ALERT_IMPERSONATION_RATE
        events = [
            _event(ip="1.1.1.1", actor_id="actor-xyz", action="admin.site_info_update")
            for _ in range(threshold)
        ]
        alerts = audit_alerts.detect_anomalies(
            events, audit_alerts.build_rules(), now, fired=set()
        )
        assert all(a.rule != "impersonation_rate" for a in alerts)

    def test_super_admin_rule_requires_global_actor(self) -> None:
        now = datetime(2026, 8, 29, 12, 0, 0, tzinfo=UTC)
        threshold = settings.ALERT_SUPER_ADMIN_ACTIONS
        tenant_actor_events = [
            _event(ip="1.1.1.1", actor_id="tenant-admin", action="admin.site_info_update")
            for _ in range(threshold)
        ]
        for event in tenant_actor_events:
            event.actor_tenant_id = "tenant-1"
        alerts = audit_alerts.detect_anomalies(
            tenant_actor_events, audit_alerts.build_rules(), now, fired=set()
        )
        assert all(a.rule != "super_admin_action_rate" for a in alerts)

        global_actor_events = [
            _event(ip="1.1.1.1", actor_id="super-admin", action="admin.site_info_update")
            for _ in range(threshold)
        ]
        alerts = audit_alerts.detect_anomalies(
            global_actor_events, audit_alerts.build_rules(), now, fired=set()
        )
        assert any(a.rule == "super_admin_action_rate" for a in alerts)

    def test_export_rule_counts_only_export_actions(self) -> None:
        now = datetime(2026, 8, 29, 12, 0, 0, tzinfo=UTC)
        threshold = settings.ALERT_EXPORT_RATE
        events = [
            *(
                _event(ip="1.1.1.1", actor_id="actor", action="auth.login")
                for _ in range(threshold)
            ),
            _event(ip="1.1.1.1", actor_id="actor", action="schedule.export"),
        ]
        alerts = [
            a
            for a in audit_alerts.detect_anomalies(
                events, audit_alerts.build_rules(), now, fired=set()
            )
            if a.rule == "export_volume"
        ]
        assert all(alert.count < threshold for alert in alerts)

    def test_no_rules_no_alert(self) -> None:
        now = datetime(2026, 8, 29, 12, 0, 0, tzinfo=UTC)
        events = _login_events("1.1.1.1", 100)
        alerts = audit_alerts.detect_anomalies(events, [], now, fired=set())
        assert alerts == []


async def test_scan_anomalies_aggregates_and_counts_rate_limited_logins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)
    aggregates = [
        SimpleNamespace(
            action="auth.login",
            outcome="failure",
            ip_address="198.51.100.2",
            actor_id=None,
            actor_tenant_id=None,
            count=1,
        ),
        SimpleNamespace(
            action="auth.login.rate_limited",
            outcome="denied",
            ip_address="198.51.100.2",
            actor_id=None,
            actor_tenant_id=None,
            count=1,
        ),
        SimpleNamespace(
            action="auth.login.rate_limited",
            outcome="denied",
            ip_address="198.51.100.2",
            actor_id="actor-1",
            actor_tenant_id=None,
            count=1,
        ),
        SimpleNamespace(
            action="schedule.export",
            outcome="success",
            ip_address="198.51.100.2",
            actor_id="actor-1",
            actor_tenant_id=None,
            count=5,
        ),
    ]
    session = _FakeScanSession(aggregates)

    async def ensure(_session: Any) -> None:
        return None

    async def fired(_session: Any) -> set[tuple[str, int]]:
        return set()

    monkeypatch.setattr(audit_alerts, "_ensure_fired_table", ensure)
    monkeypatch.setattr(audit_alerts, "_load_fired", fired)
    monkeypatch.setattr(settings, "ALERT_LOGIN_FAILURE_RATE", 2)
    monkeypatch.setattr(settings, "ALERT_EXPORT_RATE", 1)

    alerts = await cast(Any, audit_alerts.scan_anomalies)(
        session,
        now=now,
        rules=audit_alerts.build_rules(),
    )

    queries = [str(query).lower() for query in session.queries]
    assert any("count(*)" in query for query in queries)
    assert any("group by" in query for query in queries)
    assert [alert.key for alert in alerts] == ["198.51.100.2", "schedule.export"]
    assert [alert.count for alert in alerts] == [3, 5]
