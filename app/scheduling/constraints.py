"""CP-SAT constraint builders — ported from roster-wizard's logic.py.

Each function is pure: it takes the CpModel, decision vars, and domain data,
and adds constraints. No DB access, no side effects beyond the model.

This mirrors roster-wizard's RosterGenerator methods:
  _create_shift_decision_vars       -> create_shift_decision_vars
  _create_previous_shift_decision_vars -> add_previous_period_fix
  _exclude_leave_dates               -> add_leave_constraints
  _enforce_one_shift_per_day         -> add_one_shift_per_day
  _like_preferences_as_alternatives  -> add_like_preference_alternatives
  _enforce_shifts_per_roster         -> add_shifts_per_period
  _enforce_balanced_shifts           -> add_balanced_shifts
  _enforce_skill_mix_rules           -> add_skill_mix_rules
  _enforce_invalid_shift_sequences   -> add_shift_sequence_rules
  _enforce_staff_numbers             -> add_staff_count_limits
  _maximise_staff_requests           -> set_objective
"""

from __future__ import annotations

import datetime
import math
from collections import defaultdict
from weakref import WeakKeyDictionary

from ortools.sat.python import cp_model

from app.scheduling.domain import (
    SOFT_CONSTRAINT_WEIGHT,
    DomainData,
    NurseData,
    SkillMixRuleData,
)

# Type alias for the decision variable dict
# Key: (nurse_id, role_id, date, shift_template_id) -> BoolVar
ShiftVars = dict[tuple[str, str, datetime.date, str], cp_model.IntVar]


# ──────────────────────────────────────────────────────────────
# Timeslot lookup: which (date, shift_template_id) combos exist
# ──────────────────────────────────────────────────────────────
def build_timeslots(
    domain: DomainData,
) -> list[tuple[datetime.date, str]]:
    """Build the list of (date, shift_template_id) slots for the period.

    A shift template runs on a date if its day_number (1-based cycle position
    of the date) is in the template's day_numbers set. We use ISO weekday (1=Mon..7=Sun).
    """
    timeslots: list[tuple[datetime.date, str]] = []
    for date in domain.dates:
        day_num = date.isoweekday()  # Mon=1..Sun=7
        for st in domain.shift_templates:
            if day_num in st.day_numbers:
                timeslots.append((date, st.id))
    return timeslots


def build_extended_timeslots(
    domain: DomainData,
) -> list[tuple[datetime.date, str]]:
    """Timeslots including previous period (for sequence-rule continuity)."""
    timeslots: list[tuple[datetime.date, str]] = []
    for date in domain.extended_dates:
        day_num = date.isoweekday()
        for st in domain.shift_templates:
            if day_num in st.day_numbers:
                timeslots.append((date, st.id))
    return timeslots


# ──────────────────────────────────────────────────────────────
# Decision variables
# ──────────────────────────────────────────────────────────────
def create_shift_decision_vars(
    model: cp_model.CpModel,
    domain: DomainData,
) -> ShiftVars:
    """Create boolean decision vars: shift_vars[(nurse_id, role_id, date, shift_id)].

    Only created for nurses who have the role (nurse.role_ids contains role_id),
    and for dates in the current period. Previous-period vars are fixed separately.
    """
    timeslots = build_timeslots(domain)
    vars: ShiftVars = {}
    for date, shift_id in timeslots:
        for nurse in domain.nurses:
            for role_id in nurse.role_ids:
                vars[(nurse.id, role_id, date, shift_id)] = model.new_bool_var(
                    f"s_n{short(nurse.id)}_r{short(role_id)}_d{date.isoformat()}_t{short(shift_id)}"
                )
    return vars


