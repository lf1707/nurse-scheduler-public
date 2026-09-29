"""Scheduling exceptions."""

from __future__ import annotations


class SchedulingError(Exception):
    """Base for scheduling domain errors."""


class InfeasibleError(SchedulingError):
    """No feasible schedule exists for the given constraints."""

    def __init__(
        self,
        message: str = "No feasible schedule found",
        conflicts: list[str] | None = None,
    ):
        super().__init__(message)
        self.conflicts = conflicts or []


class SolverTimeoutError(SchedulingError):
    """Solver did not find a solution within the time limit."""


class ModelInvalidError(SchedulingError):
    """CP-SAT model is invalid (should not happen in production)."""


class ConcurrentGenerationError(SchedulingError):
    """A generation for this tenant is already running."""


class ScheduleCancelledError(SchedulingError):
    """The schedule request was cancelled while solving."""
