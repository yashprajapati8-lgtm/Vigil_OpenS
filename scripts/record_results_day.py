"""Record a real results-day run for the hosted replay.

Runs the Nexora results portal's rush for real and records what VIGIL's console
receives (the snapshot it loads, then every live message as it arrived), in the
same tape format the Proving Ground writes. pack_replay.py then adds it to the
replay build as the "Results day" chapter.

Before you start: the results site and VIGIL (deploy/) are running, normal-day
traffic is on, and VIGIL has finished learning normal (the console says 100%).

    python scripts/record_results_day.py
    python scripts/record_results_day.py --vigil http://localhost:3000 --site http://localhost:8000

It presses "Results declared" itself, waits for VIGIL to fix and verify it, then
goes back to a normal day and waits for VIGIL to stop the spare copy. About 5
minutes. Python 3.9+, standard library only. It only reads VIGIL's API.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "proving-ground" / "tapes"
TERMINAL = ("RESOLVED", "SELF_RESOLVED", "ESCALATED")


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), " ", msg, flush=True)


def get(url: str):
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def post(url: str):
    req = urllib.request.Request(url, data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


class Recorder:
    """Every live-stream message with its arrival time, while `on`."""

    def __init__(self, vigil: str):
        self.vigil = vigil
        self.on = False
        self.messages: list[dict] = []
        self.lock = threading.Lock()
        t = threading.Thread(target=self.listen, daemon=True)
        t.start()

    def listen(self) -> None:
        while True:
            try:
                with urllib.request.urlopen(self.vigil + "/api/stream", timeout=60) as r:
                    for raw in r:
                        line = raw.decode("utf-8").rstrip("\r\n")
                        if not self.on or not line.startswith("data: "):
                            continue
                        try:
                            msg = json.loads(line[6:])
                        except ValueError:
                            continue
                        with self.lock:
                            self.messages.append({"t": round(time.time(), 3), "m": msg})
            except Exception:
                time.sleep(2)

    def kinds(self) -> list[str]:
        with self.lock:
            out = []
            for item in self.messages:
                ev = item["m"].get("event") or {}
                out.append(ev.get("kind", ""))
            return out


def snapshot(vigil: str) -> dict:
    return {
        "state": get(vigil + "/api/state"),
        "history": get(vigil + "/api/history?minutes=10")["points"],
        "events": get(vigil + "/api/events?limit=200")["events"],
        "incidents": get(vigil + "/api/incidents?limit=40")["incidents"],
        "health": get(vigil + "/api/health"),
        "taken_at": round(time.time(), 3),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vigil", default="http://localhost:3000", help="VIGIL's console (it proxies /api)")
    ap.add_argument("--site", default="http://localhost:8000", help="the results portal")
    ap.add_argument("--service", default="results-api", help="the service the rush overloads")
    args = ap.parse_args()
    vigil, site = args.vigil.rstrip("/"), args.site.rstrip("/")

    state = get(vigil + "/api/state")
    for name, v in state.get("verdicts", {}).items():
        if v.get("state") == "learning":
            log(f"VIGIL is still learning normal for {name}. Wait until the console says 100%, then run this again.")
            return 1
    if state.get("open_incidents"):
        log("An incident is open. Wait until it is resolved (or use Fresh start), then run this again.")
        return 1

    traffic = get(site + "/demo/traffic")
    if traffic.get("mode") != "normal":
        log("Starting normal-day traffic and giving it a minute to settle")
        post(site + "/demo/traffic/normal")
        time.sleep(60)

    rec = Recorder(vigil)
    time.sleep(2)
    snap = snapshot(vigil)
    rec.on = True
    log("Recording. A few calm seconds first")
    time.sleep(8)

    t0 = time.time()
    post(site + "/demo/traffic/rush")
    log("Results declared: 60 students a second")

    inc = None
    deadline = t0 + 180
    while time.time() < deadline and inc is None:
        for i in get(vigil + "/api/incidents?limit=10")["incidents"]:
            if i["opened_at"] >= t0 - 5:
                inc = i
                break
        if inc is None:
            time.sleep(3)
    if inc is None:
        log("No incident within 3 minutes. Stopping the rush; nothing saved.")
        post(site + "/demo/traffic/normal")
        return 1
    log(f"{inc['id']} opened after {inc['opened_at'] - t0:.0f} s")

    end = time.time() + 300
    while time.time() < end:
        inc = get(vigil + f"/api/incidents/{inc['id']}")["incident"]
        if inc["status"] in TERMINAL:
            break
        time.sleep(3)
    log(f"{inc['id']} {inc['status']}")

    time.sleep(20)                      # students served by two copies, still under the rush
    post(site + "/demo/traffic/normal")
    log("Back to a normal day; waiting for VIGIL to stop the spare copy (about a minute of calm)")
    end = time.time() + 180
    while time.time() < end and "scaled_in" not in rec.kinds():
        time.sleep(3)
    log("Spare copy stopped" if "scaled_in" in rec.kinds() else "No scale-in within 3 minutes; saving anyway")
    time.sleep(10)                      # the calm after, so the replay ends settled
    rec.on = False

    detail = get(vigil + f"/api/incidents/{inc['id']}")
    d = inc.get("data", {})
    diag = d.get("diagnosis") or {}
    actions = []
    for a in d.get("actions", []):
        actions.append(f"{a['action']} {a['target']}")
    row = {
        "scenario": "results-day", "label": "Results day rush (60 students/s)", "incident": inc["id"],
        "detected": True, "mttd_s": round(inc["opened_at"] - t0, 1), "root": diag.get("root"),
        "category": diag.get("category"), "actions": actions, "status": inc["status"],
        "mttr_s": round(inc["closed_at"] - t0, 1) if inc["status"] == "RESOLVED" and inc.get("closed_at") else None,
        "expected": f"scale out {args.service}",
    }
    row["pass"] = (inc["status"] == "RESOLVED" and diag.get("root") == args.service
                   and len(actions) > 0 and actions[0] == f"scale_out {args.service}")

    with rec.lock:
        messages = list(rec.messages)
    tape = {"version": 1, "scenario": "results-day", "label": row["label"], "inject_ts": round(t0, 3),
            "end_ts": round(time.time(), 3), "snapshot": snap, "messages": messages, "detail": detail, "row": row}
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "results-day.json"
    path.write_text(json.dumps(tape, separators=(",", ":")))
    log(f"Saved {path} ({len(messages)} messages). {'PASS' if row['pass'] else 'Check it'}: "
        f"detect {row['mttd_s']} s, fix {', '.join(actions) or 'none'}, recovered {row['mttr_s']} s")
    log("Next: python scripts/pack_replay.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
