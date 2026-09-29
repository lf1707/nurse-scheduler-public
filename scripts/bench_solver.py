"""Benchmark the CP-SAT solver across realistic roster sizes.

Stage 3 acceptance: measure solve time for 100/200/500 nurses × 14/21/28 days
and confirm 500 nurses / 28 days < 120s.

Runs inside the api container:
    docker compose exec api python /app/scripts/bench_solver.py

Supply/demand is balanced per case so the hard `== shifts_per_period` constraint
can be satisfied:
    demand = period_days * shifts_per_day * staff_per_shift
    supply = num_nurses * shifts_per_period
We pick staff_per_shift so that supply == demand.
"""

from __future__ import annotations

import datetime
import sys
import time

# Ensure the repo root (containing the `app` package) is importable when this
# script is run as `python /app/scripts/bench_solver.py` from inside the container.
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.scheduling.domain import (
    ContractData,
    DomainData,
    NurseData,
    RoleData,
    ShiftTemplateData,
    SkillMixRequirementData,
    SkillMixRuleData,
    make_uuid,
)
from app.scheduling.engine import RosterCPModel, SolverConfig
from app.scheduling.exceptions import InfeasibleError, SolverTimeoutError

# (nurses, days) grid — Stage 3 acceptance grid
CASES: list[tuple[int, int]] = [
    (n, d) for n in (100, 200, 500) for d in (14, 21, 28)
]

SHIFTS_PER_DAY = 2  # early + night, both run every day
SHIFTS_PER_PERIOD = 5  # each nurse targets 5 shifts/period


def _balance(num_nurses: int, period_days: int) -> tuple[int, int, int, int, int]:
    """Find (nurses, rn_per, lv_per, n_rn, n_lv) with exact supply==demand.

    Constraints (all integer-equal, no slack):
        nurses * SHIFTS_PER_PERIOD == period_days * SHIFTS_PER_DAY * (rn_per + lv_per)
        n_rn * SHIFTS_PER_PERIOD == period_days * SHIFTS_PER_DAY * rn_per
        n_lv * SHIFTS_PER_PERIOD == period_days * SHIFTS_PER_DAY * lv_per
        n_rn + n_lv == nurses
    Pick the smallest staff_per_shift so nurses >= num_nurses, then split
    roles to land as close to 60% RN as the integer math allows.
    """
    slots = period_days * SHIFTS_PER_DAY
    target = SHIFTS_PER_PERIOD
    # staff_per_shift = nurses * target / slots. Increase nurses until integer.
    while (num_nurses * target) % slots != 0:
        num_nurses += 1
    staff_per_shift = (num_nurses * target) // slots

    # Split staff_per_shift into rn_per + lv_per. RN majority; require
    # slots * rn_per divisible by target so n_rn is integer.
    rn_per = staff_per_shift * 3 // 5  # ~60%
    rn_per = max(1, rn_per)
    # adjust rn_per down until slots*rn_per % target == 0 (so n_rn divides)
    while rn_per > 1 and (slots * rn_per) % target != 0:
        rn_per -= 1
    lv_per = staff_per_shift - rn_per
    if lv_per < 1:
        # can't have LV=0; bump staff_per_shift by increasing nurses
        num_nurses += slots // target or 1
        return _balance(num_nurses, period_days)

    n_rn = (slots * rn_per) // target
    n_lv = num_nurses - n_rn
    # sanity: n_lv * target == slots * lv_per (holds by total-balance + rn-balance)
    assert n_lv * target == slots * lv_per, (
        f"balance failed: n_lv={n_lv} target={target} slots={slots} lv_per={lv_per}"
    )
    return num_nurses, rn_per, lv_per, n_rn, n_lv


