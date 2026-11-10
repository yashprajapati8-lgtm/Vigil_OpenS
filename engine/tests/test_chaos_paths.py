"""Regressions from a live run on the laptop (incidents VGL-0051 to VGL-0062).

1. Flash sale: payment got so busy its metrics scrape timed out, so it looked
   "down". VIGIL called it crashed and restarted it, which adds no capacity: the
   same thing happened again, four incidents in a row.
2. Flash sale: inventory went from 24 ms to 250 ms under the surge while payment's
   calls to it timed out at 1 s. VIGIL blamed inventory and escalated.
3. After the surge, payment's memory sat at 73 MB instead of 55 MB and stopped
   growing. VIGIL opened an incident about it every four minutes, forever.
"""

import time

from vigil.collector import EdgeSample, ServiceSample, Snapshot, UserSample
from vigil.detector import Verdict
from vigil.diagnosis import WalkResult, blame_walk, classify, confidence
from vigil.policy import price_options

from test_engine import World, make_engine

EDGES = {("storefront", "payment"), ("payment", "inventory")}


def flash_sale_snapshot():
    snap = Snapshot(ts=time.time(), services={
        "payment": ServiceSample("payment", up=True, rps=51, error_rate=0.11, p95_ms=4900, cpu_pct=61, cpu_per_req=1.2),
        "inventory": ServiceSample("inventory", up=True, rps=51, error_rate=0.0, p95_ms=246, cpu_pct=17),
    }, edges=[EdgeSample("payment", "inventory", rps=51, fail_ratio=0.27, p95_ms=1000)],
        user=UserSample(checkout_rps=118, fail_ratio=0.16))
    verdicts = {
        "payment": Verdict("payment", "anomalous", True, reasons=["latency p95 4.9 s (normal 72 ms)"],
                           z={"p95_ms": 90, "error_rate": 20, "cpu_pct": 12, "rps": 9, "cpu_per_req": 5},
                           texts={"p95_ms": "latency p95 4.9 s (normal 72 ms)"}),
        "inventory": Verdict("inventory", "anomalous", True, reasons=["latency p95 246 ms (normal 24 ms)"],
                             z={"p95_ms": 30, "rps": 9, "cpu_pct": 4, "cpu_per_req": 0.5}),
    }
    return snap, verdicts


def test_flash_sale_blames_payment_not_the_somewhat_slower_inventory():
    snap, verdicts = flash_sale_snapshot()
    w = blame_walk(["payment", "inventory"], verdicts, snap, EDGES)
    assert w.root == "payment"
    assert "the wait is inside payment" in " ".join(w.steps)
    category, why = classify("payment", verdicts["payment"], [], time.time(), surge=(118, 20))
    assert category == "load_saturation"
    assert "118/s (normal 20/s)" in why


def test_slow_inventory_is_still_blamed():
    """The real fault: inventory itself answers in 1.5 s, slower than payment's 1 s timeout."""
    snap, verdicts = flash_sale_snapshot()
    snap.services["inventory"].p95_ms = 1515
    snap.user.checkout_rps = 20
    w = blame_walk(["payment", "inventory"], verdicts, snap, EDGES)
    assert w.root == "inventory"


def test_unanswering_under_a_surge_is_overload_not_a_crash():
    down = Verdict("payment", "down", True, hard=True, reasons=["service down"])
    assert classify("payment", down, [], time.time(), surge=(120, 20))[0] == "load_saturation"
    assert classify("payment", down, [], time.time(), surge=None)[0] == "crashed"


def test_without_a_surge_a_slow_payment_is_still_stuck():
    snap, verdicts = flash_sale_snapshot()
    v = verdicts["payment"]
    v.z["rps"] = 0.5
    assert classify("payment", v, [], time.time(), surge=None)[0] == "stuck_process"


def test_llm_saying_crashed_agrees_with_overloaded_down():
    walk = WalkResult(root="payment", path=["payment"], steps=[], own_signals=["service down"], conclusive=True)
    llm = {"status": "ok", "root_cause_service": "payment", "category": "crashed"}
    level, _ = confidence(walk, "load_saturation", llm, ["metrics", "timing"], False, overloaded_down=True)
    assert level == "High"
    level, _ = confidence(walk, "load_saturation", llm, ["metrics", "timing"], False, overloaded_down=False)
    assert level == "Medium"


