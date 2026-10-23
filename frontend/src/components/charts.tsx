// Hand-built SVG charts (no chart library). Thin marks, 2px surface gaps, hover tooltips on every
// mark, and visible labels/tables so no value is reachable only by hovering or by color.
import { useMemo, useState } from "react";
import type { Span } from "../api";
import { fmt, statusColor, useTooltip } from "./ui";

// ---------------------------------------------------------------------- status breakdown

const STATUS_GROUPS: { label: string; statuses: string[]; color: string }[] = [
  { label: "Completed", statuses: ["COMPLETED"], color: statusColor("COMPLETED") },
  { label: "In progress", statuses: ["CREATED", "PLANNING", "RUNNING", "VALIDATING"], color: "var(--series-1)" },
  { label: "Awaiting approval", statuses: ["AWAITING_APPROVAL"], color: statusColor("AWAITING_APPROVAL") },
  { label: "Recovery pending", statuses: ["RECOVERY_PENDING", "PAUSED"], color: statusColor("RECOVERY_PENDING") },
  { label: "Failed", statuses: ["FAILED"], color: statusColor("FAILED") },
  { label: "Cancelled", statuses: ["CANCELLED"], color: "var(--neutral-fill)" },
];

export function StatusBreakdown({ byStatus }: { byStatus: Record<string, number> }) {
  const tip = useTooltip();
  const groups = STATUS_GROUPS.map((g) => ({ ...g, n: g.statuses.reduce((a, s) => a + (byStatus[s] ?? 0), 0) }));
  const total = groups.reduce((a, g) => a + g.n, 0);
  if (!total) return <div className="empty">No tasks yet.</div>;
  return (
    <div>
      <div style={{ display: "flex", gap: 2, height: 14, borderRadius: 4, overflow: "hidden" }} role="img"
           aria-label={groups.map((g) => `${g.label} ${g.n}`).join(", ")}>
        {groups.filter((g) => g.n > 0).map((g) => (
          <div key={g.label} tabIndex={0}
               style={{ flex: g.n, background: g.color, minWidth: 4, cursor: "default" }}
               onPointerMove={(e) => tip.show(e, <><strong>{g.n}</strong> {g.label.toLowerCase()} · {fmt.pct(g.n / total)}</>)}
               onFocus={(e) => tip.showAt(e.currentTarget, <><strong>{g.n}</strong> {g.label.toLowerCase()}</>)}
               onPointerLeave={tip.hide} onBlur={tip.hide} />
        ))}
      </div>
      <div className="legend">
        {groups.map((g) => (
          <span key={g.label}><i className="swatch" style={{ background: g.color }} />{g.label} <b style={{ color: "var(--text-primary)" }}>{g.n}</b></span>
        ))}
      </div>
      {tip.node}
    </div>
  );
}

// ------------------------------------------------------------------- tasks created per hour

