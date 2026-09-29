"""Shift templates and day groups — defines the shift structure.

A ShiftTemplate is a recurring shift type (Early, Late, Night).
A DayGroup defines which days a shift applies to (Mon-Fri, weekends, etc.).
This mirrors roster-wizard's Shift / DayGroup / DayGroupDay model.
"""

from __future__ import annotations

import uuid
from datetime import datetime, time

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Time,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import TenantBase


class DayGroup(TenantBase):
    """A named group of day numbers (e.g., "Mon-Fri" = [1,2,3,4,5]).

    Day numbers are 1-7 (Mon=1) or 1-N for custom roster cycles.
    """

    __tablename__ = "day_groups"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    # Human description
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)

    days: Mapped[list[DayGroupDay]] = relationship(
        back_populates="day_group", cascade="all, delete-orphan", lazy="selectin"
    )

    def __repr__(self) -> str:
        return f"<DayGroup {self.name}>"


class DayGroupDay(TenantBase):
    """Association: DayGroup contains a set of day numbers."""

    __tablename__ = "day_group_days"
    __table_args__ = (UniqueConstraint("day_group_id", "day_number", name="uq_daygroup_day"),)

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    day_group_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("day_groups.id", ondelete="CASCADE"), nullable=False, index=True
    )
    day_number: Mapped[int] = mapped_column(Integer, nullable=False)

    day_group: Mapped[DayGroup] = relationship(back_populates="days")


class ShiftTemplate(TenantBase):
    """A shift type definition (e.g., "Early 07:00-15:00").

    Linked to a DayGroup specifying which days this shift runs.
    """

    __tablename__ = "shift_templates"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    code: Mapped[str] = mapped_column(String(20), nullable=False)  # "E", "L", "N"
    name: Mapped[str] = mapped_column(String(100), nullable=False)  # "Early"
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)
    # Duration in hours (for rest-calculation between shifts)
    duration_hours: Mapped[float] = mapped_column(nullable=False, default=8.0)
    # Color for UI display
    color: Mapped[str | None] = mapped_column(String(20), nullable=True)

    day_group_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("day_groups.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    day_group: Mapped[DayGroup] = relationship(lazy="selectin")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<Shift {self.code} {self.start_time}-{self.end_time}>"
