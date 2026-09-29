"""B3-10 unit tests for edit_validation (no HTTP, pure logic)."""

from __future__ import annotations

import datetime

from app.scheduling.domain import (
    ContractData,
    DomainData,
    NurseData,
    ShiftTemplateData,
    SkillMixRequirementData,
    SkillMixRuleData,
)
from app.scheduling.edit_validation import ProposedAssignment, validate_assignments


def _domain(
    nurses: list[NurseData] | None = None,
    shift_templates: list[ShiftTemplateData] | None = None,
    skill_mix_rules: list[SkillMixRuleData] | None = None,
) -> DomainData:
    return DomainData(
        tenant_id="t1",
        period_start=MON,
        period_days=7,
        nurses=nurses or [],
        roles={},
        shift_templates=shift_templates or [],
        skill_mix_rules=skill_mix_rules or [],
        shift_sequence_rules=[],
        preferences=[],
        previous_assignments=[],
    )


def _nurse(
    id: str = "n1",
    roles: list[str] | None = None,
    contract: ContractData | None = None,
    leave: set[datetime.date] | None = None,
    is_available: bool = True,
    skills: list[str] | None = None,
) -> NurseData:
    return NurseData(
        id=id, employee_id=id, first_name="", last_name="",
        is_available=is_available,
        role_ids=roles or [],
        skill_ids=skills or [],
        contract=contract,
        leave_dates=frozenset(leave or set()),
    )


def _shift(
    id: str = "s1", code: str = "D",
    start: datetime.time = datetime.time(8, 0),
    end: datetime.time = datetime.time(16, 0),
    days: frozenset[int] | None = None,
) -> ShiftTemplateData:
    return ShiftTemplateData(
        id=id, code=code, name=code, start_time=start,
        end_time=end, duration_hours=8.0,
        day_numbers=days or frozenset(range(1, 8)),
    )


def _contract(
    min_rest: int = 0, max_consec: int = 0, max_shifts: int | None = None,
    enforce_shifts: bool = False,
) -> ContractData:
    return ContractData(
        nurse_id="n1", shifts_per_period=5,
        max_shifts_per_period=max_shifts,
        min_rest_hours=min_rest,
        max_consecutive_days=max_consec,
        enforce_balanced=False,
        enforce_shifts_per_period=enforce_shifts,
        enforce_one_shift_per_day=True,
    )


MON = datetime.date(2026, 9, 14)
TUE = datetime.date(2026, 9, 15)
WED = datetime.date(2026, 9, 16)


class TestPastAndPeriod:
    def test_outside_period_rejected(self) -> None:
        d = _domain(nurses=[_nurse()], shift_templates=[_shift()])
        a = [ProposedAssignment("n1", None, datetime.date(2026, 9, 30), "s1")]
        result = validate_assignments(a, d, MON, 7)
        assert any(v["code"] == "outside_period" for v in result.hard_violations)

    def test_within_period_ok(self) -> None:
        d = _domain(nurses=[_nurse()], shift_templates=[_shift()])
        a = [ProposedAssignment("n1", None, TUE, "s1")]
        result = validate_assignments(a, d, MON, 7)
        assert result.ok


class TestLeave:
    def test_on_leave_rejected(self) -> None:
        d = _domain(
            nurses=[_nurse(leave={TUE})],
            shift_templates=[_shift()],
        )
        a = [ProposedAssignment("n1", None, TUE, "s1")]
        result = validate_assignments(a, d, MON, 7)
        assert any(v["code"] == "nurse_on_leave" for v in result.hard_violations)

    def test_not_on_leave_ok(self) -> None:
        d = _domain(nurses=[_nurse()], shift_templates=[_shift()])
        a = [ProposedAssignment("n1", None, TUE, "s1")]
        result = validate_assignments(a, d, MON, 7)
        assert result.ok


