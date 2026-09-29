"""Unit tests for the CP-SAT scheduling engine — synthetic data, no DB."""

from __future__ import annotations

import datetime
from typing import Any

import pytest

from app.scheduling.domain import (
    ContractData,
    DomainData,
    NurseData,
    NursePreferenceData,
    RoleData,
    ShiftTemplateData,
    SkillMixRequirementData,
    SkillMixRuleData,
    make_uuid,
)
from app.scheduling.engine import RosterCPModel, SolverConfig
from app.scheduling.exceptions import InfeasibleError


# ──────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────
def make_role(code: str = "RN") -> RoleData:
    rid = make_uuid()
    return RoleData(id=rid, code=code, name=f"Role {code}")


def make_shift(
    code: str = "E",
    day_numbers: frozenset[int] | None = None,
) -> ShiftTemplateData:
    sid = make_uuid()
    if day_numbers is None:
        day_numbers = frozenset(range(1, 8))  # all week
    return ShiftTemplateData(
        id=sid,
        code=code,
        name=f"Shift {code}",
        start_time=datetime.time(7, 0),
        end_time=datetime.time(15, 0),
        duration_hours=8.0,
        day_numbers=day_numbers,
    )


def make_nurse(
    role_ids: list[str],
    contract: ContractData | None = None,
    leave_dates: frozenset[datetime.date] = frozenset(),
) -> NurseData:
    return NurseData(
        id=make_uuid(),
        employee_id="N001",
        first_name="Jane",
        last_name="Doe",
        is_available=True,
        role_ids=role_ids,
        skill_ids=[],
        contract=contract,
        leave_dates=leave_dates,
    )


def make_contract(shifts_per_period: int = 5, nurse_id: str = "") -> ContractData:
    return ContractData(
        nurse_id=nurse_id,
        shifts_per_period=shifts_per_period,
        max_shifts_per_period=None,
        min_rest_hours=11,
        max_consecutive_days=5,
        enforce_balanced=True,
        enforce_shifts_per_period=True,
        enforce_one_shift_per_day=True,
    )


# ──────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────
def test_solve_simple_schedule() -> None:
    """5 nurses, 1 role, 1 shift template, 7 days. Each nurse works ~1 shift."""
    role = make_role("RN")
    shift = make_shift("E")
    period_start = datetime.date(2026, 9, 1)
    period_days = 7

    nurses = []
    for _ in range(5):
        contract = make_contract(shifts_per_period=1)
        n = make_nurse(role_ids=[role.id])
        contract = ContractData(
            nurse_id=n.id,
            shifts_per_period=contract.shifts_per_period,
            max_shifts_per_period=contract.max_shifts_per_period,
            min_rest_hours=contract.min_rest_hours,
            max_consecutive_days=contract.max_consecutive_days,
            enforce_balanced=contract.enforce_balanced,
            enforce_shifts_per_period=contract.enforce_shifts_per_period,
            enforce_one_shift_per_day=contract.enforce_one_shift_per_day,
        )
        n.contract = contract
        nurses.append(n)

    domain = DomainData(
        tenant_id="t1",
        period_start=period_start,
        period_days=period_days,
        nurses=nurses,
        roles={role.id: role},
        shift_templates=[shift],
        skill_mix_rules=[],
        shift_sequence_rules=[],
        preferences=[],
        previous_assignments=[],
        dates=[period_start + datetime.timedelta(days=i) for i in range(period_days)],
        extended_dates=[
            (period_start - datetime.timedelta(days=period_days) + datetime.timedelta(days=i))
            for i in range(period_days * 2)
        ],
    )

    engine = RosterCPModel(domain, SolverConfig(timeout_seconds=10, num_workers=4))
    result = engine.solve()

    assert result.outcome in ("optimal", "feasible")
    assert len(result.assignments) == 5  # 5 nurses × 1 shift each
    # Each nurse assigned exactly once
    nurse_counts: dict[str, int] = {}
    for a in result.assignments:
        nurse_counts[a.nurse_id] = nurse_counts.get(a.nurse_id, 0) + 1
    assert all(c == 1 for c in nurse_counts.values())
    assert len(nurse_counts) == 5


