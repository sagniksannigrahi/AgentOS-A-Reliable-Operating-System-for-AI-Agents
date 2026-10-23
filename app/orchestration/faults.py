"""Deterministic fault injection (evaluation mode only).

A fault spec is a string:

* ``crash:before_agent:<role>``   – worker dies before an agent starts
* ``crash:during_llm:<role>``     – worker dies while an LLM request is in flight
* ``crash:after_tool:<tool>``     – worker dies after a tool executed, before its result is saved
* ``crash:after_node:<node>``     – worker dies between two workflow nodes (after the checkpoint)
* ``crash:during_finalize``       – worker dies while producing the final result
* ``llm_timeout:<role>[:N]``      – the next N LLM calls of that role time out (default 1)
* ``llm_invalid:<role>[:N]``      – the next N LLM responses of that role are malformed JSON
* ``tool_error:<tool>[:N]``       – the next N executions of that tool fail transiently

Each fault fires a bounded number of times per task. Firing is recorded as an execution event
(committed *before* the fault is raised) so a recovered run does not crash again at the same point.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from app.domain.enums import EventType
from app.domain.errors import LLMTimeoutError, SimulatedCrash, ToolExecutionError
from app.persistence.db import Database
from app.persistence.repository import count_events, record_event

_SPEC = re.compile(
    r"^(crash:(before_agent|during_llm|after_tool|after_node):[a-z_]+|crash:during_finalize|"
    r"(llm_timeout|llm_invalid|tool_error):[a-z_]+(:[1-9])?)$"
)


def validate_fault_specs(specs: list[str]) -> list[str]:
    bad = [s for s in specs if not _SPEC.match(s)]
    if bad:
        raise ValueError(f"invalid fault specs: {bad}")
    return specs


@dataclass
class FaultInjector:
    task_id: uuid.UUID
    specs: tuple[str, ...]
    db: Database | None

    @classmethod
    def disabled(cls) -> FaultInjector:
        return cls(uuid.UUID(int=0), (), None)

    def _limit(self, kind: str, target: str) -> int:
        for s in self.specs:
            parts = s.split(":")
            if parts[0] == kind and parts[1] == target:
                return int(parts[2]) if len(parts) > 2 else 1
        return 0

    async def _fire(self, key: str, limit: int, message: str) -> bool:
        if self.db is None or limit <= 0:
            return False
        prefix = f"fault:{self.task_id}:{key}:"
        async with self.db.session() as s:
            fired = await count_events(s, self.task_id, prefix)
            if fired >= limit:
                return False
            await record_event(
                s,
                self.task_id,
                EventType.FAULT_INJECTED,
                status="warning",
                actor="fault_injector",
                message=message,
                data={"fault": key, "occurrence": fired + 1},
                idempotency_key=f"{prefix}{fired + 1}",
            )
        return True

    async def crash_point(self, point: str, target: str = "") -> None:
        """``point`` is one of before_agent/during_llm/after_tool/after_node/during_finalize."""
        if not self.specs:
            return
        spec = f"crash:{point}:{target}" if target else f"crash:{point}"
        if spec in self.specs and await self._fire(spec, 1, f"simulated worker crash at {spec}"):
            raise SimulatedCrash(spec)

    async def llm_fault(self, role: str) -> str | None:
        """Returns 'invalid' when a malformed response should be produced; raises on timeout."""
        if not self.specs:
            return None
        if await self._fire(f"llm_timeout:{role}", self._limit("llm_timeout", role), f"LLM timeout ({role})"):
            raise LLMTimeoutError(f"injected LLM timeout for {role}")
        if await self._fire(f"llm_invalid:{role}", self._limit("llm_invalid", role), f"invalid LLM output ({role})"):
            return "invalid"
        return None

    async def tool_fault(self, tool: str) -> None:
        if not self.specs:
            return
        if await self._fire(f"tool_error:{tool}", self._limit("tool_error", tool), f"transient tool error ({tool})"):
            raise ToolExecutionError(f"injected transient failure in tool {tool}")
