"""scripts/vigil_init.py writes a vigil.yaml VIGIL accepts, a Prometheus config that
labels every target with its service, and a private .env; it refuses bad answers."""

import importlib.util
import json
from pathlib import Path

import yaml

from vigil.config import load_settings

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("vigil_init", ROOT / "scripts" / "vigil_init.py")
vigil_init = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vigil_init)

RESULTS_PORTAL = {
    "site": "University results portal",
    "entry": "web", "entry_address": "web:8000", "entry_about": "Where students check results",
    "unit": "result lookup", "units": "result lookups",
    "metric": "result_requests_total", "duration_metric": "result_request_duration_seconds",
    "ok": ["ok", "not_found"], "failed": ["failed", "timeout"],
    "entry_calls": ["results-api"],
    "services": [
        {"name": "results-api", "address": "results-api:8000", "about": "Looks up a student's marks",
         "calls": ["auth", "pdf-render"], "allow": ["restart", "rollback", "scale_out"]},
        {"name": "auth", "about": "Checks roll number and date of birth", "allow": []},
        {"name": "pdf-render", "address": "pdf:9000", "about": "Renders the marksheet PDF", "allow": ["restart", "scale_out"]},
    ],
    "network": "portal_default", "autonomy": "L1",
}


def run(tmp_path, answers, *extra):
    f = tmp_path / "answers.json"
    f.write_text(json.dumps(answers))
    return vigil_init.main(["--answers", str(f), "--out", str(tmp_path / "deploy"), *extra])


def test_writes_the_same_site_as_the_hand_written_example(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("VIGIL_AUTONOMY", raising=False)
    assert run(tmp_path, RESULTS_PORTAL) == 0
    made = load_settings(str(tmp_path / "deploy" / "vigil.yaml"))
    example = load_settings(str(ROOT / "examples" / "results-portal.yaml"))
    for f in ("site_name", "user_facing", "monitored", "static_edges", "unit", "units", "user_metric", "user_ok",
              "user_failed", "user_duration_metric", "allowed_actions", "about", "autonomy"):
        assert getattr(made, f) == getattr(example, f), f


def test_prometheus_scrapes_every_service_with_its_label(tmp_path):
    run(tmp_path, RESULTS_PORTAL)
    prom = yaml.safe_load((tmp_path / "deploy" / "prometheus.yml").read_text())
    assert prom["global"]["scrape_interval"] == "5s"
    jobs = {j["job_name"]: j["static_configs"][0] for j in prom["scrape_configs"]}
    assert set(jobs) == {"web", "results-api", "auth", "pdf-render"}
    assert jobs["pdf-render"]["targets"] == ["pdf:9000"]
    assert jobs["auth"]["targets"] == ["auth:8000"]                     # default address
    assert all(c["labels"] == {"service": name} for name, c in jobs.items())


def test_env_holds_a_token_that_is_never_printed(tmp_path, capsys):
    run(tmp_path, RESULTS_PORTAL)
    env = dict(line.split("=", 1) for line in (tmp_path / "deploy" / ".env").read_text().splitlines()
               if line and not line.startswith("#"))
    assert env["APP_NETWORK"] == "portal_default"
    assert len(env["VIGIL_ADMIN_TOKEN"]) >= 30
    out = capsys.readouterr().out
    assert env["VIGIL_ADMIN_TOKEN"] not in out
    assert 'vigil.service: results-api' in out and "vigil.role: replica" in out and "rollback" in out


def test_a_second_run_keeps_the_token_and_needs_force(tmp_path):
    run(tmp_path, RESULTS_PORTAL)
    token = (tmp_path / "deploy" / ".env").read_text()
    assert run(tmp_path, RESULTS_PORTAL) == 1                            # files exist: refused
    assert run(tmp_path, RESULTS_PORTAL, "--force") == 0
    assert (tmp_path / "deploy" / ".env").read_text() == token           # never rotated by surprise


def test_bad_answers_write_nothing(tmp_path, capsys):
    bad = json.loads(json.dumps(RESULTS_PORTAL))
    bad["services"][0]["calls"].append("student-db")                      # not a watched service
    assert run(tmp_path, bad) == 2
    assert "services.results-api.calls: 'student-db' is not under services" in capsys.readouterr().err
    assert not (tmp_path / "deploy" / "vigil.yaml").exists()
    bad = dict(RESULTS_PORTAL, autonomy="L5")
    assert run(tmp_path, bad) == 2


def test_interactive_answers_follow_the_call_graph(tmp_path, monkeypatch):
    """Typing the answers: services the entry calls are asked about first, then what they call."""
    replies = iter([
        "Shop", "storefront", "", "", "checkout", "", "", "", "", "",   # site, entry, address, about, unit..., ok, failed
        "payment",                                                       # the entry calls
        "", "Charges the card", "inventory", "restart, scale_out",       # payment
        "", "", "", "",                                                  # inventory: address, about, calls, allow (default)
        "",                                                              # no other service
        "shop_default", "L2",
    ])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(replies))
    a = vigil_init.interview()
    assert [s["name"] for s in a["services"]] == ["payment", "inventory"]
    assert a["services"][0]["calls"] == ["inventory"] and a["services"][0]["allow"] == ["restart", "scale_out"]
    assert a["services"][1]["allow"] == ["restart"]
    assert a["metric"] == "checkout_requests_total" and a["units"] == "checkouts"
    assert a["autonomy"] == "L2"