def test_leave_excludes_nurse() -> None:
    """A nurse on leave cannot be assigned that day."""
    role = make_role("RN")
    shift = make_shift("E")
    period_start = datetime.date(2026, 9, 1)
    period_days = 3

    leave_date = period_start  # leave on day 1
    contract = make_contract(shifts_per_period=2)
    nurse = make_nurse(role_ids=[role.id], leave_dates=frozenset([leave_date]))
    nurse.contract = ContractData(
        nurse_id=nurse.id,
        shifts_per_period=contract.shifts_per_period,
        max_shifts_per_period=contract.max_shifts_per_period,
        min_rest_hours=contract.min_rest_hours,
        max_consecutive_days=contract.max_consecutive_days,
        enforce_balanced=contract.enforce_balanced,
        enforce_shifts_per_period=contract.enforce_shifts_per_period,
        enforce_one_shift_per_day=contract.enforce_one_shift_per_day,
    )


    domain = DomainData(
        tenant_id="t1",
        period_start=period_start,
        period_days=period_days,
        nurses=[nurse],
        roles={role.id: role},
        shift_templates=[shift],
        skill_mix_rules=[],
        shift_sequence_rules=[],
        preferences=[],
        previous_assignments=[],
        dates=[period_start + datetime.timedelta(days=i) for i in range(period_days)],
        extended_dates=[
            (period_start - datetime.timedelta(days=period_days) + datetime.timedelta(days=i))
            for i in range(period_days * 2)
        ],
    )

    engine = RosterCPModel(domain, SolverConfig(timeout_seconds=10, num_workers=4))
    result = engine.solve()

    # No assignment on the leave date
    leave_assignments = [a for a in result.assignments if a.date == leave_date]
    assert leave_assignments == []


def test_skill_mix_enforced() -> None:
    """Skill mix rule requires 2 RNs per shift — verify count."""
    role = make_role("RN")
    shift = make_shift("E")
    period_start = datetime.date(2026, 9, 1)
    period_days = 2

    # 4 nurses, each targets 1 shift. Skill mix needs 2/shift × 2 days = 4 total.
    nurses = []
    for _ in range(4):
        n = make_nurse(role_ids=[role.id])
        n.contract = ContractData(
            nurse_id=n.id,
            shifts_per_period=1,
            max_shifts_per_period=None,
            min_rest_hours=11,
            max_consecutive_days=5,
            enforce_balanced=False,
            enforce_shifts_per_period=True,
            enforce_one_shift_per_day=True,
        )
        nurses.append(n)

    sm_rule = SkillMixRuleData(
        id=make_uuid(),
        name="2 RNs",
        shift_template_id=shift.id,
        priority=0,
        requirements=(SkillMixRequirementData(role_id=role.id, count=2),),
    )

    domain = DomainData(
        tenant_id="t1",
        period_start=period_start,
        period_days=period_days,
        nurses=nurses,
        roles={role.id: role},
        shift_templates=[shift],
        skill_mix_rules=[sm_rule],
        shift_sequence_rules=[],
        preferences=[],
        previous_assignments=[],
        dates=[period_start + datetime.timedelta(days=i) for i in range(period_days)],
        extended_dates=[
            (period_start - datetime.timedelta(days=period_days) + datetime.timedelta(days=i))
            for i in range(period_days * 2)
        ],
    )

    engine = RosterCPModel(domain, SolverConfig(timeout_seconds=10, num_workers=4))
    result = engine.solve()

    # Each timeslot should have exactly 2 nurses
    from collections import defaultdict

    slot_counts: dict[tuple[datetime.date, str], int] = defaultdict(int)
    for a in result.assignments:
        slot_counts[(a.date, a.shift_template_id)] += 1
    assert all(c == 2 for c in slot_counts.values()), (
        f"Expected 2 per slot, got {dict(slot_counts)}"
    )


def test_skill_mix_any_role_accepts_nurses_across_roles() -> None:
    """An any-role requirement counts nurses regardless of their concrete role."""
    rn_role = make_role("RN")
    lv_role = make_role("LV")
    shift = make_shift("E")
    period_start = datetime.date(2026, 9, 1)

    nurses = [
        make_nurse(role_ids=[rn_role.id]),
        make_nurse(role_ids=[lv_role.id]),
    ]
    for nurse in nurses:
        nurse.contract = make_contract(shifts_per_period=1, nurse_id=nurse.id)

    sm_rule = SkillMixRuleData(
        id=make_uuid(),
        name="Any role",
        shift_template_id=shift.id,
        priority=0,
        requirements=(SkillMixRequirementData(role_id=None, count=2),),
    )
    domain = DomainData(
        tenant_id="t1",
        period_start=period_start,
        period_days=1,
        nurses=nurses,
        roles={rn_role.id: rn_role, lv_role.id: lv_role},
        shift_templates=[shift],
        skill_mix_rules=[sm_rule],
        shift_sequence_rules=[],
        preferences=[],
        previous_assignments=[],
        dates=[period_start],
        extended_dates=[period_start],
    )

    engine = RosterCPModel(domain, SolverConfig(timeout_seconds=10, num_workers=4))
    result = engine.solve()

    assert result.outcome in ("optimal", "feasible")
    assert {assignment.nurse_id for assignment in result.assignments} == {
        nurses[0].id,
        nurses[1].id,
    }


