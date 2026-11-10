// Shapes returned by the VIGIL engine API (engine/vigil/api.py). Kept in one place
// so a backend change shows up as a type error, not a blank panel.

export type ServiceState = "learning" | "healthy" | "anomalous" | "down" | "unknown";

export interface ServiceSample {
  service: string;
  up: boolean | null;
  rps: number | null;
  error_rate: number | null;
  p95_ms: number | null;
  cpu_pct: number | null;
  mem_mb: number | null;
  mem_slope_mb_s: number | null;
  cpu_per_req: number | null;
  version: string | null;
  instances?: number | null;
  state?: ServiceState | null;
}

export interface EdgeSample {
  caller: string;
  target: string;
  rps: number | null;
  fail_ratio: number | null;
  p95_ms: number | null;
}

export interface UserSample {
  checkout_rps: number | null;
  fail_ratio: number | null;
  p95_ms: number | null;
}

export interface Point {
  ts: number;
  telemetry_ok: boolean;
  services: Record<string, ServiceSample>;
  user: UserSample;
  edges: EdgeSample[];
}

export interface Verdict {
  service: string;
  state: ServiceState;
  flagged?: boolean;
  hard?: boolean;
  reasons?: string[];
  z?: Record<string, number>;
  score_pct?: number | null;
  learning_progress?: number;
  learning_eta_s?: number | null;
  time_to_oom_s?: number | null;
  texts?: Record<string, string>;
  demand?: boolean;
  baseline: { samples: number; created_at: number; medians: Record<string, number> } | null;
  in_incident: string | null;
}

export interface Control {
  autonomy: "L0" | "L1" | "L2" | "L3";
  paused: boolean;
  guardrails: Record<string, number>;
}

export interface Health {
  status: "ok" | "degraded";
  telemetry_ok: boolean | null;
  problem: string | null;
  last_tick_age_s: number | null;
  uptime_s: number;
  edges: string[];
  llm: string;
  control: Control;
}

export interface OptionRow {
  action: string;
  target: string | null;
  p_fix: number;
  saves: number;
  drops: number;
  score: number;
  note: string;
  executable: boolean;
  version?: string | null;
  label: string;
}

export interface Diagnosis {
  services: string[];
  root: string;
  category: string;
  category_why: string;
  confidence: "High" | "Medium" | "Low";
  confidence_reasons: string[];
  evidence_types: string[];
  walk: { root: string; path: string[]; steps: string[]; own_signals: string[]; conclusive: boolean };
  llm: { status: string; root_cause_service?: string; category?: string; hypothesis?: string; error?: string; model?: string };
  symptoms: Record<string, string>;
  evidence: Array<Record<string, unknown> & { id: string; signal: string }>;
  title: string;
  at: number;
}

export interface Decision {
  status: "chosen" | "blocked";
  action?: string;
  target?: string;
  reason?: string;
  options: OptionRow[];
  fail_per_min?: number;
  horizon_min?: number;
  acted_alone?: boolean;
  why?: string;
  autonomy?: string;
}

export interface IncidentData {
  active?: string[];
  failed_checkouts?: number;
  peak_fail_ratio?: number;
  onset?: Record<string, number>;
  diagnosis?: Diagnosis;
  decision?: Decision;
  actions?: Array<{ id: string; action: string; target: string; started_at: number }>;
  verify?: { clean: number; result: "pending" | "passed" | "failed"; deadline: number; target: string };
  metrics?: { mttd_s: number; mttr_s: number; failed_checkouts: number; peak_fail_ratio: number };
}

export type IncidentStatus =
  | "OPEN" | "INVESTIGATING" | "DIAGNOSED" | "AWAITING_APPROVAL" | "REMEDIATING"
  | "VERIFYING" | "RESOLVED" | "SELF_RESOLVED" | "ESCALATED";

export interface Incident {
  id: string;
  seq: number;
  status: IncidentStatus;
  severity: "minor" | "major" | "critical";
  opened_at: number;
  closed_at: number | null;
  services: string[];
  root: string | null;
  category: string | null;
  title: string;
  data: IncidentData;
}

export interface VigilEvent {
  id: number;
  ts: number;
  incident_id: string | null;
  stage: string;
  actor: string;
  kind: string;
  summary: string;
  payload: Record<string, unknown>;
}

export interface StateResponse {
  health: Health;
  latest: Point | null;
  verdicts: Record<string, Verdict>;
  open_incidents: Incident[];
}

export type StreamMessage =
  | { type: "tick"; point: Point; verdicts: Record<string, Verdict> }
  | { type: "event"; event: VigilEvent }
  | { type: "incident"; incident: Incident };

export interface Scenario {
  label: string;
  description: string;
  target: string;
}

export const TERMINAL: IncidentStatus[] = ["RESOLVED", "SELF_RESOLVED", "ESCALATED"];