def add_previous_period_fix(
    model: cp_model.CpModel,
    domain: DomainData,
    vars: ShiftVars,
) -> None:
    """Fix variables for the previous roster period to their known assignments.

    This ensures shift-sequence rules span the period boundary correctly
    (e.g., if a nurse worked Night on the last day of the previous period,
    they cannot work Early on the first day of the new period).

    Creates new BoolVars for previous-period dates and fixes them to 1 or 0
    based on the recorded previous_assignments.
    """
    # Build a lookup: (nurse_id, date) -> {shift_template_id: (role_id, assigned)}
    prev_map: dict[tuple[str, datetime.date], dict[str, tuple[str, bool]]] = defaultdict(dict)
    for pa in domain.previous_assignments:
        prev_map[(pa.nurse_id, pa.date)][pa.shift_template_id] = (pa.role_id, True)

    ext_timeslots = build_extended_timeslots(domain)
    prev_period_dates = set(domain.extended_dates) - set(domain.dates)

    for date, shift_id in ext_timeslots:
        if date not in prev_period_dates:
            continue
        for nurse in domain.nurses:
            for role_id in nurse.role_ids:
                key = (nurse.id, role_id, date, shift_id)
                # Skip if already created (shouldn't be, since create_shift_decision_vars
                # only covers the current period)
                if key in vars:
                    continue
                var = model.new_bool_var(
                    f"s_n{short(nurse.id)}_r{short(role_id)}_d{date.isoformat()}_t{short(shift_id)}_prev"
                )
                vars[key] = var
                # Determine if this nurse had this shift on this date
                assigned = shift_id in prev_map.get((nurse.id, date), {})
                # If assigned, fix to 1 only if role matches; else 0
                if assigned:
                    _role, _ = prev_map[(nurse.id, date)][shift_id]
                    if _role == role_id:
                        model.add(var == 1)
                    else:
                        model.add(var == 0)
                else:
                    model.add(var == 0)


# ──────────────────────────────────────────────────────────────
# B3-11: Pinned assignments (local re-solve)
# ──────────────────────────────────────────────────────────────
def add_pinned_constraints(
    model: cp_model.CpModel,
    domain: DomainData,
    vars: ShiftVars,
) -> None:
    """Force pinned assignments to be kept in the solution (B3-11).

    Matches on (nurse_id, date) so a stored role_id mismatch cannot
    silently skip a pin. All other shift options for the same (nurse,
    date) pair are excluded by the one-shift-per-day constraint.
    """
    if not domain.pinned_assignments:
        return

    pinned_map: dict[tuple[str, datetime.date], tuple[str, str]] = {
        (a.nurse_id, a.date): (a.role_id, a.shift_template_id)
        for a in domain.pinned_assignments
    }

    for key, var in vars.items():
        nurse_id, role_id, date, shift_id = key
        pinned = pinned_map.get((nurse_id, date))
        if pinned is not None:
            pinned_role_id, pinned_shift_id = pinned
            is_pinned_role = role_id == pinned_role_id
            model.add(
                var == (1 if is_pinned_role and shift_id == pinned_shift_id else 0)
            )


# ──────────────────────────────────────────────────────────────
# Leave constraints
# ──────────────────────────────────────────────────────────────
def add_leave_constraints(
    model: cp_model.CpModel,
    domain: DomainData,
    vars: ShiftVars,
) -> None:
    """Nurses on leave cannot work any shift that day."""
    leave_by_date: dict[datetime.date, set[str]] = defaultdict(set)
    nurses_by_id = {nurse.id: nurse for nurse in domain.nurses}
    for nurse in domain.nurses:
        for leave_date in nurse.leave_dates:
            leave_by_date[leave_date].add(nurse.id)

    shift_ids = {st.id for st in domain.shift_templates}
    for date, nurse_ids in leave_by_date.items():
        if date not in domain.dates:
            continue
        for nurse_id in nurse_ids:
            nurse = nurses_by_id[nurse_id]
            for role_id in nurse.role_ids:
                for shift_id in shift_ids:
                    key = (nurse_id, role_id, date, shift_id)
                    if key not in vars:
                        continue
                    model.add(vars[key] == 0)


# ──────────────────────────────────────────────────────────────
# Shared indicator variables
# ──────────────────────────────────────────────────────────────
_indicator_memo_by_model: WeakKeyDictionary[
    cp_model.CpModel, dict[tuple[str, datetime.date, str], cp_model.IntVar]
] = WeakKeyDictionary()