def test_skill_mix_skill_filter() -> None:
    """Skill-mix requirement with a skill filter only counts skilled nurses."""
    role = make_role("RN")
    shift = make_shift("E")
    skill_id = make_uuid()
    period_start = datetime.date(2026, 9, 1)
    period_days = 2

    # 6 RNs: 4 hold the skill, 2 do not. Rule requires 2 skilled RNs per shift;
    # with 2 days that needs 4 skilled-nurse-slots, matching 4 skilled × 1 shift.
    nurses = []
    for i in range(6):
        n = make_nurse(role_ids=[role.id])
        n.skill_ids = [skill_id] if i < 4 else []
        skilled = i < 4
        n.contract = ContractData(
            nurse_id=n.id,
            shifts_per_period=1 if skilled else 0,
            max_shifts_per_period=None,
            min_rest_hours=11,
            max_consecutive_days=5,
            enforce_balanced=False,
            enforce_shifts_per_period=True,
            enforce_one_shift_per_day=True,
        )
        nurses.append(n)

    sm_rule = SkillMixRuleData(
        id=make_uuid(),
        name="2 skilled RNs",
        shift_template_id=shift.id,
        priority=0,
        requirements=(
            SkillMixRequirementData(role_id=role.id, skill_id=skill_id, count=2),
        ),
    )

    domain = DomainData(
        tenant_id="t1",
        period_start=period_start,
        period_days=period_days,
        nurses=nurses,
        roles={role.id: role},
        shift_templates=[shift],
        skill_mix_rules=[sm_rule],
        shift_sequence_rules=[],
        preferences=[],
        previous_assignments=[],
        dates=[period_start + datetime.timedelta(days=i) for i in range(period_days)],
        extended_dates=[
            (period_start - datetime.timedelta(days=period_days) + datetime.timedelta(days=i))
            for i in range(period_days * 2)
        ],
    )

    engine = RosterCPModel(domain, SolverConfig(timeout_seconds=10, num_workers=4))
    result = engine.solve()

    # Only the skilled nurses should be assigned.
    skilled_nurse_ids = {nurses[i].id for i in range(4)}
    assigned = {a.nurse_id for a in result.assignments}
    assert assigned.issubset(skilled_nurse_ids), (
        f"Unskilled nurses assigned: {assigned - skilled_nurse_ids}"
    )
    # Each slot gets exactly 2 (the skilled ones).
    from collections import defaultdict

    slot_counts: dict[tuple[datetime.date, str], int] = defaultdict(int)
    for a in result.assignments:
        slot_counts[(a.date, a.shift_template_id)] += 1
    assert all(c == 2 for c in slot_counts.values()), (
        f"Expected 2 per slot, got {dict(slot_counts)}"
    )


def test_infeasible_detected() -> None:
    """Hard staffing infeasibility is not hidden by softened target constraints."""
    role = make_role("RN")
    shift = make_shift("E")
    period_start = datetime.date(2026, 9, 1)
    period_days = 1

    nurse = make_nurse(role_ids=[role.id])
    nurse.contract = ContractData(
        nurse_id=nurse.id,
        shifts_per_period=1,
        max_shifts_per_period=None,
        min_rest_hours=11,
        max_consecutive_days=5,
        enforce_balanced=False,
        enforce_shifts_per_period=True,
        enforce_one_shift_per_day=True,
    )

    domain = DomainData(
        tenant_id="t1",
        period_start=period_start,
        period_days=period_days,
        nurses=[nurse],
        roles={role.id: role},
        shift_templates=[shift],
        skill_mix_rules=[
            SkillMixRuleData(
                id=make_uuid(),
                name="E: 2RN",
                shift_template_id=shift.id,
                priority=0,
                requirements=(SkillMixRequirementData(role_id=role.id, count=2),),
            )
        ],
        shift_sequence_rules=[],
        preferences=[],
        previous_assignments=[],
        dates=[period_start],
        extended_dates=[period_start - datetime.timedelta(days=1), period_start],
    )

    engine = RosterCPModel(domain, SolverConfig(timeout_seconds=5, num_workers=2))
    with pytest.raises(InfeasibleError):
        engine.solve()


