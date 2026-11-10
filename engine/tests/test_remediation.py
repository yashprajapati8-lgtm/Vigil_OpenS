"""Decide -> act -> verify on a simulated world, plus the policy and guardrail math."""

import asyncio
import time

import pytest

from vigil.actuator_client import ActuatorError
from vigil.bus import Bus
from vigil.config import Settings
from vigil.engine import Engine
from vigil.policy import Governor, Guardrails, Option, learned_p, price_options
from vigil.store import Store

from test_diagnosis import FakeLLM, agreeing
from test_engine import World


class FakeActuator:
    """Restarting a service clears in-memory faults, exactly like the real containers."""

    def __init__(self, world, fixes=True, fail=False):
        self.world, self.fixes, self.fail = world, fixes, fail
        self.calls = []

    async def containers(self):
        return [{"name": "inventory", "service": "inventory", "status": "running"},
                {"name": "payment-v1", "service": "payment", "status": "running"},
                {"name": "loadgen", "service": None, "status": "running"}]

    async def act(self, action_id, action, container):
        if self.fail:
            raise ActuatorError("actuator unreachable (ConnectError)")
        self.calls.append((action, container))
        if self.fixes and container == "inventory":
            self.world.inventory_slow = False
        return {"action_id": action_id, "action": action, "container": container,
                "status": "done", "duration_ms": 2100}

    async def close(self):
        pass


def setup(tmp_path, llm_answer=agreeing, fixes=True, fail=False, **overrides):
    world = World()
    s = Settings(data_dir=tmp_path, baseline_seconds=0, diagnose_delay_s=0, verify_settle_s=0, **overrides)
    store = Store(s.db_path)
    act = FakeActuator(world, fixes=fixes, fail=fail)
    eng = Engine(s, collector=None, store=store, bus=Bus(), llm=FakeLLM(llm_answer), actuator=act)
    for _ in range(30):
        eng.process(world.snapshot())
    world.inventory_slow = True
    for _ in range(6):
        eng.process(world.snapshot())
    inc_id = store.list_incidents()[0]["id"]
    asyncio.run(eng.diagnose(inc_id))
    eng.process(world.snapshot())          # this tick notices the decision is due
    return world, eng, store, act, inc_id


def kinds(store, inc_id):
    return [e["kind"] for e in store.events(incident_id=inc_id)]


def run_ticks(eng, world, n):
    for _ in range(n):
        eng.process(world.snapshot())


def test_high_confidence_fixes_the_root_alone_and_verifies(tmp_path):
    world, eng, store, act, inc_id = setup(tmp_path)
    assert eng._due_remediation == [inc_id]
    asyncio.run(eng.remediator.handle(inc_id))
    assert act.calls == [("restart", "inventory")], "the root, never the loud symptom"
    assert store.get_incident(inc_id)["status"] == "VERIFYING"
    run_ticks(eng, world, 8)
    inc = store.get_incident(inc_id)
    assert inc["status"] == "RESOLVED"
    assert inc["data"]["metrics"]["failed_checkouts"] > 0
    k = kinds(store, inc_id)
    for step in ["diagnosed", "decided", "acting", "action_started", "action_done", "verifying", "resolved"]:
        assert step in k, step
    d = inc["data"]["decision"]
    assert d["options"][0]["label"] == "restart inventory"
    assert any(o["label"] == "restart payment" and o["score"] < d["options"][0]["score"] for o in d["options"])
    assert store.outcome_history()[("stuck_process", "restart")] == (1, 1)


def test_verification_waits_for_users_not_the_command(tmp_path):
    world, eng, store, act, inc_id = setup(tmp_path, fixes=False)
    asyncio.run(eng.remediator.handle(inc_id))
    run_ticks(eng, world, 8)  # restart "succeeded", but checkouts still fail
    assert store.get_incident(inc_id)["status"] == "VERIFYING"


def test_fault_that_survives_restart_escalates_without_looping(tmp_path):
    world, eng, store, act, inc_id = setup(tmp_path, fixes=False, verify_timeout_s=0)
    asyncio.run(eng.remediator.handle(inc_id))
    run_ticks(eng, world, 2)
    assert "verify_failed" in kinds(store, inc_id)
    assert eng._due_remediation == [inc_id]
    asyncio.run(eng.remediator.handle(inc_id))
    inc = store.get_incident(inc_id)
    assert inc["status"] == "ESCALATED"
    assert act.calls == [("restart", "inventory")], "exactly one attempt, no restart storm"
    assert store.outcome_history()[("stuck_process", "restart")] == (0, 1)


