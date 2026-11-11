# vigil-instrument (FastAPI)

A few lines that make a FastAPI service report what VIGIL needs. No agent, no sidecar:
it serves `/metrics` and Prometheus scrapes it.

```bash
pip install ./instrument/python        # from this repo (not on PyPI yet)
```

```python
from fastapi import FastAPI
import httpx
from vigil_instrument import AsyncVigilTransport, Vigil, instrument_fastapi

app = FastAPI()
vigil = instrument_fastapi(app, Vigil(
    version="v3",                                        # or $APP_VERSION
    targets={"auth:8000": "auth", "pdf:8000": "pdf-render"},   # host -> service name in vigil.yaml
))

# Calls to other services: one client, every call recorded.
http = httpx.AsyncClient(transport=AsyncVigilTransport(vigil))
```

That is enough for VIGIL to watch the service, see who it calls and blame the right one.

## The customer action (only on your entry service)

The one thing customers come to do, named in `vigil.yaml` under `customers`. Either count a route by its status:

```python
lookups = vigil.customer("result_requests_total", duration="result_request_duration_seconds")
vigil.count_route("GET", "/result/{roll}", lookups)   # 2xx/3xx ok, 5xx failed, 4xx client_error
```

or decide the outcome yourself:

```python
lookups = vigil.customer("result_requests_total", duration="result_request_duration_seconds",
                         outcomes=("ok", "not_found", "failed", "timeout"))

@app.get("/result/{roll}")
async def result(roll: str):
    with lookups.attempt() as a:            # an exception counts as "failed"...
        r = await http.get(f"http://results-api:8000/marks/{roll}")
        if r.status_code == 404:
            a.outcome = "not_found"         # ...unless you set the outcome first
            raise HTTPException(404, "no result yet")
        return r.json()
```

Every outcome you use must be listed in `vigil.yaml` as `ok` or `failed`; anything else counts as neither.

## Calls with other clients

```python
from vigil_instrument import VigilTransport, requests_adapter

httpx.Client(transport=VigilTransport(vigil))                       # httpx, sync
session.mount("http://", requests_adapter(vigil))                   # requests

with vigil.call("student-db"):                                      # anything else
    rows = await db.fetch(query)
```

A call's outcome: any answer below 500 is `ok` (a 404 is an answer), a 5xx is `error`, no answer
is `timeout` or `error`. Hosts not in `targets` (a payment gateway, an email API) are not recorded.

## What it exports

Exactly what VIGIL's collector reads (`engine/vigil/collector.py`); a test captures the engine's
queries and checks every series and label exists (`tests/test_contract.py`).

| Series | Labels | VIGIL uses it for |
|---|---|---|
| `http_requests_total` | route, method, status | traffic, errors |
| `http_request_duration_seconds` | route, method | latency p95 |
| `dependency_requests_total` | target, outcome | which calls fail: the blame walk |
| `dependency_request_duration_seconds` | target | slow calls |
| `build_info` | version | deploys, rollback |
| `process_cpu_seconds_total`, `process_resident_memory_bytes` | | CPU, memory, leaks (Linux) |
| your customer metric | outcome | what customers feel |

- `route` is the template (`/result/{roll}`), never the raw path. Unknown paths are `unmatched`,
  which VIGIL ignores. `/metrics` and health checks are not counted.
- No `service` label: Prometheus adds it per scrape job (see `deploy/prometheus.yml`).
  If the app set it too, Prometheus would rename the app's copy and VIGIL would see nothing.

## Limits, said plainly

- One process per container. With several Gunicorn/Uvicorn workers each has its own counters;
  use one worker per container (scale with containers) or prometheus_client's multiprocess mode.
- Process CPU and memory come from `/proc`: Linux (every Docker container) only.
- Node (Express) is planned, not built yet.