def test_target_over_supply_is_now_soft() -> None:
    """A target above available shift slots no longer makes the model infeasible."""
    role = make_role("RN")
    shift = make_shift("E")
    period_start = datetime.date(2026, 9, 1)

    nurse = make_nurse(role_ids=[role.id])
    nurse.contract = ContractData(
        nurse_id=nurse.id,
        shifts_per_period=5,
        max_shifts_per_period=None,
        min_rest_hours=11,
        max_consecutive_days=5,
        enforce_balanced=False,
        enforce_shifts_per_period=True,
        enforce_one_shift_per_day=True,
    )

    domain = DomainData(
        tenant_id="t1",
        period_start=period_start,
        period_days=1,
        nurses=[nurse],
        roles={role.id: role},
        shift_templates=[shift],
        skill_mix_rules=[],
        shift_sequence_rules=[],
        preferences=[],
        previous_assignments=[],
        dates=[period_start],
        extended_dates=[period_start - datetime.timedelta(days=1), period_start],
    )

    engine = RosterCPModel(domain, SolverConfig(timeout_seconds=5, num_workers=2))
    result = engine.solve()
    assert result.outcome in ("optimal", "feasible")
    assert len(result.assignments) == 1


def test_preferences_maximized() -> None:
    """A LIKE preference should bias the solver toward assigning that shift."""
    role = make_role("RN")
    shift = make_shift("E")
    period_start = datetime.date(2026, 9, 1)
    period_days = 2

    nurse = make_nurse(role_ids=[role.id])
    nurse.contract = ContractData(
        nurse_id=nurse.id,
        shifts_per_period=2,
        max_shifts_per_period=None,
        min_rest_hours=11,
        max_consecutive_days=5,
        enforce_balanced=False,
        enforce_shifts_per_period=True,
        enforce_one_shift_per_day=True,
    )

    pref = NursePreferenceData(
        nurse_id=nurse.id,
        date=period_start,
        shift_template_id=shift.id,
        request_type="like",
        priority=10,
    )

    domain = DomainData(
        tenant_id="t1",
        period_start=period_start,
        period_days=period_days,
        nurses=[nurse],
        roles={role.id: role},
        shift_templates=[shift],
        skill_mix_rules=[],
        shift_sequence_rules=[],
        preferences=[pref],
        previous_assignments=[],
        dates=[period_start + datetime.timedelta(days=i) for i in range(period_days)],
        extended_dates=[
            (period_start - datetime.timedelta(days=period_days) + datetime.timedelta(days=i))
            for i in range(period_days * 2)
        ],
    )

    engine = RosterCPModel(domain, SolverConfig(timeout_seconds=10, num_workers=4))
    result = engine.solve()

    # Nurse should be assigned on the preferred date
    assigned_dates = {a.date for a in result.assignments}
    assert period_start in assigned_dates
    assert result.preference_stats == {"loaded": 1, "satisfied": 1, "violated": 0}
    matching = next(
        a for a in result.assignments
        if a.date == period_start and a.shift_template_id == shift.id
    )
    assert matching.satisfied_preference is True


def test_same_day_like_preferences_are_alternatives() -> None:
    role = make_role("RN")
    early = make_shift("E")
    late = ShiftTemplateData(
        id=make_uuid(),
        code="L",
        name="Shift L",
        start_time=datetime.time(15, 0),
        end_time=datetime.time(23, 0),
        duration_hours=8.0,
        day_numbers=frozenset(range(1, 8)),
    )
    period_start = datetime.date(2026, 9, 1)

    nurse = make_nurse(role_ids=[role.id])
    nurse.contract = ContractData(
        nurse_id=nurse.id,
        shifts_per_period=2,
        max_shifts_per_period=4,
        min_rest_hours=0,
        max_consecutive_days=5,
        enforce_balanced=False,
        enforce_shifts_per_period=False,
        enforce_one_shift_per_day=False,
    )

    same_priority_early = NursePreferenceData(
        nurse_id=nurse.id,
        date=period_start,
        shift_template_id=early.id,
        request_type="like",
        priority=3,
    )
    same_priority_late = NursePreferenceData(
        nurse_id=nurse.id,
        date=period_start,
        shift_template_id=late.id,
        request_type="like",
        priority=3,
    )
    domain = DomainData(
        tenant_id="t1",
        period_start=period_start,
        period_days=1,
        nurses=[nurse],
        roles={role.id: role},
        shift_templates=[early, late],
        skill_mix_rules=[],
        shift_sequence_rules=[],
        preferences=[same_priority_early, same_priority_late],
        previous_assignments=[],
        dates=[period_start],
        extended_dates=[period_start],
    )

    engine = RosterCPModel(domain, SolverConfig(timeout_seconds=10, num_workers=1))
    result = engine.solve()

    day_assignments = [
        assignment for assignment in result.assignments if assignment.date == period_start
    ]
    assert len(day_assignments) == 1
    assert day_assignments[0].shift_template_id == early.id
    assert day_assignments[0].satisfied_preference is True
    assert result.preference_stats == {"loaded": 1, "satisfied": 1, "violated": 0}


