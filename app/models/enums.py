"""Enumerations shared across models."""

from __future__ import annotations

import enum


class UserRole(enum.StrEnum):
    """System-wide role (separate from clinical Role)."""

    SUPER_ADMIN = "super_admin"  # platform operator, cross-tenant
    TENANT_ADMIN = "tenant_admin"  # hospital admin, full tenant access
    SCHEDULER = "scheduler"  # can create/edit schedules
    NURSE = "nurse"  # can view own schedule + submit preferences
    VIEWER = "viewer"  # read-only


class ScheduleStatus(enum.StrEnum):
    """Lifecycle of a ScheduleRequest."""

    PENDING = "pending"  # queued, not yet picked up by worker
    RUNNING = "running"  # worker processing
    COMPLETED = "completed"  # solved + persisted
    FAILED = "failed"  # solver error or infeasible
    CANCELLED = "cancelled"  # user cancelled


class SolverOutcome(enum.StrEnum):
    """CP-SAT solver exit status."""

    OPTIMAL = "optimal"
    FEASIBLE = "feasible"
    INFEASIBLE = "infeasible"
    UNKNOWN = "unknown"  # no solution found within time limit
    MODEL_INVALID = "model_invalid"


class RequestType(enum.StrEnum):
    """Nurse preference type."""

    LIKE = "like"  # wants this shift
    AVOID = "avoid"  # does not want this shift


class SubscriptionPlan(enum.StrEnum):
    """Tenant subscription plan tier."""

    FREE = "free"
    PRO = "pro"
    MAX = "max"
    DEMO = "demo"


class SubscriptionType(enum.StrEnum):
    """How a subscription was created and how it is expected to renew."""

    TRIAL = "trial"
    MANUAL = "manual"
    PROVIDER = "provider"


class BillingInvoiceStatus(enum.StrEnum):
    """Lifecycle of a manually tracked tenant invoice."""

    ISSUED = "issued"
    PAID = "paid"
    VOID = "void"


class BillingProvider(enum.StrEnum):
    """Source of a billing record."""

    MANUAL = "manual"
    STRIPE = "stripe"


class BillingSubscriptionStatus(enum.StrEnum):
    """Provider-neutral subscription states stored locally."""

    TRIALING = "trialing"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCELED = "canceled"
    INCOMPLETE = "incomplete"
    UNPAID = "unpaid"
    PAUSED = "paused"


class BillingOperationType(enum.StrEnum):
    """Provider operation types that may outlive an HTTP request."""

    CREATE_CUSTOMER = "create_customer"
    CREATE_CHECKOUT_SESSION = "create_checkout_session"
    CREATE_PORTAL_SESSION = "create_portal_session"
    CHANGE_SUBSCRIPTION = "change_subscription"
    CANCEL_SUBSCRIPTION = "cancel_subscription"
    RECONCILE_SUBSCRIPTION = "reconcile_subscription"


class BillingOperationStatus(enum.StrEnum):
    """Lifecycle of a provider operation tracked locally."""

    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class TenantApplicationStatus(enum.StrEnum):
    """Lifecycle of a prospective-tenant application."""

    PENDING_EMAIL_VERIFICATION = "pending_email_verification"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class DayOfWeek(enum.StrEnum):
    """ISO weekday."""

    MON = "mon"
    TUE = "tue"
    WED = "wed"
    THU = "thu"
    FRI = "fri"
    SAT = "sat"
    SUN = "sun"
