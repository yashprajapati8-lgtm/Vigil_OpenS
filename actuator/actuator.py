"""The actuator: VIGIL's only hands, and deliberately weak ones.

It is the one container that can reach the Docker socket. It exposes exactly
three typed actions (restart, start, stop), only on containers labeled
vigil.managed=true, only to a caller holding the token, only on the private
`control` network. There is no shell, no exec, no create, no delete, no image
pulls. Repeating an action ID returns the first result instead of acting twice.
"""

from __future__ import annotations

import hmac
import logging
import os
import threading
import time
from collections import OrderedDict

import docker
from docker.errors import APIError, NotFound
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

TOKEN = os.getenv("ACTUATOR_TOKEN", "")
ALLOWED_ACTIONS = {"restart", "start", "stop"}
MANAGED_LABEL = "vigil.managed"
STOP_TIMEOUT_S = int(os.getenv("ACTUATOR_STOP_TIMEOUT_S", "5"))

logging.basicConfig(level=logging.INFO, format='{"ts":"%(asctime)s","service":"actuator","msg":"%(message)s"}')
log = logging.getLogger("actuator")

app = FastAPI(title="VIGIL actuator", docs_url=None, redoc_url=None)
_client = None
_done: OrderedDict[str, dict] = OrderedDict()   # action_id -> result (idempotency)
_lock = threading.Lock()


def client():
    global _client
    if _client is None:
        _client = docker.from_env()
    return _client


def require_token(token: str | None) -> None:
    if not TOKEN or not token or not hmac.compare_digest(token, TOKEN):
        raise HTTPException(status_code=403, detail="invalid actuator token")


class ActionRequest(BaseModel):
    action_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    action: str
    container: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")


def _describe(c) -> dict:
    state = c.attrs.get("State", {})
    return {
        "name": c.name,
        "service": c.labels.get("vigil.service"),
        "version": c.labels.get("vigil.version"),
        "role": c.labels.get("vigil.role"),
        "status": c.status,
        "health": (state.get("Health") or {}).get("Status"),
        "started_at": state.get("StartedAt"),
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/containers")
def containers(x_actuator_token: str | None = Header(None)):
    require_token(x_actuator_token)
    found = client().containers.list(all=True, filters={"label": f"{MANAGED_LABEL}=true"})
    return {"containers": [_describe(c) for c in found]}


@app.post("/actions")
def act(req: ActionRequest, x_actuator_token: str | None = Header(None)):
    require_token(x_actuator_token)
    if req.action not in ALLOWED_ACTIONS:
        raise HTTPException(status_code=400, detail=f"action not allowed: {req.action}")
    with _lock:
        if req.action_id in _done:
            return {**_done[req.action_id], "replayed": True}
        try:
            c = client().containers.get(req.container)
        except NotFound:
            raise HTTPException(status_code=404, detail=f"no container {req.container}")
        if c.labels.get(MANAGED_LABEL) != "true":
            log.warning("refused %s on unmanaged container %s", req.action, req.container)
            raise HTTPException(status_code=403, detail=f"{req.container} is not managed by VIGIL")
        started = time.perf_counter()
        try:
            if req.action == "restart":
                c.restart(timeout=STOP_TIMEOUT_S)
            elif req.action == "start":
                c.start()
            else:
                c.stop(timeout=STOP_TIMEOUT_S)
        except APIError as exc:
            raise HTTPException(status_code=502, detail=f"docker refused: {exc.explanation or exc}")
        c.reload()
        result = {"action_id": req.action_id, "action": req.action, "container": req.container,
                  "status": "done", "duration_ms": round((time.perf_counter() - started) * 1000),
                  "container_status": c.status}
        _done[req.action_id] = result
        while len(_done) > 500:
            _done.popitem(last=False)
        log.info("%s %s in %d ms", req.action, req.container, result["duration_ms"])
        return result