def test_medium_confidence_waits_for_a_human(tmp_path):
    world, eng, store, act, inc_id = setup(tmp_path, llm_answer={"status": "unavailable", "error": "timeout"})
    asyncio.run(eng.remediator.handle(inc_id))
    assert store.get_incident(inc_id)["status"] == "AWAITING_APPROVAL"
    assert act.calls == []
    asyncio.run(eng.remediator.approve(inc_id, who="judge"))
    assert act.calls == [("restart", "inventory")]
    run_ticks(eng, world, 8)
    assert store.get_incident(inc_id)["status"] == "RESOLVED"
    assert "approved" in kinds(store, inc_id)


def test_rejection_hands_over_to_a_human(tmp_path):
    world, eng, store, act, inc_id = setup(tmp_path, llm_answer={"status": "unavailable", "error": "x"})
    asyncio.run(eng.remediator.handle(inc_id))
    eng.remediator.reject(inc_id)
    assert store.get_incident(inc_id)["status"] == "ESCALATED" and act.calls == []


def test_kill_switch_blocks_and_releases(tmp_path):
    world, eng, store, act, inc_id = setup(tmp_path)
    eng.governor.paused = True
    asyncio.run(eng.remediator.handle(inc_id))
    asyncio.run(eng.remediator.handle(inc_id))
    assert act.calls == []
    assert kinds(store, inc_id).count("action_blocked") == 1, "said once, not every tick"
    eng.governor.paused = False
    eng.process(world.snapshot())
    asyncio.run(eng.remediator.handle(inc_id))
    assert act.calls == [("restart", "inventory")]


def test_actuator_outage_escalates(tmp_path):
    world, eng, store, act, inc_id = setup(tmp_path, fail=True)
    asyncio.run(eng.remediator.handle(inc_id))
    inc = store.get_incident(inc_id)
    assert inc["status"] == "ESCALATED" and "action_failed" in kinds(store, inc_id)


def test_a_new_fault_after_resolution_opens_a_new_incident(tmp_path):
    world, eng, store, act, inc_id = setup(tmp_path)
    asyncio.run(eng.remediator.handle(inc_id))
    run_ticks(eng, world, 8)
    assert store.get_incident(inc_id)["status"] == "RESOLVED"
    run_ticks(eng, world, 4)
    world.inventory_slow = True
    run_ticks(eng, world, 6)
    assert len(store.list_incidents()) == 2


# ---------------------------------------------------------------- policy math

def test_options_are_priced_in_failed_checkouts():
    opts = price_options(root="inventory", category="stuck_process", symptoms=["payment"],
                         fail_per_min=432, checkout_rps=20, horizon_min=5, restart_downtime_s=3, history={})
    best = opts[0]
    assert best.label() == "restart inventory"
    assert best.saves == round(0.85 * 432 * 5) and best.drops == 60
    payment = next(o for o in opts if o.target == "payment")
    assert payment.score < 0 and not payment.executable
    assert next(o for o in opts if o.action == "wait").score == 0


def test_track_record_moves_the_probability():
    assert learned_p(0.85, 0, 0) == 0.85
    assert learned_p(0.85, 0, 4) < 0.5
    assert learned_p(0.30, 6, 6) > 0.7


def test_restart_is_never_chosen_for_a_bad_deploy():
    opts = price_options(root="payment", category="regression_after_change", symptoms=[],
                         fail_per_min=120, checkout_rps=20, horizon_min=5, restart_downtime_s=3, history={})
    restart = next(o for o in opts if o.action == "restart")
    assert restart.score < 0


def test_guardrails():
    g = Governor(Guardrails(cooldown_s=120, max_attempts=2, budget_actions=3, budget_window_s=600))
    opt = Option("restart", "inventory", 0.85, 1800, 60, 1740, "")
    now = time.time()
    assert g.blockers(opt, 0, True, now) == []
    assert "telemetry is stale" in g.blockers(opt, 0, False, now)
    assert any("already tried" in b for b in g.blockers(opt, 2, True, now))
    g.record(opt, now)
    assert any("cooling down" in b for b in g.blockers(opt, 0, True, now + 10))
    assert g.blockers(opt, 0, True, now + 130) == []
    other = Option("restart", "payment", 0.85, 1800, 60, 1740, "")
    g.record(other, now + 1)
    g.record(Option("restart", "x", 1, 1, 0, 1, ""), now + 2)
    assert any("budget" in b for b in g.blockers(other, 0, True, now + 300))
    g.paused = True
    assert "kill switch is on" in g.blockers(opt, 0, True, now + 900)


