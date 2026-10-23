import { Fragment, useEffect, useMemo, useState } from "react";
import { allEvents, api, type ExecEvent, type TaskDetail as Task } from "../api";
import { TraceWaterfall } from "../components/charts";
import { ErrorNote, JsonView, StatTile, StatusBadge, eventColor, fmt, useAsync } from "../components/ui";

const TERMINAL = new Set(["COMPLETED", "FAILED", "CANCELLED"]);
const TABS = ["timeline", "agents", "tools", "trace", "recovery", "artifacts", "errors", "usage", "plan"] as const;
type Tab = (typeof TABS)[number];

const EVENT_GROUPS: Record<string, string[]> = {
  all: [],
  lifecycle: ["task.", "run."],
  agents: ["agent.", "plan.", "supervisor.", "llm."],
  tools: ["tool."],
  reliability: ["retry.", "loop.", "conflict.", "budget.", "checkpoint.", "recovery.", "fault.", "error."],
  approvals: ["approval."],
};

export function TaskDetail({ id, tab: initialTab }: { id: string; tab?: string }) {
  const [tab, setTab] = useState<Tab>((TABS as readonly string[]).includes(initialTab ?? "") ? (initialTab as Tab) : "timeline");
  const [live, setLive] = useState(true);
  const task = useAsync(() => api.task(id), [id], live ? 2000 : null);
  const t = task.data;
  useEffect(() => {
    if (t && TERMINAL.has(t.status)) setLive(false);
  }, [t]);
  const poll = live ? 2500 : null;
  const events = useAsync(() => allEvents(id), [id], poll);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<unknown>(null);
  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setActionError(null);
    try {
      await fn();
      setLive(true);
      await task.reload();
    } catch (e) {
      setActionError(e);
    } finally {
      setBusy(false);
    }
  };
  const go = (next: Tab) => {
    setTab(next);
    history.replaceState(null, "", `#/tasks/${id}/${next}`);
  };

  if (task.error && !t) return <ErrorNote error={task.error} />;
  if (!t) return <div className="empty">Loading…</div>;
  const evs = events.data ?? [];
  const errorCount = evs.filter((e) => e.status === "error").length;
  return (
    <>
      <div className="page-head">
        <div>
          <div className="row"><h1>{t.title}</h1><StatusBadge status={t.status} /></div>
          <p className="sub mono small">{t.id} · seed {t.workspace_seed} · trace {t.trace_id ?? "—"}</p>
        </div>
        <div className="row">
          {live && <span className="muted small">● live</span>}
          {(t.status === "RECOVERY_PENDING" || t.status === "PAUSED") && (
            <button className="btn primary" disabled={busy} onClick={() => act(() => api.resume(id))}>Resume</button>
          )}
          {!TERMINAL.has(t.status) && (
            <button className="btn danger" disabled={busy || !!t.cancel_requested_at} onClick={() => act(() => api.cancel(id))}>
              {t.cancel_requested_at ? "Cancelling…" : "Cancel"}
            </button>
          )}
        </div>
      </div>
      <ErrorNote error={actionError} />
      <Banner t={t} />
      <div className="grid cols-4" style={{ marginTop: 14 }}>
        <StatTile label="Runs" value={t.runs.length} note={`${t.counts.recoveries ?? 0} recovery · ${t.counts.retries ?? 0} retries`} />
        <StatTile label="Tool calls" value={fmt.num(t.counts.tool_calls)} note={`${t.counts.tool_denied ?? 0} denied by policy`} />
        <StatTile label="Tokens" value={fmt.num(t.usage.input_tokens + t.usage.output_tokens)}
                  note={t.usage.provider_reported_tokens ? `${fmt.num(t.usage.provider_reported_tokens)} provider-reported` : "all estimated"} />
        <StatTile label="Estimated cost" value={fmt.usd(t.usage.cost_usd)} note={`${t.usage.calls} LLM calls`} />
      </div>
      <div className="tabs" role="tablist">
        {TABS.map((x) => (
          <button key={x} role="tab" aria-selected={tab === x} className={`tab ${tab === x ? "active" : ""}`} onClick={() => go(x)}>
            {x}
            {x === "timeline" && <span className="count">{evs.length}</span>}
            {x === "tools" && <span className="count">{t.counts.tool_calls}</span>}
            {x === "agents" && <span className="count">{t.counts.agent_runs}</span>}
            {x === "errors" && errorCount > 0 && <span className="count">{errorCount}</span>}
          </button>
        ))}
      </div>
      {tab === "timeline" && <Timeline events={evs} />}
      {tab === "agents" && <Agents id={id} live={poll} />}
      {tab === "tools" && <Tools id={id} live={poll} />}
      {tab === "trace" && <Trace id={id} live={poll} />}
      {tab === "recovery" && <Recovery id={id} t={t} events={evs} live={poll} />}
      {tab === "artifacts" && <Artifacts id={id} live={poll} />}
      {tab === "errors" && <Timeline events={evs.filter((e) => e.status === "error" || e.status === "warning")} empty="No errors or warnings." />}
      {tab === "usage" && <UsagePanel t={t} />}
      {tab === "plan" && <PlanPanel t={t} />}
    </>
  );
}

