"""API v1 router aggregation."""

from fastapi import APIRouter

from app.api.v1 import (
    admin,
    auth,
    billing,
    billing_state,
    nurses,
    rules,
    schedules,
    shifts,
    subscriptions,
    tenant_applications,
    tenants,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(admin.router)
api_router.include_router(tenants.router)
api_router.include_router(tenant_applications.router)
api_router.include_router(nurses.router)
api_router.include_router(nurses.role_router)
api_router.include_router(nurses.skill_router)
api_router.include_router(shifts.day_group_router)
api_router.include_router(shifts.shift_router)
api_router.include_router(rules.skill_mix_router)
api_router.include_router(rules.shift_sequence_router)
api_router.include_router(schedules.router)
api_router.include_router(subscriptions.router)
api_router.include_router(billing.router)
api_router.include_router(billing_state.router)
