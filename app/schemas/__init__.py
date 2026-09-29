"""Pydantic v2 schemas — request/response models for the API."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, time
from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)

from app.core.security import validate_password_strength
from app.models.enums import (
    RequestType,
    ScheduleStatus,
    SolverOutcome,
    TenantApplicationStatus,
    UserRole,
)
from app.models.enums import (
    SubscriptionPlan as SubscriptionPlan,
)

T = TypeVar("T")


# ──────────────────────────────────────────────────────────────
# Generic pagination wrapper
# ──────────────────────────────────────────────────────────────
class Paginated(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int


class SecurityAuditEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    action: str
    outcome: str
    actor_id: str | None
    actor_tenant_id: str | None
    target_user_id: str | None
    tenant_id: str | None
    ip_address: str | None
    user_agent: str | None
    details: dict[str, Any]
    created_at: datetime


# ──────────────────────────────────────────────────────────────
# Auth
# ──────────────────────────────────────────────────────────────
class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=1, max_length=128)
    totp_code: str | None = Field(None, min_length=6, max_length=8)
    recovery_codes: list[str] | None = Field(
        None,
        min_length=1,
        max_length=10,
    )


class LoginResponse(TokenResponse):
    recovery_codes: list[str] = Field(default_factory=list)


class RefreshRequest(BaseModel):
    refresh_token: str | None = Field(None, min_length=1, max_length=4096)


# ──────────────────────────────────────────────────────────────
# Tenant
# ──────────────────────────────────────────────────────────────
class TenantCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    slug: str = Field(..., min_length=1, max_length=100, pattern=r"^[a-z0-9-]+$")
    settings: dict[str, Any] = Field(default_factory=dict)


class TenantUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    settings: dict[str, Any] | None = None
    is_active: bool | None = None
    admin_email: EmailStr | None = None


class TenantRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    slug: str
    settings: dict[str, Any]
    is_active: bool
    created_at: datetime


# ──────────────────────────────────────────────────────────────
# Prospective tenant application
# ──────────────────────────────────────────────────────────────
class TenantApplicationCreate(BaseModel):
    organization_name: str = Field(..., min_length=2, max_length=200)
    contact_name: str = Field(..., min_length=1, max_length=200)
    contact_email: EmailStr = Field(..., max_length=320)
    country: str = Field(..., min_length=2, max_length=100)
    timezone: str = Field(..., min_length=1, max_length=64)
    expected_nurse_count: int = Field(..., ge=1, le=10_000)
    use_case_summary: str = Field(..., min_length=20, max_length=4_000)
    honeypot: str = Field("", max_length=200)
    form_started_at: int = Field(..., ge=0)

    @field_validator(
        "organization_name",
        "contact_name",
        "country",
        "timezone",
        "use_case_summary",
    )
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Text must not be blank")
        return normalized


class TenantApplicationReceipt(BaseModel):
    message: str


class TenantApplicationVerifyRequest(BaseModel):
    token: str = Field(..., min_length=32, max_length=256)


class TenantApplicationSetupRequest(BaseModel):
    token: str = Field(..., min_length=32, max_length=256)
    new_password: str = Field(..., min_length=8, max_length=128)

    @field_validator("new_password")
    @classmethod
    def validate_new_password_strength(cls, value: str) -> str:
        validate_password_strength(value)
        return value


class TenantApplicationResendRequest(BaseModel):
    email: EmailStr


class TenantApplicationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_name: str
    contact_name: str
    contact_email: str
    country: str
    timezone: str
    expected_nurse_count: int
    use_case_summary: str
    status: TenantApplicationStatus
    verification_expires_at: datetime | None
    verified_at: datetime | None
    setup_expires_at: datetime | None
    setup_completed_at: datetime | None
    reviewed_by_user_id: str | None
    reviewed_at: datetime | None
    rejection_reason: str | None
    review_notes: str | None
    created_tenant_id: str | None
    created_user_id: str | None
    ip_address: str | None
    user_agent: str | None
    created_at: datetime
    updated_at: datetime


class TenantApplicationApproveRequest(BaseModel):
    review_notes: str | None = Field(None, max_length=2_000)


class TenantApplicationRejectRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=1_000)
    review_notes: str | None = Field(None, max_length=2_000)


class TenantApplicationDecisionResponse(BaseModel):
    application: TenantApplicationRead
    email_sent: bool = True


class TenantApplicationSetupResponse(BaseModel):
    message: str


# ──────────────────────────────────────────────────────────────
# User
# ──────────────────────────────────────────────────────────────
class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)
    first_name: str
    last_name: str
    role: UserRole = UserRole.VIEWER
    nurse_id: str | None = None

    @field_validator("password")
    @classmethod
    def validate_password_strength_field(cls, value: str) -> str:
        validate_password_strength(value)
        return value


class UserUpdate(BaseModel):
    password: str | None = Field(None, min_length=8, max_length=128)
    first_name: str | None = Field(None, min_length=1, max_length=100)
    last_name: str | None = Field(None, min_length=1, max_length=100)
    nurse_id: str | None = None

    @field_validator("password")
    @classmethod
    def validate_password_strength_field(cls, value: str | None) -> str | None:
        if value is not None:
            validate_password_strength(value)
        return value
    is_active: bool | None = None


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    email: EmailStr
    first_name: str
    last_name: str
    role: UserRole
    is_active: bool
    tenant_id: str | None
    tenant_name: str | None = None  # populated by /auth/me (join on tenants)
    tenant_slug: str | None = None  # populated by /auth/me (join on tenants)
    nurse_id: str | None
    nurse_name: str | None = None
    nurse_skill_ids: list[str] = Field(default_factory=list)
    nurse_skill_names: list[str] = Field(default_factory=list)
    nurse_role_ids: list[str] = Field(default_factory=list)
    nurse_role_names: list[str] = Field(default_factory=list)
    mfa_enabled: bool = False


class MfaEnrollmentConfirm(BaseModel):
    """POST /auth/me/mfa/confirm — verifies the first code and activates MFA."""

    code: str = Field(..., min_length=6, max_length=8)


class MfaEnrollmentRead(BaseModel):
    provisioning_uri: str
    qr_code_svg: str


class MfaRecoveryCodes(BaseModel):
    codes: list[str] = Field(..., min_length=1)


class UserProfileUpdate(BaseModel):
    """PATCH /auth/me — self-service name update."""
    first_name: str = Field(..., min_length=1, max_length=100)
    last_name: str = Field(..., min_length=1, max_length=100)


class UserSkillUpdate(BaseModel):
    """PATCH /auth/me/skills — self-service nurse skill update."""
    skill_ids: list[str]


class PasswordChange(BaseModel):
    """POST /auth/me/password — self-service password change."""
    old_password: str = Field(..., min_length=1, max_length=128)
    new_password: str = Field(..., min_length=8, max_length=128)

    @field_validator("new_password")
    @classmethod
    def validate_new_password_strength(cls, value: str) -> str:
        validate_password_strength(value)
        return value


# ──────────────────────────────────────────────────────────────
# Subscription
# ──────────────────────────────────────────────────────────────
class SubscriptionUpsert(BaseModel):
    plan: str = Field(..., min_length=1, max_length=30, pattern=r"^[a-z][a-z0-9_-]{0,29}$")
    subscription_type: str = Field(
        default="manual",
        pattern=r"^(trial|manual|provider)$",
    )
    seat_packs: int = Field(default=0, ge=0, le=1_000)
    starts_at: datetime | None = None  # null → now
    ends_at: datetime | None = None  # null → perpetual
    is_canceled: bool = False


class SubscriptionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    tenant_id: str
    plan: str
    subscription_type: str
    seat_packs: int
    starts_at: datetime
    ends_at: datetime | None
    is_canceled: bool
    is_active: bool
    created_at: datetime
    updated_at: datetime


class PlanLimitValues(BaseModel):
    max_nurses: int | None = Field(None, ge=0, le=10_000)
    max_period_days: int | None = Field(None, ge=1, le=3_650)


class TenantAdminCreate(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)
    first_name: str = Field(..., min_length=1, max_length=100)
    last_name: str = Field(..., min_length=1, max_length=100)

    @field_validator("password")
    @classmethod
    def validate_password_strength(cls, value: str) -> str:
        validate_password_strength(value)
        return value


class TenantWithAdminCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    slug: str = Field(..., min_length=1, max_length=100, pattern=r"^[a-z0-9-]+$")
    settings: dict[str, Any] = Field(default_factory=dict)
    admin: TenantAdminCreate
    subscription: SubscriptionUpsert | None = None


class TenantWithAdminRead(BaseModel):
    tenant: TenantRead
    admin: UserRead
    subscription: SubscriptionRead | None = None


class CustomPlanConfig(BaseModel):
    key: str = Field(..., max_length=30, pattern=r"^[a-z][a-z0-9_-]{0,29}$")
    name: str = Field(..., min_length=1, max_length=30)
    max_nurses: int | None = Field(None, ge=0, le=10_000)
    max_period_days: int | None = Field(None, ge=1, le=3_650)


class PlanLimitsConfig(BaseModel):
    free: PlanLimitValues | None = None
    pro: PlanLimitValues | None = None
    max: PlanLimitValues | None = None
    demo: PlanLimitValues | None = None
    deleted_system: list[str] = Field(default_factory=list)
    custom: list[CustomPlanConfig] = Field(default_factory=list, max_length=20)


class PlanLimitsRead(BaseModel):
    max_nurses: int | None
    max_period_days: int | None


class BillingInvoiceCreate(BaseModel):
    """Super-admin request to record one manual or imported invoice."""

    tenant_id: str = Field(..., min_length=36, max_length=36)
    number: str = Field(..., min_length=1, max_length=40, pattern=r"^[A-Za-z0-9._:-]+$")
    provider: str = Field(default="manual", pattern=r"^(manual|stripe)$")
    provider_invoice_id: str | None = Field(None, min_length=1, max_length=100)
    amount_minor: int = Field(..., gt=0, le=1_000_000_000_00)
    currency: str = Field(default="USD", pattern=r"^[A-Za-z]{3}$")
    status: str = Field(default="issued", pattern=r"^(issued|paid)$")
    period_start: datetime | None = None
    period_end: datetime | None = None
    issued_at: datetime | None = None
    due_at: datetime | None = None
    paid_at: datetime | None = None
    notes: str | None = Field(None, max_length=2_000)

    @model_validator(mode="after")
    def validate_billing_periods(self) -> BillingInvoiceCreate:
        if self.period_start and self.period_end and self.period_start >= self.period_end:
            raise ValueError("period_start must be earlier than period_end")
        if self.due_at and self.issued_at and self.due_at < self.issued_at:
            raise ValueError("due_at cannot be earlier than issued_at")
        if self.status == "paid" and not self.paid_at:
            self.paid_at = self.issued_at or datetime.now(UTC)
        if self.status != "paid" and self.paid_at:
            raise ValueError("paid_at requires status=paid")
        if self.status == "paid" and not self.period_end:
            raise ValueError("paid invoices require period_end")
        if self.provider == "stripe" and not self.provider_invoice_id:
            raise ValueError("provider_invoice_id is required for stripe invoices")
        return self


class BillingInvoiceUpdate(BaseModel):
    """Super-admin request to advance or annotate an invoice."""

    status: str | None = Field(None, pattern=r"^(issued|paid|void)$")
    period_start: datetime | None = None
    period_end: datetime | None = None
    due_at: datetime | None = None
    notes: str | None = Field(None, max_length=2_000)

    @model_validator(mode="after")
    def validate_billing_periods(self) -> BillingInvoiceUpdate:
        if self.period_start and self.period_end and self.period_start >= self.period_end:
            raise ValueError("period_start must be earlier than period_end")
        return self


class BillingInvoiceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    number: str
    provider: str
    provider_invoice_id: str | None
    amount_minor: int
    currency: str
    status: str
    period_start: datetime | None
    period_end: datetime | None
    issued_at: datetime
    due_at: datetime | None
    paid_at: datetime | None
    notes: str | None
    created_at: datetime
    updated_at: datetime


class BillingCustomerCreate(BaseModel):
    """Super-admin request to register one provider customer identifier."""

    tenant_id: str = Field(..., min_length=36, max_length=36)
    provider: str = Field(..., pattern=r"^(manual|stripe)$")
    provider_customer_id: str = Field(..., min_length=1, max_length=100)
    email: EmailStr | None = None


class BillingCustomerRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    provider: str
    provider_customer_id: str
    email: str | None
    created_at: datetime
    updated_at: datetime


class BillingSubscriptionCreate(BaseModel):
    """Super-admin request to record provider subscription state."""

    tenant_id: str = Field(..., min_length=36, max_length=36)
    provider: str = Field(..., pattern=r"^(manual|stripe)$")
    provider_subscription_id: str = Field(..., min_length=1, max_length=100)
    customer_id: str = Field(..., min_length=36, max_length=36)
    plan: str | None = Field(None, pattern=r"^(free|pro|max|demo)$")
    provider_price_id: str | None = Field(None, min_length=1, max_length=100)
    status: str = Field(
        default="incomplete",
        pattern=r"^(trialing|active|past_due|canceled|incomplete|unpaid|paused)$",
    )
    current_period_start: datetime | None = None
    current_period_end: datetime | None = None
    trial_end: datetime | None = None
    cancel_at_period_end: bool = False
    canceled_at: datetime | None = None

    @model_validator(mode="after")
    def validate_subscription_periods(self) -> BillingSubscriptionCreate:
        if (
            self.current_period_start
            and self.current_period_end
            and self.current_period_start >= self.current_period_end
        ):
            raise ValueError("current_period_start must be earlier than current_period_end")
        if self.canceled_at and not self.cancel_at_period_end and self.status != "canceled":
            raise ValueError("canceled_at requires cancel_at_period_end or status=canceled")
        return self


class BillingSubscriptionUpdate(BaseModel):
    """Super-admin request to synchronize provider subscription state."""

    status: str | None = Field(
        None,
        pattern=r"^(trialing|active|past_due|canceled|incomplete|unpaid|paused)$",
    )
    plan: str | None = Field(None, pattern=r"^(free|pro|max|demo)$")
    provider_price_id: str | None = Field(None, min_length=1, max_length=100)
    current_period_start: datetime | None = None
    current_period_end: datetime | None = None
    trial_end: datetime | None = None
    cancel_at_period_end: bool | None = None
    canceled_at: datetime | None = None

    @model_validator(mode="after")
    def validate_subscription_periods(self) -> BillingSubscriptionUpdate:
        if (
            self.current_period_start
            and self.current_period_end
            and self.current_period_start >= self.current_period_end
        ):
            raise ValueError("current_period_start must be earlier than current_period_end")
        return self


class BillingSubscriptionReconcile(BaseModel):
    """A verified provider snapshot submitted to the reconciliation applier."""

    provider_customer_id: str = Field(..., min_length=1, max_length=100)
    provider_subscription_id: str = Field(..., min_length=1, max_length=100)
    observation_id: str = Field(..., min_length=1, max_length=80)
    observed_at: datetime
    status: str = Field(
        ...,
        pattern=r"^(trialing|active|past_due|canceled|incomplete|unpaid|paused)$",
    )
    plan: str | None = Field(None, pattern=r"^(free|pro|max|demo)$")
    provider_price_id: str | None = Field(None, min_length=1, max_length=100)
    current_period_start: datetime | None = None
    current_period_end: datetime | None = None
    trial_end: datetime | None = None
    cancel_at_period_end: bool = False
    canceled_at: datetime | None = None

    @model_validator(mode="after")
    def validate_reconciliation(self) -> BillingSubscriptionReconcile:
        if (
            self.current_period_start is not None
            and self.current_period_end is not None
            and self.current_period_start >= self.current_period_end
        ):
            raise ValueError("current_period_start must be earlier than current_period_end")
        if self.canceled_at is not None and (
            not self.cancel_at_period_end and self.status != "canceled"
        ):
            raise ValueError("canceled_at requires cancel_at_period_end or canceled status")
        return self


class BillingReconciliationRead(BaseModel):
    """Subscription state and the operation produced by one reconciliation."""

    subscription: BillingSubscriptionRead
    outcome: str = Field(pattern=r"^(in_sync|updated)$")
    operation: BillingOperationRead
    changes: list[dict[str, str | None]]


class BillingSubscriptionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    provider: str
    provider_subscription_id: str
    customer_id: str
    plan: str | None
    provider_price_id: str | None
    status: str
    current_period_start: datetime | None
    current_period_end: datetime | None
    trial_end: datetime | None
    cancel_at_period_end: bool
    canceled_at: datetime | None
    created_at: datetime
    updated_at: datetime


class BillingOperationCreate(BaseModel):
    """Super-admin request to start or record one idempotent provider operation."""

    tenant_id: str = Field(..., min_length=36, max_length=36)
    provider: str = Field(..., pattern=r"^(manual|stripe)$")
    operation_type: str = Field(
        ...,
        pattern=r"^(create_customer|create_checkout_session|create_portal_session"
        r"|change_subscription|cancel_subscription|reconcile_subscription)$",
    )
    idempotency_key: str = Field(..., min_length=8, max_length=120)
    provider_operation_id: str | None = Field(None, min_length=1, max_length=100)
    customer_id: str | None = Field(None, min_length=36, max_length=36)
    subscription_id: str | None = Field(None, min_length=36, max_length=36)
    status: str = Field(
        default="pending",
        pattern=r"^(pending|completed|failed|canceled)$",
    )
    error_code: str | None = Field(None, min_length=1, max_length=80)
    error_message: str | None = Field(None, min_length=1, max_length=2_000)
    result: dict[str, Any] | None = Field(None, max_length=100)
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def validate_operation_state(self) -> BillingOperationCreate:
        if self.status == "pending" and any(
            [self.error_code, self.error_message, self.completed_at]
        ):
            raise ValueError("failed and terminal operations require a non-pending status")
        if self.status != "pending" and not self.provider_operation_id and (
            self.operation_type != "create_customer"
        ):
            raise ValueError("provider_operation_id is required to complete this operation")
        return self


class BillingOperationUpdate(BaseModel):
    """Super-admin request to advance one provider operation."""

    status: str | None = Field(
        None,
        pattern=r"^(pending|completed|failed|canceled)$",
    )
    provider_operation_id: str | None = Field(None, min_length=1, max_length=100)
    error_code: str | None = Field(None, min_length=1, max_length=80)
    error_message: str | None = Field(None, min_length=1, max_length=2_000)
    result: dict[str, Any] | None = Field(None, max_length=100)
    completed_at: datetime | None = None


class BillingOperationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    provider: str
    operation_type: str
    status: str
    idempotency_key: str
    provider_operation_id: str | None
    customer_id: str | None
    subscription_id: str | None
    error_code: str | None
    error_message: str | None
    result: dict[str, Any] | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


# ──────────────────────────────────────────────────────────────
# Clinical Role / Skill
# ──────────────────────────────────────────────────────────────
class RoleCreate(BaseModel):
    name: str
    code: str
    description: str | None = None


class RoleUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=50)
    code: str | None = Field(None, min_length=1, max_length=20)
    description: str | None = Field(None, max_length=255)


class RoleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    code: str
    description: str | None
    tenant_id: str
    tenant_name: str | None = None


class SkillCreate(BaseModel):
    name: str
    code: str


class SkillUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    code: str | None = Field(None, min_length=1, max_length=30)


class SkillRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    code: str
    tenant_id: str
    tenant_name: str | None = None


# ──────────────────────────────────────────────────────────────
# Contract
# ──────────────────────────────────────────────────────────────
class ContractCreate(BaseModel):
    # nurse_id is filled by the backend (from the parent nurse), not the client.
    nurse_id: str | None = None
    shifts_per_period: int = Field(default=10, ge=0)
    max_shifts_per_period: int | None = Field(default=None, ge=0)
    min_rest_hours: int = Field(default=11, ge=0)
    max_consecutive_days: int = Field(default=5, ge=1)
    enforce_balanced: bool = True
    enforce_shifts_per_period: bool = True
    enforce_one_shift_per_day: bool = True


class ContractRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    nurse_id: str
    shifts_per_period: int
    max_shifts_per_period: int | None
    min_rest_hours: int
    max_consecutive_days: int
    enforce_balanced: bool
    enforce_shifts_per_period: bool
    enforce_one_shift_per_day: bool


# ──────────────────────────────────────────────────────────────
# Nurse
# ──────────────────────────────────────────────────────────────
class NurseCreate(BaseModel):
    employee_id: str
    first_name: str
    last_name: str
    department: str | None = Field(None, max_length=100)
    is_available: bool = True
    role_ids: list[str] = Field(..., min_length=1)
    skill_ids: list[str] = Field(default_factory=list)
    preferences: dict[str, Any] = Field(default_factory=dict)
    contract: ContractCreate | None = None


class NurseRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    employee_id: str
    first_name: str
    last_name: str
    department: str | None = None
    is_available: bool
    preferences: dict[str, Any]
    role_ids: list[str] = Field(default_factory=list)
    skill_ids: list[str] = Field(default_factory=list)
    contract: ContractRead | None = None
    created_at: datetime
    tenant_id: str
    tenant_name: str | None = None
    user_email: str | None = None


# ──────────────────────────────────────────────────────────────
# Leave
# ──────────────────────────────────────────────────────────────
class LeaveCreate(BaseModel):
    nurse_id: str
    date: date
    description: str = "Leave"


class LeaveRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    nurse_id: str
    date: date
    description: str


# ──────────────────────────────────────────────────────────────
# Nurse Preference
# ──────────────────────────────────────────────────────────────
class NursePreferenceCreate(BaseModel):
    date: date
    shift_template_id: str
    request_type: RequestType = RequestType.LIKE
    priority: int = Field(default=1, ge=1)


class NursePreferenceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    nurse_id: str
    date: date
    shift_template_id: str
    request_type: RequestType
    priority: int


# ──────────────────────────────────────────────────────────────
# Shift / DayGroup
# ──────────────────────────────────────────────────────────────
class DayGroupDayCreate(BaseModel):
    day_number: int = Field(..., ge=1)


class DayGroupCreate(BaseModel):
    name: str
    description: str | None = None
    day_numbers: list[int] = Field(default_factory=list)


DayGroupUpdate = DayGroupCreate


class DayGroupRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    description: str | None
    day_numbers: list[int] = Field(
        default_factory=lambda ctx: [d.day_number for d in ctx.get("days", [])]
    )
    tenant_id: str
    tenant_name: str | None = None


class ShiftTemplateCreate(BaseModel):
    code: str
    name: str
    start_time: time
    end_time: time
    duration_hours: float = 8.0
    color: str | None = None
    day_group_id: str


class ShiftTemplateRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    code: str
    name: str
    start_time: time
    end_time: time
    duration_hours: float
    color: str | None
    day_group_id: str
    # Owning tenant id. RLS already scopes a non-super admin to their own tenant,
    # so the field is meaningful only for super-admin views (where the same
    # shift code can appear across many tenants).
    tenant_id: str
    tenant_name: str | None = None


# ──────────────────────────────────────────────────────────────
# Skill Mix Rule
# ──────────────────────────────────────────────────────────────
class SkillMixRequirementCreate(BaseModel):
    role_id: str | None = None
    skill_id: str | None = None
    count: int = Field(..., ge=0)


class SkillMixRuleCreate(BaseModel):
    name: str
    shift_template_id: str
    priority: int = 0
    requirements: list[SkillMixRequirementCreate] = Field(default_factory=list)


class SkillMixRequirementRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    role_id: str | None
    role_name: str | None = None
    role_code: str | None = None
    skill_id: str | None = None
    skill_name: str | None = None
    skill_code: str | None = None
    count: int


class SkillMixRuleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    shift_template_id: str
    shift_template_name: str | None = None
    shift_template_code: str | None = None
    priority: int
    is_active: bool
    requirements: list[SkillMixRequirementRead]
    tenant_id: str
    tenant_name: str | None = None


# ──────────────────────────────────────────────────────────────
# Shift Sequence Rule
# ──────────────────────────────────────────────────────────────
class ShiftSequenceStepCreate(BaseModel):
    position: int = Field(..., ge=0)
    shift_template_id: str | None = None  # None = day off


class ShiftSequenceRuleCreate(BaseModel):
    name: str
    description: str | None = None
    steps: list[ShiftSequenceStepCreate] = Field(default_factory=list)
    role_ids: list[str] = Field(default_factory=list)  # empty = all roles


class ShiftSequenceStepRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    position: int
    shift_template_id: str | None
    shift_template_name: str | None = None
    shift_template_code: str | None = None


class ShiftSequenceRuleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    description: str | None
    is_active: bool
    steps: list[ShiftSequenceStepRead]
    role_ids: list[str] = Field(default_factory=list)
    role_names: list[str] = Field(default_factory=list)
    role_codes: list[str] = Field(default_factory=list)
    tenant_id: str
    tenant_name: str | None = None


# ──────────────────────────────────────────────────────────────
# Schedule
# ──────────────────────────────────────────────────────────────
class SolverConfig(BaseModel):
    timeout_seconds: int = Field(default=120, ge=1, le=3600)
    num_workers: int = Field(
        default_factory=lambda: min(8, os.cpu_count() or 1), ge=1, le=64
    )


class ScheduleGenerateRequest(BaseModel):
    period_start: date
    period_days: int = Field(..., ge=1, le=62)
    # Optional participant filter (nurse ids); null/empty = all available nurses
    nurse_ids: list[str] | None = None
    # Optional rule filters; null/empty = all active rules
    skill_mix_rule_ids: list[str] | None = None
    shift_sequence_rule_ids: list[str] | None = None
    # Target tenant for super-admin acting on behalf of a tenant. Tenant
    # users MUST omit this (it must equal their own tenant if present).
    tenant_id: str | None = None
    solver_config: SolverConfig = Field(default_factory=SolverConfig)
    long_run: bool = False


class ScheduleGeneratePrecheckRequest(BaseModel):
    # Empty filters have the same "all available records" meaning as generate.
    nurse_ids: list[str] | None = None
    skill_mix_rule_ids: list[str] | None = None
    tenant_id: str | None = None


class ScheduleGeneratePrecheckResponse(BaseModel):
    nurse_count: int
    demand_rule_count: int
    contract_target_count: int
    eligible_nurse_count: int
    can_generate: bool
    reasons: list[str] = Field(default_factory=list)


class ScheduleRequestRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    period_start: date
    period_days: int
    display_id: str
    status: ScheduleStatus
    task_id: str | None
    requested_by: str | None
    requested_by_name: str | None = None
    error_message: str | None
    outcome: SolverOutcome | None
    stats: dict[str, Any]
    solver_config: dict[str, Any]
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    tenant_id: str
    tenant_name: str | None = None
    latest_version: int | None = None
    active_schedule_id: str | None = None
    active_version: int | None = None
    available_versions: list[int] = Field(default_factory=list)


class MyScheduleRequestRead(ScheduleRequestRead):
    assignment_count: int


class AssignmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    nurse_id: str
    nurse_name: str | None = None
    role_id: str | None
    date: date
    shift_template_id: str
    shift_template_code: str | None = None
    satisfied_preference: bool | None


class ShiftTemplateBrief(BaseModel):
    """Minimal shift-template info for legends / display."""
    id: str
    code: str
    name: str
    start_time: str | None = None
    end_time: str | None = None
    day_group_name: str | None = None
    day_numbers: list[int] = []


class ScheduleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    request_id: str
    period_start: date
    period_days: int
    version: int
    outcome: SolverOutcome
    objective_value: int | None
    solve_time_seconds: float | None
    summary: dict[str, Any]
    assignments: list[AssignmentRead]
    shift_templates: list[ShiftTemplateBrief] = []


class ScheduleVersionRead(BaseModel):
    """Immutable schedule version metadata shown in the history selector."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    version: int
    outcome: SolverOutcome
    summary: dict[str, Any]
    created_at: datetime
    assignment_count: int


