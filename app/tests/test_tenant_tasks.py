"""Offline tests for tenant/subscription lifecycle task wiring."""

from __future__ import annotations

import inspect

from app.tasks import tenant_tasks
from app.tasks.celery_app import celery_app


def test_subscription_expiry_task_registered_and_scheduled() -> None:
    assert "subscription.expire_trials" in celery_app.tasks
    assert celery_app.conf.beat_schedule["subscription-expire-trials"] == {
        "task": "subscription.expire_trials",
        "schedule": 3600.0,
    }


def test_subscription_expiry_has_async_wrapper() -> None:
    assert inspect.iscoroutinefunction(tenant_tasks._expire_subscriptions)
    assert hasattr(tenant_tasks.expire_trial_subscriptions, "delay")
