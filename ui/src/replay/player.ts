import { ApiError } from "../api/errors";
import type { Control, Health, Incident, Point, StateResponse, StreamMessage, VigilEvent } from "../api/types";
import { duration, num } from "../lib/format";

// Replay mode (`vite build --mode replay`): the hosted link plays back real runs
// recorded by the Proving Ground on the team's laptop. Every number on screen comes
// from a recording: the snapshot the UI loaded at that moment, then every live
// message in the order and at the pace it arrived. The clock is moved so the
// recording happens "now".
//
// The runs were recorded at autonomy L3. A visitor can still change autonomy and the
// kill switch: the player stops at VIGIL's decision and applies the same rules as the
// engine (engine/vigil/policy.py). Acting alone plays on as recorded. Needing a human
// waits for Approve or Reject. The kill switch holds. Time spent waiting is added to
// the recovery time, and checkouts lost while waiting are counted at the rate VIGIL
// measured when it decided.

export const REPLAY = import.meta.env.MODE === "replay";
export const REPO_URL: string = import.meta.env.VITE_REPO_URL ?? "";

// Same tables as engine/vigil/policy.py.
const RISK: Record<string, string> = { restart: "low", rollback: "medium", scale_out: "low" };
const ALLOWED_ALONE: Record<Control["autonomy"], string[]> = { L0: [], L1: [], L2: ["low"], L3: ["low", "medium"] };
const AUTONOMY: Record<Control["autonomy"], string> = {
  L0: "observe only: detect and diagnose",
  L1: "advise: propose, a human clicks",
  L2: "supervised: act alone on low-risk fixes, ask for the rest",
  L3: "autonomous: act alone on every allowed fix",
};

export interface Chapter {
  scenario: string;
  label: string;
  file: string;
  result?: { pass?: boolean; mttd_s?: number | null; mttr_s?: number | null; actions?: string[]; root?: string | null };
}
interface Index { recorded_at: number; chapters: Chapter[] }
interface Tape {
  scenario: string;
  label: string;
  inject_ts: number;
  end_ts: number;
  snapshot: { state: StateResponse; history: Point[]; events: VigilEvent[]; incidents: Incident[]; health: Health; taken_at: number };
  messages: Array<{ t: number; m: StreamMessage }>;
}
export interface Snapshot { state: StateResponse; history: Point[]; events: VigilEvent[]; incidents: Incident[] }
export type Phase = "loading" | "idle" | "playing" | "done";
/** Why playback is stopped mid-run: waiting for a human's approval, or held by the kill switch. */
export type Hold = null | "approval" | "blocked";

/** Move every epoch-seconds timestamp by `offset`. Only numbers in the epoch range move. */
function shift<T>(value: T, offset: number): T {
  if (typeof value === "number") return (value > 1.5e9 && value < 2.5e9 ? value + offset : value) as T;
  if (Array.isArray(value)) return value.map((v) => shift(v, offset)) as T;
  if (value && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value)) out[k] = shift(v, offset);
    return out as T;
  }
  return value;
}

type MsgFn = (m: StreamMessage) => void;
type SnapFn = (s: Snapshot) => void;

/** One recording being played, and what the visitor's settings changed about it. */
interface Run {
  tape: Tape;
  offset: number;
  gate: number;          // index of the "decided" event: where autonomy matters
  acting: number;        // index of the recorded "acting" event (skipped when a human approves)
  extra: number;         // seconds the run waited on a human or the kill switch
  lost: number;          // checkouts that failed while it waited
  waitSince: number | null;
  failPerMin: number;    // failure rate VIGIL measured when it decided
  alone: boolean;
  why: string;
  autonomy: string;
  label: string;
  incident: Incident | null;
}

class Player {
  index: Index | null = null;
  current: Chapter | null = null;
  phase: Phase = "loading";
  hold: Hold = null;
  /** Set when the visitor rejected the fix: the run stops there, as it would on the live stack. */
  rejected = false;
  /** The visitor's autonomy and kill switch. Starts as recorded (L3, armed). */
  control: Control | null = null;
  private tapes = new Map<string, Tape>();
  private timers: number[] = [];
  private msgFns = new Set<MsgFn>();
  private snapFns = new Set<SnapFn>();
  private stateFns = new Set<() => void>();
  private snap: Snapshot | null = null;
  private incidents = new Map<string, Incident>();
  private events: VigilEvent[] = [];
  private touched = false;
  private run: Run | null = null;
  private lastId = 0;     // newest recorded event id; messages made here sort just after it
  private seq = 0;
  startedAt = 0;
  injectAt = 0;

