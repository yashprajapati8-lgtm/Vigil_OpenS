"""vigil.yaml: one file describes the site. Strict, so a typo never means VIGIL
quietly watches the wrong thing; and the shipped file is exactly the demo."""

import dataclasses
from pathlib import Path

import pytest
import yaml

from vigil.collector import PrometheusCollector, ServiceSample, Snapshot, UserSample
from vigil.config import ConfigError, Settings, load_settings, parse_site
from vigil.diagnosis import build_evidence
from vigil.incidents import severity_for
from vigil.llm import SYSTEM_PROMPT, system_prompt
from vigil.policy import Governor, Option, price_options

ROOT = Path(__file__).resolve().parents[2]
SHIPPED = ROOT / "vigil.yaml"
RESULTS = ROOT / "examples" / "results-portal.yaml"

SITE_FIELDS = ["site_name", "monitored", "user_facing", "static_edges", "unit", "units", "user_metric",
               "user_outcome_label", "user_ok", "user_failed", "user_duration_metric", "autonomy"]


def minimal(**over):
    doc = {"version": 1,
           "customers": {"entry": "web", "calls": ["api"], "unit": "order", "units": "orders",
                         "metric": "orders_total"},
           "services": {"api": {}}}
    doc.update(over)
    return doc


# ---- the shipped file

def test_shipped_vigil_yaml_is_exactly_the_built_in_demo(monkeypatch):
    monkeypatch.delenv("VIGIL_AUTONOMY", raising=False)
    s, d = load_settings(str(SHIPPED)), Settings()
    for f in SITE_FIELDS:
        assert getattr(s, f) == getattr(d, f), f
    for svc in s.monitored:
        assert s.allowed(svc) == d.allowed(svc)
    assert s.config_path == str(SHIPPED)


def test_no_file_at_the_default_path_means_the_demo(monkeypatch, tmp_path):
    monkeypatch.delenv("VIGIL_CONFIG", raising=False)
    monkeypatch.setattr("vigil.config.DEFAULT_CONFIG_PATH", str(tmp_path / "absent.yaml"))
    assert load_settings() == Settings()


def test_an_explicit_path_that_is_missing_stops_vigil(monkeypatch, tmp_path):
    monkeypatch.setenv("VIGIL_CONFIG", str(tmp_path / "typo.yaml"))
    with pytest.raises(ConfigError, match="not a file"):
        load_settings()


def test_the_results_portal_example_loads(monkeypatch):
    monkeypatch.delenv("VIGIL_AUTONOMY", raising=False)
    s = load_settings(str(RESULTS))
    assert s.monitored == ("results-api", "auth", "pdf-render")
    assert ("web", "results-api") in s.static_edges and ("results-api", "pdf-render") in s.static_edges
    assert s.units == "result lookups" and s.demand_signal == "result_lookup_attempts_per_s"
    assert s.allowed("auth") == frozenset() and s.allowed("pdf-render") == {"restart", "scale_out"}
    assert s.autonomy == "L1"


def test_env_autonomy_wins_over_the_file(monkeypatch):
    monkeypatch.setenv("VIGIL_AUTONOMY", "L3")
    assert load_settings(str(RESULTS)).autonomy == "L3"


# ---- strict validation: every mistake names its key

@pytest.mark.parametrize("doc, message", [
    (minimal(version=2), "version 2 is not supported"),
    (minimal(sevices={}), "unknown key 'sevices'"),
    ({**minimal(), "services": {}}, "list at least one service"),
    ({**minimal(), "services": {"API": {}}}, "services.API"),
    (minimal(customers={"entry": "web", "calls": ["api"], "unit": "order", "units": "orders"}), "missing 'metric'"),
    (minimal(customers={"entry": "api", "calls": ["api"], "unit": "order", "units": "orders",
                        "metric": "orders_total"}), "also under services"),
    (minimal(customers={"entry": "web", "calls": ["cart"], "unit": "order", "units": "orders",
                        "metric": "orders_total"}), "customers.calls: 'cart' is not under services"),
    ({**minimal(), "services": {"api": {"calls": ["db"]}}}, "services.api.calls: 'db' is not under services"),
    ({**minimal(), "services": {"api": {"calls": ["api"]}}}, "cannot call itself"),
    ({**minimal(), "services": {"api": {"allow": ["reboot"]}}}, "unknown action 'reboot'"),
    ({**minimal(), "services": {"api": {"alow": []}}}, "services.api: unknown key 'alow'"),
    (minimal(autonomy="L4"), "autonomy: 'L4'"),
    (minimal(customers={"entry": "web", "calls": ["api"], "unit": "order", "units": "orders",
                        "metric": "orders total"}), "customers.metric"),
    (minimal(customers={"entry": "web", "calls": ["api"], "unit": "order", "units": "orders",
                        "metric": "orders_total", "ok": ["ok"], "failed": ["ok"]}), "both ok and failed"),
])
def test_mistakes_are_refused_with_the_exact_key(doc, message):
    with pytest.raises(ConfigError, match=message):
        parse_site(doc)


