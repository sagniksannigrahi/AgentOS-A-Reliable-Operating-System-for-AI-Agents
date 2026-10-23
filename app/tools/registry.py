"""Central tool registry. Every capability an agent can use is declared here with its schema,
permission scope, side-effect class, approval rule, timeout, retry policy and audit setting.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from app.domain.errors import ToolExecutionError

if TYPE_CHECKING:
    from app.tools.builtin import ToolEnv


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 1
    base_delay_s: float = 0.2
    max_delay_s: float = 2.0
    retry_on: tuple[type[BaseException], ...] = (ToolExecutionError,)


Handler = Callable[["ToolEnv", Any], Awaitable[dict[str, Any]]]
#: Returns a reason string if this specific call needs human approval, else None. May raise
#: ToolDeniedError for calls that are never allowed.
ApprovalRule = Callable[["ToolEnv", Any], str | None]
#: For idempotent mutating tools: after a crash, check whether the call already took effect.
#: Returns the output to record if it did, else None.
Verifier = Callable[["ToolEnv", Any], dict[str, Any] | None]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    scope: str
    mutating: bool
    idempotent: bool
    timeout_s: float
    handler: Handler
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    approval_rule: ApprovalRule | None = None
    verifier: Verifier | None = None
    audit: bool = True
    always_requires_approval: bool = False

    def describe(self) -> dict[str, Any]:
        """Schema shown to models in agent prompts."""
        schema = self.input_model.model_json_schema()
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": {"properties": schema.get("properties", {}), "required": schema.get("required", [])},
            "mutating": self.mutating,
        }

    def metadata(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "scope": self.scope,
            "mutating": self.mutating,
            "idempotent": self.idempotent,
            "requires_approval": "always"
            if self.always_requires_approval
            else ("conditional" if self.approval_rule else "never"),
            "timeout_s": self.timeout_s,
            "retry_max_attempts": self.retry.max_attempts,
            "audit": self.audit,
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"tool {spec.name} already registered")
        if spec.mutating and spec.retry.max_attempts > 1 and not spec.idempotent:
            raise ValueError(f"non-idempotent mutating tool {spec.name} must not auto-retry")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def for_scopes(self, scopes: frozenset[str]) -> list[ToolSpec]:
        return [t for t in self._tools.values() if t.scope in scopes]
