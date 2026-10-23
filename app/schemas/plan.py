"""Structured plan and acceptance-criteria schemas (validated output of the Planner)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ID_PATTERN = r"^[a-z0-9][a-z0-9_\-]{0,31}$"

Permission = Literal["workspace:read", "workspace:write", "tests:run", "history:read"]
ALLOWED_PERMISSIONS: frozenset[str] = frozenset({"workspace:read", "workspace:write", "tests:run", "history:read"})


class AcceptanceCriterion(BaseModel):
    """A machine-checkable acceptance criterion. Completion is decided by code, not by an LLM."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=ID_PATTERN)
    description: str = Field(min_length=1, max_length=500)
    kind: Literal["tests_pass", "file_exists", "file_contains", "file_not_contains"]
    command: list[str] | None = Field(default=None, max_length=16)
    path: str | None = Field(default=None, max_length=300)
    text: str | None = Field(default=None, max_length=2000)
    required: bool = True

    @model_validator(mode="after")
    def _check_fields(self) -> AcceptanceCriterion:
        if self.kind == "tests_pass" and not self.command:
            raise ValueError("tests_pass criteria need a 'command'")
        if self.kind != "tests_pass" and not self.path:
            raise ValueError(f"{self.kind} criteria need a 'path'")
        if self.kind in ("file_contains", "file_not_contains") and not self.text:
            raise ValueError(f"{self.kind} criteria need 'text'")
        return self


class SubtaskSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(pattern=ID_PATTERN)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=4000)
    kind: Literal["code", "analysis"] = "code"
    depends_on: list[str] = Field(default_factory=list, max_length=10)
    target_files: list[str] = Field(default_factory=list, max_length=20)
    acceptance: list[str] = Field(default_factory=list, max_length=20)
    required_permissions: list[str] = Field(default_factory=lambda: ["workspace:read"])


class Plan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    summary: str = Field(min_length=1, max_length=2000)
    subtasks: list[SubtaskSpec] = Field(min_length=1, max_length=12)
    risks: list[str] = Field(default_factory=list, max_length=20)
    assumptions: list[str] = Field(default_factory=list, max_length=20)
    needs_clarification: bool = False
    clarification_question: str | None = Field(default=None, max_length=1000)

    @field_validator("subtasks")
    @classmethod
    def _unique_ids(cls, v: list[SubtaskSpec]) -> list[SubtaskSpec]:
        ids = [s.id for s in v]
        if len(ids) != len(set(ids)):
            raise ValueError("subtask ids must be unique")
        return v

    @model_validator(mode="after")
    def _check_dependencies(self) -> Plan:
        ids = {s.id for s in self.subtasks}
        for s in self.subtasks:
            missing = [d for d in s.depends_on if d not in ids]
            if missing:
                raise ValueError(f"subtask {s.id} depends on unknown subtasks {missing}")
            if s.id in s.depends_on:
                raise ValueError(f"subtask {s.id} depends on itself")
        cycle = find_cycle(self.subtasks)
        if cycle:
            raise ValueError(f"circular dependency: {' -> '.join(cycle)}")
        return self

    @property
    def required_permissions(self) -> set[str]:
        return {p for s in self.subtasks for p in s.required_permissions}


def find_cycle(subtasks: list[SubtaskSpec]) -> list[str] | None:
    graph = {s.id: list(s.depends_on) for s in subtasks}
    white, grey, black = 0, 1, 2
    color = dict.fromkeys(graph, white)
    stack: list[str] = []

    def visit(n: str) -> list[str] | None:
        color[n] = grey
        stack.append(n)
        for m in graph.get(n, []):
            if color.get(m) == grey:
                return [*stack[stack.index(m) :], m]
            if color.get(m) == white:
                found = visit(m)
                if found:
                    return found
        stack.pop()
        color[n] = black
        return None

    for node in graph:
        if color[node] == white:
            found = visit(node)
            if found:
                return found
    return None


def topological_layers(subtasks: list[SubtaskSpec]) -> list[list[str]]:
    """Group subtasks into layers; every subtask in a layer only depends on earlier layers."""
    remaining = {s.id: set(s.depends_on) for s in subtasks}
    layers: list[list[str]] = []
    done: set[str] = set()
    while remaining:
        ready = sorted(k for k, deps in remaining.items() if deps <= done)
        if not ready:
            raise ValueError("dependency cycle")
        layers.append(ready)
        done.update(ready)
        for k in ready:
            remaining.pop(k)
    return layers
