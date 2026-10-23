"""Unit tests for the pure supervisor policy, conflict detection, retry backoff and cost math."""

from __future__ import annotations

import random
import time
from decimal import Decimal

import pytest

from app.llm.pricing import estimate_cost
from app.supervisor.conflicts import detect_change_conflicts, detect_claim_conflicts
from app.supervisor.policy import Limits, decide
from app.supervisor.retry import backoff_delay

PLAN = {
    "summary": "s",
    "subtasks": [
        {"id": "a", "kind": "code", "depends_on": [], "target_files": ["a.py"]},
        {"id": "b", "kind": "code", "depends_on": [], "target_files": ["b.py"]},
        {"id": "c", "kind": "analysis", "depends_on": ["a", "b"], "target_files": []},
    ],
}


def state(**kw):
    base = {
        "phase": "planned",
        "plan": PLAN,
        "plan_version": 1,
        "step_count": 3,
        "started_at": time.time(),
        "subtasks": {s["id"]: {"status": "pending", "attempt": 1} for s in PLAN["subtasks"]},
        "results": {},
        "history": [],
        "plan_review": {"verdict": "pass", "findings": []},
    }
    base.update(kw)
    return base


def d(st, lim=Limits(), cancel=False, fp="fp"):
    return decide(st, lim, now=time.time(), cancel_requested=cancel, workspace_fingerprint=fp)


def test_cancel_wins_over_everything():
    assert d(state(pending_approval={"x": 1}), cancel=True).route == "cancel"


def test_step_budget():
    r = d(state(step_count=60), Limits(max_steps=60))
    assert r.route == "fail" and r.updates["error"]["failure_class"] == "budget_exceeded"
    assert r.events[0][0] == "budget.exceeded"


def test_deadline():
    r = d(state(started_at=time.time() - 1000), Limits(deadline_s=10))
    assert r.route == "fail" and "deadline" in r.reason


def test_token_budget():
    assert d(state(tokens_used=5001), Limits(max_tokens=5000)).route == "fail"


def test_plan_paths():
    assert d(state()).route == "execute"
    assert d(state(), Limits(require_plan_approval=True)).route == "approval"
    assert d(state(plan_approved=True), Limits(require_plan_approval=True)).route == "execute"
    blocked = state(plan_review={"verdict": "block", "findings": []})
    assert d(blocked).route == "replan"
    assert d({**blocked, "replans": 1}).route == "fail"
    clar = state(plan={**PLAN, "needs_clarification": True, "clarification_question": "which?"})
    r = d(clar)
    assert r.route == "approval" and r.updates["pending_approval"]["kind"] == "plan"


def test_execution_progress_and_dependency_wait():
    subs = {
        "a": {"status": "done", "attempt": 1},
        "b": {"status": "pending", "attempt": 1},
        "c": {"status": "pending", "attempt": 1},
    }
    assert d(state(phase="executing", subtasks=subs)).route == "execute"
    done = {k: {"status": "done", "attempt": 1} for k in subs}
    assert d(state(phase="executing", subtasks=done)).route == "review"
    assert d(state(phase="executing", subtasks=done), Limits(supervised=False)).route == "test"


def test_failed_subtask_retry_then_replan_then_fail():
    subs = {
        "a": {"status": "failed", "attempt": 1, "error": {"message": "x"}},
        "b": {"status": "done", "attempt": 1},
        "c": {"status": "pending", "attempt": 1},
    }
    r = d(state(phase="executing", subtasks=subs))
    assert (
        r.route == "execute"
        and r.updates["subtasks"]["a"]["attempt"] == 2
        and r.updates["subtasks"]["a"]["status"] == "pending"
    )
    exhausted = {k: {**v, "attempt": 3} for k, v in subs.items()}
    assert d(state(phase="executing", subtasks=exhausted)).route == "replan"
    assert d(state(phase="executing", subtasks=exhausted, replans=1)).route == "fail"
    assert d(state(phase="executing", subtasks=subs), Limits(supervised=False)).route == "fail"


def test_conflicts_trigger_replan():
    results = {
        "a": {"output": {"changes": [{"path": "x.py", "sha256": "1"}]}, "decisions": {}},
        "b": {"output": {"changes": [{"path": "x.py", "sha256": "2"}]}, "decisions": {}},
    }
    done = {k: {"status": "done", "attempt": 1} for k in ("a", "b", "c")}
    r = d(state(phase="executing", subtasks=done, results=results))
    assert r.route == "replan" and any(e[0] == "conflict.detected" for e in r.events)
    r = d(state(phase="executing", subtasks=done, results=results, replans=1))
    assert r.route == "approval" and r.updates["pending_approval"]["kind"] == "conflict"


def test_security_block_retries_with_rollback():
    sec = {"verdict": "block", "findings": [{"severity": "critical", "rule": "credential_pattern:aws", "file": "c.py"}]}
    subs = {k: {"status": "done", "attempt": 1} for k in ("a", "b", "c")}
    r = d(state(phase="reviewed", security=sec, subtasks=subs))
    assert r.route == "execute" and r.updates["rollback_workspace"] is True
    assert "SECURITY POLICY VIOLATION" in r.updates["subtasks"]["a"]["feedback"]
    exhausted = {k: {"status": "done", "attempt": 3} for k in subs}
    assert d(state(phase="reviewed", security=sec, subtasks=exhausted)).route == "fail"