class TestOneShiftPerDay:
    def test_two_shifts_same_day_rejected(self) -> None:
        d = _domain(
            nurses=[_nurse()],
            shift_templates=[_shift("s1", "D"), _shift("s2", "N",
                datetime.time(22, 0), datetime.time(6, 0))],
        )
        a = [
            ProposedAssignment("n1", None, TUE, "s1"),
            ProposedAssignment("n1", None, TUE, "s2"),
        ]
        result = validate_assignments(a, d, MON, 7)
        assert any(v["code"] == "multiple_shifts_same_day" for v in result.hard_violations)


class TestMinRest:
    def test_insufficient_rest_rejected(self) -> None:
        late = _shift("s1", "L", datetime.time(14, 0), datetime.time(23, 0))
        early = _shift("s2", "E", datetime.time(8, 0), datetime.time(16, 0))
        d = _domain(
            nurses=[_nurse(contract=_contract(min_rest=11))],
            shift_templates=[late, early],
        )
        a = [
            ProposedAssignment("n1", None, MON, "s1"),
            ProposedAssignment("n1", None, TUE, "s2"),
        ]
        result = validate_assignments(a, d, MON, 7)
        assert any(v["code"] == "min_rest_violation" for v in result.hard_violations)

    def test_late_to_next_night_has_enough_rest(self) -> None:
        late = _shift("s1", "L", datetime.time(14, 0), datetime.time(22, 0))
        night = _shift("s2", "N", datetime.time(23, 0), datetime.time(7, 0))
        d = _domain(
            nurses=[_nurse(contract=_contract(min_rest=11))],
            shift_templates=[late, night],
        )
        a = [
            ProposedAssignment("n1", None, MON, "s1"),
            ProposedAssignment("n1", None, TUE, "s2"),
        ]
        result = validate_assignments(a, d, MON, 7)
        assert not any(v["code"] == "min_rest_violation" for v in result.hard_violations)

    def test_sufficient_rest_ok(self) -> None:
        day = _shift("s1", "D", datetime.time(8, 0), datetime.time(16, 0))
        d = _domain(
            nurses=[_nurse(contract=_contract(min_rest=4))],
            shift_templates=[day],
        )
        a = [
            ProposedAssignment("n1", None, MON, "s1"),
            ProposedAssignment("n1", None, TUE, "s1"),
        ]
        result = validate_assignments(a, d, MON, 7)
        assert not any(v["code"] == "min_rest_violation" for v in result.hard_violations)


class TestMaxConsecutive:
    def test_exceeds_max_consecutive(self) -> None:
        d = _domain(
            nurses=[_nurse(contract=_contract(max_consec=2))],
            shift_templates=[_shift()],
        )
        a = [
            ProposedAssignment("n1", None, MON, "s1"),
            ProposedAssignment("n1", None, TUE, "s1"),
            ProposedAssignment("n1", None, WED, "s1"),
        ]
        result = validate_assignments(a, d, MON, 7)
        assert any(v["code"] == "max_consecutive_violation" for v in result.hard_violations)

    def test_within_max_consecutive_ok(self) -> None:
        d = _domain(
            nurses=[_nurse(contract=_contract(max_consec=2))],
            shift_templates=[_shift()],
        )
        a = [
            ProposedAssignment("n1", None, MON, "s1"),
            ProposedAssignment("n1", None, TUE, "s1"),
        ]
        result = validate_assignments(a, d, MON, 7)
        assert result.ok


