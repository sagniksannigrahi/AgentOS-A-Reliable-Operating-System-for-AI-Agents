"""Provider-neutral LLM interface used by all agents."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.domain.enums import UsageSource


@dataclass
class LLMRequest:
    role: str  # agent role making the call
    purpose: str  # plan | plan_review | code_step | test_analysis | security_review | single_agent_step
    system: str
    messages: list[dict[str, str]]  # [{"role": "user"|"assistant", "content": str}]
    context: dict[str, Any] = field(default_factory=dict)  # task_id, subtask_id, attempt, step, ...
    max_tokens: int = 16000


@dataclass
class LLMResponse:
    text: str
    input_tokens: int
    output_tokens: int
    usage_source: UsageSource
    provider: str
    model: str
    latency_ms: int
    cache_read_tokens: int = 0
    stop_reason: str | None = None


class LLMProvider(Protocol):
    name: str
    model: str

    async def complete(self, request: LLMRequest) -> LLMResponse: ...


def estimate_tokens(text: str) -> int:
    """Rough estimate (~4 characters per token). Only used when the provider reports nothing."""
    return max(1, math.ceil(len(text) / 4))


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def extract_json(text: str) -> Any:
    """Parse the JSON object in a model reply, tolerating code fences and surrounding prose."""
    cleaned = _FENCE.sub("", text.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found in model output")
    return json.loads(cleaned[start : end + 1])
