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

Events come from two places, used the same way:
  * vigil.yaml `events` (whoever installs VIGIL), read at start;
  * the console's "Results days" box (an operator, no restart needed), saved in
    data/events.json so they survive a restart.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .engine import Engine


def clock(ts: float) -> str:
    """10:00 UTC, 25 Nov: when an event is, in words (the console shows local time)."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M UTC, %d %b")


def event_time(event: dict) -> float:
    """Sort events soonest first."""
    return event["at"]


class EventError(ValueError):
    """An event from the console that can't be used; the message says why."""


class Schedule:
    def __init__(self, engine: "Engine"):
        self.e = engine
        self.prepared: set[tuple[str, str]] = set()   # (event id, service) already started for its window
        self._path = engine.s.data_dir / "events.json"
        self.added: list[dict] = self._load()          # events added from the console
        self._next_id = 1
        for event in self.added:
            number = int(event["id"].split("-")[1])
            if number >= self._next_id:
                self._next_id = number + 1

    # ---- every event, wherever it came from

    def all_events(self) -> list[dict]:
        out = []
        index = 0
        for event in self.e.s.events:
            index = index + 1
            if "id" in event:
                out.append(event)
            else:
                out.append({**event, "id": f"yaml-{index}", "source": "vigil.yaml"})
        for event in self.added:
            out.append(event)
        return out

    # ---- added from the console

    def _load(self) -> list[dict]:
        if not self._path.exists():
            return []
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return list(data.get("events", []))
        except (OSError, ValueError):
            return []

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"events": self.added}, indent=1), encoding="utf-8")
        tmp.replace(self._path)

    def _default_services(self) -> list[str]:
        """Every watched service that may get a spare copy (scale_out)."""
        out = []
        for name in self.e.s.monitored:
            allowed = self.e.s.allowed_actions.get(name)
            if allowed is None or "scale_out" in allowed:
                out.append(name)
        return out

    def add(self, name: str, at: float, prepare_min: float = 10, lasts_min: float = 60) -> dict:
        """A new event from the console. `at` is a Unix time (the browser sends the
        operator's local date and time already converted, so time zones can't go wrong)."""
        name = (name or "").strip()
        if len(name) == 0 or len(name) > 60:
            raise EventError("Give it a short name (1 to 60 characters), e.g. Results declared")
        if prepare_min < 0 or prepare_min > 24 * 60 or lasts_min < 0 or lasts_min > 24 * 60:
            raise EventError("Minutes before and after must be between 0 and 1440")
        if at + lasts_min * 60 <= time.time():
            raise EventError("That time is already over: pick a time in the future")
        services = self._default_services()
        if not services:
            raise EventError("No watched service allows scale_out in vigil.yaml, so there is nothing to get ready")
        event = {"id": f"c-{self._next_id}", "source": "console", "name": name, "at": float(at),
                 "prepare_s": float(prepare_min) * 60, "lasts_s": float(lasts_min) * 60,
                 "services": services}
        self._next_id = self._next_id + 1
        self.added.append(event)
        self._save()
        return event

    def remove(self, event_id: str) -> dict:
        for event in self.added:
            if event["id"] == event_id:
                self.added.remove(event)
                self._save()
                return event
        raise EventError("No such event, or it comes from vigil.yaml (edit that file to change it)")

    def in_window(self, event: dict, now: float) -> bool:
        start = event["at"] - event["prepare_s"]
        end = event["at"] + event["lasts_s"]
        return start <= now < end

    def holds(self, service: str, now: float | None = None) -> bool:
        """Is this service inside an event's window? Then its spare copy stays running."""
        if now is None:
            now = time.time()
        for event in self.all_events():
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
        for event in self.all_events():
            if not self.in_window(event, now):
                continue
            for service in event["services"]:
                key = (event["id"], service)
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
        for event in self.all_events():
            start = event["at"] - event["prepare_s"]
            end = event["at"] + event["lasts_s"]
            if now < start:
                status = "upcoming"
            elif now < end:
                status = "getting ready"
            else:
                status = "over"
            ready = []
            for service in event["services"]:
                if (event["id"], service) in self.prepared and service in self.e.remediator.scaled:
                    ready.append(service)
            out.append({"id": event["id"], "source": event.get("source", "vigil.yaml"), "name": event["name"],
                        "at": event["at"], "prepare_from": start, "until": end,
                        "services": list(event["services"]), "ready": ready, "status": status})
        out.sort(key=event_time)
        return out
