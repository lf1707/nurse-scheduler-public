"""Standalone schedule-edit validation (B3-10).

Validates a proposed set of assignments against the same rules the CP-SAT
solver enforces, but using plain data instead of decision variables so the
API layer can validate batch edits without re-running the solver.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, time, timedelta
from typing import Any

from app.scheduling.domain import (
    ContractData,
    DomainData,
    NursePreferenceData,
    SkillMixRuleData,
)


@dataclass(frozen=True, slots=True)
class ProposedAssignment:
    """A single (nurse, date, shift) cell in the proposed schedule."""

    nurse_id: str
    role_id: str | None
    date: date
    shift_template_id: str


@dataclass(slots=True)
class ValidationResult:
    hard_violations: list[dict[str, Any]] = field(default_factory=list)
    soft_violations: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.hard_violations

    @property
    def needs_override(self) -> bool:
        return bool(self.soft_violations)


def _rest_hours_between(end_a: time, start_b: time) -> float:
    """Rest hours from shift A ending on day N to shift B starting on day N+1.

    Always crosses midnight: rest = (24 - end_hours) + start_hours.
    Example: L ends 22:00 day N, N starts 23:00 day N+1 → (24-22)+23 = 25h.
    """
    end_min = end_a.hour * 60 + end_a.minute
    start_min = start_b.hour * 60 + start_b.minute
    return ((24 * 60 - end_min) + start_min) / 60.0


def _shift_lookup(domain: DomainData) -> dict[str, Any]:
    return {st.id: st for st in domain.shift_templates}


def summarize_preferences(domain: DomainData, assignments: list[Any]) -> dict[str, int]:
    """Summarize preference outcomes and mark matching assignments.

    This mirrors the solver's soft preference objective. It accepts either
    solver assignment results or ORM assignments that expose the same identity
    fields, so manual edits continue from the same statistics definition.
    """
    assigned_keys = {
        (assignment.nurse_id, assignment.date, assignment.shift_template_id)
        for assignment in assignments
    }
    like_groups: dict[tuple[str, date], set[str]] = {}
    avoid_preferences: list[NursePreferenceData] = []
    for preference in domain.preferences:
        if preference.request_type == "like":
            like_groups.setdefault(
                (preference.nurse_id, preference.date), set()
            ).add(preference.shift_template_id)
        else:
            avoid_preferences.append(preference)

    satisfied = 0
    for (_nurse_id, _date), shift_ids in like_groups.items():
        if any(
            (_nurse_id, _date, shift_id) in assigned_keys
            for shift_id in shift_ids
        ):
            satisfied += 1
    for preference in avoid_preferences:
        assigned = (
            preference.nurse_id,
            preference.date,
            preference.shift_template_id,
        ) in assigned_keys
        satisfied += int(not assigned)

    preferences_by_assignment = {
        (preference.nurse_id, preference.date, preference.shift_template_id): preference
        for preference in domain.preferences
    }
    for assignment in assignments:
        assignment_preference: NursePreferenceData | None = preferences_by_assignment.get(
            (assignment.nurse_id, assignment.date, assignment.shift_template_id)
        )
        if assignment_preference and hasattr(assignment, "satisfied_preference"):
            assignment.satisfied_preference = assignment_preference.request_type == "like"

    loaded = len(like_groups) + len(avoid_preferences)
    return {
        "loaded": loaded,
        "satisfied": satisfied,
        "violated": loaded - satisfied,
    }


def validate_assignments(
    assignments: list[ProposedAssignment],
    domain: DomainData,
    period_start: date,
    period_days: int,
    affected_slots: set[tuple[date, str]] | None = None,
) -> ValidationResult:
    """Run all hard + soft constraint checks on a proposed assignment set."""
    result = ValidationResult()
    nurses_by_id = {n.id: n for n in domain.nurses}
    shifts_by_id = _shift_lookup(domain)
    period_end = period_start + timedelta(days=period_days - 1)

    by_nurse_date: dict[str, dict[date, list[ProposedAssignment]]] = defaultdict(
        lambda: defaultdict(list)
    )
    by_date_shift: dict[tuple[date, str], list[ProposedAssignment]] = defaultdict(list)
    nurse_shift_counts: dict[str, int] = defaultdict(int)
    for a in assignments:
        by_nurse_date[a.nurse_id][a.date].append(a)
        by_date_shift[(a.date, a.shift_template_id)].append(a)
        nurse_shift_counts[a.nurse_id] += 1

    for a in assignments:
        nurse = nurses_by_id.get(a.nurse_id)
        shift = shifts_by_id.get(a.shift_template_id)
        if nurse is None:
            result.hard_violations.append({
                "code": "nurse_not_found",
                "message": "护士不存在",
                "nurse_id": a.nurse_id,
            })
            continue
        if not nurse.is_available:
            result.hard_violations.append({
                "code": "nurse_inactive",
                "message": "护士已停用",
                "nurse_id": a.nurse_id,
            })

        if a.role_id and a.role_id not in nurse.role_ids:
            result.hard_violations.append({
                "code": "role_not_qualified",
                "message": "护士不具备该角色资格",
                "nurse_id": a.nurse_id,
                "role_id": a.role_id,
            })

        if shift is None:
            result.hard_violations.append({
                "code": "shift_not_found",
                "message": "班次模板不存在",
                "shift_template_id": a.shift_template_id,
            })
            continue
        if a.date.isoweekday() not in shift.day_numbers:
            result.hard_violations.append({
                "code": "shift_not_scheduled",
                "message": "该日无此班次",
                "nurse_id": a.nurse_id,
                "date": a.date.isoformat(),
                "shift_template_id": a.shift_template_id,
            })

        if a.date < period_start or a.date > period_end:
            result.hard_violations.append({
                "code": "outside_period",
                "message": "日期超出排班周期",
                "date": a.date.isoformat(),
            })

        if a.date in nurse.leave_dates:
            result.hard_violations.append({
                "code": "nurse_on_leave",
                "message": "护士当日请假",
                "nurse_id": a.nurse_id,
                "date": a.date.isoformat(),
            })

    for nurse_id, nurse_days in by_nurse_date.items():
        for day, cells in nurse_days.items():
            if len(cells) > 1:
                result.hard_violations.append({
                    "code": "multiple_shifts_same_day",
                    "message": "同日多班次",
                    "nurse_id": nurse_id,
                    "date": day.isoformat(),
                    "count": len(cells),
                })

    for nurse in domain.nurses:
        contract: ContractData | None = nurse.contract
        if not contract:
            continue
        dates = sorted(by_nurse_date.get(nurse.id, {}).keys())

        if contract.min_rest_hours > 0 and len(dates) >= 2:
            for i in range(len(dates) - 1):
                today, tomorrow = dates[i], dates[i + 1]
                if (tomorrow - today).days != 1:
                    continue
                for a in by_nurse_date[nurse.id][today]:
                    shift_a = shifts_by_id.get(a.shift_template_id)
                    if shift_a is None:
                        continue
                    for b in by_nurse_date[nurse.id][tomorrow]:
                        shift_b = shifts_by_id.get(b.shift_template_id)
                        if shift_b is None:
                            continue
                        rest = _rest_hours_between(shift_a.end_time, shift_b.start_time)
                        if rest < contract.min_rest_hours:
                            result.hard_violations.append({
                                "code": "min_rest_violation",
                                "message": f"休息时间不足 {contract.min_rest_hours}h",
                                "nurse_id": nurse.id,
                                "date": today.isoformat(),
                                "next_date": tomorrow.isoformat(),
                                "rest_hours": rest,
                            })

        if contract.max_consecutive_days > 0:
            max_consec = contract.max_consecutive_days
            run = 0
            prev: date | None = None
            for day in dates:
                if prev and (day - prev).days == 1:
                    run += 1
                else:
                    run = 1
                if run > max_consec:
                    result.hard_violations.append({
                        "code": "max_consecutive_violation",
                        "message": f"连续工作超过 {max_consec} 天",
                        "nurse_id": nurse.id,
                        "date": day.isoformat(),
                    })
                prev = day

        if contract.max_shifts_per_period is not None and contract.enforce_shifts_per_period:
            count = nurse_shift_counts.get(nurse.id, 0)
            if count > contract.max_shifts_per_period:
                result.hard_violations.append({
                    "code": "max_shifts_violation",
                    "message": f"当前 {count} 个班次超过合同上限 {contract.max_shifts_per_period}",
                    "nurse_id": nurse.id,
                    "count": count,
                    "max": contract.max_shifts_per_period,
                })

    rules_by_shift: dict[str, list[SkillMixRuleData]] = defaultdict(list)
    for rule in domain.skill_mix_rules:
        rules_by_shift[rule.shift_template_id].append(rule)

    for (day, shift_id), cells in by_date_shift.items():
        if affected_slots is not None and (day, shift_id) not in affected_slots:
            continue
        rules = rules_by_shift.get(shift_id, [])
        if not rules:
            continue
        role_counts: dict[str, int] = defaultdict(int)
        for cell in cells:
            if cell.role_id:
                role_counts[cell.role_id] += 1

        any_rule_ok = False
        for rule in rules:
            counts = [
                sum(
                    (nurse := nurses_by_id.get(cell.nurse_id)) is not None
                    and cell.role_id is not None
                    and (r.role_id is None or cell.role_id == r.role_id)
                    and (
                        r.skill_id is None
                        or r.skill_id in nurse.skill_ids
                    )
                    for cell in cells
                )
                for r in rule.requirements
            ]
            if all(
                count >= requirement.count
                for count, requirement in zip(counts, rule.requirements, strict=True)
            ):
                any_rule_ok = True
                break
        if not any_rule_ok and rules:
            result.soft_violations.append({
                "code": "skill_mix_not_met",
                "message": "技能配比不满足",
                "date": day.isoformat(),
                "shift_template_id": shift_id,
                "role_counts": dict(role_counts),
                "required": [
                    {"role_id": r.role_id, "count": r.count, "skill_id": r.skill_id}
                    for r in rules[0].requirements
                ],
            })

    return result
