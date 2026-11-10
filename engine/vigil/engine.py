"""The loop: observe -> detect -> correlate -> diagnose -> decide -> act -> verify.

Collection is async (network). Detection and incident bookkeeping run in a worker
thread, one tick at a time, so model math and SQLite writes never stall the API
or the live stream.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from dataclasses import asdict

from .bus import Bus
from .collector import PrometheusCollector, Snapshot
from .config import Settings
from .detector import ServiceDetector, Verdict
from .diagnosis import (SURGE_FACTOR, CATEGORY_LABEL, blame_walk, build_evidence, classify, confidence,
                        evidence_types)
from .incidents import IncidentManager
from .actuator_client import ActuatorClient
from .llm import LLMClient
from .policy import Governor
from .remediation import Remediator
from .store import Store

log = logging.getLogger("vigil.engine")


class Engine:
    def __init__(self, settings: Settings, collector: PrometheusCollector, store: Store, bus: Bus,
                 llm: LLMClient | None = None, actuator: ActuatorClient | None = None,
                 governor: Governor | None = None):
        self.s = settings
        self.collector = collector
        self.store = store
        self.bus = bus
        self.incidents = IncidentManager(store, settings, bus.publish)
        self.detectors = {svc: ServiceDetector(svc, settings) for svc in settings.monitored}
        self.history: deque[dict] = deque(maxlen=settings.history_points)
        self.last: Snapshot | None = None
        self.verdicts: dict[str, Verdict] = {}
        self.telemetry_ok: bool | None = None
        self.last_tick_at: float | None = None
        self.started_at = time.time()
        self._suspect: set[str] = set()
        self.lock = threading.Lock()
        self.llm = llm or LLMClient(settings.groq_api_key, settings.groq_model, timeout_s=settings.llm_timeout_s)
        self.first_flag: dict[str, float] = {}       # when each service's current anomaly began
        self.versions: dict[str, str] = {}
        self.changes: list[dict] = []                # recent version changes (deploys, rollbacks)
        self._due: list[str] = []
        self._diagnosing: set[str] = set()
        self.governor = governor or Governor(autonomy=settings.autonomy)
        self.remediator = Remediator(
            self, actuator or ActuatorClient(settings.actuator_url, settings.actuator_token), self.governor)
        self._due_remediation: list[str] = []
        self._due_scale_in: list[str] = []
        self._ticks = 0
        # After an incident nobody was hurt by is handed to a human, the same service
        # does not open again on a quiet anomaly for a while (it would only repeat the
        # same diagnosis and the same escalation). Anything that hurts users still opens.
        self.quiet_until: dict[str, float] = {}

        for svc, det in self.detectors.items():
            if det.load():
                log.info("loaded baseline for %s (%d samples)", svc, det.baseline.n_samples)
        # Services still in an open incident after a restart must prove recovery
        # (4 clean ticks) before the incident can close.
        for svc in self.incidents.active_services():
            if svc in self.detectors:
                self.detectors[svc].persistence.active = True

    async def run(self) -> None:
        while True:
            started = time.monotonic()
            try:
                await self.tick()
            except Exception:  # one bad tick must never kill the watcher
                log.exception("tick failed")
            await asyncio.sleep(max(0.0, self.s.tick_seconds - (time.monotonic() - started)))

    async def tick(self) -> None:
        snap = await self.collector.collect()
        await asyncio.to_thread(self.process, snap)
        for inc_id in self._due:
            if inc_id not in self._diagnosing:
                self._diagnosing.add(inc_id)
                asyncio.create_task(self._diagnose_guarded(inc_id))
        for inc_id in self._due_remediation:
            if inc_id not in self.remediator.inflight:
                asyncio.create_task(self._remediate_guarded(inc_id))
        for svc in self._due_scale_in:
            asyncio.create_task(self._guarded(self.remediator.scale_in(svc), f"scale-in of {svc}"))
        self._ticks += 1
        if self._ticks % 12 == 1:   # about once a minute: which containers exist, which run
            asyncio.create_task(self._guarded(self.remediator.refresh_inventory(), "inventory refresh"))

    async def _guarded(self, coro, what: str) -> None:
        try:
            await coro
        except Exception:
            log.exception("%s failed", what)

    async def _remediate_guarded(self, inc_id: str) -> None:
        try:
            await self.remediator.handle(inc_id)
        except Exception:
            log.exception("remediation failed for %s", inc_id)

    async def _diagnose_guarded(self, inc_id: str) -> None:
        try:
            await self.diagnose(inc_id)
        except Exception:
            log.exception("diagnosis failed for %s", inc_id)
        finally:
            self._diagnosing.discard(inc_id)

    async def diagnose(self, inc_id: str) -> dict | None:
        """Walk (deterministic) and LLM (independent) in parallel, then compare."""
        prepared = await asyncio.to_thread(self._prepare_diagnosis, inc_id)
        if prepared is None:
            return None
        llm_result = await self.llm.diagnose(prepared["pack"])
        return await asyncio.to_thread(self._apply_diagnosis, inc_id, prepared, llm_result)

    # ---- runs in a worker thread

    def process(self, snap: Snapshot) -> None:
        with self.lock:
            self._process(snap)

    def _process(self, snap: Snapshot) -> None:
        self.last, self.last_tick_at = snap, time.time()
        if snap.telemetry_ok != self.telemetry_ok:
            if self.telemetry_ok is not None or not snap.telemetry_ok:
                ev = self.store.append_event(
                    stage="system", actor="engine",
                    kind="telemetry_ok" if snap.telemetry_ok else "telemetry_degraded",
                    summary="Telemetry restored; decisions resume" if snap.telemetry_ok
                    else f"Telemetry degraded: {snap.problem}. No decisions on stale data",
                    payload={"problem": snap.problem})
                self.bus.publish({"type": "event", "event": ev})
            self.telemetry_ok = snap.telemetry_ok
        if not snap.telemetry_ok:
            self._publish_tick(snap)
            return

        self.incidents.observe_edges(snap)
        self._track_versions(snap)
        active = self.incidents.active_services()
        surge = self.surge()
        for svc, det in self.detectors.items():
            if svc in active:
                det.pause_learning()
            v = det.evaluate(snap.services.get(svc))
            self.verdicts[svc] = v
            if v.flagged:
                self.first_flag.setdefault(svc, snap.ts)
            elif not det.persistence.active:
                self.first_flag.pop(svc, None)
            self._narrate_suspicion(svc, v, det)
            # Busy because of more traffic (or already scaled out for it) is demand,
            # not a fault: open only if users get hurt, never on CPU above a quiet baseline.
            z = v.z or {}
            demand = z.get("rps", 0.0) > self.s.z_flag and z.get("cpu_per_req", 0.0) < 2
            # During a surge of customers every service is busier than its quiet
            # baseline. With nobody hurt that is demand, not a fault: never open on it.
            muted = time.time() < self.quiet_until.get(svc, 0.0) or surge is not None
            change = det.persistence.update(v.flagged, v.hard, impact=self._impact(svc, v, snap),
                                            allow_quiet=svc not in self.remediator.scaled and not demand
                                            and not muted)
            if change == "opened":
                self.incidents.on_service_opened(svc, v, snap, onset=self.first_flag.get(svc))
            elif change == "cleared":
                self.incidents.on_service_cleared(svc)
        self.incidents.on_tick(snap, list(self.verdicts.values()))
        self.remediator.verify_tick(snap, self.verdicts)
        self._due = self.incidents.due_for_diagnosis(time.time())
        self._due_remediation = self.remediator.due()
        self._due_scale_in = self.remediator.scale_in_due(snap)
        self._publish_tick(snap)

    def _impact(self, svc: str, v: Verdict, snap: Snapshot) -> bool:
        """Is anything actually hurting? Failing checkouts, abnormal errors in this
        service, or its calls to others failing. Latency or CPU alone is not enough
        to open fast: a busy laptop slows every service at once and harms no one."""
        if (snap.user.fail_ratio or 0.0) >= self.s.recovered_fail_ratio:
            return True
        if (v.z or {}).get("error_rate", 0.0) >= self.s.z_flag:
            return True
        return any(e.caller == svc and (e.fail_ratio or 0.0) >= self.s.edge_bad_fail_ratio for e in snap.edges)

    def normal_checkout_rps(self) -> float | None:
        """Normal checkouts per second: the learned traffic of the service customers reach first."""
        first = next((b for a, b in self.s.static_edges if a == self.s.user_facing), None)
        det = self.detectors.get(first) if first else None
        return det.baseline.medians.get("rps") if det and det.baseline else None

    def surge(self) -> tuple[float, float] | None:
        """(offered, normal) checkout attempts per second while demand is a surge, else None."""
        normal = self.normal_checkout_rps()
        offered = self.last.user.checkout_rps if self.last else None
        if normal and offered and normal >= self.s.min_rps_for_rates and offered >= SURGE_FACTOR * normal:
            return offered, normal
        return None

    def mute_quiet(self, services: list[str], seconds: float = 900) -> None:
        until = time.time() + seconds
        for svc in services:
            self.quiet_until[svc] = until

    def fresh_start(self) -> dict:
        """Operator's clean slate for a demo: close any open incident, forget the
        learned normal for every service and relearn it. VIGIL's record of which
        fixes worked is kept. Call with faults already cleared."""
        with self.lock:
            closed = []
            for inc_id, inc in list(self.incidents.open.items()):
                if inc["status"] == "OPEN":
                    self.incidents.transition(inc_id, "INVESTIGATING", actor="human", stage="system",
                                              kind="fresh_start", summary="Closing for a fresh start")
                self.incidents.transition(inc_id, "ESCALATED", actor="human", stage="system", kind="fresh_start",
                                          summary=f"{inc_id} closed by the operator for a fresh start")
                closed.append(inc_id)
            self.reset_persistence(list(self.detectors))
            for det in self.detectors.values():
                det.reset()
            self.quiet_until.clear()
            self.first_flag.clear()
            self.governor.history.clear()     # a fresh start gets a fresh action budget
            ev = self.store.append_event(stage="system", actor="human", kind="baseline_reset",
                                         summary=("Fresh start: " + (f"closed {', '.join(closed)}; " if closed else "")
                                                  + "relearning normal for every service (about 3 minutes)"),
                                         payload={"closed": closed, "services": list(self.detectors)})
            self.bus.publish({"type": "event", "event": ev})
        return {"closed": closed, "relearning": list(self.detectors)}

    def reset_persistence(self, services: list[str]) -> None:
        """After an incident closes, a fresh anomaly must open a fresh incident."""
        for svc in services:
            det = self.detectors.get(svc)
            if det:
                det.persistence.active = False
                det.persistence.recent.clear()
                det.persistence.hurting.clear()
                det.persistence.clean_streak = 0
            self.first_flag.pop(svc, None)
            self._suspect.discard(svc)

    def _track_versions(self, snap: Snapshot) -> None:
        """A version change is the most common cause of incidents. Remember every one."""
        for svc, sample in snap.services.items():
            new, old = sample.version, self.versions.get(svc)
            if new and "," in new:
                continue   # mid-switch, two versions briefly serve together: wait for one
            if new and old and new != old:
                change = {"service": svc, "from": old, "to": new, "ts": snap.ts}
                self.changes.append(change)
                ev = self.store.append_event(stage="system", actor="engine", kind="version_changed",
                                             summary=f"{svc} version changed {old} -> {new}", payload=change)
                self.bus.publish({"type": "event", "event": ev})
            if new:
                self.versions[svc] = new
        cutoff = snap.ts - 3600
        self.changes = [c for c in self.changes if c["ts"] >= cutoff]

    # ---- diagnosis (prepare and apply run in a worker thread, under the engine lock)

    def _prepare_diagnosis(self, inc_id: str) -> dict | None:
        with self.lock:
            inc = self.incidents.open.get(inc_id)
            if not inc or self.last is None:
                return None
            services = list(inc["services"])
            onsets = inc["data"].get("onset", {})
            walk = blame_walk(services, self.verdicts, self.last, self.incidents.edges,
                              self.s.edge_bad_fail_ratio, self.s.edge_bad_p95_ms)
            surge = self.surge()
            root_v = self.verdicts.get(walk.root)
            category, why = classify(walk.root, root_v, self.changes,
                                     time.time(), self.s.change_window_s, surge=surge)
            overloaded_down = bool(surge) and root_v is not None and root_v.state == "down"
            medians = {s: d.baseline.medians for s, d in self.detectors.items() if d.baseline}
            pack = build_evidence(inc_id, services, self.verdicts, self.last, medians, onsets, self.changes,
                                  normal_checkout_rps=self.normal_checkout_rps())
            route = " -> ".join(walk.path)
            ev = self.store.append_event(
                incident_id=inc_id, stage="diagnose", actor="walk", kind="blame_walk",
                summary=f"Blame walk: {route}. Stops at {walk.root}: {walk.steps[-1]}",
                payload={**walk.to_dict(), "category": category, "category_why": why})
            self.bus.publish({"type": "event", "event": ev})
            return {"services": services, "onsets": onsets, "walk": walk, "category": category,
                    "category_why": why, "pack": pack, "overloaded_down": overloaded_down,
                    "demand": category == "load_saturation" and surge is not None}

    def _apply_diagnosis(self, inc_id: str, prep: dict, llm: dict) -> dict | None:
        with self.lock:
            walk, category = prep["walk"], prep["category"]
            multi = len(prep["services"]) > 1
            if llm["status"] == "ok":
                text = (f"LLM (never shown the walk) names {llm['root_cause_service']} as "
                        f"{llm['category']}: {llm['hypothesis']}")
            elif llm["status"] == "rejected":
                text = f"LLM answer rejected ({llm['error'][:120]}); walk verdict stands"
            else:
                text = f"LLM unavailable ({llm.get('error', 'unknown')[:120]}); walk verdict stands, confidence capped at Medium"
            ev = self.store.append_event(incident_id=inc_id, stage="diagnose", actor="llm",
                                         kind=f"llm_{llm['status']}", summary=text, payload=llm)
            self.bus.publish({"type": "event", "event": ev})

            types = evidence_types(walk, prep["onsets"], self.s.tick_seconds, multi,
                                   demand=prep.get("demand", False))
            level, reasons = confidence(walk, category, llm, types, multi,
                                        overloaded_down=prep.get("overloaded_down", False))
            symptoms = walk.symptoms
            title = f"{walk.root}: {CATEGORY_LABEL[category]}"
            failing = [s for s in symptoms if s not in walk.cleared]
            fine = [s for s in symptoms if s in walk.cleared]
            if failing:
                title += f"; {', '.join(failing)} failing as a result"
            elif fine:
                title += f"; {', '.join(fine)} {'busy too' if category == 'load_saturation' else 'not the cause'}"
            diagnosis = {
                "services": prep["services"], "root": walk.root, "category": category,
                "category_why": prep["category_why"], "confidence": level, "confidence_reasons": reasons,
                "evidence_types": types, "walk": walk.to_dict(), "llm": llm, "symptoms": symptoms,
                "evidence": prep["pack"]["evidence"], "title": title, "at": time.time(),
            }
            symptom_text = "".join(f" {s} is a symptom: {why}." for s, why in symptoms.items())
            summary = (f"Root cause: {walk.root} ({CATEGORY_LABEL[category]}): {prep['category_why']}. "
                       f"Confidence {level}.{symptom_text}")
            self.incidents.record_diagnosis(inc_id, diagnosis, summary)
            return diagnosis

    def _narrate_suspicion(self, svc: str, v: Verdict, det: ServiceDetector) -> None:
        """Say so when VIGIL starts watching something, before it is confirmed."""
        z = v.z or {}
        demand = z.get("rps", 0.0) > self.s.z_flag and z.get("cpu_per_req", 0.0) < 2
        if v.flagged and demand and not v.hard and self.last is not None and not self._impact(svc, v, self.last):
            return   # busy from real traffic with nobody hurt: not worth a "watching" line every minute
        if v.flagged and not det.persistence.active and svc not in self._suspect:
            self._suspect.add(svc)
            pct = ""
            if v.score_pct and v.score_pct >= 99.95:
                pct = " (beyond anything in its baseline)"
            elif v.score_pct and v.score_pct >= 99:
                pct = f" (more unusual than {v.score_pct:.1f}% of normal)"
            ev = self.store.append_event(
                stage="detect", actor="detector", kind="suspect",
                summary=f"{svc}: {', '.join(v.reasons) or v.state}{pct}. Watching for confirmation",
                payload={"service": svc, "z": v.z, "reasons": v.reasons})
            self.bus.publish({"type": "event", "event": ev})
        elif not v.flagged:
            self._suspect.discard(svc)

    def _publish_tick(self, snap: Snapshot) -> None:
        point = {
            "ts": snap.ts,
            "telemetry_ok": snap.telemetry_ok,
            "services": {s: {**asdict(x), "state": self.verdicts.get(s).state if s in self.verdicts else None}
                         for s, x in snap.services.items()},
            "user": asdict(snap.user),
            "edges": [asdict(e) for e in snap.edges],
        }
        self.history.append(point)
        self.bus.publish({"type": "tick", "point": point, "verdicts": self.verdicts_view()})

    # ---- read models for the API

    def verdicts_view(self) -> dict:
        out = {}
        surge = self.surge()
        served = self.last is not None and (self.last.user.fail_ratio or 0.0) < self.s.recovered_fail_ratio
        for svc, det in self.detectors.items():
            v = self.verdicts.get(svc)
            out[svc] = {
                **(asdict(v) if v else {"state": "unknown"}),
                # Busy from a surge of customers while every checkout is served: demand, not a fault.
                "demand": bool(surge and served and v is not None and v.flagged and v.state != "down"),
                "baseline": None if det.baseline is None else {
                    "samples": det.baseline.n_samples, "created_at": det.baseline.created_at,
                    "medians": {k: round(x, 3) for k, x in det.baseline.medians.items()}},
                "in_incident": self.incidents.incident_of(svc),
            }
        return out

    def health(self) -> dict:
        age = None if self.last_tick_at is None else round(time.time() - self.last_tick_at, 1)
        return {
            "status": "ok" if self.telemetry_ok and age is not None and age < 3 * self.s.tick_seconds else "degraded",
            "telemetry_ok": self.telemetry_ok,
            "problem": self.last.problem if self.last else "no tick yet",
            "last_tick_age_s": age,
            "uptime_s": round(time.time() - self.started_at),
            "edges": sorted(f"{a}->{b}" for a, b in self.incidents.edges),
            "llm": "on" if self.llm.enabled else "off (no GROQ_API_KEY): diagnoses capped at Medium",
            "control": self.control(),
        }

    def control(self) -> dict:
        return {"autonomy": self.governor.autonomy, "paused": self.governor.paused,
                "guardrails": self.governor.g.__dict__}
