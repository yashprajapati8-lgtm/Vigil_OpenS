"""payment-service: takes a checkout, reserves stock in inventory, charges the order."""

import asyncio
import os
import random
import uuid

from fastapi import HTTPException
from pydantic import BaseModel, Field

from .common import Dependency, DependencyError, create_app

inventory = Dependency(
    target="inventory",
    base_url=os.getenv("INVENTORY_URL", "http://inventory:8000"),
    timeout_s=float(os.getenv("INVENTORY_TIMEOUT_S", "1.0")),
)

app = create_app("payment-service", on_shutdown=inventory.close)

# Each instance holds a small pool of connections to the card processor, like a
# real payment service. That pool is its capacity: at ~20 ms per charge, 2 slots
# serve about 100 checkouts/s. Past that, requests queue; after 1 s in the queue
# they are shed with a 503. Another instance adds another pool: real capacity.
CHARGE_SLOTS = int(os.getenv("CHARGE_SLOTS", "2"))
QUEUE_TIMEOUT_S = float(os.getenv("CHARGE_QUEUE_TIMEOUT_S", "1.0"))
_slots = asyncio.Semaphore(CHARGE_SLOTS)


class Order(BaseModel):
    sku: str = Field(min_length=1, max_length=32)
    qty: int = Field(default=1, ge=1, le=10)


@app.post("/pay")
async def pay(order: Order):
    order_id = uuid.uuid4().hex[:12]
    try:
        await inventory.call(
            "POST", "/reserve", json={"order_id": order_id, "sku": order.sku, "qty": order.qty}
        )
    except DependencyError as exc:
        # Payment itself is fine; its dependency is not. The edge metrics record which.
        raise HTTPException(status_code=503, detail=f"inventory {exc.outcome}")
    try:
        await asyncio.wait_for(_slots.acquire(), timeout=QUEUE_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=503, detail="payment busy: card processor pool full")
    try:
        await asyncio.sleep(random.uniform(0.01, 0.03))  # the card charge
    finally:
        _slots.release()
    return {"order_id": order_id, "status": "paid"}
