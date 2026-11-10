"""Load generator: steady checkout traffic, so the signals are never 0/0.

Also exports what users feel (checkout success/failure and latency), which is the
signal VIGIL verifies recovery against. The RPS control endpoint sits on the
chaos admin port, so only the chaos controller can start a "flash sale".
"""

import asyncio
import contextlib
import os
import random
import time
from collections import deque

import httpx
from fastapi import Header, HTTPException, Request
from pydantic import BaseModel, Field
from prometheus_client import Counter, Gauge, Histogram

from .common import LATENCY_BUCKETS, UNINSTRUMENTED, create_app, log, require_token

PAYMENT_URL = os.getenv("PAYMENT_URL", "http://payment:8000")
ENGINE_URL = os.getenv("ENGINE_URL", "http://vigil-engine:8080")   # read-only: what VIGIL is doing, for the phone page
SKUS = ["SKU-HEADPHONES", "SKU-KEYBOARD", "SKU-MONITOR", "SKU-MOUSE", "SKU-WEBCAM"]
MAX_IN_FLIGHT = 300

CHECKOUTS = Counter("checkout_requests_total", "Checkouts attempted by users", ["outcome"])
CHECKOUT_DURATION = Histogram(
    "checkout_duration_seconds", "End-to-end checkout latency", buckets=LATENCY_BUCKETS
)
TARGET_RPS = Gauge("loadgen_target_rps", "Configured checkout rate")


class State:
    rps: float = float(os.getenv("RPS", "20"))
    in_flight: int = 0


async def _one_checkout(client: httpx.AsyncClient) -> None:
    State.in_flight += 1
    start = time.perf_counter()
    try:
        resp = await client.post("/pay", json={"sku": random.choice(SKUS), "qty": random.randint(1, 3)})
        outcome = "ok" if resp.status_code < 400 else ("client_error" if resp.status_code < 500 else "failed")
    except httpx.HTTPError:
        outcome = "failed"
    finally:
        State.in_flight -= 1
    CHECKOUTS.labels(outcome).inc()
    CHECKOUT_DURATION.observe(time.perf_counter() - start)


async def _drive() -> None:
    # No keep-alive: every request resolves the `payment` alias again, so a
    # rollback or an extra replica receives traffic immediately.
    limits = httpx.Limits(max_keepalive_connections=0, max_connections=MAX_IN_FLIGHT)
    async with httpx.AsyncClient(base_url=PAYMENT_URL, timeout=5.0, limits=limits) as client:
        pending: set[asyncio.Task] = set()
        while True:
            TARGET_RPS.set(State.rps)
            if State.rps <= 0:
                await asyncio.sleep(0.5)
                continue
            if State.in_flight < MAX_IN_FLIGHT:
                task = asyncio.create_task(_one_checkout(client))
                pending.add(task)
                task.add_done_callback(pending.discard)
            else:
                CHECKOUTS.labels("dropped").inc()
            # Jittered spacing so traffic looks organic rather than metronomic.
            await asyncio.sleep(random.expovariate(State.rps))


_driver: asyncio.Task | None = None


async def _start() -> None:
    global _driver
    _driver = asyncio.create_task(_drive())
    log.info("load generator targeting %s at %.0f rps", PAYMENT_URL, State.rps)


async def _stop() -> None:
    if _driver:
        _driver.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _driver


class RateUpdate(BaseModel):
    rps: float = Field(ge=0, le=500)


def _rps_routes(admin) -> None:
    """Rate control lives on the chaos admin app: separate port, token required."""

    @admin.get("/rps")
    def get_rps(x_chaos_token: str | None = Header(None)):
        require_token(x_chaos_token)
        return {"rps": State.rps}

    @admin.post("/rps")
    def set_rps(update: RateUpdate, x_chaos_token: str | None = Header(None)):
        require_token(x_chaos_token)
        State.rps = update.rps
        log.warning("load set to %.0f rps", State.rps)
        return {"rps": State.rps}


app = create_app("loadgen", on_startup=_start, on_shutdown=_stop, extend_admin=_rps_routes)


