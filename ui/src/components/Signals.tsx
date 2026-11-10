import { Maximize2, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import type { Point } from "../api/types";
import { clock, ms, pct } from "../lib/format";
import { useVigil } from "../state/vigil";
import { LineChart, type Baseline, type Marker, type Series } from "./LineChart";

// Every chart here is drawn from /api/history plus the live stream: the same
// numbers VIGIL decided on. The panel shows four; clicking it opens all of them.

const SVC = [
  { name: "payment", raw: "#e3c29b" },
  { name: "inventory", raw: "#8fb8ff" },
] as const;

type SvcMetric = "p95_ms" | "error_rate" | "cpu_pct" | "mem_mb" | "mem_slope_mb_s" | "rps" | "cpu_per_req";

interface ChartDef {
  key: string;
  title: string;
  label: string;
  axis: (v: number) => string;
  tip: (v: number) => string;
  floor: number;
  ceiling?: number;
  series: (h: Point[]) => Series[];
  baselines?: Baseline[];
  now: (p: Point | undefined) => Array<{ color: string; text: string }>;
}

const secs = (v: number) => (v >= 1000 ? `${(v / 1000).toFixed(1)}s` : `${Math.round(v)}`);
const whole = (unit: string) => (v: number) => `${Math.round(v)}${unit}`;

function useCharts(): { defs: ChartDef[]; byKey: Record<string, ChartDef> } {
  const { verdicts } = useVigil();
  return useMemo(() => {
    const svc = (m: SvcMetric): Pick<ChartDef, "series" | "baselines"> => ({
      series: (h) => SVC.map((s) => ({ name: s.name, color: s.raw, values: h.map((p) => p.services[s.name]?.[m] ?? null) })),
      baselines: SVC.flatMap((s) => {
        const v = verdicts[s.name]?.baseline?.medians[m];
        return v != null ? [{ value: v, color: s.raw, name: s.name }] : [];
      }),
    });
    const svcNow = (m: SvcMetric, f: (v: number | null) => string) => (p: Point | undefined) =>
      SVC.map((s) => ({ color: s.raw, text: f(p?.services[s.name]?.[m] ?? null) }));
    const n = (f: (v: number) => string) => (v: number | null) => (v == null ? "–" : f(v));

    const defs: ChartDef[] = [
      {
        key: "fail", title: "Checkouts failing", label: "Share of checkouts failing over time",
        axis: (v) => `${Math.round(v * 100)}%`, tip: (v) => pct(v), floor: 0.05, ceiling: 1,
        series: (h) => [{ name: "checkouts failing", color: "#f9a39b", values: h.map((p) => p.user.fail_ratio) }],
        now: (p) => [{ color: "#f9a39b", text: `${pct(p?.user.fail_ratio ?? null)} of ${p?.user.checkout_rps?.toFixed(0) ?? "–"}/s` }],
      },
      {
        key: "traffic", title: "Checkout traffic", label: "Checkouts per second over time",
        axis: whole(""), tip: (v) => `${v.toFixed(1)}/s`, floor: 10,
        series: (h) => [{ name: "checkouts per second", color: "#d8cfc8", values: h.map((p) => p.user.checkout_rps) }],
        now: (p) => [{ color: "#d8cfc8", text: p?.user.checkout_rps != null ? `${p.user.checkout_rps.toFixed(1)}/s` : "–" }],
      },
      {
        key: "checkout-p95", title: "Checkout latency p95", label: "p95 time to complete a checkout",
        axis: secs, tip: (v) => ms(v), floor: 200,
        series: (h) => [{ name: "checkout p95", color: "#d8cfc8", values: h.map((p) => p.user.p95_ms) }],
        now: (p) => [{ color: "#d8cfc8", text: ms(p?.user.p95_ms) }],
      },
      { key: "p95", title: "Latency p95", label: "p95 latency of payment and inventory", axis: secs, tip: (v) => ms(v), floor: 100,
        ...svc("p95_ms"), now: svcNow("p95_ms", ms) },
      { key: "errors", title: "Error rate", label: "Share of requests that fail, per service", axis: (v) => `${Math.round(v * 100)}%`, tip: (v) => pct(v), floor: 0.05, ceiling: 1,
        ...svc("error_rate"), now: svcNow("error_rate", (v) => pct(v)) },
      { key: "edge", title: "payment → inventory calls failing", label: "Share of payment's calls to inventory that fail or time out",
        axis: (v) => `${Math.round(v * 100)}%`, tip: (v) => pct(v), floor: 0.05, ceiling: 1,
        series: (h) => [{ name: "calls failing", color: "#8fb8ff", values: h.map((p) => p.edges.find((e) => e.caller === "payment" && e.target === "inventory")?.fail_ratio ?? null) }],
        now: (p) => [{ color: "#8fb8ff", text: pct(p?.edges.find((e) => e.caller === "payment" && e.target === "inventory")?.fail_ratio ?? null) }] },
      { key: "cpu", title: "CPU", label: "CPU of payment and inventory", axis: whole("%"), tip: (v) => `${v.toFixed(1)}%`, floor: 20,
        ...svc("cpu_pct"), now: svcNow("cpu_pct", n((v) => `${Math.round(v)}%`)) },
      { key: "mem", title: "Memory", label: "Memory of payment and inventory", axis: whole(""), tip: (v) => `${v.toFixed(1)} MB`, floor: 80,
        ...svc("mem_mb"), now: svcNow("mem_mb", n((v) => `${Math.round(v)} MB`)) },
      { key: "mem-growth", title: "Memory growth", label: "How fast memory grows: a leak shows as a steady positive line", axis: (v) => v.toFixed(1), tip: (v) => `${v.toFixed(2)} MB/s`, floor: 1,
        ...svc("mem_slope_mb_s"), now: svcNow("mem_slope_mb_s", n((v) => `${v.toFixed(1)} MB/s`)) },
      { key: "rps", title: "Requests per second", label: "Requests per second served by each service", axis: whole(""), tip: (v) => `${v.toFixed(1)}/s`, floor: 10,
        ...svc("rps"), now: svcNow("rps", n((v) => `${v.toFixed(0)}/s`)) },
      { key: "cpu-req", title: "CPU per request", label: "CPU percent per request per second: rises when work gets stuck, flat under real load", axis: (v) => v.toFixed(1), tip: (v) => v.toFixed(2), floor: 1,
        ...svc("cpu_per_req"), now: svcNow("cpu_per_req", n((v) => v.toFixed(2))) },
    ];
    return { defs, byKey: Object.fromEntries(defs.map((d) => [d.key, d])) };
  }, [verdicts]);
}

function useOverlays() {
  const { focus, events } = useVigil();
  const onset = focus ? Math.min(...Object.values(focus.data.onset ?? {}), focus.opened_at) : null;
  const band = focus && onset ? { from: onset, to: focus.closed_at ?? Date.now() / 1000, label: focus.id } : null;
  const markers: Marker[] = events
    .filter((e) => focus && e.incident_id === focus.id && ["action_done", "resolved", "escalated"].includes(e.kind))
    .map((e) => ({ ts: e.ts, label: e.summary, color: e.kind === "action_done" ? "#4fd1c5" : e.kind === "resolved" ? "#3fae6e" : "#e0574c" }));
  return { band, markers };
}

function ChartCard({ def, history, ts, hover, setHover, height, tooltip }: {
  def: ChartDef; history: Point[]; ts: number[]; hover: number | null; setHover: (i: number | null) => void; height?: number; tooltip?: boolean;
}) {
  const { band, markers } = useOverlays();
  const point = history[hover ?? history.length - 1];
  return (
    <div className="chart">
      <div className="chart-head">
        <span className="chart-title">{def.title}</span>
        <span className="chart-now">
          {def.now(point).map((v, i) => <span key={i}><span className="swatch" style={{ background: v.color }} />{v.text}</span>)}
        </span>
      </div>
      <LineChart ts={ts} hover={hover} onHover={setHover} band={band} markers={markers} label={def.label}
        fmt={def.axis} tipFmt={def.tip} floor={def.floor} ceiling={def.ceiling}
        series={def.series(history)} baselines={def.baselines} height={height} tooltip={tooltip} />
    </div>
  );
}

function Key({ full = false }: { full?: boolean }) {
  return (
    <div className="chart-key">
      {SVC.map((s) => <span key={s.name}><span className="swatch" style={{ background: s.raw }} />{s.name}</span>)}
      <span><svg width="22" height="6" aria-hidden="true"><line x1="0" y1="3" x2="22" y2="3" stroke="#b8ada6" strokeDasharray="3 4" /></svg>VIGIL's learned normal</span>
      <span><span className="swatch" style={{ background: "rgba(224,87,76,0.4)" }} />incident</span>
      <span><span className="swatch" style={{ background: "#4fd1c5" }} />VIGIL acted</span>
      {full && <span><span className="swatch" style={{ background: "#3fae6e" }} />verified recovered</span>}
    </div>
  );
}

export function Signals() {
  const { history } = useVigil();
  const { byKey } = useCharts();
  const [hover, setHover] = useState<number | null>(null);
  const [open, setOpen] = useState(false);
  const ts = useMemo(() => history.map((p) => p.ts), [history]);
  const point = hover != null ? history[hover] : undefined;

  return (
    <>
      <section className="panel area-signals signals-panel" aria-labelledby="signals-title"
        onClick={(e) => { if (!(e.target as HTMLElement).closest("button")) setOpen(true); }}>
        <div className="panel-head">
          <h2 id="signals-title">Signals</h2>
          <span className="panel-head-end">
            <span className="panel-sub">{point ? `at ${clock(point.ts)}` : "last 15 minutes"}</span>
            <button type="button" className="expand-btn" onClick={() => setOpen(true)} aria-haspopup="dialog">
              <Maximize2 size={14} /> All signals
            </button>
          </span>
        </div>
        <p className="panel-caption">Four health signals against what VIGIL learned is normal (the dashed line). Click anywhere here to see every signal.</p>
        <div className="signals">
          {["fail", "p95", "cpu", "mem"].map((k) => (
            <ChartCard key={k} def={byKey[k]} history={history} ts={ts} hover={hover} setHover={setHover} />
          ))}
        </div>
        <Key />
      </section>
      {open && <SignalsExplorer onClose={() => setOpen(false)} />}
    </>
  );
}

function SignalsExplorer({ onClose }: { onClose: () => void }) {
  const { history } = useVigil();
  const { byKey } = useCharts();
  const [hover, setHover] = useState<number | null>(null);
  const ts = useMemo(() => history.map((p) => p.ts), [history]);
  const dialogRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const before = document.activeElement as HTMLElement | null;
    dialogRef.current?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = "";
      before?.focus();
    };
  }, [onClose]);

  const group = (title: string, sub: string, keys: string[]) => (
    <section className="explorer-group" aria-label={title}>
      <div className="explorer-group-head"><h3>{title}</h3><span className="panel-sub">{sub}</span></div>
      <div className="explorer-grid">
        {keys.map((k) => (
          <ChartCard key={k} def={byKey[k]} history={history} ts={ts} hover={hover} setHover={setHover} height={200} tooltip />
        ))}
      </div>
    </section>
  );

  return (
    <div className="explorer-scrim" onClick={onClose}>
      <div ref={dialogRef} tabIndex={-1} className="explorer panel" role="dialog" aria-modal="true" aria-labelledby="explorer-title" onClick={(e) => e.stopPropagation()}>
        <div className="explorer-head">
          <div>
            <h2 id="explorer-title">All signals</h2>
            <p className="panel-sub">
              {hover != null && history[hover] ? `Showing ${clock(history[hover].ts)}. ` : "Last 15 minutes, one point every 5 seconds. "}
              Hover any line to see that moment on every chart.
            </p>
          </div>
          <button type="button" className="icon-btn" onClick={onClose} aria-label="Close all signals"><X size={18} /></button>
        </div>
        <div className="explorer-body">
          {group("What users feel", "measured at the storefront", ["fail", "traffic", "checkout-p95"])}
          {group("payment and inventory", "per service, against what VIGIL learned is normal", ["p95", "errors", "edge", "cpu", "mem", "mem-growth", "rps", "cpu-req"])}
        </div>
        <Key full />
      </div>
    </div>
  );
}
