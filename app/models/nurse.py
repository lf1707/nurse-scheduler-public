"""Nurse domain models — nurses, clinical roles, skills, contracts.

Nurse is the central entity. A nurse belongs to exactly one tenant.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Table,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import TenantBase

if TYPE_CHECKING:
    from app.models.user import User


# ──────────────────────────────────────────────────────────────
# Clinical Role (e.g., "RN", "EN", "Charge Nurse", "CNA")
# ──────────────────────────────────────────────────────────────
class Role(TenantBase):
    """Clinical role/designation. Per-tenant (each hospital defines own roles)."""

    __tablename__ = "roles"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    code: Mapped[str] = mapped_column(String(20), nullable=False)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)

    def __repr__(self) -> str:
        return f"<Role {self.code}>"


# ──────────────────────────────────────────────────────────────
# Skill (e.g., "IV", "Ventilator", "Triage", "Pediatric")
# ──────────────────────────────────────────────────────────────
class Skill(TenantBase):
    """Certifiable skill. Nurses can have many skills."""

    __tablename__ = "skills"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    code: Mapped[str] = mapped_column(String(30), nullable=False)

    def __repr__(self) -> str:
        return f"<Skill {self.code}>"


# ──────────────────────────────────────────────────────────────
# Nurse ↔ Role many-to-many
# ──────────────────────────────────────────────────────────────
nurse_roles = Table(
    "nurse_roles",
    TenantBase.metadata,
    Column("nurse_id", String(36), ForeignKey("nurses.id", ondelete="CASCADE"), primary_key=True),
    Column("role_id", String(36), ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
)

# ──────────────────────────────────────────────────────────────
# Nurse ↔ Skill many-to-many
# ──────────────────────────────────────────────────────────────
nurse_skills = Table(
    "nurse_skills",
    TenantBase.metadata,
    Column("nurse_id", String(36), ForeignKey("nurses.id", ondelete="CASCADE"), primary_key=True),
    Column("skill_id", String(36), ForeignKey("skills.id", ondelete="CASCADE"), primary_key=True),
)


# ──────────────────────────────────────────────────────────────
# Contract — per-nurse work rules
# ──────────────────────────────────────────────────────────────
class Contract(TenantBase):
    """Work contract terms for a nurse."""

    __tablename__ = "contracts"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    nurse_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("nurses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Target shifts in a roster period (e.g., 12 shifts per 14-day cycle)
    shifts_per_period: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    # Hard cap on shifts per period (None = use target exactly)
    max_shifts_per_period: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Minimum rest hours between consecutive shifts
    min_rest_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=11)
    # Max consecutive working days
    max_consecutive_days: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    # Enforce balanced shifts between first/second half of period
    enforce_balanced: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Enforce exactly shifts_per_period (vs. allow <= max)
    enforce_shifts_per_period: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    # Enforce at most one shift per day
    enforce_one_shift_per_day: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )

    nurse: Mapped[Nurse] = relationship(back_populates="contract", uselist=False)


# ──────────────────────────────────────────────────────────────
# Nurse — central entity
# ──────────────────────────────────────────────────────────────
class Nurse(TenantBase):
    """A nurse in a tenant (hospital).

    Linked to a User (optional — nurses may not log in).
    Has roles, skills, contract, preferences.
    """

    __tablename__ = "nurses"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    # Hospital employee ID (internal code)
    employee_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    # Department/ward grouping (e.g. 内科, 外科, ICU)
    department: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    # Available for scheduling (set false for leave of absence)
    is_available: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # JSONB: preferred_shifts, avoid_dates, preferred_partners, etc.
    preferences: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )

    # Optional hire date / FTE fraction for future fairness tuning
    hire_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    fte_fraction: Mapped[float | None] = mapped_column(nullable=True)  # 0.0-1.0

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    roles: Mapped[list[Role]] = relationship(secondary=nurse_roles, lazy="selectin")
    skills: Mapped[list[Skill]] = relationship(secondary=nurse_skills, lazy="selectin")
    contract: Mapped[Contract | None] = relationship(
        back_populates="nurse", uselist=False, lazy="selectin"
    )
    user: Mapped[User | None] = relationship(
        "User", back_populates="nurse", foreign_keys="User.nurse_id", uselist=False
    )

    def __repr__(self) -> str:
        return f"<Nurse {self.last_name}, {self.first_name}>"


# ──────────────────────────────────────────────────────────────
# Leave — nurse unavailable on a specific date
# ──────────────────────────────────────────────────────────────
class Leave(TenantBase):
    """A nurse's leave on a date (excludes them from any shift that day)."""

    __tablename__ = "leaves"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    nurse_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("nurses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    # Description: "vacation", "sick", "training", etc.
    description: Mapped[str] = mapped_column(String(100), nullable=False, default="Leave")

    def __repr__(self) -> str:
        return f"<Leave {self.nurse_id} {self.date}>"


# ──────────────────────────────────────────────────────────────
# ScheduleRequest — nurse preference for a shift on a date
# ──────────────────────────────────────────────────────────────
from app.models.enums import RequestType  # noqa: E402


class NursePreference(TenantBase):
    """Nurse preference: like/avoid a specific shift on a specific date."""

    __tablename__ = "nurse_preferences"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    nurse_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("nurses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    shift_template_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("shift_templates.id", ondelete="CASCADE"), nullable=False, index=True
    )
    request_type: Mapped[RequestType] = mapped_column(nullable=False, default=RequestType.LIKE)
    # Priority weight (higher = stronger preference). Default 1.
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    def __repr__(self) -> str:
        return f"<NursePref {self.nurse_id} {self.date} {self.request_type.value}>"