# ---------------------------------------------------------------- phone checkout (demo)
# A person on a phone is one more customer. Their checkout takes the same path as
# the generated ones (storefront -> payment -> inventory) and counts in the same
# checkout metrics, so VIGIL sees it like any other. No money moves: the "card
# charge" is the demo payment service. This lives in the storefront because VIGIL
# never restarts it, so the list of phones and their payments survives every fix.

PHONE_CHECKOUTS = Counter("phone_checkout_requests_total", "Demo checkouts made from a phone", ["outcome"])
PHONE_ACTIVE_S = 15            # a phone that said hello this recently counts as connected
PHONE_MIN_GAP_S = 1.0          # per phone: at most one demo payment a second
PHONE_MAX_PER_MIN = 120        # all phones together, so a public link can't flood the demo
UNINSTRUMENTED.update({"/phone/hello", "/phone/status", "/phone/vigil"})
VIGIL_CACHE_S = 2.0            # many phones polling must not mean many engine calls
RECENT_FIX_S = 600             # a resolved incident stays on the phone page for 10 minutes   # bookkeeping, not customer traffic


class Phones:
    devices: dict[str, dict] = {}
    payments: deque = deque(maxlen=60)
    last_pay: dict[str, float] = {}
    recent: deque = deque(maxlen=PHONE_MAX_PER_MIN)   # timestamps of the last payments, all phones
    seq: int = 0
    totals: dict[str, int] = {"paid": 0, "failed": 0}
    vigil: dict | None = None
    vigil_at: float = 0.0


class PhoneHello(BaseModel):
    device_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,40}$")


class PhonePay(PhoneHello):
    sku: str = Field(pattern=r"^SKU-[A-Z]{2,16}$")
    amount: int = Field(ge=1, le=100_000)   # rupees, shown on the receipt only


def _device_label(ua: str) -> str:
    ua = ua or ""
    os_name = ("iPhone" if "iPhone" in ua else "iPad" if "iPad" in ua else "Android" if "Android" in ua
               else "Windows" if "Windows" in ua else "Mac" if "Macintosh" in ua else "Linux" if "Linux" in ua else "Device")
    browser = ("Edge" if "Edg/" in ua else "Samsung Internet" if "SamsungBrowser" in ua else "Chrome" if ("Chrome" in ua or "CriOS" in ua)
               else "Firefox" if ("Firefox" in ua or "FxiOS" in ua) else "Safari" if "Safari" in ua else "browser")
    return f"{os_name} · {browser}"


def _seen(device_id: str, ua: str) -> dict:
    now = time.time()
    dev = Phones.devices.get(device_id)
    if dev is None:
        if len(Phones.devices) >= 300:   # forget the oldest so a public link can't grow this forever
            for old in sorted(Phones.devices.values(), key=lambda d: d["last_seen"])[:100]:
                Phones.devices.pop(old["id"], None)
        dev = Phones.devices[device_id] = {"id": device_id, "label": _device_label(ua), "first_seen": now,
                                           "last_seen": now, "paid": 0, "failed": 0}
    dev["last_seen"] = now
    return dev


def _explain(status: int | None, detail: str, timed_out: bool) -> tuple[str, str]:
    """Why a checkout failed, in the customer's words, from what payment answered."""
    if timed_out:
        return "payment_timeout", "Payment didn't answer within 5 s."
    if status is None:
        return "payment_down", "The payment service is down, so the card could not be charged."
    if detail == "inventory timeout":
        return "inventory_timeout", "The stock check timed out: inventory didn't answer within 1 s."
    if detail == "inventory unreachable":
        return "inventory_down", "Inventory is down, so the stock could not be reserved."
    if detail.startswith("inventory"):
        return "inventory_error", "Inventory returned an error while reserving the stock."
    if detail.startswith("payment busy"):
        return "payment_busy", "Payment is overloaded: too many checkouts at once."
    if status == 500:
        return "payment_error", "The payment service hit an internal error."
    return "payment_error", f"Payment answered HTTP {status}."


@app.post("/phone/hello")
def phone_hello(body: PhoneHello, request: Request):
    dev = _seen(body.device_id, request.headers.get("user-agent", ""))
    return {"ok": True, "label": dev["label"]}


