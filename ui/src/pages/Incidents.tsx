import type { Incident } from "../api/types";
import { CATEGORY_LABEL, clock, duration, num } from "../lib/format";
import { Link } from "../lib/router";
import { LoadError, RowsSkeleton } from "../components/Skeletons";
import { useVigil } from "../state/vigil";

export function statusChip(i: Incident): { cls: string; text: string } {
  switch (i.status) {
    case "RESOLVED": return { cls: "chip-ok", text: "Resolved" };
    case "SELF_RESOLVED": return { cls: "chip-ok", text: "Recovered on its own" };
    case "ESCALATED": return { cls: "chip-bad", text: "Escalated" };
    case "AWAITING_APPROVAL": return { cls: "chip-warn", text: "Waiting for approval" };
    default: return { cls: "chip-active", text: i.status.charAt(0) + i.status.slice(1).toLowerCase().replace("_", " ") };
  }
}

export function Incidents() {
  const { incidents, loaded, error, refresh } = useVigil();
  if (!loaded) return error ? <LoadError message={error} onRetry={refresh} /> : <RowsSkeleton title="Incidents" />;
  const list = Object.values(incidents).sort((a, b) => b.seq - a.seq);
  return (
    <section className="panel" aria-labelledby="inc-title">
      <div className="panel-head">
        <h1 id="inc-title" className="page-title">Incidents</h1>
        <span className="panel-sub">every incident VIGIL has handled, newest first</span>
      </div>
      {list.length === 0 ? (
        <p className="empty">No incidents yet. Open Chaos Lab and break something to see VIGIL work.</p>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr><th>Incident</th><th>Status</th><th>Root cause</th><th>Started</th><th>Symptom to recovery</th><th>Checkouts lost</th></tr>
            </thead>
            <tbody>
              {list.map((i) => {
                const chip = statusChip(i);
                const onset = Math.min(...Object.values(i.data.onset ?? {}), i.opened_at);
                const took = i.data.metrics?.mttr_s ?? (i.closed_at ? i.closed_at - onset : null);
                return (
                  <tr key={i.id}>
                    <td><Link href={`/incidents/${i.id}`}>{i.id}</Link><div className="panel-sub">{i.title}</div></td>
                    <td><span className={`chip ${chip.cls}`}>{chip.text}</span></td>
                    <td>{i.root ? `${i.root} (${CATEGORY_LABEL[i.category ?? "unknown"] ?? i.category})` : "–"}</td>
                    <td className="num">{clock(i.opened_at)}</td>
                    <td className="num">{took != null ? duration(took) : "ongoing"}</td>
                    <td className="num">{num(i.data.metrics?.failed_checkouts ?? i.data.failed_checkouts ?? 0)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
