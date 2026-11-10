"""Client for the actuator. The engine never touches Docker directly."""

from __future__ import annotations

import httpx


class ActuatorError(Exception):
    pass


class ActuatorClient:
    def __init__(self, base_url: str, token: str, timeout_s: float = 30.0,
                 client: httpx.AsyncClient | None = None):
        self.token = token
        self._client = client or httpx.AsyncClient(base_url=base_url, timeout=timeout_s)

    async def close(self) -> None:
        await self._client.aclose()

    async def containers(self) -> list[dict]:
        try:
            r = await self._client.get("/containers", headers={"X-Actuator-Token": self.token})
        except httpx.HTTPError as exc:
            raise ActuatorError(f"actuator unreachable ({exc.__class__.__name__})") from exc
        if r.status_code != 200:
            raise ActuatorError(f"actuator answered {r.status_code}: {r.text[:120]}")
        return r.json()["containers"]

    async def act(self, action_id: str, action: str, container: str) -> dict:
        try:
            r = await self._client.post("/actions", headers={"X-Actuator-Token": self.token},
                                        json={"action_id": action_id, "action": action, "container": container})
        except httpx.HTTPError as exc:
            raise ActuatorError(f"actuator unreachable ({exc.__class__.__name__})") from exc
        if r.status_code != 200:
            raise ActuatorError(f"actuator refused ({r.status_code}): {r.json().get('detail', r.text)[:160]}")
        return r.json()
