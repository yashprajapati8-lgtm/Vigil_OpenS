"""Fan-out of live events to every connected dashboard (Server-Sent Events)."""

from __future__ import annotations

import asyncio
import json
import logging

log = logging.getLogger("vigil.bus")


class Bus:
    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._watchers: list = []

    def watch(self, callback) -> None:
        """Also hand every message to `callback` (e.g. email alerts). It runs on the
        publisher's thread, so it must only queue work, never do it."""
        self._watchers.append(callback)

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def _deliver(self, message: dict) -> None:
        data = json.dumps(message, default=str)
        for q in list(self._subs):
            if q.full():  # a slow client loses its oldest message, never blocks VIGIL
                q.get_nowait()
            q.put_nowait(data)

    def publish(self, message: dict) -> None:
        """Safe to call from the event loop or from a worker thread."""
        for watcher in self._watchers:
            try:
                watcher(message)
            except Exception:  # an alert problem must never stop VIGIL
                log.exception("bus watcher failed")
        if self._loop is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            self._deliver(message)
        else:
            self._loop.call_soon_threadsafe(self._deliver, message)
