import itertools

import pytest

from app.domain.states import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    CompletionGuardError,
    InvalidTransitionError,
    TaskStatus,
    can_transition,
    validate_transition,
)

S = TaskStatus


def test_every_status_has_rules():
    assert set(ALLOWED_TRANSITIONS) == set(TaskStatus)


def test_happy_path_is_valid():
    path = [S.CREATED, S.PLANNING, S.AWAITING_APPROVAL, S.RUNNING, S.VALIDATING]
    for a, b in itertools.pairwise(path):
        validate_transition(a, b)
    validate_transition(S.VALIDATING, S.COMPLETED, criteria_passed=True)


@pytest.mark.parametrize("terminal", sorted(TERMINAL_STATES))
@pytest.mark.parametrize("target", list(TaskStatus))
def test_terminal_states_are_final(terminal, target):
    assert not can_transition(terminal, target)
    with pytest.raises(InvalidTransitionError):
        validate_transition(terminal, target, criteria_passed=True)


def test_completion_requires_passing_criteria():
    with pytest.raises(CompletionGuardError):
        validate_transition(S.VALIDATING, S.COMPLETED)
    with pytest.raises(CompletionGuardError):
        validate_transition(S.VALIDATING, S.COMPLETED, criteria_passed=False)


@pytest.mark.parametrize("source", [S.CREATED, S.PLANNING, S.RUNNING, S.AWAITING_APPROVAL, S.PAUSED])
def test_cannot_complete_without_validation(source):
    with pytest.raises(InvalidTransitionError):
        validate_transition(source, S.COMPLETED, criteria_passed=True)


def test_skipping_planning_is_invalid():
    with pytest.raises(InvalidTransitionError):
        validate_transition(S.CREATED, S.RUNNING)


def test_noop_transition_rejected():
    with pytest.raises(InvalidTransitionError):
        validate_transition(S.RUNNING, S.RUNNING)


@pytest.mark.parametrize("source", [s for s in TaskStatus if s not in TERMINAL_STATES])
def test_any_live_state_can_fail_or_cancel(source):
    assert can_transition(source, S.FAILED)
    assert can_transition(source, S.CANCELLED)


def test_recovery_pending_can_resume_into_work():
    for target in (S.PLANNING, S.RUNNING, S.VALIDATING, S.AWAITING_APPROVAL):
        assert can_transition(S.RECOVERY_PENDING, target)