@pytest.mark.parametrize("autonomy,confidence,alone", [
    ("L2", "High", True), ("L2", "Medium", False), ("L1", "High", False),
    ("L0", "High", False), ("L3", "High", True), ("L3", "Low", False),
])
def test_autonomy_matrix(autonomy, confidence, alone):
    g = Governor(autonomy=autonomy)
    assert g.may_act_alone(Option("restart", "inventory", 0.85, 1, 0, 1, ""), confidence)[0] is alone


def test_no_fix_and_no_failing_checkouts_watches_instead_of_paging(tmp_path):
    world, eng, store, act, inc_id = setup(tmp_path)
    real_plan = eng.remediator._plan

    def no_fix_no_impact(i):
        plan = real_plan(i)
        plan.update(chosen=None, fail_per_min=0.0)
        return plan

    eng.remediator._plan = no_fix_no_impact
    asyncio.run(eng.remediator.handle(inc_id))
    asyncio.run(eng.remediator.handle(inc_id))
    inc = store.get_incident(inc_id)
    assert inc["status"] == "DIAGNOSED", "not escalated for a blip"
    assert kinds(store, inc_id).count("watching") == 1, "said once, not every tick"
    assert act.calls == []
    eng.s = Settings(data_dir=tmp_path, watch_without_impact_s=0)   # the watch window ran out
    eng.remediator.e.s = eng.s
    asyncio.run(eng.remediator.handle(inc_id))
    assert store.get_incident(inc_id)["status"] == "ESCALATED"


# ---------------------------------------------------------------- bad deploy -> rollback


class DeployWorld(World):
    """payment v2 ships a bug: 40% of payments fail in payment itself. Restarting v2
    keeps the bug; only running v1 again fixes it."""

    def __init__(self, seed=3):
        super().__init__(seed)
        self.running = {"payment-v1"}

    def snapshot(self):
        bad = "payment-v2" in self.running and "payment-v1" not in self.running
        self.pay.error_rate = 0.4 if bad else 0.0
        self.pay.latency_add_ms = 0
        self.inv.latency_add_ms = 0
        p, i = self.pay.sample(), self.inv.sample()
        p.version = ",".join(sorted(n.split("-")[1] for n in self.running if n.startswith("payment")))
        from vigil.collector import EdgeSample, Snapshot, UserSample
        return Snapshot(ts=time.time(), services={"payment": p, "inventory": i},
                        edges=[EdgeSample("payment", "inventory", rps=p.rps, fail_ratio=0.0, p95_ms=15)],
                        user=UserSample(checkout_rps=20, fail_ratio=0.4 if bad else 0.0, p95_ms=45))


class DeployActuator:
    def __init__(self, world):
        self.world, self.calls = world, []

    async def containers(self):
        out = []
        for name, svc, ver in (("inventory", "inventory", "v1"), ("payment-v1", "payment", "v1"),
                               ("payment-v2", "payment", "v2")):
            st = "running" if name == "inventory" or name in self.world.running else "exited"
            out.append({"name": name, "service": svc, "version": ver, "status": st, "health": "healthy" if st == "running" else None})
        return out

    async def act(self, action_id, action, container):
        self.calls.append((action, container))
        if action == "start":
            self.world.running.add(container)
        elif action == "stop":
            self.world.running.discard(container)
        return {"action_id": action_id, "action": action, "container": container, "status": "done", "duration_ms": 900}

    async def close(self):
        pass


def deploy_setup(tmp_path, autonomy="L3"):
    world = DeployWorld()
    s = Settings(data_dir=tmp_path, baseline_seconds=0, diagnose_delay_s=0, verify_settle_s=0, autonomy=autonomy)
    store = Store(s.db_path)
    act = DeployActuator(world)
    llm = FakeLLM({"status": "ok", "root_cause_service": "payment", "category": "regression_after_change",
                   "hypothesis": "errors began right after payment changed v1 -> v2", "evidence_ids": ["E1"],
                   "ruled_out": []})
    eng = Engine(s, collector=None, store=store, bus=Bus(), llm=llm, actuator=act)
    for _ in range(30):
        eng.process(world.snapshot())
    world.running.add("payment-v2")           # blue/green: both serve for a moment
    eng.process(world.snapshot())
    world.running.discard("payment-v1")       # then v1 stops
    for _ in range(4):
        eng.process(world.snapshot())
    return world, eng, store, act


