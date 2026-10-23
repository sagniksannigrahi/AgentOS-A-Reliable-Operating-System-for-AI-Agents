"""Supervisor decision policy.

A pure function of the workflow state and configured limits. It never calls an LLM, so no agent
output can talk it out of a budget, a policy decision or a safety rule. Every decision carries a
reason that is recorded in the execution history.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.supervisor.conflicts import detect_change_conflicts

ROUTES = ("approval", "execute", "review", "test", "replan", "finalize", "fail", "cancel")


@dataclass(frozen=True)
class Limits:
    supervised: bool = True
    security_review: bool = True
    loop_detection: bool = True
    max_attempts: int = 3
    max_replans: int = 1
    max_steps: int = 60
    deadline_s: float = 900.0
    max_tokens: int | None = None
    require_plan_approval: bool = False
    max_infra_retries: int = 2
    max_flaky_reruns: int = 1


@dataclass
class Decision:
    route: str
    reason: str
    updates: dict[str, Any] = field(default_factory=dict)
    events: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)  # (type, message, data)


def _fail(reason: str, failure_class: str, *, pause: bool = False, **details: Any) -> Decision:
    return Decision(
        "fail",
        reason,
        {"error": {"message": reason, "failure_class": failure_class, "pause": pause, "details": details}},
    )


def _approval(kind: str, reason: str, key: str, resume_status: str, payload: dict[str, Any] | None = None) -> Decision:
    return Decision(
        "approval",
        reason,
        {
            "pending_approval": {
                "kind": kind,
                "reason": reason,
                "key": key,
                "resume_status": resume_status,
                "payload": payload or {},
                "approval_id": None,
            }
        },
    )


def _code_subtasks(state: dict[str, Any]) -> list[str]:
    plan = state.get("plan") or {}
    return [s["id"] for s in plan.get("subtasks", []) if s.get("kind", "code") == "code"]


def _retry(state: dict[str, Any], lim: Limits, feedback: str, kind: str) -> Decision | None:
    """Re-run all code subtasks with feedback, if attempts remain."""
    subs = dict(state.get("subtasks") or {})
    code = _code_subtasks(state)
    if not code or not lim.supervised:
        return None
    attempt = max(subs[s]["attempt"] for s in code)
    if attempt >= lim.max_attempts:
        return None
    for sid, st in subs.items():
        if sid in code or st["status"] != "done":
            subs[sid] = {
                **st,
                "status": "pending",
                "attempt": st["attempt"] + 1 if sid in code else st["attempt"],
                "feedback": feedback[:4000],
            }
        else:
            subs[sid] = {**st, "status": "pending"}  # dependents of retried code re-run too
    return Decision(
        "execute",
        f"retrying code subtasks (attempt {attempt + 1}/{lim.max_attempts}) after {kind}",
        {
            "subtasks": subs,
            "phase": "executing",
            "retries": state.get("retries", 0) + 1,
            "security": None,
            "security_approved": False,
            "test_report": None,
        },
        [("retry.initiated", f"retry attempt {attempt + 1} after {kind}", {"kind": kind, "attempt": attempt + 1})],
    )


def _replan(state: dict[str, Any], lim: Limits, reason: str) -> Decision | None:
    if not lim.supervised or state.get("replans", 0) >= lim.max_replans:
        return None
    return Decision(
        "replan",
        f"replanning: {reason}",
        {"replans": state.get("replans", 0) + 1, "replan_reason": reason, "phase": "replanning"},
        [("replan.initiated", f"replanning: {reason}", {"reason": reason})],
    )


def decide(
    state: dict[str, Any], lim: Limits, *, now: float, cancel_requested: bool, workspace_fingerprint: str
) -> Decision:
    steps = int(state.get("step_count", 0))
    if cancel_requested:
        return Decision("cancel", "cancellation requested by user")
    if steps >= lim.max_steps:
        d = _fail(f"step budget exhausted ({steps}/{lim.max_steps} graph steps)", "budget_exceeded")
        d.events.append(("budget.exceeded", d.reason, {"budget": "graph_steps", "used": steps, "limit": lim.max_steps}))
        return d
    elapsed = now - float(state.get("started_at", now))
    if elapsed > lim.deadline_s:
        d = _fail(f"task deadline exceeded ({elapsed:.0f}s > {lim.deadline_s:.0f}s)", "budget_exceeded")
        d.events.append(("budget.exceeded", d.reason, {"budget": "deadline_s", "used": round(elapsed, 1)}))
        return d
    tokens = int(state.get("tokens_used", 0))
    if lim.max_tokens and tokens > lim.max_tokens:
        d = _fail(f"token budget exhausted ({tokens} > {lim.max_tokens})", "budget_exceeded")
        d.events.append(("budget.exceeded", d.reason, {"budget": "tokens", "used": tokens, "limit": lim.max_tokens}))
        return d
    if state.get("error"):
        return Decision("fail", state["error"].get("message", "error"))
    if state.get("pending_approval"):
        return Decision("approval", "waiting for a human decision")

    phase = state.get("phase")
    plan = state.get("plan") or {}
    pv = int(state.get("plan_version", 0))

    if phase == "planned":
        if not lim.supervised:
            return Decision("execute", "unsupervised: executing plan without review", {"phase": "executing"})
        if plan.get("needs_clarification") and not state.get("plan_approved"):
            q = plan.get("clarification_question") or "the request is ambiguous"
            return _approval(
                "plan", f"planner needs clarification: {q}", f"plan:{pv}:clarify", "PLANNING", {"question": q}
            )
        review = state.get("plan_review") or {}
        if review.get("verdict") == "block":
            return _replan(state, lim, "plan blocked by security policy") or _fail(
                "plan blocked by security policy", "policy_violation", findings=review.get("findings")
            )
        if (review.get("verdict") == "escalate" or lim.require_plan_approval) and not state.get("plan_approved"):
            return _approval(
                "plan",
                "plan requires human approval"
                + (" (security escalation)" if review.get("verdict") == "escalate" else ""),
                f"plan:{pv}:approve",
                "PLANNING",
                {"findings": review.get("findings", [])},
            )
        return Decision("execute", "plan validated; starting execution", {"phase": "executing"})

    if phase == "executing":
        subs = state.get("subtasks") or {}
        waiting = [s for s, st in subs.items() if st["status"] == "awaiting_approval"]
        if waiting:
            st = subs[waiting[0]]
            return Decision(
                "approval",
                f"subtask {waiting[0]} needs approval for a sensitive operation",
                {
                    "pending_approval": {
                        "kind": "tool",
                        "reason": st.get("approval_reason", "sensitive operation"),
                        "key": None,
                        "approval_id": st.get("approval_id"),
                        "resume_status": "RUNNING",
                        "subtask": waiting[0],
                        "payload": {},
                    }
                },
            )
        failed = [s for s, st in subs.items() if st["status"] == "failed"]
        if failed:
            errs = "; ".join(f"{s}: {(subs[s].get('error') or {}).get('message', 'failed')}" for s in failed)
            if not lim.supervised:
                return _fail(f"subtask failed: {errs}", "agent_failure")
            policy = [s for s in failed if (subs[s].get("error") or {}).get("failure_class") == "policy_violation"]
            if policy and not _code_subtasks(state):
                return _fail(f"policy violation: {errs}", "policy_violation")
            return (
                _retry(state, lim, f"previous attempt failed: {errs}", "agent_failure")
                or _replan(state, lim, f"subtasks kept failing: {errs}")
                or _fail(f"subtasks failed after {lim.max_attempts} attempts: {errs}", "agent_failure")
            )
        if any(st["status"] in ("pending", "running") for st in subs.values()):
            return Decision("execute", "executing next ready subtasks")
        phase = "executed"

    if phase == "executed":
        if lim.supervised and not state.get("conflicts_accepted"):
            conflicts = detect_change_conflicts(plan, state.get("results") or {})
            if conflicts:
                evts = [("conflict.detected", c["detail"], c) for c in conflicts]
                d = _replan(state, lim, "agent conflict: " + "; ".join(c["detail"] for c in conflicts))
                if d is None:
                    d = _approval(
                        "conflict",
                        "agents produced conflicting results; human review required",
                        f"conflict:{pv}:{state.get('retries', 0)}",
                        "RUNNING",
                        {"conflicts": conflicts},
                    )
                d.events = evts + d.events
                d.updates["conflicts"] = [*state.get("conflicts", []), *conflicts]
                return d
        if lim.supervised and lim.security_review:
            return Decision("review", "all subtasks done; running security review", {"phase": "executed"})
        return Decision("test", "all subtasks done; running acceptance tests", {"phase": "executed"})

    if phase == "reviewed":
        sec = state.get("security") or {}
        if sec.get("verdict") == "block":
            findings = "; ".join(
                f"{f['rule']} ({f.get('file') or '-'})"
                for f in sec.get("findings", [])
                if f["severity"] in ("critical", "high")
            )
            evt = (
                "conflict.detected",
                "security auditor blocked the coder's changes; policy takes precedence",
                {"kind": "policy_violation", "findings": findings},
            )
            d = _retry(
                state,
                lim,
                f"SECURITY POLICY VIOLATION - your changes were blocked: {findings}. "
                "They have been rolled back. Implement only what the task requires.",
                "policy_violation",
            )
            if d is not None:
                d.updates["rollback_workspace"] = True  # blocked changes must not survive into the retry
            else:
                d = _fail(f"changes blocked by security policy: {findings}", "policy_violation")
            d.events.insert(0, evt)
            return d
        if sec.get("verdict") == "escalate" and not state.get("security_approved"):
            return _approval(
                "security_escalation",
                "security review escalated the changes for human approval",
                f"security:{pv}:{state.get('retries', 0)}",
                "VALIDATING",
                {"findings": sec.get("findings", [])},
            )
        return Decision("test", "security review passed; running acceptance tests")

    if phase == "tested":
        rep = state.get("test_report") or {}
        if rep.get("outcome") == "passed":
            if rep.get("diff_fingerprint") != workspace_fingerprint:
                return Decision("test", "workspace changed after testing; re-running tests")
            return Decision("finalize", "all acceptance criteria passed")
        if rep.get("flaky") and lim.supervised:
            if state.get("flaky_reruns", 0) < lim.max_flaky_reruns:
                return Decision(
                    "test",
                    "conflicting test outcomes on identical code; re-running tests",
                    {"flaky_reruns": state.get("flaky_reruns", 0) + 1},
                    [
                        (
                            "conflict.detected",
                            "conflicting test outcomes (flaky tests)",
                            {"kind": "conflicting_test_outcomes"},
                        )
                    ],
                )
            return _fail(
                "tests give inconsistent results on identical code; needs human investigation", "conflict", pause=True
            )
        if rep.get("outcome") == "infra_error":
            if state.get("infra_retries", 0) < lim.max_infra_retries:
                return Decision(
                    "test",
                    "test infrastructure error; retrying tests",
                    {"infra_retries": state.get("infra_retries", 0) + 1},
                    [("retry.initiated", "retrying tests after infrastructure error", {"kind": "test_infra"})],
                )
            return _fail("test infrastructure unavailable after retries", "infrastructure", pause=True)
        if not lim.supervised:
            return _fail("acceptance tests failed", "test_failure", criteria=rep.get("criteria"))
        history = state.get("history") or []
        current = (rep.get("diff_fingerprint"), rep.get("failure_signature"))
        if (
            lim.loop_detection
            and sum(1 for h in history[:-1] if (h.get("fingerprint"), h.get("signature")) == current) >= 1
        ):
            d = _fail(
                "loop detected: an attempt produced identical code and the identical test failure as a previous attempt",
                "loop_detected",
                fingerprint=current[0],
                signature=current[1],
            )
            d.events.append(("loop.detected", d.reason, {"kind": "no_progress", "fingerprint": current[0]}))
            return d
        feedback = (
            "Acceptance tests failed. "
            + (rep.get("analysis") or "")
            + " Suggestions: "
            + "; ".join(rep.get("suggestions") or [])
            + " Failing output: "
            + " | ".join(
                c.get("output_tail", "")[-800:] for c in rep.get("commands", []) if c.get("outcome") != "passed"
            )
        )
        return (
            _retry(state, lim, feedback, "test_failure")
            or _replan(state, lim, "acceptance tests kept failing")
            or _fail(f"acceptance tests still failing after {lim.max_attempts} attempts", "test_failure")
        )

    return _fail(f"supervisor reached an unexpected phase '{phase}'", "fatal")
