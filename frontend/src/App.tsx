import { api, getApiKey } from "./api";
import { useAsync, useHashRoute } from "./components/ui";
import { Approvals, Settings } from "./pages/Approvals";
import { Overview } from "./pages/Overview";
import { TaskDetail } from "./pages/TaskDetail";
import { NewTask, TaskList } from "./pages/Tasks";

const NAV = [
  ["overview", "Overview"],
  ["tasks", "Tasks"],
  ["new", "New task"],
  ["approvals", "Approvals"],
  ["settings", "Settings"],
] as const;

export function App() {
  const route = useHashRoute();
  const hasKey = !!getApiKey();
  const pending = useAsync(() => (hasKey ? api.approvals("PENDING") : Promise.resolve(null)), [hasKey], 5000);
  const [page, id, tab] = route;
  let body;
  if (!hasKey && page !== "settings") {
    body = (
      <div className="card" style={{ maxWidth: 560 }}>
        <h2>Connect to the AgentOS API</h2>
        <p className="secondary">This dashboard talks to the real backend. Add an API key to continue.</p>
        <a className="btn primary" href="#/settings">Add API key</a>
      </div>
    );
  } else if (page === "tasks" && id) body = <TaskDetail key={id} id={id} tab={tab} />;
  else if (page === "tasks") body = <TaskList />;
  else if (page === "new") body = <NewTask />;
  else if (page === "approvals") body = <Approvals />;
  else if (page === "settings") body = <Settings />;
  else body = <Overview />;
  const count = pending.data?.total ?? 0;
  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark" aria-hidden>
            <svg width="16" height="16" viewBox="0 0 16 16"><path d="M3 12l5-8 5 8" stroke="#fff" strokeWidth="2.2" fill="none" strokeLinecap="round" strokeLinejoin="round" /></svg>
          </span>
          AgentOS
        </div>
        <nav className="nav">
          {NAV.map(([key, label]) => (
            <a key={key} href={`#/${key}`} className={page === key || (key === "overview" && !NAV.some(([k]) => k === page)) ? "active" : ""}>
              {label}
              {key === "approvals" && count > 0 && <span className="badge small" style={{ padding: "0 7px" }}><span className="dot" style={{ background: "var(--warning)" }} />{count}</span>}
            </a>
          ))}
        </nav>
        <div className="sidebar-foot">
          <span>Reliable multi-agent orchestration</span>
          <a href="/docs" target="_blank" rel="noreferrer">API docs ↗</a>
        </div>
      </aside>
      <main>{body}</main>
    </div>
  );
}
