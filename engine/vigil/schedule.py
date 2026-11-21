"""Rushes you know are coming: start the spare copies before them.

A results day is announced in advance. Waiting for it to crash the site and then
fixing it (about 80 s of error pages) makes no sense when the date is known. So
vigil.yaml can list events:

    events:
      - name: Results declared
        at: 2026-11-25T10:00+05:30
        prepare_min: 10
        lasts_min: 60

Like an alarm clock, for each event:
  * from `at - prepare_min` to `at + lasts_min` (its window), VIGIL keeps one spare
    copy running for each of the event's services: it starts it at the start of
    the window if it is not running yet, and never stops it inside the window;
  * after the window, the usual scale-in applies: once traffic is back near normal
    for a minute, VIGIL stops the spare copy, as it does after any rush.

If the rush is bigger than planned, nothing changes: VIGIL still detects, diagnoses
and fixes it as usual. The event only means it starts ready.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .engine import Engine


def clock(ts: float) -> str:
    """10:00 UTC, 25 Nov: when an event is, in words (the console shows local time)."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M UTC, %d %b")


class Schedule:
    def __init__(self, engine: "Engine"):
        self.e = engine
        self.prepared: set[tuple[str, str]] = set()   # (event name, service) already started for its window

    def in_window(self, event: dict, now: float) -> bool:
        start = event["at"] - event["prepare_s"]
        end = event["at"] + event["lasts_s"]
        return start <= now < end

    def holds(self, service: str, now: float | None = None) -> bool:
        """Is this service inside an event's window? Then its spare copy stays running."""
        if now is None:
            now = time.time()
        for event in self.e.s.events:
            if service in event["services"] and self.in_window(event, now):
                return True
        return False

    def due(self, now: float | None = None) -> list[tuple[dict, str]]:
        """(event, service) pairs whose spare copy should start now. Runs every tick."""
        if now is None:
            now = time.time()
        out = []
        if self.e.governor.paused or self.e.governor.autonomy == "L0":
            return out   # kill switch on, or watch only: VIGIL starts nothing
        for event in self.e.s.events:
            if not self.in_window(event, now):
                continue
            for service in event["services"]:
                key = (event["name"], service)
                if key in self.prepared:
                    continue
                if service in self.e.remediator.scaled:
                    self.prepared.add(key)   # a spare is already running: it stays for the window
                    continue
                out.append((event, service))
        return out

    def view(self, now: float | None = None) -> list[dict]:
        """For the console: every event and where it stands."""
        if now is None:
            now = time.time()
        out = []
        for event in self.e.s.events:
            start = event["at"] - event["prepare_s"]
            end = event["at"] + event["lasts_s"]
            if now < start:
                status = "upcoming"
            elif now < end:
                status = "ready window"
            else:
                status = "over"
            out.append({"name": event["name"], "at": event["at"], "prepare_from": start, "until": end,
                        "services": list(event["services"]), "status": status})
        return out
