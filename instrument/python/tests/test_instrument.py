"""vigil-instrument emits exactly what VIGIL's collector queries, from real apps."""

import asyncio

import httpx
import pytest
import requests
from prometheus_client import CollectorRegistry
from prometheus_client.parser import text_string_to_metric_families

from vigil_instrument import AsyncVigilTransport, Vigil, VigilTransport, instrument_fastapi, requests_adapter


def samples(text: str) -> dict[tuple, float]:
    """{(sample name, sorted labels): value} from a /metrics body."""
    out = {}
    for fam in text_string_to_metric_families(text):
        for s in fam.samples:
            out[(s.name, tuple(sorted(s.labels.items())))] = s.value
    return out


def value(text: str, name: str, **labels) -> float | None:
    return samples(text).get((name, tuple(sorted(labels.items()))))


def fresh(**kw) -> Vigil:
    return Vigil(registry=CollectorRegistry(), **kw)


# ---- FastAPI

def fastapi_app(v: Vigil):
    from fastapi import FastAPI, HTTPException

    app = FastAPI()
    instrument_fastapi(app, v)

    @app.get("/result/{roll}")
    def result(roll: str):
        if roll == "missing":
            raise HTTPException(404)
        if roll == "broken":
            raise HTTPException(503)
        return {"roll": roll, "marks": 91}

    @app.get("/crash")
    def crash():
        raise RuntimeError("bug")

    @app.get("/health")
    def health():
        return {"ok": True}

    return app


def test_fastapi_records_route_templates_status_and_latency():
    from fastapi.testclient import TestClient

    v = fresh(version="v3")
    c = TestClient(fastapi_app(v), raise_server_exceptions=False)
    for roll in ("101", "102", "103"):
        assert c.get(f"/result/{roll}").status_code == 200
    c.get("/result/missing")
    c.get("/result/broken")
    assert c.get("/crash").status_code == 500
    c.get("/wp-admin")                                        # no such route
    c.get("/health")                                          # skipped
    m = c.get("/metrics")
    assert m.status_code == 200 and m.headers["content-type"].startswith("text/plain")
    t = m.text
    assert value(t, "http_requests_total", route="/result/{roll}", method="GET", status="200") == 3
    assert value(t, "http_requests_total", route="/result/{roll}", method="GET", status="404") == 1
    assert value(t, "http_requests_total", route="/result/{roll}", method="GET", status="503") == 1
    assert value(t, "http_requests_total", route="/crash", method="GET", status="500") == 1
    assert value(t, "http_requests_total", route="unmatched", method="GET", status="404") == 1
    assert value(t, "http_request_duration_seconds_count", route="/result/{roll}", method="GET") == 5
    assert value(t, "build_info", version="v3") == 1
    names = {k[0] for k in samples(t)}
    assert not any("health" in str(k) or "/metrics" in str(k) for k in samples(t) if k[0] == "http_requests_total")
    assert "process_resident_memory_bytes" in names and "process_cpu_seconds_total" in names
    assert not any(dict(k[1]).get("service") for k in samples(t)), "never set `service`: Prometheus adds it"


def test_count_route_turns_status_into_customer_outcomes():
    from fastapi.testclient import TestClient

    v = fresh()
    lookups = v.customer("result_requests_total", duration="result_request_duration_seconds")
    v.count_route("GET", "/result/{roll}", lookups)
    c = TestClient(fastapi_app(v), raise_server_exceptions=False)
    c.get("/result/1"); c.get("/result/2"); c.get("/result/missing"); c.get("/result/broken")
    t = c.get("/metrics").text
    assert value(t, "result_requests_total", outcome="ok") == 2
    assert value(t, "result_requests_total", outcome="failed") == 1
    assert value(t, "result_requests_total", outcome="client_error") == 1
    assert value(t, "result_request_duration_seconds_count") == 4


def test_customer_outcomes_exist_at_zero_before_any_attempt():
    v = fresh()
    v.customer("orders_total")
    t = v.exposition()[0].decode()
    assert value(t, "orders_total", outcome="ok") == 0 and value(t, "orders_total", outcome="failed") == 0


def test_attempt_times_and_counts_exceptions_as_failed():
    v = fresh()
    pay = v.customer("payments_total", duration="payment_duration_seconds")
    with pay.attempt():
        pass
    with pay.attempt() as a:
        a.outcome = "declined"
    with pytest.raises(ValueError):
        with pay.attempt():
            raise ValueError("gateway said no")
    with pytest.raises(LookupError):                       # outcome set, then an exception to answer the customer
        with pay.attempt() as a:
            a.outcome = "declined"
            raise LookupError("card declined")
    t = v.exposition()[0].decode()
    assert value(t, "payments_total", outcome="ok") == 1
    assert value(t, "payments_total", outcome="declined") == 2
    assert value(t, "payments_total", outcome="failed") == 1
    assert value(t, "payment_duration_seconds_count") == 4


