// DEV ONLY. A stand-in for the engine and chaos controller so the UI can be
// developed and screenshotted without Docker. It replays the shape of the real
// VGL-0002 run (numbers from a live run). Never shipped: the Docker image builds
// from src/ only, and production talks to the real engine.
//
//   PHASE=idle|verifying|approval|resolved|escalated node dev/mock-server.mjs
import http from "node:http";

const PHASE = process.env.PHASE || "resolved";
const now = Date.now() / 1000;
const onset = now - 95;
const rnd = (a, b) => a + Math.random() * (b - a);

function point(ts) {
  const inFault = PHASE !== "idle" && ts >= onset && (PHASE === "approval" || PHASE === "escalated" || ts < onset + 40);
  const verifying = PHASE === "verifying" && ts >= onset + 40;
  const bad = inFault && !verifying;
  return {
    ts, telemetry_ok: true,
    services: {
      payment: { service: "payment", up: true, rps: rnd(18, 22), error_rate: bad ? rnd(0.8, 1) : 0, p95_ms: bad ? 2425 : rnd(60, 80), cpu_pct: rnd(13, 18), mem_mb: rnd(54, 56), mem_slope_mb_s: 0, cpu_per_req: 0.8, version: "v1", state: bad ? "anomalous" : "healthy" },
      inventory: { service: "inventory", up: true, rps: rnd(18, 22), error_rate: 0, p95_ms: bad ? 2425 : rnd(20, 28), cpu_pct: rnd(5, 8), mem_mb: rnd(51, 52), mem_slope_mb_s: 0, cpu_per_req: 0.32, version: "v1", state: bad ? "anomalous" : "healthy" },
    },
    user: { checkout_rps: rnd(18, 22), fail_ratio: bad ? rnd(0.8, 0.95) : 0, p95_ms: bad ? 1100 : rnd(85, 100) },
    edges: [{ caller: "payment", target: "inventory", rps: rnd(18, 22), fail_ratio: bad ? 1 : 0, p95_ms: bad ? 2425 : rnd(22, 30) }],
  };
}
const history = Array.from({ length: 180 }, (_, i) => point(now - (179 - i) * 5));

