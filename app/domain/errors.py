"""Error taxonomy used for failure classification and retry decisions."""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class FailureClass(StrEnum):
    TRANSIENT = "transient"  # retry with backoff (timeouts, 5xx, rate limits)
    INVALID_OUTPUT = "invalid_output"  # agent produced unparseable/invalid output
    TEST_FAILURE = "test_failure"  # code is wrong; coder may retry with feedback
    INFRASTRUCTURE = "infrastructure"  # sandbox/test harness broken, not the code
    POLICY_VIOLATION = "policy_violation"  # blocked by security policy; never retried blindly
    BUDGET_EXCEEDED = "budget_exceeded"
    LOOP_DETECTED = "loop_detected"
    CONFLICT = "conflict"
    CANCELLED = "cancelled"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"  # DB/LLM down after retries
    FATAL = "fatal"


class AgentOSError(Exception):
    failure_class: FailureClass = FailureClass.FATAL
    retryable: bool = False

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_record(self) -> dict[str, Any]:
        return {
            "type": type(self).__name__,
            "failure_class": self.failure_class.value,
            "retryable": self.retryable,
            "message": self.message,
            "details": self.details,
        }


class TransientError(AgentOSError):
    failure_class = FailureClass.TRANSIENT
    retryable = True


class LLMTimeoutError(TransientError):
    pass


class LLMUnavailableError(AgentOSError):
    failure_class = FailureClass.DEPENDENCY_UNAVAILABLE
    retryable = False


class InvalidAgentOutputError(AgentOSError):
    failure_class = FailureClass.INVALID_OUTPUT
    retryable = True


class PolicyViolationError(AgentOSError):
    failure_class = FailureClass.POLICY_VIOLATION


class ToolDeniedError(PolicyViolationError):
    pass


class ToolExecutionError(AgentOSError):
    failure_class = FailureClass.INFRASTRUCTURE
    retryable = True


class BudgetExceededError(AgentOSError):
    failure_class = FailureClass.BUDGET_EXCEEDED


class LoopDetectedError(AgentOSError):
    failure_class = FailureClass.LOOP_DETECTED


class TaskCancelledError(AgentOSError):
    failure_class = FailureClass.CANCELLED


class LeaseLostError(AgentOSError):
    """This worker no longer owns the run (lease expired or fencing token changed)."""

    failure_class = FailureClass.INFRASTRUCTURE


class ReconciliationRequiredError(AgentOSError):
    """A side effect may or may not have happened; a human has to decide."""

    failure_class = FailureClass.INFRASTRUCTURE


class SimulatedCrash(BaseException):
    """Raised by fault injection to emulate a worker process dying.

    It derives from BaseException so ordinary ``except Exception`` handlers cannot swallow it,
    which mirrors a real crash: no cleanup code runs and the run's lease is left behind.
    """


def classify(exc: BaseException) -> FailureClass:
    if isinstance(exc, AgentOSError):
        return exc.failure_class
    if isinstance(exc, TimeoutError):
        return FailureClass.TRANSIENT
    return FailureClass.FATAL


def error_record(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, AgentOSError):
        return exc.to_record()
    from app.security.redaction import redact_text

    return {
        "type": type(exc).__name__,
        "failure_class": classify(exc).value,
        "retryable": isinstance(exc, TimeoutError),
        "message": redact_text(str(exc))[:2000],
        "details": {},
    }
