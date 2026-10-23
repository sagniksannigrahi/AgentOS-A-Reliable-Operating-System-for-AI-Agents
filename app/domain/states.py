"""Explicit task lifecycle state machine.

This module is pure (no I/O) so the rules can be unit tested exhaustively. Persistence code
must call :func:`validate_transition` before writing a new status.
"""

from __future__ import annotations

from enum import StrEnum


class TaskStatus(StrEnum):
    CREATED = "CREATED"
    PLANNING = "PLANNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    RUNNING = "RUNNING"
    VALIDATING = "VALIDATING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    PAUSED = "PAUSED"
    RECOVERY_PENDING = "RECOVERY_PENDING"


S = TaskStatus

TERMINAL_STATES: frozenset[TaskStatus] = frozenset({S.COMPLETED, S.FAILED, S.CANCELLED})

#: Statuses from which a human can ask the system to continue.
RESUMABLE_STATES: frozenset[TaskStatus] = frozenset({S.PAUSED, S.RECOVERY_PENDING})

#: Statuses in which the workflow is actively owned by a worker.
ACTIVE_STATES: frozenset[TaskStatus] = frozenset({S.PLANNING, S.RUNNING, S.VALIDATING})

_ALWAYS = {S.FAILED, S.CANCELLED}

ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    S.CREATED: frozenset({S.PLANNING, *_ALWAYS}),
    S.PLANNING: frozenset({S.AWAITING_APPROVAL, S.RUNNING, S.PAUSED, S.RECOVERY_PENDING, *_ALWAYS}),
    S.AWAITING_APPROVAL: frozenset({S.RUNNING, S.VALIDATING, S.PLANNING, S.RECOVERY_PENDING, *_ALWAYS}),
    S.RUNNING: frozenset({S.VALIDATING, S.AWAITING_APPROVAL, S.PLANNING, S.PAUSED, S.RECOVERY_PENDING, *_ALWAYS}),
    S.VALIDATING: frozenset(
        {
            S.COMPLETED,
            S.RUNNING,
            S.PLANNING,
            S.AWAITING_APPROVAL,
            S.PAUSED,
            S.RECOVERY_PENDING,
            *_ALWAYS,
        }
    ),
    S.PAUSED: frozenset({S.PLANNING, S.RUNNING, S.VALIDATING, S.RECOVERY_PENDING, *_ALWAYS}),
    S.RECOVERY_PENDING: frozenset({S.PLANNING, S.RUNNING, S.VALIDATING, S.AWAITING_APPROVAL, *_ALWAYS}),
    S.COMPLETED: frozenset(),
    S.FAILED: frozenset(),
    S.CANCELLED: frozenset(),
}


class InvalidTransitionError(ValueError):
    def __init__(self, current: TaskStatus, target: TaskStatus, reason: str = "") -> None:
        self.current = current
        self.target = target
        msg = f"invalid task transition {current} -> {target}"
        super().__init__(f"{msg}: {reason}" if reason else msg)


class CompletionGuardError(InvalidTransitionError):
    """Raised when COMPLETED is requested without passing acceptance criteria."""


def can_transition(current: TaskStatus, target: TaskStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


def validate_transition(
    current: TaskStatus,
    target: TaskStatus,
    *,
    criteria_passed: bool | None = None,
) -> None:
    """Raise if ``current -> target`` is not allowed.

    ``COMPLETED`` additionally requires ``criteria_passed=True``: the caller must prove that
    every required acceptance criterion passed. This is checked here so no code path can mark a
    task complete by accident.
    """
    if current == target:
        raise InvalidTransitionError(current, target, "no-op transition")
    if not can_transition(current, target):
        raise InvalidTransitionError(current, target)
    if target == S.COMPLETED and criteria_passed is not True:
        raise CompletionGuardError(current, target, "acceptance criteria have not all passed")


def is_terminal(status: TaskStatus) -> bool:
    return status in TERMINAL_STATES
