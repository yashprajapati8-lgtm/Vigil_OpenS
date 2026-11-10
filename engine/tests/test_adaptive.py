"""Adaptive baseline: normal keeps up with real change, but never learns a failure
and never lets a slow creep become normal without a human saying so."""

import time

import joblib
import pytest

from vigil.collector import ServiceSample
from vigil.config import Settings
from vigil.detector import MODEL_VERSION, ServiceDetector

from sim import ServiceSim
from test_engine import World, kinds, make_engine

TICKS_PER_MIN = 12
TICKS_PER_HOUR = 60 * TICKS_PER_MIN


def trained(service, tmp_path, seed=1, **overrides):
    s = Settings(data_dir=tmp_path, baseline_seconds=180, **overrides)
    sim = ServiceSim(service, seed=seed)
    det = ServiceDetector(service, s)
    t = 0.0
    while det.baseline is None:
        det.evaluate(sim.sample(), now=t)
        t += 5
    return det, sim


def tick(det, sim, learn=True):
    """One tick the way the engine runs it: pause learning while an incident is open."""
    if det.persistence.active:
        det.pause_learning()
    v = det.evaluate(sim.sample(), learn=learn)
    return v, det.persistence.update(v.flagged, v.hard)


def run(det, sim, ticks, learn=True):
    events = []
    for _ in range(ticks):
        _, ev = tick(det, sim, learn)
        if ev:
            events.append(ev)
    return events


def ramp(det, sim, attr, start, end, ticks):
    """Change one property of the simulated service linearly over `ticks`."""
    events = []
    for i in range(ticks):
        setattr(sim, attr, start + (end - start) * i / ticks)
        _, ev = tick(det, sim)
        if ev:
            events.append(ev)
    setattr(sim, attr, end)
    return events


# ---- keeps up with real change

def test_traffic_tripling_over_hours_opens_nothing_and_normal_follows(tmp_path):
    det, sim = trained("payment", tmp_path)
    rps0 = det.baseline.medians["rps"]
    events = ramp(det, sim, "load_x", 1.0, 3.0, 6 * TICKS_PER_HOUR)
    events += run(det, sim, TICKS_PER_HOUR)          # an hour at the new level
    assert events == []
    assert det.baseline.refits >= 30
    assert det.baseline.medians["rps"] > 2.5 * rps0  # normal followed the traffic
    assert det.baseline.drift == {}                  # growth alone is not drift


def test_without_adapting_the_same_tripling_raises_false_incidents(tmp_path):
    """Why adapting exists: the frozen baseline treats the new normal as a fault."""
    det, sim = trained("payment", tmp_path, adapt=False)
    events = ramp(det, sim, "load_x", 1.0, 3.0, 6 * TICKS_PER_HOUR)
    events += run(det, sim, TICKS_PER_HOUR)
    assert det.baseline.refits == 0
    assert events.count("opened") >= 5


def test_a_real_fault_is_still_caught_after_hours_of_adapting(tmp_path):
    det, sim = trained("inventory", tmp_path)
    ramp(det, sim, "load_x", 1.0, 1.5, 3 * TICKS_PER_HOUR)
    assert det.baseline.refits > 0
    sim.latency_add_ms = 1500
    for n in range(1, 7):
        v, ev = tick(det, sim)
        if ev == "opened":
            break
    assert n == 3 and any("latency" in r for r in v.reasons)


@pytest.mark.parametrize("seed", range(3))
def test_six_clean_hours_of_re_learning_open_nothing(tmp_path, seed):
    """Re-learning from only unflagged ticks must not slowly tighten into false alarms."""
    det, sim = trained("payment", tmp_path, seed=seed)
    assert run(det, sim, 6 * TICKS_PER_HOUR) == []
    assert det.baseline.refits >= 30


# ---- never learns a failure

def test_an_incident_is_never_learned(tmp_path):
    det, sim = trained("inventory", tmp_path)
    run(det, sim, 15 * TICKS_PER_MIN)                # some healthy re-learning first
    p95_before = det.baseline.medians["p95_ms"]
    sim.latency_add_ms = 1500                       # a 40-minute outage
    assert "opened" in run(det, sim, 40 * TICKS_PER_MIN)
    sim.latency_add_ms = 0
    run(det, sim, 30 * TICKS_PER_MIN)               # recovery, then normal again
    assert det.baseline.medians["p95_ms"] == pytest.approx(p95_before, rel=0.25)
    assert all(s.p95_ms < 500 for s in det.window), "no outage tick may sit in the window"


def test_learning_holds_after_an_incident_and_drops_the_ticks_before_it(tmp_path):
    det, sim = trained("payment", tmp_path)
    run(det, sim, 20)
    size = len(det.window)
    det.pause_learning()                            # incident opens
    assert len(det.window) == size - det.hold_ticks  # the run-up to it is dropped
    det.evaluate(sim.sample())
    for _ in range(det.hold_ticks - 1):             # incident over: still holding
        det.evaluate(sim.sample())
        assert len(det.window) == size - det.hold_ticks
    det.evaluate(sim.sample())
    det.evaluate(sim.sample())
    assert len(det.window) == size - det.hold_ticks + 1