def _works_on_shift_var(
    model: cp_model.CpModel,
    vars: ShiftVars,
    nurse: NurseData,
    date: datetime.date,
    shift_id: str,
) -> cp_model.IntVar | None:
    """Return a memoized BoolVar that is 1 iff the nurse works `shift_id`."""
    memo = _indicator_memo_by_model.setdefault(model, {})
    memo_key = (nurse.id, date, shift_id)
    if memo_key in memo:
        return memo[memo_key]

    shift_vars = [
        vars[(nurse.id, role_id, date, shift_id)]
        for role_id in nurse.role_ids
        if (nurse.id, role_id, date, shift_id) in vars
    ]
    if not shift_vars:
        return None

    works = model.new_bool_var(
        f"works_{short(nurse.id)}_{date.isoformat()}_{short(shift_id)}"
    )
    model.add(sum(shift_vars) >= 1).OnlyEnforceIf(works)
    model.add(sum(shift_vars) == 0).OnlyEnforceIf(works.Not())
    memo[memo_key] = works
    return works


# ──────────────────────────────────────────────────────────────
# One shift per day
# ──────────────────────────────────────────────────────────────
def add_one_shift_per_day(
    model: cp_model.CpModel,
    domain: DomainData,
    vars: ShiftVars,
) -> None:
    """At most one shift per day per nurse (if contract.enforce_one_shift_per_day)."""
    shift_ids = {st.id for st in domain.shift_templates}
    for date in domain.dates:
        for nurse in domain.nurses:
            contract = nurse.contract
            if contract and not contract.enforce_one_shift_per_day:
                continue
            day_vars = [
                vars[(nurse.id, role_id, date, shift_id)]
                for role_id in nurse.role_ids
                for shift_id in shift_ids
                if (nurse.id, role_id, date, shift_id) in vars
            ]
            if day_vars:
                model.add(sum(day_vars) <= 1)


def add_like_preference_alternatives(
    model: cp_model.CpModel,
    domain: DomainData,
    vars: ShiftVars,
) -> None:
    """Treat same-day LIKE preferences for a nurse as mutually exclusive choices."""
    candidate_vars: dict[tuple[str, datetime.date], list[cp_model.IntVar]] = defaultdict(list)
    like_keys = {
        (preference.nurse_id, preference.date, preference.shift_template_id)
        for preference in domain.preferences
        if preference.request_type == "like"
    }
    for key, var in vars.items():
        nurse_id, _role_id, date, shift_id = key
        if (nurse_id, date, shift_id) in like_keys and date in domain.dates:
            candidate_vars[(nurse_id, date)].append(var)

    for (_nurse_id, _date), day_vars in candidate_vars.items():
        model.add(sum(day_vars) <= 1)


# ──────────────────────────────────────────────────────────────
# Shifts per period
# ──────────────────────────────────────────────────────────────
def _compute_target_shifts(nurse: NurseData, period_days: int) -> int:
    """Compute the target number of shifts for this period.

    Adjusts by leave fraction: if a nurse is on leave for L days out of N,
    their target shifts reduce proportionally. Matches roster-wizard's
    _get_shifts_per_roster.
    """
    contract = nurse.contract
    if not contract:
        return 0
    leave_days = len(nurse.leave_dates)
    work_fraction = 1 - (leave_days / period_days) if period_days > 0 else 1
    target = work_fraction * contract.shifts_per_period
    if contract.max_shifts_per_period is not None:
        return math.ceil(target)
    return math.floor(target)


