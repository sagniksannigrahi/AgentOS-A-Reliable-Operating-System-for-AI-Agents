import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { ApiError } from "../api";

// ------------------------------------------------------------------------------- status

const STATUS_COLOR: Record<string, string> = {
  COMPLETED: "var(--good)",
  FAILED: "var(--critical)",
  AWAITING_APPROVAL: "var(--warning)",
  RECOVERY_PENDING: "var(--serious)",
  PAUSED: "var(--serious)",
  RUNNING: "var(--series-1)",
  PLANNING: "var(--series-1)",
  VALIDATING: "var(--series-1)",
  CREATED: "var(--neutral-fill)",
  CANCELLED: "var(--neutral-fill)",
};
const STATUS_ICON: Record<string, string> = {
  COMPLETED: "✓", FAILED: "✕", AWAITING_APPROVAL: "!", RECOVERY_PENDING: "↻", PAUSED: "‖",
  RUNNING: "▸", PLANNING: "▸", VALIDATING: "▸", CREATED: "•", CANCELLED: "–",
};

export function statusColor(s: string): string {
  return STATUS_COLOR[s] ?? "var(--neutral-fill)";
}

export function StatusBadge({ status }: { status: string }) {
  return (
    <span className="badge" title={status}>
      <span className="dot" style={{ background: statusColor(status) }} />
      <span aria-hidden>{STATUS_ICON[status] ?? ""}</span>
      {status.replace(/_/g, " ").toLowerCase()}
    </span>
  );
}

const EVENT_COLOR: Record<string, string> = {
  success: "var(--good)", error: "var(--critical)", warning: "var(--warning)", info: "var(--neutral-fill)",
};
export function eventColor(status: string): string {
  return EVENT_COLOR[status] ?? "var(--neutral-fill)";
}

export function StatTile({ label, value, note }: { label: string; value: ReactNode; note?: ReactNode }) {
  return (
    <div className="card">
      <div className="tile-label">{label}</div>
      <div className="tile-value">{value}</div>
      {note != null && <div className="tile-note">{note}</div>}
    </div>
  );
}

export function ErrorNote({ error }: { error: unknown }) {
  if (!error) return null;
  const msg = error instanceof ApiError ? `${error.message} (${error.code}, HTTP ${error.status})` : String(error);
  const hint = error instanceof ApiError && error.status === 401 ? " Set a valid API key under Settings." : "";
  return <div className="callout error">{msg}{hint}</div>;
}

// -------------------------------------------------------------------------------- format

export const fmt = {
  pct: (v: number | null | undefined) => (v == null ? "—" : `${(v * 100).toFixed(v >= 0.995 || v === 0 ? 0 : 1)}%`),
  num: (v: number | null | undefined) => (v == null ? "—" : v.toLocaleString()),
  ms: (v: number | null | undefined) => (v == null ? "—" : v >= 1000 ? `${(v / 1000).toFixed(2)} s` : `${Math.round(v)} ms`),
  usd: (v: number | string | null | undefined) => {
    const n = Number(v ?? 0);
    return n === 0 ? "$0" : n < 0.01 ? `$${n.toFixed(5)}` : `$${n.toFixed(4)}`;
  },
  time: (iso: string) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }),
  ago: (iso: string) => {
    const s = (Date.now() - new Date(iso).getTime()) / 1000;
    if (s < 60) return `${Math.max(0, Math.round(s))}s ago`;
    if (s < 3600) return `${Math.round(s / 60)}m ago`;
    if (s < 86400) return `${Math.round(s / 3600)}h ago`;
    return new Date(iso).toLocaleDateString();
  },
  short: (id: string | null | undefined) => (id ? id.slice(0, 8) : "—"),
};

// --------------------------------------------------------------------------------- hooks

/** Load data; optionally re-poll while ``pollMs`` is set. Keeps the previous data while refetching. */
export function useAsync<T>(fn: () => Promise<T>, deps: unknown[], pollMs?: number | null) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const load = useCallback(async () => {
    setLoading(true);
    try {
      setData(await fnRef.current());
      setError(null);
    } catch (e) {
      setError(e);
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  useEffect(() => {
    if (!pollMs) return;
    const t = setInterval(() => void load(), pollMs);
    return () => clearInterval(t);
  }, [pollMs, load]);
  return { data, error, loading, reload: load };
}

// ------------------------------------------------------------------------------- tooltip

export interface TipState { x: number; y: number; content: ReactNode }

export function useTooltip() {
  const [tip, setTip] = useState<TipState | null>(null);
  const show = (e: { clientX: number; clientY: number }, content: ReactNode) => setTip({ x: e.clientX, y: e.clientY, content });
  const showAt = (el: Element, content: ReactNode) => {
    const r = el.getBoundingClientRect();
    setTip({ x: r.left + r.width / 2, y: r.top, content });
  };
  const hide = () => setTip(null);
  const node = tip ? (
    <div className="tooltip" role="tooltip" style={{ left: Math.min(tip.x + 12, window.innerWidth - 370), top: tip.y + 14 }}>
      {tip.content}
    </div>
  ) : null;
  return { show, showAt, hide, node };
}

// --------------------------------------------------------------------------------- router

export function useHashRoute(): string[] {
  const parse = () => (window.location.hash.replace(/^#\/?/, "") || "overview").split("/");
  const [route, setRoute] = useState(parse);
  useEffect(() => {
    const on = () => setRoute(parse());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return route;
}

export function JsonView({ value }: { value: unknown }) {
  return <pre className="block">{JSON.stringify(value, null, 2)}</pre>;
}