def test_supply_shortage_returns_closest_feasible_schedule() -> None:
    """Soft target constraints allow the largest feasible partial roster."""
    role = make_role("RN")
    shift = make_shift("E")
    period_start = datetime.date(2026, 9, 1)

    nurse = make_nurse(role_ids=[role.id])
    nurse.contract = ContractData(
        nurse_id=nurse.id,
        shifts_per_period=2,
        max_shifts_per_period=6,
        min_rest_hours=11,
        max_consecutive_days=5,
        enforce_balanced=False,
        enforce_shifts_per_period=True,
        enforce_one_shift_per_day=True,
    )

    domain = DomainData(
        tenant_id="t1",
        period_start=period_start,
        period_days=1,
        nurses=[nurse],
        roles={role.id: role},
        shift_templates=[shift],
        skill_mix_rules=[],
        shift_sequence_rules=[],
        preferences=[],
        previous_assignments=[],
        dates=[period_start],
        extended_dates=[
            period_start - datetime.timedelta(days=1),
            period_start,
        ],
    )
    engine = RosterCPModel(domain, SolverConfig(timeout_seconds=10, num_workers=4))
    result = engine.solve()

    assert result.outcome in ("optimal", "feasible")
    assert len(result.assignments) == 1
    assert result.assignments[0].nurse_id == nurse.id


def test_objective_prefers_minimizing_shift_target_deviations() -> None:
    """High soft penalties dominate equal-weight preference scores."""
    role = make_role("RN")
    shift = make_shift("E")
    period_start = datetime.date(2026, 9, 1)

    nurse = make_nurse(role_ids=[role.id])
    nurse.contract = ContractData(
        nurse_id=nurse.id,
        shifts_per_period=1,
        max_shifts_per_period=6,
        min_rest_hours=11,
        max_consecutive_days=5,
        enforce_balanced=False,
        enforce_shifts_per_period=True,
        enforce_one_shift_per_day=True,
    )
    like_one = NursePreferenceData(
        nurse_id=nurse.id,
        date=period_start,
        shift_template_id=shift.id,
        request_type="like",
        priority=10,
    )
    like_two = NursePreferenceData(
        nurse_id=nurse.id,
        date=period_start + datetime.timedelta(days=1),
        shift_template_id=shift.id,
        request_type="like",
        priority=10,
    )
    domain = DomainData(
        tenant_id="t1",
        period_start=period_start,
        period_days=2,
        nurses=[nurse],
        roles={role.id: role},
        shift_templates=[shift],
        skill_mix_rules=[],
        shift_sequence_rules=[],
        preferences=[like_one, like_two],
        previous_assignments=[],
        dates=[
            period_start,
            period_start + datetime.timedelta(days=1),
        ],
        extended_dates=[
            period_start - datetime.timedelta(days=2),
            period_start - datetime.timedelta(days=1),
            period_start,
            period_start + datetime.timedelta(days=1),
        ],
    )

    engine = RosterCPModel(domain, SolverConfig(timeout_seconds=10, num_workers=4))
    result = engine.solve()

    assigned_dates = {assignment.date for assignment in result.assignments}
    assert len(assigned_dates) == 1


def test_hard_infeasibility_is_still_detected() -> None:
    """Soft target penalties do not soften hard staffing constraints."""
    role = make_role("RN")
    shift = make_shift("E")
    period_start = datetime.date(2026, 9, 1)

    nurse = make_nurse(
        role_ids=[role.id],
        leave_dates=frozenset({period_start}),
    )
    nurse.contract = ContractData(
        nurse_id=nurse.id,
        shifts_per_period=2,
        max_shifts_per_period=6,
        min_rest_hours=11,
        max_consecutive_days=5,
        enforce_balanced=False,
        enforce_shifts_per_period=True,
        enforce_one_shift_per_day=True,
    )
    skill_mix = SkillMixRuleData(
        id=make_uuid(),
        name="E: 2RN",
        shift_template_id=shift.id,
        priority=0,
        requirements=(SkillMixRequirementData(role_id=role.id, count=2),),
    )
    domain = DomainData(
        tenant_id="t1",
        period_start=period_start,
        period_days=1,
        nurses=[nurse],
        roles={role.id: role},
        shift_templates=[shift],
        skill_mix_rules=[skill_mix],
        shift_sequence_rules=[],
        preferences=[],
        previous_assignments=[],
        dates=[period_start],
        extended_dates=[
            period_start - datetime.timedelta(days=1),
            period_start,
        ],
    )

    engine = RosterCPModel(domain, SolverConfig(timeout_seconds=10, num_workers=4))
    with pytest.raises(InfeasibleError):
        engine.solve()


