# VIGIL

**Watches every service. Fixes the real cause.**

> ## ▶️ MUST WATCH FOR BETTER UNDERSTANDING
> **Live demo (3 min):** VIGIL running for real on my laptop: it finds the real cause, fixes it on its own, and a phone payment fails and then succeeds again.
>
> **https://youtu.be/5g_4jCZjP8M**
>
> [![VIGIL live demo: must watch](https://img.youtube.com/vi/5g_4jCZjP8M/maxresdefault.jpg)](https://youtu.be/5g_4jCZjP8M)

VIGIL is an autonomous resilience engine for microservices. It notices when customers start
failing, works out which service is actually broken (not just the loudest one), applies the
right fix inside strict guardrails, and only calls it done when real checkouts recover.

| | |
| --- | --- |
| **▶️ MUST WATCH: live demo (3 min)** | **https://youtu.be/5g_4jCZjP8M** |
| **Full walkthrough (5 min, voiced)** | **https://youtu.be/AQKw7XUuKtk** |
| **Try it in your browser** (a real recorded run, nothing to install) | **https://vigil-six-blue.vercel.app** |
| **Proving Ground scorecard** | **5/5 passed** across 4 real faults and a quiet period: median detection 18.8 s, median verified recovery 1m 15s, 0 false alarms |

## The problem

The outage that hurts most is the one that looks healthy. In our demo shop, inventory slows
down but its health check stays green. Payment, which calls it, starts failing, so the loud
service is the wrong one to restart. Kubernetes liveness probes don't help either: the health check
passes, so nothing is restarted. In our test, with nobody acting, that fault was still failing after
**22 min 31 s** and had cost **20,336 checkouts**. VIGIL fixed the same fault in **1 min 13 s**
(598 checkouts lost).

## What VIGIL does

Every 5 seconds it runs one loop: **observe → detect → diagnose → decide → act → verify**.

- **Detect:** learns each service's normal for 3 minutes, then flags numbers far outside it
  (robust z-scores plus an IsolationForest). It opens an incident only when customers are hurt
  or the anomaly persists, so a busy laptop or a traffic spike doesn't page anyone.
- **Diagnose:** a blame walk follows failing calls downstream until it reaches a service whose
  own dependencies are healthy. An LLM (Groq, gpt-oss-120b) is asked the same question
  **without being shown the walk's answer**. Agreement means High confidence.
- **Decide:** every option is priced in checkouts it saves over the next 5 minutes, minus the
  checkouts it drops. Restarting a symptom scores negative.
- **Act:** the right fix for the failure. **Restart** a stuck or leaking service, **roll back**
  a bad deploy (start the old version, wait until healthy, stop the new one), **add a warm
  replica** for real demand and remove it when traffic calms down.
- **Verify:** done means services and customer checkouts stay normal 4 checks in a row.
  Otherwise VIGIL retries once, then hands over to a human. It never loops on restarts.

## Safe by design

- The LLM is only a second opinion. It never runs commands or picks fixes.
- VIGIL's only hands are a locked actuator that can restart, start or stop a labelled
  container. There is no shell and no other API.
- Guardrails: cooldowns, max attempts, an action budget, a kill switch, and autonomy levels
  **L0** (watch) to **L3** (act alone). Below High confidence, a human approves.
- Chaos runs in its own container on its own network. VIGIL can't see it and only knows what
  Prometheus knows.

## Proving Ground: VIGIL scores itself

`docker compose --profile prove run --rm proving-ground` injects every fault for real, scores
VIGIL against an answer key it never sees, and checks for false alarms during a quiet period.

Final run (2 sessions on 20 Oct 2026, autonomy L3). Times are from the moment the fault was injected:

| Scenario | Expected fix | VIGIL's root | VIGIL's fix | Detect | Recovered | Checkouts lost | Result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Quiet period (180s, nothing broken) | no incident | – | – | – | – | – | PASS |
| Slow inventory (gray failure) | restart inventory | inventory (stuck_process) | restart inventory | 18.0 s | 73.0 s | 598 | PASS |
| Memory leak in payment | restart payment | payment (resource_leak) | restart payment | 24.6 s | 74.6 s | 53 | PASS |
| Flash sale (real demand, 6x) | scale out payment | payment (load_saturation) | scale_out payment | 19.5 s | 74.6 s | 465 | PASS |
| Bad deploy (payment v2) | rollback payment | payment (regression_after_change) | rollback payment | 17.9 s | 77.9 s | 237 | PASS |

Median detection **18.8 s**, median verified recovery **74.6 s**, **0** false alarms. The runaway-loop scenario is in the code but wasn't part of this run.

## Tech stack

| Part | Built with |
| --- | --- |
| Engine | Python 3.13, FastAPI, scikit-learn (IsolationForest), NumPy, SQLite event store, 104 pytest tests |
| Telemetry | Prometheus, golden signals from every service |
| Diagnosis second opinion | Groq API, `openai/gpt-oss-120b` (optional: without a key VIGIL caps confidence at Medium and asks a human) |
| Demo system | storefront, payment (v1, v2, warm replica) and inventory services, load generator, chaos controller |
| Console | React 18, TypeScript, Vite, served by nginx. Live updates over server-sent events |
| Runs on | Docker Compose, one laptop |

## Feasibility and scale

- Everything here runs on one laptop today. The pieces map one-to-one to production tools:
  Prometheus stays, the actuator becomes a Kubernetes controller limited by RBAC to
  `rollout restart`, `scale` and `rollout undo`, and the call graph comes from tracing
  (OpenTelemetry) instead of a fixed list.
- Detection is per service and independent, so cost grows linearly with the number of
  services. The blame walk only visits services on a failing path.
- These are next steps, not built yet. What is built is tested end to end by the Proving Ground.

## Run it (Windows, Docker Desktop)

Open PowerShell in this folder:

```powershell
copy .env.example .env      # then edit .env: set CHAOS_TOKEN and VIGIL_ADMIN_TOKEN
docker compose up --build -d
docker compose ps           # every container should be "running" (services: "healthy")
```

| Open | What you should see |
| --- | --- |
| **http://localhost:3000** | **The VIGIL home page**, then **Open the console** (start here) |
| http://localhost:3000/store | Lumen Goods, the demo shop: every Buy is a real checkout through payment and inventory |
| http://localhost:8090 | The chaos controller's own bare panel (the command center's Chaos Lab drawer does the same) |
| http://localhost:9090/targets | Prometheus: payment, inventory, storefront all **UP** |
| http://localhost:8001/health | payment-service health |
| http://localhost:8080/api/state | VIGIL: per-service state (`learning` for 3 min, then `healthy`) |
| http://localhost:8080/api/events | VIGIL's narration: every step it took, in plain English |

Stop everything: `docker compose down`.

## The command center (http://localhost:3000/console)

| Part | What it shows |
| --- | --- |
| Control capsule (top) | One status line, autonomy **L0 to L3**, the kill switch (**Actions armed / paused**) and the operator lock |
| Service map | storefront → payment → inventory from live Prometheus data. Failing calls turn red and dashed; nodes get **Root cause**, **Symptom** or **VIGIL acting** badges; a teal dot traces the blame walk |
| Signals | Checkouts failing, p95 latency, CPU and memory for 15 minutes, with VIGIL's learned normal (dashed), the incident (red band) and VIGIL's action (teal line). Hover one chart and all four follow |
| The loop | Detect → Diagnose → Decide → Act → Verify. Each step turns green when VIGIL records it, with time since the first symptom |
| What VIGIL is doing | Every event in plain English, newest first |
| Why this fix | Every option priced in checkouts saved over 5 minutes, the confidence, who decided, and what the LLM said |
| Approval card | Appears by itself when VIGIL needs a human (confidence below High, or autonomy L1) |
| Chaos Lab (flask icon) | Hold a button to inject a fault. Each shows the answer key, which VIGIL never sees |
| Incidents | Every incident; open one for the full diagnosis, timeline and a **Download postmortem** (Markdown) |

Changing autonomy, the kill switch, approving and rejecting all ask once for your
`VIGIL_ADMIN_TOKEN` (kept for this browser tab only). Watching needs no token.

**A 60-second demo:** open the store in a second window and buy something (it works).
Open Chaos Lab, hold **Slow down inventory**. Buy again: it fails. Watch the map turn red,
the loop fill in, VIGIL restart inventory by itself, and checkouts recover. Buy again: it works.
Switch to **L1** first if you want the approval card instead.

### Working on the UI

The UI is React + TypeScript built with Vite, served by nginx in the `ui` container. To
change it with live reload (needs Node 22), keep the Docker stack running and:

```powershell
cd ui
npm ci
npm run dev      # http://localhost:5173, proxies to the engine, payment and chaos ports
```

## Try the flagship failure by hand

1. In Prometheus (http://localhost:9090/graph), run:

   ```promql
   sum by (outcome) (rate(checkout_requests_total[30s]))
   ```

   You should see about 20/s `ok`.
2. In Chaos Lab, click **Slow down inventory**.
3. Within ~15 s, `failed` checkouts climb. Now run:

   ```promql
   sum by (service, target, outcome) (rate(dependency_requests_total[30s]))
   ```

   Payment's calls to inventory are timing out. Payment is the loud one; inventory is the cause.
4. Note that http://localhost:8002/health still says `ok`. That is a gray failure.
5. Fix it the way VIGIL will: `docker restart inventory`. Checkouts recover.
6. Click **Reset everything** in Chaos Lab before the next run.

## Watch VIGIL catch it

Wait until http://localhost:8080/api/state shows both services `healthy` (about 3 minutes
of learning). Then click **Slow down inventory** and refresh http://localhost:8080/api/events:

1. `suspect`: inventory latency far outside normal, watching for confirmation
2. `incident_opened`: confirmed after 3 of 4 ticks because checkouts are failing (a slowdown that hurts no one, like a busy laptop, must last about a minute first)
3. `service_joined`: payment joins the same incident because it calls inventory (one incident, not two)
4. `severity_raised`: critical, based on the share of checkouts failing

About 10 s after the incident opens, VIGIL diagnoses it:

5. `blame_walk`: payment -> inventory. Stops at inventory: its own dependencies are healthy
6. `llm_ok`: the LLM, never shown the walk's answer, independently names inventory
7. `diagnosed`: root cause inventory (stuck process), confidence **High**; payment is a symptom

Then open http://localhost:8080/api/incidents/VGL-0001 to see the full diagnosis: evidence
with IDs, what was ruled out and why, and the reasons behind the confidence level.

Then VIGIL fixes it itself (confidence High, autonomy L2):

8. `decided`: every option priced in failed checkouts over the next 5 minutes:
   restart inventory saves ~2,000 · wait saves 0 · restart payment (symptom) scores negative
9. `acting`: "Acting alone: confidence High and restart is low risk at autonomy L2"
10. `action_started` / `action_done`: inventory restarted through the actuator
11. `verifying`: every affected service **and checkouts** must stay normal for 4 checks in a row
12. `resolved`: verified recovery, time from first symptom, and checkouts lost

Success is never "the restart command worked". If checkouts do not recover within 90 s,
VIGIL tries the next allowed fix or escalates to a human. It never loops.

## Ship a bad deploy and watch VIGIL roll it back

payment v2 has a bug baked into the version (40% of payments fail). A restart brings
it straight back; only running v1 again fixes it.

```powershell
.\scripts\deploy.ps1 v2      # blue/green: start v2 behind "payment", wait healthy, stop v1
```

1. `version_changed`: payment v1 -> v2 (the map shows the version on the payment box)
2. `incident_opened`: payment errors, checkouts failing
3. `diagnosed`: root payment, **regression after a deploy** ("started 1 min after payment changed v1 -> v2")
4. `decided`: roll back payment to v1 scores highest; restart payment scores negative
5. At **L2** the approval card asks you (a rollback changes which code runs: medium risk).
   At **L3** VIGIL does it alone.
6. `action_done`: v1 started and healthy *before* v2 is stopped, so users are never left with nothing
7. `resolved`: verified on checkouts, like every fix

To undo a deploy by hand: `.\scripts\deploy.ps1 v1`. To remove v2 completely:
`docker compose --profile deploy down`.

## Flash sale: VIGIL adds capacity instead of restarting

Each payment instance has a pool of 2 card-processor slots (about 100 checkouts/s).
The flash sale sends 6x normal traffic (about 120/s): one instance sheds checkouts
with "payment busy", even though nothing is broken.

1. Diagnosis: **load saturation** (traffic 6x while CPU per request stays normal)
2. Options: **add a payment replica** scores highest; restart scores 0 or less (a restart
   adds no capacity and drops the checkouts in flight)
3. Adding capacity is low risk, so at L2 VIGIL does it alone: the actuator starts the
   warm spare `payment-replica`, which joins the same `payment` name (map: `v1 ×2`)
4. Verified on checkouts while the traffic is still 6x
5. When traffic has been back near normal for a minute, VIGIL stops the replica (`scaled_in`)

Create the warm spare once (it stays stopped until VIGIL needs it):

```powershell
docker compose --profile replica create payment-replica
```

## Proving Ground: score VIGIL against every fault

One command runs a quiet period (false alarms?) and then every fault, one at a time:
slow inventory, memory leak, flash sale, bad deploy, runaway loop. For each it waits
for a calm system, injects the fault, watches VIGIL's own API, scores it against the
answer key and puts the world back. VIGIL never sees the answers.

```powershell
docker compose --profile prove build proving-ground      # once
docker compose --profile prove run --rm proving-ground   # about 20 minutes
```

- Runs at autonomy **L3** (so rollbacks need no click) and restores your level after.
- VIGIL's guardrails stay on: if the next fix would hit the cooldown or the action
  budget, the Proving Ground waits rather than scoring a block.
- A pass means: incident opened, right root service, right fix, and verified recovery
  on checkouts. For the flash sale, the right fix is adding a replica (and never a
  restart); the Proving Ground also records how long VIGIL took to scale back in.
- The scorecard also appears in the UI: **Proving Ground** (trophy icon) in the left bar.
- Results: `data\proving-ground\scorecard.md` (also `.json`), one file per run plus the latest.
- One scenario: `docker compose --profile prove run --rm proving-ground --only bad-deploy`

Needs payment v2 to exist: run `.\scripts\deploy.ps1 v2` then `.\scripts\deploy.ps1 v1` once first.

## Pay from your phone (demo checkout, no real money)

Open the **Phone checkout** page in the console (phone icon in the left rail) and scan the QR code.
The phone opens a small checkout (`/pay`). "Pay" sends a real order down the same path as every other
customer, storefront → payment → inventory, so when a fault is running the phone's payment fails too,
with the reason in plain words, and the strip at the bottom shows what VIGIL is doing about it. The
console shows each phone payment as a toast, plus the connected phones and their latest payments.
No money moves: the card charge is the demo payment service.

For a phone that is not on this laptop's Wi-Fi (or judges on a video call), start a public link:

```powershell
docker compose --profile public up -d cloudflared
```

It is a Cloudflare quick tunnel (no account) and the link appears on the Phone page by itself (or run
`docker compose --profile public logs cloudflared` and copy the `https://…trycloudflare.com` address).
The tunnel points at a separate gateway (port 8081 inside the console container) that serves **only**
the checkout page and its three routes: the console, Chaos Lab, the engine API and the operator
controls are not reachable through it. Stop it with `docker compose --profile public stop cloudflared`.

## Hosted replay: a link that works without the stack

Judges can't run Docker from a link, so the UI has a **replay build**: it plays back
the real runs the Proving Ground recorded (the snapshot the UI loaded, then every live
message at the pace it arrived). Nothing is generated; only the clock moves so the
recording happens "now". A banner says it's a replay, and Chaos Lab picks which
recorded fault to play. Operator controls and the shop say they need the live stack.

```powershell
docker compose --profile prove run --rm proving-ground   # records data\proving-ground\tapes
python scripts\pack_replay.py                            # copies them into ui\public\replay
```

Deploy `ui/` on Vercel (root directory `ui`; `vercel.json` sets `npm run build:replay`
and the SPA rewrite). Optional env var `VITE_REPO_URL` adds a "Run it live" button.

## You stay in control

```powershell
$h = @{ "X-Vigil-Token" = "<your VIGIL_ADMIN_TOKEN>" }

# Kill switch: VIGIL keeps detecting and diagnosing, but takes no action
Invoke-RestMethod -Method Post http://localhost:8080/api/control -Headers $h -ContentType "application/json" -Body '{"paused": true}'
Invoke-RestMethod -Method Post http://localhost:8080/api/control -Headers $h -ContentType "application/json" -Body '{"paused": false}'

# Autonomy: L1 = VIGIL proposes, you approve every fix
Invoke-RestMethod -Method Post http://localhost:8080/api/control -Headers $h -ContentType "application/json" -Body '{"autonomy": "L1"}'

# Approve or reject a waiting fix
Invoke-RestMethod -Method Post http://localhost:8080/api/incidents/VGL-0003/approve -Headers $h
Invoke-RestMethod -Method Post http://localhost:8080/api/incidents/VGL-0003/reject -Headers $h
```

| Guardrail | Rule |
| --- | --- |
| Who acts alone | Only with confidence High, and only low-risk actions at autonomy L2 |
| Approval | Confidence Medium or Low: a human must approve |
| Cooldown | The same fix on the same service at most once per 2 minutes |
| Attempts | At most 2 fixes per incident, then escalate |
| Budget | At most 3 actions per 10 minutes across all incidents |
| Blind | Stale telemetry: no actions |
| Hands | Only the actuator touches Docker: restart / start / stop, on `vigil.managed=true` containers, token required, private network, no shell. A rollback is start-the-old-version then stop-the-new one |
| Rollback | Only offered when the root changed version in the last 10 minutes; medium risk, so at L2 a human approves |

## How VIGIL decides what is actually broken

| Piece | What it does |
| --- | --- |
| Blame walk | Starts at the failing service nearest users, follows calls that fail into other failing services, stops at the one whose own dependencies are healthy |
| Category rules | Deterministic: crashed, memory leak, stuck process, load saturation, regression after a deploy, unknown |
| LLM second key | Reads the same evidence **without** the walk's answer; strict JSON; must cite real evidence IDs; can never name an action |
| Confidence | **High**: walk and LLM agree, category matches, all evidence types present. **Medium**: LLM down, or partial agreement. **Low**: disagreement |

Only High confidence will let VIGIL act alone (step 4). Check your Groq key works:

```powershell
docker compose exec vigil-engine python -m vigil.llm_check
```

## Engine tests

```powershell
cd engine
pip install -r requirements-dev.txt
pytest -q
```

103 tests, including the leak fixed before the crash, the flash-sale scale-out and scale-in, "real demand is never answered with a restart", the bad-deploy rollback (old version up before the bad one stops; restart never wakes a stopped version), the busy-laptop false alarm (now filtered), the operator lock, the full decide-act-verify loop, approvals, the kill switch, a fault
that survives a restart (escalates, no loop), actuator refusals and the policy math. Also 40 simulated clean half-hours with zero false incidents, detection in
exactly 3 ticks, leak prediction before the crash, telemetry outages, restart mid-incident,
the blame walk, every category rule, LLM hallucination/outage/retry handling, and the
confidence matrix.

## Layout

```
ui/            command center + demo shop (React, built into an nginx image that proxies every backend)
proving_ground/ test harness: injects every fault, scores VIGIL, writes the scorecard (not part of VIGIL)
scripts/       deploy.ps1: blue/green payment deploys
engine/        VIGIL itself: collect, detect, diagnose, decide, verify; event log, API + live stream
actuator/      VIGIL's only hands: typed container actions behind a token, on a private network
services/      payment, inventory and loadgen (one image, shared svc/common.py)
chaos/         chaos controller + Chaos Lab panel (VIGIL has no access)
prometheus/    scrape config (5 s)
data/          vigil/ = incidents DB + baselines; chaos/ = ground truth VIGIL can't see (gitignored)
```

## Ground rules baked into the design

- **No undo button.** VIGIL never calls a fault-reset endpoint. Faults are toggled
  through admin ports on the `chaos` network, which VIGIL is not on, behind a token
  VIGIL never gets. VIGIL's only hands are container actions through the actuator.
- **Faults live where real faults live.** Leaks and stuck workers sit in process memory,
  so a restart clears them. A bug shipped in a version (step 6) survives restarts;
  only a rollback fixes it.
- **Health checks stay green during faults.** `/health` is never faulted, on purpose.
- **Users are measured.** The load generator exports checkout success and latency,
  the signal VIGIL verifies recovery against.

## Metrics exported by every service

| Metric | Meaning |
| --- | --- |
| `http_requests_total{route,method,status}` | Traffic and errors |
| `http_request_duration_seconds` | Latency histogram (p95 via `histogram_quantile`) |
| `dependency_requests_total{target,outcome}` | Calls to other services: ok, error, timeout |
| `dependency_request_duration_seconds{target}` | Latency of those calls |
| `process_cpu_seconds_total`, `process_resident_memory_bytes` | Saturation |
| `build_info{version}` | Running version, so VIGIL can see deploys |
| `chaos_fault_active{fault}` | Ground truth only; VIGIL must never read it |
| `checkout_requests_total{outcome}` (storefront) | What users feel |
