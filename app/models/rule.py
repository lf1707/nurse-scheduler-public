"""Scheduling rules — skill mix and shift sequence constraints.

Mirrors roster-wizard's SkillMixRule / SkillMixRuleRole / ShiftSequence models
but adapted to multi-tenant SQLAlchemy.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import TenantBase


# ──────────────────────────────────────────────────────────────
# Skill Mix Rule — required role counts for a shift
# ──────────────────────────────────────────────────────────────
class SkillMixRule(TenantBase):
    """A rule defining required role counts for a shift template.

    Multiple rules can apply to the same shift (one enforced at a time,
    chosen by the solver). e.g., ICU Day shift needs >= 2 RN + 1 Charge.
    """

    __tablename__ = "skill_mix_rules"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # Which shift this rule applies to
    shift_template_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("shift_templates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Priority/ordering (lower = higher priority). For tie-breaking when
    # multiple rules could apply. Solver picks one per timeslot via intermediate BoolVar.
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)

    requirements: Mapped[list[SkillMixRequirement]] = relationship(
        back_populates="rule", cascade="all, delete-orphan", lazy="selectin"
    )

    def __repr__(self) -> str:
        return f"<SkillMixRule {self.name}>"


class SkillMixRequirement(TenantBase):
    """One staffing requirement within a SkillMixRule.

    ``role_id=None`` means any role (a total headcount requirement).
    ``skill_id`` optionally narrows either form: ``count`` nurses must also
    hold the skill.
    """

    __tablename__ = "skill_mix_requirements"
    __table_args__ = (
        UniqueConstraint(
            "skill_mix_rule_id", "role_id", "skill_id", name="uq_skillmix_role_skill"
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    skill_mix_rule_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("skill_mix_rules.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("roles.id", ondelete="CASCADE"), nullable=True
    )
    skill_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("skills.id", ondelete="CASCADE"), nullable=True
    )
    count: Mapped[int] = mapped_column(Integer, nullable=False)  # required count

    rule: Mapped[SkillMixRule] = relationship(back_populates="requirements")


# ──────────────────────────────────────────────────────────────
# Shift Sequence Rule — forbidden shift patterns
# ──────────────────────────────────────────────────────────────
class ShiftSequenceRule(TenantBase):
    """A forbidden shift sequence (e.g., "Night → Early next day").

    Defines a pattern of shifts over consecutive days that must not occur.
    `None` in a step means day off.

    roster-wizard models this via ShiftSequence + ShiftSequenceShift with positions.
    Here we store the sequence as ordered steps.
    """

    __tablename__ = "shift_sequence_rules"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)

    # Which roles this rule applies to (None = all roles)
    # Stored as a many-to-many for flexibility
    steps: Mapped[list[ShiftSequenceStep]] = relationship(
        back_populates="rule",
        cascade="all, delete-orphan",
        order_by="ShiftSequenceStep.position",
        lazy="selectin",
    )
    # Optional role restriction via association table
    role_restrictions: Mapped[list[ShiftSequenceRoleRestriction]] = relationship(
        back_populates="rule", cascade="all, delete-orphan", lazy="selectin"
    )

    def __repr__(self) -> str:
        return f"<ShiftSequenceRule {self.name}>"


class ShiftSequenceStep(TenantBase):
    """One step in a forbidden shift sequence.

    position = day index (0-based) in the sequence.
    shift_template_id = the shift on that day (None = day off).
    """

    __tablename__ = "shift_sequence_steps"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    rule_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("shift_sequence_rules.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    # NULL means "day off" (no shift that day)
    shift_template_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("shift_templates.id", ondelete="CASCADE"), nullable=True
    )

    rule: Mapped[ShiftSequenceRule] = relationship(back_populates="steps")

    def __repr__(self) -> str:
        return f"<SeqStep pos={self.position} shift={self.shift_template_id}>"


class ShiftSequenceRoleRestriction(TenantBase):
    """Optional: restrict a sequence rule to specific roles."""

    __tablename__ = "shift_sequence_role_restrictions"
    __table_args__ = (
        UniqueConstraint("rule_id", "role_id", name="uq_seqrule_role"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    rule_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("shift_sequence_rules.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("roles.id", ondelete="CASCADE"), nullable=False
    )

    rule: Mapped[ShiftSequenceRule] = relationship(back_populates="role_restrictions")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