# ──────────────────────────────────────────────────────────────
# Scale test (Stage 3 acceptance: realistic roster size)
# ──────────────────────────────────────────────────────────────
def test_solve_scale_42_nurses_14_days() -> None:
    """42 nurses, 2 roles, 2 shift templates, 14 days.

    Stage 3 acceptance: a realistic roster must solve feasibly and satisfy:
      - one shift per nurse per day
      - skill-mix counts honored on every timeslot
      - per-nurse assignment counts within [target-2, target+2]

    Supply/demand must be exactly balanced because add_shifts_per_period uses
    a hard `== target` constraint:
      Supply: 42 nurses × 4 shifts = 168 nurse-shifts.
      Demand: 14 days × 2 shifts × (4 RN + 2 LV) = 14 × 12 = 168 slots. ✓
      RN supply 24×4=96 ≥ demand 14×2×4=112? NO — need enough RN per role.
      RN: 28 × 4 = 112 ≥ 112 ✓ ; LV: 14 × 4 = 56 = 14×2×2 = 56 ✓.
      Total nurses = 28 + 14 = 42, total shifts = 168 = 168. ✓
    """
    role_rn = make_role("RN")
    role_lv = make_role("LV")
    shift_e = make_shift("E")
    shift_n = make_shift("N")
    period_start = datetime.date(2026, 9, 1)
    period_days = 14
    dates = [period_start + datetime.timedelta(days=i) for i in range(period_days)]

    # 42 nurses: 28 RN, 14 LV. Each targets 4 shifts/period (exact match).
    # NOTE: balanced penalties are now soft; this scale test can exercise them
    # without making role-specific slot demand infeasible.
    nurses = []
    for i in range(42):
        role_id = role_rn.id if i < 28 else role_lv.id
        n = make_nurse(role_ids=[role_id])
        n.contract = ContractData(
            nurse_id=n.id,
            shifts_per_period=4,
            max_shifts_per_period=6,
            min_rest_hours=11,
            max_consecutive_days=5,
            enforce_balanced=True,
            enforce_shifts_per_period=True,
            enforce_one_shift_per_day=True,
        )
        nurses.append(n)

    # Skill mix: each shift needs 4 RN + 2 LV per timeslot.
    # Demand: 14 × 2 × 6 = 168. Supply: 120 RN + 80 LV = 200. Balanced.
    sm_e = SkillMixRuleData(
        id=make_uuid(),
        name="E: 4RN+2LV",
        shift_template_id=shift_e.id,
        priority=0,
        requirements=(
            SkillMixRequirementData(role_id=role_rn.id, count=4),
            SkillMixRequirementData(role_id=role_lv.id, count=2),
        ),
    )
    sm_n = SkillMixRuleData(
        id=make_uuid(),
        name="N: 4RN+2LV",
        shift_template_id=shift_n.id,
        priority=0,
        requirements=(
            SkillMixRequirementData(role_id=role_rn.id, count=4),
            SkillMixRequirementData(role_id=role_lv.id, count=2),
        ),
    )

    domain = DomainData(
        tenant_id="t1",
        period_start=period_start,
        period_days=period_days,
        nurses=nurses,
        roles={role_rn.id: role_rn, role_lv.id: role_lv},
        shift_templates=[shift_e, shift_n],
        skill_mix_rules=[sm_e, sm_n],
        shift_sequence_rules=[],
        preferences=[],
        previous_assignments=[],
        dates=dates,
        extended_dates=[
            (period_start - datetime.timedelta(days=period_days) + datetime.timedelta(days=i))
            for i in range(period_days * 2)
        ],
    )

    engine = RosterCPModel(domain, SolverConfig(timeout_seconds=60, num_workers=8))
    result = engine.solve()

    assert result.outcome in ("optimal", "feasible")

    from collections import defaultdict

    # One shift per nurse per day
    per_nurse_per_day: dict[tuple[str, datetime.date], int] = defaultdict(int)
    for a in result.assignments:
        per_nurse_per_day[(a.nurse_id, a.date)] += 1
    assert all(c == 1 for c in per_nurse_per_day.values()), (
        "Some nurse assigned >1 shift on the same day"
    )

    # Skill mix honored on every (date, shift) timeslot that fired
    slot_counts: dict[tuple[datetime.date, str], dict[str, int]] = defaultdict(
        lambda: defaultdict(int)
    )
    for a in result.assignments:
        slot_counts[(a.date, a.shift_template_id)][a.role_id] += 1
    for (d, sid), counts in slot_counts.items():
        assert counts[role_rn.id] == 4, f"RN count {counts[role_rn.id]} != 4 on {d}/{sid}"
        assert counts[role_lv.id] == 2, f"LV count {counts[role_lv.id]} != 2 on {d}/{sid}"

    # Each nurse should have ~4 assignments, allow 2..6 for slack
    nurse_total: dict[str, int] = defaultdict(int)
    for a in result.assignments:
        nurse_total[a.nurse_id] += 1
    assert all(2 <= c <= 6 for c in nurse_total.values()), (
        f"Nurse assignment counts out of [2,6]: {dict(nurse_total)}"
    )
    # Total assignments must fill every skill-mix slot
    assert len(result.assignments) == 168, (
        f"Expected 168 assignments (14d×2shifts×6), got {len(result.assignments)}"
    )


