"""Planner agent: request -> validated, dependency-ordered plan with acceptance criteria."""

from __future__ import annotations

from typing import Any

from app.agents.runtime import agent_run, as_json, call_llm, finish_agent
from app.orchestration.context import RunContext
from app.schemas.agent import AgentResult
from app.schemas.plan import ALLOWED_PERMISSIONS, Plan
from app.security.injection import wrap_untrusted

SYSTEM = """You are the Planner agent of AgentOS, a supervised multi-agent software system.
Turn the user's request into a small, concrete execution plan for coding agents.

Output a JSON object:
{"summary": str,
 "subtasks": [{"id": "lowercase-id", "title": str, "description": str, "kind": "code"|"analysis",
               "depends_on": [subtask ids], "target_files": [relative paths],
               "acceptance": [acceptance criterion ids this subtask contributes to],
               "required_permissions": subset of PERMISSIONS}],
 "risks": [str], "assumptions": [str],
 "needs_clarification": bool, "clarification_question": str|null}

Rules:
- Split work so that subtasks which can run in parallel never edit the same file.
- Use depends_on for real ordering constraints only; never create cycles.
- Agents cannot run shell commands, access the network, or touch files outside the workspace.
- Set needs_clarification=true (and ask one precise question) when the request is too vague to
  verify against its acceptance criteria. Do not guess.""".replace("PERMISSIONS", str(sorted(ALLOWED_PERMISSIONS)))


async def run_planner(
    ctx: RunContext, *, plan_version: int, replan_reason: str | None
) -> tuple[Plan, AgentResult, int]:
    req = ctx.request
    listing = await _workspace_listing(ctx)
    user: dict[str, Any] = {
        "task": {"title": req.title, "description": req.description},
        "acceptance_criteria": [c.model_dump() for c in req.acceptance_criteria],
    }
    if replan_reason:
        user["replan_reason"] = replan_reason
    content = f"Plan this task.\n{as_json(user)}\n\nWorkspace files:\n" + wrap_untrusted(
        "\n".join(listing), "workspace:listing"
    )
    async with agent_run(
        ctx,
        "planner",
        subtask=None,
        attempt=1,
        plan_version=plan_version + 1,
        summary={"replan_reason": replan_reason, "plan_version": plan_version + 1},
    ) as h:
        plan = await call_llm(
            ctx,
            h,
            purpose="plan",
            system=SYSTEM,
            messages=[{"role": "user", "content": content}],
            schema=Plan,
            context={"plan_version": plan_version, "replan_reason": replan_reason},
        )
        result = AgentResult(
            role="planner",
            summary=plan.summary,
            output=plan.model_dump(),
            risks=plan.risks,
            validation_status="passed",
            evidence=[],
        )
        await finish_agent(ctx, h, result.model_dump())
        return plan, result, h.tokens


async def _workspace_listing(ctx: RunContext) -> list[str]:
    from app.tools.builtin import ListFilesIn, list_files

    out = await list_files(ctx.gateway.env, ListFilesIn(path="."))
    return list(out["files"])[:200]
