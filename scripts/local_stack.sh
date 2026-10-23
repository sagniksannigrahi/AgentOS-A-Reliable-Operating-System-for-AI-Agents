#!/usr/bin/env bash
# Run AgentOS without Docker: project-local PostgreSQL (pgserver), API, worker and dashboard.
# Usage: scripts/local_stack.sh            (Ctrl-C stops everything)
# Uses the fake LLM, the *local* (non-isolated) sandbox and evaluation mode. Development only.
set -euo pipefail
cd "$(dirname "$0")/.."

export AGENTOS_DATABASE_URL="${AGENTOS_DATABASE_URL:-$(uv run python scripts/dev_postgres.py | tail -1)}"
export AGENTOS_REDIS_URL="${AGENTOS_REDIS_URL:-}"
export AGENTOS_LLM_PROVIDER="${AGENTOS_LLM_PROVIDER:-fake}"
export AGENTOS_SANDBOX_BACKEND="${AGENTOS_SANDBOX_BACKEND:-local}"
export AGENTOS_EVALUATION_MODE="${AGENTOS_EVALUATION_MODE:-true}"
export AGENTOS_LOG_JSON="${AGENTOS_LOG_JSON:-true}"

uv run python -m app.cli migrate
echo "database: $AGENTOS_DATABASE_URL"
if [[ "${CREATE_KEYS:-1}" == "1" ]]; then
  echo "operator key:  $(uv run python -m app.cli create-key --name operator --scopes tasks:read,tasks:write,approvals:read 2>/dev/null)"
  echo "approver key:  $(uv run python -m app.cli create-key --name approver --scopes tasks:read,approvals:read,approvals:decide 2>/dev/null)"
fi

trap 'kill 0' EXIT
uv run python -m app.cli api &
uv run python -m app.cli worker &
(cd frontend && npm run dev -- --host 127.0.0.1) &
wait
