# Reliability, recovery and idempotency

## Mechanisms

| Mechanism | Where |
|---|---|
| Leases (30 s default) renewed by heartbeats; expired leases are recovered | `repository.claim_run`, `worker/runner.py` |
| Fencing tokens: every write by a run checks `(run_id, fencing_token, status=RUNNING)` under `FOR SHARE` | `repository.guard_lease`, `RunContext.write()` |
| One active run per task (partial unique index) | `uq_task_runs_one_active` |
| Synchronous checkpoints after every node | `graph.astream(..., durability="sync")` |
| Write-ahead tool intents + idempotency keys | `tools/gateway.py` |
| Bounded retries with exponential backoff + full jitter (LLM, tools, test infra) | `agents/runtime.py`, `gateway.py`, `supervisor/retry.py` |
| Repair of invalid model output (2 attempts) | `agents/runtime.call_llm` |
| Per-agent tool-step budget, graph-step budget, deadline, token budget | `coder.run_tool_loop`, `supervisor/policy.py` |
| Duplicate-action detection (identical call > 3x in one agent run is denied) | gateway |
| No-progress loop detection (identical code + identical failure as an earlier attempt) | supervisor |
| Crash-loop protection (> `max_recoveries` -> `RECOVERY_PENDING`) | runner |
| Graceful shutdown: SIGTERM -> grace period -> cancel -> lease released immediately | `worker/main.py` |
| Cancellation observed at every node and before every side effect | node wrapper, gateway |

## What happens when a worker dies

1. Its lease expires. Another worker's claim marks the run `ABANDONED` and creates a `recovery`
   run with `fencing_token + 1`. The old worker can no longer write anything.
2. Write-ahead `INTENT` tool records from the dead run become `ABANDONED` (outcome unknown).
3. The new run loads the latest LangGraph checkpoint (`checkpoint.restored` event, `checkpoint_refs.restored`)
   and continues with the node that was running. Completed nodes are **not** re-run.
4. The interrupted node re-executes from its start:
   * LLM calls are repeated (and paid for again; tokens are recorded per call).
   * Tool calls get the same idempotency key (`task:role:subtask:attempt:plan_version:call_index:tool:args_hash`):
     a call already `COMPLETED` returns its recorded output; an `ABANDONED` mutating call is
     **verified** (e.g. `write_file` compares the file's hash with the intended content) and marked
     `RECONCILED` if it took effect, or executed if it did not. A mutating tool with no verifier
     raises `ReconciliationRequiredError` -> the task waits in `RECOVERY_PENDING` for a human.

## Crash points tested (`tests/recovery/test_crash_recovery.py`)

| # | Crash point | Resumes at | Duplicate side effects |
|---|---|---|---|
| 1 | before an agent starts | `execute_subtasks` | none |
| 2 | during an LLM request | `execute_subtasks` | none (LLM call repeated) |
| 3 | after a tool executed, before its result was saved | `execute_subtasks` | none: write verified and reconciled |
| 4 | between two nodes | `supervisor` (next node) | none |
| 5 | during final result generation | `finalize` | none: artifacts are idempotent |

## Where duplicate execution is still possible (be explicit)

* **LLM calls** in the interrupted node are always repeated. They have no external side effects but
  cost tokens.
* **Idempotent tools without a verifier** (`run_tests`, read-only tools) are re-executed after a crash.
  They are safe to repeat by construction.
* **`write_file`** is verified by content hash. If an unrelated actor changed the file between the
  crash and recovery, the hash differs and the write is applied again (the intended end state wins).
* **Zombie writes:** fencing stops a lost-lease worker's database writes, but a filesystem write it
  had already started may still land. The next writer overwrites with full-file content; the
  reconciliation check compares hashes rather than trusting either worker.
* **LangGraph's own checkpoint writes** are not fenced by AgentOS. A worker that lost its lease is
  cancelled immediately by its heartbeat loop, which narrows but does not eliminate this window.
* Exactly-once execution of external side effects is **not** claimed. Future non-idempotent tools
  (e.g. sending email) must be registered with `idempotent=False` and will always require a human
  decision after an ambiguous crash.

## Dependency outages

| Dependency | Behaviour |
|---|---|
| LLM provider | retries with backoff; after `llm_max_retries` the node fails with `failure_class=transient` and the task is parked in `RECOVERY_PENDING`; `POST /resume` continues from the checkpoint. Auth/model errors are non-retryable. |
| PostgreSQL | the API's `/health/ready` returns 503; workers back off (exponential, capped at 15 s) and keep trying; an in-flight run stops, its lease expires and it is recovered when the database returns. |
| Redis | rate limiting fails open (logged), workers fall back to polling, readiness reports `degraded`. Correctness is unaffected. |
| Sandbox (Docker missing, harness error) | test runs are retried; persistent infrastructure errors park the task in `RECOVERY_PENDING` and are never reported as test failures. |