function Banner({ t }: { t: Task }) {
  if (t.status === "COMPLETED" && t.result) {
    return (
      <div className="callout">
        <b style={{ color: "var(--good-text)" }}>✓ Completed.</b> {t.result.summary}
        {t.result.criteria && <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>{t.result.criteria.map((c) => <li key={c.criterion_id}>{c.passed ? "✓" : "✕"} <code>{c.criterion_id}</code> — {c.detail}</li>)}</ul>}
      </div>
    );
  }
  if (t.error) {
    return (
      <div className="callout error">
        <b style={{ color: "var(--critical-text)" }}>{t.status === "RECOVERY_PENDING" ? "↻ Waiting for a human to resume." : "✕ Failed."}</b>{" "}
        {t.error.message} {t.error.failure_class && <span className="badge">{t.error.failure_class}</span>}
      </div>
    );
  }
  if (t.status === "AWAITING_APPROVAL") return <div className="callout warn"><b>! Waiting for approval.</b> <a href="#/approvals">Review pending approvals →</a></div>;
  return null;
}

function Timeline({ events, empty = "No events yet." }: { events: ExecEvent[]; empty?: string }) {
  const [group, setGroup] = useState("all");
  const [open, setOpen] = useState<number | null>(null);
  const shown = useMemo(() => {
    const prefixes = EVENT_GROUPS[group];
    return prefixes.length ? events.filter((e) => prefixes.some((p) => e.type.startsWith(p))) : events;
  }, [events, group]);
  return (
    <div className="card">
      <div className="row" style={{ marginBottom: 10 }}>
        {Object.keys(EVENT_GROUPS).map((g) => <button key={g} className={`btn ${group === g ? "primary" : ""}`} onClick={() => setGroup(g)}>{g}</button>)}
        <span className="muted small">{shown.length} events</span>
      </div>
      {shown.length === 0 ? <div className="empty">{empty}</div> : (
        <ul className="timeline">
          {shown.map((e) => (
            <li key={e.id}>
              <span className="when" title={e.created_at}>{fmt.time(e.created_at)}</span>
              <span className="rail"><span className="dot" style={{ background: eventColor(e.status) }} /></span>
              <div>
                <div className="row" style={{ gap: 6 }}>
                  <span className="etype">{e.type}</span><span className="muted small">#{e.seq} · {e.actor}</span>
                  {e.status !== "info" && <span className="small" style={{ color: e.status === "error" ? "var(--critical-text)" : e.status === "success" ? "var(--good-text)" : "var(--text-secondary)" }}>{e.status}</span>}
                </div>
                <div className="msg">{e.message}</div>
                {Object.keys(e.data).length > 0 && (
                  <button className="tab small" style={{ padding: 0 }} onClick={() => setOpen(open === e.id ? null : e.id)}>{open === e.id ? "hide details" : "details"}</button>
                )}
                {open === e.id && <JsonView value={{ ...e.data, trace_id: e.trace_id, span_id: e.span_id, run_id: e.run_id }} />}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Agents({ id, live }: { id: string; live: number | null }) {
  const { data, error } = useAsync(() => api.agents(id), [id], live);
  const [open, setOpen] = useState<string | null>(null);
  return (
    <div className="card" style={{ padding: 0 }}>
      <ErrorNote error={error} />
      <table>
        <thead><tr><th>Agent</th><th>Subtask</th><th className="num">Attempt</th><th>Status</th><th>Validation</th><th className="num">Tools</th><th className="num">Tokens</th><th className="num">Latency</th><th>Summary</th></tr></thead>
        <tbody>
          {data?.items.map((a) => (
            <Fragment key={a.id}>
              <tr className="clickable" onClick={() => setOpen(open === a.id ? null : a.id)}>
                <td><b>{a.role.replace("_", " ")}</b>{a.replay_count > 0 && <span className="badge small" title="re-executed after a crash">replayed ×{a.replay_count}</span>}</td>
                <td className="mono">{a.subtask_key ?? "—"}</td>
                <td className="num">{a.attempt}</td>
                <td><StatusBadge status={a.status === "SUCCEEDED" ? "COMPLETED" : a.status === "RUNNING" ? "RUNNING" : "FAILED"} /></td>
                <td>{a.result?.validation_status ?? "—"}</td>
                <td className="num">{a.tool_calls}</td>
                <td className="num">{fmt.num(a.tokens)}</td>
                <td className="num">{fmt.ms(a.latency_ms)}</td>
                <td className="secondary">{a.error?.message ?? a.result?.summary ?? ""}</td>
              </tr>
              {open === a.id && <tr><td colSpan={9}><JsonView value={a.result ?? a.error} />{a.result?.confidence != null && <p className="muted small">Confidence is self-reported by the agent and is not used to resolve disagreements.</p>}</td></tr>}
            </Fragment>
          ))}
        </tbody>
      </table>
      {data && !data.items.length && <div className="empty">No agent runs yet.</div>}
    </div>
  );
}

function Tools({ id, live }: { id: string; live: number | null }) {
  const { data, error } = useAsync(() => api.tools(id), [id], live);
  const [open, setOpen] = useState<string | null>(null);
  const color = (s: string) => (s === "COMPLETED" || s === "RECONCILED" ? "var(--good)" : s === "DENIED" ? "var(--critical)" : s === "FAILED" ? "var(--serious)" : "var(--neutral-fill)");
  return (
    <div className="card" style={{ padding: 0 }}>
      <ErrorNote error={error} />
      <table>
        <thead><tr><th>When</th><th>Agent</th><th>Tool</th><th>Outcome</th><th className="num">Attempts</th><th className="num">Duration</th><th>Arguments / reason</th></tr></thead>
        <tbody>
          {data?.items.map((c) => (
            <Fragment key={c.id}>
              <tr className="clickable" onClick={() => setOpen(open === c.id ? null : c.id)}>
                <td className="muted small">{fmt.time(c.created_at)}</td>
                <td>{c.agent_role}</td>
                <td className="mono">{c.tool_name}{c.mutating && <span className="muted small"> · mutating</span>}</td>
                <td><span className="badge"><span className="dot" style={{ background: color(c.status) }} />{c.status.toLowerCase()}</span></td>
                <td className="num">{c.attempts}</td>
                <td className="num">{fmt.ms(c.duration_ms)}</td>
                <td className="secondary mono small" style={{ maxWidth: 420, overflowWrap: "anywhere" }}>
                  {c.decision_reason ? <span style={{ color: "var(--critical-text)" }}>{c.decision_reason}</span> : summarizeArgs(c.args_redacted)}
                </td>
              </tr>
              {open === c.id && <tr><td colSpan={7}><div className="grid cols-2"><div><div className="small muted">arguments (redacted)</div><JsonView value={c.args_redacted} /></div><div><div className="small muted">output preview{c.output_truncated ? " (truncated)" : ""}</div><pre className="block">{c.output_preview ?? c.error?.message ?? "—"}</pre></div></div></td></tr>}
            </Fragment>
          ))}
        </tbody>
      </table>
      {data && !data.items.length && <div className="empty">No tool calls yet.</div>}
    </div>
  );
}

function summarizeArgs(a: Record<string, unknown>): string {
  if (typeof a.path === "string") return a.path + (typeof a.content === "string" ? ` (${a.content.length} chars)` : "");
  if (Array.isArray(a.command)) return a.command.join(" ");
  return JSON.stringify(a).slice(0, 140);
}

function Trace({ id, live }: { id: string; live: number | null }) {
  const { data, error } = useAsync(() => api.trace(id), [id], live ? 4000 : null);
  return <div className="card"><ErrorNote error={error} />{data && <TraceWaterfall spans={data} />}</div>;
}

function Recovery({ id, t, events, live }: { id: string; t: Task; events: ExecEvent[]; live: number | null }) {
  const cks = useAsync(() => api.checkpoints(id), [id], live);
  const trans = useAsync(() => api.transitions(id), [id], live);
  const rec = events.filter((e) => ["recovery.started", "checkpoint.restored", "run.abandoned", "tool.reconciled", "fault.injected"].includes(e.type));
  return (
    <div className="grid" style={{ gap: 16 }}>
      <div className="card" style={{ padding: 0 }}>
        <div style={{ padding: "14px 18px 0" }}><h2>Runs (attempts)</h2></div>
        <table>
          <thead><tr><th className="num">#</th><th>Reason</th><th>Status</th><th>Worker</th><th>Started</th><th>Finished</th><th>Note</th></tr></thead>
          <tbody>{t.runs.map((r) => (
            <tr key={r.id}><td className="num">{r.attempt}</td><td>{r.reason}</td><td>{r.status.toLowerCase()}</td><td className="mono small">{r.worker_id ?? "—"}</td>
              <td className="small muted">{r.started_at ? fmt.time(r.started_at) : "—"}</td><td className="small muted">{r.finished_at ? fmt.time(r.finished_at) : "—"}</td>
              <td className="small secondary">{r.error ? String((r.error as Record<string, unknown>).reason ?? (r.error as Record<string, unknown>).message ?? "") : r.recovered_from_run_id ? `recovered from ${fmt.short(r.recovered_from_run_id)}` : ""}</td></tr>
          ))}</tbody>
        </table>
      </div>
      <div className="grid cols-2">
        <div className="card">
          <h2>Recovery events</h2>
          {rec.length ? <ul className="timeline">{rec.map((e) => <li key={e.id}><span className="when">{fmt.time(e.created_at)}</span><span className="rail"><span className="dot" style={{ background: eventColor(e.status) }} /></span><div><div className="etype">{e.type}</div><div className="msg">{e.message}</div></div></li>)}</ul>
            : <div className="empty">No crashes or recoveries for this task.</div>}
        </div>
        <div className="card">
          <h2>State transitions</h2>
          <ul className="timeline">{trans.data?.map((x) => <li key={x.id}><span className="when">{fmt.time(x.created_at)}</span><span className="rail"><span className="dot" style={{ background: "var(--series-1)" }} /></span><div><div><b>{x.from_status}</b> → <b>{x.to_status}</b> <span className="muted small">by {x.actor}</span></div><div className="msg secondary small">{x.reason}</div></div></li>)}</ul>
        </div>
      </div>
      <div className="card" style={{ padding: 0 }}>
        <div style={{ padding: "14px 18px 0" }}><h2>Checkpoints <span className="muted small">(LangGraph, PostgreSQL)</span></h2></div>
        <table>
          <thead><tr><th>When</th><th>After node</th><th className="num">Step</th><th>Checkpoint id</th><th>Run</th><th></th></tr></thead>
          <tbody>{cks.data?.map((c) => (
            <tr key={c.id}><td className="small muted">{fmt.time(c.created_at)}</td><td className="mono">{c.node ?? "—"}</td><td className="num">{c.step ?? "—"}</td><td className="mono small">{c.checkpoint_id}</td><td className="mono small">{fmt.short(c.run_id)}</td><td>{c.restored && <span className="badge">↻ restored</span>}</td></tr>
          ))}</tbody>
        </table>
      </div>
    </div>
  );
}

function Artifacts({ id, live }: { id: string; live: number | null }) {
  const { data } = useAsync(() => api.artifacts(id), [id], live);
  const [sel, setSel] = useState<string | null>(null);
  const content = useAsync(() => (sel ? api.artifact(id, sel) : Promise.resolve(null)), [sel]);
  useEffect(() => {
    if (!sel && data?.items.length) setSel((data.items.find((a) => a.kind === "diff") ?? data.items[0]).id);
  }, [data, sel]);
  if (data && !data.items.length) return <div className="card empty">No artifacts yet. They are produced when the task finishes.</div>;
  return (
    <div className="grid" style={{ gridTemplateColumns: "260px 1fr", gap: 16 }}>
      <div className="card" style={{ padding: 6 }}>
        {data?.items.map((a) => (
          <button key={a.id} className={`tab ${sel === a.id ? "active" : ""}`} style={{ display: "block", width: "100%", textAlign: "left", borderBottom: "none" }} onClick={() => setSel(a.id)}>
            <div>{a.name}</div><div className="muted small">{a.kind} · {fmt.num(a.size_bytes)} B · {a.sha256.slice(0, 10)}</div>
          </button>
        ))}
      </div>
      <div className="card">
        {content.data ? (content.data.kind === "diff" ? <Diff text={content.data.content} /> : <pre className="block">{content.data.content}</pre>) : <div className="empty">Select an artifact.</div>}
      </div>
    </div>
  );
}

function Diff({ text }: { text: string }) {
  return (
    <pre className="block">
      {text.split("\n").map((l, i) => (
        <div key={i} className={l.startsWith("+") && !l.startsWith("+++") ? "diff-add" : l.startsWith("-") && !l.startsWith("---") ? "diff-del" : l.startsWith("@@") ? "muted" : undefined}>{l || " "}</div>
      ))}
    </pre>
  );
}

function UsagePanel({ t }: { t: Task }) {
  const u = t.usage;
  return (
    <div className="grid cols-2">
      <div className="card">
        <h2>Tokens by source</h2>
        <table><tbody>
          <tr><td>Provider-reported (from the LLM API)</td><td className="num">{fmt.num(u.provider_reported_tokens)}</td><td className="num">{fmt.usd(u.cost_usd_from_reported_tokens)}</td></tr>
          <tr><td>Estimated locally (~4 chars/token)</td><td className="num">{fmt.num(u.estimated_tokens)}</td><td className="num">{fmt.usd(u.cost_usd_from_estimated_tokens)}</td></tr>
          <tr><td><b>Total</b></td><td className="num"><b>{fmt.num(u.input_tokens + u.output_tokens)}</b></td><td className="num"><b>{fmt.usd(u.cost_usd)}</b></td></tr>
        </tbody></table>
        <p className="muted small">Costs are always estimates from a versioned pricing table; providers report tokens, not money. The fake provider uses a notional price.</p>
      </div>
      <div className="card">
        <h2>By agent role</h2>
        <table>
          <thead><tr><th>Role</th><th className="num">Calls</th><th className="num">Input</th><th className="num">Output</th><th className="num">Cost</th></tr></thead>
          <tbody>{Object.entries(u.by_role).map(([role, r]) => (
            <tr key={role}><td>{role.replace("_", " ")}</td><td className="num">{r.calls}</td><td className="num">{fmt.num(r.input_tokens)}</td><td className="num">{fmt.num(r.output_tokens)}</td><td className="num">{fmt.usd(r.cost_usd)}</td></tr>
          ))}</tbody>
        </table>
      </div>
    </div>
  );
}

function PlanPanel({ t }: { t: Task }) {
  if (!t.plan) return <div className="card empty">No plan yet.</div>;
  return (
    <div className="grid" style={{ gap: 16 }}>
      <div className="card">
        <div className="card-head"><h2>Plan v{t.plan.version}</h2>{t.plan.needs_clarification && <span className="badge">needs clarification</span>}</div>
        <p style={{ marginTop: 0 }}>{t.plan.summary}</p>
        <table>
          <thead><tr><th>Subtask</th><th>Kind</th><th>Depends on</th><th>Files</th></tr></thead>
          <tbody>{t.plan.subtasks.map((s) => (
            <tr key={s.id}><td><code>{s.id}</code> {s.title}</td><td>{s.kind}</td><td className="mono small">{s.depends_on.join(", ") || "—"}</td><td className="mono small">{s.target_files.join(", ") || "—"}</td></tr>
          ))}</tbody>
        </table>
        {t.plan.risks?.length ? <><h2 style={{ marginTop: 14 }}>Risks</h2><ul>{t.plan.risks.map((r) => <li key={r}>{r}</li>)}</ul></> : null}
      </div>
      <div className="card"><h2>Request</h2><p className="secondary">{t.description}</p><JsonView value={t.request.acceptance_criteria} /></div>
    </div>
  );
}
