"""Root cause: the blame walk, category rules, the LLM second key, and confidence."""

import asyncio
import json
import time

import httpx
import pytest

from vigil.bus import Bus
from vigil.collector import EdgeSample, ServiceSample, Snapshot
from vigil.config import Settings
from vigil.detector import Verdict
from vigil.diagnosis import CATEGORIES, WalkResult, blame_walk, classify, confidence
from vigil.engine import Engine
from vigil.llm import LLMClient, validate
from vigil.store import Store

from test_engine import World


# ---------------------------------------------------------------- blame walk

def cascade_snapshot():
    snap = Snapshot(ts=time.time(), services={
        "payment": ServiceSample("payment", up=True, rps=20, error_rate=0.45, p95_ms=1000, cpu_pct=9),
        "inventory": ServiceSample("inventory", up=True, rps=11, error_rate=0.0, p95_ms=1515, cpu_pct=6),
    }, edges=[EdgeSample("payment", "inventory", rps=20, fail_ratio=0.45, p95_ms=1000)])
    verdicts = {
        "payment": Verdict("payment", "anomalous", True, reasons=["error rate +80.0σ"],
                           z={"error_rate": 80, "p95_ms": 30, "cpu_pct": 0.2, "mem_mb": 0.1}),
        "inventory": Verdict("inventory", "anomalous", True, reasons=["latency p95 +60.0σ"],
                             z={"p95_ms": 60, "rps": -2, "cpu_pct": 0.1}),
    }
    return snap, verdicts


def test_walk_follows_failing_calls_to_the_service_that_stopped():
    snap, verdicts = cascade_snapshot()
    edges = {("storefront", "payment"), ("payment", "inventory")}
    w = blame_walk(["inventory", "payment"], verdicts, snap, edges)
    assert w.root == "inventory"
    assert w.path == ["payment", "inventory"]
    assert w.conclusive
    assert "calls to inventory" in w.symptoms["payment"]
    assert "CPU normal" in w.ruled_out["payment"]


def test_walk_stays_put_when_calls_are_healthy():
    snap, verdicts = cascade_snapshot()
    snap.edges = [EdgeSample("payment", "inventory", rps=20, fail_ratio=0.0, p95_ms=15)]
    w = blame_walk(["payment"], verdicts, snap, {("payment", "inventory")})
    assert w.root == "payment" and w.path == ["payment"]


def test_a_starved_caller_does_not_blame_a_healthy_callee():
    """Runaway loop in payment: its event loop is starved, so its calls to inventory
    look slow and time out from payment's side, while inventory itself answers in
    ~15 ms. Regression: Proving Ground VGL-0020 blamed inventory."""
    snap = Snapshot(ts=time.time(), services={
        "payment": ServiceSample("payment", up=True, rps=19, error_rate=0.3, p95_ms=2400, cpu_pct=98, cpu_per_req=5.1),
        "inventory": ServiceSample("inventory", up=True, rps=12, error_rate=0.0, p95_ms=15, cpu_pct=3),
    }, edges=[EdgeSample("payment", "inventory", rps=19, fail_ratio=0.3, p95_ms=1000)])
    verdicts = {
        "payment": Verdict("payment", "anomalous", True, reasons=["CPU 98% (normal 15%)"],
                           z={"cpu_pct": 40, "cpu_per_req": 30, "p95_ms": 50, "error_rate": 60}),
        "inventory": Verdict("inventory", "anomalous", True, reasons=["CPU 3% (normal 6%)"],
                             z={"p95_ms": 0.2, "error_rate": 0.0, "rps": -4, "cpu_pct": 3.5}),
    }
    w = blame_walk(["payment", "inventory"], verdicts, snap, {("storefront", "payment"), ("payment", "inventory")})
    assert w.root == "payment" and w.path == ["payment"]
    assert "the wait is inside payment" in " ".join(w.steps)
    assert "too busy" in w.symptoms["inventory"]
    assert w.cleared == ["inventory"]
    assert classify("payment", verdicts["payment"], [], time.time())[0] == "stuck_process"


# ---------------------------------------------------------------- categories

def v(**z):
    return Verdict("x", "anomalous", True, reasons=["r"], z=z)


@pytest.mark.parametrize("verdict,changes,expected", [
    (Verdict("x", "down", True, True, ["service down"]), [], "crashed"),
    (v(error_rate=40), [{"service": "x", "from": "v1", "to": "v2", "ts": time.time() - 90}], "regression_after_change"),
    (v(mem_slope_mb_s=12, mem_mb=5), [], "resource_leak"),
    (v(rps=9, cpu_pct=8, cpu_per_req=0.3, p95_ms=4), [], "load_saturation"),
    (v(cpu_pct=30, cpu_per_req=25, rps=0.2), [], "stuck_process"),
    (v(p95_ms=60), [], "stuck_process"),
    (v(rps=-1), [], "unknown"),
])
def test_categories(verdict, changes, expected):
    assert classify("x", verdict, changes, time.time())[0] == expected


