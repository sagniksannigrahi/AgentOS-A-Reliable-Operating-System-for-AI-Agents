"""Structured agent results and the JSON protocols agents speak with the LLM."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ValidationStatus = Literal["passed", "failed", "unverified", "blocked"]


class Evidence(BaseModel):
    kind: str  # e.g. "file", "test_output", "tool_call", "diff"
    ref: str
    detail: str = ""


class AgentResult(BaseModel):
    """Every agent returns this envelope so the supervisor can compare outputs uniformly.

    ``confidence`` is the agent's *self-reported* confidence. It is recorded for analysis but is
    never treated as a calibrated probability or used to resolve disagreements.
    """

    role: str
    subtask_id: str | None = None
    summary: str
    output: dict[str, Any] = Field(default_factory=dict)
    evidence: list[Evidence] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1)
    risks: list[str] = Field(default_factory=list)
    validation_status: ValidationStatus = "unverified"
    decisions: dict[str, str] = Field(default_factory=dict)


class FileChange(BaseModel):
    path: str
    action: Literal["created", "modified"]
    sha256: str
    size_bytes: int


class TestCommandResult(BaseModel):
    __test__ = False  # not a pytest test class

    criterion_id: str
    command: list[str]
    outcome: Literal["passed", "failed", "infra_error", "timeout", "denied"]
    exit_code: int | None
    duration_ms: int
    output_tail: str = ""
    rerun_outcome: str | None = None


class CriterionResult(BaseModel):
    criterion_id: str
    passed: bool
    required: bool = True
    detail: str = ""


class TestReport(BaseModel):
    __test__ = False

    outcome: Literal["passed", "failed", "infra_error"]
    commands: list[TestCommandResult] = Field(default_factory=list)
    criteria: list[CriterionResult] = Field(default_factory=list)
    failure_signature: str | None = None
    diff_fingerprint: str = ""
    flaky: bool = False
    analysis: str = ""
    suggestions: list[str] = Field(default_factory=list)


class SecurityFinding(BaseModel):
    severity: Literal["low", "medium", "high", "critical"]
    rule: str
    detail: str
    file: str | None = None


class SecurityReview(BaseModel):
    verdict: Literal["pass", "escalate", "block"]
    findings: list[SecurityFinding] = Field(default_factory=list)
    rationale: str = ""


# --------------------------------------------------------------------------------------------
# LLM-facing protocols. These are the only shapes we accept from a model; anything else is an
# InvalidAgentOutputError and goes through bounded repair/retry.
# --------------------------------------------------------------------------------------------


class ToolCallAction(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: Literal["call_tool"]
    tool: str = Field(min_length=1, max_length=64)
    args: dict[str, Any] = Field(default_factory=dict)
    thought: str = ""


class FinishAction(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: Literal["finish"]
    summary: str = Field(min_length=1, max_length=4000)
    decisions: dict[str, str] = Field(default_factory=dict)
    confidence: float | None = Field(default=None, ge=0, le=1)
    risks: list[str] = Field(default_factory=list)
    claims_tests_pass: bool | None = None


class AgentStep(BaseModel):
    """Discriminated wrapper for one step of a tool-using agent."""

    step: ToolCallAction | FinishAction = Field(discriminator="action")


class TestAnalysis(BaseModel):
    __test__ = False
    model_config = ConfigDict(extra="ignore")

    summary: str = Field(max_length=4000)
    likely_cause: str = Field(default="", max_length=2000)
    suggestions: list[str] = Field(default_factory=list, max_length=10)


class AuditorAssessment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    verdict: Literal["pass", "escalate", "block"]
    findings: list[SecurityFinding] = Field(default_factory=list, max_length=30)
    rationale: str = Field(default="", max_length=4000)
