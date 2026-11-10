"""All tunables in one place, read from the environment with safe defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _i(name: str, default: int) -> int:
    return int(os.getenv(name, default))


@dataclass(frozen=True)
class Settings:
    prometheus_url: str = os.getenv("PROMETHEUS_URL", "http://prometheus:9090")
    data_dir: Path = Path(os.getenv("VIGIL_DATA_DIR", "/data"))

    tick_seconds: float = _f("VIGIL_TICK_SECONDS", 5)
    rate_window: str = os.getenv("VIGIL_RATE_WINDOW", "20s")
    query_timeout_s: float = _f("VIGIL_QUERY_TIMEOUT_S", 3)
    # A sample older than this many ticks means telemetry is stale: no decisions.
    stale_after_ticks: int = _i("VIGIL_STALE_AFTER_TICKS", 3)

    # Services VIGIL watches for anomalies. The storefront is the user's view:
    # VIGIL measures impact and verifies recovery there, but never acts on it.
    monitored: tuple[str, ...] = tuple(os.getenv("VIGIL_SERVICES", "payment,inventory").split(","))
    user_facing: str = os.getenv("VIGIL_USER_FACING", "storefront")
    # Calls VIGIL knows about without metrics (the storefront has no edge metrics).
    static_edges: tuple[tuple[str, str], ...] = (("storefront", "payment"),)

    # Baseline learning.
    baseline_seconds: int = _i("VIGIL_BASELINE_SECONDS", 180)
    min_rps_for_rates: float = _f("VIGIL_MIN_RPS", 0.5)

    # Detection rule (see detector.py).
    z_flag: float = _f("VIGIL_Z_FLAG", 3.0)
    z_extreme: float = _f("VIGIL_Z_EXTREME", 6.0)
    open_window: int = _i("VIGIL_OPEN_WINDOW", 4)       # look at the last 4 ticks
    open_hits: int = _i("VIGIL_OPEN_HITS", 3)           # 3 of them flagged -> open
    close_clean: int = _i("VIGIL_CLOSE_CLEAN", 4)       # 4 clean in a row -> clear
    # Without impact (no failing checkouts, no errors, no failing calls), a slowdown
    # must persist: 10 of the last 12 ticks (~1 min). Filters host-wide blips such as
    # a CPU-heavy docker build, which slows everything for ~20 s and hurts no one.
    open_window_quiet: int = _i("VIGIL_OPEN_WINDOW_QUIET", 12)
    open_hits_quiet: int = _i("VIGIL_OPEN_HITS_QUIET", 10)
    memory_limit_mb: float = _f("VIGIL_MEMORY_LIMIT_MB", 256)
    leak_horizon_s: float = _f("VIGIL_LEAK_HORIZON_S", 120)

    history_points: int = _i("VIGIL_HISTORY_POINTS", 720)  # 1 h of 5 s ticks for charts

    # Diagnosis.
    diagnose_delay_s: float = _f("VIGIL_DIAGNOSE_DELAY_S", 10)   # let connected symptoms join first
    edge_bad_fail_ratio: float = _f("VIGIL_EDGE_BAD_FAIL_RATIO", 0.05)
    edge_bad_p95_ms: float = _f("VIGIL_EDGE_BAD_P95_MS", 500)
    change_window_s: float = _f("VIGIL_CHANGE_WINDOW_S", 600)    # a deploy this recent is a suspect
    groq_api_key: str = field(default_factory=lambda: os.getenv("GROQ_API_KEY", ""), repr=False)
    groq_model: str = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
    llm_timeout_s: float = _f("VIGIL_LLM_TIMEOUT_S", 10)

    # Decide, act, verify.
    actuator_url: str = os.getenv("ACTUATOR_URL", "http://actuator:8000")
    actuator_token: str = field(default_factory=lambda: os.getenv("ACTUATOR_TOKEN", ""), repr=False)
    autonomy: str = os.getenv("VIGIL_AUTONOMY", "L2")
    horizon_min: float = _f("VIGIL_HORIZON_MIN", 5)
    restart_downtime_s: float = _f("VIGIL_RESTART_DOWNTIME_S", 3)
    verify_settle_s: float = _f("VIGIL_VERIFY_SETTLE_S", 10)
    verify_timeout_s: float = _f("VIGIL_VERIFY_TIMEOUT_S", 90)
    verify_clean_ticks: int = _i("VIGIL_VERIFY_CLEAN_TICKS", 4)
    recovered_fail_ratio: float = _f("VIGIL_RECOVERED_FAIL_RATIO", 0.02)
    scale_in_load_ratio: float = _f("VIGIL_SCALE_IN_LOAD_RATIO", 1.5)   # traffic within 1.5x normal...
    scale_in_calm_ticks: int = _i("VIGIL_SCALE_IN_CALM_TICKS", 12)     # ...for 1 min -> stop the replica
    rollback_ready_s: float = _f("VIGIL_ROLLBACK_READY_S", 40)   # previous version must be healthy by then
    # No fix known and no checkout failing: keep watching this long before paging a human.
    watch_without_impact_s: float = _f("VIGIL_WATCH_WITHOUT_IMPACT_S", 180)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "vigil.sqlite3"

    @property
    def model_dir(self) -> Path:
        return self.data_dir / "models"


SETTINGS = Settings()
