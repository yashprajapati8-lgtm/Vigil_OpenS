"""The collector against canned Prometheus answers: parsing, derived signals, failure modes."""

import asyncio
import json

import httpx

from vigil.collector import PrometheusCollector
from vigil.config import Settings


def vec(*rows):
    return {"status": "success", "data": {"resultType": "vector", "result": [
        {"metric": m, "value": [1792169600, str(v)]} for m, v in rows]}}


def answer(query: str):
    if "time() - timestamp(up)" in query:
        return vec(({"service": "payment"}, 2.1), ({"service": "inventory"}, 2.4))
    if query.startswith("max by (service) (up)"):
        return vec(({"service": "payment"}, 1), ({"service": "inventory"}, 1))
    if "build_info" in query:
        return vec(({"service": "payment", "version": "v1"}, 1), ({"service": "inventory", "version": "v1"}, 1))
    if "checkout_duration_seconds_bucket" in query:
        return vec(({}, 0.045))
    if "checkout_requests_total" in query:
        return vec(({"outcome": "ok"}, 15), ({"outcome": "failed"}, 5))
    if "dependency_request_duration_seconds_bucket" in query:
        return vec(({"service": "payment", "target": "inventory"}, 1.0))
    if "dependency_requests_total" in query and 'outcome!="ok"' in query:
        return vec(({"service": "payment", "target": "inventory"}, 4))
    if "dependency_requests_total" in query:
        return vec(({"service": "payment", "target": "inventory"}, 20))
    if "http_request_duration_seconds_bucket" in query:
        return vec(({"service": "payment"}, 0.03), ({"service": "inventory"}, "NaN"))
    if 'status=~"5.."' in query:
        return vec(({"service": "payment"}, 5))
    if "http_requests_total" in query:
        return vec(({"service": "payment"}, 20), ({"service": "inventory"}, 0.1))
    if "process_cpu_seconds_total" in query:
        return vec(({"service": "payment"}, 0.09), ({"service": "inventory"}, 0.05))
    if "deriv(" in query:
        return vec(({"service": "payment"}, 3 * 2**20), ({"service": "inventory"}, 0))
    if "process_resident_memory_bytes" in query:
        return vec(({"service": "payment"}, 60 * 2**20), ({"service": "inventory"}, 52 * 2**20))
    return vec()


def collect(handler):
    s = Settings()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://prom")
    return asyncio.run(PrometheusCollector(s, client).collect())


def test_parses_signals_and_derives_ratios():
    snap = collect(lambda req: httpx.Response(200, json=answer(req.url.params["query"])))
    assert snap.telemetry_ok
    pay, inv = snap.services["payment"], snap.services["inventory"]
    assert pay.error_rate == 0.25
    assert round(pay.p95_ms) == 30
    assert round(pay.cpu_pct) == 9 and round(pay.cpu_per_req, 2) == 0.45
    assert round(pay.mem_mb) == 60 and round(pay.mem_slope_mb_s) == 3
    assert pay.version == "v1" and pay.up is True
    assert inv.error_rate is None, "too little traffic: no data, not 0% errors"
    assert inv.p95_ms is None, "NaN quantile becomes no data"
    edge = snap.edges[0]
    assert (edge.caller, edge.target, edge.fail_ratio) == ("payment", "inventory", 0.2)
    assert snap.user.fail_ratio == 0.25 and round(snap.user.p95_ms) == 45


def test_prometheus_down_degrades_instead_of_crashing():
    def boom(req):
        raise httpx.ConnectError("refused")
    snap = collect(boom)
    assert not snap.telemetry_ok and "unreachable" in snap.problem


def test_stale_scrapes_are_refused():
    def stale(req):
        q = req.url.params["query"]
        if "time() - timestamp(up)" in q:
            return httpx.Response(200, json=vec(({"service": "payment"}, 60), ({"service": "inventory"}, 2)))
        return httpx.Response(200, json=answer(q))
    snap = collect(stale)
    assert not snap.telemetry_ok and "stale" in snap.problem


def test_bad_query_is_reported():
    snap = collect(lambda req: httpx.Response(400, json={"status": "error", "error": "parse error"}))
    assert not snap.telemetry_ok
