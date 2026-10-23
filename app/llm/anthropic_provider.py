"""Anthropic Claude adapter (official ``anthropic`` SDK)."""

from __future__ import annotations

import time
from typing import Any

import anthropic

from app.config.settings import Settings
from app.domain.enums import UsageSource
from app.domain.errors import LLMTimeoutError, LLMUnavailableError, PolicyViolationError, TransientError
from app.llm.base import LLMRequest, LLMResponse, estimate_tokens

# Models that accept the server-side refusal fallback parameter.
_FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}


class LLMRefusalError(PolicyViolationError):
    pass


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, settings: Settings) -> None:
        if settings.anthropic_api_key is None:
            raise LLMUnavailableError("AGENTOS_ANTHROPIC_API_KEY is not set")
        self.model = settings.llm_model
        self._effort = settings.llm_effort
        self._fallback = settings.anthropic_refusal_fallback and self.model in _FALLBACK_MODELS
        # Retries are owned by AgentOS (bounded, observable); disable the SDK's own retries.
        self._client = anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key.get_secret_value(),
            timeout=settings.llm_timeout_s,
            max_retries=0,
        )

    async def complete(self, request: LLMRequest) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": request.max_tokens,
            "system": request.system,
            "messages": request.messages,
            "output_config": {"effort": self._effort},
        }
        started = time.perf_counter()
        try:
            if self._fallback:
                resp = await self._client.beta.messages.create(
                    **kwargs, betas=["server-side-fallback-2026-07-01"], fallbacks="default"
                )
            else:
                resp = await self._client.messages.create(**kwargs)
        except anthropic.APITimeoutError as exc:
            raise LLMTimeoutError("LLM request timed out") from exc
        except (anthropic.RateLimitError, anthropic.InternalServerError, anthropic.APIConnectionError) as exc:
            raise TransientError(f"LLM provider temporarily unavailable: {type(exc).__name__}") from exc
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.NotFoundError) as exc:
            raise LLMUnavailableError(f"LLM provider rejected credentials/model: {type(exc).__name__}") from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                raise TransientError(f"LLM provider error {exc.status_code}") from exc
            raise LLMUnavailableError(f"LLM request rejected ({exc.status_code})") from exc
        latency_ms = int((time.perf_counter() - started) * 1000)
        if resp.stop_reason == "refusal":
            raise LLMRefusalError("the model declined this request")
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        usage = getattr(resp, "usage", None)
        if usage is not None and usage.input_tokens is not None:
            return LLMResponse(
                text=text,
                input_tokens=int(usage.input_tokens or 0),
                output_tokens=int(usage.output_tokens or 0),
                cache_read_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
                usage_source=UsageSource.PROVIDER_REPORTED,
                provider=self.name,
                model=str(getattr(resp, "model", self.model)),
                latency_ms=latency_ms,
                stop_reason=resp.stop_reason,
            )
        prompt = request.system + "".join(m["content"] for m in request.messages)
        return LLMResponse(
            text=text,
            input_tokens=estimate_tokens(prompt),
            output_tokens=estimate_tokens(text),
            usage_source=UsageSource.ESTIMATED,
            provider=self.name,
            model=self.model,
            latency_ms=latency_ms,
            stop_reason=resp.stop_reason,
        )
