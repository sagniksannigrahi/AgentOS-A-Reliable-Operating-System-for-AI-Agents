"""Tester agent: evaluates acceptance criteria with real test runs and file checks.

Pass/fail is decided by code (exit codes, file contents), never by the model. The model is only
asked to explain failures so the coder gets useful feedback.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from app.agents.runtime import agent_run, as_json, call_llm, finish_agent
from app.orchestration.context import RunContext
from app.schemas.agent import (
    AgentResult,
    CriterionResult,
    Evidence,
    TestAnalysis,
    TestCommandResult,
    TestReport,
)
from app.security.injection import wrap_untrusted
from app.security.paths import PathPolicyError, resolve_in_workspace

SYSTEM = """You are the Tester agent of AgentOS. Given failing test output, explain the most likely
cause and give concrete suggestions to the coder. Output JSON:
{"summary": str, "likely_cause": str, "suggestions": [str]}"""

_FAIL_LINE = re.compile(r"^(FAILED|ERROR) (\S+)", re.MULTILINE)


def failure_signature(outputs: list[str]) -> str | None:
    ids = sorted({m.group(2) for out in outputs for m in _FAIL_LINE.finditer(out)})
    if not ids:
        return None
    return hashlib.sha256("|".join(ids).encode()).hexdigest()[:16] + ":" + ",".join(ids)[:200]


async def run_tester(
    ctx: RunContext, *, attempt: int, plan_version: int, analyze: bool = True
) -> tuple[TestReport, AgentResult, int]:
    criteria = ctx.request.acceptance_criteria
    fingerprint = ctx.workspace.fingerprint()
    async with agent_run(
        ctx,
        "tester",
        subtask=None,
        attempt=attempt,
        plan_version=plan_version,
        summary={"criteria": [c.id for c in criteria], "fingerprint": fingerprint},
    ) as h:
        tc = ctx.tool_context("tester", h.id, key_prefix=h.key.split(":", 1)[1] + f":{fingerprint}")
        commands: list[TestCommandResult] = []
        results: list[CriterionResult] = []
        outputs: list[str] = []
        call_index = 0
        for c in criteria:
            if c.kind == "tests_pass":
                res = await ctx.gateway.invoke(tc, "run_tests", {"command": c.command}, call_index)
                call_index += 1
                cmd = _command_result(c.id, c.command or [], res)
                if cmd.outcome == "failed":
                    # Rerun once: different outcomes on identical code => flaky (conflicting evidence).
                    res2 = await ctx.gateway.invoke(tc, "run_tests", {"command": c.command}, call_index)
                    call_index += 1
                    cmd.rerun_outcome = _command_result(c.id, c.command or [], res2).outcome
                commands.append(cmd)
                outputs.append(cmd.output_tail)
                results.append(
                    CriterionResult(
                        criterion_id=c.id,
                        passed=cmd.outcome == "passed",
                        required=c.required,
                        detail=f"{cmd.outcome} (exit {cmd.exit_code})",
                    )
                )
            else:
                results.append(_file_check(ctx, c))
        infra_ids = {c.criterion_id for c in commands if c.outcome in ("infra_error", "denied")}
        failed_required = [r for r in results if r.required and not r.passed]
        flaky = any(c.rerun_outcome is not None and c.rerun_outcome != c.outcome for c in commands)
        if not failed_required:
            outcome = "passed"
        elif all(r.criterion_id in infra_ids for r in failed_required):
            outcome = "infra_error"  # nothing failed because of the code; the harness is broken
        else:
            outcome = "failed"
        report = TestReport(
            outcome=outcome,
            commands=commands,
            criteria=results,
            failure_signature=failure_signature(outputs) if outcome == "failed" else None,
            diff_fingerprint=fingerprint,
            flaky=flaky,
        )
        if outcome == "failed" and analyze:
            failing = "\n\n".join(c.output_tail for c in commands if c.outcome != "passed")[-6000:]
            crit = "\n".join(f"- {r.criterion_id}: {r.detail}" for r in results if not r.passed)
            analysis = await call_llm(
                ctx,
                h,
                purpose="test_analysis",
                system=SYSTEM,
                messages=[
                    {
                        "role": "user",
                        "content": f"Failed criteria:\n{crit}\n\nTest output:\n"
                        + wrap_untrusted(failing, "run_tests:output"),
                    }
                ],
                schema=TestAnalysis,
                context={"attempt": attempt},
            )
            report.analysis = analysis.summary + (
                f" Likely cause: {analysis.likely_cause}" if analysis.likely_cause else ""
            )
            report.suggestions = analysis.suggestions
        result = AgentResult(
            role="tester",
            summary=f"acceptance: {outcome} ({sum(r.passed for r in results)}/{len(results)} criteria)",
            output=report.model_dump(),
            evidence=[
                Evidence(kind="test_output", ref=c.criterion_id, detail=f"{c.outcome}: {c.output_tail[-300:]}")
                for c in commands
            ],
            validation_status="passed" if outcome == "passed" else "failed",
        )
        await finish_agent(ctx, h, result.model_dump(), ok=True)
        return report, result, h.tokens


def _command_result(cid: str, command: list[str], res: Any) -> TestCommandResult:
    if res.ok and res.output:
        out = res.output
        return TestCommandResult(
            criterion_id=cid,
            command=list(out.get("command", command)),
            outcome=out["outcome"],
            exit_code=out.get("exit_code"),
            duration_ms=out.get("duration_ms", 0),
            output_tail=str(out.get("output", ""))[-4000:],
        )
    outcome = "denied" if res.status.value == "DENIED" else "infra_error"
    return TestCommandResult(
        criterion_id=cid,
        command=command,
        outcome=outcome,
        exit_code=None,
        duration_ms=0,
        output_tail=str(res.error or "")[-2000:],
    )


def _file_check(ctx: RunContext, c: Any) -> CriterionResult:
    try:
        p, norm = resolve_in_workspace(ctx.workspace.repo, c.path)
    except PathPolicyError as exc:
        return CriterionResult(
            criterion_id=c.id, passed=False, required=c.required, detail=f"invalid path: {exc.message}"
        )
    exists = p.is_file()
    if c.kind == "file_exists":
        return CriterionResult(
            criterion_id=c.id, passed=exists, required=c.required, detail=f"{norm} {'exists' if exists else 'missing'}"
        )
    if not exists:
        return CriterionResult(criterion_id=c.id, passed=False, required=c.required, detail=f"{norm} missing")
    text = p.read_text(encoding="utf-8", errors="replace")
    found = c.text in text
    passed = found if c.kind == "file_contains" else not found
    return CriterionResult(
        criterion_id=c.id,
        passed=passed,
        required=c.required,
        detail=f"{norm} {'contains' if found else 'does not contain'} {c.text!r}",
    )


def criteria_summary(report: TestReport) -> str:
    return as_json([r.model_dump() for r in report.criteria])
