"""Deterministic conflict detection between agent results.

Disagreements are resolved by evidence (tests, policy), re-planning or a human, never by majority
vote and never by comparing self-reported confidence.
"""

from __future__ import annotations

from typing import Any


def _ancestors(plan: dict[str, Any]) -> dict[str, set[str]]:
    deps = {s["id"]: set(s.get("depends_on", [])) for s in plan.get("subtasks", [])}
    memo: dict[str, set[str]] = {}

    def anc(n: str) -> set[str]:
        if n not in memo:
            memo[n] = set()
            for d in deps.get(n, ()):
                memo[n] |= {d} | anc(d)
        return memo[n]

    return {n: anc(n) for n in deps}


def detect_change_conflicts(plan: dict[str, Any], results: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Contradictory file changes and incompatible implementation decisions between subtasks
    that were allowed to run independently (neither depends on the other)."""
    anc = _ancestors(plan)
    conflicts: list[dict[str, Any]] = []
    writes: dict[str, list[tuple[str, str]]] = {}
    decisions: dict[str, dict[str, list[str]]] = {}
    for sid, res in sorted(results.items()):
        for ch in (res.get("output") or {}).get("changes", []):
            writes.setdefault(ch["path"], []).append((sid, ch["sha256"]))
        for k, v in (res.get("decisions") or {}).items():
            decisions.setdefault(k, {}).setdefault(str(v), []).append(sid)
    for path, ws in sorted(writes.items()):
        for i, (a, ha) in enumerate(ws):
            for b, hb in ws[i + 1 :]:
                independent = a not in anc.get(b, set()) and b not in anc.get(a, set())
                if independent and ha != hb:
                    conflicts.append(
                        {
                            "kind": "contradictory_changes",
                            "path": path,
                            "subtasks": [a, b],
                            "detail": f"independent subtasks {a} and {b} wrote different content to {path}",
                        }
                    )
    for key, values in sorted(decisions.items()):
        if len(values) > 1:
            conflicts.append(
                {
                    "kind": "incompatible_decisions",
                    "decision": key,
                    "values": values,
                    "detail": f"subtasks disagree on '{key}': {values}",
                }
            )
    return conflicts


def detect_claim_conflicts(results: dict[str, dict[str, Any]], report: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Agents that claimed success while the acceptance tests say otherwise."""
    if not report or report.get("outcome") != "failed":
        return []
    claimants = [sid for sid, r in sorted(results.items()) if (r.get("output") or {}).get("claims_tests_pass")]
    if not claimants:
        return []
    return [
        {
            "kind": "implementation_vs_criteria",
            "subtasks": claimants,
            "detail": "coder(s) reported success but acceptance tests failed; test evidence takes precedence",
        }
    ]
