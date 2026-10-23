"""Application configuration loaded from environment variables (prefix ``AGENTOS_``)."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class LLMProviderName(StrEnum):
    FAKE = "fake"
    ANTHROPIC = "anthropic"


class SandboxBackend(StrEnum):
    DOCKER = "docker"  # isolated container, no network (recommended)
    LOCAL = "local"  # restricted subprocess; NOT an isolation boundary, opt-in only
    DISABLED = "disabled"  # test execution refused


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENTOS_", env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: str = "development"
    log_level: str = "INFO"
    log_json: bool = True

    # --- persistence -------------------------------------------------------
    database_url: str = "postgresql+psycopg://agentos:agentos@localhost:5432/agentos"
    db_pool_size: int = 10
    redis_url: str | None = "redis://localhost:6379/0"

    # --- API ----------------------------------------------------------------
    api_host: str = "0.0.0.0"  # noqa: S104 - bound inside a container by default
    api_port: int = 8000
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    # Optional key created on startup if no keys exist (local development only).
    bootstrap_api_key: SecretStr | None = None
    task_create_rate_per_minute: int = 30

    # --- LLM ----------------------------------------------------------------
    llm_provider: LLMProviderName = LLMProviderName.FAKE
    llm_model: str = "claude-opus-5-5"
    llm_effort: str = "medium"
    llm_max_tokens: int = 16000
    llm_timeout_s: float = 120.0
    anthropic_api_key: SecretStr | None = None
    anthropic_refusal_fallback: bool = True

    # --- workspaces & sandbox ----------------------------------------------
    workspace_root: Path = PROJECT_ROOT / "var" / "workspaces"
    artifact_root: Path = PROJECT_ROOT / "var" / "artifacts"
    seed_repos_dir: Path = PROJECT_ROOT / "fixtures" / "repos"
    sandbox_backend: SandboxBackend = SandboxBackend.DOCKER
    sandbox_image: str = "agentos-sandbox:latest"
    sandbox_timeout_s: float = 60.0
    sandbox_memory_mb: int = 512
    tool_output_limit_bytes: int = 64_000
    max_file_size_bytes: int = 256_000

    # --- budgets & reliability ---------------------------------------------
    max_graph_steps: int = 60
    max_subtask_attempts: int = 3
    max_replans: int = 1
    max_concurrent_agents: int = 3
    task_deadline_s: float = 900.0
    agent_timeout_s: float = 180.0
    llm_max_retries: int = 3
    retry_base_delay_s: float = 0.5
    retry_max_delay_s: float = 8.0
    max_identical_tool_calls: int = 3
    max_agent_tool_steps: int = 12
    max_recoveries: int = 3
    approval_ttl_s: float = 86_400.0
    require_plan_approval: bool = False

    # --- worker -------------------------------------------------------------
    worker_id: str | None = None
    worker_concurrency: int = 2
    shutdown_grace_s: float = 20.0
    lease_seconds: float = 30.0
    heartbeat_interval_s: float = 5.0
    poll_interval_s: float = 1.0
    worker_metrics_port: int = 9101

    # --- observability ------------------------------------------------------
    otel_exporter_otlp_endpoint: str | None = None
    otel_service_name: str = "agentos"
    persist_spans: bool = True

    # --- testing / evaluation ----------------------------------------------
    # Enables fault injection, scripted fake-LLM scenarios and workflow-mode overrides
    # (e.g. disabling supervision) in task options. Never enable in production.
    evaluation_mode: bool = False
    allow_self_approval: bool = False

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        if isinstance(v, str) and not v.startswith("["):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    @property
    def psycopg_conninfo(self) -> str:
        """Plain libpq URI for psycopg / LangGraph's checkpointer."""
        return self.database_url.replace("postgresql+psycopg://", "postgresql://", 1)


@lru_cache
def get_settings() -> Settings:
    return Settings()
