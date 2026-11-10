import { ArrowLeft, Download } from "lucide-react";
import { Fragment, useEffect, useState } from "react";
import { api } from "../api/client";
import type { Incident, VigilEvent } from "../api/types";
import { DecisionPanel } from "../components/DecisionPanel";
import { LoopStepper } from "../components/LoopStepper";
import { Narration } from "../components/Narration";
import { cap, CATEGORY_LABEL, clock, duration, num, pct } from "../lib/format";
import { Link } from "../lib/router";
import { useVigil } from "../state/vigil";
import { RowsSkeleton } from "../components/Skeletons";
import { statusChip } from "./Incidents";

function postmortem(i: Incident, events: VigilEvent[]): string {
  const d = i.data.diagnosis;
  const dec = i.data.decision;
  const onset = Math.min(...Object.values(i.data.onset ?? {}), i.opened_at);
  const lines = [
    `# ${i.id}: ${i.title}`,
    "",
    `- Status: ${i.status}`,
    `- Severity: ${i.severity}`,
    `- Services: ${i.services.join(", ")}`,
    `- First symptom: ${new Date(onset * 1000).toISOString()}`,
    `- Time to detect: ${i.data.metrics ? duration(i.data.metrics.mttd_s) : duration(i.opened_at - onset)}`,
    `- First symptom to recovery: ${i.data.metrics ? duration(i.data.metrics.mttr_s) : i.closed_at ? duration(i.closed_at - onset) : "ongoing"}`,
    `- Checkouts lost: ${num(i.data.metrics?.failed_checkouts ?? i.data.failed_checkouts ?? 0)} (peak ${pct(i.data.peak_fail_ratio ?? 0)} failing)`,
    "",
    "## Root cause",
    d ? `${d.root}: ${CATEGORY_LABEL[d.category] ?? d.category}. ${d.category_why}.` : "Not diagnosed.",
    d ? `Confidence ${d.confidence}: ${d.confidence_reasons.join("; ")}.` : "",
    d ? `Blame walk: ${d.walk.path.join(" -> ")}. ${d.walk.steps.join(". ")}.` : "",
    d?.llm.hypothesis ? `LLM (independent): ${d.llm.hypothesis}` : "",
    ...(d ? Object.entries(d.symptoms).map(([s, why]) => `- ${s} was a symptom: ${why}`) : []),
    "",
    "## Decision",
    dec?.options?.length ? dec.options.map((o) => `- ${o.label}: score ${num(o.score)} (saves ~${num(o.saves)}, costs ${num(o.drops)})`).join("\n") : "No decision recorded.",
    dec?.why ? `Why: ${dec.why}.` : "",
    "",
    "## Timeline",
    ...events.map((e) => `- ${clock(e.ts)} [${e.actor}] ${e.summary}`),
    "",
    "## Follow-ups",
    "- [ ] Find why the root cause got into this state (VIGIL restored service; it did not fix the underlying bug).",
    "- [ ] Add a test or alert that would have caught it earlier.",
  ];
  return lines.filter((l, idx, arr) => !(l === "" && arr[idx - 1] === "")).join("\n");
}

export function IncidentDetail({ id }: { id: string }) {
  const { incidents, health } = useVigil();
  const live = incidents[id];
  const [data, setData] = useState<{ incident: Incident; events: VigilEvent[] } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    api.get<{ incident: Incident; events: VigilEvent[] }>(`/api/incidents/${id}`)
      .then((r) => alive && setData(r))
      .catch((e) => alive && setError(e instanceof Error ? e.message : "Couldn't load the incident"));
    return () => { alive = false; };
  }, [id, live?.status, live?.data.verify?.clean]);

  if (error) return <section className="panel"><p className="error-text">{error}</p><Link href="/incidents" className="btn">Back to incidents</Link></section>;
  if (!data) return <RowsSkeleton title={id} rows={6} />;
  const inc = live ?? data.incident;
  const chip = statusChip(inc);
  const d = inc.data.diagnosis;

  const download = () => {
    const blob = new Blob([postmortem(inc, data.events)], { type: "text/markdown" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${inc.id}-postmortem.md`;
    a.click();
    URL.revokeObjectURL(a.href);
  };

  return (
    <div className="stack">
      <section className="panel">
        <div className="panel-head" style={{ marginBottom: 0, alignItems: "center", flexWrap: "wrap" }}>
          <div>
            <Link href="/incidents" className="panel-sub" style={{ display: "inline-flex", gap: 6, alignItems: "center", textDecoration: "none" }}>
              <ArrowLeft size={14} /> All incidents
            </Link>
            <h1 className="page-title">{inc.id}: {inc.title}</h1>
          </div>
          <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
            <span className={`chip ${chip.cls}`}>{chip.text}</span>
            <button type="button" className="btn" onClick={download}><Download size={16} /> Download postmortem</button>
          </div>
        </div>
      </section>
      <div className="detail-grid">
        <div className="stack detail-loop">
          <LoopStepper incident={inc} events={data.events} verdicts={{}} compact />
        </div>
        <div className="stack">
          {d && (
            <section className="panel" aria-labelledby="diag-title">
              <div className="panel-head"><h2 id="diag-title">Diagnosis</h2><span className="panel-sub">confidence {d.confidence}</span></div>
              <dl className="kv">
                <dt>Root cause</dt><dd>{d.root}: {CATEGORY_LABEL[d.category] ?? d.category}. {cap(d.category_why)}.</dd>
                <dt>Blame walk</dt><dd>{d.walk.path.join(" → ")}. {d.walk.steps.map(cap).join(". ")}.</dd>
                <dt>LLM, independently</dt><dd>{d.llm.status === "ok" ? `${d.llm.root_cause_service} (${d.llm.category}): ${d.llm.hypothesis}` : `not available (${d.llm.error ?? d.llm.status})`}</dd>
                {Object.entries(d.symptoms).map(([s, why]) => (<Fragment key={s}><dt>{s}</dt><dd>Symptom: {why}</dd></Fragment>))}
                <dt>Why this confidence</dt><dd>{cap(d.confidence_reasons.join("; "))}</dd>
                <dt>Evidence</dt><dd>{d.evidence.length} items with IDs the LLM had to cite</dd>
              </dl>
            </section>
          )}
          <DecisionPanel incident={inc} control={health?.control} />
          <Narration events={data.events} title="Timeline" limit={200} />
        </div>
      </div>
    </div>
  );
}
