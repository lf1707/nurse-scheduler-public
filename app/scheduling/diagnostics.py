"""Structural infeasibility diagnostics for scheduling domain data."""

from __future__ import annotations

import datetime
import math
from collections import defaultdict

from app.scheduling.constraints import build_timeslots
from app.scheduling.domain import DomainData, NurseData, RoleData, SkillMixRuleData


def _enforces_one_shift_per_day(nurse: NurseData) -> bool:
    return not nurse.contract or nurse.contract.enforce_one_shift_per_day


def _is_available(nurse: NurseData, date: datetime.date) -> bool:
    return nurse.is_available and date not in nurse.leave_dates


def _role_label(role_id: str | None, roles: dict[str, RoleData]) -> str:
    if role_id is None:
        return "任意角色"
    role = roles.get(role_id)
    return f"{role.name}({role.code})" if role else role_id


def _minimum_rule_total(rule: SkillMixRuleData) -> int:
    return sum(requirement.count for requirement in rule.requirements)


def _eligible_nurses(
    domain: DomainData,
    date: datetime.date,
    role_id: str | None,
    skill_id: str | None,
) -> list[NurseData]:
    return [
        nurse
        for nurse in domain.nurses
        if _is_available(nurse, date)
        and (role_id is None or role_id in nurse.role_ids)
        and (skill_id is None or skill_id in nurse.skill_ids)
    ]


def _roles_for_shift(rules: list[SkillMixRuleData]) -> set[str | None]:
    return {
        requirement.role_id
        for rule in rules
        for requirement in rule.requirements
    }


def _requirement_pairs(
    rules: list[SkillMixRuleData],
) -> set[tuple[str | None, str | None]]:
    """All distinct (role_id, skill_id) pairs required by a set of rules."""
    return {
        (req.role_id, req.skill_id)
        for rule in rules
        for req in rule.requirements
    }