@app.post("/phone/pay")
async def phone_pay(body: PhonePay, request: Request):
    now = time.time()
    dev = _seen(body.device_id, request.headers.get("user-agent", ""))
    if now - Phones.last_pay.get(body.device_id, 0.0) < PHONE_MIN_GAP_S:
        raise HTTPException(status_code=429, detail="One demo payment a second, please.")
    if len(Phones.recent) >= PHONE_MAX_PER_MIN and now - Phones.recent[0] < 60:
        raise HTTPException(status_code=429, detail="Too many demo payments right now. Try again in a minute.")
    Phones.last_pay[body.device_id] = now
    Phones.recent.append(now)

    status, detail, order_id, timed_out = None, "", None, False
    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(base_url=PAYMENT_URL, timeout=5.0) as client:
            resp = await client.post("/pay", json={"sku": body.sku, "qty": 1})
        status = resp.status_code
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if status < 400:
            order_id = data.get("order_id")
        else:
            detail = str(data.get("detail", ""))
    except httpx.TimeoutException:
        timed_out = True
    except httpx.HTTPError:
        status = None
    elapsed = time.perf_counter() - start

    ok = status is not None and status < 400
    CHECKOUTS.labels("ok" if ok else "failed").inc()      # the same metric VIGIL verifies recovery on
    CHECKOUT_DURATION.observe(elapsed)
    PHONE_CHECKOUTS.labels("paid" if ok else "failed").inc()
    code, reason = ("paid", "") if ok else _explain(status, detail, timed_out)
    Phones.seq += 1
    Phones.totals["paid" if ok else "failed"] += 1
    dev["paid" if ok else "failed"] += 1
    record = {"id": Phones.seq, "ts": now, "device": dev["label"], "device_id": body.device_id,
              "sku": body.sku, "amount": body.amount, "ok": ok, "ms": round(elapsed * 1000),
              "code": code, "reason": reason, "order_id": order_id}
    Phones.payments.appendleft(record)
    log.info("phone checkout %s in %d ms from %s%s", "paid" if ok else "FAILED", record["ms"], dev["label"],
             f" ({code})" if not ok else "")
    return record


@app.get("/phone/status")
def phone_status():
    now = time.time()
    active = [{"id": d["id"], "label": d["label"], "seen_s": round(now - d["last_seen"], 1),
               "paid": d["paid"], "failed": d["failed"]}
              for d in sorted(Phones.devices.values(), key=lambda d: -d["last_seen"])
              if now - d["last_seen"] <= PHONE_ACTIVE_S]
    return {"now": now, "connected": active, "payments": list(Phones.payments)[:20], "totals": dict(Phones.totals)}



@app.get("/phone/vigil")
async def phone_vigil():
    """A small, read-only summary of what VIGIL is doing, so the phone page can show it
    without exposing the engine API. Cached for 2 s."""
    now = time.time()
    if Phones.vigil is not None and now - Phones.vigil_at < VIGIL_CACHE_S:
        return Phones.vigil
    summary: dict = {"online": False, "autonomy": None, "paused": None, "incident": None}
    try:
        async with httpx.AsyncClient(base_url=ENGINE_URL, timeout=1.5) as client:
            health = (await client.get("/api/health")).json()
            latest = (await client.get("/api/incidents", params={"limit": 1})).json().get("incidents", [])
        control = health.get("control") or {}
        summary.update(online=True, autonomy=control.get("autonomy"), paused=control.get("paused"))
        if latest:
            inc = latest[0]
            closed = inc.get("closed_at")
            if closed is None or now - closed <= RECENT_FIX_S:
                metrics = (inc.get("data") or {}).get("metrics") or {}
                summary["incident"] = {"id": inc.get("id"), "status": inc.get("status"), "root": inc.get("root"),
                                       "category": inc.get("category"), "title": inc.get("title"),
                                       "mttr_s": metrics.get("mttr_s"), "closed_at": closed}
    except (httpx.HTTPError, ValueError):
        pass
    Phones.vigil, Phones.vigil_at = summary, now
    return summary
