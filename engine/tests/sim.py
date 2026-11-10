"""Synthetic telemetry that mimics the demo services, for fast deterministic tests."""

from __future__ import annotations

import numpy as np

from vigil.collector import ServiceSample

NORMAL = {
    "payment": dict(p95=28.0, cpu=9.0, mem=58.0),
    "inventory": dict(p95=14.0, cpu=6.0, mem=52.0),
}


class ServiceSim:
    def __init__(self, service: str, seed: int = 0):
        self.service = service
        self.rng = np.random.default_rng(seed)
        self.base = NORMAL[service]
        self.mem = self.base["mem"]
        self.latency_add_ms = 0.0
        self.error_rate = 0.0
        self.leak_mb_s = 0.0
        self.cpu_burn = False
        self.load_x = 1.0
        self.down = False

    def sample(self, tick_s: float = 5.0) -> ServiceSample:
        r = self.rng
        if self.down:
            return ServiceSample(self.service, up=False)
        rps = max(r.poisson(20 * self.load_x) / 1.0, 0.0)
        p95 = self.base["p95"] * r.lognormal(0, 0.12) * (1 + 0.15 * (self.load_x - 1)) + self.latency_add_ms
        cpu = max(self.base["cpu"] * self.load_x + r.normal(0, 1.5) + (95 if self.cpu_burn else 0), 0.5)
        errors = r.binomial(int(rps * tick_s), min(self.error_rate + 0.0005, 1)) if rps else 0
        self.mem += self.leak_mb_s * tick_s
        slope = self.leak_mb_s + r.normal(0, 0.04)
        mem = self.mem + r.normal(0, 0.4)
        return ServiceSample(
            service=self.service,
            up=True,
            rps=rps,
            error_rate=errors / (rps * tick_s) if rps else None,
            p95_ms=p95,
            cpu_pct=cpu,
            mem_mb=mem,
            mem_slope_mb_s=slope,
            cpu_per_req=cpu / rps if rps else None,
            version="v1",
        )
