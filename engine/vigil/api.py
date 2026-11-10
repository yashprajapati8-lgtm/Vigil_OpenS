"""HTTP API and live stream for the command center."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import os

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .bus import Bus
from .collector import PrometheusCollector
from .config import SETTINGS
from .engine import Engine
from .policy import AUTONOMY
from .store import Store

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format='{"ts":"%(asctime)s","logger":"%(name)s","level":"%(levelname)s","msg":"%(message)s"}',
)
log = logging.getLogger("vigil.api")

ADMIN_TOKEN = os.getenv("VIGIL_ADMIN_TOKEN", "")
UI_ORIGINS = [o for o in os.getenv("VIGIL_UI_ORIGINS", "http://localhost:5173").split(",") if o]

bus = Bus()
store: Store | None = None
engine: Engine | None = None


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    global engine, store
    bus.bind(asyncio.get_running_loop())
    store = Store(SETTINGS.db_path)
    collector = PrometheusCollector(SETTINGS)
    engine = Engine(SETTINGS, collector, store, bus)
    task = asyncio.create_task(engine.run())
    log.info("VIGIL engine started; watching %s via %s; LLM second key %s", ",".join(SETTINGS.monitored),
             SETTINGS.prometheus_url, "on" if engine.llm.enabled else "off (no GROQ_API_KEY)")
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await collector.close()
    await engine.llm.close()
    await engine.remediator.actuator.close()
    store.close()


app = FastAPI(title="VIGIL engine", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=UI_ORIGINS, allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-Vigil-Token"],
)


def require_admin(token: str | None) -> None:
    if not ADMIN_TOKEN or not token or not hmac.compare_digest(token, ADMIN_TOKEN):
        raise HTTPException(status_code=403, detail="admin token required")


def _engine() -> Engine:
    if engine is None:
        raise HTTPException(status_code=503, detail="engine starting")
    return engine


def _store() -> Store:
    if store is None:
        raise HTTPException(status_code=503, detail="engine starting")
    return store


@app.get("/api/health")
def health():
    return _engine().health()


@app.get("/api/state")
def state():
    e = _engine()
    last = e.history[-1] if e.history else None
    return {
        "health": e.health(),
        "latest": last,
        "verdicts": e.verdicts_view(),
        "open_incidents": list(e.incidents.open.values()),
    }


@app.get("/api/history")
def history(minutes: int = Query(10, ge=1, le=60)):
    e = _engine()
    points = int(minutes * 60 / SETTINGS.tick_seconds)
    return {"points": list(e.history)[-points:]}


@app.get("/api/incidents")
def incidents(limit: int = Query(50, ge=1, le=500)):
    return {"incidents": _store().list_incidents(limit)}


@app.get("/api/incidents/{incident_id}")
def incident(incident_id: str):
    inc = _store().get_incident(incident_id)
    if not inc:
        raise HTTPException(status_code=404, detail="no such incident")
    return {"incident": inc, "events": _store().events(incident_id=incident_id)}


@app.get("/api/events")
def events(since_id: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
    return {"events": _store().events(since_id=since_id, limit=limit)}


@app.get("/api/stream")
async def stream(request: Request):
    """SSE. On reconnect the UI refetches /api/state, so dropped messages self-heal."""
    q = bus.subscribe()

    async def gen():
        try:
            yield "retry: 3000\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {data}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/baseline/reset")
def reset_baseline(service: str | None = None, x_vigil_token: str | None = Header(None)):
    """Forget what normal looks like and relearn it. Only on a clean system."""
    require_admin(x_vigil_token)
    e = _engine()
    targets = [service] if service else list(e.detectors)
    unknown = [t for t in targets if t not in e.detectors]
    if unknown:
        raise HTTPException(status_code=404, detail=f"unknown service {unknown[0]}")
    if any(e.incidents.incident_of(t) for t in targets):
        raise HTTPException(status_code=409, detail="cannot relearn during an open incident")
    with e.lock:  # never mid-tick
        for t in targets:
            e.detectors[t].reset()
    ev = _store().append_event(stage="system", actor="human", kind="baseline_reset",
                            summary=f"Baseline reset for {', '.join(targets)}; relearning normal",
                            payload={"services": targets})
    bus.publish({"type": "event", "event": ev})
    return {"relearning": targets}


@app.post("/api/fresh-start")
def fresh_start(x_vigil_token: str | None = Header(None)):
    """Close any open incident and relearn normal from scratch. Clear chaos faults first."""
    require_admin(x_vigil_token)
    return _engine().fresh_start()


# ---------------------------------------------------------------- human control


@app.post("/api/operator/check")
def operator_check(x_vigil_token: str | None = Header(None)):
    """Lets the UI confirm a typed admin token before it is used for a real action."""
    require_admin(x_vigil_token)
    return {"ok": True}


class ControlUpdate(BaseModel):
    autonomy: str | None = None
    paused: bool | None = None


@app.get("/api/control")
def get_control():
    return {**_engine().control(), "levels": AUTONOMY}


@app.post("/api/control")
def set_control(update: ControlUpdate, x_vigil_token: str | None = Header(None)):
    """Kill switch and autonomy level. Takes effect on the next decision."""
    require_admin(x_vigil_token)
    e = _engine()
    if update.autonomy is not None and update.autonomy not in AUTONOMY:
        raise HTTPException(status_code=400, detail=f"autonomy must be one of {list(AUTONOMY)}")
    with e.lock:
        changes = []
        if update.autonomy is not None and update.autonomy != e.governor.autonomy:
            e.governor.autonomy = update.autonomy
            changes.append(f"autonomy {update.autonomy} ({AUTONOMY[update.autonomy]})")
        if update.paused is not None and update.paused != e.governor.paused:
            e.governor.paused = update.paused
            changes.append("kill switch ON: no actions will run" if update.paused else "kill switch OFF: actions allowed")
        if changes:
            ev = _store().append_event(stage="system", actor="human", kind="control_changed",
                                       summary="Control: " + "; ".join(changes), payload=e.control())
            bus.publish({"type": "event", "event": ev})
    return e.control()


@app.post("/api/incidents/{incident_id}/approve")
async def approve(incident_id: str, x_vigil_token: str | None = Header(None)):
    require_admin(x_vigil_token)
    try:
        inc = await _engine().remediator.approve(incident_id, who="operator")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"incident": inc}


@app.post("/api/incidents/{incident_id}/reject")
def reject(incident_id: str, x_vigil_token: str | None = Header(None)):
    require_admin(x_vigil_token)
    try:
        inc = _engine().remediator.reject(incident_id, who="operator")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"incident": inc}