def test_invalid_yaml_is_refused(tmp_path):
    bad = tmp_path / "vigil.yaml"
    bad.write_text("version: 1\nservices: [unclosed\n")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_settings(str(bad))


# ---- the config reaches every part of the loop

def results_settings():
    return dataclasses.replace(Settings(), **parse_site(yaml.safe_load(RESULTS.read_text())))


def test_collector_reads_the_configured_customer_metric_and_outcomes():
    import asyncio
    s = results_settings()
    seen = []

    class Fake(PrometheusCollector):
        async def _query(self, promql):
            seen.append(promql)
            if "result_requests_total" in promql:
                return [{"metric": {"outcome": o}, "value": [0, str(v)]}
                        for o, v in (("ok", 80), ("not_found", 10), ("failed", 6), ("timeout", 4), ("weird", 99))]
            if "up" in promql and "time()" in promql:
                return [{"metric": {"service": svc}, "value": [0, "1"]} for svc in s.monitored]
            return []

    snap = asyncio.run(Fake(s, client=object()).collect())
    assert any("result_request_duration_seconds_bucket" in q for q in seen)
    assert not any("checkout" in q for q in seen)
    assert snap.user.checkout_rps == 100                    # ok + not_found + failed + timeout; "weird" is neither
    assert snap.user.fail_ratio == pytest.approx(0.10)


def test_wording_follows_the_site():
    snap = Snapshot(ts=0, user=UserSample(checkout_rps=50, fail_ratio=0.2))
    assert severity_for(snap, [], "result lookup", "result lookups") == ("critical", "20% of result lookups failing")
    opts = price_options(root="api", category="stuck_process", symptoms=[], fail_per_min=60, checkout_rps=1,
                         horizon_min=5, restart_downtime_s=3, history={}, units="result lookups")
    assert "result lookups keep failing" in next(o for o in opts if o.action == "wait").note


def test_evidence_and_prompt_name_the_sites_entry_and_demand_signal():
    s = results_settings()
    snap = Snapshot(ts=0, user=UserSample(checkout_rps=900.0))
    pack = build_evidence("VGL-1", [], {}, snap, {}, {}, [], normal_checkout_rps=40.0,
                          entry=s.user_facing, demand_signal=s.demand_signal, units=s.units)
    item = pack["evidence"][0]
    assert (item["service"], item["signal"], item["baseline"]) == ("web", "result_lookup_attempts_per_s", 40.0)
    prompt = system_prompt(s.user_facing, s.demand_signal)
    assert "when web result_lookup_attempts_per_s is far above" in prompt
    assert "storefront" not in prompt and "checkout" not in prompt


def test_the_demo_prompt_is_unchanged():
    assert "when storefront checkout_attempts_per_s is far above its baseline" in SYSTEM_PROMPT


# ---- allowed fixes are a hard rule

def test_a_fix_the_file_forbids_is_blocked_even_if_a_human_approves():
    gov = Governor(autonomy="L3", allowed={"auth": frozenset(), "pdf-render": frozenset({"restart"})})
    restart_auth = Option("restart", "auth", 0.9, 100, 1, 99, "")
    scale_pdf = Option("scale_out", "pdf-render", 0.9, 100, 0, 100, "")
    restart_pdf = Option("restart", "pdf-render", 0.9, 100, 1, 99, "")
    assert "vigil.yaml does not allow restart on auth" in gov.blockers(restart_auth, 0, True)
    assert "vigil.yaml does not allow scale_out on pdf-render" in gov.blockers(scale_pdf, 0, True)
    assert gov.blockers(restart_pdf, 0, True) == []
    assert gov.permits(Option("wait", None, 0, 0, 0, 0, ""))
    assert gov.permits(Option("restart", "unlisted", 0.9, 1, 1, 0, ""))   # not in the file: all fixes


def test_engine_never_runs_a_forbidden_fix_and_says_why(tmp_path):
    """inventory is stuck and a restart is the right fix, but vigil.yaml forbids it:
    VIGIL shows the option as not allowed, does nothing it may not do, and hands
    the incident to a human with the reason."""
    import asyncio
    from test_remediation import kinds, setup
    world, eng, store, act, inc_id = setup(tmp_path, allowed_actions={"inventory": frozenset({"rollback"})})
    plan = eng.remediator._plan(inc_id)
    restart = next(o for o in plan["options"] if o.action == "restart" and o.target == "inventory")
    assert not restart.executable and "not allowed on inventory by vigil.yaml" in restart.note
    assert plan["chosen"] is None
    asyncio.run(eng.remediator.handle(inc_id))
    assert act.calls == []
    assert store.get_incident(inc_id)["status"] == "ESCALATED"
    esc = next(e for e in store.events(incident_id=inc_id) if e["kind"] == "escalated")
    assert "the best fix, restart inventory, is not allowed on inventory by vigil.yaml" in esc["summary"]
    assert "no safe automatic fix" not in esc["summary"]


def test_a_bad_autonomy_in_env_is_refused(monkeypatch):
    monkeypatch.setenv("VIGIL_AUTONOMY", "L9")
    with pytest.raises(ConfigError, match="VIGIL_AUTONOMY"):
        load_settings(str(RESULTS))
