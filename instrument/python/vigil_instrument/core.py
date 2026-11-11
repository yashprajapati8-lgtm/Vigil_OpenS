"""The metrics VIGIL reads, and nothing else.

VIGIL's collector (engine/vigil/collector.py) asks Prometheus for exactly these
series. Each is grouped by a `service` label that Prometheus adds when it scrapes
(prometheus.yml, one job per service). This package never sets `service` itself:
if an app exported it, Prometheus would rename the app's copy to
`exported_service` and VIGIL would see nothing.

    http_requests_total{route, method, status}            traffic and errors
    http_request_duration_seconds{route, method}          latency (histogram)
    dependency_requests_total{target, outcome}            calls to other services:
    dependency_request_duration_seconds{target}             outcome ok | error | timeout
    build_info{version} = 1                               which version is running
    process_cpu_seconds_total, process_resident_memory_bytes   (prometheus_client, Linux)
    <your customer metric>{outcome}                       what customers come to do

`route` is the route *template* ("/result/{roll}"), never the raw path, so one
route is one series however many students look up results. A request no route
matched is "unmatched"; VIGIL leaves those out (a scanner hitting /wp-admin is not
your app failing).
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from typing import Iterator

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    PlatformCollector,
    ProcessCollector,
    generate_latest,
)

LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
UNMATCHED = "unmatched"
DEFAULT_SKIP = ("/metrics", "/health", "/healthz", "/ready", "/readyz", "/livez")


def _is_timeout(exc: BaseException) -> bool:
    """httpx.TimeoutException, requests.Timeout, asyncio/builtin TimeoutError, ..."""
    if isinstance(exc, TimeoutError):
        return True
    return any("Timeout" in cls.__name__ for cls in type(exc).__mro__)


class Call:
    """One call to another service, used by Vigil.call(). Mark it failed when the
    other side answered but could not do the job (e.g. HTTP 5xx)."""

    def __init__(self) -> None:
        self.outcome = "ok"

    def failed(self) -> None:
        self.outcome = "error"

    def timed_out(self) -> None:
        self.outcome = "timeout"


class Attempt:
    """One customer attempt, used by CustomerAction.attempt(). Set `outcome` to any
    value listed in vigil.yaml (`customers.ok` / `customers.failed`). An outcome you
    set wins over an exception raised afterwards, so you can answer a student with
    HTTP 404 and still count the attempt as "not_found", not "failed"."""

    def __init__(self, default: str) -> None:
        self._outcome = default
        self.explicit = False

    @property
    def outcome(self) -> str:
        return self._outcome

    @outcome.setter
    def outcome(self, value: str) -> None:
        self._outcome = value
        self.explicit = True


class CustomerAction:
    """What customers come to do: one counter by outcome, plus an optional latency
    histogram. The names must match vigil.yaml `customers.metric` and
    `customers.duration_metric` (without `_bucket`)."""

    def __init__(self, metric: str, *, duration: str | None = None, outcome_label: str = "outcome",
                 outcomes: tuple[str, ...] = ("ok", "failed"), registry: CollectorRegistry = REGISTRY,
                 description: str = "Customer attempts by outcome"):
        name = metric[:-len("_total")] if metric.endswith("_total") else metric
        self.metric = metric
        self.counter = Counter(name, description, [outcome_label], registry=registry)
        # Outcomes exist from the start at 0, so "no failures yet" is 0%, not "no data".
        for o in outcomes:
            self.counter.labels(o)
        self.duration = (Histogram(duration, f"{description}: latency", buckets=LATENCY_BUCKETS, registry=registry)
                         if duration else None)

    def record(self, outcome: str, seconds: float | None = None) -> None:
        self.counter.labels(outcome).inc()
        if seconds is not None and self.duration is not None:
            self.duration.observe(seconds)

    def ok(self, seconds: float | None = None) -> None:
        self.record("ok", seconds)

    def failed(self, seconds: float | None = None) -> None:
        self.record("failed", seconds)

    @contextmanager
    def attempt(self, default: str = "ok") -> Iterator[Attempt]:
        """Time one attempt. An exception counts as "failed" (and is re-raised),
        unless the code already set an outcome."""
        a = Attempt(default)
        start = time.perf_counter()
        try:
            yield a
        except BaseException:
            if not a.explicit:
                a.outcome = "failed"
            raise
        finally:
            self.record(a.outcome, time.perf_counter() - start)


class Vigil:
    """Everything one service reports to VIGIL.

    version:  the running version ("v2", a git sha). Defaults to $APP_VERSION.
              To let VIGIL roll back, label the container vigil.version=<the same>.
    targets:  outbound calls to record, by host: {"auth": "auth", "pdf:8000": "pdf-render"}.
              Values are service names from vigil.yaml. Calls to other hosts
              (a payment gateway, an email API) are not recorded.
    skip:     paths that are not your app (health checks, /metrics).
    """

    def __init__(self, version: str | None = None, *, targets: dict[str, str] | None = None,
                 skip: tuple[str, ...] = DEFAULT_SKIP, registry: CollectorRegistry = REGISTRY):
        self.registry = registry
        self.version = version or os.getenv("APP_VERSION") or os.getenv("VIGIL_VERSION") or "unknown"
        self.targets = {k.lower(): v for k, v in (targets or {}).items()}
        self.skip = set(skip)
        self._customer_routes: dict[tuple[str, str], CustomerAction] = {}
        if registry is not REGISTRY:
            # The default registry already has these; a private one (tests) needs its own.
            ProcessCollector(registry=registry)
            PlatformCollector(registry=registry)
        self.requests = Counter("http_requests", "HTTP requests served", ["route", "method", "status"], registry=registry)
        self.latency = Histogram("http_request_duration_seconds", "HTTP request latency", ["route", "method"],
                                 buckets=LATENCY_BUCKETS, registry=registry)
        self.dep_requests = Counter("dependency_requests", "Calls this service made to another service",
                                    ["target", "outcome"], registry=registry)
        self.dep_latency = Histogram("dependency_request_duration_seconds", "Latency of calls to another service",
                                     ["target"], buckets=LATENCY_BUCKETS, registry=registry)
        Gauge("build_info", "Running version of this service", ["version"], registry=registry).labels(self.version).set(1)

    # ---- inbound

    def observe_request(self, route: str, method: str, status: int, seconds: float) -> None:
        """Record one served request. Framework adapters call this."""
        self.requests.labels(route, method, str(status)).inc()
        self.latency.labels(route, method).observe(seconds)
        action = self._customer_routes.get((method.upper(), route))
        if action is not None:
            # Server errors let the customer down; a 4xx is the customer's own request
            # (a wrong roll number), which vigil.yaml counts as neither unless listed.
            outcome = "ok" if status < 400 else "failed" if status >= 500 else "client_error"
            action.record(outcome, seconds)

    def customer(self, metric: str, *, duration: str | None = None, outcome_label: str = "outcome",
                 outcomes: tuple[str, ...] = ("ok", "failed")) -> CustomerAction:
        """The customer action counter (vigil.yaml `customers.metric`)."""
        return CustomerAction(metric, duration=duration, outcome_label=outcome_label, outcomes=outcomes,
                              registry=self.registry)

    def count_route(self, method: str, route: str, action: CustomerAction) -> None:
        """Count every request to this route as one customer attempt, by its status:
        2xx/3xx "ok", 5xx "failed", 4xx "client_error". `route` is the template as
        FastAPI writes it: "/result/{roll}"."""
        self._customer_routes[(method.upper(), route)] = action

    # ---- outbound

    def target_for(self, host: str | None, port: int | None = None) -> str | None:
        if not host:
            return None
        host = host.lower()
        if port is not None and f"{host}:{port}" in self.targets:
            return self.targets[f"{host}:{port}"]
        return self.targets.get(host)

    def observe_call(self, target: str, outcome: str, seconds: float) -> None:
        self.dep_requests.labels(target, outcome).inc()
        self.dep_latency.labels(target).observe(seconds)

    @contextmanager
    def call(self, target: str) -> Iterator[Call]:
        """Record a call to another service made with any client (gRPC, a queue, a
        database you listed in vigil.yaml). An exception is an error, or a timeout
        if it is one, and is re-raised."""
        c = Call()
        start = time.perf_counter()
        try:
            yield c
        except BaseException as exc:
            c.outcome = "timeout" if _is_timeout(exc) else "error"
            raise
        finally:
            self.observe_call(target, c.outcome, time.perf_counter() - start)

    # ---- /metrics

    def exposition(self) -> tuple[bytes, str]:
        return generate_latest(self.registry), CONTENT_TYPE_LATEST
