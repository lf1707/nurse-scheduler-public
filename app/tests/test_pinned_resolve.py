"""B3-11: local re-solve with pinned assignments — synthetic, no DB."""

import datetime

from app.scheduling.domain import (
    AssignmentResult,
    ContractData,
    DomainData,
    NurseData,
    RoleData,
    ShiftTemplateData,
    make_uuid,
)
from app.scheduling.engine import RosterCPModel, SolverConfig


def _make_domain(
    period_days: int = 7,
    num_nurses: int = 5,
) -> tuple[DomainData, NurseData, RoleData, ShiftTemplateData]:
    role = RoleData(id=make_uuid(), code="RN", name="Nurse")
    shift = ShiftTemplateData(
        id=make_uuid(), code="E", name="Early",
        start_time=datetime.time(7, 0), end_time=datetime.time(15, 0),
        duration_hours=8.0, day_numbers=frozenset(range(1, 8)),
    )
    period_start = datetime.date(2026, 10, 1)
    nurses = []
    for _ in range(num_nurses):
        n = NurseData(
            id=make_uuid(), employee_id="N", first_name="J", last_name="D",
            is_available=True, role_ids=[role.id], skill_ids=[],
        )
        n.contract = ContractData(
            nurse_id=n.id, shifts_per_period=1, max_shifts_per_period=None,
            min_rest_hours=11, max_consecutive_days=5,
            enforce_balanced=True, enforce_shifts_per_period=True,
            enforce_one_shift_per_day=True,
        )
        nurses.append(n)
    domain = DomainData(
        tenant_id="t1", period_start=period_start, period_days=period_days,
        nurses=nurses, roles={role.id: role}, shift_templates=[shift],
        skill_mix_rules=[], shift_sequence_rules=[], preferences=[],
        previous_assignments=[],
        dates=[period_start + datetime.timedelta(days=i) for i in range(period_days)],
        extended_dates=[
            period_start - datetime.timedelta(days=period_days) + datetime.timedelta(days=i)
            for i in range(period_days * 2)
        ],
    )
    return domain, nurses[0], role, shift


def test_pinned_assignment_appears_in_solution() -> None:
    domain, nurse, role, shift = _make_domain()
    day0 = domain.period_start
    domain.pinned_assignments = [
        AssignmentResult(nurse_id=nurse.id, role_id=role.id, date=day0, shift_template_id=shift.id)
    ]
    engine = RosterCPModel(domain, SolverConfig(timeout_seconds=15, num_workers=4))
    result = engine.solve()
    pinned = [a for a in result.assignments if a.nurse_id == nurse.id and a.date == day0]
    assert len(pinned) == 1 and pinned[0].shift_template_id == shift.id


def test_pinned_nurse_not_double_booked() -> None:
    domain, nurse, role, shift = _make_domain()
    day0 = domain.period_start
    domain.pinned_assignments = [
        AssignmentResult(nurse_id=nurse.id, role_id=role.id, date=day0, shift_template_id=shift.id)
    ]
    engine = RosterCPModel(domain, SolverConfig(timeout_seconds=15, num_workers=4))
    result = engine.solve()
    day0_all = [a for a in result.assignments if a.nurse_id == nurse.id and a.date == day0]
    assert len(day0_all) == 1  # one shift per day
