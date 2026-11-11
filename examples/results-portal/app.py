"""A tiny university results portal, instrumented for VIGIL with vigil-instrument.

Four FastAPI services, one file, picked by SERVICE_NAME:

    web          the page students open: GET /result/{roll}      (the entry)
    results-api  looks up marks: GET /marks/{roll}               -> auth, pdf-render
    auth         checks roll number + date of birth: GET /check/{roll}
    pdf-render   renders the marksheet: GET /render/{roll}

It matches examples/results-portal.yaml. Everything VIGIL-specific is the block
marked "VIGIL" in make_app(): about ten lines.

    SERVICE_NAME=web uvicorn app:app --port 8000
    PDF_DELAY_MS=1500   make pdf-render slow (a gray failure: up, but useless)
    PDF_BROKEN=1        make pdf-render answer 500
"""

from __future__ import annotations

import asyncio
import os
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException
from prometheus_client import REGISTRY, CollectorRegistry

from vigil_instrument import AsyncVigilTransport, Vigil, instrument_fastapi

URLS = {
    "results-api": os.getenv("RESULTS_API_URL", "http://results-api:8000"),
    "auth": os.getenv("AUTH_URL", "http://auth:8000"),
    "pdf-render": os.getenv("PDF_RENDER_URL", "http://pdf-render:8000"),
}


def make_app(service: str, *, registry: CollectorRegistry = REGISTRY, transport: httpx.AsyncBaseTransport | None = None,
             urls: dict[str, str] = URLS) -> FastAPI:
    app = FastAPI(title=f"results portal: {service}")

    # ---- VIGIL ------------------------------------------------------------------
    # Record calls to the other services by host, under their vigil.yaml names.
    targets = {urlsplit(u).netloc: name for name, u in urls.items()}
    vigil = instrument_fastapi(app, Vigil(version=os.getenv("APP_VERSION", "v1"), targets=targets, registry=registry))
    http = httpx.AsyncClient(transport=AsyncVigilTransport(vigil, transport), timeout=2.0)
    if service == "web":
        # What students come to do. Names match vigil.yaml `customers`.
        lookups = vigil.customer("result_requests_total", duration="result_request_duration_seconds",
                                 outcomes=("ok", "not_found", "failed", "timeout"))
    # -------------------------------------------------------------------------------

    if service == "web":
        @app.get("/result/{roll}")
        async def result(roll: str):
            with lookups.attempt() as a:
                try:
                    r = await http.get(f"{urls['results-api']}/marks/{roll}")
                except httpx.TimeoutException:
                    a.outcome = "timeout"
                    raise HTTPException(504, "results are taking too long, try again")
                except httpx.HTTPError:
                    raise HTTPException(502, "results are unavailable")
                if r.status_code == 404:
                    a.outcome = "not_found"
                    raise HTTPException(404, "no result for this roll number yet")
                if r.status_code >= 500:
                    a.outcome = "failed"
                    raise HTTPException(502, "results are unavailable")
                return r.json()

    elif service == "results-api":
        @app.get("/marks/{roll}")
        async def marks(roll: str):
            ok = await http.get(f"{urls['auth']}/check/{roll}")
            if ok.status_code == 404:
                raise HTTPException(404, "unknown roll number")
            if ok.status_code >= 500:
                raise HTTPException(503, "auth unavailable")
            pdf = await http.get(f"{urls['pdf-render']}/render/{roll}")
            if pdf.status_code >= 500:
                raise HTTPException(503, "marksheet unavailable")
            return {"roll": roll, "marks": {"maths": 91, "physics": 84}, "marksheet": pdf.json()["url"]}

    elif service == "auth":
        @app.get("/check/{roll}")
        async def check(roll: str):
            if roll.startswith("x"):
                raise HTTPException(404, "unknown roll number")
            return {"roll": roll, "ok": True}

    elif service == "pdf-render":
        @app.get("/render/{roll}")
        async def render(roll: str):
            await asyncio.sleep(int(os.getenv("PDF_DELAY_MS", "15")) / 1000)
            if os.getenv("PDF_BROKEN") == "1":
                raise HTTPException(500, "renderer crashed")
            return {"url": f"/marksheets/{roll}.pdf"}

    else:
        raise ValueError(f"unknown SERVICE_NAME {service!r}")

    @app.get("/health")
    async def health():
        return {"ok": True}

    return app


app = make_app(os.getenv("SERVICE_NAME", "web")) if os.getenv("SERVICE_NAME") else None