def test_ticks_the_engine_marks_abnormal_are_judged_but_never_learned(tmp_path):
    det, sim = trained("payment", tmp_path)
    size = len(det.window)
    run(det, sim, 50, learn=False)                  # e.g. a surge, or failing checkouts
    assert len(det.window) == size


def test_memory_growth_is_never_re_learned(tmp_path):
    det, sim = trained("payment", tmp_path)
    slope0 = det.baseline.medians["mem_slope_mb_s"]
    sim.leak_mb_s = 0.05                            # too slow to flag on its own
    run(det, sim, 2 * TICKS_PER_HOUR)
    assert det.baseline.refits > 0
    assert det.baseline.medians["mem_slope_mb_s"] == slope0


# ---- a slow creep is a human's call

def creep(det, sim, add_ms):
    """Latency rises by add_ms over 6 hours, too slowly to flag at any one moment."""
    events = ramp(det, sim, "latency_add_ms", 0.0, add_ms, 6 * TICKS_PER_HOUR)
    return events + run(det, sim, TICKS_PER_HOUR)


def test_slow_latency_creep_is_capped_and_reported_as_drift(tmp_path):
    det, sim = trained("inventory", tmp_path)
    anchor = det.baseline.anchor_medians["p95_ms"]
    cap = anchor + det.s.drift_max_z * det.baseline.anchor_scales["p95_ms"]
    events = creep(det, sim, 25.0)                  # ~14 ms -> ~39 ms
    assert events == []                             # not an outage...
    assert det.baseline.medians["p95_ms"] <= cap + 1e-9   # ...but never silently the new normal
    assert "keeps rising" in det.baseline.drift["p95_ms"]
    v, _ = tick(det, sim)
    assert "p95_ms" in v.drift


def test_a_creep_far_past_the_cap_is_caught_as_a_fault(tmp_path):
    det, sim = trained("inventory", tmp_path)
    events = creep(det, sim, 60.0)                  # ~14 ms -> ~74 ms
    assert "opened" in events
    assert "p95_ms" in det.baseline.drift


def test_accepting_drift_lets_normal_follow_again(tmp_path):
    det, sim = trained("inventory", tmp_path)
    creep(det, sim, 25.0)
    held = det.baseline.medians["p95_ms"]
    assert det.accept_drift() == ["p95_ms"]
    assert det.baseline.drift == {}
    assert det.baseline.anchor_medians["p95_ms"] == held
    assert run(det, sim, 2 * TICKS_PER_HOUR) == []
    assert det.baseline.medians["p95_ms"] > held + 5     # now free to follow, up to the new cap
    assert det.baseline.drift == {}


# ---- survives restarts and old files

def test_the_adapted_baseline_survives_a_restart(tmp_path):
    det, sim = trained("payment", tmp_path)
    run(det, sim, TICKS_PER_HOUR)
    assert det.baseline.refits > 0
    fresh = ServiceDetector("payment", det.s)
    assert fresh.load()
    assert fresh.baseline.refits == det.baseline.refits
    assert fresh.baseline.medians == det.baseline.medians
    assert fresh.baseline.anchor_medians == det.baseline.anchor_medians


def test_a_baseline_saved_before_adapting_existed_is_kept_not_relearned(tmp_path):
    det, _ = trained("payment", tmp_path)
    old = det.baseline
    old.version = 2
    for attr in ("anchor_medians", "anchor_scales", "updated_at", "refits", "drift"):
        old.__dict__.pop(attr, None)
    joblib.dump(old, det.model_path)
    fresh = ServiceDetector("payment", det.s)
    assert fresh.load()
    assert fresh.baseline.version == MODEL_VERSION
    assert fresh.baseline.anchor_medians == fresh.baseline.medians
    assert fresh.baseline.refits == 0 and fresh.baseline.drift == {}


def test_reset_forgets_the_window_too(tmp_path):
    det, sim = trained("payment", tmp_path)
    run(det, sim, 30)
    det.reset()
    assert det.baseline is None and len(det.window) == 0


# ---- the engine

def test_engine_announces_drift_once(tmp_path):
    world = World()
    eng, store = make_engine(tmp_path, world)
    det = eng.detectors["inventory"]
    det.baseline.drift = {"p95_ms": "latency p95 keeps rising"}
    for _ in range(5):
        eng.process(world.snapshot())
    assert kinds(store).count("baseline_drift") == 1
    assert eng.verdicts_view()["inventory"]["baseline"]["drift"] == {"p95_ms": "latency p95 keeps rising"}
    eng.accept_drift("inventory")
    assert eng.verdicts_view()["inventory"]["baseline"]["drift"] == {}


def test_engine_does_not_learn_while_checkouts_fail(tmp_path):
    world = World()
    eng, _ = make_engine(tmp_path, world)
    det = eng.detectors["inventory"]
    size = len(det.window)
    world.inventory_slow = True
    for _ in range(40):
        eng.process(world.snapshot())
    world.inventory_slow = False
    assert len(det.window) <= size
    assert all(s.p95_ms < 500 for s in det.window)
