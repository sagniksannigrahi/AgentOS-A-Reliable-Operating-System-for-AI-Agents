"""Enumerations shared by persistence, orchestration and the API."""

from __future__ import annotations

from enum import StrEnum


class RunStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"  # workflow reached END (task completed/failed/cancelled cleanly)
    PAUSED = "PAUSED"  # workflow interrupted waiting for a human
    FAILED = "FAILED"  # run crashed with an error that was recorded
    ABANDONED = "ABANDONED"  # lease expired / worker died; superseded by a recovery run
    CANCELLED = "CANCELLED"


class RunReason(StrEnum):
    INITIAL = "initial"
    APPROVAL = "approval"
    RESUME = "resume"
    RECOVERY = "recovery"


class AgentRole(StrEnum):
    PLANNER = "planner"
    CODER = "coder"
    TESTER = "tester"
    SECURITY_AUDITOR = "security_auditor"
    SUPERVISOR = "supervisor"
    SINGLE_AGENT = "single_agent"


class AgentRunStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"


class ToolCallStatus(StrEnum):
    INTENT = "INTENT"  # recorded before execution (write-ahead)
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    DENIED = "DENIED"
    ABANDONED = "ABANDONED"  # intent from a dead run, outcome unknown
    RECONCILED = "RECONCILED"  # intent from a dead run, outcome verified after the fact


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class ApprovalKind(StrEnum):
    PLAN = "plan"
    TOOL = "tool"
    SECURITY_ESCALATION = "security_escalation"
    CONFLICT = "conflict"


class UsageSource(StrEnum):
    PROVIDER_REPORTED = "provider_reported"
    ESTIMATED = "estimated"


class EventType(StrEnum):
    TASK_CREATED = "task.created"
    TASK_STATUS_CHANGED = "task.status_changed"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    TASK_CANCELLED = "task.cancelled"
    CANCEL_REQUESTED = "task.cancel_requested"
    RUN_QUEUED = "run.queued"
    RUN_STARTED = "run.started"
    RUN_FINISHED = "run.finished"
    RUN_ABANDONED = "run.abandoned"
    PLAN_GENERATED = "plan.generated"
    PLAN_POLICY_CHECKED = "plan.policy_checked"
    AGENT_STARTED = "agent.started"
    AGENT_FINISHED = "agent.finished"
    AGENT_FAILED = "agent.failed"
    TOOL_REQUESTED = "tool.requested"
    TOOL_ALLOWED = "tool.allowed"
    TOOL_DENIED = "tool.denied"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    TOOL_RECONCILED = "tool.reconciled"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_DECIDED = "approval.decided"
    TEST_EXECUTED = "test.executed"
    SECURITY_REVIEWED = "security.reviewed"
    SUPERVISOR_DECISION = "supervisor.decision"
    RETRY_INITIATED = "retry.initiated"
    REPLAN_INITIATED = "replan.initiated"
    LOOP_DETECTED = "loop.detected"
    CONFLICT_DETECTED = "conflict.detected"
    BUDGET_EXCEEDED = "budget.exceeded"
    CHECKPOINT_WRITTEN = "checkpoint.written"
    CHECKPOINT_RESTORED = "checkpoint.restored"
    RECOVERY_STARTED = "recovery.started"
    ERROR = "error.detected"
    FAULT_INJECTED = "fault.injected"
    ARTIFACT_CREATED = "artifact.created"
    LLM_CALL = "llm.call"
