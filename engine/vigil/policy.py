"""Decide: price every option in one unit, failed checkouts, and pick within guardrails.

    score(action) = P(fix | category, action) x checkouts failing per minute x horizon
                    - checkouts the action itself drops
                    - lambda x infrastructure cost

Waiting is always an option, scored 0, while checkouts keep failing. The LLM
never chooses an action: the category comes from the deterministic rules, the
probabilities from a prior that VIGIL's own track record updates.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass

# How likely each action fixes each category, before VIGIL has its own history.
PRIORS: dict[tuple[str, str], float] = {
    ("stuck_process", "restart"): 0.85,
    ("resource_leak", "restart"): 0.90,
    ("crashed", "restart"): 0.80,
    ("load_saturation", "restart"): 0.0,       # a restart adds no capacity and drops the requests in flight
    ("regression_after_change", "restart"): 0.0,   # the bug ships in the version: a restart brings it back
    ("unknown", "restart"): 0.30,
    ("regression_after_change", "rollback"): 0.90,  # the previous version ran fine minutes ago
    ("load_saturation", "scale_out"): 0.85,         # real demand: more capacity is the fix
}
SCALE_OTHER_P = 0.0          # a replica runs the same code: it does not fix a fault, only adds room
ROLLBACK_OTHER_P = 0.15      # a recent deploy that is not the cause: rolling back rarely helps
SYMPTOM_RESTART_P = 0.0      # the cause keeps running: restarting a symptom fixes nothing
# Overloaded so badly it stopped answering: a replica is the fix, but if the instance
# itself died under the load, starting it again is the fallback once there is room.
OVERLOAD_DOWN_RESTART_P = 0.5
PRIOR_STRENGTH = 4           # the prior counts as 4 past attempts

# Actions this build can execute, and how risky each is. A restart runs the same
# code again; a rollback changes which code runs, so at L2 a human approves it.
RISK = {"restart": "low", "rollback": "medium", "scale_out": "low"}
AUTONOMY = {
    "L0": "observe only: detect and diagnose",
    "L1": "advise: propose, a human clicks",
    "L2": "supervised: act alone on low-risk fixes, ask for the rest",
    "L3": "autonomous: act alone on every allowed fix",
}
ALLOWED_ALONE = {"L0": set(), "L1": set(), "L2": {"low"}, "L3": {"low", "medium"}}


@dataclass
class Option:
    action: str              # restart | rollback | scale_out | wait
    target: str | None
    p_fix: float
    saves: float             # failed checkouts it prevents over the horizon
    drops: float             # checkouts the action itself fails
    score: float
    note: str
    executable: bool = True
    version: str | None = None   # rollback only: the version to go back to

    def label(self) -> str:
        if self.action == "wait":
            return "wait and watch"
        if self.action == "rollback":
            return f"roll back {self.target} to {self.version}"
        if self.action == "scale_out":
            return f"add a {self.target} replica"
        return f"{self.action} {self.target}"


def learned_p(prior: float, successes: int, attempts: int) -> float:
    return (prior * PRIOR_STRENGTH + successes) / (PRIOR_STRENGTH + attempts)


def price_options(*, root: str, category: str, symptoms: list[str], fail_per_min: float,
                  checkout_rps: float, horizon_min: float, restart_downtime_s: float,
                  history: dict[tuple[str, str], tuple[int, int]],
                  last_change: dict | None = None, spare: bool = False,
                  root_down: bool = False) -> list[Option]:
    """last_change: the root's most recent version change, if recent. Only then is
    a rollback an option (there must be a previous version to go back to).
    spare: a stopped warm replica of the root exists, so adding capacity is an option.
    root_down: the root is not answering at all right now."""
    at_stake = fail_per_min * horizon_min
    restart_drops = restart_downtime_s * checkout_rps
    options = [Option("wait", None, 0.0, 0.0, 0.0, 0.0,
                      f"~{fail_per_min:.0f} checkouts keep failing every minute", executable=True)]

    prior = PRIORS.get((category, "restart"), 0.3)
    restart_note = f"clears a {category.replace('_', ' ')}"
    if category == "load_saturation" and root_down:
        prior = OVERLOAD_DOWN_RESTART_P
        restart_note = "brings back an instance that stopped answering, but adds no capacity"
    succ, tries = history.get((category, "restart"), (0, 0))
    p = learned_p(prior, succ, tries)
    saves = p * at_stake
    track = f"; VIGIL's record: {succ}/{tries}" if tries else ""
    options.append(Option("restart", root, round(p, 2), round(saves), round(restart_drops),
                          round(saves - restart_drops), f"{restart_note}{track}"))
    if last_change:
        prior_rb = PRIORS.get((category, "rollback"), ROLLBACK_OTHER_P)
        succ_rb, tries_rb = history.get((category, "rollback"), (0, 0))
        p_rb = learned_p(prior_rb, succ_rb, tries_rb)
        saves_rb = p_rb * at_stake
        drops_rb = 1.0 * checkout_rps      # the old version starts before the new one stops
        track_rb = f"; VIGIL's record: {succ_rb}/{tries_rb}" if tries_rb else ""
        options.append(Option("rollback", root, round(p_rb, 2), round(saves_rb), round(drops_rb),
                              round(saves_rb - drops_rb),
                              f"undoes {last_change['from']} -> {last_change['to']}{track_rb}",
                              version=last_change["from"]))
    if spare:
        prior_sc = PRIORS.get((category, "scale_out"), SCALE_OTHER_P)
        succ_sc, tries_sc = history.get((category, "scale_out"), (0, 0))
        p_sc = learned_p(prior_sc, succ_sc, tries_sc)
        saves_sc = p_sc * at_stake
        track_sc = f"; VIGIL's record: {succ_sc}/{tries_sc}" if tries_sc else ""
        note = ("starts a warm replica: more capacity, same code, nothing restarted" if prior_sc
                else "a replica adds room but does not fix a fault")
        options.append(Option("scale_out", root, round(p_sc, 2), round(saves_sc), 0,
                              round(saves_sc), note + track_sc))
    for s in symptoms:
        saves_s = SYMPTOM_RESTART_P * at_stake
        options.append(Option("restart", s, SYMPTOM_RESTART_P, round(saves_s), round(restart_drops),
                              round(saves_s - restart_drops),
                              "a symptom: its failures come from another service", executable=False))
    return sorted(options, key=lambda o: -o.score)


@dataclass
class Guardrails:
    cooldown_s: float = 120          # same action on the same service
    max_attempts: int = 2            # per incident, then escalate
    budget_actions: int = 3          # across all incidents...
    budget_window_s: float = 600     # ...per 10 minutes


class Governor:
    """Who may act, when. Every refusal comes with a reason a human can read."""

    def __init__(self, guardrails: Guardrails | None = None, autonomy: str = "L2"):
        self.g = guardrails or Guardrails()
        self.autonomy = autonomy
        self.paused = False
        self.history: list[dict] = []    # executed actions: {action, target, ts}

    def blockers(self, option: Option, incident_attempts: int, telemetry_ok: bool,
                 now: float | None = None) -> list[str]:
        now = time.time() if now is None else now
        out = []
        if self.paused:
            out.append("kill switch is on")
        if not telemetry_ok:
            out.append("telemetry is stale")
        if not option.executable:
            out.append("not a valid fix for the root cause")
        if incident_attempts >= self.g.max_attempts:
            out.append(f"already tried {incident_attempts} fixes on this incident")
        recent = [h for h in self.history if now - h["ts"] < self.g.budget_window_s]
        if len(recent) >= self.g.budget_actions:
            out.append(f"action budget used ({len(recent)} in {self.g.budget_window_s / 60:.0f} min)")
        same = [h for h in recent if h["action"] == option.action and h["target"] == option.target
                and now - h["ts"] < self.g.cooldown_s]
        if same:
            wait = self.g.cooldown_s - (now - same[-1]["ts"])
            out.append(f"{option.label()} is cooling down ({wait:.0f}s left)")
        return out

    def may_act_alone(self, option: Option, confidence: str) -> tuple[bool, str]:
        risk = RISK.get(option.action, "high")
        if confidence != "High":
            return False, f"confidence {confidence}: a human must approve"
        if risk not in ALLOWED_ALONE[self.autonomy]:
            return False, f"autonomy {self.autonomy} does not allow {risk}-risk actions alone"
        return True, f"confidence High and {option.action} is {risk} risk at autonomy {self.autonomy}"

    def record(self, option: Option, now: float | None = None) -> None:
        self.history.append({"action": option.action, "target": option.target,
                             "ts": time.time() if now is None else now})


def new_action_id() -> str:
    return uuid.uuid4().hex[:16]


def options_table(options: list[Option]) -> list[dict]:
    return [{**asdict(o), "label": o.label()} for o in options]
