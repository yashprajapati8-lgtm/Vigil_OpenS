import numpy as np
import pytest

from vigil.collector import ServiceSample
from vigil.config import Settings
from vigil.detector import Persistence, ServiceDetector, fit_baseline

from sim import ServiceSim

TICKS_PER_MIN = 12


def trained(service: str, tmp_path, seed: int = 1, minutes: int = 3) -> tuple[ServiceDetector, ServiceSim]:
    s = Settings(data_dir=tmp_path, baseline_seconds=minutes * 60)
    sim = ServiceSim(service, seed=seed)
    det = ServiceDetector(service, s)
    t = 0.0
    while det.baseline is None:
        det.evaluate(sim.sample(), now=t)
        t += 5
    return det, sim


def run(det: ServiceDetector, sim: ServiceSim, ticks: int) -> list[str]:
    events = []
    for _ in range(ticks):
        v = det.evaluate(sim.sample())
        ev = det.persistence.update(v.flagged, v.hard)
        if ev:
            events.append(ev)
    return events


@pytest.mark.parametrize("service", ["payment", "inventory"])
@pytest.mark.parametrize("seed", range(5))
def test_no_phantom_incidents_in_30_clean_minutes(tmp_path, service, seed):
    det, sim = trained(service, tmp_path, seed=seed)
    assert run(det, sim, 30 * TICKS_PER_MIN) == []


def test_slow_dependency_opens_within_three_ticks(tmp_path):
    det, sim = trained("inventory", tmp_path)
    run(det, sim, 20)
    sim.latency_add_ms = 1500
    events = []
    for tick in range(1, 7):
        v = det.evaluate(sim.sample())
        if det.persistence.update(v.flagged, v.hard) == "opened":
            break
    assert tick == 3
    assert any("latency" in r for r in v.reasons)


def test_recovery_clears_after_four_clean_ticks(tmp_path):
    det, sim = trained("inventory", tmp_path)
    sim.latency_add_ms = 1500
    assert "opened" in run(det, sim, 4)
    sim.latency_add_ms = 0
    clean = 0
    for clean in range(1, 10):
        v = det.evaluate(sim.sample())
        if det.persistence.update(v.flagged, v.hard) == "cleared":
            break
    assert clean == 4


def test_leak_is_predicted_before_the_crash(tmp_path):
    det, sim = trained("payment", tmp_path)
    sim.leak_mb_s = 3.0
    for _ in range(30):
        v = det.evaluate(sim.sample())
        if v.time_to_oom_s:
            break
    assert v.time_to_oom_s is not None and v.time_to_oom_s < 120
    assert sim.mem < 256, "must fire before memory reaches the limit"


def test_flash_sale_traffic_alone_is_not_flagged_but_saturation_is(tmp_path):
    det, sim = trained("payment", tmp_path)
    sim.load_x = 1.3  # mild growth: context only
    assert run(det, sim, 24) == []
    sim.load_x = 4.0  # real saturation: CPU climbs well past normal
    assert "opened" in run(det, sim, 6)


def test_down_service_opens_immediately(tmp_path):
    det, sim = trained("payment", tmp_path)
    sim.down = True
    v = det.evaluate(sim.sample())
    assert v.state == "down" and det.persistence.update(v.flagged, v.hard) == "opened"


def test_flat_zero_error_baseline_does_not_explode(tmp_path):
    samples = [ServiceSample("x", up=True, rps=20, error_rate=0.0, p95_ms=20 + i % 3, cpu_pct=8,
                             mem_mb=50, mem_slope_mb_s=0.0, cpu_per_req=0.4) for i in range(40)]
    bl = fit_baseline("x", samples)
    z = bl.z(ServiceSample("x", up=True, rps=20, error_rate=0.001, p95_ms=21, cpu_pct=8,
                           mem_mb=50, mem_slope_mb_s=0.0, cpu_per_req=0.4))
    assert np.isfinite(z["error_rate"]) and z["error_rate"] < 1


def test_baseline_survives_restart(tmp_path):
    det, _ = trained("payment", tmp_path)
    fresh = ServiceDetector("payment", det.s)
    assert fresh.load() and fresh.baseline.cut == pytest.approx(det.baseline.cut)


def test_persistence_ignores_isolated_spikes():
    p = Persistence(Settings())
    pattern = [True, False, False, True, False, False, True, False]
    assert [p.update(f) for f in pattern] == [None] * len(pattern)


def test_learning_reports_progress_and_time_left(tmp_path):
    from vigil.config import Settings
    from vigil.detector import ServiceDetector
    from sim import ServiceSim
    s = Settings(data_dir=tmp_path, baseline_seconds=180)
    det, sim = ServiceDetector("payment", s), ServiceSim("payment")
    t = 1000.0
    v = det.evaluate(sim.sample(), now=t)
    assert v.state == "learning" and v.learning_eta_s == 180
    v = det.evaluate(sim.sample(), now=t + 60)
    assert 0 < v.learning_progress < 1 and v.learning_eta_s == 120
