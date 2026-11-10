import { CheckCircle2, CircleX } from "lucide-react";
import { useEffect, useState } from "react";
import { RowsSkeleton } from "../components/Skeletons";
import { CATEGORY_LABEL } from "../lib/format";

// The latest Proving Ground run, written by the proving-ground container to
// data/proving-ground/scorecard.json and served read-only by nginx. Nothing here
// is computed by the UI: it shows exactly what the harness measured.

interface Row {
  scenario: string;
  label: string;
  expected?: string;
  incident?: string | null;
  detected?: boolean;
  mttd_s?: number | null;
  root?: string | null;
  category?: string | null;
  confidence?: string | null;
  actions?: string[];
  status?: string | null;
  mttr_s?: number | null;
  checkouts_lost?: number | null;
  false_alarms?: number;
  pass: boolean;
  error?: string;
}
interface Scorecard {
  summary: { started_at: number; finished_at: number; autonomy_during_run: string; passed: number; total: number;
    median_detect_s: number | null; median_recover_s: number | null; false_alarms: number };
  rows: Row[];
}

const secs = (v: number | null | undefined) => (v == null ? "–" : `${Math.round(v)} s`);

export function ProvingGround() {
  const [card, setCard] = useState<Scorecard | null>(null);
  const [state, setState] = useState<"loading" | "none" | "ok" | "error">("loading");

  useEffect(() => {
    let alive = true;
    fetch("/proving-ground/scorecard.json", { cache: "no-store" })
      .then(async (r) => {
        if (!alive) return;
        if (r.status === 404) return setState("none");
        if (!r.ok) throw new Error(String(r.status));
        setCard(await r.json());
        setState("ok");
      })
      .catch(() => alive && setState("error"));
    return () => { alive = false; };
  }, []);

  const run = <code className="fault-cmd">docker compose --profile prove run --rm proving-ground</code>;
  if (state === "loading") return <RowsSkeleton title="Proving Ground" rows={4} />;
  if (state !== "ok" || !card) {
    return (
      <section className="panel" aria-labelledby="pg-title">
        <div className="panel-head"><h1 id="pg-title" className="page-title">Proving Ground</h1></div>
        {(
          <div className="pg-empty">
            <p>{state === "none" ? "No run yet. The Proving Ground breaks the system once per fault and scores VIGIL against the answer key. Run it from the VS Code terminal:"
              : "Couldn't load the scorecard. Run the Proving Ground, then refresh:"}</p>
            {run}
          </div>
        )}
      </section>
    );
  }

  const s = card.summary;
  const when = new Date(s.started_at * 1000);
  const took = Math.round((s.finished_at - s.started_at) / 60);
  return (
    <div className="stack">
      <section className="panel pg-hero" aria-labelledby="pg-title">
        <div className="panel-head">
          <h1 id="pg-title" className="page-title">Proving Ground</h1>
          <span className="panel-sub">
            run {when.toLocaleDateString("en-GB", { day: "numeric", month: "short" })}, {when.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })},
            {" "}{took} min at autonomy {s.autonomy_during_run}
          </span>
        </div>
        <p className="pg-lede">Every fault injected for real, one at a time. VIGIL never sees the answer key; times are measured from the moment of injection.</p>
        <div className="pg-stats">
          <div className="pg-stat"><b className={s.passed === s.total ? "pg-good" : "pg-warn"}>{s.passed}/{s.total}</b><span>scenarios passed</span></div>
          <div className="pg-stat"><b>{secs(s.median_detect_s)}</b><span>median time to detect</span></div>
          <div className="pg-stat"><b>{secs(s.median_recover_s)}</b><span>median time to verified recovery</span></div>
          <div className="pg-stat"><b className={s.false_alarms === 0 ? "pg-good" : "pg-warn"}>{s.false_alarms}</b><span>false alarms with nothing broken</span></div>
        </div>
      </section>
      <section className="panel" aria-label="Results per scenario">
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr><th>Scenario</th><th>Right answer</th><th>VIGIL blamed</th><th>VIGIL did</th><th>Detect</th><th>Recovered</th><th>Checkouts lost</th><th>Result</th></tr>
            </thead>
            <tbody>
              {card.rows.map((r) => (
                <tr key={r.scenario}>
                  <td><b>{r.label}</b>{r.incident && <div className="panel-sub">{r.incident}</div>}{r.error && <div className="error-text">{r.error}</div>}</td>
                  <td>{r.expected ?? "–"}</td>
                  <td>{r.root ? `${r.root} (${CATEGORY_LABEL[r.category ?? ""] ?? r.category})` : r.scenario === "quiet" ? `${r.false_alarms ?? 0} incidents` : "–"}
                    {r.confidence && <div className="panel-sub">confidence {r.confidence}</div>}</td>
                  <td>{r.actions?.length ? r.actions.join(", ") : r.scenario === "quiet" ? "–" : "nothing"}</td>
                  <td className="num">{secs(r.mttd_s)}</td>
                  <td className="num">{secs(r.mttr_s)}</td>
                  <td className="num">{r.checkouts_lost ?? "–"}</td>
                  <td>{r.pass
                    ? <span className="chip chip-ok"><CheckCircle2 size={14} /> Pass</span>
                    : <span className="chip chip-bad"><CircleX size={14} /> Fail</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="panel-sub pg-foot">Run it yourself: {run}</p>
      </section>
    </div>
  );
}