# ──────────────────────────────────────────────────────────────
# Min rest hours + max consecutive days tests
# ──────────────────────────────────────────────────────────────

def _rest_role(code: str = "RN") -> RoleData:
    return RoleData(id=make_uuid(), code=code, name=f"Role {code}")


def _rest_shift(
    code: str = "E",
    start: tuple[int, int] = (7, 0),
    end: tuple[int, int] = (15, 0),
    day_numbers: frozenset[int] | None = None,
) -> ShiftTemplateData:
    if day_numbers is None:
        day_numbers = frozenset(range(1, 8))
    return ShiftTemplateData(
        id=make_uuid(),
        code=code,
        name=f"Shift {code}",
        start_time=datetime.time(*start),
        end_time=datetime.time(*end),
        duration_hours=8.0,
        day_numbers=day_numbers,
    )


def _rest_nurse(
    role_ids: list[str],
    contract: ContractData | None = None,
) -> NurseData:
    return NurseData(
        id=make_uuid(),
        employee_id="N001",
        first_name="Jane",
        last_name="Doe",
        is_available=True,
        role_ids=role_ids,
        skill_ids=[],
        contract=contract,
        leave_dates=frozenset(),
    )


def _rest_contract(
    nurse_id: str,
    shifts_per_period: int = 5,
    min_rest_hours: int = 11,
    max_consecutive_days: int = 5,
    enforce_balanced: bool = True,
    enforce_shifts_per_period: bool = True,
) -> ContractData:
    return ContractData(
        nurse_id=nurse_id,
        shifts_per_period=shifts_per_period,
        max_shifts_per_period=None,
        min_rest_hours=min_rest_hours,
        max_consecutive_days=max_consecutive_days,
        enforce_balanced=enforce_balanced,
        enforce_shifts_per_period=enforce_shifts_per_period,
        enforce_one_shift_per_day=True,
    )


def _rest_domain(
    nurses: list[NurseData],
    roles: list[RoleData],
    shifts: list[ShiftTemplateData],
    period_start: datetime.date,
    period_days: int,
    **kw: Any,
) -> DomainData:
    return DomainData(
        tenant_id="t1",
        period_start=period_start,
        period_days=period_days,
        nurses=nurses,
        roles={r.id: r for r in roles},
        shift_templates=shifts,
        skill_mix_rules=kw.get("skill_mix_rules", []),
        shift_sequence_rules=kw.get("shift_sequence_rules", []),
        preferences=kw.get("preferences", []),
        previous_assignments=kw.get("previous_assignments", []),
        dates=[period_start + datetime.timedelta(days=i) for i in range(period_days)],
        extended_dates=[
            (period_start - datetime.timedelta(days=period_days) + datetime.timedelta(days=i))
            for i in range(period_days * 2)
        ],
    )


def test_min_rest_hours_allows_compatible_shifts() -> None:
    """Early→Early next day = 16h rest; with min_rest=11 it's allowed.
    One nurse works 3 days of Early — should be feasible."""
    role = _rest_role("RN")
    early = _rest_shift("E")
    period_start = datetime.date(2026, 9, 1)
    period_days = 3

    nurse = _rest_nurse(role_ids=[role.id])
    nurse.contract = _rest_contract(nurse.id, shifts_per_period=3, min_rest_hours=11,
                                    enforce_balanced=False)
    domain = _rest_domain([nurse], [role], [early], period_start, period_days)

    engine = RosterCPModel(domain, SolverConfig(timeout_seconds=10, num_workers=4))
    result = engine.solve()
    assert result.outcome in ("optimal", "feasible")
    assert len(result.assignments) == 3


