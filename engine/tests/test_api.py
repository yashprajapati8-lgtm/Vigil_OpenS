"""The operator lock: every human action needs the admin token, checked in constant time."""

from fastapi.testclient import TestClient

from vigil import api


def client(monkeypatch, token: str = "s3cret") -> TestClient:
    monkeypatch.setattr(api, "ADMIN_TOKEN", token)
    # No `with`: the lifespan (Prometheus collector, engine loop) is not started.
    return TestClient(api.app)


def test_operator_check_accepts_the_right_token(monkeypatch):
    r = client(monkeypatch).post("/api/operator/check", headers={"X-Vigil-Token": "s3cret"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_operator_check_rejects_wrong_or_missing_token(monkeypatch):
    c = client(monkeypatch)
    assert c.post("/api/operator/check", headers={"X-Vigil-Token": "nope"}).status_code == 403
    assert c.post("/api/operator/check").status_code == 403


def test_no_configured_token_locks_every_action(monkeypatch):
    c = client(monkeypatch, token="")
    assert c.post("/api/operator/check", headers={"X-Vigil-Token": ""}).status_code == 403
    assert c.post("/api/control", json={"paused": True}).status_code == 403
    assert c.post("/api/incidents/VGL-0001/approve").status_code == 403


def test_actions_check_the_token_before_touching_the_engine(monkeypatch):
    c = client(monkeypatch)
    # Engine is not running here: a bad token must still be a 403, never a 503 that leaks state.
    assert c.post("/api/incidents/VGL-0001/reject", headers={"X-Vigil-Token": "x"}).status_code == 403
    assert c.post("/api/baseline/reset", headers={"X-Vigil-Token": "x"}).status_code == 403
