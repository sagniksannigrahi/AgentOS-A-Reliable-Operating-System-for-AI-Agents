import { api } from "../api";
import { HourlyTasks, StatusBreakdown, UsageSplit } from "../components/charts";
import { ErrorNote, StatTile, StatusBadge, fmt, useAsync } from "../components/ui";

export function Overview() {
  const stats = useAsync(() => api.stats(), [], 5000);
  const recent = useAsync(() => api.tasks(new URLSearchParams({ limit: "8" })), [], 5000);
  const ready = useAsync(() => api.ready(), [], 15000);
  const s = stats.data;
  const reported = s?.usage.provider_reported;
  const estimated = s?.usage.estimated;
  const tokens = (u?: { input_tokens: number; output_tokens: number }) => (u ? u.input_tokens + u.output_tokens : 0);
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Overview</h1>
          <p className="sub">Live from the AgentOS database · refreshes every 5 s</p>
        </div>
        {ready.data && (
          <div className="row small">
            {Object.entries(ready.data.checks).map(([k, v]) => (
              <span key={k} className="badge" title={v}>
                <span className="dot" style={{ background: v === "ok" ? "var(--good)" : v === "disabled" ? "var(--neutral-fill)" : "var(--critical)" }} />
                {k}: {v === "ok" ? "ok" : v}
              </span>
            ))}
          </div>
        )}
      </div>
      <ErrorNote error={stats.error} />
      {s && (
        <div className="grid" style={{ gap: 16 }}>
          <div className="grid cols-4">
            <StatTile label="Tasks" value={fmt.num(s.tasks.total)} note={`${fmt.num(s.tasks.finished)} finished`} />
            <StatTile label="Completion rate" value={fmt.pct(s.rates.completion_rate)} note="of finished tasks" />
            <StatTile label="Failure rate" value={fmt.pct(s.rates.failure_rate)} note={`${fmt.num(s.tasks.by_status.FAILED ?? 0)} failed`} />
            <StatTile label="Avg end-to-end" value={s.latency.avg_end_to_end_s == null ? "—" : `${s.latency.avg_end_to_end_s.toFixed(1)} s`} note="creation → terminal state" />
            <StatTile label="Retries" value={fmt.num(s.events["retry.initiated"] ?? 0)} note={`${s.rates.retry_rate_per_task?.toFixed(2) ?? "—"} per task`} />
            <StatTile label="Loops prevented" value={fmt.num(s.events["loop.detected"] ?? 0)} note="duplicate actions + no-progress" />
            <StatTile label="Checkpoint recovery" value={fmt.pct(s.rates.checkpoint_recovery_success_rate)}
                      note={`${s.recoveries.completed_after_recovery}/${s.recoveries.tasks_recovered} recovered tasks completed`} />
            <StatTile label="Approvals requested" value={fmt.num(s.events["approval.requested"] ?? 0)}
                      note={`${s.rates.approval_frequency_per_task?.toFixed(2) ?? "—"} per task`} />
          </div>
          <div className="grid cols-2">
            <div className="card">
              <div className="card-head"><h2>Task status</h2><span className="muted small">{s.tasks.total} total</span></div>
              <StatusBreakdown byStatus={s.tasks.by_status} />
            </div>
            <div className="card">
              <div className="card-head"><h2>Tasks created per hour</h2><span className="muted small">last 24 h</span></div>
              <HourlyTasks timeline={s.timeline} />
            </div>
          </div>
          <div className="grid cols-3">
            <div className="card">
              <div className="card-head"><h2>LLM tokens</h2></div>
              <UsageSplit reported={tokens(reported)} estimated={tokens(estimated)} />
              <table style={{ marginTop: 12 }}>
                <tbody>
                  <tr><td>Estimated cost (from reported tokens)</td><td className="num">{fmt.usd(reported?.cost_usd)}</td></tr>
                  <tr><td>Estimated cost (from estimated tokens)</td><td className="num">{fmt.usd(estimated?.cost_usd)}</td></tr>
                  <tr><td>LLM calls</td><td className="num">{fmt.num((reported?.calls ?? 0) + (estimated?.calls ?? 0))}</td></tr>
                </tbody>
              </table>
              <p className="muted small" style={{ marginBottom: 0 }}>{s.usage_note}</p>
            </div>
            <div className="card">
              <div className="card-head"><h2>Tool calls</h2><span className="muted small">{fmt.num(s.tool_calls.total)} total</span></div>
              <table>
                <tbody>
                  {Object.entries(s.tool_calls.by_status).sort().map(([k, v]) => (
                    <tr key={k}><td>{k.toLowerCase()}</td><td className="num">{fmt.num(v)}</td></tr>
                  ))}
                  <tr><td>error rate</td><td className="num">{fmt.pct(s.rates.tool_error_rate)}</td></tr>
                  <tr><td>denial rate</td><td className="num">{fmt.pct(s.rates.tool_denial_rate)}</td></tr>
                </tbody>
              </table>
            </div>
            <div className="card">
              <div className="card-head"><h2>Agent latency</h2></div>
              {s.latency.agents.length ? (
                <table>
                  <thead><tr><th>Role</th><th className="num">Runs</th><th className="num">Avg</th><th className="num">p95</th></tr></thead>
                  <tbody>
                    {s.latency.agents.map((a) => (
                      <tr key={a.role}><td>{a.role.replace("_", " ")}</td><td className="num">{a.count}</td><td className="num">{fmt.ms(a.avg_ms)}</td><td className="num">{fmt.ms(a.p95_ms)}</td></tr>
                    ))}
                  </tbody>
                </table>
              ) : <div className="empty">No agent runs yet.</div>}
            </div>
          </div>
          <div className="card">
            <div className="card-head"><h2>Recent tasks</h2><a href="#/tasks">All tasks →</a></div>
            {recent.data?.items.length ? (
              <table>
                <thead><tr><th>Title</th><th>Status</th><th>Seed</th><th className="num">Created</th></tr></thead>
                <tbody>
                  {recent.data.items.map((t) => (
                    <tr key={t.id} className="clickable" onClick={() => (window.location.hash = `#/tasks/${t.id}`)}>
                      <td><a href={`#/tasks/${t.id}`}>{t.title}</a></td><td><StatusBadge status={t.status} /></td>
                      <td className="mono">{t.workspace_seed}</td><td className="num muted">{fmt.ago(t.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : <div className="empty">No tasks yet. <a href="#/new">Create one →</a></div>}
          </div>
        </div>
      )}
    </>
  );
}