def test_mid_switch_overlap_records_one_clean_version_change(tmp_path):
    world, eng, store, act = deploy_setup(tmp_path)
    changes = [e["payload"] for e in store.events() if e["kind"] == "version_changed"]
    assert [(c["from"], c["to"]) for c in changes] == [("v1", "v2")]


def test_bad_deploy_is_rolled_back_not_restarted(tmp_path):
    world, eng, store, act = deploy_setup(tmp_path, autonomy="L3")
    inc_id = store.list_incidents()[0]["id"]
    asyncio.run(eng.diagnose(inc_id))
    inc = store.get_incident(inc_id)
    assert inc["data"]["diagnosis"]["category"] == "regression_after_change"
    eng.process(world.snapshot())
    asyncio.run(eng.remediator.handle(inc_id))
    d = store.get_incident(inc_id)["data"]["decision"]
    assert d["options"][0]["label"] == "roll back payment to v1"
    restart = next(o for o in d["options"] if o["action"] == "restart")
    assert restart["score"] < 0, "a restart brings the same bug back"
    assert act.calls == [("start", "payment-v1"), ("stop", "payment-v2")], "old version up before the bad one stops"
    run_ticks(eng, world, 8)
    inc = store.get_incident(inc_id)
    assert inc["status"] == "RESOLVED"
    assert any("rolled back to v1" in e["summary"] for e in store.events(incident_id=inc_id))
    assert store.outcome_history()[("regression_after_change", "rollback")] == (1, 1)


def test_rollback_asks_a_human_at_L2(tmp_path):
    world, eng, store, act = deploy_setup(tmp_path, autonomy="L2")
    inc_id = store.list_incidents()[0]["id"]
    asyncio.run(eng.diagnose(inc_id))
    eng.process(world.snapshot())
    asyncio.run(eng.remediator.handle(inc_id))
    inc = store.get_incident(inc_id)
    assert inc["status"] == "AWAITING_APPROVAL", "a rollback changes which code runs: medium risk"
    assert act.calls == []
    asyncio.run(eng.remediator.approve(inc_id, who="judge"))
    assert act.calls == [("start", "payment-v1"), ("stop", "payment-v2")]


def test_restart_never_wakes_a_stopped_old_version(tmp_path):
    world, eng, store, act = deploy_setup(tmp_path)
    from vigil.policy import Option
    inc_id = store.list_incidents()[0]["id"]
    asyncio.run(eng.diagnose(inc_id))
    eng.incidents.transition(inc_id, "REMEDIATING", actor="human", stage="act", kind="approved", summary="test")
    asyncio.run(eng.remediator._execute(inc_id, Option("restart", "payment", 0.5, 0, 0, 0, ""), actor="executor"))
    assert act.calls == [("restart", "payment-v2")]


def test_rollback_only_offered_after_a_recent_change():
    no_change = price_options(root="payment", category="stuck_process", symptoms=[], fail_per_min=100,
                              checkout_rps=20, horizon_min=5, restart_downtime_s=3, history={})
    assert all(o.action != "rollback" for o in no_change)
    with_change = price_options(root="payment", category="regression_after_change", symptoms=[], fail_per_min=100,
                                checkout_rps=20, horizon_min=5, restart_downtime_s=3, history={},
                                last_change={"service": "payment", "from": "v1", "to": "v2", "ts": 0})
    assert with_change[0].action == "rollback" and with_change[0].version == "v1"


def test_real_demand_is_never_answered_with_a_restart():
    """Flash sale: payment is saturated by real customers. Restarting it adds no
    capacity and drops in-flight checkouts, so it must never score positive."""
    opts = price_options(root="payment", category="load_saturation", symptoms=[], fail_per_min=1500,
                         checkout_rps=80, horizon_min=5, restart_downtime_s=3, history={})
    assert all(o.score <= 0 for o in opts if o.action == "restart")


# ---------------------------------------------------------------- flash sale -> scale out, then back in