class ScheduleActiveVersionRequest(BaseModel):
    version: int = Field(ge=1)


class ScheduleActiveVersionRead(BaseModel):
    request_id: str
    active_schedule_id: str
    active_version: int


# ──────────────────────────────────────────────────────────────
# Schedule editing (B3-10)
# ──────────────────────────────────────────────────────────────
class ScheduleEditAdd(BaseModel):
    action: Literal["add"]
    nurse_id: str
    date: date
    shift_template_id: str
    role_id: str | None = None


class ScheduleEditReplace(BaseModel):
    action: Literal["replace"]
    nurse_id: str
    date: date
    shift_template_id: str
    new_nurse_id: str
    new_role_id: str | None = None


class ScheduleEditRemove(BaseModel):
    action: Literal["remove"]
    nurse_id: str
    date: date
    shift_template_id: str


ScheduleEditOperation = Annotated[
    ScheduleEditAdd | ScheduleEditReplace | ScheduleEditRemove,
    Field(discriminator="action"),
]


class ScheduleEditRequest(BaseModel):
    """Atomic batch edit on a completed schedule.

    base_version must match the current Schedule.version for optimistic
    concurrency; the server bumps version += 1 on success.
    """

    base_version: int
    operations: list[ScheduleEditOperation] = Field(..., min_length=1)
    # Required when soft constraints (skill mix ratio) are violated;
    # hard-constraint violations always reject regardless of this field.
    override_reason: str | None = Field(None, max_length=500)

