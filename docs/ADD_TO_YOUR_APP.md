# Add VIGIL to your app

From zero to VIGIL watching your own site: a results portal, a shop, a payments API.
About 20 minutes. You need Docker, your services running in Docker Compose, and Python 3.9+.

**What VIGIL will do:** learn each service's normal, notice when customers are hurt, find the
service that is really at fault, and either fix it (restart, roll back, add capacity) or tell a
human exactly what to do, depending on the autonomy you pick.

**What it will not do:** touch anything you did not label, use a fix `vigil.yaml` does not allow,
or act at all at L0/L1 without a human clicking.

## 1. Make each service report (FastAPI)

In every service VIGIL should watch, and in the one customers arrive at:

```bash
pip install /path/to/Vigil_OpenS/instrument/python
```

```python
from vigil_instrument import AsyncVigilTransport, Vigil, instrument_fastapi

vigil = instrument_fastapi(app, Vigil(targets={"auth:8000": "auth", "pdf-render:8000": "pdf-render"}))
http = httpx.AsyncClient(transport=AsyncVigilTransport(vigil))   # use it for calls to your other services
```

In the service customers arrive at, also count what they come to do:

```python
lookups = vigil.customer("result_requests_total", duration="result_request_duration_seconds")
vigil.count_route("GET", "/result/{roll}", lookups)
```

Check it: open `http://<service>:8000/metrics`, you should see `http_requests_total`.
Full reference: [`instrument/README.md`](../instrument/README.md). A complete four-service example:
[`examples/results-portal/app.py`](../examples/results-portal/app.py).

## 2. Describe your site

```bash
python scripts/vigil_init.py
```

It asks: the site's name, where customers arrive, what they do ("result lookup"), each service,
who calls whom, which fixes VIGIL may use on each, your Docker network, and the autonomy.
It writes `deploy/vigil.yaml`, `deploy/prometheus.yml` and `deploy/.env` (with a new operator
token; open the file to see it). Every answer is checked with the same rules VIGIL applies at
start. Edit `deploy/vigil.yaml` later as you like.

Start at **L1**: VIGIL diagnoses and proposes, you click. Move to L2 once you trust it.

## 3. Label what VIGIL may act on

In **your** `docker-compose.yml`, for each watched service (`vigil_init.py` prints these):

```yaml
  results-api:
    labels:
      vigil.managed: "true"
      vigil.service: results-api
      vigil.version: v1          # same as the app's APP_VERSION
```

Then `docker compose up -d` so the labels apply. Unlabelled containers are invisible to the actuator.

- **Rollback** needs the previous version still there: deploy the new one as a new container and
  keep the old one stopped, with its own `vigil.version`.
- **Add capacity** needs a stopped spare labelled `vigil.role: replica` on the same network alias,
  scraped by Prometheus. VIGIL starts it under real load and stops it when load is back to normal.

Only `restart` needs nothing extra.

## 4. Start VIGIL

```bash
cd deploy
docker compose up --build -d
```

Open **http://localhost:3000**. For the first 3 minutes VIGIL learns what normal looks like
(the map says "learning"); after that it keeps learning from healthy traffic on its own.

## 5. See it work

Make one service slow or failing (stop it, or add a delay) and watch the console: the map marks
the root cause and the symptoms, the loop shows each step, and the decision shows every fix
priced in your customers' terms. At L1 it waits for your click; unlock operator mode with the
token from `deploy/.env`.

## Limits, said plainly

- VIGIL **fixes** only what runs in Docker on the same machine (the actuator uses the Docker
  socket). On shared hosting or a managed platform it can still **watch, diagnose and tell a
  human** (use L0 or L1).
- Kubernetes is not supported yet.
- The instrumentation package is FastAPI only for now; Node (Express) is next.
- One Uvicorn worker per container (scale with containers), or use prometheus_client's
  multiprocess mode.
