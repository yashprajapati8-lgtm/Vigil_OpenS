import type { EdgeSample, Incident, Point, ServiceSample, Verdict } from "../api/types";
import { TERMINAL } from "../api/types";
import { api } from "../api/client";
import { isDemand, trafficMultiple } from "../lib/demand";
import { CATEGORY_LABEL, ms, pct } from "../lib/format";
import { useOperator } from "../lib/operator";
import { columns, upper, useSite, type Site } from "../lib/site";
import { Tween } from "../lib/tween";
import { useVigil } from "../state/vigil";
import { REPLAY } from "../replay/player";
import { Check, Smartphone, TrendingUp } from "lucide-react";
import { useCallback, useLayoutEffect, useState, type CSSProperties, type ReactNode } from "react";
import { usePhones, rupees, type PhonePayment } from "../lib/phones";

// The hero. Every watched service and the calls between them, laid out from the
// site's real call graph (vigil.yaml + what Prometheus shows). Each card says in
// plain words what is happening to it, shows every number next to what VIGIL
// learned is normal, and marks where the blame walk stopped.
//
// A straight chain (the demo: storefront -> payment -> inventory) is drawn as
// before, cards and wires in one row. When a service calls several others, the
// cards stack in columns by call depth and the calls are drawn as curves measured
// from the cards themselves.

type Look = "ok" | "learning" | "suspect" | "bad" | "root" | "symptom" | "acting" | "down";

// The demo's own words. Any other site gets the generic line.
const DEMO_HEALTHY: Record<string, string> = {
  payment: "Healthy: cards are charged as fast as usual.",
  inventory: "Healthy: stock checks answer as fast as usual.",
};
const DOING: Record<string, [string, string]> = {
  // action: [while acting, while verifying]
  restart: ["VIGIL is restarting it", "Restarted. Checking customers recover"],
  rollback: ["VIGIL is rolling it back", "Rolled back. Checking customers recover"],
  scale_out: ["VIGIL is adding a replica", "Replica added. Checking customers recover"],
};

type Stats = { fail_ratio: number | null; p95_ms: number | null };

function edgeBad(e: Stats | undefined) {
  return !!e && ((e.fail_ratio ?? 0) >= 0.05 || (e.p95_ms ?? 0) >= 500);
}

interface Card { look: Look; state: string; story: string }

function entryCard(site: Site, inc: Incident | null, latest: Point | null, verdicts: Record<string, Verdict>): Card {
  const open = inc && !TERMINAL.includes(inc.status) ? inc : null;
  const fail = latest?.user.fail_ratio ?? 0;
  const busy = Object.values(verdicts).find((x) => x.flagged && isDemand(x));
  const x = busy ? trafficMultiple(busy, latest?.services[busy.service]?.rps) : null;
  if (fail >= 0.02) {
    return { look: "bad", state: "customers affected",
      story: `${pct(fail)} of ${site.units} are failing right now.${open ? " VIGIL is already on it." : ""}` };
  }
  return { look: "ok", state: "customers fine",
    story: x ? `${upper(site.units)} go through normally, at ${x} the usual traffic.` : `${upper(site.units)} go through normally.` };
}