def add_shifts_per_period(
    model: cp_model.CpModel,
    domain: DomainData,
    vars: ShiftVars,
) -> dict[str, cp_model.IntVar]:
    """Minimize deviations from the target number of shifts per nurse."""
    penalties: dict[str, cp_model.IntVar] = {}
    shift_ids = {st.id for st in domain.shift_templates}
    for nurse in domain.nurses:
        contract = nurse.contract
        if not contract or not contract.enforce_shifts_per_period:
            continue
        period_vars = [
            vars[(nurse.id, role_id, date, shift_id)]
            for role_id in nurse.role_ids
            for date in domain.dates
            for shift_id in shift_ids
            if (nurse.id, role_id, date, shift_id) in vars
        ]
        if not period_vars:
            continue
        target = _compute_target_shifts(nurse, domain.period_days)
        penalty = model.new_int_var(0, max(abs(target), len(period_vars)), f"shift_target_diff_{short(nurse.id)}")
        model.add(sum(period_vars) - target <= penalty)
        model.add(target - sum(period_vars) <= penalty)
        penalties[nurse.id] = penalty
    return penalties


# ──────────────────────────────────────────────────────────────
# Balanced shifts (first half vs second half)
# ──────────────────────────────────────────────────────────────
def add_balanced_shifts(
    model: cp_model.CpModel,
    domain: DomainData,
    vars: ShiftVars,
    *,
    penalties: dict[str, cp_model.IntVar] | None = None,
) -> dict[str, cp_model.IntVar]:
    """Minimize deviations from an even first/second-half distribution."""
    penalties = {} if penalties is None else penalties
    shift_ids = {st.id for st in domain.shift_templates}
    half = domain.period_days // 2
    first_half_dates = domain.dates[:half]
    for nurse in domain.nurses:
        contract = nurse.contract
        if not contract or not contract.enforce_balanced or not contract.enforce_shifts_per_period:
            continue
        # Exclude leave dates
        avail_dates = [d for d in first_half_dates if d not in nurse.leave_dates]
        first_half_vars = [
            vars[(nurse.id, role_id, date, shift_id)]
            for role_id in nurse.role_ids
            for date in avail_dates
            for shift_id in shift_ids
            if (nurse.id, role_id, date, shift_id) in vars
        ]
        if not first_half_vars:
            continue
        target = _compute_target_shifts(nurse, domain.period_days)
        penalty = penalties.get(nurse.id)
        if penalty is None:
            penalty = model.new_int_var(
                0,
                max(abs(target // 2), len(first_half_vars)),
                f"balanced_shift_diff_{short(nurse.id)}",
            )
            penalties[nurse.id] = penalty
        model.add(sum(first_half_vars) - target // 2 <= penalty)
        model.add(target // 2 - sum(first_half_vars) <= penalty)
    return penalties


# ──────────────────────────────────────────────────────────────
# Staff count limits per timeslot
# ──────────────────────────────────────────────────────────────
def add_staff_count_limits(
    model: cp_model.CpModel,
    domain: DomainData,
    vars: ShiftVars,
) -> None:
    """Min/max staff per timeslot, derived from skill mix rule totals."""
    # For each shift_template, compute min/max total staff from its skill mix rules
    sm_by_shift: dict[str, list[int]] = defaultdict(list)
    for rule in domain.skill_mix_rules:
        total = sum(req.count for req in rule.requirements)
        sm_by_shift[rule.shift_template_id].append(total)

    for date, shift_id in build_timeslots(domain):
        totals = sm_by_shift.get(shift_id, [])
        if not totals:
            continue
        min_staff = min(totals)
        max_staff = max(totals)
        slot_vars = [
            vars[(nurse.id, role_id, date, shift_id)]
            for nurse in domain.nurses
            for role_id in nurse.role_ids
            if (nurse.id, role_id, date, shift_id) in vars
        ]
        if slot_vars:
            model.add(sum(slot_vars) >= min_staff)
            model.add(sum(slot_vars) <= max_staff)


# ──────────────────────────────────────────────────────────────
# Skill mix rules (the complex one — intermediate BoolVars + OnlyEnforceIf)
# ──────────────────────────────────────────────────────────────
def add_skill_mix_rules(
    model: cp_model.CpModel,
    domain: DomainData,
    vars: ShiftVars,
) -> None:
    """Enforce skill mix: for each timeslot, pick ONE skill mix rule and enforce it.

    Uses the intermediate-BoolVar + OnlyEnforceIf pattern from roster-wizard:
    - Create a BoolVar per (timeslot, rule)
    - Exactly one rule BoolVar must be true per timeslot
    - When rule BoolVar is true, the role-count constraint is enforced
    """
    # Group skill mix rules by shift template
    rules_by_shift: dict[str, list[SkillMixRuleData]] = defaultdict(list)
    for rule in domain.skill_mix_rules:
        rules_by_shift[rule.shift_template_id].append(rule)

    timeslots = build_timeslots(domain)
    for date, shift_id in timeslots:
        rules = rules_by_shift.get(shift_id, [])
        if not rules:
            continue
        # One BoolVar per rule for this timeslot
        rule_vars = [
            model.new_bool_var(f"sm_{short(shift_id)}_{short(rule.id)}_{date.isoformat()}")
            for rule in rules
        ]
        # Exactly one rule chosen
        model.add(sum(rule_vars) == 1)
        # Enforce each rule's role counts when selected
        for rule_idx, rule in enumerate(rules):
            rule_var = rule_vars[rule_idx]
            for req in rule.requirements:
                # Nurses with the required role (or any role), optionally
                # filtered by skill.
                slot_role_vars = [
                    vars[(nurse.id, role_id, date, shift_id)]
                    for nurse in domain.nurses
                    for role_id in nurse.role_ids
                    if (
                        req.role_id is None or role_id == req.role_id
                    )
                    and (req.skill_id is None or req.skill_id in nurse.skill_ids)
                    and (nurse.id, role_id, date, shift_id) in vars
                ]
                if slot_role_vars:
                    model.add(sum(slot_role_vars) == req.count).OnlyEnforceIf(rule_var)


# ──────────────────────────────────────────────────────────────
# Shift sequence rules (forbidden patterns across consecutive days)
# ──────────────────────────────────────────────────────────────
def add_shift_sequence_rules(
    model: cp_model.CpModel,
    domain: DomainData,
    vars: ShiftVars,
) -> None:
    """Forbid shift sequences (e.g., Night → Early next day).

    For each rule, for each nurse (whose roles match), for each starting date:
    - Build a BoolVar "is this forbidden pattern happening here"
    - The pattern matches if each day in the pattern has the specified shift
      (or, for None steps, NO shift that day)
    - Forbid by requiring the pattern-match BoolVar is 0.

    Adapted from roster-wizard's _enforce_invalid_shift_sequences, simplified:
    instead of the OnlyEnforceIf intermediate-var approach, we directly forbid
    the conjunction of the pattern's per-day conditions.
    """
    for rule in domain.shift_sequence_rules:
        steps = rule.steps
        if not steps:
            continue
        pattern_length = max(s.position for s in steps) + 1
        # For each nurse whose roles match
        for nurse in domain.nurses:
            if rule.applies_to_role_ids and not (
                set(nurse.role_ids) & rule.applies_to_role_ids
            ):
                continue
            # For each starting date in the extended range (so boundary cases covered)
            for start_date in domain.extended_dates:
                # Check the pattern window fits
                window = [
                    start_date + datetime.timedelta(days=i)
                    for i in range(pattern_length)
                ]
                # Skip if window extends beyond extended range
                if window[-1] > domain.extended_dates[-1]:
                    continue

                # Build condition: all steps match
                # A "match" BoolVar = AND of per-step conditions
                step_match_vars: list[cp_model.LinearExpr] = []
                for step in steps:
                    day = window[step.position]
                    if day not in domain.extended_dates:
                        step_match_vars = []
                        break
                    if step.shift_template_id is None:
                        # Day off: no shift assigned that day for this nurse
                        works = _works_on_day_var(model, domain, vars, nurse, day)
                        if works is None:
                            step_match_vars = []
                            break
                        step_match_vars.append(works.Not())
                    else:
                        # Specific shift assigned that day for this nurse
                        assigned = _works_on_shift_var(
                            model, vars, nurse, day, step.shift_template_id
                        )
                        if assigned is None:
                            step_match_vars = []
                            break
                        step_match_vars.append(assigned)

                if not step_match_vars:
                    continue
                # The forbidden pattern match = AND of all step matches
                # Forbid it: at least one step must NOT match
                model.add(sum(step_match_vars) <= len(step_match_vars) - 1)


# ──────────────────────────────────────────────────────────────
# Minimum rest hours between consecutive shifts
# ──────────────────────────────────────────────────────────────

def _rest_hours_between(
    end_a: datetime.time,
    start_b: datetime.time,
) -> float:
    """Compute rest hours between shift A ending and shift B starting.

    Shifts A and B are on consecutive days (A on day N, B on day N+1).
    Rest always crosses midnight: rest = (24 - end_a) + start_b.
    Example: L ends 22:00 day N, N starts 23:00 day N+1 → 25h.
    """
    end_min = end_a.hour * 60 + end_a.minute
    start_min = start_b.hour * 60 + start_b.minute
    return ((24 * 60 - end_min) + start_min) / 60.0


def _works_on_day_var(
    model: cp_model.CpModel,
    domain: DomainData,
    vars: ShiftVars,
    nurse: NurseData,
    date: datetime.date,
) -> cp_model.IntVar | None:
    """Return a BoolVar that is 1 iff the nurse works any shift on `date`.

    Uses the existing decision vars. Only covers dates that have timeslots
    in the extended range (previous-period vars included).
    """
    memo = _indicator_memo_by_model.setdefault(model, {})
    memo_key = (nurse.id, date, "")
    if memo_key in memo:
        return memo[memo_key]

    day_vars = [
        vars[(nurse.id, role_id, date, shift_id)]
        for role_id in nurse.role_ids
        for shift_id in (st.id for st in domain.shift_templates)
        if (nurse.id, role_id, date, shift_id) in vars
    ]
    if not day_vars:
        return None
    works = model.new_bool_var(f"works_{short(nurse.id)}_{date.isoformat()}")
    model.add(sum(day_vars) >= 1).OnlyEnforceIf(works)
    model.add(sum(day_vars) == 0).OnlyEnforceIf(works.Not())
    memo[memo_key] = works
    return works


def add_min_rest_hours(
    model: cp_model.CpModel,
    domain: DomainData,
    vars: ShiftVars,
) -> None:
    """Enforce minimum rest hours between consecutive-day shifts.

    For each nurse with a contract.min_rest_hours > 0, for each pair of
    consecutive dates (day, day+1), if a shift on `day` ending at T1 and a
    shift on `day+1` starting at T2 would leave fewer rest hours than the
    contract minimum, the two shifts cannot both be assigned.
    """
    # Build shift_id -> ShiftTemplateData lookup

    for nurse in domain.nurses:
        contract = nurse.contract
        if not contract or contract.min_rest_hours <= 0:
            continue
        min_rest = contract.min_rest_hours

        for i in range(len(domain.extended_dates) - 1):
            day = domain.extended_dates[i]
            next_day = domain.extended_dates[i + 1]
            # Only constrain pairs where at least one day is in the current period
            if day not in domain.dates and next_day not in domain.dates:
                continue

            # Find incompatible (shift_a on day, shift_b on next_day) pairs
            for shift_a in domain.shift_templates:
                # Check shift_a runs on `day`
                if day.isoweekday() not in shift_a.day_numbers:
                    continue
                for shift_b in domain.shift_templates:
                    if next_day.isoweekday() not in shift_b.day_numbers:
                        continue
                    rest = _rest_hours_between(shift_a.end_time, shift_b.start_time)
                    if rest >= min_rest:
                        continue  # enough rest, no constraint needed

                    # Forbid: nurse works shift_a on day AND shift_b on next_day
                    works_a = _works_on_shift_var(
                        model, vars, nurse, day, shift_a.id
                    )
                    works_b = _works_on_shift_var(
                        model, vars, nurse, next_day, shift_b.id
                    )
                    if works_a is None or works_b is None:
                        continue
                    model.add_bool_or([works_a.Not(), works_b.Not()])


# ──────────────────────────────────────────────────────────────
# Maximum consecutive working days
# ──────────────────────────────────────────────────────────────

def add_max_consecutive_days(
    model: cp_model.CpModel,
    domain: DomainData,
    vars: ShiftVars,
) -> None:
    """Enforce maximum consecutive working days.

    For each nurse with contract.max_consecutive_days > 0, in any sliding
    window of (max_consecutive_days + 1) consecutive days, at least one day
    must be off (i.e., total worked days ≤ max_consecutive_days).

    Uses helper BoolVars "works on day" built from the decision vars.
    """
    for nurse in domain.nurses:
        contract = nurse.contract
        if not contract or contract.max_consecutive_days <= 0:
            continue
        max_consec = contract.max_consecutive_days
        window_size = max_consec + 1

        # Build works-on-day vars for all extended dates (so boundary works)
        works_vars: dict[datetime.date, cp_model.IntVar] = {}
        for date in domain.extended_dates:
            wv = _works_on_day_var(model, domain, vars, nurse, date)
            if wv is not None:
                works_vars[date] = wv

        # Slide window over extended dates
        for i in range(len(domain.extended_dates) - window_size + 1):
            window = domain.extended_dates[i : i + window_size]
            # Only constrain windows that overlap the current period
            if not any(d in domain.dates for d in window):
                continue
            window_vars: list[cp_model.IntVar] = []
            for date in window:
                work_var = works_vars.get(date)
                if work_var is None:
                    break
                window_vars.append(work_var)
            else:
                model.add(sum(window_vars) <= max_consec)


# ──────────────────────────────────────────────────────────────
# Objective: maximize satisfied nurse preferences
# ──────────────────────────────────────────────────────────────
def set_objective(
    model: cp_model.CpModel,
    domain: DomainData,
    vars: ShiftVars,
    penalties: dict[str, cp_model.IntVar] | None = None,
) -> None:
    """Maximize preferences and minimize deviations from roster targets.

    AVOID preferences subtract -priority when assigned (i.e., reward for not assigning).
    LIKE preferences for the same nurse/date are alternative choices. Priority
    always dominates, while equal-priority candidates use a deterministic
    shift-timing tie-break.
    """
    shifts_by_id = {shift.id: shift for shift in domain.shift_templates}
    ordered_shift_ids = sorted(
        shifts_by_id,
        key=lambda shift_id: (
            shifts_by_id[shift_id].start_time,
            shifts_by_id[shift_id].end_time,
            shifts_by_id[shift_id].code,
            shifts_by_id[shift_id].name,
            shift_id,
        ),
    )
    tie_break_scale = max(1, len(ordered_shift_ids))
    shift_rank = {shift_id: rank for rank, shift_id in enumerate(ordered_shift_ids)}

    # Build preference weight lookup: (nurse_id, date, shift_id) -> signed weight
    pref_weight: dict[tuple[str, datetime.date, str], int] = {}
    for p in domain.preferences:
        rank = shift_rank[p.shift_template_id]
        if p.request_type == "like":
            weight = p.priority * tie_break_scale + (tie_break_scale - rank)
        else:
            weight = -p.priority * tie_break_scale
        pref_weight[(p.nurse_id, p.date, p.shift_template_id)] = weight

    objective_terms = []
    for (nurse_id, _role_id, date, shift_id), var in vars.items():
        if date not in domain.dates:
            continue  # previous-period vars are fixed, not part of objective
        weight = pref_weight.get((nurse_id, date, shift_id), 0)
        if weight != 0:
            objective_terms.append(weight * var)

    if penalties:
        objective_terms.extend(
            -SOFT_CONSTRAINT_WEIGHT * tie_break_scale * penalty
            for penalty in penalties.values()
        )
    if objective_terms:
        model.maximize(sum(objective_terms))
    else:
        model.maximize(0)


# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────
def short(uuid_str: str) -> str:
    """Shorten a UUID string for variable naming (CP-SAT var names are bounded)."""
    return uuid_str.replace("-", "")[:12]
