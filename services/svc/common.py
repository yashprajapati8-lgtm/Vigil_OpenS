"""Shared plumbing for the demo services: metrics, fault injection, chaos admin API.

Design rules (see docs/ARCHITECTURE.md):
- Every service exports the golden signals (latency, errors, traffic, saturation)
  plus per-dependency edge metrics, so VIGIL can tell a symptom from a cause.
- Faults live in process memory, exactly like real leaks or stuck workers do.
  A restart clears them; a bug baked into a version (APP_VERSION=v2) does not.
- The chaos admin API listens on a separate port, bound only to the service's
  address on the `chaos` network, and requires a token VIGIL never receives.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import os
import random
import socket
import threading
import time
from dataclasses import asdict, dataclass, field

import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from pydantic import BaseModel, Field

SERVICE = os.getenv("SERVICE_NAME", "unknown")
VERSION = os.getenv("APP_VERSION", "v1")
# A bug that ships with a version. Restarting does not remove it; rolling back does.
BUG_ERROR_RATE = float(os.getenv("BUG_ERROR_RATE", "0"))

ADMIN_PORT = int(os.getenv("ADMIN_PORT", "9000"))
ADMIN_ENABLED = os.getenv("ADMIN_ENABLED", "true").lower() == "true"
# Hostname of this container on the chaos network (a compose alias). The admin
# server binds only to that address, so other networks cannot reach it.
CHAOS_BIND_HOST = os.getenv("CHAOS_BIND_HOST", "")
CHAOS_TOKEN = os.getenv("CHAOS_TOKEN", "")

UNINSTRUMENTED = {"/metrics", "/health"}

logging.basicConfig(
    level=logging.INFO,
    format=f'{{"ts":"%(asctime)s","service":"{SERVICE}","level":"%(levelname)s","msg":"%(message)s"}}',
)
log = logging.getLogger(SERVICE)

# ---------------------------------------------------------------- metrics

LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

HTTP_REQUESTS = Counter(
    "http_requests_total", "HTTP requests served", ["route", "method", "status"]
)
HTTP_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["route", "method"],
    buckets=LATENCY_BUCKETS,
)
DEP_REQUESTS = Counter(
    "dependency_requests_total",
    "Calls this service made to another service",
    ["target", "outcome"],  # outcome: ok | error | timeout
)
DEP_DURATION = Histogram(
    "dependency_request_duration_seconds",
    "Latency of calls to another service",
    ["target"],
    buckets=LATENCY_BUCKETS,
)
BUILD_INFO = Gauge("build_info", "Running version of this service", ["version"])
BUILD_INFO.labels(version=VERSION).set(1)
FAULT_ACTIVE = Gauge(
    "chaos_fault_active", "1 while a chaos fault is active (ground truth)", ["fault"]
)

# ---------------------------------------------------------------- faults


class FaultUpdate(BaseModel):
    latency_ms: int | None = Field(None, ge=0, le=10_000)
    error_rate: float | None = Field(None, ge=0, le=1)
    leak_mb_per_s: float | None = Field(None, ge=0, le=50)
    cpu_burn: bool | None = None


@dataclass
class Faults:
    latency_ms: int = 0
    error_rate: float = 0.0
    leak_mb_per_s: float = 0.0
    cpu_burn: bool = False
    _leaked: list = field(default_factory=list, repr=False)

    def public(self) -> dict:
        data = asdict(self)
        data.pop("_leaked")
        data["leaked_mb"] = round(sum(len(b) for b in self._leaked) / 2**20, 1)
        return data

    def publish_gauges(self) -> None:
        FAULT_ACTIVE.labels("latency").set(1 if self.latency_ms else 0)
        FAULT_ACTIVE.labels("errors").set(1 if self.error_rate else 0)
        FAULT_ACTIVE.labels("memory_leak").set(1 if self.leak_mb_per_s else 0)
        FAULT_ACTIVE.labels("cpu_burn").set(1 if self.cpu_burn else 0)


FAULTS = Faults()
FAULTS.publish_gauges()


async def _leak_loop() -> None:
    """Grow memory while the leak fault is on. Bytes are touched so they are resident."""
    while True:
        await asyncio.sleep(1)
        if FAULTS.leak_mb_per_s > 0:
            FAULTS._leaked.append(bytearray(b"\x01") * int(FAULTS.leak_mb_per_s * 2**20))


def _cpu_burn_loop() -> None:
    """A runaway loop: burns a core and steals the GIL from request handlers."""
    while True:
        if FAULTS.cpu_burn:
            end = time.perf_counter() + 0.05
            while time.perf_counter() < end:
                pass
        else:
            time.sleep(0.2)


# ---------------------------------------------------------------- dependencies


class DependencyError(Exception):
    def __init__(self, target: str, outcome: str):
        super().__init__(f"{target} {outcome}")
        self.target = target
        self.outcome = outcome


class Dependency:
    """HTTP client for one downstream service that records edge metrics."""

    def __init__(self, target: str, base_url: str, timeout_s: float):
        self.target = target
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout_s)

    async def call(self, method: str, path: str, **kwargs) -> httpx.Response:
        start = time.perf_counter()
        try:
            resp = await self._client.request(method, path, **kwargs)
        except httpx.TimeoutException:
            DEP_REQUESTS.labels(self.target, "timeout").inc()
            raise DependencyError(self.target, "timeout")
        except httpx.HTTPError:
            DEP_REQUESTS.labels(self.target, "error").inc()
            raise DependencyError(self.target, "unreachable")
        finally:
            DEP_DURATION.labels(self.target).observe(time.perf_counter() - start)
        if resp.status_code >= 500:
            DEP_REQUESTS.labels(self.target, "error").inc()
            raise DependencyError(self.target, f"http_{resp.status_code}")
        DEP_REQUESTS.labels(self.target, "ok").inc()
        return resp

    async def close(self) -> None:
        await self._client.aclose()


# ---------------------------------------------------------------- chaos admin API


def require_token(token: str | None) -> None:
    if not CHAOS_TOKEN or not token or not hmac.compare_digest(token, CHAOS_TOKEN):
        raise HTTPException(status_code=403, detail="invalid chaos token")


def build_admin_app(extend=None) -> FastAPI:
    admin = FastAPI(title=f"{SERVICE} chaos admin", docs_url=None, redoc_url=None)

    @admin.get("/faults")
    def get_faults(x_chaos_token: str | None = Header(None)):
        require_token(x_chaos_token)
        return FAULTS.public()

    @admin.post("/faults")
    def set_faults(update: FaultUpdate, x_chaos_token: str | None = Header(None)):
        require_token(x_chaos_token)
        for key, value in update.model_dump(exclude_none=True).items():
            setattr(FAULTS, key, value)
        FAULTS.publish_gauges()
        log.warning("chaos faults set: %s", FAULTS.public())
        return FAULTS.public()

    @admin.post("/faults/reset")
    def reset_faults(x_chaos_token: str | None = Header(None)):
        require_token(x_chaos_token)
        FAULTS.latency_ms, FAULTS.error_rate = 0, 0.0
        FAULTS.leak_mb_per_s, FAULTS.cpu_burn = 0.0, False
        FAULTS._leaked.clear()
        FAULTS.publish_gauges()
        log.warning("chaos faults reset")
        return FAULTS.public()

    if extend:
        extend(admin)
    return admin


class _QuietServer(uvicorn.Server):
    """A second uvicorn server inside the app; the main server owns the signals."""

    def install_signal_handlers(self) -> None:  # uvicorn < 0.29
        pass

    @contextlib.contextmanager
    def capture_signals(self):  # uvicorn >= 0.29
        yield


def _admin_bind_host() -> str:
    if not CHAOS_BIND_HOST:
        return "127.0.0.1"  # local dev: loopback only
    try:
        return socket.gethostbyname(CHAOS_BIND_HOST)
    except OSError:
        log.error("cannot resolve %s; chaos admin stays on loopback", CHAOS_BIND_HOST)
        return "127.0.0.1"


# ---------------------------------------------------------------- app factory


def create_app(title: str, on_startup=None, on_shutdown=None, extend_admin=None) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks = [asyncio.create_task(_leak_loop())]
        threading.Thread(target=_cpu_burn_loop, daemon=True).start()
        admin_server = None
        if ADMIN_ENABLED:
            if not CHAOS_TOKEN:
                log.warning("CHAOS_TOKEN not set: chaos admin API rejects every call")
            host = _admin_bind_host()
            admin_server = _QuietServer(
                uvicorn.Config(build_admin_app(extend_admin), host=host, port=ADMIN_PORT, log_level="warning")
            )
            tasks.append(asyncio.create_task(admin_server.serve()))
            log.info("chaos admin on %s:%s", host, ADMIN_PORT)
        if on_startup:
            await on_startup()
        log.info("started version=%s", VERSION)
        yield
        if on_shutdown:
            await on_shutdown()
        if admin_server:
            admin_server.should_exit = True
        for task in tasks[:1]:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title=title, lifespan=lifespan)

    @app.middleware("http")
    async def instrument(request: Request, call_next):
        path = request.url.path
        if path in UNINSTRUMENTED:
            return await call_next(request)
        start = time.perf_counter()
        status = 500
        try:
            # Injected faults hit business routes only. /health stays green on
            # purpose: that is what a gray failure looks like.
            if FAULTS.latency_ms:
                await asyncio.sleep(FAULTS.latency_ms / 1000)
            if random.random() < FAULTS.error_rate + BUG_ERROR_RATE:
                response = JSONResponse({"detail": "internal error"}, status_code=500)
            else:
                response = await call_next(request)
            status = response.status_code
            return response
        finally:
            # A fault answered before routing ran, so the scope has no route yet.
            # Label it by its path when that is a real route: those failed
            # payments must count as payment errors, not vanish as "unmatched".
            route = request.scope.get("route")
            route_path = getattr(route, "path", None) or (
                path if any(getattr(r, "path", None) == path for r in request.app.router.routes) else "unmatched")
            HTTP_REQUESTS.labels(route_path, request.method, str(status)).inc()
            HTTP_DURATION.labels(route_path, request.method).observe(time.perf_counter() - start)

    @app.get("/health")
    def health():
        return {"status": "ok", "service": SERVICE, "version": VERSION}

    @app.get("/metrics")
    def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app