def test_security_escalation_needs_human():
    sec = {"verdict": "escalate", "findings": []}
    assert d(state(phase="reviewed", security=sec)).route == "approval"
    assert d(state(phase="reviewed", security=sec, security_approved=True)).route == "test"


def test_tests_passed_but_stale_fingerprint_reruns():
    rep = {"outcome": "passed", "diff_fingerprint": "old", "criteria": []}
    assert d(state(phase="tested", test_report=rep), fp="new").route == "test"
    assert d(state(phase="tested", test_report={**rep, "diff_fingerprint": "new"}), fp="new").route == "finalize"


def test_no_progress_loop_detection():
    rep = {"outcome": "failed", "diff_fingerprint": "f1", "failure_signature": "s1", "commands": []}
    subs = {k: {"status": "done", "attempt": 2} for k in ("a", "b", "c")}
    hist = [{"fingerprint": "f1", "signature": "s1"}, {"fingerprint": "f1", "signature": "s1"}]
    r = d(state(phase="tested", test_report=rep, subtasks=subs, history=hist))
    assert r.route == "fail" and r.updates["error"]["failure_class"] == "loop_detected"
    # different code -> legitimate retry, not a loop
    r = d(
        state(
            phase="tested", test_report=rep, subtasks=subs, history=[{"fingerprint": "f0", "signature": "s1"}, hist[1]]
        )
    )
    assert r.route == "execute"
    # loop detection disabled
    r = d(state(phase="tested", test_report=rep, subtasks=subs, history=hist), Limits(loop_detection=False))
    assert r.route == "execute"


def test_flaky_and_infra_handling():
    flaky = {"outcome": "failed", "flaky": True, "diff_fingerprint": "f"}
    assert d(state(phase="tested", test_report=flaky)).route == "test"
    r = d(state(phase="tested", test_report=flaky, flaky_reruns=1))
    assert r.route == "fail" and r.updates["error"]["pause"] is True
    infra = {"outcome": "infra_error", "diff_fingerprint": "f"}
    assert d(state(phase="tested", test_report=infra)).route == "test"
    r = d(state(phase="tested", test_report=infra, infra_retries=2))
    assert r.route == "fail" and r.updates["error"]["pause"] is True


def test_existing_error_routes_to_fail():
    assert d(state(error={"message": "boom"})).route == "fail"


def test_decide_is_pure():
    st = state()
    snapshot = repr(st)
    d(st)
    assert repr(st) == snapshot


# ------------------------------------------------------------------------------- conflicts


def test_dependent_subtasks_may_edit_same_file():
    plan = {"subtasks": [{"id": "a", "depends_on": []}, {"id": "b", "depends_on": ["a"]}]}
    res = {
        "a": {"output": {"changes": [{"path": "x", "sha256": "1"}]}},
        "b": {"output": {"changes": [{"path": "x", "sha256": "2"}]}},
    }
    assert detect_change_conflicts(plan, res) == []


def test_identical_content_is_not_a_conflict():
    plan = {"subtasks": [{"id": "a", "depends_on": []}, {"id": "b", "depends_on": []}]}
    res = {
        "a": {"output": {"changes": [{"path": "x", "sha256": "1"}]}},
        "b": {"output": {"changes": [{"path": "x", "sha256": "1"}]}},
    }
    assert detect_change_conflicts(plan, res) == []


def test_incompatible_decisions():
    plan = {"subtasks": [{"id": "a", "depends_on": []}, {"id": "b", "depends_on": []}]}
    res = {"a": {"decisions": {"sep": "-"}}, "b": {"decisions": {"sep": "_"}}}
    [c] = detect_change_conflicts(plan, res)
    assert c["kind"] == "incompatible_decisions"


def test_claims_vs_evidence():
    res = {"a": {"output": {"claims_tests_pass": True}}}
    assert detect_claim_conflicts(res, {"outcome": "failed"})[0]["kind"] == "implementation_vs_criteria"
    assert detect_claim_conflicts(res, {"outcome": "passed"}) == []


# ---------------------------------------------------------------------------- retry / cost


@pytest.mark.parametrize("attempt", [1, 2, 3, 4, 10])
def test_backoff_bounded_with_jitter(attempt):
    rng = random.Random(0)
    delays = [backoff_delay(attempt, 0.5, 8.0, rng) for _ in range(200)]
    cap = min(8.0, 0.5 * 2 ** (attempt - 1))
    assert all(0 <= x <= cap for x in delays)
    assert len(set(delays)) > 100  # jittered, not constant


def test_cost_estimates():
    assert estimate_cost("claude-opus-5-5", 1_000_000, 0) == Decimal("4.00000000")
    assert estimate_cost("claude-opus-5-5", 0, 1_000_000) == Decimal("20.00000000")
    assert estimate_cost("claude-sonnet-5-5", 2000, 500) == Decimal("0.00900000")
    assert estimate_cost("claude-opus-5-5", 1000, 0, cache_read_tokens=1_000_000) == Decimal("0.20400000")
    assert estimate_cost("unknown-model", 10, 10) is None
