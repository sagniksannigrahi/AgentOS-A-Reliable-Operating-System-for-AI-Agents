"""The tool gateway: the single choke point between agents and side effects.

For every call it performs, in order:

1. **Replay deduplication** by idempotency key: a call already COMPLETED by this task returns the
   recorded output instead of executing again.
2. **Crash reconciliation**: a call whose write-ahead INTENT was recorded by a run that died is
   verified (idempotent tools with a verifier), re-executed (safe idempotent tools) or escalated
   to a human (anything else) — never silently assumed.
3. **Input validation** against the tool's Pydantic schema.
4. **Authorization**: the agent's role must hold the tool's permission scope.
5. **Policy**: path confinement, command allowlist, forbidden paths (raise → DENIED).
6. **Duplicate-action detection**: identical calls repeated within one agent run are denied after
   ``max_identical_tool_calls`` (loop prevention).
7. **Human approval** for sensitive operations, bound to the exact arguments.
8. **Write-ahead intent** committed *before* execution (fenced by the run's lease).
9. **Execution** with timeout, bounded retries (only for idempotent tools) and output limits.
10. **Audit**: outcome, redacted arguments, output hash/preview, duration and trace ids.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select, update

from app.domain.enums import ApprovalKind, ApprovalStatus, EventType, ToolCallStatus
from app.domain.errors import (
    AgentOSError,
    ReconciliationRequiredError,
    TaskCancelledError,
    ToolDeniedError,
    ToolExecutionError,
)
from app.observability import metrics
from app.observability.tracing import start_span
from app.orchestration.faults import FaultInjector
from app.persistence.db import Database
from app.persistence.models import Task, ToolInvocation
from app.persistence.repository import (
    current_trace_ids,
    get_or_create_approval,
    guard_lease,
    record_event,
    sha256_text,
    stable_hash,
    utcnow,
)
from app.security.redaction import redact, redact_text, truncate
from app.supervisor.retry import backoff_delay
from app.tools.builtin import ToolEnv
from app.tools.registry import ToolRegistry, ToolSpec


class ApprovalRequiredError(AgentOSError):
    """Raised out of an agent when a tool call is waiting for a human decision."""

    def __init__(self, approval_id: uuid.UUID, reason: str) -> None:
        super().__init__(reason, details={"approval_id": str(approval_id)})
        self.approval_id = approval_id
        self.reason = reason


@dataclass(frozen=True)
class ToolCallContext:
    task_id: uuid.UUID
    run_id: uuid.UUID
    fencing_token: int
    agent_run_id: uuid.UUID | None
    role: str
    scopes: frozenset[str]
    key_prefix: str  # deterministic per (role, subtask, attempt, plan version)


@dataclass
class ToolResult:
    ok: bool
    status: ToolCallStatus
    output: dict[str, Any] | None = None
    error: str | None = None
    invocation_id: uuid.UUID | None = None
    deduplicated: bool = False

    def observation(self) -> dict[str, Any]:
        if self.ok:
            return {"ok": True, "result": self.output}
        return {"ok": False, "status": self.status.value, "error": self.error}


class ToolGateway:
    def __init__(
        self,
        registry: ToolRegistry,
        env: ToolEnv,
        db: Database,
        faults: FaultInjector,
        *,
        max_identical_calls: int,
        output_limit: int,
        approval_ttl_s: float,
        loop_detection: bool = True,
    ) -> None:
        self.registry = registry
        self.env = env
        self.db = db
        self.faults = faults
        self.max_identical_calls = max_identical_calls
        self.output_limit = output_limit
        self.approval_ttl_s = approval_ttl_s
        self.loop_detection = loop_detection

    # ---------------------------------------------------------------------------- helpers

    async def _deny(
        self,
        cc: ToolCallContext,
        tool: str,
        args: Any,
        key: str,
        reason: str,
        rule: str,
        mutating: bool = False,
    ) -> ToolResult:
        trace_id, span_id = current_trace_ids()
        async with self.db.session() as s:
            await guard_lease(s, cc.run_id, cc.fencing_token)
            existing = await s.scalar(select(ToolInvocation).where(ToolInvocation.idempotency_key == key))
            if existing is None:
                inv = ToolInvocation(
                    task_id=cc.task_id,
                    run_id=cc.run_id,
                    agent_run_id=cc.agent_run_id,
                    agent_role=cc.role,
                    tool_name=tool[:64],
                    status=ToolCallStatus.DENIED.value,
                    decision_reason=redact_text(reason)[:2000],
                    mutating=mutating,
                    args_redacted=redact(args if isinstance(args, dict) else {"raw": str(args)[:500]}),
                    args_hash=stable_hash(args),
                    idempotency_key=key,
                    fencing_token=cc.fencing_token,
                    trace_id=trace_id,
                    span_id=span_id,
                    completed_at=utcnow(),
                )
                s.add(inv)
                await s.flush()
                await record_event(
                    s,
                    cc.task_id,
                    EventType.TOOL_DENIED,
                    run_id=cc.run_id,
                    actor=cc.role,
                    status="error",
                    message=f"{tool} denied: {reason}",
                    data={"tool": tool, "rule": rule},
                    agent_run_id=cc.agent_run_id,
                    tool_invocation_id=inv.id,
                )
                inv_id = inv.id
            else:
                inv_id = existing.id
        metrics.TOOL_CALLS.labels(tool=tool[:64], outcome="denied").inc()
        metrics.SECURITY_DENIALS.labels(rule=rule).inc()
        return ToolResult(False, ToolCallStatus.DENIED, error=reason, invocation_id=inv_id)

    async def _cancel_requested(self, task_id: uuid.UUID) -> bool:
        async with self.db.session() as s:
            return (await s.scalar(select(Task.cancel_requested_at).where(Task.id == task_id))) is not None

    # ------------------------------------------------------------------------------ invoke

    async def invoke(self, cc: ToolCallContext, tool_name: str, raw_args: Any, call_index: int) -> ToolResult:
        args_hash = stable_hash(raw_args)
        key = f"{cc.task_id}:{cc.key_prefix}:{call_index}:{tool_name[:40]}:{args_hash[:16]}"
        with start_span(
            f"tool {tool_name[:40]}",
            task_id=str(cc.task_id),
            attributes={"tool.name": tool_name[:64], "agent.role": cc.role, "tool.call_index": call_index},
        ) as span:
            result = await self._invoke(cc, tool_name, raw_args, call_index, key, args_hash)
            span.set_attribute("tool.status", result.status.value)
            return result

    async def _invoke(
        self, cc: ToolCallContext, tool_name: str, raw_args: Any, call_index: int, key: str, args_hash: str
    ) -> ToolResult:
        spec = self.registry.get(tool_name) if isinstance(tool_name, str) else None
        if spec is None:
            return await self._deny(
                cc, str(tool_name), raw_args, key, f"unknown tool '{str(tool_name)[:40]}'", "unknown_tool"
            )

        # 1-2. replay dedupe / crash reconciliation
        async with self.db.session() as s:
            existing = await s.scalar(select(ToolInvocation).where(ToolInvocation.idempotency_key == key))
        if existing is not None:
            if existing.status in (ToolCallStatus.COMPLETED.value, ToolCallStatus.RECONCILED.value):
                metrics.TOOL_CALLS.labels(tool=spec.name, outcome="deduplicated").inc()
                return ToolResult(
                    True,
                    ToolCallStatus(existing.status),
                    output=existing.output,
                    invocation_id=existing.id,
                    deduplicated=True,
                )
            if existing.status == ToolCallStatus.DENIED.value:
                return ToolResult(
                    False, ToolCallStatus.DENIED, error=existing.decision_reason, invocation_id=existing.id
                )
            if (
                existing.status in (ToolCallStatus.INTENT.value, ToolCallStatus.ABANDONED.value)
                and existing.run_id != cc.run_id
            ):
                reconciled = await self._reconcile(cc, spec, raw_args, existing)
                if reconciled is not None:
                    return reconciled
                # else: verified not applied / safe to redo -> fall through and execute again

        # 3. input validation
        try:
            args = spec.input_model.model_validate(raw_args)
        except ValidationError as exc:
            msg = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:5])
            return await self._deny(
                cc, spec.name, raw_args, key, f"invalid arguments: {msg}", "invalid_arguments", spec.mutating
            )

        # 4. authorization
        if spec.scope not in cc.scopes:
            return await self._deny(
                cc,
                spec.name,
                raw_args,
                key,
                f"role '{cc.role}' lacks permission '{spec.scope}' required by {spec.name}",
                "missing_scope",
                spec.mutating,
            )

        # 5. policy (may also determine that approval is needed)
        approval_reason: str | None = None
        try:
            if spec.approval_rule is not None:
                approval_reason = spec.approval_rule(self.env, args)
            if spec.always_requires_approval:
                approval_reason = approval_reason or f"{spec.name} always requires approval"
        except ToolDeniedError as exc:
            rule = type(exc).__name__.replace("Error", "").lower()
            return await self._deny(cc, spec.name, raw_args, key, exc.message, rule, spec.mutating)

        # 6. duplicate-action detection within this agent run
        if self.loop_detection and cc.agent_run_id is not None:
            async with self.db.session() as s:
                same = await s.scalar(
                    select(func.count())
                    .select_from(ToolInvocation)
                    .where(
                        ToolInvocation.agent_run_id == cc.agent_run_id,
                        ToolInvocation.tool_name == spec.name,
                        ToolInvocation.args_hash == args_hash,
                        ToolInvocation.status != ToolCallStatus.DENIED.value,
                    )
                )
            if (same or 0) >= self.max_identical_calls:
                async with self.db.session() as s:
                    await record_event(
                        s,
                        cc.task_id,
                        EventType.LOOP_DETECTED,
                        run_id=cc.run_id,
                        actor="tool_gateway",
                        status="warning",
                        message=f"identical {spec.name} call repeated {same + 1} times; blocked",
                        data={"kind": "duplicate_action", "tool": spec.name, "count": int(same) + 1},
                        agent_run_id=cc.agent_run_id,
                    )
                metrics.LOOPS_PREVENTED.labels(kind="duplicate_action").inc()
                return await self._deny(
                    cc,
                    spec.name,
                    raw_args,
                    key,
                    f"duplicate action: identical {spec.name} call already made {same} times in this agent run",
                    "duplicate_action",
                    spec.mutating,
                )

        # 7. human approval for sensitive operations (bound to the exact arguments)
        if approval_reason:
            approval_key = f"tool:{cc.task_id}:{spec.name}:{args_hash}"
            async with self.db.session() as s:
                await guard_lease(s, cc.run_id, cc.fencing_token)
                req, created = await get_or_create_approval(
                    s,
                    task_id=cc.task_id,
                    run_id=cc.run_id,
                    kind=ApprovalKind.TOOL.value,
                    reason=approval_reason,
                    payload={
                        "tool": spec.name,
                        "args": redact(raw_args if isinstance(raw_args, dict) else {}),
                        "role": cc.role,
                        "args_hash": args_hash,
                    },
                    requested_by_agent=cc.role,
                    idempotency_key=approval_key,
                    ttl_seconds=self.approval_ttl_s,
                )
                status = req.status
                req_id = req.id
            if created:
                metrics.APPROVALS.labels(event="requested").inc()
            if status == ApprovalStatus.REJECTED.value:
                return await self._deny(
                    cc, spec.name, raw_args, key, "a human rejected this operation", "approval_rejected", spec.mutating
                )
            if status == ApprovalStatus.EXPIRED.value:
                return await self._deny(
                    cc, spec.name, raw_args, key, "approval request expired", "approval_expired", spec.mutating
                )
            if status != ApprovalStatus.APPROVED.value:
                raise ApprovalRequiredError(req_id, approval_reason)

        # cancellation is checked before any side effect
        if await self._cancel_requested(cc.task_id):
            raise TaskCancelledError("task cancellation requested")

        # 8. write-ahead intent
        return await self._execute(cc, spec, args, raw_args, key, args_hash, existing)

    async def _reconcile(
        self, cc: ToolCallContext, spec: ToolSpec, raw_args: Any, existing: ToolInvocation
    ) -> ToolResult | None:
        """Decide what to do with an operation that a dead run may or may not have performed."""
        if spec.mutating and spec.verifier is not None:
            try:
                args = spec.input_model.model_validate(raw_args)
                applied = spec.verifier(self.env, args)
            except (ValidationError, ToolDeniedError):
                applied = None
            if applied is not None:
                async with self.db.session() as s:
                    await guard_lease(s, cc.run_id, cc.fencing_token)
                    await s.execute(
                        update(ToolInvocation)
                        .where(ToolInvocation.id == existing.id)
                        .values(
                            status=ToolCallStatus.RECONCILED.value,
                            output=applied,
                            output_preview=redact_text(str(applied))[:2000],
                            output_hash=stable_hash(applied),
                            decision_reason="verified after crash: operation had already taken effect",
                            completed_at=utcnow(),
                        )
                    )
                    await record_event(
                        s,
                        cc.task_id,
                        EventType.TOOL_RECONCILED,
                        run_id=cc.run_id,
                        actor="tool_gateway",
                        message=f"{spec.name} from crashed run verified as already applied; not re-executed",
                        data={"tool": spec.name, "outcome": "already_applied"},
                        tool_invocation_id=existing.id,
                    )
                metrics.TOOL_CALLS.labels(tool=spec.name, outcome="reconciled").inc()
                return ToolResult(
                    True, ToolCallStatus.RECONCILED, output=applied, invocation_id=existing.id, deduplicated=True
                )
            async with self.db.session() as s:
                await record_event(
                    s,
                    cc.task_id,
                    EventType.TOOL_RECONCILED,
                    run_id=cc.run_id,
                    actor="tool_gateway",
                    message=f"{spec.name} from crashed run had not taken effect; executing it now",
                    data={"tool": spec.name, "outcome": "not_applied"},
                    tool_invocation_id=existing.id,
                )
            return None
        if not spec.mutating and spec.idempotent:
            return None  # read-only: safe to execute again
        raise ReconciliationRequiredError(
            f"{spec.name} may have executed before the worker crashed and cannot be verified",
            details={"tool_invocation_id": str(existing.id)},
        )

    async def _execute(
        self,
        cc: ToolCallContext,
        spec: ToolSpec,
        args: Any,
        raw_args: Any,
        key: str,
        args_hash: str,
        existing: ToolInvocation | None,
    ) -> ToolResult:
        trace_id, span_id = current_trace_ids()
        async with self.db.session() as s:
            await guard_lease(s, cc.run_id, cc.fencing_token)
            if existing is not None:
                inv = await s.get(ToolInvocation, existing.id)
                assert inv is not None
                inv.status = ToolCallStatus.INTENT.value
                inv.run_id = cc.run_id
                inv.fencing_token = cc.fencing_token
                inv.agent_run_id = cc.agent_run_id
            else:
                inv = ToolInvocation(
                    task_id=cc.task_id,
                    run_id=cc.run_id,
                    agent_run_id=cc.agent_run_id,
                    agent_role=cc.role,
                    tool_name=spec.name,
                    status=ToolCallStatus.INTENT.value,
                    mutating=spec.mutating,
                    args_redacted=redact(raw_args),
                    args_hash=args_hash,
                    idempotency_key=key,
                    fencing_token=cc.fencing_token,
                    trace_id=trace_id,
                    span_id=span_id,
                )
                s.add(inv)
            await s.flush()
            inv_id = inv.id
            await record_event(
                s,
                cc.task_id,
                EventType.TOOL_REQUESTED,
                run_id=cc.run_id,
                actor=cc.role,
                message=f"{spec.name} requested",
                data={"tool": spec.name},
                agent_run_id=cc.agent_run_id,
                tool_invocation_id=inv_id,
            )
            await record_event(
                s,
                cc.task_id,
                EventType.TOOL_ALLOWED,
                run_id=cc.run_id,
                actor="tool_gateway",
                message=f"{spec.name} allowed (scope {spec.scope})",
                data={"tool": spec.name, "mutating": spec.mutating},
                agent_run_id=cc.agent_run_id,
                tool_invocation_id=inv_id,
            )
        # 9. execute
        started = time.perf_counter()
        attempts = 0
        error: AgentOSError | None = None
        output: dict[str, Any] | None = None
        max_attempts = spec.retry.max_attempts if spec.idempotent else 1
        while attempts < max_attempts:
            attempts += 1
            try:
                await self.faults.tool_fault(spec.name)
                raw_out = await asyncio.wait_for(spec.handler(self.env, args), timeout=spec.timeout_s)
                output = spec.output_model.model_validate(raw_out).model_dump()
                error = None
                break
            except ToolDeniedError as exc:
                error = exc
                break
            except TimeoutError:
                error = ToolExecutionError(f"{spec.name} timed out after {spec.timeout_s}s")
            except spec.retry.retry_on as exc:
                error = exc if isinstance(exc, AgentOSError) else ToolExecutionError(str(exc))
            except (OSError, ValueError) as exc:
                error = ToolExecutionError(f"{spec.name} failed: {type(exc).__name__}: {exc}")
                break
            if attempts < max_attempts:
                async with self.db.session() as s:
                    await record_event(
                        s,
                        cc.task_id,
                        EventType.RETRY_INITIATED,
                        run_id=cc.run_id,
                        actor="tool_gateway",
                        status="warning",
                        message=f"retrying {spec.name} (attempt {attempts + 1}/{max_attempts}): {error.message if error else ''}",
                        data={"kind": "tool", "tool": spec.name, "attempt": attempts + 1},
                        tool_invocation_id=inv_id,
                    )
                metrics.RETRIES.labels(kind="tool").inc()
                await asyncio.sleep(backoff_delay(attempts, spec.retry.base_delay_s, spec.retry.max_delay_s))
        duration_ms = int((time.perf_counter() - started) * 1000)
        metrics.TOOL_LATENCY.labels(tool=spec.name).observe(duration_ms / 1000)

        # 10. the dangerous window: the side effect happened but is not yet recorded
        await self.faults.crash_point("after_tool", spec.name)

        preview_src = ""
        truncated = False
        if output is not None:
            preview_src = str(output.get("content") or output.get("output") or output)
            output, truncated = _cap_output(output, self.output_limit)
        preview, _ = truncate(redact_text(preview_src), 2000)
        async with self.db.session() as s:
            await guard_lease(s, cc.run_id, cc.fencing_token)
            values: dict[str, Any] = {
                "attempts": attempts,
                "duration_ms": duration_ms,
                "completed_at": utcnow(),
            }
            if error is None and output is not None:
                values.update(
                    status=ToolCallStatus.COMPLETED.value,
                    output=redact(output),
                    output_preview=preview,
                    output_hash=sha256_text(str(output)),
                    output_truncated=truncated,
                )
            else:
                denied = isinstance(error, ToolDeniedError)
                values.update(
                    status=(ToolCallStatus.DENIED if denied else ToolCallStatus.FAILED).value,
                    error=error.to_record() if error else {"message": "unknown"},
                    decision_reason=error.message if (denied and error) else None,
                )
            await s.execute(update(ToolInvocation).where(ToolInvocation.id == inv_id).values(**values))
            if error is None:
                await record_event(
                    s,
                    cc.task_id,
                    EventType.TOOL_COMPLETED,
                    run_id=cc.run_id,
                    actor=cc.role,
                    status="success",
                    message=f"{spec.name} completed in {duration_ms} ms",
                    data={"tool": spec.name, "attempts": attempts},
                    agent_run_id=cc.agent_run_id,
                    tool_invocation_id=inv_id,
                )
                if spec.name == "read_file" and output and output.get("injection_flags"):
                    await record_event(
                        s,
                        cc.task_id,
                        EventType.ERROR,
                        run_id=cc.run_id,
                        actor="security",
                        status="warning",
                        message=f"possible prompt injection in {output.get('path')}: {output['injection_flags']}",
                        data={
                            "kind": "prompt_injection_suspected",
                            "flags": output["injection_flags"],
                            "path": output.get("path"),
                        },
                        tool_invocation_id=inv_id,
                    )
            else:
                await record_event(
                    s,
                    cc.task_id,
                    EventType.TOOL_FAILED if not isinstance(error, ToolDeniedError) else EventType.TOOL_DENIED,
                    run_id=cc.run_id,
                    actor=cc.role,
                    status="error",
                    message=f"{spec.name} failed: {error.message}",
                    data={"tool": spec.name, **error.to_record()},
                    agent_run_id=cc.agent_run_id,
                    tool_invocation_id=inv_id,
                )
        if error is None:
            metrics.TOOL_CALLS.labels(tool=spec.name, outcome="completed").inc()
            return ToolResult(True, ToolCallStatus.COMPLETED, output=output, invocation_id=inv_id)
        metrics.TOOL_CALLS.labels(tool=spec.name, outcome="failed").inc()
        status = ToolCallStatus.DENIED if isinstance(error, ToolDeniedError) else ToolCallStatus.FAILED
        return ToolResult(False, status, error=error.message, invocation_id=inv_id)


def _cap_output(output: dict[str, Any], limit: int) -> tuple[dict[str, Any], bool]:
    """Truncate the large text fields of a tool output to the configured byte limit."""
    truncated = False
    out = dict(output)
    for field_name in ("content", "output"):
        val = out.get(field_name)
        if isinstance(val, str):
            new, cut = truncate(val, limit)
            if cut:
                out[field_name] = new
                out["truncated"] = True
                truncated = True
    return out, truncated