class ScheduleEditWarning(BaseModel):
    """A non-blocking issue detected during edit validation."""

    code: str
    message: str
    field: str | None = None
    index: int | None = None


class ScheduleEditResponse(BaseModel):
    """Response after a successful schedule edit."""

    version: int
    warnings: list[ScheduleEditWarning] = []
    summary: dict[str, Any] = {}


class ScheduleResolveRequest(BaseModel):
    """B3-11: local re-solve keeping pinned assignments fixed.

    When ``pin_all`` is true every existing assignment is locked and the
    solver only fills empty cells. Otherwise ``pin_nurse_ids`` and/or
    ``pin_dates`` narrow which cells stay unchanged.
    """

    base_version: int
    pin_all: bool = False
    pin_nurse_ids: list[str] = Field(default_factory=list, max_length=200)
    pin_dates: list[date] = Field(default_factory=list, max_length=62)


class ScheduleResolveDiffItem(BaseModel):
    """One changed assignment between the old and re-solved schedule."""

    action: str  # "added" | "removed" | "changed"
    nurse_id: str
    date: date
    old_shift_template_id: str | None = None
    new_shift_template_id: str | None = None


class ScheduleResolveAssignment(BaseModel):
    """A solver-proposed assignment returned by a resolve preview."""

    nurse_id: str
    role_id: str | None = None
    date: date
    shift_template_id: str


class ScheduleResolveResponse(BaseModel):
    """An unsaved local re-solve preview."""

    base_version: int
    assignments: list[ScheduleResolveAssignment] = []
    diff: list[ScheduleResolveDiffItem] = []
    summary: dict[str, Any] = {}


class ScheduleResolveCommitRequest(BaseModel):
    """Persist a previously previewed local re-solve as a new version."""

    base_version: int
    assignments: list[ScheduleResolveAssignment] = Field(default_factory=list)
    override_reason: str | None = Field(None, max_length=500)
