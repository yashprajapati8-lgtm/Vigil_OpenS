"""The actuator refuses everything except typed actions on labeled containers."""

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "actuator"))


class FakeContainer:
    def __init__(self, name, labels):
        self.name, self.labels, self.status = name, labels, "running"
        self.attrs = {"State": {"Health": {"Status": "healthy"}, "StartedAt": "now"}}
        self.calls = []

    def restart(self, timeout):
        self.calls.append("restart")

    def start(self):
        self.calls.append("start")

    def stop(self, timeout):
        self.calls.append("stop")

    def reload(self):
        pass


class FakeDocker:
    def __init__(self, containers):
        self.by_name = {c.name: c for c in containers}
        outer = self

        class Containers:
            def get(self, name):
                from docker.errors import NotFound
                if name not in outer.by_name:
                    raise NotFound("nope")
                return outer.by_name[name]

            def list(self, all, filters):
                return [c for c in outer.by_name.values() if c.labels.get("vigil.managed") == "true"]

        self.containers = Containers()


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("ACTUATOR_TOKEN", "secret-token")
    mod = importlib.reload(importlib.import_module("actuator"))
    inv = FakeContainer("inventory", {"vigil.managed": "true", "vigil.service": "inventory"})
    gen = FakeContainer("loadgen", {"vigil.managed": "false"})
    mod._client = FakeDocker([inv, gen])
    return TestClient(mod.app), inv, gen


H = {"X-Actuator-Token": "secret-token"}


def test_restart_managed_container(api):
    client, inv, _ = api
    r = client.post("/actions", headers=H, json={"action_id": "a1b2c3d4e5", "action": "restart", "container": "inventory"})
    assert r.status_code == 200 and inv.calls == ["restart"]


def test_replayed_action_id_does_not_act_twice(api):
    client, inv, _ = api
    body = {"action_id": "same-id-123", "action": "restart", "container": "inventory"}
    client.post("/actions", headers=H, json=body)
    r = client.post("/actions", headers=H, json=body)
    assert r.json()["replayed"] is True and inv.calls == ["restart"]


@pytest.mark.parametrize("body,headers,code", [
    ({"action_id": "a1b2c3d4e5", "action": "restart", "container": "loadgen"}, H, 403),      # not managed
    ({"action_id": "a1b2c3d4e5", "action": "exec", "container": "inventory"}, H, 400),        # not allowed
    ({"action_id": "a1b2c3d4e5", "action": "restart", "container": "inventory"}, {}, 403),    # no token
    ({"action_id": "a1b2c3d4e5", "action": "restart", "container": "ghost"}, H, 404),         # unknown
    ({"action_id": "a1b2c3d4e5", "action": "restart", "container": "inv; rm -rf /"}, H, 422),  # injection
])
def test_refusals(api, body, headers, code):
    client, inv, gen = api
    assert client.post("/actions", headers=headers, json=body).status_code == code
    assert inv.calls == [] and gen.calls == []


def test_lists_only_managed(api):
    client, _, _ = api
    names = [c["name"] for c in client.get("/containers", headers=H).json()["containers"]]
    assert names == ["inventory"]