function cardFor(site: Site, name: string, v: Verdict | undefined, inc: Incident | null, latest: Point | null): Card {
  const open = inc && !TERMINAL.includes(inc.status) ? inc : null;
  const reason = upper(v?.reasons?.[0] ?? "");
  if (open && (open.status === "REMEDIATING" || open.status === "VERIFYING") && open.data.decision?.target === name) {
    const [doing, checking] = DOING[open.data.decision.action ?? "restart"] ?? DOING.restart;
    return open.status === "REMEDIATING"
      ? { look: "acting", state: "VIGIL acting", story: `${doing} through the locked actuator.` }
      : { look: "acting", state: "fix applied", story: `${checking}: 4 clean checks in a row.` };
  }
  if (!v) return { look: "ok", state: "no data", story: "No measurements yet." };
  if (v.state === "down") return { look: "down", state: "down", story: "Not answering at all. Every call to it fails." };
  if (open?.data.diagnosis && open.services.includes(name)) {
    const d = open.data.diagnosis;
    if (d.root === name) {
      return { look: "root", state: "root cause", story: `The real fault is here: ${CATEGORY_LABEL[d.category] ?? d.category}.${reason ? ` ${reason}.` : ""}` };
    }
    return { look: "symptom", state: "symptom only", story: `Failing only because it waits on ${d.root}. Restarting it wouldn't help.` };
  }
  if (v.in_incident) return { look: "bad", state: "abnormal", story: `${reason || "Out of its normal range"}. VIGIL is tracing why.` };
  if (v.flagged && isDemand(v)) {
    const x = trafficMultiple(v, latest?.services[name]?.rps);
    return { look: "suspect", state: x ? `busy: ${x} traffic` : "busy: high traffic",
      story: "Busy, not broken: more customers, and each request costs the same as usual." };
  }
  if (v.flagged) return { look: "suspect", state: "looks abnormal", story: `${reason || "Out of its normal range"}. VIGIL is confirming it's real before acting.` };
  if (v.state === "learning") {
    return { look: "learning", state: `learning ${Math.round((v.learning_progress ?? 0) * 100)}%`, story: "VIGIL is learning what normal looks like here." };
  }
  return { look: "ok", state: "healthy", story: (site.demo && DEMO_HEALTHY[name]) || "Healthy: answering as fast as usual." };
}

interface Row { label: string; value: number | null | undefined; fmt: (v: number | null) => string; normal?: string; hot?: boolean; tip?: string }

const msOr = (v: number | null) => ms(v);
const pctOr = (v: number | null) => pct(v);
const cpuOr = (v: number | null) => (v != null ? `${Math.round(v)}%` : "–");
const perSec = (v: number | null) => (v != null ? `${v.toFixed(0)}/s` : "–");

function serviceRows(s: ServiceSample | undefined, v: Verdict | undefined): Row[] {
  const m = v?.baseline?.medians ?? {};
  const z = v?.z ?? {};
  // Highlight a number when VIGIL scores it far outside normal; without a score yet, when it's plainly off.
  const hot = (k: string, now: number | null | undefined, far: (n: number, base: number) => boolean) =>
    z[k] != null ? z[k] >= 3 : now != null && m[k] != null && far(now, m[k]);
  return [
    { label: "p95 latency", value: s?.p95_ms, fmt: msOr, normal: m.p95_ms != null ? ms(m.p95_ms) : undefined,
      hot: hot("p95_ms", s?.p95_ms, (n, b) => n > Math.max(b * 3, b + 100)), tip: "95% of requests finish faster than this" },
    { label: "errors", value: s?.error_rate, fmt: pctOr, normal: m.error_rate != null ? pct(m.error_rate) : undefined,
      hot: hot("error_rate", s?.error_rate, (n, b) => n > b + 0.05) },
    { label: "CPU", value: s?.cpu_pct, fmt: cpuOr, normal: m.cpu_pct != null ? `${Math.round(m.cpu_pct)}%` : undefined,
      hot: hot("cpu_pct", s?.cpu_pct, (n, b) => n > Math.max(b * 2, b + 25)) },
  ];
}

const BADGE: Partial<Record<Look, string>> = { root: "Root cause", symptom: "Symptom", acting: "VIGIL acting" };

