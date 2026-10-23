"""Typed, versioned workflow state (what LangGraph checkpoints).

Only JSON-serialisable data lives here. Agents receive the minimum context for their job (their
subtask, the acceptance criteria, feedback) rather than the full history of every other agent.
"""

from __future__ import annotations

import time
from typing import Any, TypedDict

STATE_SCHEMA_VERSION = 1


class SubtaskState(TypedDict, total=False):
    status: str  # pending | running | done | failed | awaiting_approval
    attempt: int
    feedback: str | None
    approval_id: str | None
    approval_reason: str | None
    error: dict[str, Any] | None


class WorkflowState(TypedDict, total=False):
    schema_version: int
    task_id: str
    mode: str  # supervised | unsupervised | single_agent
    phase: str  # planned | executing | executed | reviewed | tested | replanning
    route: str
    route_reason: str
    plan: dict[str, Any] | None
    plan_version: int
    plan_review: dict[str, Any] | None
    plan_approved: bool
    replan_reason: str | None
    subtasks: dict[str, SubtaskState]
    results: dict[str, dict[str, Any]]
    code_fingerprint: str
    security: dict[str, Any] | None
    security_approved: bool
    conflicts_accepted: bool
    conflicts: list[dict[str, Any]]
    test_report: dict[str, Any] | None
    history: list[dict[str, Any]]
    pending_approval: dict[str, Any] | None
    approvals: dict[str, str]
    approved_sensitive_paths: list[str]
    rollback_workspace: bool
    injection_flags: list[str]
    step_count: int
    tokens_used: int
    retries: int
    replans: int
    infra_retries: int
    flaky_reruns: int
    test_runs: int
    started_at: float
    error: dict[str, Any] | None
    final: dict[str, Any] | None


def initial_state(task_id: str, mode: str) -> WorkflowState:
    return WorkflowState(
        schema_version=STATE_SCHEMA_VERSION,
        task_id=task_id,
        mode=mode,
        phase="new",
        plan=None,
        plan_version=0,
        plan_review=None,
        plan_approved=False,
        replan_reason=None,
        subtasks={},
        results={},
        code_fingerprint="",
        security=None,
        security_approved=False,
        conflicts_accepted=False,
        conflicts=[],
        test_report=None,
        history=[],
        pending_approval=None,
        approvals={},
        approved_sensitive_paths=[],
        rollback_workspace=False,
        injection_flags=[],
        step_count=0,
        tokens_used=0,
        retries=0,
        replans=0,
        infra_retries=0,
        flaky_reruns=0,
        test_runs=0,
        started_at=time.time(),
        error=None,
        final=None,
    )
