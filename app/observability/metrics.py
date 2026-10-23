"""Prometheus metrics. The API serves them at ``/metrics``; each worker on its own port.

Per-process counters are what Prometheus aggregates. The dashboard's ``/api/v1/stats`` endpoint
computes the same rates from PostgreSQL so they are correct across all processes and restarts.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

LATENCY_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600)

HTTP_REQUESTS = Counter("agentos_http_requests_total", "HTTP requests", ["method", "route", "status"])
HTTP_LATENCY = Histogram(
    "agentos_http_request_duration_seconds",
    "HTTP latency",
    ["method", "route"],
    buckets=LATENCY_BUCKETS,
)
TASKS_CREATED = Counter("agentos_tasks_created_total", "Tasks created")
TASKS_FINISHED = Counter("agentos_tasks_finished_total", "Tasks reaching a terminal state", ["status"])
TASK_E2E_LATENCY = Histogram(
    "agentos_task_e2e_seconds",
    "Task creation to terminal state",
    ["status"],
    buckets=LATENCY_BUCKETS,
)
RUNS_STARTED = Counter("agentos_runs_started_total", "Workflow runs started", ["reason"])
RETRIES = Counter("agentos_retries_total", "Retries initiated", ["kind"])
LOOPS_PREVENTED = Counter("agentos_loop_prevention_total", "Loop/duplicate detections", ["kind"])
CONFLICTS = Counter("agentos_conflicts_total", "Agent conflicts detected", ["kind"])
AGENT_LATENCY = Histogram(
    "agentos_agent_duration_seconds",
    "Agent invocation latency",
    ["role", "status"],
    buckets=LATENCY_BUCKETS,
)
NODE_LATENCY = Histogram("agentos_graph_node_duration_seconds", "Graph node latency", ["node"], buckets=LATENCY_BUCKETS)
LLM_TOKENS = Counter("agentos_llm_tokens_total", "LLM tokens", ["provider", "model", "direction", "source"])
LLM_COST = Counter("agentos_llm_cost_usd_total", "Estimated LLM cost (USD)", ["provider", "model", "source"])
LLM_CALLS = Counter("agentos_llm_calls_total", "LLM calls", ["provider", "role", "outcome"])
TOOL_CALLS = Counter("agentos_tool_calls_total", "Tool invocations", ["tool", "outcome"])
TOOL_LATENCY = Histogram("agentos_tool_duration_seconds", "Tool latency", ["tool"], buckets=LATENCY_BUCKETS)
CHECKPOINTS_WRITTEN = Counter("agentos_checkpoints_written_total", "Checkpoints written")
RECOVERIES = Counter("agentos_checkpoint_recoveries_total", "Recovery attempts", ["outcome"])
APPROVALS = Counter("agentos_approvals_total", "Approval requests/decisions", ["event"])
SECURITY_DENIALS = Counter("agentos_security_denials_total", "Policy denials", ["rule"])
ACTIVE_RUNS = Gauge("agentos_worker_active_runs", "Runs currently executing in this worker")