def test_min_rest_hours_prevents_late_then_early() -> None:
    """Late ends 22:00, Early starts 07:00 → 9h rest < min_rest=11.
    Two nurses cover Early+Late each day; no nurse may do Late→Early."""
    from app.scheduling.domain import SkillMixRequirementData, SkillMixRuleData

    role = _rest_role("RN")
    early = _rest_shift("E", start=(7, 0), end=(15, 0))
    late = _rest_shift("L", start=(14, 0), end=(22, 0))
    period_start = datetime.date(2026, 9, 1)
    period_days = 4

    nurses = []
    for _ in range(2):
        n = _rest_nurse(role_ids=[role.id])
        n.contract = _rest_contract(n.id, shifts_per_period=4, min_rest_hours=11,
                                    max_consecutive_days=5, enforce_balanced=False)
        nurses.append(n)

    # Skill mix: 1 RN per shift per day → 2 assignments/day over 4 days = 8 total.
    sm_rules = [
        SkillMixRuleData(
            id=make_uuid(), name=f"SM-{st.code}", shift_template_id=st.id,
            priority=1,
            requirements=(SkillMixRequirementData(role_id=role.id, count=1, skill_id=None),),
        )
        for st in [early, late]
    ]
    domain = _rest_domain(nurses, [role], [early, late], period_start, period_days,
                          skill_mix_rules=sm_rules)

    engine = RosterCPModel(domain, SolverConfig(timeout_seconds=10, num_workers=4))
    result = engine.solve()
    assert result.outcome in ("optimal", "feasible")

    # Group each nurse's assignments by date, then check consecutive days.
    nurse_by_date: dict[str, dict[datetime.date, str]] = {}
    for a in result.assignments:
        nurse_by_date.setdefault(a.nurse_id, {})[a.date] = a.shift_template_id
    for nid, day_map in nurse_by_date.items():
        sorted_dates = sorted(day_map)
        for i in range(len(sorted_dates) - 1):
            cur = day_map[sorted_dates[i]]
            nxt = day_map[sorted_dates[i + 1]]
            if cur == late.id and nxt == early.id:
                pytest.fail(
                    f"Nurse {nid}: Late(22:00)→Early(07:00) = 9h rest < 11h"
                )


def test_min_rest_hours_handles_multi_role_assignments() -> None:
    from app.scheduling.domain import SkillMixRequirementData, SkillMixRuleData

    role_rn = _rest_role("RN")
    role_sr = _rest_role("SR")
    early = _rest_shift("E", start=(7, 0), end=(15, 0))
    late = _rest_shift("L", start=(14, 0), end=(22, 0))
    period_start = datetime.date(2026, 9, 1)

    nurses = []
    for role_ids in ([role_rn.id, role_sr.id], [role_rn.id]):
        nurse = _rest_nurse(role_ids=role_ids)
        nurse.contract = _rest_contract(
            nurse.id, shifts_per_period=2, min_rest_hours=11, enforce_balanced=False
        )
        nurses.append(nurse)

    skill_mix_rules = [
        SkillMixRuleData(
            id=make_uuid(),
            name=f"SM-{shift.code}",
            shift_template_id=shift.id,
            priority=1,
            requirements=(SkillMixRequirementData(role_id=role_rn.id, count=1),),
        )
        for shift in (early, late)
    ]
    domain = _rest_domain(
        nurses, [role_rn, role_sr], [early, late], period_start, period_days=2,
        skill_mix_rules=skill_mix_rules,
    )

    engine = RosterCPModel(domain, SolverConfig(timeout_seconds=10, num_workers=4))
    result = engine.solve()
    assert result.outcome in ("optimal", "feasible")

    multi_role = next(nurse for nurse in nurses if len(nurse.role_ids) == 2)
    by_date = {
        assignment.date: assignment.shift_template_id
        for assignment in result.assignments
        if assignment.nurse_id == multi_role.id
    }
    assert not (
        by_date.get(period_start) == late.id
        and by_date.get(period_start + datetime.timedelta(days=1)) == early.id
    )


def test_max_consecutive_days_caps_work_streak() -> None:
    """max_consecutive_days=2 over 6 days → no 3-consecutive-day streak."""
    role = _rest_role("RN")
    shift = _rest_shift("E")
    period_start = datetime.date(2026, 9, 1)
    period_days = 6

    nurse = _rest_nurse(role_ids=[role.id])
    nurse.contract = _rest_contract(nurse.id, shifts_per_period=6, min_rest_hours=0,
                                    max_consecutive_days=2, enforce_balanced=False,
                                    enforce_shifts_per_period=False)

    domain = _rest_domain([nurse], [role], [shift], period_start, period_days)
    engine = RosterCPModel(domain, SolverConfig(timeout_seconds=10, num_workers=4))
    result = engine.solve()
    assert result.outcome in ("optimal", "feasible")

    work_days = sorted(a.date for a in result.assignments)
    for i in range(len(work_days) - 2):
        d0, d1, d2 = work_days[i], work_days[i + 1], work_days[i + 2]
        if (d1 - d0).days == 1 and (d2 - d1).days == 1:
            pytest.fail(f"3 consecutive work days: {d0}, {d1}, {d2}")
