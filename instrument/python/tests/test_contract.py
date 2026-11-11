"""Contract: every series VIGIL's collector asks Prometheus for exists in what
vigil-instrument exports, with the labels the query groups and filters by.

The queries are not copied here: they are captured from the engine's own
PrometheusCollector (configured with examples/results-portal.yaml), so if the
engine ever asks for something new, this fails. The apps are the four services
of examples/results-portal/app.py, calling each other for real (in-process ASGI).
"""

import asyncio
import re
import sys
from pathlib import Path

import httpx
import pytest
from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.parser import text_string_to_metric_families

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "engine"), str(ROOT / "examples" / "results-portal")]

from vigil.collector import PrometheusCollector  # noqa: E402
from vigil.config import load_settings  # noqa: E402

from app import make_app  # noqa: E402

SERVICES = ("web", "results-api", "auth", "pdf-render")
URLS = {name: f"http://{name}:8000" for name in SERVICES if name != "web"}
LABELS_PROMETHEUS_ADDS = {"service"}          # per scrape job, see prometheus.yml
SERIES_PROMETHEUS_MAKES = {"up"}              # one per scrape target


class Router(httpx.AsyncBaseTransport):
    """Sends each request to the app named by its host, like Docker DNS would."""

    def __init__(self, apps):
        self.apps = {name: httpx.ASGITransport(app=a) for name, a in apps.items()}

    async def handle_async_request(self, request):
        return await self.apps[request.url.host].handle_async_request(request)


@pytest.fixture(scope="module")
def scraped():
    """Run a results-day burst through the portal; return each service's samples,
    with the `service` label Prometheus would add."""
    registries = {name: CollectorRegistry() for name in SERVICES}
    apps = {}
    router = Router(apps)
    for name in SERVICES:
        apps[name] = make_app(name, registry=registries[name], transport=router, urls=URLS)
    router.apps = {name: httpx.ASGITransport(app=a) for name, a in apps.items()}

    async def burst():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=apps["web"]), base_url="http://web") as c:
            for roll in [*(str(n) for n in range(40)), "x1", "x2"]:
                await c.get(f"/result/{roll}")

    asyncio.run(burst())
    out = {}
    for name in SERVICES:
        text = generate_latest(registries[name]).decode()
        out[name] = [(s.name, {**s.labels, "service": name}, s.value)
                     for fam in text_string_to_metric_families(text) for s in fam.samples]
    return out


@pytest.fixture(scope="module")
def queries():
    """The PromQL VIGIL's collector sends, captured from the engine itself."""
    settings = load_settings(str(ROOT / "examples" / "results-portal.yaml"))
    seen = []

    class Capture(PrometheusCollector):
        async def _query(self, promql):
            seen.append(promql)
            return []

    asyncio.run(Capture(settings, client=object()).collect())
    assert len(seen) >= 10
    return seen


FUNCS = {"sum", "max", "min", "avg", "rate", "deriv", "histogram_quantile", "time", "timestamp", "by"}


def selectors(promql: str):
    """(metric, {label: matcher}) for every series a query reads, plus the labels it groups by."""
    groups = set()
    for g in re.findall(r"\bby\s*\(([^)]*)\)", promql):
        groups |= {x.strip() for x in g.split(",") if x.strip()}
    q = re.sub(r"\bby\s*\([^)]*\)", " ", promql)
    q = re.sub(r"\[[^\]]*\]", "", q)                 # range windows: [20s]
    sels = []
    for m in re.finditer(r"([a-zA-Z_:][a-zA-Z0-9_:]*)\s*(\{[^}]*\})?", q):
        name, matchers = m.group(1), m.group(2) or ""
        if name in FUNCS or q[m.end():].lstrip().startswith("(") or re.fullmatch(r"\d+s?", name):
            continue
        if name in groups:
            continue
        labels = dict(re.findall(r'(\w+)\s*(?:=~|!=|=)\s*"([^"]*)"', matchers))
        sels.append((name, labels))
    return sels, groups


def test_every_series_vigil_reads_is_exported_with_its_labels(scraped, queries):
    all_samples = [s for rows in scraped.values() for s in rows]
    checked = set()
    for promql in queries:
        sels, groups = selectors(promql)
        assert sels, f"could not read a series from: {promql}"
        for name, matchers in sels:
            if name in SERIES_PROMETHEUS_MAKES:
                continue
            rows = [(n, l) for n, l, _ in all_samples if n == name]
            assert rows, f"VIGIL reads {name} ({promql}) but no service exports it"
            for label in set(matchers) | (groups - {"le"}):
                assert any(label in l for _, l in rows), f"{name} has no label {label!r}, needed by: {promql}"
            if "le" in groups:
                assert name.endswith("_bucket") and any("le" in l for _, l in rows), f"{name} is not a histogram"
            checked.add(name)
    # Everything the engine needs per service, the customer metric, and calls between services.
    assert {"http_requests_total", "http_request_duration_seconds_bucket", "process_cpu_seconds_total",
            "process_resident_memory_bytes", "build_info", "dependency_requests_total",
            "dependency_request_duration_seconds_bucket", "result_requests_total",
            "result_request_duration_seconds_bucket"} <= checked


def total(rows, name, **labels):
    return sum(v for n, l, v in rows if n == name and all(l.get(k) == x for k, x in labels.items()))


def test_the_portal_reports_what_happened(scraped):
    web, api = scraped["web"], scraped["results-api"]
    # 42 lookups by students: 40 found, 2 unknown roll numbers.
    assert total(web, "result_requests_total", outcome="ok") == 40
    assert total(web, "result_requests_total", outcome="not_found") == 2
    assert total(web, "result_requests_total", outcome="failed") == 0
    assert total(web, "http_requests_total", route="/result/{roll}") == 42
    # The call graph the blame walk follows: web -> results-api -> auth, pdf-render.
    assert total(web, "dependency_requests_total", target="results-api", outcome="ok") == 42
    assert total(api, "dependency_requests_total", target="auth", outcome="ok") == 42
    assert total(api, "dependency_requests_total", target="pdf-render", outcome="ok") == 40
    # Each service reports its version, and nobody sets `service` but Prometheus.
    for name in SERVICES:
        assert total(scraped[name], "build_info", version="v1") == 1


def test_a_broken_pdf_render_shows_up_where_vigil_looks(monkeypatch):
    """pdf-render answers 500: the failing call is visible on results-api's edge to
    it, and students see failed lookups, which is how VIGIL tells cause from symptom."""
    registries = {name: CollectorRegistry() for name in SERVICES}
    apps = {}
    router = Router(apps)
    for name in SERVICES:
        apps[name] = make_app(name, registry=registries[name], transport=router, urls=URLS)

    monkeypatch.setenv("PDF_BROKEN", "1")
    router.apps = {name: httpx.ASGITransport(app=a) for name, a in apps.items()}

    async def burst():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=apps["web"]), base_url="http://web") as c:
            for roll in range(10):
                await c.get(f"/result/{roll}")

    asyncio.run(burst())
    rows = {n: [(s.name, s.labels, s.value) for fam in text_string_to_metric_families(generate_latest(r).decode())
                for s in fam.samples] for n, r in registries.items()}
    assert total(rows["results-api"], "dependency_requests_total", target="pdf-render", outcome="error") == 10
    assert total(rows["results-api"], "dependency_requests_total", target="auth", outcome="ok") == 10
    assert total(rows["web"], "result_requests_total", outcome="failed") == 10
    assert total(rows["pdf-render"], "http_requests_total", route="/render/{roll}", status="500") == 10
