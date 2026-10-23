import { useState } from "react";
import { api, type Approval, getApiKey, setApiKey } from "../api";
import { ErrorNote, JsonView, fmt, useAsync } from "../components/ui";

function ApprovalCard({ a, onDone }: { a: Approval; onDone: () => void }) {
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const decide = async (d: "approve" | "reject") => {
    setBusy(true);
    setError(null);
    try {
      await api.decide(a.id, d, comment);
      onDone();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="card grid" style={{ gap: 10 }}>
      <div className="card-head" style={{ marginBottom: 0 }}>
        <h2>{a.kind.replace("_", " ")} approval · <a href={`#/tasks/${a.task_id}`}>{a.task_title ?? fmt.short(a.task_id)}</a></h2>
        <span className="muted small">requested by {a.requested_by_agent} · {fmt.ago(a.created_at)} · expires {new Date(a.expires_at).toLocaleString()}</span>
      </div>
      <div>{a.reason}</div>
      {Object.keys(a.payload).length > 0 && <JsonView value={a.payload} />}
      {a.status === "PENDING" ? (
        <>
          <input placeholder="Comment (optional, recorded in the audit log)" value={comment} onChange={(e) => setComment(e.target.value)} />
          <div className="row">
            <button className="btn primary" disabled={busy} onClick={() => decide("approve")}>Approve</button>
            <button className="btn danger" disabled={busy} onClick={() => decide("reject")}>Reject</button>
            <span className="muted small">Approvals are bound to this exact request; the creator of a task cannot approve it.</span>
          </div>
        </>
      ) : (
        <div className="muted">{a.status.toLowerCase()} {a.decision ? `(${a.decision})` : ""} {a.comment ? `— ${a.comment}` : ""}</div>
      )}
      <ErrorNote error={error} />
    </div>
  );
}

export function Approvals() {
  const [status, setStatus] = useState("PENDING");
  const { data, error, reload } = useAsync(() => api.approvals(status), [status], 4000);
  return (
    <>
      <div className="page-head">
        <div><h1>Approvals</h1><p className="sub">Sensitive operations, plans and escalations waiting for a human.</p></div>
        <select value={status} onChange={(e) => setStatus(e.target.value)} style={{ width: 180 }} aria-label="Approval status">
          {["PENDING", "APPROVED", "REJECTED", "EXPIRED", "ALL"].map((s) => <option key={s} value={s}>{s.toLowerCase()}</option>)}
        </select>
      </div>
      <ErrorNote error={error} />
      <div className="grid" style={{ gap: 12 }}>
        {data?.items.length ? data.items.map((a) => <ApprovalCard key={a.id} a={a} onDone={reload} />)
          : <div className="card empty">Nothing {status === "ALL" ? "" : status.toLowerCase()} right now.</div>}
      </div>
    </>
  );
}

export function Settings() {
  const [key, setKey] = useState(getApiKey());
  const [saved, setSaved] = useState(false);
  const check = useAsync(() => api.meta(), [saved]);
  const [theme, setTheme] = useState(() => {
    try { return localStorage.getItem("agentos.theme") ?? "system"; } catch { return "system"; }
  });
  const applyTheme = (t: string) => {
    setTheme(t);
    try { localStorage.setItem("agentos.theme", t); } catch { /* ignore */ }
    if (t === "system") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", t);
  };
  return (
    <>
      <div className="page-head"><div><h1>Settings</h1><p className="sub">Stored only in this browser.</p></div></div>
      <div className="grid" style={{ gap: 16, maxWidth: 720 }}>
        <form className="card grid" style={{ gap: 10 }} onSubmit={(e) => { e.preventDefault(); setApiKey(key.trim()); setSaved((x) => !x); }}>
          <h2>API key</h2>
          <p className="muted small" style={{ margin: 0 }}>Create one with <code>uv run python -m app.cli create-key --name me</code>. Approving tasks needs the <code>approvals:decide</code> scope (and a different key than the task's creator).</p>
          <input type="password" value={key} onChange={(e) => setKey(e.target.value)} placeholder="aos_…" autoComplete="off" />
          <div className="row"><button className="btn primary" type="submit">Save</button>
            {check.data && <span className="small" style={{ color: "var(--good-text)" }}>✓ connected — provider {check.data.llm_provider}, sandbox {check.data.sandbox_backend}</span>}
            {check.error != null && <span className="small" style={{ color: "var(--critical-text)" }}>✕ not authorised</span>}
          </div>
        </form>
        <div className="card grid" style={{ gap: 10 }}>
          <h2>Theme</h2>
          <div className="row">
            {["system", "light", "dark"].map((t) => <button key={t} className={`btn ${theme === t ? "primary" : ""}`} onClick={() => applyTheme(t)}>{t}</button>)}
          </div>
        </div>
        <div className="card">
          <h2>Observability</h2>
          <ul className="secondary" style={{ margin: 0, paddingLeft: 18 }}>
            <li>Prometheus metrics: <a href="/metrics/" target="_blank" rel="noreferrer">/metrics</a> (API) and port 9101 (worker)</li>
            <li>Grafana (Docker Compose): <code>http://localhost:3000</code> · Jaeger: <code>http://localhost:16686</code></li>
            <li>Every task page shows its correlated trace from the persisted spans.</li>
          </ul>
        </div>
      </div>
    </>
  );
}