def test_an_old_deploy_is_not_blamed():
    old = [{"service": "x", "from": "v1", "to": "v2", "ts": time.time() - 3600}]
    assert classify("x", v(p95_ms=50), old, time.time())[0] == "stuck_process"


# ---------------------------------------------------------------- LLM validation

PACK = {"services": ["payment", "inventory"], "categories": CATEGORIES,
        "evidence": [{"id": "E1"}, {"id": "E2"}, {"id": "E3"}]}
GOOD = {"root_cause_service": "inventory", "category": "stuck_process",
        "hypothesis": "Inventory stalls; payment errors are timeouts calling it.",
        "evidence_ids": ["E1", "E3"], "ruled_out": [{"service": "payment", "why": "CPU normal"}]}


@pytest.mark.parametrize("bad,msg", [
    ({**GOOD, "root_cause_service": "database"}, "not one of"),
    ({**GOOD, "category": "cosmic_rays"}, "category"),
    ({**GOOD, "evidence_ids": ["E1", "E9"]}, "does not exist"),
    ({**GOOD, "evidence_ids": []}, "shape"),
])
def test_llm_answers_are_validated_against_the_evidence(bad, msg):
    with pytest.raises(ValueError, match=msg):
        validate(json.dumps(bad), PACK)


def test_llm_free_text_is_rejected():
    with pytest.raises(ValueError):
        validate("I think it's the network. Restart everything!", PACK)