  async loadIndex(): Promise<Index> {
    if (this.index) return this.index;
    const r = await fetch("/replay/index.json", { cache: "no-store" });
    if (!r.ok) throw new Error("No recordings found in this build");
    this.index = (await r.json()) as Index;
    return this.index;
  }

  private async tape(ch: Chapter): Promise<Tape> {
    const hit = this.tapes.get(ch.scenario);
    if (hit) return hit;
    const r = await fetch(`/replay/${ch.file}`);
    if (!r.ok) throw new Error(`Recording for ${ch.label} is missing`);
    const t = (await r.json()) as Tape;
    this.tapes.set(ch.scenario, t);
    return t;
  }

  chapter(scenario: string): Chapter | undefined {
    return this.index?.chapters.find((c) => c.scenario === scenario);
  }

  /** True when the visitor has changed autonomy or the kill switch from how the runs were recorded. */
  get changedFromRecording(): boolean {
    return !!this.control && (this.control.autonomy !== "L3" || this.control.paused);
  }

  onMessage(fn: MsgFn): () => void { this.msgFns.add(fn); return () => { this.msgFns.delete(fn); }; }
  onSnapshot(fn: SnapFn): () => void { this.snapFns.add(fn); return () => { this.snapFns.delete(fn); }; }
  onChange(fn: () => void): () => void { this.stateFns.add(fn); return () => { this.stateFns.delete(fn); }; }
  private changed() { for (const fn of this.stateFns) fn(); }

  /** The calm moment at the start of a recording, shifted to now. */
  private async stage(ch: Chapter): Promise<{ tape: Tape; snap: Snapshot; offset: number }> {
    const tape = await this.tape(ch);
    const offset = Date.now() / 1000 - tape.snapshot.taken_at;
    const s = shift(tape.snapshot, offset);
    const since = s.taken_at - 60;
    if (!this.control) this.control = s.state.health.control;
    const snap: Snapshot = {
      // Only what happens in this recording: earlier incidents and events would mix stories.
      state: { ...s.state, health: { ...s.state.health, control: this.control }, open_incidents: [] },
      history: s.history,
      events: s.events.filter((e) => e.ts >= since && !e.incident_id),
      incidents: [],
    };
    return { tape, snap, offset };
  }

  async snapshot(): Promise<Snapshot> {
    if (this.snap) return this.snap;
    const idx = await this.loadIndex();
    const first = idx.chapters[0];
    const { snap } = await this.stage(first);
    this.current = first;
    this.setSnap(snap);
    this.phase = "idle";
    this.changed();
    return snap;
  }

  private setSnap(snap: Snapshot) {
    this.snap = snap;
    this.incidents = new Map(snap.incidents.map((i) => [i.id, i]));
    this.events = [...snap.events];
    this.lastId = Math.max(0, ...snap.events.map((e) => e.id));
    this.seq = 0;
  }

  private clear() {
    for (const t of this.timers) window.clearTimeout(t);
    this.timers = [];
    this.run = null;
    this.hold = null;
    this.rejected = false;
  }

  /** Play one recording from its calm start, stopping at the decision to apply the visitor's settings. */
  async start(scenario: string, byUser = true): Promise<void> {
    if (byUser) this.touched = true;
    const ch = this.chapter(scenario);
    if (!ch) throw new Error(`No recording for ${scenario}`);
    this.clear();
    const { tape, snap, offset } = await this.stage(ch);
    this.current = ch;
    this.setSnap(snap);
    for (const fn of this.snapFns) fn(snap);
    this.phase = "playing";
    this.startedAt = Date.now();
    this.injectAt = (tape.inject_ts + offset) * 1000;
    const kinds = tape.messages.map((x) => (x.m.type === "event" ? x.m.event.kind : ""));
    const gate = kinds.indexOf("decided");
    const acting = kinds.indexOf("acting", gate);
    this.run = {
      tape, offset, gate, acting, extra: 0, lost: 0, waitSince: null, failPerMin: 0,
      alone: true, why: "", autonomy: "", label: "", incident: null,
    };
    this.changed();
    const t0 = tape.snapshot.taken_at;
    const stop = gate >= 0 && acting > gate ? gate : tape.messages.length;
    tape.messages.slice(0, stop).forEach(({ t, m }) => {
      this.timers.push(window.setTimeout(() => this.emit(this.fix(m)), Math.max(0, (t - t0) * 1000)));
    });
    if (stop < tape.messages.length) {
      this.timers.push(window.setTimeout(() => this.decide(), Math.max(0, (tape.messages[stop].t - t0) * 1000)));
    } else {
      this.timers.push(window.setTimeout(() => { this.phase = "done"; this.changed(); }, Math.max(0, (tape.end_ts - t0) * 1000)));
    }
  }

