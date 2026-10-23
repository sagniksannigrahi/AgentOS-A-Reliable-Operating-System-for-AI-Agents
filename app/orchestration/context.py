"""Per-run execution context made available to graph nodes through a ContextVar.

Graph state (checkpointed) holds only serializable data. Live resources — DB handle, LLM client,
tool gateway, fencing token — belong to the *run* and are rebuilt by whichever worker resumes the
task, so they live here instead of in the checkpoint.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings
from app.llm.base import LLMProvider
from app.orchestration.faults import FaultInjector
from app.persistence.db import Database
from app.persistence.repository import guard_lease
from app.schemas.api import TaskCreate
from app.tools.gateway import ToolCallContext, ToolGateway
from app.tools.workspace import Workspace

ROLE_SCOPES: dict[str, frozenset[str]] = {
    "planner": frozenset({"workspace:read"}),
    "coder": frozenset({"workspace:read", "workspace:write"}),
    "tester": frozenset({"workspace:read", "tests:run"}),
    "security_auditor": frozenset({"workspace:read", "history:read"}),
    "supervisor": frozenset({"history:read"}),
    "single_agent": frozenset({"workspace:read", "workspace:write", "tests:run"}),
}


@dataclass
class RunContext:
    settings: Settings
    db: Database
    llm: LLMProvider
    gateway: ToolGateway
    workspace: Workspace
    faults: FaultInjector
    task_id: uuid.UUID
    run_id: uuid.UUID
    fencing_token: int
    request: TaskCreate
    extra: dict[str, Any] = field(default_factory=dict)

    @asynccontextmanager
    async def write(self) -> AsyncIterator[AsyncSession]:
        """A transaction fenced by this run's lease (raises LeaseLostError if we lost it)."""
        async with self.db.session() as s:
            await guard_lease(s, self.run_id, self.fencing_token)
            yield s

    def tool_context(
        self, role: str, agent_run_id: uuid.UUID | None, key_prefix: str, scopes: frozenset[str] | None = None
    ) -> ToolCallContext:
        granted = ROLE_SCOPES.get(role, frozenset())
        # A caller may narrow a role's scopes (e.g. analysis subtasks are read-only), never widen them.
        effective = granted & scopes if scopes is not None else granted
        return ToolCallContext(
            task_id=self.task_id,
            run_id=self.run_id,
            fencing_token=self.fencing_token,
            agent_run_id=agent_run_id,
            role=role,
            scopes=effective,
            key_prefix=key_prefix,
        )

    @property
    def options(self) -> Any:
        return self.request.options


CURRENT_RUN: ContextVar[RunContext] = ContextVar("agentos_run_context")


def current() -> RunContext:
    return CURRENT_RUN.get()