def groq(answers):
    """A fake Groq endpoint that replies with each answer in turn."""
    calls = []

    def handler(req):
        calls.append(json.loads(req.content))
        a = answers[min(len(calls), len(answers)) - 1]
        if isinstance(a, int):
            return httpx.Response(a, json={"error": "nope"})
        return httpx.Response(200, json={"choices": [{"message": {"content": a}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://groq")
    return LLMClient("test-key", "test-model", client=client), calls


def test_llm_ok():
    llm, calls = groq([json.dumps(GOOD)])
    out = asyncio.run(llm.diagnose(PACK))
    assert out["status"] == "ok" and out["root_cause_service"] == "inventory" and out["attempts"] == 1
    body = calls[0]
    assert body["temperature"] == 0 and body["response_format"] == {"type": "json_object"}
    assert "walk" not in body["messages"][1]["content"], "the LLM must never see the walk's verdict"


def test_llm_gets_one_retry_with_the_reason():
    llm, calls = groq([json.dumps({**GOOD, "root_cause_service": "database"}), json.dumps(GOOD)])
    out = asyncio.run(llm.diagnose(PACK))
    assert out["status"] == "ok" and out["attempts"] == 2
    assert "rejected" in calls[1]["messages"][-1]["content"]


def test_llm_rejected_after_two_bad_answers():
    llm, _ = groq(["not json", "still not json"])
    assert asyncio.run(llm.diagnose(PACK))["status"] == "rejected"


def test_llm_outage_is_survivable():
    llm, _ = groq([503])
    assert asyncio.run(llm.diagnose(PACK))["status"] == "unavailable"
    assert asyncio.run(LLMClient("", "m").diagnose(PACK))["status"] == "unavailable"


# ---------------------------------------------------------------- confidence

WALK = WalkResult(root="inventory", path=["payment", "inventory"], steps=[], own_signals=["latency"], conclusive=True)
OK = {"status": "ok", "root_cause_service": "inventory", "category": "stuck_process"}
ALL3 = ["metrics", "edges", "timing"]


@pytest.mark.parametrize("walk,llm,types,expected", [
    (WALK, OK, ALL3, "High"),
    (WALK, {"status": "unavailable"}, ALL3, "Medium"),
    (WALK, {**OK, "root_cause_service": "payment"}, ALL3, "Low"),
    (WALK, {**OK, "category": "resource_leak"}, ALL3, "Medium"),
    (WALK, OK, ["metrics", "timing"], "Medium"),
    (WalkResult("inventory", ["inventory"], [], [], False), OK, ALL3, "Low"),
])
def test_confidence_is_agreement(walk, llm, types, expected):
    assert confidence(walk, "stuck_process", llm, types, multi_service=True)[0] == expected


# ---------------------------------------------------------------- whole engine

class FakeLLM:
    enabled = True

    def __init__(self, answer):
        self.answer = answer
        self.packs = []

    async def diagnose(self, pack):
        self.packs.append(pack)
        return self.answer(pack) if callable(self.answer) else self.answer

    async def close(self):
        pass


def engine_with(tmp_path, llm, world):
    s = Settings(data_dir=tmp_path, baseline_seconds=0, diagnose_delay_s=0)
    store = Store(s.db_path)
    eng = Engine(s, collector=None, store=store, bus=Bus(), llm=llm)
    for _ in range(30):
        eng.process(world.snapshot())
    return eng, store


def run_cascade(tmp_path, llm):
    world = World()
    eng, store = engine_with(tmp_path, llm, world)
    world.inventory_slow = True
    for _ in range(6):
        eng.process(world.snapshot())
    inc_id = store.list_incidents()[0]["id"]
    assert eng._due == [inc_id]
    asyncio.run(eng.diagnose(inc_id))
    return eng, store, store.get_incident(inc_id)


def agreeing(pack):
    ids = [e["id"] for e in pack["evidence"][:3]]
    return {"status": "ok", "root_cause_service": "inventory", "category": "stuck_process",
            "hypothesis": "Inventory stalls; payment times out on it.", "evidence_ids": ids, "ruled_out": []}


def test_engine_diagnoses_the_cascade_with_high_confidence(tmp_path):
    llm = FakeLLM(agreeing)
    eng, store, inc = run_cascade(tmp_path, llm)
    assert inc["status"] == "DIAGNOSED"
    assert inc["root"] == "inventory" and inc["category"] == "stuck_process"
    d = inc["data"]["diagnosis"]
    assert d["confidence"] == "High", d["confidence_reasons"]
    assert "payment" in d["symptoms"]
    kinds = [e["kind"] for e in store.events(incident_id=inc["id"])]
    assert kinds[-3:] == ["blame_walk", "llm_ok", "diagnosed"]
    pack = llm.packs[0]
    assert "inventory" not in json.dumps({k: v for k, v in pack.items() if k not in ("services", "calls", "evidence")})
    assert not eng.incidents.due_for_diagnosis(time.time()), "diagnosed once, not every tick"


def test_engine_without_llm_caps_at_medium(tmp_path):
    _, _, inc = run_cascade(tmp_path, FakeLLM({"status": "unavailable", "error": "timeout"}))
    assert inc["status"] == "DIAGNOSED" and inc["root"] == "inventory"
    assert inc["data"]["diagnosis"]["confidence"] == "Medium"


def test_engine_disagreement_is_low(tmp_path):
    answer = lambda pack: {**agreeing(pack), "root_cause_service": "payment"}
    _, _, inc = run_cascade(tmp_path, FakeLLM(answer))
    assert inc["root"] == "inventory", "policy follows the walk, never the LLM alone"
    assert inc["data"]["diagnosis"]["confidence"] == "Low"


def test_bad_deploy_is_diagnosed_as_regression(tmp_path):
    world = World(seed=5)
    eng, store = engine_with(tmp_path, FakeLLM({"status": "unavailable", "error": "off"}), world)
    original = world.snapshot

    def deployed():
        snap = original()
        p = snap.services["payment"]
        p.version = "v2"
        p.error_rate = 0.12
        return snap
    world.snapshot = deployed
    for _ in range(6):
        eng.process(world.snapshot())
    inc_id = store.list_incidents()[0]["id"]
    asyncio.run(eng.diagnose(inc_id))
    inc = store.get_incident(inc_id)
    assert inc["root"] == "payment" and inc["category"] == "regression_after_change"
    assert "version_changed" in [e["kind"] for e in store.events()]


def test_evidence_buckets_onsets_and_states_explained_errors():
    """Live-run regression: payment's latency showed 5 s before inventory's."""
    from vigil.diagnosis import build_evidence
    snap, verdicts = cascade_snapshot()
    snap.services["payment"].error_rate = 0.85
    snap.edges = [EdgeSample("payment", "inventory", rps=17, fail_ratio=1.0, p95_ms=2425)]
    snap.services["payment"].rps = 20
    t = time.time()
    pack = build_evidence("VGL-1", ["payment", "inventory"], verdicts, snap,
                          {"payment": {"p95_ms": 72}, "inventory": {"p95_ms": 24}},
                          {"payment": t, "inventory": t + 5}, [])
    onsets = {(e["service"], e["signal"]): e.get("onset_s") for e in pack["evidence"] if "service" in e}
    assert onsets[("payment", "p95_ms")] == onsets[("inventory", "p95_ms")] == 0
    share = [e for e in pack["evidence"] if e["signal"] == "share_of_errors_that_are_failed_calls_to_inventory"]
    assert share and share[0]["value"] == 1.0
    ups = {e["service"]: e["value"] for e in pack["evidence"] if e["signal"] == "up"}
    assert ups == {"payment": 1, "inventory": 1}


def test_root_reported_slightly_later_still_counts_as_timing_evidence():
    from vigil.diagnosis import evidence_types
    t = time.time()
    w = WalkResult("inventory", ["payment", "inventory"], [], ["latency"], True)
    assert "timing" in evidence_types(w, {"payment": t, "inventory": t + 5}, 5, True)
    assert "timing" not in evidence_types(w, {"payment": t, "inventory": t + 40}, 5, True)