# ---- calls to other services

def mock(handler):
    return httpx.MockTransport(handler)


def test_httpx_records_only_listed_hosts_with_the_right_outcome():
    v = fresh(targets={"auth": "auth", "pdf:8000": "pdf-render"})

    def handler(req):
        if req.url.host == "auth":
            return httpx.Response(200)
        if req.url.path == "/slow":
            raise httpx.ReadTimeout("slow", request=req)
        if req.url.path == "/down":
            raise httpx.ConnectError("refused", request=req)
        if req.url.path == "/missing":
            return httpx.Response(404)
        return httpx.Response(502)

    with httpx.Client(transport=VigilTransport(v, mock(handler))) as c:
        c.get("http://auth/check")
        c.get("http://pdf:8000/render")
        c.get("http://pdf:8000/missing")
        for path in ("/slow", "/down"):
            with pytest.raises(httpx.HTTPError):
                c.get(f"http://pdf:8000{path}")
        c.get("http://pdf:9999/render")                     # another port: not listed
        c.get("https://api.razorpay.com/v1/orders")          # third party: never recorded
    t = v.exposition()[0].decode()
    assert value(t, "dependency_requests_total", target="auth", outcome="ok") == 1
    assert value(t, "dependency_requests_total", target="pdf-render", outcome="ok") == 1      # the 404
    assert value(t, "dependency_requests_total", target="pdf-render", outcome="error") == 2   # 502 + refused
    assert value(t, "dependency_requests_total", target="pdf-render", outcome="timeout") == 1
    assert value(t, "dependency_request_duration_seconds_count", target="pdf-render") == 4
    assert not any("razorpay" in str(k) for k in samples(t))


def test_async_httpx_transport():
    v = fresh(targets={"auth": "auth"})

    async def go():
        async with httpx.AsyncClient(transport=AsyncVigilTransport(v, mock(lambda r: httpx.Response(503)))) as c:
            await c.get("http://auth/check")

    asyncio.run(go())
    assert value(v.exposition()[0].decode(), "dependency_requests_total", target="auth", outcome="error") == 1


def test_requests_adapter(monkeypatch):
    from requests.adapters import HTTPAdapter

    v = fresh(targets={"auth": "auth"})

    def fake_send(self, request, **kw):
        if "timeout" in request.url:
            raise requests.exceptions.ReadTimeout("slow")
        r = requests.Response()
        r.status_code = 200 if "ok" in request.url else 500
        return r

    monkeypatch.setattr(HTTPAdapter, "send", fake_send)
    s = requests.Session()
    s.mount("http://", requests_adapter(v))
    s.get("http://auth/ok"); s.get("http://auth/fail")
    with pytest.raises(requests.exceptions.Timeout):
        s.get("http://auth/timeout")
    s.get("http://elsewhere/ok")
    t = v.exposition()[0].decode()
    assert value(t, "dependency_requests_total", target="auth", outcome="ok") == 1
    assert value(t, "dependency_requests_total", target="auth", outcome="error") == 1
    assert value(t, "dependency_requests_total", target="auth", outcome="timeout") == 1
    assert not any("elsewhere" in str(k) for k in samples(t))


def test_call_context_for_any_client():
    v = fresh()
    with v.call("student-db"):
        pass
    with pytest.raises(TimeoutError):
        with v.call("student-db"):
            raise TimeoutError()
    with v.call("student-db") as c:
        c.failed()
    t = v.exposition()[0].decode()
    assert value(t, "dependency_requests_total", target="student-db", outcome="ok") == 1
    assert value(t, "dependency_requests_total", target="student-db", outcome="timeout") == 1
    assert value(t, "dependency_requests_total", target="student-db", outcome="error") == 1


def test_version_defaults_to_app_version(monkeypatch):
    monkeypatch.setenv("APP_VERSION", "v9")
    assert value(fresh().exposition()[0].decode(), "build_info", version="v9") == 1


def test_plain_starlette_finds_the_route_template():
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    async def marks(request):
        return PlainTextResponse("91")

    app = Starlette(routes=[Route("/marks/{roll}", marks)])
    v = fresh()
    instrument_fastapi(app, v)
    c = TestClient(app)
    c.get("/marks/7"); c.get("/marks/8"); c.get("/nothing")
    t = c.get("/metrics").text
    assert value(t, "http_requests_total", route="/marks/{roll}", method="GET", status="200") == 2
    assert value(t, "http_requests_total", route="unmatched", method="GET", status="404") == 1
