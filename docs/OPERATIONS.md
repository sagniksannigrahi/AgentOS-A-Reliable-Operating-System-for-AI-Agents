# Setup, operation and troubleshooting

## Option A: Docker Compose (full stack)

Requires Docker Engine 24+ with Compose v2.

```bash
cp .env.example .env            # optional; Compose has working defaults
export AGENTOS_BOOTSTRAP_API_KEY=aos_dev_$(openssl rand -hex 16)   # dev convenience key
docker compose up --build
```

| Service | URL |
|---|---|
| Dashboard | http://localhost:5173 (paste the key under Settings) |
| API + OpenAPI | http://localhost:8000/docs |
| Grafana ("AgentOS overview" dashboard) | http://localhost:3000 |
| Jaeger traces | http://localhost:16686 |
| Prometheus | http://localhost:9090 |

The bootstrap key has every scope. For a realistic setup create separate keys so the approver is not
the task creator:

```bash
docker compose exec api python -m app.cli create-key --name operator --scopes tasks:read,tasks:write,approvals:read
docker compose exec api python -m app.cli create-key --name approver --scopes tasks:read,approvals:read,approvals:decide
```

Compose defaults: fake LLM, evaluation mode on (so scripted scenarios and fault injection are
available from the dashboard), Docker sandbox. Workspaces live in `/tmp/agentos-workspaces` on the
host (`AGENTOS_HOST_WORKSPACES`) because sandbox containers are started through the host daemon.

## Option B: no Docker (development)

```bash
uv sync                          # Python 3.12 venv + locked dependencies
cd frontend && npm ci && cd ..
scripts/local_stack.sh           # pgserver Postgres, migrations, 2 keys, API :8000, worker, dashboard :5173
```

This uses the **local** sandbox backend (not an isolation boundary; see the threat model) and no
Redis. Stop with Ctrl-C. Stop the background PostgreSQL with `uv run python scripts/dev_postgres.py --stop`.

## Using a real LLM

```bash
export AGENTOS_LLM_PROVIDER=anthropic
export AGENTOS_ANTHROPIC_API_KEY=...     # never commit it; it is redacted from logs
export AGENTOS_LLM_MODEL=claude-opus-5-5  # default
export AGENTOS_EVALUATION_MODE=false
```

Token counts then come from the provider (`provider_reported`); cost is still an estimate from
`app/llm/pricing.py`. The real-provider path is implemented against the official SDK but is not
exercised by the automated tests (they use the fake provider to avoid paid calls).

## Running tests and the benchmark

```bash
uv run pytest -q                                   # starts a throw-away PostgreSQL via pgserver
AGENTOS_TEST_DATABASE_URL=postgresql+psycopg://... uv run pytest -q   # or use your own server
uv run python scripts/run_benchmark.py --trials 5  # writes docs/benchmarks/latest.{json,md}
```

## Environment variables

All settings are in `app/config/settings.py` and documented in `.env.example`. The most important:

| Variable | Default | Meaning |
|---|---|---|
| `AGENTOS_DATABASE_URL` | localhost Postgres | `postgresql+psycopg://…` |
| `AGENTOS_REDIS_URL` | localhost Redis | empty disables Redis |
| `AGENTOS_LLM_PROVIDER` | `fake` | `fake` or `anthropic` |
| `AGENTOS_ANTHROPIC_API_KEY` | — | required for `anthropic` |
| `AGENTOS_SANDBOX_BACKEND` | `docker` | `docker`, `local` (dev only), `disabled` |
| `AGENTOS_EVALUATION_MODE` | `false` | scenarios, fault injection, workflow overrides |
| `AGENTOS_BOOTSTRAP_API_KEY` | — | created at startup if no keys exist |
| `AGENTOS_ALLOW_SELF_APPROVAL` | `false` | let a creator approve their own task |
| `AGENTOS_OTEL_EXPORTER_OTLP_ENDPOINT` | — | e.g. `http://jaeger:4318` |
| `AGENTOS_MAX_GRAPH_STEPS`, `AGENTOS_MAX_SUBTASK_ATTEMPTS`, `AGENTOS_TASK_DEADLINE_S` | 60, 3, 900 | budgets |
| `AGENTOS_LEASE_SECONDS`, `AGENTOS_HEARTBEAT_INTERVAL_S` | 30, 5 | worker leases |

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `postgres: invalid argument` or pgserver fails to start | PostgreSQL tools break on paths containing spaces; `scripts/dev_postgres.py` and the tests keep data in the system temp dir for this reason. |
| `/health/ready` 503 with `migrations: pending` | run `python -m app.cli migrate` (Compose runs the `migrate` service first). |
| Task stuck in `CREATED` | no worker is running, or it cannot reach the database; check worker logs. |
| Task in `RECOVERY_PENDING` | read `error` on the task page: LLM unavailable, sandbox/infra errors, repeated crashes, or an unverifiable side effect. Fix the cause and press **Resume**. |
| `run_tests` failing with "sandbox backend 'docker' unavailable" | Docker not reachable from the worker; mount the socket (Compose does) or use `AGENTOS_SANDBOX_BACKEND=local` for development. |
| 403 `self_approval_forbidden` | approve with a different key, or set `AGENTOS_ALLOW_SELF_APPROVAL=true` for single-user development. |
| 403 `evaluation_features_disabled` | scenario/fault/workflow options need `AGENTOS_EVALUATION_MODE=true`. |
| Dashboard shows "not authorised" | set a valid key under Settings (stored in this browser's localStorage only). |
| No spans on the Trace tab | spans are exported in ~1 s batches; refresh. They require `AGENTOS_PERSIST_SPANS=true`. |