  /** VIGIL's decision, under the visitor's autonomy and kill switch (policy.py: Governor). */
  private decide() {
    const run = this.run;
    const ctl = this.control;
    if (!run || !ctl) return;
    const msgs = run.tape.messages;
    const rec = msgs.slice(run.gate).find((x) => x.m.type === "incident" && x.m.incident.data.decision?.action);
    if (!rec || rec.m.type !== "incident") { this.resume(run.gate); return; }
    const d = rec.m.incident.data.decision!;
    const confidence = rec.m.incident.data.diagnosis?.confidence ?? "Medium";
    run.label = d.options.find((o) => o.action === d.action && o.target === d.target)?.label ?? `${d.action} ${d.target}`;
    run.failPerMin = d.fail_per_min ?? 0;
    const incId = rec.m.incident.id;

    if (ctl.paused) {
      if (this.hold === "blocked") return;
      run.waitSince ??= Date.now();
      this.say(incId, "decide", "policy", "action_blocked", `Would ${run.label}, but kill switch is on. Holding.`, { blockers: ["kill switch is on"] });
      const base = run.incident ?? (this.fix(rec.m) as { type: "incident"; incident: Incident }).incident;
      this.emit({ type: "incident", incident: { ...base, status: "DIAGNOSED", data: { ...base.data, decision: { status: "blocked", reason: "kill switch is on", options: d.options } } } });
      this.hold = "blocked";
      this.changed();
      return;
    }

    const risk = RISK[d.action ?? ""] ?? "high";
    run.alone = confidence === "High" && ALLOWED_ALONE[ctl.autonomy].includes(risk);
    run.why = confidence !== "High" ? `confidence ${confidence}: a human must approve`
      : run.alone ? `confidence High and ${d.action} is ${risk} risk at autonomy ${ctl.autonomy}`
      : `autonomy ${ctl.autonomy} does not allow ${risk}-risk actions alone`;
    run.autonomy = ctl.autonomy;
    if (run.alone) { this.resume(run.gate); return; }

    // A human must approve: show the decision, then wait for Approve or Reject.
    run.waitSince ??= Date.now();
    const waited = run.extra + (Date.now() - run.waitSince) / 1000;
    for (let i = run.gate; i < run.acting; i++) this.emit(this.fix(msgs[i].m, waited));
    this.say(incId, "decide", "policy", "awaiting_approval", `Waiting for approval to ${run.label}: ${run.why}`,
      { from: "DIAGNOSED", to: "AWAITING_APPROVAL", action: d.action, target: d.target });
    const inc = run.incident!;
    this.emit({ type: "incident", incident: { ...inc, status: "AWAITING_APPROVAL" } });
    this.hold = "approval";
    this.changed();
  }

  /** Play the rest of the recording from message `k`, after any wait. */
  private resume(k: number) {
    const run = this.run;
    if (!run) return;
    if (run.waitSince != null) {
      const waited = (Date.now() - run.waitSince) / 1000;
      run.extra += waited;
      run.lost += (waited * run.failPerMin) / 60;
      run.waitSince = null;
    }
    this.hold = null;
    this.changed();
    const msgs = run.tape.messages;
    const tk = msgs[k]?.t ?? run.tape.end_ts;
    for (let i = k; i < msgs.length; i++) {
      const { t, m } = msgs[i];
      this.timers.push(window.setTimeout(() => this.emit(this.fix(m)), Math.max(0, (t - tk) * 1000)));
    }
    this.timers.push(window.setTimeout(() => { this.phase = "done"; this.changed(); }, Math.max(0, (run.tape.end_ts - tk) * 1000)));
  }

