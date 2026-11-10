import { ArrowDown, Check, Clock, Eye, LoaderCircle, Scale, Search, ShieldCheck, X, Zap } from "lucide-react";
import type { Incident, Verdict, VigilEvent } from "../api/types";
import { TERMINAL } from "../api/types";
import { duration, num, pct } from "../lib/format";
import { deriveLoop, idleSteps, type Step } from "../lib/loop";

const ICON = { detect: Eye, diagnose: Search, decide: Scale, act: Zap, verify: ShieldCheck };

function Mark({ status }: { status: Step["status"] }) {
  if (status === "done") return <Check size={20} color="#fff" strokeWidth={2.4} aria-label="done" />;
  if (status === "active") return <LoaderCircle size={20} color="var(--vigil)" className="spin" aria-label="in progress" />;
  if (status === "waiting") return <Clock size={20} color="var(--warn)" className="breathe" aria-label="waiting for a human" />;
  if (status === "failed") return <X size={20} color="#fff" strokeWidth={2.4} aria-label="failed" />;
  return null;
}

function chipFor(inc: Incident | null, steps: Step[]): { cls: string; text: string } {
  if (!inc) return steps[0].status === "active" ? { cls: "chip-warn", text: "Confirming" } : { cls: "chip-idle", text: "Watching" };
  if (inc.status === "RESOLVED") return { cls: "chip-ok", text: `Resolved in ${duration(inc.data.metrics?.mttr_s ?? 0)}` };
  if (inc.status === "SELF_RESOLVED") return { cls: "chip-ok", text: "Recovered on its own" };
  if (inc.status === "ESCALATED") return { cls: "chip-bad", text: "Escalated to a human" };
  if (steps.some((s) => s.status === "waiting")) return { cls: "chip-warn", text: "Waiting for you" };
  return { cls: "chip-active", text: "VIGIL working" };
}

export function LoopStepper({ incident, events, verdicts, compact = false }: {
  incident: Incident | null; events: VigilEvent[]; verdicts: Record<string, Verdict>; compact?: boolean;
}) {
  const steps = incident ? deriveLoop(incident, events) : idleSteps(verdicts);
  const chip = chipFor(incident, steps);
  const open = incident && !TERMINAL.includes(incident.status);
  const onset = incident ? Math.min(...Object.values(incident.data.onset ?? {}), incident.opened_at) : 0;

  return (
    <section className={`panel ${compact ? "" : "area-loop"}`} aria-labelledby="loop-title">
      <div className="panel-head" style={{ marginBottom: 6 }}>
        <span className="panel-sub">The loop</span>
        <span className={`chip ${chip.cls}`} aria-live="polite">{chip.text}</span>
      </div>
      <h2 id="loop-title" className="loop-title">{incident ? `${incident.id}: ${incident.title}` : "No open incident"}</h2>
      {!incident && (
        <p className="panel-caption loop-caption">When something breaks, VIGIL works down these five steps by itself. Each one turns green the moment it's done.</p>
      )}
      <ol className="steps">
        {steps.map((s, i) => {
          const Icon = ICON[s.key];
          return (
            <li key={s.key}>
              <div className="step" data-status={s.status}>
                <span className="step-icon"><Icon size={21} strokeWidth={1.75} aria-hidden="true" /></span>
                <span className="step-body">
                  <span className="step-top">
                    <span className="step-label">{s.label}</span>
                    {s.at && <span className="step-at">{s.at}</span>}
                  </span>
                  <span className="step-detail">{s.detail}</span>
                </span>
                <span className="step-mark"><Mark status={s.status} /></span>
              </div>
              {i < steps.length - 1 && (
                <div className={`step-arrow ${s.status === "done" ? "lit" : ""}`} aria-hidden="true"><ArrowDown size={16} /></div>
              )}
            </li>
          );
        })}
      </ol>
      {incident && (
        <div className="loop-foot">
          {open ? (
            <>
              <span>Running for<b>{duration(Date.now() / 1000 - onset)}</b></span>
              <span>Checkouts lost so far<b>{num(Math.round(incident.data.failed_checkouts ?? 0))}</b></span>
              <span>Worst moment<b>{pct(incident.data.peak_fail_ratio ?? 0)} failing</b></span>
            </>
          ) : incident.status === "ESCALATED" ? (
            <>
              <span>Handed to a human after<b>{duration((incident.closed_at ?? onset) - onset)}</b></span>
              <span>Checkouts lost until then<b>{num(Math.round(incident.data.failed_checkouts ?? 0))}</b></span>
            </>
          ) : (
            <>
              <span>First symptom to recovery<b>{duration(incident.data.metrics?.mttr_s ?? ((incident.closed_at ?? onset) - onset))}</b></span>
              <span>Checkouts lost<b>{num(incident.data.metrics?.failed_checkouts ?? incident.data.failed_checkouts ?? 0)}</b></span>
            </>
          )}
        </div>
      )}
    </section>
  );
}