class FlashWorld(World):
    """Real demand 6x. One payment instance sheds a third of checkouts; two serve everyone."""

    def __init__(self, seed=3):
        super().__init__(seed)
        self.sale = False
        self.replica_on = False

    def snapshot(self):
        x = 6.0 if self.sale else 1.0
        self.pay.load_x = self.inv.load_x = x
        shed = 0.35 if (self.sale and not self.replica_on) else 0.0
        self.pay.error_rate = shed
        self.pay.latency_add_ms = 0
        p, i = self.pay.sample(), self.inv.sample()
        p.instances = 2 if self.replica_on else 1
        from vigil.collector import EdgeSample, Snapshot, UserSample
        return Snapshot(ts=time.time(), services={"payment": p, "inventory": i},
                        edges=[EdgeSample("payment", "inventory", rps=p.rps, fail_ratio=0.0, p95_ms=15)],
                        user=UserSample(checkout_rps=20 * x, fail_ratio=shed, p95_ms=45))


class ScaleActuator:
    def __init__(self, world):
        self.world, self.calls = world, []

    async def containers(self):
        return [{"name": "inventory", "service": "inventory", "version": "v1", "status": "running", "health": "healthy"},
                {"name": "payment-v1", "service": "payment", "version": "v1", "status": "running", "health": "healthy"},
                {"name": "payment-replica", "service": "payment", "version": "v1", "role": "replica",
                 "status": "running" if self.world.replica_on else "exited",
                 "health": "healthy" if self.world.replica_on else None}]

    async def act(self, action_id, action, container):
        self.calls.append((action, container))
        if container == "payment-replica":
            self.world.replica_on = action == "start"
        return {"action_id": action_id, "action": action, "container": container, "status": "done", "duration_ms": 1200}

    async def close(self):
        pass


def test_flash_sale_adds_capacity_alone_then_scales_back_in(tmp_path):
    world = FlashWorld()
    s = Settings(data_dir=tmp_path, baseline_seconds=0, diagnose_delay_s=0, verify_settle_s=0, autonomy="L2")
    store = Store(s.db_path)
    act = ScaleActuator(world)
    llm = FakeLLM(lambda pack: {"status": "ok", "root_cause_service": "payment", "category": "load_saturation",
                                "hypothesis": "6x traffic, CPU per request flat: real demand",
                                "evidence_ids": [e["id"] for e in pack["evidence"][:3]], "ruled_out": []})
    eng = Engine(s, collector=None, store=store, bus=Bus(), llm=llm, actuator=act)
    for _ in range(30):
        eng.process(world.snapshot())
    world.sale = True
    for _ in range(4):
        eng.process(world.snapshot())
    inc_id = store.list_incidents()[0]["id"]
    asyncio.run(eng.diagnose(inc_id))
    assert store.get_incident(inc_id)["data"]["diagnosis"]["category"] == "load_saturation"
    eng.process(world.snapshot())
    asyncio.run(eng.remediator.handle(inc_id))
    inc = store.get_incident(inc_id)
    assert "failing as a result" not in inc["title"], inc["title"]   # inventory is busy, not broken
    d = inc["data"]["decision"]
    assert d["options"][0]["label"] == "add a payment replica"
    assert all(o["score"] <= 0 for o in d["options"] if o["action"] == "restart"), "never restart real demand"
    assert d["acted_alone"], "adding capacity is low risk: VIGIL does it alone at L2"
    assert act.calls == [("start", "payment-replica")]
    run_ticks(eng, world, 6)        # still 6x traffic, but now served
    assert store.get_incident(inc_id)["status"] == "RESOLVED"
    run_ticks(eng, world, 20)       # the sale goes on: busy is expected now, users are fine
    assert len(store.list_incidents()) == 1, "no second incident just because CPU is above the quiet baseline"
    assert store.get_incident(inc_id)["title"].startswith("payment: load saturation"), "a late joiner never resets the title"

    world.sale = False
    run_ticks(eng, world, s.scale_in_calm_ticks - 1)
    assert eng._due_scale_in == []
    run_ticks(eng, world, 1)
    assert eng._due_scale_in == ["payment"]
    asyncio.run(eng.remediator.scale_in("payment"))
    assert act.calls[-1] == ("stop", "payment-replica")
    assert "scaled_in" in [e["kind"] for e in store.events()]
    assert eng.remediator.scaled == {}


