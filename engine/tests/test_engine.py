"""The whole engine on a simulated world: detection, correlation, lifecycle, restart."""

import time

import pytest

from vigil.bus import Bus
from vigil.collector import EdgeSample, Snapshot, UserSample
from vigil.config import Settings
from vigil.engine import Engine
from vigil.incidents import IllegalTransition
from vigil.store import Store

from sim import ServiceSim


class World:
    """payment calls inventory; when inventory stalls, payment's calls time out."""

    def __init__(self, seed=3):
        self.pay = ServiceSim("payment", seed=seed)
        self.inv = ServiceSim("inventory", seed=seed + 100)
        self.inventory_slow = False

    def snapshot(self) -> Snapshot:
        self.inv.latency_add_ms = 1500 if self.inventory_slow else 0
        # The cascade: payment errors are timeouts to inventory; payment's own CPU is fine.
        self.pay.error_rate = 0.45 if self.inventory_slow else 0.0
        self.pay.latency_add_ms = 1000 if self.inventory_slow else 0
        p, i = self.pay.sample(), self.inv.sample()
        fail = 0.45 if self.inventory_slow else 0.0
        return Snapshot(
            ts=time.time(),
            services={"payment": p, "inventory": i},
            edges=[EdgeSample("payment", "inventory", rps=p.rps, fail_ratio=fail,
                              p95_ms=1000 if self.inventory_slow else 15)],
            user=UserSample(checkout_rps=20, fail_ratio=fail, p95_ms=1100 if self.inventory_slow else 45),
        )


def make_engine(tmp_path, world):
    s = Settings(data_dir=tmp_path, baseline_seconds=0)  # learn from the first 24 samples
    bus = Bus()
    store = Store(s.db_path)
    eng = Engine(s, collector=None, store=store, bus=bus)
    for _ in range(30):
        eng.process(world.snapshot())
    assert all(d.baseline for d in eng.detectors.values())
    return eng, store


def kinds(store):
    return [e["kind"] for e in store.events()]


def test_cascade_becomes_one_incident_then_self_resolves(tmp_path):
    world = World()
    eng, store = make_engine(tmp_path, world)

    world.inventory_slow = True
    for _ in range(6):
        eng.process(world.snapshot())
    incs = store.list_incidents()
    assert len(incs) == 1, "two anomalous services, connected: one incident"
    inc = incs[0]
    assert set(inc["services"]) == {"payment", "inventory"}
    assert inc["status"] == "INVESTIGATING"
    assert inc["severity"] == "critical"
    assert "service_joined" in kinds(store)

    world.inventory_slow = False
    for _ in range(8):
        eng.process(world.snapshot())
    inc = store.get_incident(inc["id"])
    assert inc["status"] == "SELF_RESOLVED"
    assert inc["data"]["failed_checkouts"] > 0
    assert not eng.incidents.open


def test_no_incidents_while_healthy(tmp_path):
    world = World(seed=11)
    eng, store = make_engine(tmp_path, world)
    for _ in range(360):  # 30 minutes
        eng.process(world.snapshot())
    assert store.list_incidents() == []


def test_telemetry_outage_pauses_decisions(tmp_path):
    world = World()
    eng, store = make_engine(tmp_path, world)
    world.inventory_slow = True
    for _ in range(6):
        eng.process(Snapshot(ts=time.time(), telemetry_ok=False, problem="prometheus unreachable"))
    assert store.list_incidents() == []
    assert "telemetry_degraded" in kinds(store)
    eng.process(world.snapshot())
    assert "telemetry_ok" in kinds(store)


def test_restart_mid_incident_resumes(tmp_path):
    world = World()
    eng, store = make_engine(tmp_path, world)
    world.inventory_slow = True
    for _ in range(6):
        eng.process(world.snapshot())
    inc_id = store.list_incidents()[0]["id"]

    # A brand-new engine on the same database and models, as after a crash.
    eng2 = Engine(eng.s, collector=None, store=store, bus=Bus())
    assert inc_id in eng2.incidents.open
    for _ in range(3):
        eng2.process(world.snapshot())
    assert len(store.list_incidents()) == 1, "still the same incident, not a duplicate"
    world.inventory_slow = False
    for _ in range(8):
        eng2.process(world.snapshot())
    assert store.get_incident(inc_id)["status"] == "SELF_RESOLVED"


def test_illegal_transition_is_refused(tmp_path):
    world = World()
    eng, store = make_engine(tmp_path, world)
    world.inventory_slow = True
    for _ in range(6):
        eng.process(world.snapshot())
    inc_id = store.list_incidents()[0]["id"]
    with pytest.raises(IllegalTransition):
        eng.incidents.transition(inc_id, "RESOLVED", actor="test", stage="verify",
                                 kind="x", summary="skip verification")


class BusyLaptop(World):
    """A CPU-heavy docker build: every service slows at once, nothing fails, no user hurts."""

    def __init__(self, seed=3):
        super().__init__(seed)
        self.busy = False

    def snapshot(self) -> Snapshot:
        for svc, add in ((self.pay, 2100), (self.inv, 60)):
            svc.latency_add_ms = add if self.busy else 0
        self.pay.load_x = self.inv.load_x = 1.0
        p, i = self.pay.sample(), self.inv.sample()
        if self.busy:
            p.cpu_pct, i.cpu_pct = p.cpu_pct + 30, i.cpu_pct + 4
        return Snapshot(
            ts=time.time(), services={"payment": p, "inventory": i},
            edges=[EdgeSample("payment", "inventory", rps=p.rps, fail_ratio=0.0, p95_ms=24)],
            user=UserSample(checkout_rps=20, fail_ratio=0.0, p95_ms=2300 if self.busy else 45),
        )


def test_a_20_second_host_blip_that_hurts_no_one_opens_nothing(tmp_path):
    """Regression: VGL-0003..0010 were docker builds slowing the laptop for ~20 s."""
    world = BusyLaptop()
    eng, store = make_engine(tmp_path, world)
    world.busy = True
    for _ in range(4):
        eng.process(world.snapshot())
    world.busy = False
    for _ in range(8):
        eng.process(world.snapshot())
    assert store.list_incidents() == []
    assert "suspect" in kinds(store), "VIGIL still says it noticed; it just doesn't raise an incident"


def test_a_slowdown_that_persists_opens_even_without_impact(tmp_path):
    world = BusyLaptop()
    eng, store = make_engine(tmp_path, world)
    world.busy = True
    for _ in range(9):
        eng.process(world.snapshot())
    assert store.list_incidents() == []
    for _ in range(3):
        eng.process(world.snapshot())
    assert len(store.list_incidents()) == 1, "about a minute of sustained slowness is real"


def test_real_user_impact_still_opens_within_three_ticks(tmp_path):
    world = World()
    eng, store = make_engine(tmp_path, world)
    world.inventory_slow = True
    for _ in range(3):
        eng.process(world.snapshot())
    assert len(store.list_incidents()) == 1