def explain_infeasibility(domain: DomainData) -> list[str]:
    reasons: list[str] = []
    timeslots = build_timeslots(domain)
    shifts_by_date: dict[datetime.date, list[str]] = defaultdict(list)
    for date, shift_id in timeslots:
        shifts_by_date[date].append(shift_id)

    shift_by_id = {shift.id: shift for shift in domain.shift_templates}
    rules_by_shift: dict[str, list[SkillMixRuleData]] = defaultdict(list)
    for rule in domain.skill_mix_rules:
        rules_by_shift[rule.shift_template_id].append(rule)

    for date, shift_ids in shifts_by_date.items():
        demand_details: list[str] = []
        minimum_total = 0
        for shift_id in shift_ids:
            shift = shift_by_id[shift_id]
            rules = rules_by_shift.get(shift_id, [])
            if not rules:
                continue
            shift_demand = min(_minimum_rule_total(rule) for rule in rules)
            minimum_total += shift_demand
            demand_details.append(f"{shift.name}({shift.code})需{shift_demand}人")

        capacity = 0
        for nurse in domain.nurses:
            if not _is_available(nurse, date):
                continue
            is_eligible = any(
                (role_id is None or role_id in nurse.role_ids)
                and (skill_id is None or skill_id in nurse.skill_ids)
                for shift_id in shift_ids
                for role_id, skill_id in _requirement_pairs(rules_by_shift[shift_id])
            )
            if not is_eligible:
                continue
            capacity += 1 if _enforces_one_shift_per_day(nurse) else len(shift_ids)

        if minimum_total > capacity:
            reasons.append(
                f"{date.isoformat()} 当日至少需要 {minimum_total} 人"
                f"（{'、'.join(demand_details)}），但只有 {capacity} 名可排班护士"
            )

        role_demands: dict[str | None, int] = defaultdict(int)
        if all(len(rules_by_shift[shift_id]) == 1 for shift_id in shift_ids):
            for shift_id in shift_ids:
                for requirement in rules_by_shift[shift_id][0].requirements:
                    role_demands[requirement.role_id] += requirement.count

        for role_id, demand in role_demands.items():
            if demand <= 0:
                continue
            eligible = [
                nurse
                for nurse in domain.nurses
                if _is_available(nurse, date)
                and (role_id is None or role_id in nurse.role_ids)
            ]
            role_capacity = sum(
                1 if _enforces_one_shift_per_day(nurse) else len(shift_ids)
                for nurse in eligible
            )
            if demand > role_capacity:
                reasons.append(
                    f"{date.isoformat()} {_role_label(role_id, domain.roles)} "
                    f"至少需要 {demand} 人，但符合条件的护士最多可提供 {role_capacity} 人班"
                )

    for date, shift_id in timeslots:
        shift = shift_by_id[shift_id]
        impossible_rules: list[str] = []
        for rule in rules_by_shift.get(shift_id, []):
            impossible = False
            for requirement in rule.requirements:
                eligible = _eligible_nurses(
                    domain, date, requirement.role_id, requirement.skill_id
                )
                if requirement.count > len(eligible):
                    impossible = True
                    break
            if not impossible:
                eligible_ids = {
                    nurse.id
                    for requirement in rule.requirements
                    for nurse in _eligible_nurses(
                        domain, date, requirement.role_id, requirement.skill_id
                    )
                }
                impossible = _minimum_rule_total(rule) > len(eligible_ids)
            if impossible:
                impossible_rules.append(rule.name)
        if rules_by_shift.get(shift_id) and len(impossible_rules) == len(
            rules_by_shift[shift_id]
        ):
            reasons.append(
                f"{date.isoformat()} {shift.name}({shift.code}) 的所有技能配比候选规则"
                f"（{'、'.join(impossible_rules)}）均超过符合条件的护士数量"
            )

        # Per-requirement skill shortfall: report when a role+skill demand
        # exceeds the number of eligible nurses (even if other rules could
        # cover it — helps pinpoint the missing skill).
        for rule in rules_by_shift.get(shift_id, []):
            for requirement in rule.requirements:
                eligible = _eligible_nurses(
                    domain, date, requirement.role_id, requirement.skill_id
                )
                if requirement.count > len(eligible):
                    skill_label = ""
                    if requirement.skill_id:
                        skill_name = domain.skills.get(requirement.skill_id)
                        skill_label = f"+技能 {skill_name}" if skill_name else f"+技能 {requirement.skill_id[:8]}"
                    reasons.append(
                        f"{date.isoformat()} {shift.name}({shift.code}) 需要 "
                        f"{requirement.count} 名 {_role_label(requirement.role_id, domain.roles)}"
                        f"{skill_label}，但只有 {len(eligible)} 名符合条件的护士"
                    )

    # Aggregate skill+role capacity: total demand across all timeslots vs
    # total eligible nurse-days. A per-timeslot check (count <= eligible
    # nurses) can pass while the period-wide demand still exceeds what the
    # eligible pool can cover (e.g. 42 SENIOR+BLS slots vs 2 nurses * 14 days).
    demand_by_role_skill: dict[tuple[str | None, str | None], int] = defaultdict(int)
    for _date, shift_id in timeslots:
        for rule in rules_by_shift.get(shift_id, []):
            for requirement in rule.requirements:
                demand_by_role_skill[(requirement.role_id, requirement.skill_id)] += requirement.count

    for (role_id, skill_id), total_demand in demand_by_role_skill.items():
        if total_demand <= 0:
            continue
        eligible_nurses = [
            nurse
            for nurse in domain.nurses
            if nurse.is_available
            and (role_id is None or role_id in nurse.role_ids)
            and (skill_id is None or skill_id in nurse.skill_ids)
        ]
        if not eligible_nurses:
            skill_label = ""
            if skill_id:
                skill_name = domain.skills.get(skill_id)
                skill_label = f"+技能 {skill_name}" if skill_name else f"+技能 {skill_id[:8]}"
            reasons.append(
                f"{_role_label(role_id, domain.roles)}{skill_label} 在排班周期内共需 {total_demand} 班次，"
                f"但没有符合条件的护士"
            )
            continue
        total_capacity = sum(
            domain.period_days - len(nurse.leave_dates)
            if _enforces_one_shift_per_day(nurse)
            else (domain.period_days - len(nurse.leave_dates)) * 3
            for nurse in eligible_nurses
        )
        if total_demand > total_capacity:
            skill_label = ""
            if skill_id:
                skill_name = domain.skills.get(skill_id)
                skill_label = f"+技能 {skill_name}" if skill_name else f"+技能 {skill_id[:8]}"
            reasons.append(
                f"{_role_label(role_id, domain.roles)}{skill_label} "
                f"在排班周期内共需 {total_demand} 班次，"
                f"但符合条件的护士最多可提供 {total_capacity} 班次"
            )

    # Combined demand: total staff needed across ALL timeslots vs total
    # nurse-days of nurses who can fill at least one (role, skill) requirement
    # anywhere. When multiple requirements share the same nurse pool, per-pair
    # checks can pass while the combined demand exceeds the shared capacity.
    total_staff_demand = 0
    for _date, shift_id in timeslots:
        rules = rules_by_shift.get(shift_id, [])
        if not rules:
            continue
        total_staff_demand += min(_minimum_rule_total(rule) for rule in rules)

    if total_staff_demand > 0:
        all_pairs = set()
        for _date, shift_id in timeslots:
            all_pairs |= _requirement_pairs(rules_by_shift.get(shift_id, []))
        versatile_nurses = [
            nurse
            for nurse in domain.nurses
            if nurse.is_available
            and any(
                (role_id is None or role_id in nurse.role_ids)
                and (skill_id is None or skill_id in nurse.skill_ids)
                for role_id, skill_id in all_pairs
            )
        ]
        versatile_capacity = sum(
            domain.period_days - len(nurse.leave_dates)
            if _enforces_one_shift_per_day(nurse)
            else (domain.period_days - len(nurse.leave_dates)) * 3
            for nurse in versatile_nurses
        )
        if total_staff_demand > versatile_capacity:
            reasons.append(
                f"全部班次在排班周期内共需 {total_staff_demand} 人班，"
                f"但能胜任至少一项需求的护士最多可提供 {versatile_capacity} 人班"
            )

    for nurse in domain.nurses:
        if not nurse.contract or not nurse.contract.enforce_shifts_per_period:
            continue
        eligible_dates: set[datetime.date] = set()
        eligible_slot_count = 0
        for date, _shift_id in timeslots:
            if not _is_available(nurse, date):
                continue
            eligible_dates.add(date)
            eligible_slot_count += 1
        leave_days = len(nurse.leave_dates)
        fraction = 1 - leave_days / domain.period_days if domain.period_days else 1
        target = (
            math.ceil(fraction * nurse.contract.shifts_per_period)
            if nurse.contract.max_shifts_per_period is not None
            else math.floor(fraction * nurse.contract.shifts_per_period)
        )
        daily_limit = (
            len(eligible_dates)
            if _enforces_one_shift_per_day(nurse)
            else eligible_slot_count
        )
        if target > daily_limit:
            reasons.append(
                f"护士 {nurse.employee_id} 合约要求 {target} 班，"
                f"但当前日期/角色/请假约束下最多只能排 {daily_limit} 班"
            )

    # Nurse preferences (avoid-type): when capacity is tight, AVOID
    # preferences can further reduce the effective nurse pool for a
    # timeslot. Report timeslots where the number of nurses who can and
    # are willing to work (no AVOID on that slot) falls below demand.
    avoid_by_nurse_date: dict[tuple[str, datetime.date], set[str]] = defaultdict(set)
    for pref in domain.preferences:
        if pref.request_type == "avoid":
            avoid_by_nurse_date[(pref.nurse_id, pref.date)].add(pref.shift_template_id)

    for date, shift_id in timeslots:
        rules = rules_by_shift.get(shift_id, [])
        if not rules:
            continue
        demand = min(_minimum_rule_total(rule) for rule in rules)
        if demand <= 0:
            continue
        willing = 0
        for nurse in domain.nurses:
            if not _is_available(nurse, date):
                continue
            is_eligible = any(
                (role_id is None or role_id in nurse.role_ids)
                and (skill_id is None or skill_id in nurse.skill_ids)
                for role_id, skill_id in _requirement_pairs(rules)
            )
            if not is_eligible:
                continue
            avoided = shift_id in avoid_by_nurse_date.get((nurse.id, date), set())
            if not avoided:
                willing += 1
        if demand > willing:
            shift = shift_by_id[shift_id]
            reasons.append(
                f"{date.isoformat()} {shift.name}({shift.code}) 需要 {demand} 人，"
                f"但排除不期望该班次的护士后只有 {willing} 人可排"
            )

    return reasons
