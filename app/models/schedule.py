"""Schedule and Assignment — generated roster output.

ScheduleRequest = a request to generate a schedule (queued to Celery).
Schedule = the solved roster (one per successful generation).
Assignment = a nurse assigned to a shift on a date (the actual roster cells).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import TenantBase
from app.models.enums import ScheduleStatus, SolverOutcome


class ScheduleRequest(TenantBase):
    """A request to generate a schedule for a period.

    Lifecycle: PENDING → RUNNING → COMPLETED | FAILED | CANCELLED
    Celery task id tracked for cancellation.
    """

    __tablename__ = "schedule_requests"
    __table_args__ = (
        UniqueConstraint(
            "request_date",
            "daily_sequence",
            name="uq_schedule_requests_date_seq",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    period_start: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    period_days: Mapped[int] = mapped_column(Integer, nullable=False)
    request_date: Mapped[date] = mapped_column(Date, nullable=False)
    daily_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[ScheduleStatus] = mapped_column(
        nullable=False, default=ScheduleStatus.PENDING, index=True
    )
    # Solver config snapshot (timeout, weights, workers)
    solver_config: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    # Optional participant filter (nurse ids); null/empty = all available nurses
    nurse_ids: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    # Optional rule filters; null/empty = all active rules
    skill_mix_rule_ids: Mapped[list[str] | None] = mapped_column(
        JSON, nullable=True
    )
    shift_sequence_rule_ids: Mapped[list[str] | None] = mapped_column(
        JSON, nullable=True
    )
    # Celery task id
    task_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    # Who requested it
    requested_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Error message if failed
    error_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    # Solver outcome + stats (objective value, wall time, num conflicts)
    outcome: Mapped[SolverOutcome | None] = mapped_column(nullable=True)
    stats: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # The resulting schedule (one-to-one, set when COMPLETED)
    schedule: Mapped[Schedule | None] = relationship(
        back_populates="request", foreign_keys="[Schedule.request_id]",
        uselist=False, lazy="noload",
    )
    active_schedule_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("schedules.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
        index=True,
    )

    def __repr__(self) -> str:
        return f"<ScheduleRequest {self.id[:8]} {self.status.value}>"

    @property
    def display_id(self) -> str:
        return f"{self.request_date:%Y%m%d}-{self.daily_sequence:04d}"


class Schedule(TenantBase):
    """A completed schedule for a period (output of a ScheduleRequest)."""

    __tablename__ = "schedules"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    request_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("schedule_requests.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    period_start: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    period_days: Mapped[int] = mapped_column(Integer, nullable=False)
    # Solver outcome + objective
    outcome: Mapped[SolverOutcome] = mapped_column(nullable=False)
    objective_value: Mapped[int | None] = mapped_column(Integer, nullable=True)
    solve_time_seconds: Mapped[float | None] = mapped_column(nullable=True)
    # Summary stats (nurses_scheduled, total_shifts, preference_satisfaction_rate)
    summary: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    # Immutable version; schedule_requests.active_schedule_id marks the
    # version shown to nurses and used as the default operational roster.
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # Soft delete keeps immutable snapshots and version numbers intact while
    # removing obsolete edit/re-solve versions from operational views.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    request: Mapped[ScheduleRequest] = relationship(
        back_populates="schedule", foreign_keys=[request_id]
    )
    assignments: Mapped[list[Assignment]] = relationship(
        back_populates="schedule", cascade="all, delete-orphan", lazy="selectin"
    )

    def __repr__(self) -> str:
        return f"<Schedule {self.id[:8]} {self.period_start}+{self.period_days}d>"


class Assignment(TenantBase):
    """One nurse assigned to one shift on one date (a roster cell)."""

    __tablename__ = "assignments"
    __table_args__ = (
        # A nurse can have at most one assignment per (date, shift) — but
        # typically one per date. Soft-enforced by solver, hard constraint at DB
        # for (nurse, date, shift_template).
        UniqueConstraint(
            "schedule_id", "nurse_id", "date", "shift_template_id",
            name="uq_assignment_cell",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    schedule_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("schedules.id", ondelete="CASCADE"), nullable=False, index=True
    )
    nurse_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("nurses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("roles.id", ondelete="SET NULL"), nullable=True, index=True
    )
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    shift_template_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("shift_templates.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # Whether this assignment satisfied a nurse preference (for reporting)
    satisfied_preference: Mapped[bool | None] = mapped_column(nullable=True)

    schedule: Mapped[Schedule] = relationship(back_populates="assignments")

    def __repr__(self) -> str:
        return f"<Assignment nurse={self.nurse_id[:8]} {self.date} {self.shift_template_id[:8]}>"
