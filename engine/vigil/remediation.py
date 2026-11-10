"""Decide -> act -> verify, with a human in the loop when trust is not earned.

Success is never "the command returned 200". An incident is RESOLVED only when
every affected service AND the user-facing checkout signal stay normal for 4
checks in a row. If they do not, VIGIL tries the next allowed fix or escalates,
and it never loops: cooldowns, a per-incident attempt cap and a global action
budget stop it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from .actuator_client import ActuatorClient, ActuatorError
from .diagnosis import CATEGORY_LABEL
from .incidents import fmt_duration
from .policy import Governor, Option, new_action_id, options_table, price_options

if TYPE_CHECKING:
    from .engine import Engine

log = logging.getLogger("vigil.remediation")

RECOMMEND_WITHOUT_FIX = {
    "load_saturation": "add capacity: start another {root} replica",
    "regression_after_change": "roll {root} back to its previous version",
    "unknown": "a human should investigate {root}",
}


class Remediator:
    def __init__(self, engine: "Engine", actuator: ActuatorClient, governor: Governor):
        self.e = engine
        self.actuator = actuator
        self.gov = governor
        self.inflight: set[str] = set()
        self.inventory: list[dict] = []          # managed containers, refreshed from the actuator
        self.scaled: dict[str, dict] = {}        # service -> replica VIGIL started {container, since, incident}
        self._calm: dict[str, int] = {}          # ticks of normal traffic while scaled out
        self._scaling_in: set[str] = set()

    # ---- helpers (call with the engine lock held)

    def _event(self, inc_id: str | None, stage: str, actor: str, kind: str, summary: str, payload=None) -> dict:
        ev = self.e.store.append_event(incident_id=inc_id, stage=stage, actor=actor, kind=kind,
                                       summary=summary, payload=payload or {})
        self.e.bus.publish({"type": "event", "event": ev})
        return ev

    def _set_data(self, inc_id: str, **changes) -> dict:
        inc = self.e.incidents.open[inc_id]
        data = {**inc["data"], **changes}
        self.e.store.update_incident(inc_id, {"data": data})
        return self.e.incidents._refresh(inc_id)

    def _impact(self) -> tuple[float, float]:
        snap = self.e.last
        rps = (snap.user.checkout_rps or 0.0) if snap else 0.0
        fail = (snap.user.fail_ratio or 0.0) if snap else 0.0
        return fail * rps * 60, rps

    async def refresh_inventory(self) -> None:
        """What containers exist and which are running. Also adopts a replica that is
        already running (say, after VIGIL itself restarted) so it still scales in."""
        try:
            found = await self.actuator.containers()
        except ActuatorError:
            return
        with self.e.lock:
            self.inventory = found
            for c in found:
                if c.get("role") == "replica" and c.get("status") == "running" and c.get("service"):
                    self.scaled.setdefault(c["service"], {"container": c["name"], "since": time.time(), "incident": None})
            for svc, info in list(self.scaled.items()):
                if not any(c["name"] == info["container"] and c.get("status") == "running" for c in found):
                    self.scaled.pop(svc, None)

    def _stuck_primaries(self, service: str) -> list[str]:
        """Primary instances that stopped answering: exited, or failing their health
        check. An instance overloaded long enough can stay wedged on its backlog even
        after a replica takes half the traffic; only a restart clears that."""
        cur = self.e.versions.get(service)
        return [c["name"] for c in self.inventory if c.get("service") == service and c.get("role") != "replica"
                and (cur is None or c.get("version") in (None, cur))
                and (c.get("status") != "running" or c.get("health") == "unhealthy")]

    def _spare_for(self, service: str) -> dict | None:
        if service in self.scaled:
            return None
        return next((c for c in self.inventory if c.get("service") == service and c.get("role") == "replica"
                     and c.get("status") != "running"), None)

    # ---- what needs attention this tick

    def due(self) -> list[str]:
        """Diagnosed incidents still needing a decision, or a retry after failed verification."""
        out = []
        for inc_id, inc in self.e.incidents.open.items():
            if inc_id in self.inflight:
                continue
            d = inc["data"].get("decision")
            if inc["status"] == "DIAGNOSED" and (d is None or d.get("status") == "blocked"):
                out.append(inc_id)
            elif inc["status"] == "VERIFYING" and inc["data"].get("verify", {}).get("result") == "failed":
                out.append(inc_id)
        return out

    # ---- decide

    def _plan(self, inc_id: str) -> dict | None:
        with self.e.lock:
            inc = self.e.incidents.open.get(inc_id)
            if not inc:
                return None
            diag = inc["data"]["diagnosis"]
            fail_per_min, rps = self._impact()
            # A leak hurts in the future: once the process hits its memory limit it is
            # killed and every checkout fails until something restarts it. Price that
            # coming outage, not just today's (often zero) failures.
            projected = None
            v = self.e.verdicts.get(diag["root"])
            horizon_s = self.e.s.horizon_min * 60
            if v is not None and v.time_to_oom_s and 0 < v.time_to_oom_s < horizon_s:
                outage = rps * 60 * (1 - v.time_to_oom_s / horizon_s)
                if outage > fail_per_min:
                    fail_per_min = outage
                    projected = (f"{diag['root']} hits its memory limit in ~{v.time_to_oom_s:.0f} s; "
                                 "after that every checkout fails")
            now = time.time()
            last_change = next((c for c in reversed(self.e.changes)
                                if c["service"] == diag["root"] and now - c["ts"] <= self.e.s.change_window_s), None)
            options = price_options(
                root=diag["root"], category=diag["category"], symptoms=list(diag.get("symptoms", {})),
                fail_per_min=fail_per_min, checkout_rps=rps, horizon_min=self.e.s.horizon_min,
                restart_downtime_s=self.e.s.restart_downtime_s, history=self.e.store.outcome_history(),
                last_change=last_change, spare=self._spare_for(diag["root"]) is not None,
                root_down=(v is not None and v.state == "down") or bool(self._stuck_primaries(diag["root"])))
            attempts = len(inc["data"].get("actions", []))
            tried = {(a["action"], a["target"]) for a in inc["data"].get("actions", [])}
            viable = [o for o in options if o.action != "wait" and o.executable and o.score > 0
                      and (o.action, o.target) not in tried]
            chosen = viable[0] if viable else None
            blockers = self.gov.blockers(chosen, attempts, bool(self.e.telemetry_ok)) if chosen else []
            alone, why_alone = self.gov.may_act_alone(chosen, diag["confidence"]) if chosen else (False, "")
            return {"inc": inc, "diag": diag, "options": options, "chosen": chosen, "blockers": blockers,
                    "alone": alone, "why_alone": why_alone, "fail_per_min": fail_per_min, "attempts": attempts,
                    "projected": projected}

    def _options_text(self, plan: dict) -> str:
        parts = []
        for o in plan["options"]:
            if o.action == "wait":
                parts.append("wait saves 0")
            else:
                tag = " (symptom)" if not o.executable else ""
                parts.append(f"{o.label()}{tag} saves ~{o.saves:,.0f} (score {o.score:,.0f})")
        return " · ".join(parts)

    async def handle(self, inc_id: str) -> None:
        """Decide for a diagnosed incident (or retry after a failed verification)."""
        self.inflight.add(inc_id)
        try:
            await self._handle(inc_id)
        finally:
            self.inflight.discard(inc_id)

    async def _handle(self, inc_id: str) -> None:
        await self.refresh_inventory()
        plan = self._plan(inc_id)
        if plan is None:
            return
        inc, diag, chosen = plan["inc"], plan["diag"], plan["chosen"]
        retry = inc["status"] == "VERIFYING"

        with self.e.lock:
            age = time.time() - inc["opened_at"]
            if chosen is None and not retry and plan["fail_per_min"] < 1 and age < self.e.s.watch_without_impact_s:
                # Nothing VIGIL can safely do, and no checkout is failing: don't page a
                # human for a blip. Keep watching; it either clears (self-resolved) or
                # starts hurting users (then the normal path decides or escalates).
                reason = (f"no checkout is failing, so VIGIL keeps watching before calling a human "
                          f"(up to {self.e.s.watch_without_impact_s / 60:.0f} min)")
                if (inc["data"].get("decision") or {}).get("reason") != reason:
                    self._event(inc_id, "decide", "policy", "watching",
                                f"No safe automatic fix for {CATEGORY_LABEL[diag['category']]}, but {reason}.", {})
                    self._set_data(inc_id, decision={"status": "blocked", "reason": reason,
                                                     "options": options_table(plan["options"])})
                return
            if chosen is None:
                rec = RECOMMEND_WITHOUT_FIX.get(diag["category"], "a human should investigate {root}").format(root=diag["root"])
                if (diag["category"] == "load_saturation" and diag["root"] not in self.scaled
                        and not any(c.get("service") == diag["root"] and c.get("role") == "replica"
                                    for c in self.inventory)):
                    rec += (f" (there is no warm {diag['root']} spare to start; create one with "
                            f"`docker compose --profile replica create {diag['root']}-replica`)")
                why = "previous fix did not restore service" if retry else f"no safe automatic fix for {CATEGORY_LABEL[diag['category']]} in this build"
                self._escalate(inc_id, f"{why}. Recommended: {rec}", plan)
                return
            if plan["blockers"]:
                reason = "; ".join(plan["blockers"])
                if retry or plan["attempts"] >= self.gov.g.max_attempts:
                    self._escalate(inc_id, f"cannot try {chosen.label()}: {reason}", plan)
                    return
                prev = (inc["data"].get("decision") or {}).get("reason")
                if prev != reason:  # say it once, not every tick
                    self._event(inc_id, "decide", "policy", "action_blocked",
                                f"Would {chosen.label()}, but {reason}. Holding.", {"blockers": plan["blockers"]})
                self._set_data(inc_id, decision={"status": "blocked", "reason": reason,
                                                  "options": options_table(plan["options"])})
                return

            decision = {"status": "chosen", "action": chosen.action, "target": chosen.target,
                        "options": options_table(plan["options"]), "fail_per_min": round(plan["fail_per_min"]),
                        "projected": plan.get("projected"),
                        "horizon_min": self.e.s.horizon_min, "acted_alone": plan["alone"],
                        "why": plan["why_alone"], "autonomy": self.gov.autonomy}
            rate = (f"~{plan['fail_per_min']:,.0f} failed checkouts/min projected ({plan['projected']})"
                    if plan.get("projected") else f"~{plan['fail_per_min']:,.0f} failed checkouts/min")
            headline = f"Decision: {chosen.label()}. Next {self.e.s.horizon_min:.0f} min at {rate}: {self._options_text(plan)}"
            self._event(inc_id, "decide", "policy", "decided", headline, decision)
            self._set_data(inc_id, decision=decision)

            if not plan["alone"] and not retry:
                self.e.incidents.transition(
                    inc_id, "AWAITING_APPROVAL", actor="policy", stage="decide", kind="awaiting_approval",
                    summary=f"Waiting for approval to {chosen.label()}: {plan['why_alone']}",
                    payload={"action": chosen.action, "target": chosen.target})
                return
            if retry:
                self.e.incidents.transition(inc_id, "REMEDIATING", actor="policy", stage="act", kind="retrying",
                                            summary=f"First fix did not hold. Trying {chosen.label()}",
                                            payload={"action": chosen.action, "target": chosen.target})
            else:
                self.e.incidents.transition(inc_id, "REMEDIATING", actor="policy", stage="act", kind="acting",
                                            summary=f"Acting alone: {plan['why_alone']}",
                                            payload={"action": chosen.action, "target": chosen.target})
        await self._execute(inc_id, chosen, actor="executor")

    # ---- human in the loop

    async def approve(self, inc_id: str, who: str = "human") -> dict:
        with self.e.lock:
            inc = self.e.incidents.open.get(inc_id)
            if not inc or inc["status"] != "AWAITING_APPROVAL":
                raise ValueError("incident is not waiting for approval")
            d = inc["data"]["decision"]
            option = next(Option(**{k: v for k, v in o.items() if k != "label"}) for o in d["options"]
                          if o["action"] == d["action"] and o["target"] == d["target"])
            blockers = [b for b in self.gov.blockers(option, len(inc["data"].get("actions", [])),
                                                     bool(self.e.telemetry_ok))]
            if blockers:
                raise ValueError("; ".join(blockers))
            self.e.incidents.transition(inc_id, "REMEDIATING", actor="human", stage="act", kind="approved",
                                        summary=f"Approved by {who}: {option.label()}",
                                        payload={"action": option.action, "target": option.target, "by": who})
        await self._execute(inc_id, option, actor="executor")
        return self.e.store.get_incident(inc_id)

    def reject(self, inc_id: str, who: str = "human") -> dict:
        with self.e.lock:
            inc = self.e.incidents.open.get(inc_id)
            if not inc or inc["status"] != "AWAITING_APPROVAL":
                raise ValueError("incident is not waiting for approval")
            return self.e.incidents.transition(
                inc_id, "ESCALATED", actor="human", stage="decide", kind="rejected",
                summary=f"{who} rejected the proposed fix and took over {inc_id}", payload={"by": who})

    # ---- act

    async def _execute(self, inc_id: str, option: Option, actor: str) -> None:
        action_id = new_action_id()
        with self.e.lock:
            self.gov.record(option)
            inc = self.e.incidents.open.get(inc_id)
            actions = inc["data"].get("actions", []) + [{"id": action_id, "action": option.action,
                                                         "target": option.target, "started_at": time.time()}]
            self._set_data(inc_id, actions=actions)
            doing = {"rollback": f"Rolling {option.target} back to {option.version}",
                     "scale_out": f"Adding a {option.target} replica"}.get(option.action, f"Restarting {option.target}")
            self._event(inc_id, "act", actor, "action_started", f"{doing} (action {action_id[:8]})",
                        {"action_id": action_id, "action": option.action, "target": option.target,
                         "version": option.version})
        try:
            managed = await self.actuator.containers()
            if option.action == "rollback":
                results = await self._rollback(action_id, option, managed)
            elif option.action == "scale_out":
                results = await self._scale_out(action_id, option, managed, inc_id)
            else:
                # Only what is running now: restarting a stopped old version would
                # quietly put two versions behind the same name. A replica VIGIL started
                # for extra room is never the patient: faults live in the primary.
                mine = [c for c in managed if c["service"] == option.target]
                primaries = [c for c in mine if c.get("role") != "replica"]
                targets = [c["name"] for c in primaries if c.get("status") == "running"]
                if not targets and not primaries:
                    targets = [c["name"] for c in mine if c.get("status") == "running"]
                if not targets:
                    # Nothing running: it crashed (say, killed at its memory limit). Bring
                    # back the instance of the version that was serving, never an old one.
                    cur = self.e.versions.get(option.target)
                    crashed = [c["name"] for c in managed if c["service"] == option.target and c.get("role") != "replica"
                               and (cur is None or c.get("version") in (None, cur))]
                    targets = crashed[:1]
                if not targets:
                    raise ActuatorError(f"no running managed container for {option.target}")
                results = [await self.actuator.act(f"{action_id}-{i}", option.action, name)
                           for i, name in enumerate(targets)]
        except ActuatorError as exc:
            with self.e.lock:
                self._event(inc_id, "act", actor, "action_failed", f"{option.label()} failed: {exc}",
                            {"action_id": action_id, "error": str(exc)})
                diag = self.e.incidents.open[inc_id]["data"]["diagnosis"]
                self.e.store.record_outcome(inc_id, diag["category"], option.action, option.target, False)
                self._escalate(inc_id, f"the fix could not be executed ({exc})", None)
            return

        with self.e.lock:
            took = sum(r["duration_ms"] for r in results) / 1000
            now = time.time()
            names = [r["container"] for r in results]
            where = "" if names == [option.target] else f" ({', '.join(names)})"
            if option.action == "rollback":
                done = f"{option.target} rolled back to {option.version} in {took:.1f} s{where}"
            elif option.action == "scale_out":
                done = f"{option.target} scaled out in {took:.1f} s: {names[0]} is serving too"
            else:
                done = f"{option.target} restarted in {took:.1f} s{where}"
            self._event(inc_id, "act", actor, "action_done", done,
                        {"action_id": action_id, "results": results})
            verify = {"started_at": now, "settle_until": now + self.e.s.verify_settle_s,
                      "deadline": now + self.e.s.verify_timeout_s, "clean": 0, "result": "pending",
                      "action": option.action, "target": option.target}
            self._set_data(inc_id, verify=verify)
            self.e.incidents.transition(
                inc_id, "VERIFYING", actor="verifier", stage="verify", kind="verifying",
                summary=(f"Verifying: {', '.join(self.e.incidents.open[inc_id]['services'])} and checkouts must stay "
                         f"normal for {self.e.s.verify_clean_ticks} checks in a row (up to {self.e.s.verify_timeout_s:.0f} s)"))

    async def _rollback(self, action_id: str, option: Option, managed: list[dict]) -> list[dict]:
        """Blue/green back: start the previous version, wait until it is healthy, then
        stop the bad one. Users keep being served by something the whole time."""
        mine = [c for c in managed if c["service"] == option.target]
        prev = next((c for c in mine if c.get("version") == option.version), None)
        if prev is None:
            raise ActuatorError(f"no managed container for {option.target} {option.version}")
        bad = [c for c in mine if c.get("version") != option.version and c.get("status") == "running"]
        results = []
        if prev.get("status") != "running":
            results.append(await self.actuator.act(f"{action_id}-start", "start", prev["name"]))
        await self._wait_ready(prev["name"], "left the current version running")
        for i, c in enumerate(bad):
            results.append(await self.actuator.act(f"{action_id}-stop{i}", "stop", c["name"]))
        return results

    async def _wait_ready(self, name: str, fallback: str) -> None:
        deadline = time.monotonic() + self.e.s.rollback_ready_s
        while True:
            now_c = next((c for c in await self.actuator.containers() if c["name"] == name), None)
            if now_c and now_c.get("status") == "running" and now_c.get("health") in (None, "healthy"):
                return
            if time.monotonic() > deadline:
                raise ActuatorError(f"{name} did not become healthy in {self.e.s.rollback_ready_s:.0f} s; {fallback}")
            await asyncio.sleep(1)

    async def _scale_out(self, action_id: str, option: Option, managed: list[dict], inc_id: str) -> list[dict]:
        """Start the warm spare. It joins the same service name, so traffic spreads
        across both instances as soon as it is healthy."""
        spare = next((c for c in managed if c.get("service") == option.target and c.get("role") == "replica"
                      and c.get("status") != "running"), None)
        if spare is None:
            raise ActuatorError(f"no stopped {option.target} replica to start")
        result = await self.actuator.act(f"{action_id}-start", "start", spare["name"])
        await self._wait_ready(spare["name"], "stopped trying; the original instance keeps serving")
        with self.e.lock:
            self.scaled[option.target] = {"container": spare["name"], "since": time.time(), "incident": inc_id}
            self._calm[option.target] = 0
        return [result]

    def scale_in_due(self, snap) -> list[str]:
        """Scaled-out services whose traffic has been back near normal for a while,
        with no open incident on them. Runs every tick under the engine lock."""
        due = []
        for svc in list(self.scaled):
            if svc in self._scaling_in or self.gov.paused:
                continue
            det = self.e.detectors.get(svc)
            base = det.baseline.medians.get("rps") if det and det.baseline else None
            rps = snap.services.get(svc).rps if snap.services.get(svc) else None
            busy = any(svc in inc["services"] for inc in self.e.incidents.open.values())
            calm = (not busy and base is not None and rps is not None and rps <= base * self.e.s.scale_in_load_ratio)
            self._calm[svc] = self._calm.get(svc, 0) + 1 if calm else 0
            if self._calm[svc] >= self.e.s.scale_in_calm_ticks:
                due.append(svc)
        return due

    async def scale_in(self, svc: str) -> None:
        with self.e.lock:
            info = self.scaled.get(svc)
            if not info or svc in self._scaling_in:
                return
            self._scaling_in.add(svc)
        try:
            result = await self.actuator.act(new_action_id(), "stop", info["container"])
            with self.e.lock:
                self.scaled.pop(svc, None)
                self._calm.pop(svc, None)
                calm_s = self.e.s.scale_in_calm_ticks * self.e.s.tick_seconds
                self._event(info.get("incident"), "act", "executor", "scaled_in",
                            f"Traffic back to normal for {calm_s:.0f} s: stopped {info['container']} "
                            f"({result.get('duration_ms', 0) / 1000:.1f} s). {svc} is back to one instance",
                            {"service": svc, "container": info["container"]})
        except ActuatorError as exc:
            with self.e.lock:
                self._event(info.get("incident"), "act", "executor", "action_failed",
                            f"Could not scale {svc} back in: {exc}", {"service": svc})
        finally:
            with self.e.lock:
                self._scaling_in.discard(svc)

    # ---- verify (runs every tick inside the engine's processing, lock held)

    def verify_tick(self, snap, verdicts) -> None:
        now = time.time()
        for inc_id, inc in list(self.e.incidents.open.items()):
            v = inc["data"].get("verify")
            if inc["status"] != "VERIFYING" or not v or v["result"] != "pending" or now < v["settle_until"]:
                continue
            fail = snap.user.fail_ratio
            if v["action"] == "scale_out" or inc["data"].get("diagnosis", {}).get("category") == "load_saturation":
                # Demand is still high by design; capacity is what was fixed. Users
                # served and nothing down is recovery (also after restarting an
                # instance that wedged under the surge: it stays busy while the sale runs).
                services_ok = all(verdicts.get(s) is not None and verdicts[s].state != "down" for s in inc["services"])
            else:
                services_ok = all(verdicts.get(s) is not None and verdicts[s].state == "healthy"
                                  for s in inc["services"])
            users_ok = fail is not None and fail <= self.e.s.recovered_fail_ratio
            clean = v["clean"] + 1 if (services_ok and users_ok) else 0
            if clean >= self.e.s.verify_clean_ticks:
                self._resolve(inc_id, inc, snap, verdicts)
            elif now > v["deadline"]:
                self._set_data(inc_id, verify={**v, "clean": clean, "result": "failed"})
                diag = inc["data"]["diagnosis"]
                self.e.store.record_outcome(inc_id, diag["category"], v["action"], v["target"], False)
                bad = [s for s in inc["services"] if not (verdicts.get(s) and verdicts[s].state == "healthy")]
                self._event(inc_id, "verify", "verifier", "verify_failed",
                            f"Not recovered after {self.e.s.verify_timeout_s:.0f} s: "
                            f"{', '.join(bad) or 'checkouts'} still abnormal ({(fail or 0):.0%} of checkouts failing)",
                            {"still_bad": bad, "fail_ratio": fail})
            else:
                self._set_data(inc_id, verify={**v, "clean": clean})

    def _resolve(self, inc_id: str, inc: dict, snap, verdicts) -> None:
        v = inc["data"]["verify"]
        diag = inc["data"]["diagnosis"]
        onset = min(inc["data"].get("onset", {}).values(), default=inc["opened_at"])
        now = time.time()
        mttr, mttd = now - onset, inc["opened_at"] - onset
        lost = int(round(inc["data"].get("failed_checkouts", 0)))
        root_text = verdicts[diag["root"]].texts.get("p95_ms", "") if diag["root"] in verdicts else ""
        self.e.store.record_outcome(inc_id, diag["category"], v["action"], v["target"], True)
        metrics = {"mttd_s": round(mttd, 1), "mttr_s": round(mttr, 1), "failed_checkouts": lost,
                   "peak_fail_ratio": inc["data"].get("peak_fail_ratio", 0)}
        self._set_data(inc_id, verify={**v, "result": "passed"}, metrics=metrics)
        peak = inc["data"].get("peak_fail_ratio", 0)
        self.e.incidents.transition(
            inc_id, "RESOLVED", actor="verifier", stage="verify", kind="resolved",
            summary=(f"{inc_id} resolved and verified: checkouts failing {(snap.user.fail_ratio or 0):.0%} "
                     f"(peak {peak:.0%}){'; ' + root_text if root_text else ''}. "
                     f"{fmt_duration(mttr)} from first symptom to verified recovery; {lost:,} checkouts failed."),
            payload=metrics)
        self.e.reset_persistence(inc["services"])

    def _escalate(self, inc_id: str, why: str, plan: dict | None) -> None:
        inc = self.e.incidents.open.get(inc_id)
        if not inc:
            return
        if inc["data"].get("failed_checkouts", 0) < 1 and (inc["data"].get("peak_fail_ratio") or 0) < self.e.s.recovered_fail_ratio:
            # Nobody was hurt: a human has it now. Don't reopen the same quiet anomaly
            # every few minutes; anything that starts failing checkouts still opens.
            self.e.mute_quiet(inc["services"])
        diag = inc["data"].get("diagnosis", {})
        tried = ", ".join(f"{a['action']} {a['target']}" for a in inc["data"].get("actions", [])) or "nothing"
        self.e.incidents.transition(
            inc_id, "ESCALATED", actor="policy", stage="decide", kind="escalated",
            summary=(f"{inc_id} escalated to a human: {why}. Root: {diag.get('root', '?')} "
                     f"({CATEGORY_LABEL.get(diag.get('category', 'unknown'), '?')}); tried: {tried}."),
            payload={"why": why, "tried": tried})
        self.e.reset_persistence(inc["services"])
