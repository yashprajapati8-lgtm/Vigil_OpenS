"""Incidents: one incident per connected failure, with a strict lifecycle.

Correlation: when a service turns anomalous and a service it calls (or that calls
it) is already in an open incident, it joins that incident instead of opening a
second one. Three alerts become one incident.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from .collector import Snapshot
from .config import Settings
from .detector import Verdict
from .store import TERMINAL, Store

log = logging.getLogger("vigil.incidents")

# Every legal move. Anything else is a bug and is refused.
ALLOWED: dict[str, set[str]] = {
    "OPEN": {"INVESTIGATING", "SELF_RESOLVED"},
    "INVESTIGATING": {"DIAGNOSED", "SELF_RESOLVED", "ESCALATED"},
    "DIAGNOSED": {"AWAITING_APPROVAL", "REMEDIATING", "SELF_RESOLVED", "ESCALATED"},
    "AWAITING_APPROVAL": {"REMEDIATING", "SELF_RESOLVED", "ESCALATED"},
    "REMEDIATING": {"VERIFYING", "ESCALATED"},
    "VERIFYING": {"RESOLVED", "REMEDIATING", "ESCALATED"},
}
SELF_RESOLVABLE = {"OPEN", "INVESTIGATING", "DIAGNOSED", "AWAITING_APPROVAL"}
SEVERITY_ORDER = ["minor", "major", "critical"]


class IllegalTransition(Exception):
    pass


def fmt_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def severity_for(snap: Snapshot, verdicts: list[Verdict]) -> tuple[str, str]:
    """Severity is what users feel, not how loud a metric is."""
    fail = snap.user.fail_ratio or 0.0
    p95 = snap.user.p95_ms or 0.0
    if fail >= 0.10:
        return "critical", f"{fail:.0%} of checkouts failing"
    if fail >= 0.02 or p95 >= 1000 or any(v.state == "down" for v in verdicts):
        why = f"{fail:.0%} of checkouts failing" if fail >= 0.02 else (
            f"checkout p95 {p95 / 1000:.1f}s" if p95 >= 1000 else "a service is down")
        return "major", why
    return "minor", "no user impact yet"


class IncidentManager:
    def __init__(self, store: Store, settings: Settings, publish: Callable[[dict], None]):
        self.store = store
        self.s = settings
        self.publish = publish
        self.edges: set[tuple[str, str]] = set(settings.static_edges)
        self.open: dict[str, dict] = {i["id"]: i for i in store.open_incidents()}

    # ---- graph

    def observe_edges(self, snap: Snapshot) -> None:
        for e in snap.edges:
            if e.rps:
                self.edges.add((e.caller, e.target))

    def neighbors(self, svc: str) -> set[str]:
        return {b for a, b in self.edges if a == svc} | {a for a, b in self.edges if b == svc}

    def incident_of(self, svc: str) -> str | None:
        for inc_id, inc in self.open.items():
            if svc in inc["services"]:
                return inc_id
        return None

    def active_services(self) -> set[str]:
        return {s for inc in self.open.values() for s in inc["data"].get("active", [])}

    # ---- lifecycle

    def _emit(self, ev: dict | None) -> None:
        if ev:
            self.publish({"type": "event", "event": ev})

    def _refresh(self, inc_id: str) -> dict:
        inc = self.store.get_incident(inc_id)
        if inc["status"] in TERMINAL:
            self.open.pop(inc_id, None)
        else:
            self.open[inc_id] = inc
        self.publish({"type": "incident", "incident": inc})
        return inc

    def transition(self, inc_id: str, new_status: str, *, actor: str, stage: str, kind: str,
                   summary: str, payload: dict | None = None, fields: dict | None = None) -> dict:
        inc = self.open.get(inc_id) or self.store.get_incident(inc_id)
        if new_status not in ALLOWED.get(inc["status"], set()):
            raise IllegalTransition(f"{inc_id}: {inc['status']} -> {new_status}")
        changes = {"status": new_status, **(fields or {})}
        if new_status in TERMINAL:
            changes["closed_at"] = time.time()
        ev = self.store.update_incident(inc_id, changes, {
            "stage": stage, "actor": actor, "kind": kind, "summary": summary,
            "payload": {"from": inc["status"], "to": new_status, **(payload or {})},
        })
        self._emit(ev)
        return self._refresh(inc_id)

    def on_service_opened(self, svc: str, verdict: Verdict, snap: Snapshot, onset: float | None = None) -> dict:
        evidence = {"reasons": verdict.reasons, "z": verdict.z, "score_pct": verdict.score_pct,
                    "time_to_oom_s": verdict.time_to_oom_s, "state": verdict.state}
        why = ", ".join(verdict.reasons) or verdict.state
        onset = onset or snap.ts
        existing = self.incident_of(svc)
        if existing:  # flapped back inside its own incident
            inc = self.open[existing]
            active = sorted(set(inc["data"].get("active", [])) | {svc})
            self.store.update_incident(existing, {"data": {**inc["data"], "active": active}})
            return self._refresh(existing)

        linked = next((i for i, inc in self.open.items()
                       if self.neighbors(svc) & set(inc["services"])), None)
        if linked:
            inc = self.open[linked]
            services = inc["services"] + [svc]
            data = {**inc["data"], "active": sorted(set(inc["data"].get("active", [])) | {svc}),
                    "onset": {**inc["data"].get("onset", {}), svc: onset}}
            via = sorted(self.neighbors(svc) & set(inc["services"]))
            fields = {"services": services, "data": data}
            if not inc["data"].get("diagnosis"):   # never overwrite a diagnosed title
                fields["title"] = f"Anomaly across {' and '.join(services)}"
            ev = self.store.update_incident(linked, fields, {"stage": "detect", "actor": "engine", "kind": "service_joined",
                "summary": f"{svc} joined {linked}: {why} (connected to {', '.join(via)})",
                "payload": {"service": svc, "via": via, **evidence}})
            self._emit(ev)
            return self._refresh(linked)

        severity, sev_why = severity_for(snap, [verdict])
        inc, ev = self.store.create_incident(
            services=[svc], title=f"Anomaly in {svc}", severity=severity,
            event={"stage": "detect", "actor": "detector", "kind": "incident_opened",
                   "summary": f"Incident {{id}} opened: {svc} {why}; {sev_why}",
                   "payload": {"service": svc, **evidence}},
        )
        self.store.update_incident(inc["id"], {"data": {"active": [svc], "failed_checkouts": 0.0,
                                                        "peak_fail_ratio": 0.0, "onset": {svc: onset}}})
        self._emit(ev)
        self._refresh(inc["id"])
        return self.transition(inc["id"], "INVESTIGATING", actor="engine", stage="incident",
                               kind="investigating", summary="Investigating: collecting evidence across services")

    def on_service_cleared(self, svc: str) -> dict | None:
        inc_id = self.incident_of(svc)
        if not inc_id:
            return None
        inc = self.open[inc_id]
        active = [s for s in inc["data"].get("active", []) if s != svc]
        ev = self.store.update_incident(inc_id, {"data": {**inc["data"], "active": active}}, {
            "stage": "verify", "actor": "detector", "kind": "service_recovered",
            "summary": f"{svc} back inside its normal band", "payload": {"service": svc}})
        self._emit(ev)
        inc = self._refresh(inc_id)
        if not active and inc["status"] in SELF_RESOLVABLE:
            took = fmt_duration(time.time() - inc["opened_at"])
            lost = int(round(inc["data"].get("failed_checkouts", 0)))
            return self.transition(
                inc_id, "SELF_RESOLVED", actor="engine", stage="verify", kind="self_resolved",
                summary=f"{inc_id} recovered on its own after {took} with no action; {lost} checkouts failed",
                payload={"duration_s": time.time() - inc["opened_at"], "failed_checkouts": lost})
        return inc

    def on_tick(self, snap: Snapshot, verdicts: list[Verdict]) -> None:
        """Track user impact and escalate severity while incidents are open."""
        if not self.open:
            return
        fail = snap.user.fail_ratio or 0.0
        failed_now = fail * (snap.user.checkout_rps or 0.0) * self.s.tick_seconds
        severity, why = severity_for(snap, verdicts)
        for inc_id, inc in list(self.open.items()):
            data = {**inc["data"],
                    "failed_checkouts": inc["data"].get("failed_checkouts", 0.0) + failed_now,
                    "peak_fail_ratio": max(inc["data"].get("peak_fail_ratio", 0.0), fail)}
            fields: dict = {"data": data}
            event = None
            if SEVERITY_ORDER.index(severity) > SEVERITY_ORDER.index(inc["severity"]):
                fields["severity"] = severity
                event = {"stage": "detect", "actor": "engine", "kind": "severity_raised",
                         "summary": f"{inc_id} severity {severity}: {why}", "payload": {"severity": severity}}
            ev = self.store.update_incident(inc_id, fields, event)
            self._emit(ev)
            self.open[inc_id] = {**inc, **{k: v for k, v in fields.items()}}
            if ev:
                self.publish({"type": "incident", "incident": self.open[inc_id]})

    # ---- diagnosis hooks

    def due_for_diagnosis(self, now: float) -> list[str]:
        """Incidents ready for (re)diagnosis: settled for a moment, or the picture changed."""
        due = []
        for inc_id, inc in self.open.items():
            diag = inc["data"].get("diagnosis")
            if inc["status"] == "INVESTIGATING" and not diag and now - inc["opened_at"] >= self.s.diagnose_delay_s:
                due.append(inc_id)
            elif inc["status"] == "DIAGNOSED" and diag and sorted(diag["services"]) != sorted(inc["services"]):
                due.append(inc_id)
        return due

    def record_diagnosis(self, inc_id: str, diagnosis: dict, summary: str) -> dict | None:
        inc = self.open.get(inc_id)
        if not inc or inc["status"] not in ("INVESTIGATING", "DIAGNOSED"):
            return None  # resolved or moved on while the LLM was thinking
        fields = {"root": diagnosis["root"], "category": diagnosis["category"],
                  "title": diagnosis["title"], "data": {**inc["data"], "diagnosis": diagnosis}}
        payload = {"root": diagnosis["root"], "category": diagnosis["category"],
                   "confidence": diagnosis["confidence"]}
        if inc["status"] == "INVESTIGATING":
            return self.transition(inc_id, "DIAGNOSED", actor="engine", stage="diagnose", kind="diagnosed",
                                   summary=summary, payload=payload, fields=fields)
        ev = self.store.update_incident(inc_id, fields, {
            "stage": "diagnose", "actor": "engine", "kind": "rediagnosed",
            "summary": f"Re-diagnosed after new evidence: {summary}", "payload": payload})
        self._emit(ev)
        return self._refresh(inc_id)
