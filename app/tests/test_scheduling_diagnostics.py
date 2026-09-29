"""Structural diagnostics for infeasible scheduling data."""

from __future__ import annotations

import datetime

from app.scheduling.diagnostics import explain_infeasibility
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


def _make_shift(code: str) -> ShiftTemplateData:
    return ShiftTemplateData(
        id=make_uuid(),
        code=code,
        name=f"{code}班",
        start_time=datetime.time(7, 0),
        end_time=datetime.time(15, 0),
        duration_hours=8,
        day_numbers=frozenset(range(1, 8)),
    )


def _make_nurse(
    number: int,
    role_ids: list[str],
    contract: ContractData | None = None,
) -> NurseData:
    return NurseData(
        id=make_uuid(),
        employee_id=f"T-{number:02d}",
        first_name=f"护士{number}",
        last_name="测",
        is_available=True,
        role_ids=role_ids,
        skill_ids=[],
        contract=contract,
    )


def _make_domain(
    role: RoleData,
    shifts: list[ShiftTemplateData],
    nurses: list[NurseData],
    rules: list[SkillMixRuleData],
) -> DomainData:
    period_start = datetime.date(2026, 9, 1)
    period_days = 7
    return DomainData(
        tenant_id=make_uuid(),
        period_start=period_start,
        period_days=period_days,
        nurses=nurses,
        roles={role.id: role},
        shift_templates=shifts,
        skill_mix_rules=rules,
        shift_sequence_rules=[],
        preferences=[],
        previous_assignments=[],
        dates=[
            period_start + datetime.timedelta(days=offset)
            for offset in range(period_days)
        ],
        extended_dates=[
            period_start - datetime.timedelta(days=period_days)
            + datetime.timedelta(days=offset)
            for offset in range(period_days * 2)
        ],
    )


def test_daily_staff_demand_conflict_is_explained() -> None:
    nurse_role = RoleData(id=make_uuid(), code="NURSE", name="责任护士")
    senior_role = RoleData(id=make_uuid(), code="SENIOR", name="高级责任护士")
    shifts = [_make_shift(code) for code in ("D", "L", "N")]
    nurses = [
        _make_nurse(
            number,
            [nurse_role.id] + ([senior_role.id] if number < 3 else []),
        )
        for number in range(6)
    ]
    rules = [
        SkillMixRuleData(
            id=make_uuid(),
            name=f"{shift.code}标准配置",
            shift_template_id=shift.id,
            priority=0,
            requirements=(
                SkillMixRequirementData(role_id=nurse_role.id, count=2),
                SkillMixRequirementData(role_id=senior_role.id, count=1),
            ),
        )
        for shift in shifts
    ]
    domain = _make_domain(nurse_role, shifts, nurses, rules)

    reasons = explain_infeasibility(domain)

    assert any("当日至少需要 9 人" in reason for reason in reasons)
    assert any("只有 6 名可排班护士" in reason for reason in reasons)


def test_contract_target_conflict_is_explained() -> None:
    role = RoleData(id=make_uuid(), code="RN", name="护士")
    shift = _make_shift("D")
    nurse = _make_nurse(0, [role.id])
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
    domain = _make_domain(role, [shift], [nurse], [])
    domain.period_days = 1
    domain.dates = [domain.period_start]
    domain.extended_dates = [
        domain.period_start - datetime.timedelta(days=1),
        domain.period_start,
    ]

    reasons = explain_infeasibility(domain)

    assert any("合约要求 5 班" in reason for reason in reasons)
    assert any("最多只能排 1 班" in reason for reason in reasons)


def _make_skill_shift(code: str, day_numbers: frozenset[int] | None = None) -> ShiftTemplateData:
    return ShiftTemplateData(
        id=make_uuid(),
        code=code,
        name=f"{code}班",
        start_time=datetime.time(7, 0),
        end_time=datetime.time(15, 0),
        duration_hours=8,
        day_numbers=day_numbers or frozenset(range(1, 8)),
    )


