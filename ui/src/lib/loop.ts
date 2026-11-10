import type { Incident, Verdict, VigilEvent } from "../api/types";
import { CATEGORY_LABEL, duration, num, pct } from "./format";

// Turns an incident plus its events into the five steps of the loop. Every word
// on the stepper comes from something VIGIL actually recorded.

export type StepStatus = "pending" | "active" | "done" | "waiting" | "failed" | "skipped";
export interface Step {
  key: "detect" | "diagnose" | "decide" | "act" | "verify";
  label: string;
  status: StepStatus;
  detail: string;
  at: string | null; // "T+21s" from the first symptom
}

const IDLE: Record<Step["key"], string> = {
  detect: "Watching every service every 5 seconds",
  diagnose: "Blame walk plus an independent LLM check",
  decide: "Price every fix in failed checkouts",
  act: "Locked actuator: restart, start or stop",
  verify: "Services and checkouts normal 4 checks in a row",
};
const LABEL: Record<Step["key"], string> = {
  detect: "Detect", diagnose: "Diagnose", decide: "Decide", act: "Act", verify: "Verify",
};
const KEYS: Step["key"][] = ["detect", "diagnose", "decide", "act", "verify"];

export function idleSteps(verdicts: Record<string, Verdict>): Step[] {
  const suspect = Object.values(verdicts).find((v) => v.flagged && !v.in_incident);
  return KEYS.map((key) => {
    if (key === "detect" && suspect) {
      return { key, label: LABEL[key], status: "active", at: null,
        detail: `${suspect.service}: ${suspect.reasons?.[0] ?? "abnormal"}. Confirming` };
    }
    return { key, label: LABEL[key], status: "pending", detail: IDLE[key], at: null };
  });
}

export function deriveLoop(inc: Incident, events: VigilEvent[]): Step[] {
  const d = inc.data;
  const onset = Math.min(...Object.values(d.onset ?? {}), inc.opened_at);
  const evs = events.filter((e) => e.incident_id === inc.id);
  const last = (...kinds: string[]) => [...evs].reverse().find((e) => kinds.includes(e.kind));
  const at = (ts?: number) => (ts ? `T+${duration(ts - onset)}` : null);
  const s = inc.status;
  const steps: Record<Step["key"], Step> = Object.fromEntries(
    KEYS.map((k) => [k, { key: k, label: LABEL[k], status: "pending" as StepStatus, detail: IDLE[k], at: null }]),
  ) as Record<Step["key"], Step>;

  // Detect
  steps.detect.status = "done";
  steps.detect.at = at(inc.opened_at);
  steps.detect.detail = `${inc.services.join(" and ")} abnormal; ${pct(d.peak_fail_ratio ?? 0)} of checkouts failing at peak`;

  // Diagnose
  if (d.diagnosis) {
    steps.diagnose.status = "done";
    steps.diagnose.at = at(d.diagnosis.at);
    steps.diagnose.detail = `Root cause: ${d.diagnosis.root} (${CATEGORY_LABEL[d.diagnosis.category] ?? d.diagnosis.category}), confidence ${d.diagnosis.confidence}`;
  } else if (s === "INVESTIGATING" || s === "OPEN") {
    steps.diagnose.status = "active";
    steps.diagnose.detail = "Following failing calls; asking the LLM without the answer";
  }

  // Decide
  const dec = d.decision;
  const decidedEv = last("approved", "acting", "retrying");
  if (s === "AWAITING_APPROVAL" && dec?.action) {
    steps.decide.status = "waiting";
    const chosen = dec.options.find((o) => o.action === dec.action && o.target === dec.target);
    steps.decide.detail = `Proposes ${chosen?.label ?? `${dec.action} ${dec.target}`} (saves ~${num(chosen?.saves ?? 0)} checkouts). Confidence ${d.diagnosis?.confidence ?? "not high"}, so a human approves`;
  } else if (dec?.status === "blocked") {
    steps.decide.status = "waiting";
    steps.decide.detail = `Holding: ${dec.reason}`;
  } else if (dec?.status === "chosen") {
    const chosen = dec.options.find((o) => o.action === dec.action && o.target === dec.target);
    steps.decide.status = "done";
    steps.decide.at = at(decidedEv?.ts ?? last("decided")?.ts);
    const how = last("approved") ? "approved by a human" : dec.acted_alone ? `acting alone at ${dec.autonomy}` : "";
    steps.decide.detail = `${chosen?.label ?? `${dec.action} ${dec.target}`}: saves ~${num(chosen?.saves ?? 0)} checkouts${how ? `, ${how}` : ""}`;
  } else if (s === "DIAGNOSED") {
    steps.decide.status = "active";
    steps.decide.detail = "Pricing every option in failed checkouts";
  }

  // Act
  const done = last("action_done");
  const failed = last("action_failed");
  if (s === "REMEDIATING") {
    steps.act.status = "active";
    const verb = ({ restart: "Restarting", rollback: "Rolling back", scale_out: "Adding a replica of" } as Record<string, string>)[dec?.action ?? ""] ?? "Fixing";
    steps.act.detail = `${verb} ${dec?.target ?? "the service"} through the locked actuator`;
  } else if (failed && (!done || failed.id > done.id)) {
    steps.act.status = "failed";
    steps.act.at = at(failed.ts);
    steps.act.detail = failed.summary;
  } else if (done) {
    steps.act.status = "done";
    steps.act.at = at(done.ts);
    steps.act.detail = done.summary;
  }

  // Verify and terminal states
  if (s === "VERIFYING") {
    steps.verify.status = "active";
    steps.verify.detail = `Services and checkouts: ${d.verify?.clean ?? 0} of 4 clean checks`;
  } else if (s === "RESOLVED") {
    steps.verify.status = "done";
    steps.verify.at = at(inc.closed_at ?? undefined);
    steps.verify.detail = `Recovered and verified; ${num(d.metrics?.failed_checkouts ?? d.failed_checkouts ?? 0)} checkouts failed in total`;
  } else if (s === "SELF_RESOLVED") {
    for (const k of ["decide", "act"] as const) {
      if (steps[k].status === "pending") {
        steps[k].status = "skipped";
        steps[k].detail = "Not needed";
      }
    }
    steps.verify.status = "done";
    steps.verify.at = at(inc.closed_at ?? undefined);
    steps.verify.detail = "Recovered before any action";
  } else if (s === "ESCALATED") {
    const where: Step["key"] = done ? "verify" : failed ? "act" : "decide";
    const esc = last("escalated", "rejected");
    steps[where].status = "failed";
    steps[where].at = at(esc?.ts);
    steps[where].detail = esc?.kind === "rejected" ? "Rejected: a human took over" : "Escalated to a human";
    if (where === "decide" && !failed) steps.act.detail = "Not attempted";
  }
  return KEYS.map((k) => steps[k]);
}
