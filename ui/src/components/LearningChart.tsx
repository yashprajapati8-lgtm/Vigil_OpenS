import { useLayoutEffect, useMemo, useRef, useState } from "react";
import type { Point } from "../api/types";
import { ms } from "../lib/format";

// While VIGIL learns a service's normal, show it happening: the service's p95
// latency as it arrives every 5 s (the zig-zag), and the normal band forming
// around it with the same robust maths the engine uses (detector.py):
//
//   normal  = median of what it has seen
//   spread  = max(1.4826 x MAD, 5 ms, 25% of the median)      (the p95 floors)
//   band    = normal +/- 3 spreads                            (VIGIL flags past z = 3)
//
// A reading inside the band is "near normal" and shapes it. A spike far outside
// barely moves it: a median ignores outliers, so one spike can't drag normal up.
// (After the first fit, VIGIL goes further: a flagged spike is never learned.)

const WINDOW = 36;            // 3 minutes of 5 s ticks: the learning window
const FLOOR_ABS = 5;          // ms
const FLOOR_REL = 0.25;
const Z = 3;

function median(xs: number[]): number {
  const s = [...xs].sort((a, b) => a - b);
  const mid = Math.floor(s.length / 2);
  if (s.length % 2 === 1) {
    return s[mid];
  }
  return (s[mid - 1] + s[mid]) / 2;
}

export interface Band { normal: number; low: number; high: number; n: number }

/** The band VIGIL is forming from these readings (null until there are a few). */
export function formingBand(values: number[]): Band | null {
  if (values.length < 4) {
    return null;
  }
  const normal = median(values);
  const deviations: number[] = [];
  for (const v of values) {
    deviations.push(Math.abs(v - normal));
  }
  const mad = median(deviations);
  const spread = Math.max(1.4826 * mad, FLOOR_ABS, FLOOR_REL * Math.abs(normal));
  return { normal, low: Math.max(0, normal - Z * spread), high: normal + Z * spread, n: values.length };
}

function useWidth<T extends HTMLElement>(): [React.RefObject<T>, number] {
  const ref = useRef<T>(null);
  const [w, setW] = useState(600);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => setW(Math.max(240, Math.round(el.getBoundingClientRect().width)));
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, w];
}

export function LearningChart({ service, history, progress }: { service: string; history: Point[]; progress: number }) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const H = 132;
  const pad = { l: 46, r: 12, t: 10, b: 20 };

  const view = useMemo(() => {
    const recent = history.slice(-WINDOW);
    // The readings since learning (re)started: the newest share of the window that
    // matches the progress. Older ones are drawn faded and never shape the band.
    const learnedCount = Math.max(4, Math.round(progress * WINDOW));
    const firstLearned = Math.max(0, recent.length - learnedCount);
    const pts: Array<{ i: number; v: number; learning: boolean }> = [];
    recent.forEach((p, i) => {
      const v = p.services[service]?.p95_ms;
      if (v != null && Number.isFinite(v)) {
        pts.push({ i, v, learning: i >= firstLearned });
      }
    });
    const band = formingBand(pts.filter((p) => p.learning).map((p) => p.v));
    // Scale to the band, not the biggest spike, so normal stays readable; spikes
    // above the top are drawn at the top edge with their real value on hover.
    const top = band ? Math.max(band.high * 1.6, band.normal * 2.2) : Math.max(...pts.map((p) => p.v), 1);
    return { pts, band, top, slots: Math.max(recent.length, WINDOW) };
  }, [history, service, progress]);

  const innerW = width - pad.l - pad.r;
  const innerH = H - pad.t - pad.b;
  const x = (i: number) => pad.l + (view.slots <= 1 ? 0 : (i / (view.slots - 1)) * innerW);
  const y = (v: number) => pad.t + innerH - (Math.min(v, view.top) / view.top) * innerH;
  const far = (v: number) => !!view.band && v > view.band.high;
  const line = view.pts.map((p) => `${x(p.i).toFixed(1)},${y(p.v).toFixed(1)}`).join(" ");
  const last = view.pts[view.pts.length - 1];
  const nFar = view.pts.filter((p) => p.learning && far(p.v)).length;
  const b = view.band;

  return (
    <figure className="lc" ref={ref}>
      <svg width={width} height={H} role="img"
        aria-label={b ? `${service} p95 latency while learning: normal so far ${ms(b.normal)}, band up to ${ms(b.high)}; ${nFar} far spikes ignored by the median`
          : `${service} p95 latency while learning`}>
        {/* the band VIGIL is forming, and its centre: they move as readings arrive */}
        {b && (
          <>
            <rect className="lc-band" x={pad.l} width={innerW} y={y(b.high)} height={Math.max(2, y(b.low) - y(b.high))} rx="6" />
            <line className="lc-normal" x1={pad.l} x2={pad.l + innerW} y1={y(b.normal)} y2={y(b.normal)} />
            <text className="lc-axis" x={pad.l - 8} y={y(b.normal) + 4} textAnchor="end">{ms(b.normal)}</text>
            <text className="lc-axis lc-axis-dim" x={pad.l - 8} y={y(b.high) + 4} textAnchor="end">{ms(b.high)}</text>
          </>
        )}
        <polyline className="lc-line" points={line} />
        {view.pts.map((p) => {
          const isFar = far(p.v);
          const clipped = p.v > view.top;
          return (
            <g key={p.i} className="lc-pt" data-far={isFar || undefined} data-old={!p.learning || undefined}>
              <circle cx={x(p.i)} cy={y(p.v)} r={isFar ? 4.5 : 3} />
              {clipped && <path d={`M ${x(p.i) - 4} ${y(p.v) + 2} l 4 -6 l 4 6`} className="lc-clip" />}
              <title>{`${ms(p.v)}: ${!p.learning ? "before learning started" : isFar ? "far spike: barely moves normal" : "near normal: shapes it"}`}</title>
            </g>
          );
        })}
        {last && <circle className="lc-now" cx={x(last.i)} cy={y(last.v)} r="7" key={`now-${last.i}-${last.v}`} />}
        {/* how far learning has come, along the bottom */}
        <line className="lc-track" x1={pad.l} x2={pad.l + innerW} y1={H - 4} y2={H - 4} />
        <line className="lc-progress" x1={pad.l} x2={pad.l + innerW * Math.min(1, progress)} y1={H - 4} y2={H - 4} />
      </svg>
      <figcaption className="lc-legend">
        <span><i className="lc-key lc-key-band" aria-hidden="true" />normal so far{b ? ` (${b.n} readings)` : ": needs a few readings"}</span>
        <span><i className="lc-key lc-key-near" aria-hidden="true" />near normal: shapes it</span>
        <span><i className="lc-key lc-key-far" aria-hidden="true" />far spike: barely moves it{nFar ? ` (${nFar})` : ""}</span>
      </figcaption>
    </figure>
  );
}