  /** A recorded message moved to now, with the decision as the visitor's settings made it. */
  private fix(m: StreamMessage, extra = this.run?.extra ?? 0): StreamMessage {
    const run = this.run;
    if (!run) return m;
    const s = shift(m, run.offset + extra);
    if (s.type === "event") {
      const e = { ...s.event };
      if (run.why && e.kind === "decided") e.payload = { ...e.payload, acted_alone: run.alone, why: run.why, autonomy: run.autonomy };
      if (run.why && e.kind === "acting") e.summary = `Acting alone: ${run.why}`;
      if (e.kind === "resolved" && run.extra > 0) {
        const mttr = Number(e.payload.mttr_s ?? 0) + run.extra;
        const lost = Math.round(Number(e.payload.failed_checkouts ?? 0) + run.lost);
        e.payload = { ...e.payload, mttr_s: Math.round(mttr * 10) / 10, failed_checkouts: lost };
        e.summary = e.summary
          .replace(/\d+m \d+s from first symptom/, `${duration(mttr)} from first symptom`)
          .replace(/[\d,]+ checkouts failed/, `${num(lost)} checkouts failed`)
          + ` That includes ${duration(run.extra)} waiting for a human.`;
      }
      return { type: "event", event: e };
    }
    if (s.type === "incident") {
      const inc = s.incident;
      const data = { ...inc.data };
      if (run.why && data.decision?.status === "chosen") data.decision = { ...data.decision, acted_alone: run.alone, why: run.why, autonomy: run.autonomy };
      let opened = inc.opened_at;
      if (extra > 0) {
        // What happened before the wait keeps its time; the wait only delays what follows.
        const before = (shift(m, run.offset) as { type: "incident"; incident: Incident }).incident;
        opened = before.opened_at;
        data.onset = before.data.onset;
        data.diagnosis = before.data.diagnosis;
        if (data.failed_checkouts != null) data.failed_checkouts += run.lost;
        if (data.metrics) data.metrics = { ...data.metrics, mttr_s: data.metrics.mttr_s + run.extra, failed_checkouts: Math.round(data.metrics.failed_checkouts + run.lost) };
      }
      return { type: "incident", incident: { ...inc, opened_at: opened, data } };
    }
    return s;
  }

  /** An event VIGIL would emit for the visitor's settings, ordered just after the newest recorded one. */
  private say(incident_id: string | null, stage: string, actor: string, kind: string, summary: string, payload: Record<string, unknown>, id?: number) {
    this.seq += 1;
    const event: VigilEvent = { id: id ?? this.lastId + this.seq * 1e-4, ts: Date.now() / 1000, incident_id, stage, actor, kind, summary, payload };
    this.emit({ type: "event", event }, id === undefined);
  }

  private emit(m: StreamMessage, made = false) {
    if (m.type === "event") {
      this.events = [...this.events, m.event].slice(-400);
      if (!made && m.event.id > this.lastId) { this.lastId = m.event.id; this.seq = 0; }
    }
    if (m.type === "incident") {
      this.incidents.set(m.incident.id, m.incident);
      if (this.run) this.run.incident = m.incident;
    }
    if (m.type === "tick" && this.snap) this.snap = { ...this.snap, state: { ...this.snap.state, latest: m.point, verdicts: m.verdicts } };
    for (const fn of this.msgFns) fn(m);
  }

  // ---- operator controls (no token: they only change what plays in this browser)

  async setControl(body: Partial<Control>): Promise<Control> {
    const cur = this.control;
    if (!cur) throw new ApiError(409, "The recording is still loading");
    const next: Control = { ...cur };
    const changes: string[] = [];
    if (body.autonomy && body.autonomy in AUTONOMY && body.autonomy !== cur.autonomy) {
      next.autonomy = body.autonomy;
      changes.push(`autonomy ${body.autonomy} (${AUTONOMY[body.autonomy]})`);
    }
    if (typeof body.paused === "boolean" && body.paused !== cur.paused) {
      next.paused = body.paused;
      changes.push(body.paused ? "kill switch ON: no actions will run" : "kill switch OFF: actions allowed");
    }
    this.control = next;
    if (this.snap) this.snap = { ...this.snap, state: { ...this.snap.state, health: { ...this.snap.state.health, control: next } } };
    if (changes.length) this.say(null, "system", "human", "control_changed", `Control: ${changes.join("; ")}`, next as unknown as Record<string, unknown>);
    if (this.hold === "blocked" && !next.paused) this.decide();
    this.changed();
    return next;
  }

