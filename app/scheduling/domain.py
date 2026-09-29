"""In-memory domain data structures loaded from DB for the CP-SAT solver.

These are plain dataclasses (not SQLAlchemy models) — the solver works purely
in-memory for performance. The loader.py fetches from DB and builds these.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, time
from typing import Any

SOFT_CONSTRAINT_WEIGHT = 1_000


@dataclass(frozen=True)
class RoleData:
    id: str
    code: str
    name: str


@dataclass(frozen=True)
class ShiftTemplateData:
    id: str
    code: str
    name: str
    start_time: time
    end_time: time
    duration_hours: float
    day_numbers: frozenset[int]  # which day-of-cycle this shift runs on


@dataclass(frozen=True)
class ContractData:
    nurse_id: str
    shifts_per_period: int
    max_shifts_per_period: int | None
    min_rest_hours: int
    max_consecutive_days: int
    enforce_balanced: bool
    enforce_shifts_per_period: bool
    enforce_one_shift_per_day: bool


@dataclass
class NurseData:
    """In-memory representation of a nurse for the solver."""

    id: str
    employee_id: str
    first_name: str
    last_name: str
    is_available: bool
    role_ids: list[str] = field(default_factory=list)
    skill_ids: list[str] = field(default_factory=list)
    contract: ContractData | None = None
    # leave dates within the scheduling period
    leave_dates: frozenset[date] = field(default_factory=frozenset)


@dataclass(frozen=True)
class SkillMixRequirementData:
    """A staffing requirement; ``role_id=None`` means any role."""

    role_id: str | None
    count: int
    skill_id: str | None = None


@dataclass(frozen=True)
class SkillMixRuleData:
    """One skill-mix rule for a shift template."""

    id: str
    name: str
    shift_template_id: str
    priority: int
    requirements: tuple[SkillMixRequirementData, ...]


@dataclass(frozen=True)
class ShiftSequenceStepData:
    """One step in a forbidden shift sequence."""

    position: int
    shift_template_id: str | None  # None = day off


@dataclass(frozen=True)
class ShiftSequenceRuleData:
    """A forbidden shift sequence (e.g., Night → Early)."""

    id: str
    name: str
    steps: tuple[ShiftSequenceStepData, ...]
    # applies_to_role_ids: empty = all roles
    applies_to_role_ids: frozenset[str]


@dataclass(frozen=True)
class NursePreferenceData:
    """Nurse preference (like/avoid) for a shift on a date."""

    nurse_id: str
    date: date
    shift_template_id: str
    request_type: str  # "like" or "avoid"
    priority: int


@dataclass(frozen=True)
class PreviousAssignmentData:
    """An assignment from the previous roster period (for continuity)."""

    nurse_id: str
    role_id: str
    date: date
    shift_template_id: str


@dataclass
class AssignmentResult:
    """One cell of the solved schedule."""

    nurse_id: str
    role_id: str
    date: date
    shift_template_id: str
    satisfied_preference: bool | None = None


@dataclass
class SolveResult:
    """Output of a successful solver run."""

    assignments: list[AssignmentResult]
    outcome: str  # "optimal" | "feasible"
    objective_value: int
    solve_time_seconds: float
    num_nurses: int
    num_conflicts: int = 0
    num_booleans: int = 0
    preference_stats: dict[str, Any] = field(default_factory=dict)


@dataclass
class DomainData:
    """All in-memory data needed to build the CP-SAT model."""

    tenant_id: str
    period_start: date
    period_days: int
    nurses: list[NurseData]
    roles: dict[str, RoleData]  # id -> RoleData
    shift_templates: list[ShiftTemplateData]
    skill_mix_rules: list[SkillMixRuleData]
    shift_sequence_rules: list[ShiftSequenceRuleData]
    preferences: list[NursePreferenceData]
    previous_assignments: list[PreviousAssignmentData]
    # B3-11: assignments that must not change during a local re-solve.
    # Populated when re-solving an existing schedule; empty for a fresh solve.
    pinned_assignments: list[AssignmentResult] = field(default_factory=list)
    # Derived helpers (populated by loader)
    dates: list[date] = field(default_factory=list)
    extended_dates: list[date] = field(default_factory=list)  # includes prev period for continuity
    skills: dict[str, str] = field(default_factory=dict)  # id -> "name（code）"


def make_uuid() -> str:
    """Generate a UUID string (used in tests + fixtures)."""
    return str(uuid.uuid4())
