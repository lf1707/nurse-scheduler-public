"""RosterCPModel — orchestrates constraint building and solving.

Port of roster-wizard's RosterGenerator, adapted to in-memory DomainData
and FastAPI/Celery async context. The engine itself is synchronous (CP-SAT
is CPU-bound); callers run it in a thread pool or Celery task.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any, cast

from ortools.sat.python import cp_model

from app.scheduling import constraints
from app.scheduling.domain import (
    AssignmentResult,
    DomainData,
    SolveResult,
)
from app.scheduling.edit_validation import summarize_preferences
from app.scheduling.exceptions import (
    InfeasibleError,
    ModelInvalidError,
    ScheduleCancelledError,
    SolverTimeoutError,
)

log = logging.getLogger(__name__)

# Progress callback: (stage: str, progress: int, message: str) -> None
ProgressCallback = Callable[[str, int, str], None]
StopCallback = Callable[[], bool]


class _StopSearchCallback(cp_model.CpSolverSolutionCallback):
    """Stop CP-SAT cooperatively when the database reports cancellation."""

    def __init__(self, should_stop: StopCallback):
        super().__init__()
        self._should_stop = should_stop

    def on_solution_callback(self) -> None:
        if self._should_stop():
            self.StopSearch()


class SolverConfig:
    """Solver configuration."""

    def __init__(
        self,
        timeout_seconds: int = 120,
        num_workers: int = 8,
    ):
        self.timeout_seconds = timeout_seconds
        self.num_workers = num_workers


class RosterCPModel:
    """Builds and solves the nurse roster CP-SAT model.

    Usage:
        engine = RosterCPModel(domain, config)
        result = engine.solve(progress_cb)
    """

    def __init__(
        self,
        domain: DomainData,
        config: SolverConfig | None = None,
    ):
        self.domain = domain
        self.config = config or SolverConfig()
        self.model = cp_model.CpModel()
        self.solver = cp_model.CpSolver()
        self.solver.parameters.max_time_in_seconds = self.config.timeout_seconds
        self.solver.parameters.num_search_workers = self.config.num_workers
        self.vars: constraints.ShiftVars = {}
        self._result: SolveResult | None = None

    def build_model(self, progress: ProgressCallback | None = None) -> None:
        """Build all constraints on the model."""
        p = progress or (lambda *_: None)

        p("loading", 5, "Creating decision variables")
        self.vars = constraints.create_shift_decision_vars(self.model, self.domain)

        p("loading", 10, "Fixing previous period assignments")
        constraints.add_previous_period_fix(self.model, self.domain, self.vars)

        if self.domain.pinned_assignments:
            p("loading", 12, "Fixing pinned assignments (local re-solve)")
            constraints.add_pinned_constraints(self.model, self.domain, self.vars)

        p("building", 20, "Adding leave constraints")
        constraints.add_leave_constraints(self.model, self.domain, self.vars)

        p("building", 30, "Adding one-shift-per-day")
        constraints.add_one_shift_per_day(self.model, self.domain, self.vars)

        p("building", 40, "Adding shifts-per-period targets")
        period_penalties = constraints.add_shifts_per_period(
            self.model, self.domain, self.vars
        )

        p("building", 50, "Adding balance constraints")
        balance_penalties = constraints.add_balanced_shifts(
            self.model, self.domain, self.vars, penalties=period_penalties
        )

        p("building", 60, "Adding skill mix rules")
        constraints.add_skill_mix_rules(self.model, self.domain, self.vars)

        p("building", 75, "Adding staff count limits")
        constraints.add_staff_count_limits(self.model, self.domain, self.vars)

        p("building", 85, "Adding shift sequence rules")
        constraints.add_shift_sequence_rules(self.model, self.domain, self.vars)

        p("building", 87, "Adding min rest hours")
        constraints.add_min_rest_hours(self.model, self.domain, self.vars)

        p("building", 88, "Adding max consecutive days")
        constraints.add_max_consecutive_days(self.model, self.domain, self.vars)

        p("building", 89, "Adding same-day preference alternatives")
        constraints.add_like_preference_alternatives(self.model, self.domain, self.vars)

        p("building", 90, "Setting objective")
        constraints.set_objective(
            self.model, self.domain, self.vars, penalties=balance_penalties
        )

        p("solving", 95, "Ready to solve")

    def solve(
        self,
        progress: ProgressCallback | None = None,
        should_stop: StopCallback | None = None,
    ) -> SolveResult:
        """Solve the model and return the result.

        Raises:
            InfeasibleError: no feasible schedule exists
            SolverTimeoutError: solver timed out without finding any solution
            ModelInvalidError: model is structurally invalid
        """
        p = progress or (lambda *_: None)
        if not self.vars:
            self.build_model(progress)

        p("solving", 95, "Running CP-SAT solver")
        start = time.monotonic()
        callback = _StopSearchCallback(should_stop) if should_stop else None
        solver_status = self.solver.Solve(self.model, callback)
        elapsed = time.monotonic() - start

        if should_stop and should_stop():
            p("failed", 100, "Cancelled")
            raise ScheduleCancelledError("Schedule generation was cancelled")

        if solver_status == cp_model.MODEL_INVALID:
            p("failed", 100, "Model invalid")
            raise ModelInvalidError("CP-SAT model is invalid")
        if solver_status == cp_model.INFEASIBLE:
            p("failed", 100, "Infeasible")
            raise InfeasibleError("No feasible schedule for the given constraints")
        if solver_status == cp_model.UNKNOWN:
            p("failed", 100, "Timed out without solution")
            raise SolverTimeoutError(
                f"Solver found no solution within {self.config.timeout_seconds}s"
            )
        if solver_status not in (cp_model.FEASIBLE, cp_model.OPTIMAL):
            p("failed", 100, f"Unexpected status: {solver_status}")
            raise InfeasibleError(f"Unexpected solver status: {solver_status}")

        # FEASIBLE or OPTIMAL — extract assignments
        outcome = "optimal" if solver_status == cp_model.OPTIMAL else "feasible"
        assignments = self._extract_assignments()
        preference_stats = self._summarize_preferences(assignments)
        self._result = SolveResult(
            assignments=assignments,
            outcome=outcome,
            objective_value=(
                int(self.solver.ObjectiveValue()) if self.solver.ObjectiveValue() else 0
            ),
            solve_time_seconds=round(elapsed, 3),
            num_nurses=len(self.domain.nurses),
            num_conflicts=self.solver.NumConflicts(),
            num_booleans=self.solver.NumBooleans(),
            preference_stats=preference_stats,
        )
        p("done", 100, f"Solver finished ({outcome})")
        return self._result

    def diagnose_infeasibility_by_relaxation(self) -> list[str]:
        """When explain_infeasibility returns nothing, find which constraint
        group is the bottleneck by selectively relaxing each group and
        re-solving. Returns human-readable reasons.

        Each constraint group is rebuilt into a fresh model with that one
        group omitted. If the relaxed model becomes feasible, the omitted
        group is a contributing bottleneck.
        """
        # Constraint groups to test, in order of likelihood.
        # Each entry: (label, build_fn without that group)
        # We rebuild the full model each time, skipping one group.
        groups: list[tuple[str, list[str]]] = [
            ("技能配比规则（skill mix）", ["skill_mix", "staff_count"]),
            ("班次序列禁排规则（shift sequence）", ["shift_sequence"]),
            ("每班次人数限制（staff count limits）", ["staff_count"]),
            ("护士同日期期望候选班次（preferences）", ["preferences"]),
            ("每天最多一个班次（one shift per day）", ["one_shift_per_day"]),
            ("每周期班次数目标（shifts per period）", ["shifts_per_period"]),
            ("前半/后半平衡（balanced shifts）", ["balanced"]),
            ("最低休息时间（min rest hours）", ["rest_hours"]),
            ("最多连续工作天数（max consecutive days）", ["consecutive_days"]),
        ]

        results: list[str] = []
        for label, skip in groups:
            status = self._solve_relaxed(skip)
            if status in (cp_model.FEASIBLE, cp_model.OPTIMAL):
                results.append(
                    f"移除「{label}」后模型变为可行，说明该约束是导致不可满足的瓶颈之一"
                )
            # else: still infeasible without this group — not the sole bottleneck
        return results

    def _solve_relaxed(self, skip: list[str]) -> int:
        """Build a fresh model skipping specified constraint groups, solve,
        return the CP-SAT status code."""
        model = cp_model.CpModel()
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = min(self.config.timeout_seconds, 10)
        solver.parameters.num_search_workers = min(self.config.num_workers, 4)
        solver.parameters.stop_after_first_solution = True

        vars = constraints.create_shift_decision_vars(model, self.domain)
        constraints.add_previous_period_fix(model, self.domain, vars)
        constraints.add_leave_constraints(model, self.domain, vars)
        if "one_shift_per_day" not in skip:
            constraints.add_one_shift_per_day(model, self.domain, vars)
        if "shifts_per_period" not in skip:
            period_penalties = constraints.add_shifts_per_period(
                model, self.domain, vars
            )
        else:
            period_penalties = {}
        if "balanced" not in skip:
            balance_penalties = constraints.add_balanced_shifts(
                model, self.domain, vars, penalties=period_penalties
            )
        else:
            balance_penalties = period_penalties
        if "skill_mix" not in skip:
            constraints.add_skill_mix_rules(model, self.domain, vars)
        if "staff_count" not in skip:
            constraints.add_staff_count_limits(model, self.domain, vars)
        if "shift_sequence" not in skip:
            constraints.add_shift_sequence_rules(model, self.domain, vars)
        if "rest_hours" not in skip:
            constraints.add_min_rest_hours(model, self.domain, vars)
        if "consecutive_days" not in skip:
            constraints.add_max_consecutive_days(model, self.domain, vars)
        if "preferences" not in skip:
            constraints.add_like_preference_alternatives(model, self.domain, vars)
            constraints.set_objective(
                model, self.domain, vars, penalties=balance_penalties
            )

        return cast(int, solver.Solve(model))

    def _extract_assignments(self) -> list[AssignmentResult]:
        """Read assigned shifts from the solved model."""
        results: list[AssignmentResult] = []
        for (nurse_id, role_id, date, shift_id), var in self.vars.items():
            # Only current-period vars (previous are fixed, not actual assignments)
            if date not in self.domain.dates:
                continue
            if self.solver.Value(var) == 1:
                results.append(
                    AssignmentResult(
                        nurse_id=nurse_id,
                        role_id=role_id,
                        date=date,
                        shift_template_id=shift_id,
                    )
                )
        return results

    def _summarize_preferences(
        self, assignments: list[AssignmentResult]
    ) -> dict[str, Any]:
        return summarize_preferences(self.domain, assignments)

    @property
    def result(self) -> SolveResult | None:
        return self._result
