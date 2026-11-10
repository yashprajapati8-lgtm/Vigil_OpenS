"""inventory-service: reserves stock for an order. Called by payment-service."""

import asyncio
import random

from pydantic import BaseModel, Field

from .common import create_app

app = create_app("inventory-service")


class Reservation(BaseModel):
    order_id: str = Field(min_length=1, max_length=64)
    sku: str = Field(min_length=1, max_length=32)
    qty: int = Field(ge=1, le=10)


@app.post("/reserve")
async def reserve(item: Reservation):
    await asyncio.sleep(random.uniform(0.005, 0.015))  # simulated stock lookup
    return {"order_id": item.order_id, "sku": item.sku, "reserved": item.qty}
