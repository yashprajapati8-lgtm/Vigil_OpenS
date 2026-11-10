"""Observe: turn Prometheus into one typed snapshot per tick.

VIGIL reads only telemetry. It never asks a service how it feels, and it never
reads the chaos ground-truth metric (chaos_fault_active).
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import asdict, dataclass, field

import httpx

from .config import Settings


@dataclass
class ServiceSample:
    service: str
    up: bool | None = None
    rps: float | None = None
    error_rate: float | None = None      # 0..1, None when there is no traffic
    p95_ms: float | None = None
    cpu_pct: float | None = None
    mem_mb: float | None = None
    mem_slope_mb_s: float | None = None
    cpu_per_req: float | None = None     # CPU % per request/s: load vs stuck process
    version: str | None = None
    instances: int | None = None         # how many containers serve this service right now


@dataclass
class EdgeSample:
    caller: str
    target: str
    rps: float | None = None
    fail_ratio: float | None = None      # errors + timeouts / calls
    p95_ms: float | None = None


@dataclass
class UserSample:
    checkout_rps: float | None = None
    fail_ratio: float | None = None
    p95_ms: float | None = None


@dataclass
class Snapshot:
    ts: float
    services: dict[str, ServiceSample] = field(default_factory=dict)
    edges: list[EdgeSample] = field(default_factory=list)
    user: UserSample = field(default_factory=UserSample)
    telemetry_ok: bool = True
    problem: str | None = None           # why telemetry is not ok

    def to_dict(self) -> dict:
        return asdict(self)


class TelemetryError(Exception):
    pass


def _num(value: str) -> float | None:
    out = float(value)
    return None if math.isnan(out) or math.isinf(out) else out


class PrometheusCollector:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.s = settings
        self._client = client or httpx.AsyncClient(
            base_url=settings.prometheus_url, timeout=settings.query_timeout_s
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _query(self, promql: str) -> list[dict]:
        try:
            resp = await self._client.get("/api/v1/query", params={"query": promql})
        except httpx.HTTPError as exc:
            raise TelemetryError(f"prometheus unreachable ({exc.__class__.__name__})") from exc
        if resp.status_code != 200:
            raise TelemetryError(f"prometheus answered {resp.status_code}")
        body = resp.json()
        if body.get("status") != "success":
            raise TelemetryError(f"query failed: {body.get('error', 'unknown')}")
        return body["data"]["result"]

    async def _by(self, promql: str, *labels: str) -> dict[tuple, float | None]:
        rows = await self._query(promql)
        return {tuple(r["metric"].get(l, "") for l in labels): _num(r["value"][1]) for r in rows}

    async def collect(self) -> Snapshot:
        w = self.s.rate_window
        biz = 'route!="unmatched"'
        q = {
            "rps": (f"sum by (service) (rate(http_requests_total{{{biz}}}[{w}]))", ("service",)),
            "err": (f'sum by (service) (rate(http_requests_total{{{biz},status=~"5.."}}[{w}]))', ("service",)),
            "p95": (f"histogram_quantile(0.95, sum by (service, le) (rate(http_request_duration_seconds_bucket{{{biz}}}[{w}])))", ("service",)),
            "cpu": (f"sum by (service) (rate(process_cpu_seconds_total[{w}]))", ("service",)),
            # Memory is per instance (max): each container has its own limit, and two
            # instances during a deploy or a scale-out are not one leaking process.
            "mem": ("max by (service) (process_resident_memory_bytes)", ("service",)),
            "slope": ("max by (service) (deriv(process_resident_memory_bytes[60s]))", ("service",)),
            "up": ("max by (service) (up)", ("service",)),
            "instances": ("sum by (service) (up)", ("service",)),
            "age": ("max by (service) (time() - timestamp(up))", ("service",)),
            "ver": ("max by (service, version) (build_info)", ("service", "version")),
            "edge_rps": (f"sum by (service, target) (rate(dependency_requests_total[{w}]))", ("service", "target")),
            "edge_fail": (f'sum by (service, target) (rate(dependency_requests_total{{outcome!="ok"}}[{w}]))', ("service", "target")),
            "edge_p95": (f"histogram_quantile(0.95, sum by (service, target, le) (rate(dependency_request_duration_seconds_bucket[{w}])))", ("service", "target")),
            "user": (f"sum by (outcome) (rate(checkout_requests_total[{w}]))", ("outcome",)),
            "user_p95": (f"histogram_quantile(0.95, sum by (le) (rate(checkout_duration_seconds_bucket[{w}])))", ()),
        }
        snap = Snapshot(ts=time.time())
        try:
            results = await asyncio.gather(*(self._by(expr, *labels) for expr, labels in q.values()))
        except TelemetryError as exc:
            snap.telemetry_ok, snap.problem = False, str(exc)
            return snap
        r = dict(zip(q.keys(), results))

        stale_after = self.s.stale_after_ticks * self.s.tick_seconds
        ages = [a for (svc,), a in r["age"].items() if svc in self.s.monitored and a is not None]
        if not ages:
            snap.telemetry_ok, snap.problem = False, "no scrape data for monitored services"
            return snap
        if max(ages) > stale_after:
            snap.telemetry_ok, snap.problem = False, f"telemetry stale ({max(ages):.0f}s old)"
            return snap

        for svc in self.s.monitored:
            k = (svc,)
            s = ServiceSample(service=svc)
            up = r["up"].get(k)
            s.up = None if up is None else up >= 1
            s.rps = r["rps"].get(k)
            if s.rps is not None and s.rps >= self.s.min_rps_for_rates:
                s.error_rate = (r["err"].get(k) or 0.0) / s.rps
            s.p95_ms = None if r["p95"].get(k) is None else r["p95"][k] * 1000
            cpu = r["cpu"].get(k)
            s.cpu_pct = None if cpu is None else cpu * 100
            mem = r["mem"].get(k)
            s.mem_mb = None if mem is None else mem / 2**20
            slope = r["slope"].get(k)
            s.mem_slope_mb_s = None if slope is None else slope / 2**20
            if s.cpu_pct is not None and s.rps and s.rps >= self.s.min_rps_for_rates:
                s.cpu_per_req = s.cpu_pct / s.rps
            inst = r["instances"].get(k)
            s.instances = None if inst is None else int(round(inst))
            versions = sorted(v for (sv, v), val in r["ver"].items() if sv == svc and val)
            s.version = ",".join(versions) or None
            snap.services[svc] = s

        for (caller, target), rps in r["edge_rps"].items():
            e = EdgeSample(caller=caller, target=target, rps=rps)
            if rps and rps >= self.s.min_rps_for_rates:
                e.fail_ratio = (r["edge_fail"].get((caller, target)) or 0.0) / rps
            p95 = r["edge_p95"].get((caller, target))
            e.p95_ms = None if p95 is None else p95 * 1000
            snap.edges.append(e)

        ok = r["user"].get(("ok",)) or 0.0
        # A checkout the shop could not even start (too many already waiting) failed too.
        failed = (r["user"].get(("failed",)) or 0.0) + (r["user"].get(("dropped",)) or 0.0)
        total = ok + failed
        snap.user.checkout_rps = total
        if total >= self.s.min_rps_for_rates:
            snap.user.fail_ratio = failed / total
        p95 = r["user_p95"].get(())
        snap.user.p95_ms = None if p95 is None else p95 * 1000
        return snap