const options = [
  { action: "restart", target: "inventory", p_fix: 0.85, saves: 4999, drops: 59, score: 4940, note: "clears a stuck process", executable: true, label: "restart inventory" },
  { action: "wait", target: null, p_fix: 0, saves: 0, drops: 0, score: 0, note: "~1,176 checkouts keep failing every minute", executable: true, label: "wait and watch" },
  { action: "restart", target: "payment", p_fix: 0, saves: 0, drops: 59, score: -59, note: "a symptom: its failures come from another service", executable: false, label: "restart payment" },
];
const diagnosis = {
  services: ["inventory", "payment"], root: "inventory", category: "stuck_process", category_why: "latency p95 2.4 s (normal 24 ms) while traffic is flat",
  confidence: PHASE === "approval" ? "Medium" : "High",
  confidence_reasons: PHASE === "approval" ? ["evidence types: metrics, edges, timing", "LLM unavailable: walk alone, capped at Medium"] : ["evidence types: metrics, edges, timing", "walk and LLM independently agree on inventory"],
  evidence_types: ["metrics", "edges", "timing"],
  walk: { root: "inventory", path: ["payment", "inventory"], steps: ["payment -> inventory: 100% of calls failing, p95 2425 ms", "its own dependencies are healthy"], own_signals: ["latency p95 2.4 s (normal 24 ms)"], conclusive: true },
  llm: PHASE === "approval" ? { status: "unavailable", error: "timeout" } : { status: "ok", root_cause_service: "inventory", category: "stuck_process", hypothesis: "Inventory's latency spiked dramatically while traffic stayed flat, causing payment to time out on its calls." },
  symptoms: { payment: "100% of its errors are failed calls to inventory (100% of calls failing, p95 2425 ms)" },
  evidence: Array.from({ length: 19 }, (_, i) => ({ id: `E${i + 1}`, signal: "x" })), title: "inventory: stuck process; payment failing as a result", at: onset + 21,
};
const statusByPhase = { idle: null, verifying: "VERIFYING", approval: "AWAITING_APPROVAL", resolved: "RESOLVED", escalated: "ESCALATED" };
const status = statusByPhase[PHASE];
const incident = status && {
  id: "VGL-0002", seq: 2, status, severity: "critical", opened_at: onset + 10,
  closed_at: ["RESOLVED", "ESCALATED"].includes(status) ? onset + 66 : null,
  services: ["inventory", "payment"], root: "inventory", category: "stuck_process", title: diagnosis.title,
  data: {
    active: [], failed_checkouts: 1213, peak_fail_ratio: 1, onset: { inventory: onset, payment: onset + 5 }, diagnosis,
    decision: { status: "chosen", action: "restart", target: "inventory", options, fail_per_min: 1176, horizon_min: 5,
      acted_alone: PHASE !== "approval", why: PHASE === "approval" ? "confidence Medium: a human must approve" : "confidence High and restart is low risk at autonomy L2", autonomy: "L2" },
    actions: PHASE === "approval" ? [] : [{ id: "c92852d6", action: "restart", target: "inventory", started_at: onset + 26 }],
    verify: { clean: 2, result: PHASE === "resolved" ? "passed" : "pending", deadline: now + 60, target: "inventory" },
    metrics: PHASE === "resolved" ? { mttd_s: 10.1, mttr_s: 66, failed_checkouts: 1213, peak_fail_ratio: 1 } : undefined,
  },
};
let id = 0;
const ev = (dt, actor, kind, summary, inc = "VGL-0002") => ({ id: ++id, ts: onset + dt, incident_id: inc, stage: "x", actor, kind, summary, payload: {} });
const events = PHASE === "idle" ? [ev(-600, "engine", "telemetry_ok", "Telemetry restored; decisions resume", null)] : [
  ev(0, "detector", "suspect", "inventory: latency p95 2.1 s (normal 24 ms) (beyond anything in its baseline). Watching for confirmation", null),
  ev(5, "detector", "suspect", "payment: latency p95 2.3 s (normal 72 ms), error rate 32% (normal 0.0%). Watching for confirmation", null),
  ev(10, "detector", "incident_opened", "Incident VGL-0002 opened: inventory latency p95 2.4 s (normal 24 ms); 83% of checkouts failing"),
  ev(10.1, "engine", "investigating", "Investigating: collecting evidence across services"),
  ev(15, "engine", "service_joined", "payment joined VGL-0002: error rate 100% (normal 0.0%), latency p95 2.4 s (normal 72 ms) (connected to inventory)"),
  ev(20, "walk", "blame_walk", "Blame walk: payment -> inventory. Stops at inventory: its own dependencies are healthy"),
  ev(21.4, "llm", PHASE === "approval" ? "llm_unavailable" : "llm_ok", PHASE === "approval" ? "LLM unavailable (timeout); walk verdict stands, confidence capped at Medium" : "LLM (never shown the walk) names inventory as stuck_process: Inventory's latency spiked dramatically while traffic stayed flat, causing payment to time out on its calls."),
  ev(21.4, "engine", "diagnosed", `Root cause: inventory (stuck process): latency p95 2.4 s (normal 24 ms) while traffic is flat. Confidence ${diagnosis.confidence}. payment is a symptom: 100% of its errors are failed calls to inventory.`),
  ev(26, "policy", "decided", "Decision: restart inventory. Next 5 min at ~1,176 failed checkouts/min: restart inventory saves ~4,999 (score 4,940) · wait saves 0 · restart payment (symptom) saves ~0 (score -59)"),
  ...(PHASE === "approval" ? [ev(26.1, "policy", "awaiting_approval", "Waiting for approval to restart inventory: confidence Medium: a human must approve")] : [
    ev(26.1, "policy", "acting", "Acting alone: confidence High and restart is low risk at autonomy L2"),
    ev(26.2, "executor", "action_started", "Restarting inventory (action c92852d6)"),
    ev(29.1, "executor", "action_done", "inventory restarted in 2.9 s"),
    ev(29.2, "verifier", "verifying", "Verifying: inventory, payment and checkouts must stay normal for 4 checks in a row (up to 90 s)"),
  ]),
  ...(PHASE === "resolved" ? [
    ev(56, "detector", "service_recovered", "inventory back inside its normal band"),
    ev(61, "detector", "service_recovered", "payment back inside its normal band"),
    ev(66, "verifier", "resolved", "VGL-0002 resolved and verified: checkouts failing 0% (peak 100%); latency p95 24 ms (normal 24 ms). 1m 06s from first symptom to verified recovery; 1,213 checkouts failed."),
  ] : []),
  ...(PHASE === "escalated" ? [
    ev(120, "verifier", "verify_failed", "Not recovered after 90 s: inventory still abnormal (88% of checkouts failing)"),
    ev(121, "policy", "escalated", "VGL-0002 escalated to a human: previous fix did not restore service. Root: inventory (stuck process); tried: restart inventory."),
  ] : []),
];
const medians = (p95, cpu, mem) => ({ p95_ms: p95, error_rate: 0, cpu_pct: cpu, mem_mb: mem, mem_slope_mb_s: 0, rps: 19.8, cpu_per_req: 0.8 });
const latest = history[history.length - 1];
const state_learning = {};
if (process.env.INSTANCES) latest.services.payment.instances = Number(process.env.INSTANCES);  // dev: preview a scaled-out map
const v = (svc, med) => {
  const st = latest.services[svc].state;
  return { service: svc, state: st, flagged: st === "anomalous", reasons: st === "anomalous" ? ["latency p95 2.4 s (normal 24 ms)"] : [], z: {}, score_pct: 50, learning_progress: 1, texts: {},
    baseline: { samples: 37, created_at: now - 3600, medians: med }, in_incident: incident && status !== "RESOLVED" && status !== "ESCALATED" ? "VGL-0002" : null };
};
if (process.env.LEARNING) {   // dev: preview the learning screen, e.g. LEARNING=0.42
  for (const [name, val] of Object.entries({ payment: Number(process.env.LEARNING), inventory: Math.min(1, Number(process.env.LEARNING) + 0.2) })) {
    state_learning[name] = { service: name, state: val >= 1 ? "healthy" : "learning", learning_progress: val, learning_eta_s: Math.round((1 - val) * 180), baseline: null, in_incident: null, z: {}, reasons: [], texts: {} };
  }
}
const control = { autonomy: "L2", paused: false, guardrails: { cooldown_s: 120, max_attempts: 2, budget_actions: 3, budget_window_s: 600 } };
const health = { status: "ok", telemetry_ok: true, problem: null, last_tick_age_s: 1, uptime_s: 3600, edges: ["payment->inventory", "storefront->payment"], llm: "on", control };
const state = { health, latest, verdicts: { payment: v("payment", medians(72, 14.8, 54.6)), inventory: v("inventory", medians(24, 6.2, 51.3)), ...state_learning }, open_incidents: incident && !["RESOLVED", "ESCALATED"].includes(status) ? [incident] : [] };