/** "4 min ago": coarse on purpose, it is read at a glance and redrawn every 5 s. */
function ago(ts: number): string {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

/** The engine words drift as "latency p95 keeps rising: accepted normal 24 ms, recent
 *  healthy traffic 39 ms. Held at 39 ms until someone accepts the change" (detector.py).
 *  On a card that reads better as "latency p95 · 24 ms → 39 ms". Anything else is shown as is. */
function driftLine(text: string): { what: string; from?: string; to?: string; held?: string } {
  const m = text.match(/^(.+?) keeps rising: accepted normal (.+?), recent healthy traffic (.+?)\. Held at (.+?) until/);
  return m ? { what: m[1], from: m[2], to: m[3], held: m[4] } : { what: text };
}

/** What VIGIL currently believes is normal for this service, and whether a human needs
 *  to look at it. Only engines with the adaptive baseline report this. */
function NormalNote({ name, v }: { name: string; v: Verdict | undefined }) {
  const run = useOperator();
  const [state, setState] = useState<"idle" | "busy" | "done" | string>("idle");
  const bl = v?.baseline;
  if (!bl || bl.adapting === undefined || REPLAY) return null;
  const drift = Object.values(bl.drift ?? {});
  if (drift.length) {
    const accept = () => run(async () => {
      setState("busy");
      try {
        await api.post(`/api/baseline/accept?service=${encodeURIComponent(name)}`, undefined, true);
        setState("done");
      } catch (e) {
        setState(e instanceof Error ? e.message : "Could not accept");
      }
    });
    return (
      <div className="node-drift" role="status">
        <p className="node-drift-head"><TrendingUp size={15} aria-hidden="true" /> Normal keeps rising</p>
        {(() => {
          const d = driftLine(drift[0]);
          return d.from ? (
            <p className="node-drift-text" title={drift[0]}>
              <span className="node-drift-what">{d.what}</span>
              <span className="node-drift-move">{d.from} <span aria-label="to">→</span> <b>{d.to}</b></span>
              <span className="node-drift-held">Held at {d.held} until you accept it</span>
            </p>
          ) : <p className="node-drift-text">{drift[0]}.</p>;
        })()}
        {state === "done" ? (
          <p className="node-drift-ok"><Check size={14} aria-hidden="true" /> Accepted. VIGIL follows from here.</p>
        ) : (
          <button type="button" className="btn btn-small node-drift-btn" onClick={accept} disabled={state === "busy"}>
            {state === "busy" ? "Accepting…" : "Accept new normal"}
          </button>
        )}
        {state !== "idle" && state !== "busy" && state !== "done" && <p className="node-drift-err">{state}</p>}
      </div>
    );
  }
  const when = bl.updated_at ? `updated ${ago(bl.updated_at)}` : `learned ${ago(bl.created_at)}`;
  return (
    <p className="node-learned" title={bl.adapting
      ? "VIGIL keeps learning normal from healthy traffic, never from an incident, and asks before following a slow rise."
      : "Adapting is off (VIGIL_ADAPT=0): normal stays as first learned."}>
      <span className="node-learned-dot" data-on={bl.adapting || undefined} aria-hidden="true" />
      normal {when}
    </p>
  );
}

function ServiceCard({ name, role, card, rows, version, instances, extra, footer }: {
  name: string; role?: string; card: Card; rows: Row[]; version?: string | null; instances?: number | null;
  extra?: ReactNode; footer?: ReactNode;
}) {
  const many = (instances ?? 1) > 1;
  const mixed = !!version && version.includes(",");
  const showNormal = rows.some((r) => r.normal);
  return (
    <article className="node" data-node={name} data-look={card.look} data-many={many || undefined} aria-label={`${name}: ${card.state}. ${card.story}`}>
      {card.look === "root" && <span className="lockon" aria-hidden="true"><i /><i /><i /></span>}
      {BADGE[card.look] && <span className="node-badge" key={card.look}>{BADGE[card.look]}</span>}
      <header className="node-head">
        <span>
          <span className="node-name">{name}</span>
          {role && <span className="node-role">{role}</span>}
        </span>
        {version && (
          <span className="node-ver" data-mixed={mixed || undefined}
            title={mixed ? "Two versions running side by side" : many ? `${instances} copies running` : "Running version"}>
            {version.replace(",", "+")}{many ? ` ×${instances}` : ""}
          </span>
        )}
      </header>
      <p className="node-state"><span className="node-dot" aria-hidden="true" /><span className="node-state-text" key={card.state}>{card.state}</span></p>
      {extra}
      <table className="node-table">
        {showNormal && (
          <thead><tr><th scope="col"><span className="sr-only">signal</span></th><th scope="col">now</th><th scope="col">normal</th></tr></thead>
        )}
        <tbody>
          {rows.map((r) => (
            <tr key={r.label} data-hot={r.hot || undefined}>
              <th scope="row">{r.tip ? <abbr title={r.tip}>{r.label}</abbr> : r.label}</th>
              <td><Tween value={r.value} format={r.fmt} /></td>
              {showNormal && <td className="node-normal">{r.normal ?? "–"}</td>}
            </tr>
          ))}
        </tbody>
      </table>
      {footer}
      <p className="node-story">{card.story}</p>
    </article>
  );
}

interface Link { from: string; to: string; stats?: Stats; rps: number | null; unit: string; walking: boolean }

function WireLabel({ link, phone }: { link: Link; phone?: PhonePayment | null }) {
  const bad = edgeBad(link.stats);
  return (
    <>
      {bad ? <><b>{pct(link.stats?.fail_ratio ?? 0)} failing</b><span>p95 {ms(link.stats?.p95_ms)}</span></>
        : <span>{link.rps != null ? <><Tween value={link.rps} format={(v) => (v != null ? v.toFixed(0) : "")} /> {link.unit}</> : ""}</span>}
      {phone && <em className="wire-phone" data-ok={phone.ok}><Smartphone size={12} aria-hidden="true" /> +1 from a phone</em>}
    </>
  );
}

/** A call between two cards in a straight chain: label above the line. */
function Wire({ link, phone }: { link: Link; phone?: PhonePayment | null }) {
  const bad = edgeBad(link.stats);
  return (
    <div className="wire" data-bad={bad || undefined}>
      <span className="wire-label"><WireLabel link={link} phone={phone} /></span>
      <span className="wire-line" aria-hidden="true">{link.walking && <span className="walk-dot" />}</span>
    </div>
  );
}

const reducedMotion = () => typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

interface Drawn { link: Link; d: string; mx: number; my: number }

/** Calls drawn as curves between the actual card positions; re-measured whenever
 *  the layout changes size (window, wrapping text, a card growing a drift note). */
function Edges({ host, links, phone, entry }: { host: HTMLDivElement | null; links: Link[]; phone?: PhonePayment | null; entry: string }) {
  const [drawn, setDrawn] = useState<Drawn[]>([]);
  const measure = useCallback(() => {
    if (!host) return;
    const box = host.getBoundingClientRect();
    const out: Drawn[] = [];
    for (const link of links) {
      const a = host.querySelector<HTMLElement>(`[data-node="${CSS.escape(link.from)}"]`);
      const b = host.querySelector<HTMLElement>(`[data-node="${CSS.escape(link.to)}"]`);
      if (!a || !b) continue;
      const ra = a.getBoundingClientRect();
      const rb = b.getBoundingClientRect();
      if (rb.left <= ra.right) continue;           // only forward calls are drawn
      const x1 = ra.right - box.left, y1 = ra.top + ra.height / 2 - box.top;
      const x2 = rb.left - box.left - 6, y2 = rb.top + rb.height / 2 - box.top;
      const dx = (x2 - x1) * 0.55;
      out.push({ link, d: `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`, mx: (x1 + x2) / 2, my: (y1 + y2) / 2 });
    }
    setDrawn(out);
  }, [host, links]);

  useLayoutEffect(() => {
    measure();
    if (!host) return;
    const ro = new ResizeObserver(measure);
    ro.observe(host);
    host.querySelectorAll("[data-node]").forEach((n) => ro.observe(n));
    return () => ro.disconnect();
  }, [host, measure]);

  const still = reducedMotion();
  return (
    <>
      <svg className="map-edges" aria-hidden="true">
        <defs>
          <marker id="vg-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">
            <path d="M0,0.5 L7.5,4 L0,7.5" fill="none" stroke="#7d726b" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
          </marker>
          <marker id="vg-arrow-bad" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">
            <path d="M0,0.5 L7.5,4 L0,7.5" fill="none" stroke="#e0574c" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
          </marker>
        </defs>
        {drawn.map(({ link, d, mx, my }) => {
          const bad = edgeBad(link.stats);
          return (
            <g key={`${link.from}>${link.to}`}>
              <path className="map-edge" data-bad={bad || undefined} d={d} markerEnd={`url(#${bad ? "vg-arrow-bad" : "vg-arrow"})`} />
              {link.walking && (
                <circle className="map-walk" r="5.5" cx={still ? mx : 0} cy={still ? my : 0}>
                  {!still && <animateMotion dur="1.6s" repeatCount="indefinite" path={d} keyPoints="0;1" keyTimes="0;1" calcMode="spline" keySplines="0.4 0 0.2 1" />}
                </circle>
              )}
            </g>
          );
        })}
      </svg>
      {drawn.map(({ link, mx, my }) => (
        <span key={`l-${link.from}>${link.to}`} className="edge-label" data-bad={edgeBad(link.stats) || undefined} style={{ left: mx, top: my }}>
          <WireLabel link={link} phone={link.from === entry ? phone : null} />
        </span>
      ))}
    </>
  );
}

export function ServiceMap() {
  const { latest, verdicts, focus } = useVigil();
  const site = useSite();
  const s = latest?.services ?? {};
  const user = latest?.user;
  const open = focus && !TERMINAL.includes(focus.status) ? focus : null;
  const walk = open?.data.diagnosis?.walk.path ?? [];
  const walkPairs = new Set(walk.slice(1).map((to, i) => `${walk[i]}>${to}`));
  const cols = columns(site);
  const chain = cols.every((c) => c.length === 1);
  const [host, setHost] = useState<HTMLDivElement | null>(null);
  // A payment someone just made from a phone (the last 6 s): shown on the entry,
  // because one purchase can't move a 20-second average, but people should see it land.
  const { status: phones } = usePhones();
  const latestPhone = phones?.payments[0];
  const phone = site.demo && latestPhone && phones && phones.now - latestPhone.ts < 6 ? latestPhone : null;

  const entryCalls = site.edges.filter(([a]) => a === site.entry).length;
  const linkFor = (from: string, to: string): Link => {
    const walking = walkPairs.has(`${from}>${to}`);
    const e: EdgeSample | undefined = latest?.edges.find((x) => x.caller === from && x.target === to);
    if (from === site.entry && !e) {
      // The entry has no call metrics: what customers feel IS the call. With several
      // calls from the entry the customer signal can't be split, so it labels none.
      return entryCalls === 1
        ? { from, to, walking, stats: user ? { fail_ratio: user.fail_ratio, p95_ms: user.p95_ms } : undefined,
            rps: user?.checkout_rps ?? null, unit: `${site.units}/s` }
        : { from, to, walking, rps: null, unit: "" };
    }
    return { from, to, walking, stats: e, rps: e?.rps ?? null, unit: "calls/s" };
  };

  const entry = (
    <ServiceCard key={site.entry} name={site.entry} role={site.about[site.entry] ?? "Where customers arrive"}
      card={entryCard(site, focus, latest, verdicts)}
      rows={[
        { label: site.units, value: user?.checkout_rps, fmt: perSec },
        { label: "failing", value: user?.fail_ratio, fmt: pctOr, hot: (user?.fail_ratio ?? 0) >= 0.02 },
        { label: `p95 ${site.unit}`, value: user?.p95_ms, fmt: msOr, tip: `95% of ${site.units} finish faster than this` },
      ]}
      extra={phone && (
        <p className="node-phone" data-ok={phone.ok} key={phone.id}>
          <Smartphone size={14} aria-hidden="true" />
          {phone.ok ? `Phone paid · ${rupees(phone.amount)}` : "Phone payment failed"}
        </p>
      )} />
  );
  const service = (name: string) => (
    <ServiceCard key={name} name={name} role={site.about[name]} card={cardFor(site, name, verdicts[name], focus, latest)}
      rows={serviceRows(s[name], verdicts[name])} version={s[name]?.version} instances={s[name]?.instances}
      footer={<NormalNote name={name} v={verdicts[name]} />} />
  );
  const cardOf = (name: string) => (name === site.entry ? entry : service(name));

  const order = cols.flat();
  const caption = chain
    ? `A ${site.unit} flows left to right: ${order.slice(0, -1).map((n, i) => `${n} calls ${order[i + 1]}`).join(", ")}.`
    : `A ${site.unit} flows left to right, from ${site.entry} through every service it calls.`;
  const gutters = `minmax(76px, 132px)`;
  const template = cols.map(() => "minmax(200px, 1fr)").join(` ${gutters} `);

  let body: ReactNode;
  if (chain) {
    const parts: ReactNode[] = [];
    order.forEach((name, i) => {
      if (i) parts.push(<Wire key={`w-${name}`} link={linkFor(order[i - 1], name)} phone={i === 1 ? phone : null} />);
      parts.push(cardOf(name));
    });
    body = <div className="map-flow" style={{ gridTemplateColumns: template, minWidth: Math.max(720, cols.length * 250) } as CSSProperties}>{parts}</div>;
  } else {
    const placed = new Set(order);
    const links = site.edges.filter(([a, b]) => placed.has(a) && placed.has(b)).map(([a, b]) => linkFor(a, b));
    const graphCols = cols.flatMap((c, i) => [
      ...(i ? [<div key={`g-${i}`} className="map-gutter" aria-hidden="true" />] : []),
      <div key={`c-${i}`} className="map-col">{c.map(cardOf)}</div>,
    ]);
    body = (
      <div className="map-graph" ref={setHost} style={{ gridTemplateColumns: template, minWidth: Math.max(760, cols.length * 270) } as CSSProperties}>
        {graphCols}
        <Edges host={host} links={links} phone={phone} entry={site.entry} />
      </div>
    );
  }

  return (
    <section className="panel map area-map" aria-labelledby="map-title">
      <div className="panel-head">
        <h2 id="map-title">Service map</h2>
        <span className="panel-sub">{walk.length > 1 ? `Blame walk: ${walk.join(" → ")}` : (REPLAY ? "Recorded from Prometheus every 5 s" : "Live from Prometheus every 5 s")}</span>
      </div>
      <p className="panel-caption">
        {caption} A <b>red dashed line</b> is a call that's failing. VIGIL marks which service is the real cause and which are only symptoms.
      </p>
      <p className="map-scroll-hint">Swipe sideways to see the whole map</p>
      <div className="map-scroll">{body}</div>
      <div className="map-legend">
        <span><svg width="26" height="8" aria-hidden="true"><line x1="0" y1="4" x2="26" y2="4" stroke="#6a605a" strokeWidth="2" /></svg>call</span>
        <span><svg width="26" height="8" aria-hidden="true"><line x1="0" y1="4" x2="26" y2="4" stroke="#e0574c" strokeWidth="2.5" strokeDasharray="6 3" /></svg>failing call</span>
        <span><svg width="12" height="12" aria-hidden="true"><circle cx="6" cy="6" r="5" fill="#4fd1c5" /></svg>VIGIL tracing the cause</span>
        <span><b className="legend-key">normal</b> what VIGIL learned is usual for that service</span>
      </div>
    </section>
  );
}
