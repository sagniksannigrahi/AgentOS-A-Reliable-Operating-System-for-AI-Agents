// Typed client for the AgentOS REST API. The API key lives in this browser's localStorage only.

export type TaskStatus =
  | "CREATED" | "PLANNING" | "AWAITING_APPROVAL" | "RUNNING" | "VALIDATING"
  | "COMPLETED" | "FAILED" | "CANCELLED" | "PAUSED" | "RECOVERY_PENDING";

export interface Page<T> { items: T[]; total: number; limit: number; offset: number }

export interface TaskRun {
  id: string; attempt: number; status: string; reason: string; worker_id: string | null;
  recovered_from_run_id: string | null; error: Record<string, unknown> | null;
  created_at: string; started_at: string | null; finished_at: string | null;
}

export interface Usage {
  input_tokens: number; output_tokens: number; provider_reported_tokens: number; estimated_tokens: number;
  cost_usd: string; cost_usd_from_reported_tokens: string; cost_usd_from_estimated_tokens: string; calls: number;
  by_role: Record<string, { calls: number; input_tokens: number; output_tokens: number; cost_usd: number }>;
}

export interface TaskSummary {
  id: string; title: string; status: TaskStatus; workspace_seed: string; created_at: string; updated_at: string;
  completed_at: string | null; cancel_requested_at: string | null; trace_id: string | null;
}

export interface Criterion { criterion_id: string; passed: boolean; required?: boolean; detail?: string }

export interface TaskDetail extends TaskSummary {
  description: string;
  request: { acceptance_criteria: Array<Record<string, unknown>>; options: Record<string, unknown> };
  current_plan_version: number | null;
  result: { summary?: string; criteria?: Criterion[]; artifacts?: Record<string, string>; files_changed?: { path: string; status: string }[] } | null;
  error: { message?: string; failure_class?: string; [k: string]: unknown } | null;
  paused_from: string | null;
  plan: { version: number; summary: string; subtasks: Subtask[]; risks?: string[]; needs_clarification?: boolean } | null;
  runs: TaskRun[]; usage: Usage; latest_checkpoint: { checkpoint_id: string; node: string | null; step: number | null; created_at: string } | null;
  pending_approvals: number; counts: Record<string, number>;
}

export interface Subtask { id: string; title: string; kind: string; depends_on: string[]; target_files: string[] }

export interface ExecEvent {
  id: number; seq: number; run_id: string | null; type: string; status: string; actor: string; message: string;
  data: Record<string, unknown>; agent_run_id: string | null; tool_invocation_id: string | null;
  trace_id: string | null; span_id: string | null; created_at: string;
}

export interface AgentRun {
  id: string; run_id: string; role: string; subtask_key: string | null; attempt: number; status: string;
  replay_count: number; result: { summary?: string; validation_status?: string; confidence?: number | null; risks?: string[] } | null;
  error: { message?: string } | null; latency_ms: number | null; started_at: string; finished_at: string | null;
  tool_calls: number; tokens: number;
}

export interface ToolCall {
  id: string; run_id: string; agent_role: string; tool_name: string; status: string; decision_reason: string | null;
  mutating: boolean; args_redacted: Record<string, unknown>; output_preview: string | null; output_truncated: boolean;
  attempts: number; duration_ms: number | null; error: { message?: string } | null; created_at: string;
}

export interface Artifact { id: string; kind: string; name: string; content_type: string; sha256: string; size_bytes: number; created_at: string }
export interface Checkpoint { id: number; run_id: string; checkpoint_id: string; node: string | null; step: number | null; restored: boolean; created_at: string }
export interface Transition { id: number; from_status: string; to_status: string; actor: string; reason: string; created_at: string }
export interface Span {
  span_id: string; trace_id: string; parent_span_id: string | null; name: string; kind: string; service: string;
  start_time: string; end_time: string; duration_ms: number; status_code: string; attributes: Record<string, unknown>;
}
export interface Approval {
  id: string; task_id: string; kind: string; status: string; reason: string; payload: Record<string, unknown>;
  requested_by_agent: string; expires_at: string; created_at: string; decision: string | null; comment: string | null;
  task_title: string | null;
}

export interface Meta {
  llm_provider: string; llm_model: string; sandbox_backend: string; evaluation_mode: boolean;
  seeds: { name: string; description: string }[]; allowed_commands: Record<string, string>;
  scenarios: { name: string; description: string; seed: string }[];
  examples: { name: string; request: Record<string, unknown> }[];
  budgets: Record<string, number>;
}

