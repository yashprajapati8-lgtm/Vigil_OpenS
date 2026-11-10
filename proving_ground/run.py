"""Proving Ground: break the system on purpose, one fault at a time, and score VIGIL.

For each scenario it waits for a healthy system, injects the fault, then watches
VIGIL's own API: did it open an incident, blame the right service, name the
right cause, apply the right fix, and verify recovery? Times are measured from
the moment of injection. The answer key lives here and in the chaos controller;
VIGIL never sees it.

This is test tooling, not part of VIGIL. It holds the Docker socket only to ship
a bad deploy (start payment-v2, stop payment-v1), the way a CI pipeline would.

    docker compose --profile prove run --rm proving-ground
    docker compose --profile prove run --rm proving-ground --only slow-inventory
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import docker
import httpx

VIGIL = os.getenv("VIGIL_URL", "http://vigil-engine:8080")
CHAOS = os.getenv("CHAOS_URL", "http://chaos-controller:8090")
TOKEN = os.getenv("VIGIL_ADMIN_TOKEN", "")
OUT = Path(os.getenv("OUT_DIR", "/out"))

TERMINAL = {"RESOLVED", "SELF_RESOLVED", "ESCALATED"}

# The answer key. `action` is what a correct fix looks like; None means the right
# move is to do nothing (real demand is not a fault).
SCENARIOS = [
    {"name": "slow-inventory", "label": "Slow inventory (gray failure)", "kind": "chaos",
     "root": "inventory", "category": "stuck_process", "action": ("restart", "inventory")},
    {"name": "payment-leak", "label": "Memory leak in payment", "kind": "chaos",
     "root": "payment", "category": "resource_leak", "action": ("restart", "payment")},
    {"name": "flash-sale", "label": "Flash sale (real demand, 6x)", "kind": "chaos",
     "root": "payment", "category": "load_saturation", "action": ("scale_out", "payment"), "watch_s": 90,
     "never": {"restart", "rollback"}},
    {"name": "bad-deploy", "label": "Bad deploy (payment v2)", "kind": "deploy",
     "root": "payment", "category": "regression_after_change", "action": ("rollback", "payment")},
    {"name": "payment-runaway", "label": "Runaway loop in payment", "kind": "chaos",
     "root": "payment", "category": "stuck_process", "action": ("restart", "payment")},
]


def log(msg: str) -> None:
    print(f"{datetime.now():%H:%M:%S}  {msg}", flush=True)


class Recorder:
    """Records what VIGIL's UI receives during one scenario: the snapshot it loads,
    then every live-stream message with its arrival time. The UI's replay mode plays
    a tape back exactly as it happened, so a hosted link can show a real run."""

    def __init__(self):
        self.messages: list[dict] = []
        self.on = False
        self._lock = threading.Lock()
        threading.Thread(target=self._listen, daemon=True).start()

    def _listen(self) -> None:
        while True:
            try:
                with httpx.Client(timeout=httpx.Timeout(10, read=None)) as c:
                    with c.stream("GET", f"{VIGIL}/api/stream") as r:
                        for line in r.iter_lines():
                            if self.on and line.startswith("data: "):
                                try:
                                    msg = json.loads(line[6:])
                                except ValueError:
                                    continue
                                with self._lock:
                                    self.messages.append({"t": round(time.time(), 3), "m": msg})
            except httpx.HTTPError:
                time.sleep(2)

    def start(self, http: httpx.Client) -> dict:
        snapshot = {
            "state": http.get(f"{VIGIL}/api/state").json(),
            "history": http.get(f"{VIGIL}/api/history", params={"minutes": 10}).json()["points"],
            "events": http.get(f"{VIGIL}/api/events", params={"limit": 200}).json()["events"],
            "incidents": http.get(f"{VIGIL}/api/incidents", params={"limit": 40}).json()["incidents"],
            "health": http.get(f"{VIGIL}/api/health").json(),
            "taken_at": round(time.time(), 3),
        }
        with self._lock:
            self.messages = []
        self.on = True
        return snapshot

    def stop(self) -> list[dict]:
        self.on = False
        with self._lock:
            return list(self.messages)


def save_tape(sc: dict, snapshot: dict, messages: list[dict], t0: float, inc_id: str | None, http: httpx.Client, row: dict) -> None:
    detail = None
    if inc_id:
        try:
            detail = http.get(f"{VIGIL}/api/incidents/{inc_id}").json()
        except (httpx.HTTPError, ValueError):
            detail = None
    tape = {"version": 1, "scenario": sc["name"], "label": sc["label"], "inject_ts": round(t0, 3),
            "end_ts": round(time.time(), 3), "snapshot": snapshot, "messages": messages,
            "detail": detail, "row": row}
    folder = OUT / "tapes"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{sc['name']}.json").write_text(json.dumps(tape, separators=(",", ":")))
    log(f"  recorded replay tape: {len(messages)} messages, {(tape['end_ts'] - t0):.0f}s")


class Lab:
    def __init__(self):
        self.http = httpx.Client(timeout=15)
        self.docker = docker.from_env()
        self.actions: list[tuple[float, str, str]] = []   # VIGIL's actions we observed: (ts, action, target)

    # ---- VIGIL (read-only, plus the documented control endpoint)

    def state(self) -> dict:
        return self.http.get(f"{VIGIL}/api/state").json()

    def control(self) -> dict:
        return self.http.get(f"{VIGIL}/api/control").json()

    def set_autonomy(self, level: str) -> None:
        r = self.http.post(f"{VIGIL}/api/control", json={"autonomy": level}, headers={"X-Vigil-Token": TOKEN})
        r.raise_for_status()

    def incidents_since(self, ts: float) -> list[dict]:
        rows = self.http.get(f"{VIGIL}/api/incidents", params={"limit": 50}).json()["incidents"]
        return sorted((i for i in rows if i["opened_at"] >= ts - 2), key=lambda i: i["opened_at"])

    def incident(self, inc_id: str) -> dict:
        return self.http.get(f"{VIGIL}/api/incidents/{inc_id}").json()["incident"]

    # ---- the world

    def chaos_inject(self, name: str) -> None:
        self.http.post(f"{CHAOS}/scenarios/{name}/inject").raise_for_status()

    def chaos_reset(self) -> None:
        try:
            self.http.post(f"{CHAOS}/reset")
        except httpx.HTTPError as exc:
            log(f"  chaos reset failed ({exc}); continuing")

    def _container(self, name: str):
        try:
            return self.docker.containers.get(name)
        except docker.errors.NotFound:
            return None

    def _wait_healthy(self, name: str, timeout_s: float = 60) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            c = self._container(name)
            if c is not None:
                c.reload()
                health = (c.attrs.get("State", {}).get("Health") or {}).get("Status")
                if c.status == "running" and health in (None, "healthy"):
                    return True
            time.sleep(2)
        return False

    def switch_payment(self, to: str) -> None:
        """Blue/green, exactly like scripts/deploy.ps1."""
        new, old = f"payment-{to}", ("payment-v1" if to == "v2" else "payment-v2")
        c = self._container(new)
        if c is None:
            raise RuntimeError(f"{new} does not exist yet. Run .\\scripts\\deploy.ps1 v2 once, then .\\scripts\\deploy.ps1 v1")
        c.reload()
        if c.status != "running":
            c.start()
        if not self._wait_healthy(new):
            raise RuntimeError(f"{new} did not become healthy")
        o = self._container(old)
        if o is not None:
            o.reload()
            if o.status == "running":
                o.stop(timeout=5)

    def restore(self) -> None:
        """Back to a clean world, whatever VIGIL did or did not do."""
        self.chaos_reset()
        v1 = self._container("payment-v1")
        if v1 is not None:
            v1.reload()
            v2 = self._container("payment-v2")
            v2_running = v2 is not None and (v2.reload() or v2.status == "running")
            if v1.status != "running" or v2_running:
                log("  restoring payment v1")
                self.switch_payment("v1")
        inv = self._container("inventory")
        if inv is not None:
            inv.reload()
            if inv.status != "running":
                inv.start()

    def replica_running(self) -> bool:
        c = self._container("payment-replica")
        if c is None:
            return False
        c.reload()
        return c.status == "running"

    def wait_scaled_in(self, timeout_s: float = 240) -> float | None:
        """VIGIL should stop the spare by itself once traffic is normal again."""
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            if not self.replica_running():
                return time.time() - t0
            time.sleep(5)
        return None

    def wait_calm(self, timeout_s: float = 300) -> bool:
        """Healthy services, no open incident, checkouts fine, for 3 checks in a row."""
        deadline, calm = time.time() + timeout_s, 0
        while time.time() < deadline:
            try:
                s = self.state()
                verdicts = s.get("verdicts", {})
                fail = ((s.get("latest") or {}).get("user") or {}).get("fail_ratio")
                ok = (verdicts and all(v.get("state") == "healthy" for v in verdicts.values())
                      and not s.get("open_incidents") and fail is not None and fail < 0.02)
            except (httpx.HTTPError, ValueError):
                ok = False
            calm = calm + 1 if ok else 0
            if calm >= 3:
                return True
            time.sleep(5)
        return False

    def wait_guardrails(self, action: tuple[str, str] | None) -> None:
        """VIGIL's guardrails are real: if the next scenario's fix would be blocked by
        the cooldown or the action budget, wait them out rather than score a block."""
        if action is None:
            return
        g = self.control().get("guardrails", {})
        cooldown, window, budget = g.get("cooldown_s", 120), g.get("budget_window_s", 600), g.get("budget_actions", 3)
        while True:
            now = time.time()
            recent = [a for a in self.actions if now - a[0] < window]
            same = [a for a in recent if (a[1], a[2]) == action and now - a[0] < cooldown]
            wait = 0.0
            if same:
                wait = max(wait, cooldown - (now - same[-1][0]) + 5)
            if len(recent) >= budget:
                wait = max(wait, window - (now - recent[0][0]) + 5)
            if wait <= 0:
                return
            log(f"  waiting {wait:.0f}s so VIGIL's guardrails (cooldown/budget) allow the next fix")
            time.sleep(min(wait, 30))


def score(sc: dict, inc: dict | None, t0: float, actions: list[dict]) -> dict:
    taken = [(a["action"], a["target"]) for a in actions]
    row = {"scenario": sc["name"], "label": sc["label"], "incident": inc["id"] if inc else None,
           "detected": inc is not None, "mttd_s": None, "root": None, "root_ok": None,
           "category": None, "category_ok": None, "actions": [f"{a} {t}" for a, t in taken],
           "action_ok": None, "status": inc["status"] if inc else None, "mttr_s": None,
           "checkouts_lost": None, "confidence": None}
    if sc.get("never") and inc is None:
        # Real demand that one instance happened to absorb: nothing to fix, nothing touched.
        row["action_ok"] = not taken
        row["pass"] = row["action_ok"]
        row["expected"] = f"{sc['action'][0].replace('_', ' ')} {sc['action'][1]} if users suffer; never restart"
        return row
    if sc["action"] is None:   # the right move is no fix at all
        row["action_ok"] = not taken
        row["pass"] = row["action_ok"]
        row["expected"] = "no fix (real demand)"
        if inc:
            row["mttd_s"] = round(inc["opened_at"] - t0, 1)
        return row
    row["expected"] = f"{sc['action'][0].replace('_', ' ')} {sc['action'][1]}"
    if inc:
        d = inc.get("data", {})
        diag = d.get("diagnosis") or {}
        row["mttd_s"] = round(inc["opened_at"] - t0, 1)
        row["root"], row["category"] = diag.get("root"), diag.get("category")
        row["confidence"] = diag.get("confidence")
        row["root_ok"] = row["root"] == sc["root"]
        row["category_ok"] = row["category"] == sc["category"]
        row["action_ok"] = bool(taken) and taken[0] == sc["action"]
        if sc.get("never") and any(a in sc["never"] for a, _ in taken):
            row["action_ok"] = False
        if inc["status"] == "RESOLVED" and inc.get("closed_at"):
            row["mttr_s"] = round(inc["closed_at"] - t0, 1)
        row["checkouts_lost"] = int(round((d.get("metrics") or {}).get("failed_checkouts", d.get("failed_checkouts", 0)) or 0))
    row["pass"] = bool(row["detected"] and row["root_ok"] and row["action_ok"] and row["status"] == "RESOLVED")
    return row


def run_scenario(lab: Lab, sc: dict, incident_timeout_s: float, resolve_timeout_s: float,
                 rec: "Recorder | None" = None) -> dict:
    log(f"== {sc['label']}")
    lab.wait_guardrails(sc["action"])
    if not lab.wait_calm():
        log("  system never calmed down; scoring as not run")
        return {"scenario": sc["name"], "label": sc["label"], "pass": False, "error": "system not calm before the run"}
    snapshot = rec.start(lab.http) if rec else None
    time.sleep(8 if rec else 0)          # a few calm seconds at the start of the replay
    t0 = time.time()
    try:
        if sc["kind"] == "deploy":
            log("  shipping payment v2 (blue/green)")
            lab.switch_payment("v2")
        else:
            lab.chaos_inject(sc["name"])
        log("  injected; watching VIGIL")

        watch = sc.get("watch_s")
        inc = None
        deadline = t0 + (watch or incident_timeout_s)
        while time.time() < deadline and inc is None:
            found = lab.incidents_since(t0)
            inc = found[0] if found else None
            if inc is None:
                time.sleep(3)
        if inc is None and watch:
            log(f"  no incident in {watch}s (correct for real demand)")
        elif inc is None:
            log(f"  no incident within {incident_timeout_s:.0f}s")
        else:
            log(f"  {inc['id']} opened after {inc['opened_at'] - t0:.0f}s")
            end = time.time() + resolve_timeout_s
            while time.time() < end:
                inc = lab.incident(inc["id"])
                if inc["status"] in TERMINAL:
                    break
                time.sleep(3)
            log(f"  {inc['id']} {inc['status']}")
        actions = (inc or {}).get("data", {}).get("actions", []) if inc else []
        for a in actions:
            lab.actions.append((a.get("started_at", time.time()), a["action"], a["target"]))
        row = score(sc, inc, t0, actions)
    finally:
        lab.restore()
    if lab.replica_running():
        log("  traffic is normal again; waiting for VIGIL to scale payment back in")
        took = lab.wait_scaled_in()
        row["scaled_in_after_s"] = None if took is None else round(took, 1)
        if took is None:
            log("  VIGIL did not scale in; stopping the replica")
            c = lab._container("payment-replica")
            c and c.stop(timeout=5)
        else:
            log(f"  VIGIL scaled payment back in {took:.0f}s after traffic dropped")
    if rec and snapshot:
        time.sleep(10)                    # the calm after, so the replay ends settled
        save_tape(sc, snapshot, rec.stop(), t0, row.get("incident"), lab.http, row)
    verdict = "PASS" if row["pass"] else "FAIL"
    log(f"  {verdict}: detect {row.get('mttd_s')}s, root {row.get('root')}, fix {', '.join(row.get('actions') or []) or 'none'}, "
        f"recovered {row.get('mttr_s')}s")
    return row


def quiet_period(lab: Lab, seconds: int) -> dict:
    log(f"== Quiet period: {seconds}s of normal traffic, no faults")
    lab.wait_calm()
    t0 = time.time()
    time.sleep(seconds)
    found = lab.incidents_since(t0)
    log(f"  {'PASS' if not found else 'FAIL'}: {len(found)} incident(s) with nothing broken")
    return {"scenario": "quiet", "label": f"Quiet period ({seconds}s, nothing broken)", "expected": "no incident",
            "false_alarms": len(found), "incidents": [i["id"] for i in found], "pass": not found}


def fmt(v, unit="") -> str:
    return "–" if v is None else f"{v}{unit}"


def write_scorecard(rows: list[dict], started: float, autonomy: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    faults = [r for r in rows if r["scenario"] not in ("quiet",)]
    mttd = [r["mttd_s"] for r in faults if r.get("mttd_s") is not None and r.get("detected") and r.get("expected") != "no fix (real demand)"]
    mttr = [r["mttr_s"] for r in faults if r.get("mttr_s") is not None]
    summary = {
        "started_at": started, "finished_at": time.time(), "autonomy_during_run": autonomy,
        "passed": sum(1 for r in rows if r.get("pass")), "total": len(rows),
        "median_detect_s": round(statistics.median(mttd), 1) if mttd else None,
        "median_recover_s": round(statistics.median(mttr), 1) if mttr else None,
        "false_alarms": sum(r.get("false_alarms", 0) for r in rows),
    }
    data = {"summary": summary, "rows": rows}
    stamp = datetime.fromtimestamp(started).strftime("%Y%m%d-%H%M%S")
    for name in (f"scorecard-{stamp}.json", "scorecard.json"):
        (OUT / name).write_text(json.dumps(data, indent=2))

    lines = [f"# VIGIL Proving Ground: {summary['passed']}/{summary['total']} passed", "",
             f"Run {datetime.fromtimestamp(started):%Y-%m-%d %H:%M} (container time, UTC), autonomy {autonomy}. "
             "Times are from the moment the fault was injected.", "",
             f"- Median time to detect: **{fmt(summary['median_detect_s'], ' s')}**",
             f"- Median time to verified recovery: **{fmt(summary['median_recover_s'], ' s')}**",
             f"- False alarms while nothing was broken: **{summary['false_alarms']}**", "",
             "| Scenario | Expected fix | VIGIL's root | VIGIL's fix | Detect | Recovered | Checkouts lost | Result |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in rows:
        lines.append("| {label} | {exp} | {root} | {fix} | {d} | {m} | {lost} | {res} |".format(
            label=r["label"], exp=r.get("expected", "–"),
            root=fmt(r.get("root")) + (f" ({r['category']})" if r.get("category") else ""),
            fix=", ".join(r.get("actions") or []) or ("none" if r["scenario"] != "quiet" else "–"),
            d=fmt(r.get("mttd_s"), " s"), m=fmt(r.get("mttr_s"), " s"), lost=fmt(r.get("checkouts_lost")),
            res="PASS" if r.get("pass") else "FAIL"))
    md = "\n".join(lines) + "\n"
    for name in (f"scorecard-{stamp}.md", "scorecard.md"):
        (OUT / name).write_text(md)
    print("\n" + md)


def main() -> int:
    ap = argparse.ArgumentParser(description="Score VIGIL against faults it has never seen the answers to.")
    ap.add_argument("--only", nargs="*", help="run only these scenarios")
    ap.add_argument("--quiet", type=int, default=180, help="seconds of no-fault traffic to check for false alarms (0 to skip)")
    ap.add_argument("--autonomy", default="L3", help="autonomy during the run (L3: VIGIL applies every allowed fix alone)")
    ap.add_argument("--incident-timeout", type=float, default=120)
    ap.add_argument("--resolve-timeout", type=float, default=240)
    ap.add_argument("--no-record", action="store_true", help="don't record replay tapes")
    args = ap.parse_args()
    if not TOKEN:
        print("VIGIL_ADMIN_TOKEN is not set (it comes from your .env).", file=sys.stderr)
        return 2

    lab = Lab()
    rec = None if args.no_record else Recorder()
    try:
        before = lab.control()
    except httpx.HTTPError as exc:
        print(f"Cannot reach VIGIL at {VIGIL}: {exc}", file=sys.stderr)
        return 2
    if before.get("paused"):
        print("VIGIL's actions are paused (kill switch). Arm them first.", file=sys.stderr)
        return 2

    chosen = [s for s in SCENARIOS if not args.only or s["name"] in args.only]
    started, rows = time.time(), []
    lab.set_autonomy(args.autonomy)
    log(f"Proving Ground: {len(chosen)} scenario(s), autonomy {args.autonomy} for the run (was {before.get('autonomy')})")
    try:
        lab.restore()
        if lab.replica_running():          # start from one payment instance, like production at rest
            log("stopping a payment replica left running from earlier")
            c = lab._container("payment-replica")
            c and c.stop(timeout=5)
        if args.quiet and not args.only:
            rows.append(quiet_period(lab, args.quiet))
        for sc in chosen:
            try:
                rows.append(run_scenario(lab, sc, args.incident_timeout, args.resolve_timeout, rec))
            except Exception as exc:  # one broken scenario must not lose the others
                log(f"  error: {exc}")
                rows.append({"scenario": sc["name"], "label": sc["label"], "pass": False, "error": str(exc)})
                lab.restore()
    finally:
        try:
            lab.set_autonomy(before.get("autonomy", "L2"))
            log(f"autonomy restored to {before.get('autonomy')}")
        except httpx.HTTPError:
            log("could not restore autonomy; set it in the UI")
        write_scorecard(rows, started, args.autonomy)
    return 0 if all(r.get("pass") for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
