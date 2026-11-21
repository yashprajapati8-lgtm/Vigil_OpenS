"""Rushes you know are coming: VIGIL starts the spare copy before them (schedule.py)."""

import asyncio
import time

import pytest

from vigil.bus import Bus
from vigil.config import ConfigError, Settings, parse_events, parse_site
from vigil.engine import Engine
from vigil.store import Store

from test_diagnosis import FakeLLM, agreeing
from test_remediation import FlashWorld, ScaleActuator, run_ticks


def event(at, prepare_min=10, lasts_min=60, services=("payment",)):
    return {"name": "Results declared", "at": at, "prepare_s": prepare_min * 60, "lasts_s": lasts_min * 60,
            "services": tuple(services)}


def engine_with(tmp_path, events, **overrides):
    world = FlashWorld()
    s = Settings(data_dir=tmp_path, baseline_seconds=0, diagnose_delay_s=0, verify_settle_s=0,
                 autonomy="L2", events=tuple(events), **overrides)
    store = Store(s.db_path)
    act = ScaleActuator(world)
    eng = Engine(s, collector=None, store=store, bus=Bus(), llm=FakeLLM(agreeing), actuator=act)
    for _ in range(30):
        eng.process(world.snapshot())
    return world, eng, store, act


def prepare_due(eng):
    for ev, svc in eng._due_prepare:
        asyncio.run(eng.remediator.prepare(ev, svc))


def test_nothing_happens_before_the_window(tmp_path):
    world, eng, store, act = engine_with(tmp_path, [event(time.time() + 3600)])
    run_ticks(eng, world, 2)
    assert eng._due_prepare == []
    assert eng.schedule.view()[0]["status"] == "upcoming"


def test_spare_starts_before_the_rush_and_nobody_fails(tmp_path):
    world, eng, store, act = engine_with(tmp_path, [event(time.time() + 5 * 60)])   # 5 min away, prepare 10 min
    run_ticks(eng, world, 1)
    assert [svc for _, svc in eng._due_prepare] == ["payment"]
    prepare_due(eng)
    assert act.calls == [("start", "payment-replica")]
    assert "payment" in eng.remediator.scaled
    assert any(e["kind"] == "prepared" and "Getting ready for Results declared" in e["summary"]
               for e in store.events())

    run_ticks(eng, world, 2)
    assert eng._due_prepare == [], "started once per window, never again"

    world.sale = True                  # the rush: the replica is already serving
    run_ticks(eng, world, 10)
    assert store.list_incidents() == [], "ready before the rush: nothing to fix"


def test_spare_stays_for_the_whole_window_even_when_calm(tmp_path):
    world, eng, store, act = engine_with(tmp_path, [event(time.time() + 60)])
    run_ticks(eng, world, 1)
    prepare_due(eng)
    run_ticks(eng, world, eng.s.scale_in_calm_ticks + 5)    # calm traffic for over a minute
    assert eng._due_scale_in == [], "inside the window the spare copy is never stopped"


def test_after_the_window_it_scales_back_in_as_usual(tmp_path):
    world, eng, store, act = engine_with(tmp_path, [event(time.time() + 60)])
    run_ticks(eng, world, 1)
    prepare_due(eng)
    eng.s.events[0]["at"] = time.time() - 2 * 3600    # the window is over
    due = False
    for _ in range(4 * eng.s.scale_in_calm_ticks):    # a minute of calm traffic (noisy, so allow a few tries)
        run_ticks(eng, world, 1)
        if eng._due_scale_in == ["payment"]:
            due = True
            break
    assert due
    asyncio.run(eng.remediator.scale_in("payment"))
    assert act.calls[-1] == ("stop", "payment-replica")


def test_kill_switch_and_l0_start_nothing(tmp_path):
    world, eng, store, act = engine_with(tmp_path, [event(time.time() + 60)])
    eng.governor.paused = True
    run_ticks(eng, world, 1)
    assert eng._due_prepare == []
    eng.governor.paused = False
    eng.governor.autonomy = "L0"
    run_ticks(eng, world, 1)
    assert eng._due_prepare == []


def test_no_spare_copy_says_how_to_make_one(tmp_path):
    world, eng, store, act = engine_with(tmp_path, [event(time.time() + 60, services=("inventory",))])
    run_ticks(eng, world, 1)
    prepare_due(eng)
    failed = [e for e in store.events() if e["kind"] == "action_failed"]
    assert len(failed) == 1 and "docker compose --profile replica create inventory-replica" in failed[0]["summary"]


# ---- vigil.yaml

SERVICES = ["results-api", "auth"]
ALLOWED = {"results-api": frozenset({"restart", "scale_out"}), "auth": frozenset({"restart"})}


def test_events_in_vigil_yaml():
    out = parse_events([{"name": "Results declared", "at": "2026-11-25T10:00+05:30"}], SERVICES, ALLOWED)
    assert out[0]["at"] == 1795581000.0               # 25 Nov 2026, 04:30 UTC
    assert out[0]["prepare_s"] == 600 and out[0]["lasts_s"] == 3600
    assert out[0]["services"] == ("results-api",), "default: every service that allows scale_out"


def test_a_time_without_a_time_zone_is_refused():
    with pytest.raises(ConfigError, match="time zone"):
        parse_events([{"name": "Results", "at": "2026-11-25T10:00"}], SERVICES, ALLOWED)


def test_an_event_for_a_service_that_may_not_scale_out_is_refused():
    with pytest.raises(ConfigError, match="does not allow scale_out"):
        parse_events([{"name": "Results", "at": "2026-11-25T10:00+05:30", "services": ["auth"]}], SERVICES, ALLOWED)


def test_bad_values_name_the_key():
    with pytest.raises(ConfigError, match=r"events\[0\]\.at"):
        parse_events([{"name": "Results", "at": "tomorrow"}], SERVICES, ALLOWED)
    with pytest.raises(ConfigError, match=r"events\[0\]\.prepare_min"):
        parse_events([{"name": "Results", "at": "2026-11-25T10:00+05:30", "prepare_min": -5}], SERVICES, ALLOWED)
    with pytest.raises(ConfigError, match="events: expected a list"):
        parse_events({"name": "Results"}, SERVICES, ALLOWED)


def test_parse_site_reads_events():
    doc = {"version": 1, "services": {"results-api": {"allow": ["restart", "scale_out"]}},
           "customers": {"entry": "portal", "calls": ["results-api"], "unit": "result lookup",
                         "units": "result lookups", "metric": "result_requests_total"},
           "events": [{"name": "Results declared", "at": "2026-11-25T10:00+05:30", "prepare_min": 1}]}
    out = parse_site(doc)
    assert out["events"][0]["prepare_s"] == 60