const send = (res, code, body) => { res.writeHead(code, { "Content-Type": "application/json" }); res.end(JSON.stringify(body)); };
http.createServer((req, res) => {
  if (process.env.DELAY && req.url.startsWith("/api/state")) {   // dev: preview loading skeletons
    return setTimeout(() => send(res, 200, state), Number(process.env.DELAY));
  }
  const u = new URL(req.url, "http://x");
  const p = u.pathname;
  if (p === "/api/state") return send(res, 200, state);
  if (p === "/api/health") return send(res, 200, health);
  if (p === "/api/history") return send(res, 200, { points: history });
  if (p === "/api/events") return send(res, 200, { events });
  if (p === "/api/incidents") return send(res, 200, { incidents: incident ? [incident] : [] });
  if (p.startsWith("/api/incidents/")) return incident ? send(res, 200, { incident, events: events.filter((e) => e.incident_id === "VGL-0002") }) : send(res, 404, { detail: "no such incident" });
  if (p === "/api/operator/check") return req.headers["x-vigil-token"] === "demo" ? send(res, 200, { ok: true }) : send(res, 403, { detail: "admin token required" });
  if (p === "/api/control") return send(res, 200, control);
  if (p === "/proving-ground/scorecard.json") return send(res, 200, { summary: { started_at: now - 1500, finished_at: now - 300, autonomy_during_run: "L3", passed: 6, total: 6, median_detect_s: 13.2, median_recover_s: 68.4, false_alarms: 0 },
    rows: [
      { scenario: "quiet", label: "Quiet period (180s, nothing broken)", expected: "no incident", false_alarms: 0, pass: true },
      { scenario: "slow-inventory", label: "Slow inventory (gray failure)", expected: "restart inventory", incident: "VGL-0021", mttd_s: 12.1, root: "inventory", category: "stuck_process", confidence: "High", actions: ["restart inventory"], mttr_s: 66.0, checkouts_lost: 830, pass: true },
      { scenario: "flash-sale", label: "Flash sale (real demand)", expected: "no restart or rollback", actions: [], pass: true },
    ] });
  if (p === "/api/stream") { res.writeHead(200, { "Content-Type": "text/event-stream" }); res.write("retry: 3000\n\n"); const t = setInterval(() => res.write(": keep-alive\n\n"), 15000); req.on("close", () => clearInterval(t)); return; }
  if (p === "/scenarios") return send(res, 200, {
    "slow-inventory": { label: "Slow down inventory", description: "Inventory requests stall for 1.5 s. Payment starts timing out on it.", target: "inventory" },
    "payment-leak": { label: "Memory leak in payment", description: "Payment leaks 3 MB/s and will hit its 256 MB limit in about a minute.", target: "payment" },
    "payment-runaway": { label: "Runaway loop in payment", description: "A busy loop burns a core in payment. Traffic is flat, CPU per request jumps.", target: "payment" },
    "flash-sale": { label: "Flash sale (4x traffic)", description: "Real demand: checkout traffic quadruples. Nothing is broken.", target: "loadgen" },
  });
  if (p === "/status") return send(res, 200, { payment: { latency_ms: 0, error_rate: 0, leak_mb_per_s: 0, cpu_burn: false }, inventory: { latency_ms: PHASE === "approval" ? 1500 : 0 }, loadgen: { rps: 20 } });
  if (p === "/pay") return PHASE === "approval" ? send(res, 503, { detail: "inventory timeout" }) : send(res, 200, { order_id: "a1b2c3d4e5f6", status: "paid" });
  send(res, 404, { detail: "not mocked" });
}).listen(Number(process.env.PORT || 8080), () => console.log(`mock (${PHASE}) on ${process.env.PORT || 8080}`));