def build_domain(num_nurses: int, period_days: int) -> DomainData:
    """Build a balanced synthetic domain so supply == demand exactly."""
    role_rn = RoleData(id=make_uuid(), code="RN", name="RN")
    role_lv = RoleData(id=make_uuid(), code="LV", name="LV")

    shift_e = ShiftTemplateData(
        id=make_uuid(), code="E", name="Early",
        start_time=datetime.time(7, 0), end_time=datetime.time(15, 0),
        duration_hours=8.0, day_numbers=frozenset(range(1, 8)),
    )
    shift_n = ShiftTemplateData(
        id=make_uuid(), code="N", name="Night",
        start_time=datetime.time(15, 0), end_time=datetime.time(23, 0),
        duration_hours=8.0, day_numbers=frozenset(range(1, 8)),
    )

    num_nurses, rn_per, lv_per, n_rn, n_lv = _balance(num_nurses, period_days)

    nurses: list[NurseData] = []
    for i in range(n_rn + n_lv):
        role_id = role_rn.id if i < n_rn else role_lv.id
        n = NurseData(
            id=make_uuid(), employee_id=f"N{i}", first_name="J", last_name="D",
            is_available=True, role_ids=[role_id], skill_ids=[],
            leave_dates=frozenset(),
        )
        n.contract = ContractData(
            nurse_id=n.id, shifts_per_period=SHIFTS_PER_PERIOD,
            max_shifts_per_period=SHIFTS_PER_PERIOD + 2, min_rest_hours=11,
            max_consecutive_days=5, enforce_balanced=False,
            enforce_shifts_per_period=True, enforce_one_shift_per_day=True,
        )
        nurses.append(n)

    sm_e = SkillMixRuleData(
        id=make_uuid(), name="E mix", shift_template_id=shift_e.id, priority=0,
        requirements=(
            SkillMixRequirementData(role_id=role_rn.id, count=rn_per),
            SkillMixRequirementData(role_id=role_lv.id, count=lv_per),
        ),
    )
    sm_n = SkillMixRuleData(
        id=make_uuid(), name="N mix", shift_template_id=shift_n.id, priority=0,
        requirements=(
            SkillMixRequirementData(role_id=role_rn.id, count=rn_per),
            SkillMixRequirementData(role_id=role_lv.id, count=lv_per),
        ),
    )

    period_start = datetime.date(2026, 9, 1)
    dates = [period_start + datetime.timedelta(days=i) for i in range(period_days)]
    return DomainData(
        tenant_id="bench", period_start=period_start, period_days=period_days,
        nurses=nurses, roles={role_rn.id: role_rn, role_lv.id: role_lv},
        shift_templates=[shift_e, shift_n], skill_mix_rules=[sm_e, sm_n],
        shift_sequence_rules=[], preferences=[], previous_assignments=[],
        dates=dates,
        extended_dates=[
            (period_start - datetime.timedelta(days=period_days) + datetime.timedelta(days=i))
            for i in range(period_days * 2)
        ],
    )


def run_one(num_nurses: int, period_days: int, timeout: int = 120) -> dict:
    """Solve one case, return a result row."""
    domain = build_domain(num_nurses, period_days)
    engine = RosterCPModel(domain, SolverConfig(timeout_seconds=timeout, num_workers=8))
    t0 = time.monotonic()
    try:
        result = engine.solve()
        elapsed = time.monotonic() - t0
        return {
            "nurses": num_nurses, "days": period_days,
            "assignments": len(result.assignments),
            "outcome": result.outcome,
            "solve_s": round(result.solve_time_seconds, 2),
            "wall_s": round(elapsed, 2),
            "booleans": result.num_booleans,
            "status": "OK",
        }
    except InfeasibleError:
        return {"nurses": num_nurses, "days": period_days, "status": "INFEASIBLE",
                "solve_s": round(time.monotonic() - t0, 2)}
    except SolverTimeoutError:
        return {"nurses": num_nurses, "days": period_days, "status": "TIMEOUT",
                "solve_s": round(time.monotonic() - t0, 2)}


def main() -> int:
    print(f"{'nurses':>7} {'days':>5} {'status':>11} {'solve_s':>9} {'wall_s':>8} {'assigns':>9} {'bools':>9}")
    print("-" * 64)
    failures = 0
    for n, d in CASES:
        row = run_one(n, d)
        if row["status"] == "OK":
            print(f"{row['nurses']:>7} {row['days']:>5} {'OK':>11} "
                  f"{row.get('solve_s','-'):>9} {row.get('wall_s','-'):>8} "
                  f"{row.get('assignments','-'):>9} {row.get('booleans','-'):>9}")
        else:
            failures += 1
            print(f"{row['nurses']:>7} {row['days']:>5} {row['status']:>11} "
                  f"{row.get('solve_s','-'):>9} {'-':>8} {'-':>9} {'-':>9}")

    print("-" * 64)
    # Stage 3 acceptance gate: 500 nurses / 28 days < 120s
    gate = run_one(500, 28)
    if gate["status"] == "OK" and gate.get("solve_s", 999) < 120:
        print(f"ACCEPTANCE: 500 nurses/28 days solved in {gate['solve_s']}s < 120s ✓")
    else:
        print(f"ACCEPTANCE: 500 nurses/28 days FAILED — {gate}")
        failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
