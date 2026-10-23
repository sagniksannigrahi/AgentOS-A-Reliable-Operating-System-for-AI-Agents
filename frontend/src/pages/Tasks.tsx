import { useEffect, useMemo, useState } from "react";
import { ApiError, api, type Meta, type TaskStatus } from "../api";
import { ErrorNote, StatusBadge, fmt, useAsync } from "../components/ui";

const STATUSES: TaskStatus[] = ["CREATED", "PLANNING", "AWAITING_APPROVAL", "RUNNING", "VALIDATING", "COMPLETED", "FAILED", "CANCELLED", "PAUSED", "RECOVERY_PENDING"];
const PAGE = 20;

export function TaskList() {
  const [status, setStatus] = useState<string>("");
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const params = useMemo(() => {
    const p = new URLSearchParams({ limit: String(PAGE), offset: String(offset) });
    if (status) p.append("status", status);
    if (query) p.set("q", query);
    return p;
  }, [status, query, offset]);
  const { data, error, loading } = useAsync(() => api.tasks(params), [params.toString()], 4000);
  return (
    <>
      <div className="page-head">
        <div><h1>Tasks</h1><p className="sub">{data ? `${data.total} task${data.total === 1 ? "" : "s"}` : " "}</p></div>
        <a className="btn primary" href="#/new">New task</a>
      </div>
      <div className="row" style={{ marginBottom: 14 }}>
        <select value={status} onChange={(e) => { setStatus(e.target.value); setOffset(0); }} style={{ width: 200 }} aria-label="Status filter">
          <option value="">All statuses</option>
          {STATUSES.map((s) => <option key={s} value={s}>{s.replace(/_/g, " ").toLowerCase()}</option>)}
        </select>
        <form onSubmit={(e) => { e.preventDefault(); setQuery(q); setOffset(0); }} className="row" style={{ flex: 1, maxWidth: 420 }}>
          <input placeholder="Search title or description" value={q} onChange={(e) => setQ(e.target.value)} style={{ flex: 1 }} />
          <button className="btn" type="submit">Search</button>
        </form>
      </div>
      <ErrorNote error={error} />
      <div className="card" style={{ padding: 0, opacity: loading && data ? 0.7 : 1 }}>
        {data?.items.length ? (
          <table>
            <thead><tr><th>Title</th><th>Status</th><th>Seed</th><th>ID</th><th className="num">Created</th><th className="num">Updated</th></tr></thead>
            <tbody>
              {data.items.map((t) => (
                <tr key={t.id} className="clickable" onClick={() => (window.location.hash = `#/tasks/${t.id}`)}>
                  <td><a href={`#/tasks/${t.id}`}>{t.title}</a>{t.cancel_requested_at && t.status !== "CANCELLED" && <span className="muted small"> · cancelling</span>}</td>
                  <td><StatusBadge status={t.status} /></td>
                  <td className="mono">{t.workspace_seed}</td>
                  <td className="mono muted">{fmt.short(t.id)}</td>
                  <td className="num muted">{fmt.ago(t.created_at)}</td>
                  <td className="num muted">{fmt.ago(t.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <div className="empty">{loading ? "Loading…" : "No tasks match."}</div>}
      </div>
      {data && data.total > PAGE && (
        <div className="row" style={{ marginTop: 12, justifyContent: "flex-end" }}>
          <span className="muted small">{offset + 1}–{Math.min(offset + PAGE, data.total)} of {data.total}</span>
          <button className="btn" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>Previous</button>
          <button className="btn" disabled={offset + PAGE >= data.total} onClick={() => setOffset(offset + PAGE)}>Next</button>
        </div>
      )}
    </>
  );
}

const DEFAULT_CRITERIA = [
  { id: "tests", description: "the full test suite passes", kind: "tests_pass", command: ["pytest", "-q"] },
];

export function NewTask() {
  const meta = useAsync<Meta>(() => api.meta(), []);
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [seed, setSeed] = useState("");
  const [criteria, setCriteria] = useState(JSON.stringify(DEFAULT_CRITERIA, null, 2));
  const [requireApproval, setRequireApproval] = useState(false);
  const [scenario, setScenario] = useState("");
  const [mode, setMode] = useState("supervised");
  const [faults, setFaults] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const m = meta.data;

  useEffect(() => {
    if (m && !seed && m.seeds.length) setSeed(m.seeds[0].name);
  }, [m, seed]);

  const loadExample = (name: string) => {
    const ex = m?.examples.find((e) => e.name === name);
    if (!ex) return;
    const r = ex.request as Record<string, unknown>;
    setTitle(String(r.title ?? ""));
    setDescription(String(r.description ?? ""));
    setSeed(String(r.workspace_seed ?? seed));
    setCriteria(JSON.stringify(r.acceptance_criteria ?? DEFAULT_CRITERIA, null, 2));
    const o = (r.options ?? {}) as Record<string, unknown>;
    setRequireApproval(Boolean(o.require_plan_approval));
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    let parsed: unknown;
    try {
      parsed = JSON.parse(criteria);
    } catch {
      setError(new Error("Acceptance criteria must be valid JSON (a list of criteria)."));
      return;
    }
    const options: Record<string, unknown> = {};
    if (requireApproval) options.require_plan_approval = true;
    if (m?.evaluation_mode) {
      if (scenario) options.llm_scenario = scenario;
      if (mode !== "supervised") options.workflow_mode = mode;
      const f = faults.split(",").map((x) => x.trim()).filter(Boolean);
      if (f.length) options.faults = f;
    }
    setBusy(true);
    try {
      const task = await api.createTask({ title, description, workspace_seed: seed, acceptance_criteria: parsed, options }, crypto.randomUUID());
      window.location.hash = `#/tasks/${task.id}`;
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="page-head">
        <div><h1>New task</h1><p className="sub">Submitted to the API; a worker picks it up from the PostgreSQL queue.</p></div>
        {m?.examples.length ? (
          <select onChange={(e) => loadExample(e.target.value)} defaultValue="" style={{ width: 260 }} aria-label="Load example">
            <option value="" disabled>Load an example request…</option>
            {m.examples.map((x) => <option key={x.name} value={x.name}>{x.name}</option>)}
          </select>
        ) : null}
      </div>
      <ErrorNote error={meta.error} />
      <form className="card grid" style={{ gap: 14, maxWidth: 860 }} onSubmit={submit}>
        <label className="field">Title<input required maxLength={200} value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Implement multiply and divide" /></label>
        <label className="field">Description
          <span className="hint">What should the agents do? Be specific; the planner asks for clarification when it cannot verify the request.</span>
          <textarea required value={description} onChange={(e) => setDescription(e.target.value)} style={{ minHeight: 90, fontFamily: "inherit", fontSize: 14 }} />
        </label>
        <label className="field">Workspace seed
          <span className="hint">Agents work on an isolated copy of this repository.</span>
          <select value={seed} onChange={(e) => setSeed(e.target.value)}>
            {m?.seeds.map((s) => <option key={s.name} value={s.name}>{s.name}{s.description ? ` — ${s.description}` : ""}</option>)}
          </select>
        </label>
        <label className="field">Acceptance criteria (JSON)
          <span className="hint">Machine-checked: kinds tests_pass (allowlisted command), file_exists, file_contains, file_not_contains. The task only completes when all required criteria pass.</span>
          <textarea value={criteria} onChange={(e) => setCriteria(e.target.value)} spellCheck={false} />
        </label>
        <label className="check"><input type="checkbox" checked={requireApproval} onChange={(e) => setRequireApproval(e.target.checked)} />Require human approval of the plan before execution</label>
        {m?.evaluation_mode && (
          <fieldset className="callout warn grid" style={{ gap: 10, margin: 0 }}>
            <legend className="small" style={{ padding: "0 4px" }}>Evaluation options (server runs with AGENTOS_EVALUATION_MODE=true)</legend>
            <div className="grid cols-2">
              <label className="field">Scripted scenario (fake LLM)
                <select value={scenario} onChange={(e) => { setScenario(e.target.value); const sc = m.scenarios.find((s) => s.name === e.target.value); if (sc) setSeed(sc.seed); }}>
                  <option value="">Default for the seed</option>
                  {m.scenarios.map((s) => <option key={s.name} value={s.name}>{s.name} — {s.description}</option>)}
                </select>
              </label>
              <label className="field">Workflow mode
                <select value={mode} onChange={(e) => setMode(e.target.value)}>
                  <option value="supervised">supervised</option><option value="unsupervised">unsupervised (baseline)</option><option value="single_agent">single agent (baseline)</option>
                </select>
              </label>
            </div>
            <label className="field">Fault injection
              <span className="hint">Comma-separated, e.g. llm_timeout:coder:2, crash:after_tool:write_file</span>
              <input value={faults} onChange={(e) => setFaults(e.target.value)} className="mono" />
            </label>
          </fieldset>
        )}
        {error != null && (error instanceof ApiError && error.details ? <div className="callout error">{error.message}<pre className="small" style={{ margin: "6px 0 0" }}>{JSON.stringify(error.details, null, 2)}</pre></div> : <ErrorNote error={error} />)}
        <div className="row">
          <button className="btn primary" type="submit" disabled={busy || !seed}>{busy ? "Submitting…" : "Create task"}</button>
          <span className="muted small">Provider: {m?.llm_provider ?? "…"} ({m?.llm_model}) · sandbox: {m?.sandbox_backend}</span>
        </div>
      </form>
    </>
  );
}