  async approve(id: string): Promise<Incident> {
    const run = this.run;
    if (!run || this.hold !== "approval" || run.incident?.id !== id) throw new ApiError(409, "incident is not waiting for approval");
    if (this.control?.paused) throw new ApiError(409, "kill switch is on");
    const acting = run.tape.messages[run.acting].m;
    const d = run.incident.data.decision;
    this.say(id, "act", "human", "approved", `Approved by operator: ${run.label}`,
      { from: "AWAITING_APPROVAL", to: "REMEDIATING", action: d?.action, target: d?.target, by: "operator" },
      acting.type === "event" ? acting.event.id : undefined);
    const inc = run.incident;
    this.resume(run.acting + 1);
    return inc;
  }

  async reject(id: string): Promise<Incident> {
    const run = this.run;
    if (!run || this.hold !== "approval" || run.incident?.id !== id) throw new ApiError(409, "incident is not waiting for approval");
    const inc = run.incident;
    this.say(id, "decide", "human", "rejected", `Operator rejected the proposed fix and took over ${id}`,
      { from: "AWAITING_APPROVAL", to: "ESCALATED", by: "operator" });
    const closed: Incident = { ...inc, status: "ESCALATED", closed_at: Date.now() / 1000 };
    this.emit({ type: "incident", incident: closed });
    for (const t of this.timers) window.clearTimeout(t);
    this.timers = [];
    this.hold = null;
    this.rejected = true;
    this.phase = "done";
    this.changed();
    return closed;
  }

  /** Back to the calm start of the current recording, nothing playing. */
  async reset(): Promise<void> {
    this.touched = true;
    this.clear();
    const ch = this.current ?? (await this.loadIndex()).chapters[0];
    const { snap } = await this.stage(ch);
    this.setSnap(snap);
    for (const fn of this.snapFns) fn(snap);
    this.phase = "idle";
    this.changed();
  }

  /** Start the first recording by itself if the visitor hasn't picked one (off with ?autoplay=0). */
  autoplay(delayMs = 2500) {
    if (new URLSearchParams(window.location.search).get("autoplay") === "0") return;
    window.setTimeout(() => {
      if (!this.touched && this.phase === "idle" && this.index?.chapters[0]) void this.start(this.index.chapters[0].scenario, false);
    }, delayMs);
  }

  incident(id: string): { incident: Incident; events: VigilEvent[] } | null {
    const inc = this.incidents.get(id);
    return inc ? { incident: inc, events: this.events.filter((e) => e.incident_id === id) } : null;
  }

  incidentList(): Incident[] {
    return [...this.incidents.values()];
  }

  health(): Health | null {
    return this.snap?.state.health ?? null;
  }

  /** What Chaos Lab shows as "running": the fault of the recording being played. */
  chaosStatus(): Record<string, Record<string, number | boolean>> {
    const on = this.phase === "playing" && Date.now() >= this.injectAt ? this.current?.scenario : null;
    return {
      inventory: { latency_ms: on === "slow-inventory" ? 1500 : 0 },
      payment: { leak_mb_per_s: on === "payment-leak" ? 3 : 0, cpu_burn: on === "payment-runaway" },
      loadgen: { rps: on === "flash-sale" ? 120 : 20 },
      deploy: { v2: on === "bad-deploy" },
    };
  }
}

export const player = new Player();

export const SCENARIO_TEXT: Record<string, { description: string; target: string }> = {
  "slow-inventory": { description: "Inventory requests stall for 1.5 s. Payment starts timing out on it, but every health check stays green.", target: "inventory" },
  "payment-leak": { description: "Payment leaks 3 MB/s and would hit its 256 MB limit in about a minute.", target: "payment" },
  "flash-sale": { description: "Real demand: checkout traffic jumps 6x, more than one payment instance can serve. Nothing is broken.", target: "loadgen" },
  "bad-deploy": { description: "A developer ships payment v2 with a bug baked in: 40% of payments fail. A restart brings it back.", target: "payment" },
  "payment-runaway": { description: "A busy loop burns a core in payment. Traffic is flat, CPU per request jumps.", target: "payment" },
};
