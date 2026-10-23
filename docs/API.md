# REST API

Interactive OpenAPI docs: `http://localhost:8000/docs`. All `/api/v1` routes need
`Authorization: Bearer <key>` (or `X-API-Key: <key>`).

Errors always look like:

```json
{"error": {"code": "validation_error", "message": "request validation failed", "details": [...], "request_id": "…"}}
```

Codes: `unauthenticated` 401, `forbidden`/`self_approval_forbidden`/`evaluation_features_disabled` 403,
`not_found` 404, `conflict`/`invalid_state`/`already_decided`/`approval_expired`/`idempotency_conflict` 409,
`validation_error`/`unknown_workspace_seed`/`command_not_allowed` 422, `rate_limited` 429, `internal_error` 500.
Every response carries `X-Request-ID` and a W3C `traceparent`.

| Method & path | Scope | Notes |
|---|---|---|
| `POST /api/v1/tasks` | tasks:write | 201 + `Location`. Optional `Idempotency-Key` header: replay returns 200 with the same task; different body -> 409 |
| `GET /api/v1/tasks?status=&q=&limit=&offset=` | tasks:read | `limit` 1–100; repeat `status` for several |
| `GET /api/v1/tasks/{id}` | tasks:read | plan, runs, usage (reported vs estimated), latest checkpoint, counts |
| `POST /api/v1/tasks/{id}/cancel` | tasks:write | 200 if cancelled immediately, 202 if a running workflow will stop at the next safe point; 409 if terminal |
| `POST /api/v1/tasks/{id}/resume` | tasks:write | only `PAUSED`/`RECOVERY_PENDING`; continues from the checkpoint |
| `GET /api/v1/tasks/{id}/events?after_seq=&limit=&type_prefix=` | tasks:read | cursor pagination (`next_after_seq`) |
| `GET /api/v1/tasks/{id}/agents` | tasks:read | agent runs with tool and token counts |
| `GET /api/v1/tasks/{id}/artifacts` / `…/artifacts/{aid}` | tasks:read | metadata / content |
| `GET /api/v1/tasks/{id}/tools?status=` | tasks:read | tool invocation audit (redacted) |
| `GET /api/v1/tasks/{id}/transitions` | tasks:read | state machine history |
| `GET /api/v1/tasks/{id}/checkpoints` | tasks:read | checkpoint references (`restored` flag) |
| `GET /api/v1/tasks/{id}/trace` | tasks:read | correlated spans |
| `GET /api/v1/approvals?status=PENDING|ALL|…&task_id=` | approvals:read | |
| `POST /api/v1/approvals/{id}/decision` | approvals:decide | `{"decision": "approve"|"reject", "comment"}`; creator cannot self-approve |
| `GET /api/v1/stats` | tasks:read | rates computed from PostgreSQL |
| `GET /api/v1/meta` | tasks:read | seeds, allowlisted commands, scenarios (evaluation mode), examples |
| `GET /health/live` | none | process is up |
| `GET /health/ready` | none | DB, migrations at head, checkpointer tables, Redis (degraded only) |
| `GET /metrics/` | none | Prometheus (bind to an internal network) |

## Task request

```json
{
  "title": "Implement multiply and divide",
  "description": "Implement multiply and divide in calc.py …",
  "workspace_seed": "calculator",
  "acceptance_criteria": [
    {"id": "tests", "description": "suite passes", "kind": "tests_pass", "command": ["pytest", "-q"]},
    {"id": "no-stub", "description": "no stubs", "kind": "file_not_contains", "path": "calc.py", "text": "NotImplementedError"}
  ],
  "options": {"require_plan_approval": false, "max_subtask_attempts": 3, "max_graph_steps": 60, "max_total_tokens": null}
}
```

Evaluation-only options (403 unless `AGENTOS_EVALUATION_MODE=true`): `workflow_mode`
(`supervised` | `unsupervised` | `single_agent`), `loop_detection`, `recovery`, `llm_scenario`,
`faults`, `seed`.

## curl walkthrough

```bash
KEY=$(uv run python -m app.cli create-key --name me --scopes tasks:read,tasks:write,approvals:read)
curl -s -X POST localhost:8000/api/v1/tasks -H "Authorization: Bearer $KEY" \
     -H 'Content-Type: application/json' -H "Idempotency-Key: $(uuidgen)" -d @examples/calculator.json
curl -s localhost:8000/api/v1/tasks/<id> -H "Authorization: Bearer $KEY"
curl -s "localhost:8000/api/v1/tasks/<id>/events?limit=500" -H "Authorization: Bearer $KEY"
```