export interface Stats {
  tasks: { total: number; by_status: Record<string, number>; finished: number };
  rates: Record<string, number | null>;
  events: Record<string, number>;
  tool_calls: { total: number; by_status: Record<string, number> };
  recoveries: { tasks_recovered: number; completed_after_recovery: number };
  latency: { avg_end_to_end_s: number | null; agents: { role: string; count: number; avg_ms: number; p95_ms: number }[] };
  usage: Record<string, { input_tokens: number; output_tokens: number; cost_usd: number; calls: number }>;
  usage_note: string;
  timeline: { hour: string; status: string; count: number }[];
}

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public details?: unknown) {
    super(message);
  }
}

const KEY = "agentos.apiKey";

export function getApiKey(): string {
  try { return localStorage.getItem(KEY) ?? ""; } catch { return ""; }
}

export function setApiKey(v: string): void {
  try { if (v) localStorage.setItem(KEY, v); else localStorage.removeItem(KEY); } catch { /* storage unavailable */ }
}

async function request<T>(method: string, path: string, body?: unknown, extraHeaders?: Record<string, string>): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json", ...extraHeaders };
  const key = getApiKey();
  if (key) headers.Authorization = `Bearer ${key}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    const e = data?.error ?? {};
    throw new ApiError(res.status, e.code ?? "http_error", e.message ?? res.statusText, e.details);
  }
  return data as T;
}

export const api = {
  ready: () => request<{ status: string; degraded: boolean; checks: Record<string, string> }>("GET", "/health/ready"),
  meta: () => request<Meta>("GET", "/api/v1/meta"),
  stats: () => request<Stats>("GET", "/api/v1/stats"),
  tasks: (params: URLSearchParams) => request<Page<TaskSummary>>("GET", `/api/v1/tasks?${params}`),
  task: (id: string) => request<TaskDetail>("GET", `/api/v1/tasks/${id}`),
  createTask: (body: unknown, idempotencyKey: string) =>
    request<TaskDetail>("POST", "/api/v1/tasks", body, { "Idempotency-Key": idempotencyKey }),
  cancel: (id: string) => request<{ status: string }>("POST", `/api/v1/tasks/${id}/cancel`, { reason: "cancelled from dashboard" }),
  resume: (id: string) => request<{ status: string }>("POST", `/api/v1/tasks/${id}/resume`, { reason: "resumed from dashboard" }),
  events: (id: string, afterSeq = 0) => request<{ items: ExecEvent[]; next_after_seq: number | null }>("GET", `/api/v1/tasks/${id}/events?limit=500&after_seq=${afterSeq}`),
  agents: (id: string) => request<Page<AgentRun>>("GET", `/api/v1/tasks/${id}/agents?limit=100`),
  tools: (id: string) => request<Page<ToolCall>>("GET", `/api/v1/tasks/${id}/tools?limit=100`),
  artifacts: (id: string) => request<Page<Artifact>>("GET", `/api/v1/tasks/${id}/artifacts`),
  artifact: (id: string, aid: string) => request<Artifact & { content: string }>("GET", `/api/v1/tasks/${id}/artifacts/${aid}`),
  checkpoints: (id: string) => request<Checkpoint[]>("GET", `/api/v1/tasks/${id}/checkpoints`),
  transitions: (id: string) => request<Transition[]>("GET", `/api/v1/tasks/${id}/transitions`),
  trace: (id: string) => request<Span[]>("GET", `/api/v1/tasks/${id}/trace`),
  approvals: (status = "PENDING") => request<Page<Approval>>("GET", `/api/v1/approvals?status=${status}&limit=100`),
  decide: (id: string, decision: "approve" | "reject", comment: string) =>
    request<Approval>("POST", `/api/v1/approvals/${id}/decision`, { decision, comment: comment || null }),
};

/** Fetch every page of events (the endpoint uses an after_seq cursor). */
export async function allEvents(id: string): Promise<ExecEvent[]> {
  const out: ExecEvent[] = [];
  let after = 0;
  for (let i = 0; i < 20; i++) {
    const page = await api.events(id, after);
    out.push(...page.items);
    if (page.next_after_seq == null) break;
    after = page.next_after_seq;
  }
  return out;
}