def test_overloaded_and_down_adds_a_replica_first_then_restart_as_fallback():
    opts = price_options(root="payment", category="load_saturation", symptoms=[], fail_per_min=3500,
                         checkout_rps=120, horizon_min=5, restart_downtime_s=3, history={},
                         spare=True, root_down=True)
    viable = [o for o in opts if o.action != "wait" and o.score > 0]
    assert [o.action for o in viable] == ["scale_out", "restart"]
    # Still overloaded but answering: a restart stays worthless.
    opts = price_options(root="payment", category="load_saturation", symptoms=[], fail_per_min=3500,
                         checkout_rps=120, horizon_min=5, restart_downtime_s=3, history={}, spare=True)
    assert [o.action for o in opts if o.action != "wait" and o.score > 0] == ["scale_out"]


def test_engine_sees_the_surge_from_the_client_side(tmp_path):
    world = World()
    eng, _ = make_engine(tmp_path, world)
    snap = world.snapshot()
    eng.process(snap)
    assert eng.surge() is None
    snap = world.snapshot()
    snap.user.checkout_rps = 120
    eng.process(snap)
    offered, normal = eng.surge()
    assert offered == 120 and 15 <= normal <= 25


def test_memory_that_stopped_growing_is_not_an_incident(tmp_path):
    world = World()
    eng, store = make_engine(tmp_path, world)
    world.pay.mem += 18           # 58 -> 76 MB, then flat: what a burst of traffic leaves behind
    for _ in range(40):
        eng.process(world.snapshot())
    assert not eng.verdicts["payment"].flagged
    assert not eng.incidents.open


def test_memory_near_the_limit_still_counts(tmp_path):
    world = World()
    eng, _ = make_engine(tmp_path, world)
    world.pay.mem = 200           # flat, but within 56 MB of the 256 MB limit
    eng.process(world.snapshot())
    assert eng.verdicts["payment"].flagged
    assert any("memory" in r for r in eng.verdicts["payment"].reasons)


def test_a_growing_leak_still_counts(tmp_path):
    world = World()
    eng, _ = make_engine(tmp_path, world)
    world.pay.leak_mb_s = 3
    for _ in range(4):
        eng.process(world.snapshot())
    assert eng.verdicts["payment"].flagged


def test_after_a_quiet_escalation_the_same_anomaly_does_not_reopen(tmp_path):
    world = World()
    eng, _ = make_engine(tmp_path, world)
    eng.mute_quiet(["payment"])
    world.pay.cpu_burn = True     # CPU far above normal, nobody hurt: a quiet anomaly
    for _ in range(20):
        eng.process(world.snapshot())
    assert eng.verdicts["payment"].flagged
    assert not eng.incidents.open
    # Users getting hurt still opens at once.
    world.pay.cpu_burn = False
    world.inventory_slow = True
    for _ in range(6):
        eng.process(world.snapshot())
    assert eng.incidents.open


def test_busy_from_a_surge_with_nobody_hurt_does_not_open(tmp_path):
    """Flash sale with a replica serving: inventory's CPU is far above its quiet
    baseline, no checkout fails. VGL-0079 opened on it and blamed inventory."""
    world = World()
    eng, _ = make_engine(tmp_path, world)
    world.inv.cpu_burn = True
    for _ in range(20):
        snap = world.snapshot()
        snap.user.checkout_rps = 110
        eng.process(snap)
    assert eng.verdicts["inventory"].flagged
    assert not eng.incidents.open


def test_fresh_start_closes_incidents_and_relearns(tmp_path):
    world = World()
    eng, store = make_engine(tmp_path, world)
    world.inventory_slow = True
    for _ in range(6):
        eng.process(world.snapshot())
    assert eng.incidents.open
    eng.governor.record(type("O", (), {"action": "restart", "target": "inventory"})())
    out = eng.fresh_start()
    assert out["closed"] and not eng.incidents.open
    assert all(d.baseline is None for d in eng.detectors.values())
    assert not eng.governor.history
    world.inventory_slow = False
    eng.process(world.snapshot())
    assert all(v.state == "learning" for v in eng.verdicts.values())
