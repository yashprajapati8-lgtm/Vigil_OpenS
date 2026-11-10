import type { EdgeSample, Incident, Point, ServiceSample, Verdict } from "../api/types";
import { TERMINAL } from "../api/types";
import { isDemand, trafficMultiple } from "../lib/demand";
import { CATEGORY_LABEL, ms, pct } from "../lib/format";
import { useVigil } from "../state/vigil";
import { REPLAY } from "../replay/player";
import { Smartphone } from "lucide-react";
import type { ReactNode } from "react";
import { usePhones, rupees, type PhonePayment } from "../lib/phones";

// The hero. Three services and the calls between them, drawn from live data.
// Each card says in plain words what is happening to it, shows every number next
// to what VIGIL learned is normal, and marks where the blame walk stopped.

type Name = "storefront" | "payment" | "inventory";
type Look = "ok" | "learning" | "suspect" | "bad" | "root" | "symptom" | "acting" | "down";

const ROLE: Record<Name, string> = {
  storefront: "Where customers check out",
  payment: "Charges the card",
  inventory: "Reserves the stock",
};
const HEALTHY: Record<Name, string> = {
  storefront: "Checkouts go through normally.",
  payment: "Healthy: cards are charged as fast as usual.",
  inventory: "Healthy: stock checks answer as fast as usual.",
};
const DOING: Record<string, [string, string]> = {
  // action: [while acting, while verifying]
  restart: ["VIGIL is restarting it", "Restarted. Checking customers recover"],
  rollback: ["VIGIL is rolling it back", "Rolled back. Checking customers recover"],
  scale_out: ["VIGIL is adding a replica", "Replica added. Checking customers recover"],
};

function edgeBad(e: { fail_ratio: number | null; p95_ms: number | null } | undefined) {
  return !!e && ((e.fail_ratio ?? 0) >= 0.05 || (e.p95_ms ?? 0) >= 500);
}
const cap = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s);

interface Card { look: Look; state: string; story: string }

function cardFor(name: Name, v: Verdict | undefined, inc: Incident | null, latest: Point | null, verdicts: Record<string, Verdict>): Card {
  const open = inc && !TERMINAL.includes(inc.status) ? inc : null;
  if (name === "storefront") {
    const fail = latest?.user.fail_ratio ?? 0;
    const busy = Object.values(verdicts).find((x) => x.flagged && isDemand(x));
    const x = busy ? trafficMultiple(busy, latest?.services[busy.service]?.rps) : null;
    if (fail >= 0.02) {
      return { look: "bad", state: "customers affected",
        story: `${pct(fail)} of checkouts are failing right now.${open ? " VIGIL is already on it." : ""}` };
    }
    return { look: "ok", state: "customers fine",
      story: x ? `Checkouts go through normally, at ${x} the usual traffic.` : "Checkouts go through normally." };
  }
  const reason = cap(v?.reasons?.[0] ?? "");
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
  return { look: "ok", state: "healthy", story: HEALTHY[name] };
}

interface Row { label: string; now: string; normal?: string; hot?: boolean; tip?: string }

function serviceRows(s: ServiceSample | undefined, v: Verdict | undefined): Row[] {
  const m = v?.baseline?.medians ?? {};
  const z = v?.z ?? {};
  // Highlight a number when VIGIL scores it far outside normal; without a score yet, when it's plainly off.
  const hot = (k: string, now: number | null | undefined, far: (n: number, base: number) => boolean) =>
    z[k] != null ? z[k] >= 3 : now != null && m[k] != null && far(now, m[k]);
  return [
    { label: "p95 latency", now: ms(s?.p95_ms), normal: m.p95_ms != null ? ms(m.p95_ms) : undefined,
      hot: hot("p95_ms", s?.p95_ms, (n, b) => n > Math.max(b * 3, b + 100)), tip: "95% of requests finish faster than this" },
    { label: "errors", now: pct(s?.error_rate), normal: m.error_rate != null ? pct(m.error_rate) : undefined,
      hot: hot("error_rate", s?.error_rate, (n, b) => n > b + 0.05) },
    { label: "CPU", now: s?.cpu_pct != null ? `${Math.round(s.cpu_pct)}%` : "–", normal: m.cpu_pct != null ? `${Math.round(m.cpu_pct)}%` : undefined,
      hot: hot("cpu_pct", s?.cpu_pct, (n, b) => n > Math.max(b * 2, b + 25)) },
  ];
}

const BADGE: Partial<Record<Look, string>> = { root: "Root cause", symptom: "Symptom", acting: "VIGIL acting" };

function ServiceCard({ name, card, rows, version, instances, extra }: {
  name: Name; card: Card; rows: Row[]; version?: string | null; instances?: number | null; extra?: ReactNode;
}) {
  const many = (instances ?? 1) > 1;
  const mixed = !!version && version.includes(",");
  const showNormal = rows.some((r) => r.normal);
  return (
    <article className="node" data-look={card.look} data-many={many || undefined} aria-label={`${name}: ${card.state}. ${card.story}`}>
      {card.look === "root" && <span className="lockon" aria-hidden="true"><i /><i /><i /></span>}
      {BADGE[card.look] && <span className="node-badge" key={card.look}>{BADGE[card.look]}</span>}
      <header className="node-head">
        <span>
          <span className="node-name">{name}</span>
          <span className="node-role">{ROLE[name]}</span>
        </span>
        {version && (
          <span className="node-ver" data-mixed={mixed || undefined}
            title={mixed ? "Two versions running side by side" : many ? `${instances} copies running` : "Running version"}>
            {version.replace(",", "+")}{many ? ` ×${instances}` : ""}
          </span>
        )}
      </header>
      <p className="node-state"><span className="node-dot" aria-hidden="true" />{card.state}</p>
      {extra}
      <table className="node-table">
        {showNormal && (
          <thead><tr><th scope="col"><span className="sr-only">signal</span></th><th scope="col">now</th><th scope="col">normal</th></tr></thead>
        )}
        <tbody>
          {rows.map((r) => (
            <tr key={r.label} data-hot={r.hot || undefined}>
              <th scope="row">{r.tip ? <abbr title={r.tip}>{r.label}</abbr> : r.label}</th>
              <td>{r.now}</td>
              {showNormal && <td className="node-normal">{r.normal ?? "–"}</td>}
            </tr>
          ))}
        </tbody>
      </table>
      <p className="node-story">{card.story}</p>
    </article>
  );
}