class TestMaxShifts:
    def test_exceeds_max_shifts(self) -> None:
        d = _domain(
            nurses=[_nurse(contract=_contract(max_shifts=2, enforce_shifts=True))],
            shift_templates=[_shift()],
        )
        a = [
            ProposedAssignment("n1", None, MON, "s1"),
            ProposedAssignment("n1", None, TUE, "s1"),
            ProposedAssignment("n1", None, WED, "s1"),
        ]
        result = validate_assignments(a, d, MON, 7)
        assert any(v["code"] == "max_shifts_violation" for v in result.hard_violations)

    def test_max_shifts_skipped_when_not_enforced(self) -> None:
        d = _domain(
            nurses=[_nurse(contract=_contract(max_shifts=2, enforce_shifts=False))],
            shift_templates=[_shift()],
        )
        a = [
            ProposedAssignment("n1", None, MON, "s1"),
            ProposedAssignment("n1", None, TUE, "s1"),
            ProposedAssignment("n1", None, WED, "s1"),
        ]
        result = validate_assignments(a, d, MON, 7)
        assert not any(v["code"] == "max_shifts_violation" for v in result.hard_violations)


class TestSkillMix:
    def test_not_met_needs_override(self) -> None:
        rule = SkillMixRuleData(
            id="r1", name="test", shift_template_id="s1", priority=1,
            requirements=(SkillMixRequirementData(role_id="r_nurse", count=1),),
        )
        d = _domain(
            nurses=[_nurse(roles=["r_admin"])],
            shift_templates=[_shift()],
            skill_mix_rules=[rule],
        )
        a = [ProposedAssignment("n1", "r_admin", MON, "s1")]
        result = validate_assignments(a, d, MON, 7)
        assert result.ok
        assert result.needs_override
        assert any(v["code"] == "skill_mix_not_met" for v in result.soft_violations)

    def test_met_no_override_needed(self) -> None:
        rule = SkillMixRuleData(
            id="r1", name="test", shift_template_id="s1", priority=1,
            requirements=(SkillMixRequirementData(role_id="r_nurse", count=1),),
        )
        d = _domain(
            nurses=[_nurse(roles=["r_nurse"])],
            shift_templates=[_shift()],
            skill_mix_rules=[rule],
        )
        a = [ProposedAssignment("n1", "r_nurse", MON, "s1")]
        result = validate_assignments(a, d, MON, 7)
        assert result.ok
        assert not result.needs_override

    def test_any_role_accepts_concrete_role(self) -> None:
        rule = SkillMixRuleData(
            id="r1", name="any", shift_template_id="s1", priority=1,
            requirements=(SkillMixRequirementData(role_id=None, count=1),),
        )
        d = _domain(
            nurses=[_nurse(roles=["r_nurse"])],
            shift_templates=[_shift()],
            skill_mix_rules=[rule],
        )
        a = [ProposedAssignment("n1", "r_nurse", MON, "s1")]
        result = validate_assignments(a, d, MON, 7)
        assert result.ok and not result.needs_override

    def test_any_role_with_skill_requires_that_skill(self) -> None:
        rule = SkillMixRuleData(
            id="r1", name="any+bls", shift_template_id="s1", priority=1,
            requirements=(SkillMixRequirementData(role_id=None, skill_id="bls", count=1),),
        )
        d = _domain(
            nurses=[_nurse(roles=["r_nurse"], skills=["bls"])],
            shift_templates=[_shift()],
            skill_mix_rules=[rule],
        )
        a = [ProposedAssignment("n1", "r_nurse", MON, "s1")]
        result = validate_assignments(a, d, MON, 7)
        assert result.ok and not result.needs_override

    def test_unskilled_nurse_fails_any_role_skill_rule(self) -> None:
        rule = SkillMixRuleData(
            id="r1", name="any+bls", shift_template_id="s1", priority=1,
            requirements=(SkillMixRequirementData(role_id=None, skill_id="bls", count=1),),
        )
        d = _domain(
            nurses=[_nurse(roles=["r_nurse"])],
            shift_templates=[_shift()],
            skill_mix_rules=[rule],
        )
        a = [ProposedAssignment("n1", "r_nurse", MON, "s1")]
        result = validate_assignments(a, d, MON, 7)
        assert result.ok and result.needs_override
        assert any(v["code"] == "skill_mix_not_met" for v in result.soft_violations)
