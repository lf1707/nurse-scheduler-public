"""Subscription plan feature limits.

Centralises the numeric caps per plan so API endpoints, the solver, and the
frontend all agree on what each tier can do.

Demo tenants always use the demo caps regardless of the subscription value
stored for them, so an older PRO subscription cannot expand a demo dataset.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.billing_entitlements import invoice_paid_through
from app.core.database import clear_tenant_context, set_tenant_context
from app.models.enums import SubscriptionPlan
from app.models.platform import PlatformSetting
from app.models.subscription import Subscription
from app.models.tenant import Tenant

PLAN_LIMITS_SETTING_KEY = "plan_limits"
NURSE_SEAT_PACK_SIZE = 10
MAX_NURSE_LIMIT = 10_000


@dataclass(frozen=True)
class PlanLimits:
    max_nurses: int | None  # None = unlimited
    max_period_days: int | None  # None = unlimited


@dataclass(frozen=True)
class PlanDefinition:
    key: str
    name: str
    max_nurses: int | None
    max_period_days: int | None
    is_system: bool


SYSTEM_PLAN_KEYS = {plan.value for plan in SubscriptionPlan}
DELETED_SYSTEM_KEY = "deleted_system"
PLAN_KEY_PATTERN = r"^[a-z][a-z0-9_-]{0,29}$"


def _system_definitions() -> dict[str, PlanDefinition]:
    return {
        "free": PlanDefinition("free", "Free", 8, 14, True),
        "pro": PlanDefinition("pro", "Pro", 20, 30, True),
        "max": PlanDefinition("max", "Max", None, None, True),
        "demo": PlanDefinition("demo", "Demo", 10, 14, True),
    }


async def configured_plan_definitions(
    session: AsyncSession,
    restore_tenant_id: str | None = None,
) -> tuple[dict[str, PlanDefinition], set[str]]:
    """Return defaults merged with super-admin configured overrides."""
    await clear_tenant_context(session)
    try:
        row = await session.get(PlatformSetting, PLAN_LIMITS_SETTING_KEY)
        overrides = json.loads(row.value) if row and isinstance(row.value, str) else {}
        if not isinstance(overrides, dict):
            overrides = {}
    finally:
        if restore_tenant_id:
            await set_tenant_context(session, restore_tenant_id)

    configured: dict[str, PlanDefinition] = _system_definitions()
    deleted_system = {
        key
        for key in overrides.get(DELETED_SYSTEM_KEY, [])
        if key in SYSTEM_PLAN_KEYS
    }
    configured = {
        key: definition
        for key, definition in configured.items()
        if key not in deleted_system
    }
    for key, default in configured.items():
        if key in deleted_system:
            configured.pop(key)
            continue
        values = overrides.get(key, {})
        configured[key] = PlanDefinition(
            key=key,
            name=default.name,
            max_nurses=values.get("max_nurses", default.max_nurses),
            max_period_days=values.get("max_period_days", default.max_period_days),
            is_system=True,
        )

    for values in overrides.get("custom", []):
        if not isinstance(values, dict):
            continue
        key = cast(str, values.get("key", ""))
        if key in SYSTEM_PLAN_KEYS or key in configured:
            raise ValueError(f"Duplicate plan key: {key}")
        configured[key] = PlanDefinition(
            key=key,
            name=cast(str, values.get("name", key)),
            max_nurses=values.get("max_nurses"),
            max_period_days=values.get("max_period_days"),
            is_system=False,
        )
    return configured, deleted_system


async def configured_plan_limits(
    session: AsyncSession,
    restore_tenant_id: str | None = None,
) -> dict[str, PlanDefinition]:
    """Return active plan definitions merged with configured overrides."""
    configured, _deleted = await configured_plan_definitions(session, restore_tenant_id)
    return configured


async def effective_limits_for_plan(
    session: AsyncSession, plan: str
) -> PlanLimits:
    definition = (await configured_plan_limits(session)).get(str(plan))
    if definition is None:
        return PlanLimits(0, 0)
    return PlanLimits(definition.max_nurses, definition.max_period_days)


def _effective_nurse_limit(
    definition: PlanDefinition,
    subscription: Subscription | None,
) -> int | None:
    if definition.max_nurses is None:
        return None
    seat_packs = subscription.seat_packs if subscription is not None else 0
    return min(
        MAX_NURSE_LIMIT,
        definition.max_nurses + seat_packs * NURSE_SEAT_PACK_SIZE,
    )


async def effective_limits_for_subscription(
    session: AsyncSession,
    subscription: Subscription | None,
) -> PlanLimits:
    plan = subscription.plan if subscription else SubscriptionPlan.FREE
    definition = (await configured_plan_limits(session)).get(str(plan))
    if definition is None:
        return PlanLimits(0, 0)
    return PlanLimits(
        _effective_nurse_limit(definition, subscription),
        definition.max_period_days,
    )


async def effective_limits_for_tenant(
    session: AsyncSession,
    tenant: Tenant,
    subscription: Subscription | None,
) -> PlanLimits:
    """Return effective limits, applying demo caps and paid invoices."""
    configured = await configured_plan_limits(session, tenant.id)
    if is_demo_tenant(tenant):
        definition = configured["demo"]
        return PlanLimits(definition.max_nurses, definition.max_period_days)
    else:
        key = str(subscription.plan) if subscription else SubscriptionPlan.FREE.value
        if subscription is None or not subscription.is_active:
            paid_through = await invoice_paid_through(session, tenant.id)
            if paid_through is not None and datetime.now(UTC) < paid_through:
                key = str(subscription.plan) if subscription else SubscriptionPlan.PRO.value
        if key not in configured:
            key = SubscriptionPlan.FREE.value
        definition = configured[key]
        return PlanLimits(
            _effective_nurse_limit(definition, subscription),
            definition.max_period_days,
        )


def is_demo_tenant(tenant: Tenant | None) -> bool:
    """True when the tenant is the seeded demo dataset."""
    if not tenant:
        return False
    if tenant.slug == "demo":
        return True
    if not tenant.settings:
        return False
    if tenant.settings.get("kind") == "test":
        return False
    return tenant.settings.get("dataset") == "demo"