def test_aggregate_skill_capacity_shortfall_is_explained() -> None:
    nurse_role = RoleData(id=make_uuid(), code="NURSE", name="责任护士")
    senior_role = RoleData(id=make_uuid(), code="SENIOR", name="高级责任护士")
    bls_skill = make_uuid()
    shifts = [_make_skill_shift(code) for code in ("D", "L", "N")]
    # 6 nurses; only 2 have SENIOR+BLS.
    nurses = []
    for number in range(6):
        n = _make_nurse(
            number,
            [nurse_role.id] + ([senior_role.id] if number < 2 else []),
        )
        n.skill_ids = [bls_skill]
        nurses.append(n)
    rules = [
        SkillMixRuleData(
            id=make_uuid(),
            name=f"{shift.code}标准配置",
            shift_template_id=shift.id,
            priority=0,
            requirements=(
                SkillMixRequirementData(role_id=nurse_role.id, count=1, skill_id=bls_skill),
                SkillMixRequirementData(role_id=senior_role.id, count=1, skill_id=bls_skill),
            ),
        )
        for shift in shifts
    ]
    domain = _make_domain(nurse_role, shifts, nurses, rules)
    domain.roles = {nurse_role.id: nurse_role, senior_role.id: senior_role}
    domain.skills = {bls_skill: "基础急救（BLS）"}
    # 14-day period: 3 shifts * 14 days * 1 = 42 SENIOR+BLS demand;
    # 2 eligible nurses * 14 days = 28 capacity -> shortfall.
    domain.period_days = 14
    domain.dates = [
        domain.period_start + datetime.timedelta(days=offset)
        for offset in range(14)
    ]

    reasons = explain_infeasibility(domain)

    assert any("共需 42 班次" in reason for reason in reasons)
    assert any("最多可提供 28 班次" in reason for reason in reasons)
    assert any("SENIOR" in reason or "高级责任护士" in reason for reason in reasons)


def test_daily_capacity_excludes_nurses_without_required_skill() -> None:
    """A nurse with the right role but missing the required skill should
    not be counted in daily capacity — the original bug where T-01 had
    NURSE role but no BLS, yet was counted, hiding the shortfall."""
    nurse_role = RoleData(id=make_uuid(), code="NURSE", name="责任护士")
    senior_role = RoleData(id=make_uuid(), code="SENIOR", name="高级责任护士")
    bls_skill = make_uuid()
    shifts = [_make_skill_shift(code) for code in ("D", "L", "N")]
    # 6 nurses; T-01 has NURSE but only ICU (no BLS).
    nurses = []
    for number in range(6):
        n = _make_nurse(
            number,
            [nurse_role.id] + ([senior_role.id] if number < 3 else []),
        )
        n.skill_ids = [bls_skill] if number != 1 else [make_uuid()]  # T-01 lacks BLS
        nurses.append(n)
    rules = [
        SkillMixRuleData(
            id=make_uuid(),
            name=f"{shift.code}标准配置",
            shift_template_id=shift.id,
            priority=0,
            requirements=(
                SkillMixRequirementData(role_id=nurse_role.id, count=1, skill_id=bls_skill),
                SkillMixRequirementData(role_id=senior_role.id, count=1, skill_id=bls_skill),
            ),
        )
        for shift in shifts
    ]
    domain = _make_domain(nurse_role, shifts, nurses, rules)
    domain.roles = {nurse_role.id: nurse_role, senior_role.id: senior_role}
    domain.skills = {bls_skill: "基础急救（BLS）"}

    reasons = explain_infeasibility(domain)

    # 3 shifts * 2 = 6 demand/day, only 5 eligible nurses (T-01 excluded) -> 5 capacity
    assert any("当日至少需要 6 人" in reason for reason in reasons)
    assert any("只有 5 名可排班护士" in reason for reason in reasons)



def test_avoid_preference_reduces_effective_capacity() -> None:
    """When AVOID preferences shrink the willing nurse pool below demand,
    diagnostics should report it — even if raw capacity (without prefs)
    would be sufficient."""
    nurse_role = RoleData(id=make_uuid(), code="NURSE", name="责任护士")
    bls_skill = make_uuid()
    shift = _make_skill_shift("D")
    # 3 nurses, all eligible (NURSE+BLS), demand=2 per day.
    # But 2 of them AVOID the shift on one date -> only 1 willing -> shortfall.
    nurses = []
    for number in range(3):
        n = _make_nurse(number, [nurse_role.id])
        n.skill_ids = [bls_skill]
        nurses.append(n)
    rules = [
        SkillMixRuleData(
            id=make_uuid(),
            name="标准配置",
            shift_template_id=shift.id,
            priority=0,
            requirements=(
                SkillMixRequirementData(role_id=nurse_role.id, count=2, skill_id=bls_skill),
            ),
        )
    ]
    domain = _make_domain(nurse_role, [shift], nurses, rules)
    domain.skills = {bls_skill: "基础急救（BLS）"}
    domain.preferences = [
        NursePreferenceData(
            nurse_id=nurses[0].id,
            date=domain.period_start,
            shift_template_id=shift.id,
            request_type="avoid",
            priority=5,
        ),
        NursePreferenceData(
            nurse_id=nurses[1].id,
            date=domain.period_start,
            shift_template_id=shift.id,
            request_type="avoid",
            priority=5,
        ),
    ]

    reasons = explain_infeasibility(domain)

    assert any("排除不期望该班次的护士后只有 1 人可排" in reason for reason in reasons)