def test_no_spare_replica_means_no_scale_option():
    opts = price_options(root="payment", category="load_saturation", symptoms=[], fail_per_min=1500,
                         checkout_rps=120, horizon_min=5, restart_downtime_s=3, history={})
    assert all(o.action != "scale_out" for o in opts)
    opts = price_options(root="payment", category="stuck_process", symptoms=[], fail_per_min=1500,
                         checkout_rps=120, horizon_min=5, restart_downtime_s=3, history={}, spare=True)
    scale = next(o for o in opts if o.action == "scale_out")
    assert scale.score <= 0, "a replica runs the same stuck code: not a fix"


# ---------------------------------------------------------------- memory leak: act before the crash


class LeakWorld(World):
    """payment leaks 3 MB/s toward its 256 MB limit; no checkout fails until it is killed."""

    def __init__(self, seed=3):
        super().__init__(seed)
        self.crashed = False

    def snapshot(self):
        self.pay.down = self.crashed
        self.pay.error_rate = 0.0
        self.pay.latency_add_ms = 0
        p, i = self.pay.sample(), self.inv.sample()
        from vigil.collector import EdgeSample, Snapshot, UserSample
        fail = 1.0 if self.crashed else 0.0
        return Snapshot(ts=time.time(), services={"payment": p, "inventory": i},
                        edges=[EdgeSample("payment", "inventory", rps=p.rps or 0, fail_ratio=0.0, p95_ms=15)],
                        user=UserSample(checkout_rps=20, fail_ratio=fail, p95_ms=45))


class LeakActuator(FakeActuator):
    def __init__(self, world):
        super().__init__(world)

    async def containers(self):
        st = "exited" if self.world.crashed else "running"
        return [{"name": "inventory", "service": "inventory", "version": "v1", "status": "running"},
                {"name": "payment-v1", "service": "payment", "version": "v1", "status": st},
                {"name": "payment-v2", "service": "payment", "version": "v2", "status": "exited"}]

    async def act(self, action_id, action, container):
        self.calls.append((action, container))
        if container == "payment-v1":
            self.world.crashed = False
            self.world.pay.leak_mb_s = 0.0
            self.world.pay.mem = 58.0
        return {"action_id": action_id, "action": action, "container": container, "status": "done", "duration_ms": 2100}


def leak_setup(tmp_path):
    world = LeakWorld()
    s = Settings(data_dir=tmp_path, baseline_seconds=0, diagnose_delay_s=0, verify_settle_s=0, autonomy="L2")
    store = Store(s.db_path)
    act = LeakActuator(world)
    llm = FakeLLM(lambda pack: {"status": "ok", "root_cause_service": "payment", "category": "resource_leak",
                                "hypothesis": "memory climbs steadily toward the limit",
                                "evidence_ids": [e["id"] for e in pack["evidence"][:3]], "ruled_out": []})
    eng = Engine(s, collector=None, store=store, bus=Bus(), llm=llm, actuator=act)
    for _ in range(30):
        eng.process(world.snapshot())
    world.pay.leak_mb_s = 3.0
    for _ in range(12):
        eng.process(world.snapshot())
        if store.list_incidents():
            break
    return world, eng, store, act


def test_leak_is_fixed_before_the_crash_even_with_no_failures_yet(tmp_path):
    """Regression: Proving Ground VGL-0016 'watched' a leak with 0 failing checkouts
    until payment was killed at its memory limit."""
    world, eng, store, act = leak_setup(tmp_path)
    inc_id = store.list_incidents()[0]["id"]
    asyncio.run(eng.diagnose(inc_id))
    eng.process(world.snapshot())
    asyncio.run(eng.remediator.handle(inc_id))
    k = kinds(store, inc_id)
    assert "watching" not in k, "a coming crash is impact"
    d = store.get_incident(inc_id)["data"]["decision"]
    assert d["projected"] and "memory limit" in d["projected"]
    assert act.calls == [("restart", "payment-v1")]


def test_a_crashed_service_is_brought_back_not_its_old_version(tmp_path):
    world, eng, store, act = leak_setup(tmp_path)
    inc_id = store.list_incidents()[0]["id"]
    asyncio.run(eng.diagnose(inc_id))
    world.crashed = True                       # killed at the limit before the fix
    eng.process(world.snapshot())
    from vigil.policy import Option
    eng.incidents.transition(inc_id, "REMEDIATING", actor="human", stage="act", kind="approved", summary="test")
    asyncio.run(eng.remediator._execute(inc_id, Option("restart", "payment", 0.8, 0, 0, 0, ""), actor="executor"))
    assert act.calls == [("restart", "payment-v1")], "the stopped v2 from an old deploy stays stopped"