export function HourlyTasks({ timeline }: { timeline: { hour: string; status: string; count: number }[] }) {
  const tip = useTooltip();
  const [hover, setHover] = useState<number | null>(null);
  const buckets = useMemo(() => {
    const now = new Date();
    now.setMinutes(0, 0, 0);
    const arr = Array.from({ length: 24 }, (_, i) => ({ t: new Date(now.getTime() - (23 - i) * 3600_000), n: 0 }));
    for (const r of timeline) {
      const t = new Date(r.hour).getTime();
      const b = arr.find((x) => x.t.getTime() === t);
      if (b) b.n += r.count;
    }
    return arr;
  }, [timeline]);
  const max = Math.max(1, ...buckets.map((b) => b.n));
  const W = 640, H = 150, pad = { l: 28, r: 6, t: 8, b: 22 };
  const bw = (W - pad.l - pad.r) / buckets.length;
  const y = (v: number) => pad.t + (H - pad.t - pad.b) * (1 - v / max);
  const ticks = max <= 4 ? Array.from({ length: max + 1 }, (_, i) => i) : [0, Math.round(max / 2), max];
  return (
    <div>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img" aria-label="Tasks created per hour, last 24 hours">
        {ticks.map((v) => (
          <g key={v}>
            <line x1={pad.l} x2={W - pad.r} y1={y(v)} y2={y(v)} stroke={v === 0 ? "var(--baseline)" : "var(--grid)"} strokeWidth={1} />
            <text x={pad.l - 6} y={y(v) + 4} textAnchor="end">{v}</text>
          </g>
        ))}
        {buckets.map((b, i) => {
          const h = y(0) - y(b.n);
          const x = pad.l + i * bw + 1;
          return (
            <g key={i}>
              {b.n > 0 && (
                <path d={roundedTop(x, y(b.n), bw - 2, h, Math.min(4, h))}
                      fill="var(--series-1)" opacity={hover === null || hover === i ? 1 : 0.55} />
              )}
              <rect x={x - 1} y={pad.t} width={bw} height={H - pad.t - pad.b} fill="transparent" tabIndex={0}
                    onPointerMove={(e) => { setHover(i); tip.show(e, <><strong>{b.n}</strong> task{b.n === 1 ? "" : "s"} · {b.t.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</>); }}
                    onFocus={(e) => { setHover(i); tip.showAt(e.currentTarget, <><strong>{b.n}</strong> tasks</>); }}
                    onPointerLeave={() => { setHover(null); tip.hide(); }} onBlur={() => { setHover(null); tip.hide(); }} />
              {i % 6 === 0 && <text x={x + bw / 2} y={H - 6} textAnchor="middle">{b.t.toLocaleTimeString([], { hour: "2-digit" })}</text>}
            </g>
          );
        })}
      </svg>
      {tip.node}
    </div>
  );
}

function roundedTop(x: number, y: number, w: number, h: number, r: number): string {
  if (h <= 0) return "";
  r = Math.min(r, w / 2, h);
  return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
}

// -------------------------------------------------------------------- reported vs estimated

export function UsageSplit({ reported, estimated }: { reported: number; estimated: number }) {
  const tip = useTooltip();
  const total = reported + estimated;
  if (!total) return <div className="empty">No LLM calls recorded.</div>;
  const parts = [
    { label: "Provider-reported", n: reported, color: "var(--series-1)" },
    { label: "Estimated", n: estimated, color: "var(--series-2)" },
  ];
  return (
    <div>
      <div style={{ display: "flex", gap: 2, height: 14, borderRadius: 4, overflow: "hidden" }}>
        {parts.filter((p) => p.n > 0).map((p) => (
          <div key={p.label} tabIndex={0} style={{ flex: p.n, background: p.color, minWidth: 4 }}
               onPointerMove={(e) => tip.show(e, <><strong>{fmt.num(p.n)}</strong> tokens · {p.label.toLowerCase()}</>)}
               onFocus={(e) => tip.showAt(e.currentTarget, <><strong>{fmt.num(p.n)}</strong> tokens</>)}
               onPointerLeave={tip.hide} onBlur={tip.hide} />
        ))}
      </div>
      <div className="legend">
        {parts.map((p) => (
          <span key={p.label}><i className="swatch" style={{ background: p.color }} />{p.label} <b style={{ color: "var(--text-primary)" }}>{fmt.num(p.n)}</b></span>
        ))}
      </div>
      {tip.node}
    </div>
  );
}

// --------------------------------------------------------------------------- trace waterfall

const SPAN_KINDS: { key: string; label: string; color: string; test: (n: string) => boolean }[] = [
  { key: "http", label: "HTTP / run", color: "var(--series-1)", test: (n) => n.startsWith("HTTP") || n === "task run" || n.startsWith("fastapi") },
  { key: "node", label: "Graph node", color: "var(--series-2)", test: (n) => n.startsWith("node ") },
  { key: "agent", label: "Agent", color: "var(--series-3)", test: (n) => n.startsWith("agent ") },
  { key: "llm", label: "LLM call", color: "var(--series-4)", test: (n) => n.startsWith("llm ") },
  { key: "tool", label: "Tool call", color: "var(--series-5)", test: (n) => n.startsWith("tool ") },
];

function kindOf(name: string) {
  return SPAN_KINDS.find((k) => k.test(name)) ?? SPAN_KINDS[0];
}

export function TraceWaterfall({ spans }: { spans: Span[] }) {
  const tip = useTooltip();
  const [filter, setFilter] = useState<string>("all");
  const rows = useMemo(() => {
    const byId = new Map(spans.map((s) => [s.span_id, s]));
    const depth = (s: Span): number => {
      let d = 0, cur = s;
      while (cur.parent_span_id && byId.has(cur.parent_span_id) && d < 20) { cur = byId.get(cur.parent_span_id)!; d++; }
      return d;
    };
    return [...spans].sort((a, b) => a.start_time.localeCompare(b.start_time)).map((s) => ({ s, depth: depth(s), kind: kindOf(s.name) }));
  }, [spans]);
  if (!spans.length) return <div className="empty">No spans recorded for this task yet (spans are exported in batches every ~1 s).</div>;
  const t0 = Math.min(...spans.map((s) => new Date(s.start_time).getTime()));
  const t1 = Math.max(...spans.map((s) => new Date(s.end_time).getTime()));
  const span = Math.max(1, t1 - t0);
  const shown = rows.filter((r) => filter === "all" || r.kind.key === filter).slice(0, 400);
  const traces = new Set(spans.map((s) => s.trace_id));
  return (
    <div>
      <div className="row" style={{ marginBottom: 10 }}>
        <select value={filter} onChange={(e) => setFilter(e.target.value)} style={{ width: "auto" }} aria-label="Filter span kind">
          <option value="all">All spans ({spans.length})</option>
          {SPAN_KINDS.map((k) => <option key={k.key} value={k.key}>{k.label} ({rows.filter((r) => r.kind.key === k.key).length})</option>)}
        </select>
        <span className="muted small">{traces.size === 1 ? "1 correlated trace" : `${traces.size} traces`} · {fmt.ms(span)} total · trace id <code>{[...traces][0]?.slice(0, 16)}…</code></span>
      </div>
      <div className="legend" style={{ marginTop: 0, marginBottom: 8 }}>
        {SPAN_KINDS.map((k) => <span key={k.key}><i className="swatch" style={{ background: k.color }} />{k.label}</span>)}
      </div>
      <div style={{ maxHeight: 560, overflow: "auto", border: "1px solid var(--grid)", borderRadius: 8 }}>
        <table>
          <thead><tr><th style={{ width: "34%" }}>Span</th><th className="num" style={{ width: 80 }}>Duration</th><th>Timeline</th></tr></thead>
          <tbody>
            {shown.map(({ s, depth, kind }) => {
              const start = new Date(s.start_time).getTime() - t0;
              const left = (start / span) * 100;
              const width = Math.max(0.4, (s.duration_ms / span) * 100);
              return (
                <tr key={s.span_id}>
                  <td style={{ paddingLeft: 10 + Math.min(depth, 10) * 12 }}>
                    <span className="mono" style={{ color: s.status_code === "ERROR" ? "var(--critical-text)" : undefined }}>{s.name}</span>
                    {s.status_code === "ERROR" && <span className="small" style={{ color: "var(--critical-text)" }}> ✕ error</span>}
                  </td>
                  <td className="num small">{fmt.ms(s.duration_ms)}</td>
                  <td style={{ position: "relative", minWidth: 220 }}>
                    <div tabIndex={0}
                         style={{ position: "absolute", left: `${left}%`, width: `${width}%`, top: 11, height: 10, borderRadius: 4,
                                  background: kind.color, minWidth: 3 }}
                         onPointerMove={(e) => tip.show(e, <div><strong>{fmt.ms(s.duration_ms)}</strong> {s.name}<div className="muted">+{fmt.ms(start)} · {s.service} · {s.kind.toLowerCase()}</div>{Object.entries(s.attributes).filter(([k]) => !k.startsWith("agentos.")).slice(0, 6).map(([k, v]) => <div key={k} className="mono small">{k}={String(v)}</div>)}</div>)}
                         onFocus={(e) => tip.showAt(e.currentTarget, <><strong>{fmt.ms(s.duration_ms)}</strong> {s.name}</>)}
                         onPointerLeave={tip.hide} onBlur={tip.hide} />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {rows.length > 400 && <div className="muted small" style={{ marginTop: 6 }}>Showing the first 400 of {rows.length} spans.</div>}
      {tip.node}
    </div>
  );
}
