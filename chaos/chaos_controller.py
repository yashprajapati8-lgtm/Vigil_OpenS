"""Chaos controller: breaks things on purpose. VIGIL cannot see or call this.

It lives only on the `chaos` network and talks to each service's token-protected
admin port. Every injection is written to a ground-truth log, so the Proving
Ground can later score VIGIL's diagnosis against what really happened.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

CHAOS_TOKEN = os.getenv("CHAOS_TOKEN", "")
GROUND_TRUTH = Path(os.getenv("GROUND_TRUTH_PATH", "/data/ground_truth.jsonl"))
BASE_RPS = float(os.getenv("BASE_RPS", "20"))

# Admin endpoints of each target, reachable only over the chaos network.
TARGETS = {
    "payment": os.getenv("PAYMENT_ADMIN_URL", "http://payment-v1-chaos:9000"),
    "inventory": os.getenv("INVENTORY_ADMIN_URL", "http://inventory-chaos:9000"),
    "loadgen": os.getenv("LOADGEN_ADMIN_URL", "http://loadgen-chaos:9000"),
}

# Each scenario: what it breaks, and the answer VIGIL should reach (ground truth).
SCENARIOS = {
    "slow-inventory": {
        "label": "Slow down inventory",
        "description": "Inventory requests stall for 1.5 s. Payment starts timing out on it.",
        "target": "inventory",
        "faults": {"latency_ms": 1500},
        "expected": {"root": "inventory", "category": "stuck_process", "fix": "restart inventory"},
    },
    "payment-leak": {
        "label": "Memory leak in payment",
        "description": "Payment leaks 3 MB/s and will hit its 256 MB limit in about a minute.",
        "target": "payment",
        "faults": {"leak_mb_per_s": 3},
        "expected": {"root": "payment", "category": "resource_leak", "fix": "restart payment"},
    },
    "payment-runaway": {
        "label": "Runaway loop in payment",
        "description": "A busy loop burns a core in payment. Traffic is flat, CPU per request jumps.",
        "target": "payment",
        "faults": {"cpu_burn": True},
        "expected": {"root": "payment", "category": "stuck_process", "fix": "restart payment"},
    },
    "flash-sale": {
        "label": "Flash sale (6x traffic)",
        "description": "Real demand: checkout traffic jumps 6x, more than one payment instance can serve. Nothing is broken.",
        "target": "loadgen",
        "rps_multiplier": float(os.getenv("FLASH_SALE_MULTIPLIER", "6")),
        "expected": {"root": "payment", "category": "load_saturation", "fix": "scale out payment"},
    },
}

logging.basicConfig(level=logging.INFO, format='{"ts":"%(asctime)s","service":"chaos","msg":"%(message)s"}')
log = logging.getLogger("chaos")

app = FastAPI(title="VIGIL chaos controller")
_client = httpx.AsyncClient(timeout=3.0, headers={"X-Chaos-Token": CHAOS_TOKEN})


def _record(event: dict) -> None:
    event = {"ts": time.time(), **event}
    try:
        GROUND_TRUTH.parent.mkdir(parents=True, exist_ok=True)
        with GROUND_TRUTH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")
    except OSError as exc:
        log.error("could not write ground truth: %s", exc)


async def _admin(target: str, method: str, path: str, body: dict | None = None) -> dict:
    try:
        resp = await _client.request(method, f"{TARGETS[target]}{path}", json=body)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"{target} admin unreachable: {exc.__class__.__name__}")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"{target} admin answered {resp.status_code}")
    return resp.json()


@app.get("/scenarios")
def list_scenarios():
    return {name: {k: v for k, v in s.items() if k != "expected"} for name, s in SCENARIOS.items()}


@app.post("/scenarios/{name}/inject")
async def inject(name: str):
    scenario = SCENARIOS.get(name)
    if not scenario:
        raise HTTPException(status_code=404, detail=f"unknown scenario {name}")
    target = scenario["target"]
    if "rps_multiplier" in scenario:
        result = await _admin(target, "POST", "/rps", {"rps": BASE_RPS * scenario["rps_multiplier"]})
    else:
        result = await _admin(target, "POST", "/faults", scenario["faults"])
    _record({"event": "inject", "scenario": name, "target": target, "expected": scenario["expected"]})
    log.info("injected %s on %s", name, target)
    return {"injected": name, "target": target, "state": result}


@app.post("/reset")
async def reset():
    """Clear every fault and restore normal traffic. For re-running the demo only."""
    results, errors = {}, {}
    for target in ("payment", "inventory"):
        try:
            results[target] = await _admin(target, "POST", "/faults/reset")
        except HTTPException as exc:
            errors[target] = exc.detail  # e.g. the container is down; VIGIL's problem now
    try:
        results["loadgen"] = await _admin("loadgen", "POST", "/rps", {"rps": BASE_RPS})
    except HTTPException as exc:
        errors["loadgen"] = exc.detail
    _record({"event": "reset", "errors": errors})
    return {"reset": results, "errors": errors}


@app.get("/status")
async def status():
    out = {}
    for target in ("payment", "inventory"):
        try:
            out[target] = await _admin(target, "GET", "/faults")
        except HTTPException as exc:
            out[target] = {"error": exc.detail}
    try:
        out["loadgen"] = await _admin("loadgen", "GET", "/rps")
    except HTTPException as exc:
        out["loadgen"] = {"error": exc.detail}
    return out


@app.get("/", response_class=HTMLResponse)
def panel():
    buttons = "".join(
        f'<button onclick="go(\'/scenarios/{n}/inject\')"><b>{s["label"]}</b><br><small>{s["description"]}</small></button>'
        for n, s in SCENARIOS.items()
    )
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>VIGIL Chaos Lab</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{{font-family:system-ui,sans-serif;background:#111;color:#eee;max-width:760px;margin:24px auto;padding:0 16px}}
button{{display:block;width:100%;text-align:left;margin:8px 0;padding:12px;background:#1d1d1d;color:#eee;border:1px solid #444;border-radius:8px;cursor:pointer}}
button:hover{{border-color:#e5484d}} small{{color:#aaa}} pre{{background:#1d1d1d;padding:12px;border-radius:8px;overflow:auto}}
.note{{color:#f5a524}}
</style></head><body>
<h1>Chaos Lab</h1><p class="note">Chaos controller: VIGIL has no access to this panel or its network.</p>
{buttons}
<button onclick="go('/reset')"><b>Reset everything</b><br><small>Clear all faults and restore normal traffic (demo re-runs only)</small></button>
<h3>Live fault state</h3><pre id="out">loading...</pre>
<script>
async function go(p){{const r=await fetch(p,{{method:'POST'}});document.getElementById('out').textContent=JSON.stringify(await r.json(),null,2);}}
async function poll(){{try{{const r=await fetch('/status');document.getElementById('out').textContent=JSON.stringify(await r.json(),null,2);}}catch(e){{}}}}
poll();setInterval(poll,3000);
</script></body></html>"""
