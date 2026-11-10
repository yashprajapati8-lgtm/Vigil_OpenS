"""Detect: is this service behaving unlike its own normal, persistently?

Per service, VIGIL learns a baseline from clean traffic, then flags a tick when:

    (Isolation Forest score above the calibrated cut AND some symptom z > 3)
    OR some symptom z > 6
    OR the service is down
    OR memory will hit its limit within the leak horizon

An incident opens only after 3 flagged ticks out of the last 4, and clears only
after 4 clean ticks in a row (hysteresis), so a single noisy sample never acts.

Robust z = (x - median) / scale, where scale = max(1.4826 * MAD, floors). The
floors stop a perfectly flat baseline (error rate always 0) from turning the
first stray error into z = infinity.

Adapting. Real traffic changes: it grows, a deploy shifts latency a little, a busy
day looks different from a quiet one. A baseline frozen after 3 minutes would
slowly turn into false alarms or missed faults. So after the first fit VIGIL keeps
a rolling window of the last 2 hours of healthy ticks and re-learns from it every
10 minutes, under four rules:

    1. Only healthy ticks are learned: never a flagged tick, never during an open
       incident, a surge or failing checkouts, and not for 2 minutes after an
       incident (the ticks just before it are dropped too).
    2. Normal moves at most 1 sigma per re-learn, so no single window can jump it.
    3. Latency, errors and CPU per request may rise at most 3 sigma above
       what a human last accepted. Past that VIGIL holds normal at the cap and
       reports "baseline drift": a slow creep is a human's call, never silently
       the new normal.
    4. Memory growth is never re-learned. Normal memory growth is zero; learning
       a slow leak would hide it.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import IsolationForest

from .collector import ServiceSample
from .config import Settings

log = logging.getLogger("vigil.detector")

MODEL_VERSION = 3
MIN_SAMPLES = 24
# Memory that sits higher than normal but has stopped growing is not a failure:
# after a burst of traffic Python keeps what it allocated. Memory counts as a
# symptom only while it grows, or once it passes this share of the limit.
MEM_ALARM_SHARE = 0.7

# signal -> (absolute floor, relative floor as a share of the median).
# Only the bad direction counts: faster responses are never an incident.
SYMPTOMS: dict[str, tuple[float, float]] = {
    "p95_ms": (5.0, 0.25),
    "error_rate": (0.005, 0.0),
    "cpu_pct": (3.0, 0.15),
    "mem_mb": (2.0, 0.05),
    "mem_slope_mb_s": (0.2, 0.0),
}
# Context signals are scored and shown, but never open an incident on their own:
# more traffic is not a failure. They feed diagnosis (load vs stuck process).
CONTEXT: dict[str, tuple[float, float]] = {
    "rps": (1.0, 0.10),
    "cpu_per_req": (0.05, 0.15),
}
ALL_SIGNALS = {**SYMPTOMS, **CONTEXT}

# Adapting (see the module docstring). Never re-learned: normal memory growth is zero.
FROZEN = {"mem_slope_mb_s"}
# May rise only so far above what a human accepted before it is reported as drift.
# Left out: CPU, which rightly doubles when traffic doubles (CPU *per request* is
# guarded instead, so a stuck process still cannot creep in), and memory, which
# counts only while it grows (see MEM_ALARM_SHARE; leaks are caught by growth rate).
DRIFT_GUARDED = {"p95_ms", "error_rate", "cpu_per_req"}
SCALE_STEP = 1.25            # spread may change at most 25% per re-learn...
SCALE_RANGE = (0.67, 1.5)    # ...and stay within this share of the accepted spread

LABELS = {
    "p95_ms": "latency p95",
    "error_rate": "error rate",
    "cpu_pct": "CPU",
    "mem_mb": "memory",
    "mem_slope_mb_s": "memory growth",
    "rps": "traffic",
    "cpu_per_req": "CPU per request",
}


def _fmt(sig: str, x: float) -> str:
    if sig == "p95_ms":
        return f"{x / 1000:.1f} s" if x >= 1000 else f"{x:.0f} ms"
    if sig == "error_rate":
        return f"{x:.1%}" if x < 0.1 else f"{x:.0%}"
    if sig == "cpu_pct":
        return f"{x:.0f}%"
    if sig == "mem_mb":
        return f"{x:.0f} MB"
    if sig == "mem_slope_mb_s":
        return f"{x:+.1f} MB/s"
    if sig == "rps":
        return f"{x:.0f} req/s"
    return f"{x:.2f}"


def describe(sig: str, value: float, normal: float) -> str:
    """'latency p95 1.5 s (normal 14 ms)': numbers people read at a glance, not sigmas."""
    return f"{LABELS[sig]} {_fmt(sig, value)} (normal {_fmt(sig, normal)})"


@dataclass
class Baseline:
    service: str
    medians: dict[str, float]
    scales: dict[str, float]
    forest: IsolationForest
    cut: float
    train_scores: np.ndarray            # sorted, for "more extreme than X% of normal"
    n_samples: int
    created_at: float
    version: int = MODEL_VERSION
    # What a human last accepted as normal (the first fit, or an accepted drift).
    anchor_medians: dict[str, float] = field(default_factory=dict)
    anchor_scales: dict[str, float] = field(default_factory=dict)
    updated_at: float = 0.0                 # last re-learn (0: never re-learned)
    refits: int = 0
    drift: dict[str, str] = field(default_factory=dict)   # signal -> plain-English warning

    def z(self, sample: ServiceSample) -> dict[str, float]:
        out = {}
        for sig in ALL_SIGNALS:
            value = getattr(sample, sig)
            if value is not None and sig in self.medians:
                out[sig] = (value - self.medians[sig]) / self.scales[sig]
        return out

    def score(self, z: dict[str, float]) -> float:
        vec = np.array([[z.get(sig, 0.0) for sig in SYMPTOMS]])
        return float(-self.forest.score_samples(vec)[0])  # higher = more anomalous

    def percentile(self, score: float) -> float:
        return float(np.searchsorted(self.train_scores, score) / len(self.train_scores) * 100)


def _robust_stats(samples: list[ServiceSample]) -> tuple[dict[str, float], dict[str, float]]:
    """Median and robust spread per signal, with the floors applied."""
    medians, scales = {}, {}
    for sig, (abs_floor, rel_floor) in ALL_SIGNALS.items():
        values = np.array([getattr(s, sig) for s in samples if getattr(s, sig) is not None], dtype=float)
        if len(values) < MIN_SAMPLES // 2:
            continue
        med = float(np.median(values))
        mad = float(np.median(np.abs(values - med)))
        medians[sig] = med
        scales[sig] = max(1.4826 * mad, abs_floor, rel_floor * abs(med))
    return medians, scales


def _fit_forest(samples: list[ServiceSample], medians: dict[str, float],
                scales: dict[str, float]) -> tuple[IsolationForest, float, np.ndarray]:
    """Train the forest on z-vectors; calibrate the cut on a held-out tail."""
    def zvec(s: ServiceSample) -> list[float]:
        row = []
        for sig in SYMPTOMS:
            v = getattr(s, sig)
            row.append(0.0 if v is None or sig not in medians else (v - medians[sig]) / scales[sig])
        return row

    X = np.array([zvec(s) for s in samples])
    split = max(int(len(X) * 0.7), MIN_SAMPLES // 2)
    train, held = X[:split], X[split:]
    forest = IsolationForest(n_estimators=100, contamination="auto", random_state=7).fit(train)
    train_scores = np.sort(-forest.score_samples(train))
    held_scores = -forest.score_samples(held) if len(held) else np.array([])
    # The cut sits above everything normal we have seen, including data the
    # forest never trained on. Contamination never decides it.
    cut = float(max(np.quantile(train_scores, 0.995), held_scores.max() if len(held_scores) else 0.0))
    all_scores = np.sort(-forest.score_samples(X))
    return forest, cut, all_scores


def fit_baseline(service: str, samples: list[ServiceSample]) -> Baseline:
    """Learn normal from clean samples. This first fit is also the accepted normal."""
    medians, scales = _robust_stats(samples)
    forest, cut, all_scores = _fit_forest(samples, medians, scales)
    return Baseline(service, medians, scales, forest, cut, all_scores, len(samples), time.time(),
                    anchor_medians=dict(medians), anchor_scales=dict(scales))


def _clamp(x: float, lo: float, hi: float) -> float:
    return min(max(x, lo), hi)


def refit_baseline(old: Baseline, samples: list[ServiceSample], settings: Settings,
                   now: float | None = None) -> Baseline:
    """Re-learn normal from recent healthy samples, moving it only within the rules
    in the module docstring. Returns a new Baseline; `old` is left untouched."""
    now = time.time() if now is None else now
    fresh_med, fresh_scale = _robust_stats(samples)
    medians, scales, drift = {}, {}, {}
    anchor_med, anchor_scale = dict(old.anchor_medians), dict(old.anchor_scales)

    for sig in set(old.medians) | set(fresh_med):
        abs_floor, rel_floor = ALL_SIGNALS[sig]
        if sig not in old.medians:
            # A signal seen for the first time (e.g. no errors were ever measured
            # before because there was no traffic): learn it as is, accept it as normal.
            medians[sig], scales[sig] = fresh_med[sig], fresh_scale[sig]
            anchor_med.setdefault(sig, fresh_med[sig])
            anchor_scale.setdefault(sig, fresh_scale[sig])
            continue
        m0, s0 = old.medians[sig], old.scales[sig]
        if sig in FROZEN or sig not in fresh_med:
            medians[sig], scales[sig] = m0, s0
            continue

        # Rule 2: at most one step per re-learn.
        step = settings.adapt_step_z * s0
        m = _clamp(fresh_med[sig], m0 - step, m0 + step)
        sc = _clamp(fresh_scale[sig], s0 / SCALE_STEP, s0 * SCALE_STEP)

        # Rule 3: a rise past the cap is a human's call. Falling is always fine:
        # a lower normal only makes detection stricter.
        am = anchor_med.get(sig, m0)
        a_sc = anchor_scale.get(sig, s0)
        if sig in DRIFT_GUARDED:
            cap = am + settings.drift_max_z * a_sc
            if m > cap:
                m = cap
            if fresh_med[sig] > cap:
                drift[sig] = (f"{LABELS[sig]} keeps rising: accepted normal {_fmt(sig, am)}, "
                              f"recent healthy traffic {_fmt(sig, fresh_med[sig])}. "
                              f"Held at {_fmt(sig, cap)} until someone accepts the change")
            sc = _clamp(sc, a_sc * SCALE_RANGE[0], a_sc * SCALE_RANGE[1])

        medians[sig] = m
        scales[sig] = max(sc, abs_floor, rel_floor * abs(m))

    forest, cut, all_scores = _fit_forest(samples, medians, scales)
    return Baseline(old.service, medians, scales, forest, cut, all_scores, len(samples), old.created_at,
                    anchor_medians=anchor_med, anchor_scales=anchor_scale,
                    updated_at=now, refits=old.refits + 1, drift=drift)


@dataclass
class Verdict:
    service: str
    state: str                      # learning | healthy | anomalous | down | unknown
    flagged: bool = False
    hard: bool = False              # unambiguous (down, imminent OOM): skip persistence
    reasons: list[str] = field(default_factory=list)
    z: dict[str, float] = field(default_factory=dict)
    score_pct: float | None = None  # more extreme than X% of normal
    learning_progress: float = 1.0
    learning_eta_s: float | None = None   # while learning: seconds until the baseline is ready
    time_to_oom_s: float | None = None
    texts: dict[str, str] = field(default_factory=dict)  # human wording per signal: value vs normal
    drift: dict[str, str] = field(default_factory=dict)  # baseline drift waiting for a human (see Adapting)


class Persistence:
    """Open on 3 of 4 flagged ticks when something is hurting (checkouts failing,
    errors, failing calls); on 10 of 12 when nothing is. Hard signals (down,
    imminent OOM) open at once. 4 clean ticks in a row clear."""

    def __init__(self, settings: Settings):
        self.s = settings
        self.recent: deque[bool] = deque(maxlen=max(settings.open_window, settings.open_window_quiet))
        self.hurting: deque[bool] = deque(maxlen=settings.open_window)
        self.clean_streak = 0
        self.active = False

    def update(self, flagged: bool, hard: bool = False, impact: bool = True, allow_quiet: bool = True) -> str | None:
        self.recent.append(flagged)
        self.hurting.append(flagged and impact)
        self.clean_streak = 0 if flagged else self.clean_streak + 1
        quiet = list(self.recent)[-self.s.open_window_quiet:]
        if not self.active and (hard or sum(self.hurting) >= self.s.open_hits
                                or (allow_quiet and sum(quiet) >= self.s.open_hits_quiet)):
            self.active = True
            return "opened"
        if self.active and self.clean_streak >= self.s.close_clean:
            self.active = False
            self.recent.clear()
            self.hurting.clear()
            return "cleared"
        return None


class ServiceDetector:
    def __init__(self, service: str, settings: Settings, baseline: Baseline | None = None):
        self.service = service
        self.s = settings
        self.baseline = baseline
        self.learning: list[ServiceSample] = []
        self.learning_started: float | None = None
        self.persistence = Persistence(settings)
        # Adapting: recent healthy ticks, and when to re-learn from them.
        tick = max(settings.tick_seconds, 0.001)
        self.window: deque[ServiceSample] = deque(maxlen=max(int(settings.adapt_window_s / tick), MIN_SAMPLES))
        self.every_ticks = max(int(settings.adapt_every_s / tick), 1)
        self.min_window = max(int(settings.adapt_min_s / tick), MIN_SAMPLES)
        self.hold_ticks = max(int(settings.adapt_hold_s / tick), 0)
        self.ticks_since_refit = 0
        self.hold = 0                    # ticks left before learning may resume
        self.pausing = False             # inside an incident (or its hold)

    @property
    def model_path(self) -> Path:
        return self.s.model_dir / f"{self.service}.joblib"

    # ---- baseline lifecycle

    def load(self) -> bool:
        try:
            bl = joblib.load(self.model_path)
        except FileNotFoundError:
            return False
        except Exception as exc:  # corrupt or incompatible file: relearn, never crash
            log.warning("discarding unreadable baseline for %s: %s", self.service, exc)
            return False
        version = getattr(bl, "version", None)
        if version == 2:
            # Learned before adapting existed: what it learned is the accepted normal.
            bl.anchor_medians, bl.anchor_scales = dict(bl.medians), dict(bl.scales)
            bl.updated_at, bl.refits, bl.drift = 0.0, 0, {}
            bl.version = MODEL_VERSION
        elif version != MODEL_VERSION:
            return False
        self.baseline = bl
        return True

    def save(self) -> None:
        self.s.model_dir.mkdir(parents=True, exist_ok=True)
        joblib.dump(self.baseline, self.model_path)

    def reset(self) -> None:
        self.baseline = None
        self.learning.clear()
        self.learning_started = None
        self.window.clear()
        self.ticks_since_refit = 0
        self.hold = 0
        self.pausing = False
        self.model_path.unlink(missing_ok=True)

    def _learn(self, sample: ServiceSample, now: float) -> Verdict:
        if self.learning_started is None:
            self.learning_started = now
        if sample.up and sample.rps:
            self.learning.append(sample)
        elapsed = now - self.learning_started
        time_progress = 1.0 if self.s.baseline_seconds <= 0 else elapsed / self.s.baseline_seconds
        progress = min(time_progress, len(self.learning) / MIN_SAMPLES, 1.0)
        if progress >= 1.0:
            self.baseline = fit_baseline(self.service, self.learning)
            self.window.extend(self.learning)    # the first healthy window seeds adapting
            self.ticks_since_refit = 0
            self.learning.clear()
            self.save()
            log.info("baseline learned for %s from %d samples, cut=%.3f",
                     self.service, self.baseline.n_samples, self.baseline.cut)
            return Verdict(self.service, "healthy")
        # Done when both the time window and the sample count are met; the slower one decides.
        eta = max(0.0, self.s.baseline_seconds - elapsed, (MIN_SAMPLES - len(self.learning)) * self.s.tick_seconds)
        return Verdict(self.service, "learning", learning_progress=round(progress, 3), learning_eta_s=round(eta))

    def pause_learning(self) -> None:
        """Called every tick while an incident is open: never learn from a failure.
        The ticks just before it may already have been drifting, so they are dropped,
        and learning stays off for adapt_hold_s after the incident ends."""
        self.learning.clear()
        self.learning_started = None
        if not self.pausing:
            for _ in range(min(self.hold_ticks, len(self.window))):
                self.window.pop()
            self.pausing = True
        self.hold = self.hold_ticks + 1   # +1: the evaluate() of this same tick counts one down

    def accept_drift(self) -> list[str]:
        """A human says the current normal is right: it becomes the accepted normal,
        and the drift cap is measured from here. Returns the signals that were drifting."""
        bl = self.baseline
        if bl is None:
            return []
        accepted = sorted(bl.drift)
        bl.anchor_medians, bl.anchor_scales = dict(bl.medians), dict(bl.scales)
        bl.drift = {}
        self.save()
        return accepted

    def _adapt(self, sample: ServiceSample, v: Verdict, learn: bool) -> None:
        """Keep a healthy tick for the next re-learn, and re-learn when due."""
        if not self.s.adapt or self.baseline is None:
            return
        self.ticks_since_refit += 1
        if self.hold > 0:
            self.hold -= 1
            if self.hold == 0:
                self.pausing = False
            return
        if learn and not v.flagged and v.state == "healthy" and sample.rps:
            self.window.append(sample)
        if self.ticks_since_refit >= self.every_ticks and len(self.window) >= self.min_window:
            self.baseline = refit_baseline(self.baseline, list(self.window), self.s)
            self.ticks_since_refit = 0
            self.save()
            log.info("baseline re-learned for %s from %d healthy samples (refit %d)%s", self.service,
                     self.baseline.n_samples, self.baseline.refits,
                     f"; drift: {', '.join(self.baseline.drift)}" if self.baseline.drift else "")

    # ---- per tick

    def evaluate(self, sample: ServiceSample | None, now: float | None = None, learn: bool = True) -> Verdict:
        """Judge one tick. `learn=False` when the engine knows the system is not in a
        normal state (surge, failing checkouts, scaled out): judge it, never learn it."""
        now = time.time() if now is None else now
        if sample is None or sample.up is None:
            return Verdict(self.service, "unknown", reasons=["no data"])
        if sample.up is False:
            return Verdict(self.service, "down", flagged=True, hard=True, reasons=["service down"])
        if self.baseline is None:
            return self._learn(sample, now)

        bl = self.baseline
        z = bl.z(sample)
        score = bl.score(z)
        v = Verdict(self.service, "healthy", z={k: round(val, 2) for k, val in z.items()})
        v.score_pct = round(bl.percentile(score), 1)

        for sig in z:
            value = getattr(sample, sig)
            v.texts[sig] = describe(sig, value, bl.medians[sig])
        bad = {sig: val for sig, val in z.items() if sig in SYMPTOMS and val > self.s.z_flag}
        if ("mem_mb" in bad and z.get("mem_slope_mb_s", 0.0) <= self.s.z_flag
                and (sample.mem_mb or 0.0) < MEM_ALARM_SHARE * self.s.memory_limit_mb):
            bad.pop("mem_mb")
        extreme = {sig: val for sig, val in bad.items() if val > self.s.z_extreme}
        if extreme or (bad and score > bl.cut):
            v.flagged = True
            for sig, val in sorted(bad.items(), key=lambda kv: -kv[1]):
                v.reasons.append(v.texts[sig])

        # Predict a leak before it becomes a crash.
        slope, mem = sample.mem_slope_mb_s, sample.mem_mb
        if slope and mem and slope > 0 and z.get("mem_slope_mb_s", 0) > 4:
            tte = (self.s.memory_limit_mb - mem) / slope
            if 0 < tte < self.s.leak_horizon_s:
                v.time_to_oom_s = round(tte)
                v.flagged, v.hard = True, True
                v.reasons.append(f"memory limit in ~{tte:.0f}s")

        v.state = "anomalous" if v.flagged else "healthy"
        v.drift = dict(bl.drift)
        self._adapt(sample, v, learn)
        return v