function Wire({ stats, rps, unit, walking, phone }: {
  stats?: { fail_ratio: number | null; p95_ms: number | null }; rps: number | null; unit: string; walking?: boolean; phone?: PhonePayment | null;
}) {
  const bad = edgeBad(stats);
  return (
    <div className="wire" data-bad={bad || undefined}>
      <span className="wire-label">
        {bad ? <><b>{pct(stats?.fail_ratio ?? 0)} failing</b><span>p95 {ms(stats?.p95_ms)}</span></>
          : <span>{rps != null ? `${rps.toFixed(0)} ${unit}` : ""}</span>}
        {phone && <em className="wire-phone" data-ok={phone.ok}><Smartphone size={12} aria-hidden="true" /> +1 from a phone</em>}
      </span>
      <span className="wire-line" aria-hidden="true">{walking && <span className="walk-dot" />}</span>
    </div>
  );
}

export function ServiceMap() {
  const { latest, verdicts, focus } = useVigil();
  const s = latest?.services ?? {};
  const pi: EdgeSample | undefined = latest?.edges.find((e) => e.caller === "payment" && e.target === "inventory");
  const user = latest?.user;
  const open = focus && !TERMINAL.includes(focus.status) ? focus : null;
  const walk = open?.data.diagnosis?.walk.path;
  const walking = !!walk && walk.length > 1;
  const card = (n: Name) => cardFor(n, verdicts[n], focus, latest, verdicts);
  // A payment someone just made from a phone (the last 6 s): shown on the storefront,
  // because one checkout can't move a 20-second average, but people should see it land.
  const { status: phones } = usePhones();
  const latestPhone = phones?.payments[0];
  const phone = latestPhone && phones && phones.now - latestPhone.ts < 6 ? latestPhone : null;

  return (
    <section className="panel map area-map" aria-labelledby="map-title">
      <div className="panel-head">
        <h2 id="map-title">Service map</h2>
        <span className="panel-sub">{walking ? `Blame walk: ${walk!.join(" → ")}` : (REPLAY ? "Recorded from Prometheus every 5 s" : "Live from Prometheus every 5 s")}</span>
      </div>
      <p className="panel-caption">
        A checkout flows left to right: storefront calls payment, payment calls inventory. A <b>red dashed line</b> is a call
        that's failing. VIGIL marks which service is the real cause and which are only symptoms.
      </p>
      <p className="map-scroll-hint">Swipe sideways to see the whole map</p>
      <div className="map-scroll">
        <div className="map-flow">
          <ServiceCard name="storefront" card={card("storefront")}
            rows={[
              { label: "checkouts", now: user?.checkout_rps != null ? `${user.checkout_rps.toFixed(0)}/s` : "–" },
              { label: "failing", now: pct(user?.fail_ratio), hot: (user?.fail_ratio ?? 0) >= 0.02 },
              { label: "p95 checkout", now: ms(user?.p95_ms), tip: "95% of checkouts finish faster than this" },
            ]}
            extra={phone && (
              <p className="node-phone" data-ok={phone.ok} key={phone.id}>
                <Smartphone size={14} aria-hidden="true" />
                {phone.ok ? `Phone paid · ${rupees(phone.amount)}` : "Phone payment failed"}
              </p>
            )} />
          <Wire stats={user ? { fail_ratio: user.fail_ratio, p95_ms: user.p95_ms } : undefined} rps={user?.checkout_rps ?? null} unit="checkouts/s" phone={phone} />
          <ServiceCard name="payment" card={card("payment")} rows={serviceRows(s.payment, verdicts.payment)}
            version={s.payment?.version} instances={s.payment?.instances} />
          <Wire stats={pi} rps={pi?.rps ?? null} unit="calls/s" walking={walking} />
          <ServiceCard name="inventory" card={card("inventory")} rows={serviceRows(s.inventory, verdicts.inventory)}
            version={s.inventory?.version} instances={s.inventory?.instances} />
        </div>
      </div>
      <div className="map-legend">
        <span><svg width="26" height="8" aria-hidden="true"><line x1="0" y1="4" x2="26" y2="4" stroke="#6a605a" strokeWidth="2" /></svg>call</span>
        <span><svg width="26" height="8" aria-hidden="true"><line x1="0" y1="4" x2="26" y2="4" stroke="#e0574c" strokeWidth="2.5" strokeDasharray="6 3" /></svg>failing call</span>
        <span><svg width="12" height="12" aria-hidden="true"><circle cx="6" cy="6" r="5" fill="#4fd1c5" /></svg>VIGIL tracing the cause</span>
        <span><b className="legend-key">normal</b> what VIGIL learned is usual for that service</span>
      </div>
    </section>
  );
}
