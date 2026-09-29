"""All SQLAlchemy models — import here so Alembic can autodetect them."""

from app.core.database import Base, TenantBase
from app.models.billing import (
    BillingCustomer,
    BillingInvoice,
    BillingOperation,
    BillingSubscription,
)
from app.models.enums import (
    BillingInvoiceStatus,
    BillingOperationStatus,
    BillingOperationType,
    BillingProvider,
    BillingSubscriptionStatus,
    DayOfWeek,
    RequestType,
    ScheduleStatus,
    SolverOutcome,
    SubscriptionPlan,
    TenantApplicationStatus,
    UserRole,
)
from app.models.nurse import (
    Contract,
    Leave,
    Nurse,
    NursePreference,
    Role,
    Skill,
    nurse_roles,
    nurse_skills,
)
from app.models.platform import AuditExportWatermark, PlatformSetting, SecurityAuditEvent
from app.models.refresh_token import RefreshToken
from app.models.rule import (
    ShiftSequenceRoleRestriction,
    ShiftSequenceRule,
    ShiftSequenceStep,
    SkillMixRequirement,
    SkillMixRule,
)
from app.models.schedule import Assignment, Schedule, ScheduleRequest
from app.models.shift import DayGroup, DayGroupDay, ShiftTemplate
from app.models.subscription import Subscription
from app.models.tenant import Tenant
from app.models.tenant import _tenant_name as _tenant_name
from app.models.tenant_application import TenantApplication
from app.models.user import User

__all__ = [
    "Base",
    "TenantBase",
    "BillingInvoice",
    "BillingCustomer",
    "BillingOperation",
    "BillingSubscription",
    "UserRole",
    "BillingInvoiceStatus",
    "BillingProvider",
    "BillingOperationStatus",
    "BillingOperationType",
    "BillingSubscriptionStatus",
    "ScheduleStatus",
    "SolverOutcome",
    "RequestType",
    "DayOfWeek",
    "Tenant",
    "PlatformSetting",
    "SecurityAuditEvent",
    "AuditExportWatermark",
    "RefreshToken",
    "User",
    "Nurse",
    "Role",
    "Skill",
    "Contract",
    "Leave",
    "NursePreference",
    "nurse_roles",
    "nurse_skills",
    "DayGroup",
    "DayGroupDay",
    "ShiftTemplate",
    "SkillMixRule",
    "SkillMixRequirement",
    "ShiftSequenceRule",
    "ShiftSequenceStep",
    "ShiftSequenceRoleRestriction",
    "ScheduleRequest",
    "Schedule",
    "Assignment",
    "Subscription",
    "SubscriptionPlan",
    "TenantApplication",
    "TenantApplicationStatus",
]
