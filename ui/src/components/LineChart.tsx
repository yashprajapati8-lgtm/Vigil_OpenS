import { useLayoutEffect, useRef, useState, type PointerEvent } from "react";
import { clock } from "../lib/format";

// A small hand-rolled SVG line chart: no charting library, so every mark is ours
// and the hover crosshair can be shared across every chart on screen. The SVG is
// drawn at its real pixel width, so text stays the same size at any chart size.

export interface Series { name: string; color: string; values: Array<number | null> }
export interface Marker { ts: number; color: string; label: string }
export interface Baseline { value: number; color: string; name?: string }

const PAD = { l: 48, r: 10, t: 12, b: 24 };

function niceMax(v: number): number {
  if (v <= 0) return 1;
  const p = 10 ** Math.floor(Math.log10(v));
  const n = v / p;
  // Steps whose halves stay round, so the middle gridline never reads "13%".
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 4 ? 4 : n <= 6 ? 6 : n <= 8 ? 8 : 10) * p;
}

export function LineChart({
  ts, series, fmt, tipFmt, floor = 0, ceiling, baselines = [], markers = [], band, hover, onHover, label,
  height = 140, tooltip = false,
}: {
  ts: number[];
  series: Series[];
  fmt: (v: number) => string;            // axis labels (short)
  tipFmt?: (v: number) => string;        // tooltip values (with units)
  floor?: number;
  ceiling?: number;
  baselines?: Baseline[];
  markers?: Marker[];
  band?: { from: number; to: number; label?: string } | null;
  hover: number | null;
  onHover: (i: number | null) => void;
  label: string;
  height?: number;
  tooltip?: boolean;
}) {
  const box = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(360);
  const [mine, setMine] = useState(false);
  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => setW(Math.max(220, Math.round(e.contentRect.width))));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  if (ts.length < 2) return <div ref={box}><p className="empty">Collecting data</p></div>;

  const VW = w;
  const VH = height;
  const t0 = ts[0];
  const t1 = ts[ts.length - 1];
  const all = series.flatMap((s) => s.values.filter((v): v is number => v != null));
  const rawMax = niceMax(Math.max(floor, ...all, ...baselines.map((b) => b.value)) * 1.08);
  const yMax = ceiling != null ? Math.min(rawMax, ceiling) : rawMax;
  const low = Math.min(0, ...all);
  const yMin = low < 0 ? -niceMax(-low * 1.08) : 0;
  const x = (t: number) => PAD.l + ((t - t0) / Math.max(1, t1 - t0)) * (VW - PAD.l - PAD.r);
  const y = (v: number) => PAD.t + (1 - (Math.max(yMin, Math.min(v, yMax)) - yMin) / (yMax - yMin)) * (VH - PAD.t - PAD.b);

  const path = (vals: Array<number | null>) => {
    let d = "";
    let pen = false;
    vals.forEach((v, i) => {
      if (v == null) { pen = false; return; }
      d += `${pen ? "L" : "M"}${x(ts[i]).toFixed(1)} ${y(v).toFixed(1)}`;
      pen = true;
    });
    return d;
  };

  const move = (e: PointerEvent<SVGSVGElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    const px = ((e.clientX - r.left) / r.width) * VW;
    const t = t0 + ((px - PAD.l) / (VW - PAD.l - PAD.r)) * (t1 - t0);
    let best = 0;
    for (let i = 1; i < ts.length; i++) if (Math.abs(ts[i] - t) < Math.abs(ts[best] - t)) best = i;
    setMine(true);
    onHover(best);
  };

  const grid = [yMin, (yMin + yMax) / 2, yMax];
  if (yMin < 0) grid.splice(1, 0, 0);
  const wide = VW >= 400;
  const minutes = Math.max(1, Math.round((t1 - t0) / 60));
  const hx = hover != null && hover < ts.length ? x(ts[hover]) : null;
  const step = ts.length > 1 ? (t1 - t0) / (ts.length - 1) : 5;
  const near = hover != null ? markers.filter((m) => Math.abs(m.ts - ts[hover]) <= step * 1.5) : [];
  const inBand = hover != null && band && ts[hover] >= band.from && ts[hover] <= band.to;
  const tf = tipFmt ?? fmt;

  return (
    <div className="linechart" ref={box}>
      <svg viewBox={`0 0 ${VW} ${VH}`} width={VW} height={VH} role="img" aria-label={label}
        onPointerMove={move} onPointerLeave={() => { setMine(false); onHover(null); }}>
        {band && band.to > t0 && (
          <rect x={x(Math.max(band.from, t0))} y={PAD.t} width={Math.max(2, x(Math.min(band.to, t1)) - x(Math.max(band.from, t0)))}
            height={VH - PAD.t - PAD.b} fill="#e0574c" fillOpacity={0.09} />
        )}
        {grid.map((v) => (
          <g key={v}>
            <line x1={PAD.l} x2={VW - PAD.r} y1={y(v)} y2={y(v)} stroke={v === 0 && yMin < 0 ? "rgba(237,231,226,0.18)" : "rgba(237,231,226,0.07)"} />
            <text x={PAD.l - 7} y={y(v) + 4} textAnchor="end" fontSize={11.5} fill="#958a83">{fmt(v)}</text>
          </g>
        ))}
        {wide ? (
          [t0, (t0 + t1) / 2, t1].map((t, i) => (
            <text key={i} x={x(t)} y={VH - 5} fontSize={11.5} fill="#958a83" textAnchor={i === 0 ? "start" : i === 2 ? "end" : "middle"}>{clock(t)}</text>
          ))
        ) : (
          <>
            <text x={PAD.l} y={VH - 5} fontSize={11.5} fill="#958a83">{minutes} min ago</text>
            <text x={VW - PAD.r} y={VH - 5} fontSize={11.5} fill="#958a83" textAnchor="end">now</text>
          </>
        )}
        {baselines.map((b, i) => (
          <line key={i} x1={PAD.l} x2={VW - PAD.r} y1={y(b.value)} y2={y(b.value)} stroke={b.color} strokeOpacity={0.55} strokeDasharray="3 4" />
        ))}
        {markers.filter((m) => m.ts >= t0 && m.ts <= t1).map((m, i) => (
          <line key={i} x1={x(m.ts)} x2={x(m.ts)} y1={PAD.t} y2={VH - PAD.b} stroke={m.color} strokeWidth={1.5}>
            <title>{m.label}</title>
          </line>
        ))}
        {series.map((s) => (
          <path key={s.name} d={path(s.values)} fill="none" stroke={s.color} strokeWidth={1.8} strokeLinejoin="round" strokeLinecap="round" />
        ))}
        {hx != null && (
          <g>
            <line x1={hx} x2={hx} y1={PAD.t} y2={VH - PAD.b} stroke="#ede7e2" strokeOpacity={0.45} />
            {series.map((s) => s.values[hover!] != null && (
              <circle key={s.name} cx={hx} cy={y(s.values[hover!]!)} r={4} fill={s.color} stroke="#0f0d0c" strokeWidth={1.5} />
            ))}
          </g>
        )}
        <rect x={PAD.l} y={PAD.t} width={Math.max(0, VW - PAD.l - PAD.r)} height={VH - PAD.t - PAD.b} fill="transparent" />
      </svg>
      {tooltip && mine && hx != null && (
        <div className="chart-tip" role="status"
          style={{ left: hx, transform: hx > VW * 0.6 ? "translateX(calc(-100% - 14px))" : "translateX(14px)" }}>
          <div className="chart-tip-time">{clock(ts[hover!])}{inBand ? `, during ${band?.label ?? "the incident"}` : ""}</div>
          {series.map((s) => {
            const v = s.values[hover!];
            const b = baselines.find((bl) => bl.name === s.name);
            return (
              <div key={s.name} className="chart-tip-row">
                <span className="swatch" style={{ background: s.color }} />
                <span className="chart-tip-name">{s.name}</span>
                <b>{v == null ? "no data" : tf(v)}</b>
                {b && <span className="chart-tip-normal">normal {tf(b.value)}</span>}
              </div>
            );
          })}
          {near.map((m, i) => <div key={i} className="chart-tip-event" style={{ color: m.color }}>{m.label}</div>)}
        </div>
      )}
    </div>
  );
}
